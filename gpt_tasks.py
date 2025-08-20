# gpt_tasks.py
import json
import re
import base64
import mimetypes
import backoff
import logging
import asyncio
from pathlib import Path
from typing import Any, Dict

from config import settings
from openai import OpenAI

logger = logging.getLogger(__name__)
client = OpenAI(api_key=settings.OPENAI_API_KEY, base_url=str(settings.OPENAI_BASE_URL))


# ----------------- helpers -----------------
def _clamp_deposit(v: int, lo: int = 500, hi: int = 100_000) -> int:
    try:
        v = int(v)
    except Exception:
        v = lo
    return max(lo, min(hi, v))


def _parse_money_to_int(val: Any) -> int:
    """
    Принимает int/str вида "6 500 ₽", "6500", "≈7000 руб." и т.п.
    Возвращает целое число (без валюты).
    """
    if isinstance(val, int):
        return val
    s = str(val or "")
    digits = re.findall(r"\d+", s)
    if not digits:
        return 0
    try:
        return int("".join(digits))
    except Exception:
        return 0


def _safe_json_extract(text: str) -> Dict[str, Any]:
    """
    Пытается распарсить JSON. Если не получилось — вытаскивает первую {...} скобку.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Попытка выдрать первый JSON-объект
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


# ----------------- task generation -----------------
@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def generate_gpt_task(program: str | None = None) -> str:
    """Генерирует короткое задание для спортзала, подтверждаемое фото."""
    prompt = """
Придумай простое задание для спортзала, которое можно подтвердить фото.
Условия:
1) Один вид инвентаря.
2) Только одна деталь.
3) Формат: "Сделай [действие] с [инвентарь] + [деталь]".
4) Избегай сложных формулировок.
5) Примеры:
   - "Фото гантелей"
   - "Фото жима штанги"
6) Одно короткое предложение.
""".strip()
    if program:
        prompt += f"\nУчитывай программу тренировок пользователя: {program}"

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text.replace("*", "").replace("_", "").strip()
    except Exception as e:
        logger.exception("Ошибка обращения к GPT (generate_gpt_task): %s", e)
        return "Фото гантелей"


# ----------------- photo verify -----------------
@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def verify_task_with_gpt(task_text: str, image_path: str | Path) -> dict:
    """
    Проверка фото с помощью GPT.
    image_path — путь к JPG/PNG файлу.
    Возвращает СТРОГО словарь с ключами: success (bool), is_home (bool), reason (str)
    """
    try:
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Файл {image_path} не найден.")

        mime_type, _ = mimetypes.guess_type(image_path)
        if mime_type not in {"image/jpeg", "image/png"}:
            raise ValueError(f"Неподдерживаемый формат файла: {mime_type}")

        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        sys_text = (
            "Ты спортивный тренер-верификатор. Проверь, соответствует ли фото заданию и сделано ли оно прямо сейчас. "
            "Запрещены монтаж/скрин/редактирование. Также оцени локацию: ДОМ/не дом. "
            "Ответ В СТРОГОМ JSON без пояснений: "
            '{"success": true|false, "is_home": true|false, "reason": "краткое объяснение"}'
        )

        user_text = (
            f"Задание: {task_text}\n"
            "Оцени:\n"
            "1) Выполняется ли упражнение (не селфи поза/демонстрация инвентаря)?\n"
            "2) Нет ли явных признаков монтажа/скриншота?\n"
            "3) Локация — домашняя (комната/квартира) vs коммерческий зал/публичный фитнес-центр.\n"
            "Верни ровно JSON объект."
        )

        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[
                {"role": "system", "content": [{"type": "text", "text": sys_text}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}},
                    ],
                },
            ],
            temperature=0,
            max_tokens=300,
        )

        result_text = (resp.choices[0].message.content or "").strip()
        data = _safe_json_extract(result_text)

        success = bool(data.get("success"))
        is_home = bool(data.get("is_home"))
        reason = str(data.get("reason") or "")

        # Если модель не вернула is_home — считаем неизвестным (False) для строгой логики в handlers.py
        return {"success": success, "is_home": is_home, "reason": reason}

    except Exception as e:
        logger.exception("Ошибка проверки у GPT (verify_task_with_gpt): %s", e)
        return {"success": False, "is_home": False, "reason": "Ошибка верификации"}


# ----------------- deposit recommendation -----------------
@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def recommend_deposit_with_gpt(profile: dict) -> dict:
    """
    Ожидается ответ строго вида:
    {"deposit": 5000, "reason": "Короткое объяснение (<=200 симв.)"}

    Где deposit — целое число в диапазоне [500; 100000].
    """
    try:
        system = (
            "Ты — ассистент тренера. По анкете предложи размер залога в рублях, чтобы поддержать дисциплину. "
            "Учти цель/мотивацию/самооценку дисциплины, частоту и длительность тренировок, стоимость прошлой программы. "
            "Верни СТРОГО JSON без пояснений: "
            '{"deposit": <int 500..100000>, "reason": "<=200 символов, краткое объяснение>"}'
        )
        user = (
            "Анкета пользователя в JSON ниже. Если какие-то поля отсутствуют, делай здравое допущение.\n\n"
            + json.dumps(profile, ensure_ascii=False)
        )

        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=250,
        )

        raw = (resp.choices[0].message.content or "").strip()
        data = _safe_json_extract(raw)

        dep_raw = data.get("deposit")
        reason_raw = data.get("reason")

        dep = _parse_money_to_int(dep_raw)
        dep = _clamp_deposit(dep)

        reason = (str(reason_raw or "").strip())[:200] or "ИИ-рекомендация по анкете"

        # sanity: если модель промахнулась с цифрой — эвристика
        if dep < 500:
            raise ValueError("Bad deposit from model")

        return {"deposit": dep, "reason": reason}

    except Exception as e:
        # --------- Fallback эвристика ---------
        logger.exception("recommend_deposit_with_gpt failed, using heuristic: %s", e)
        # Базовая ставка
        dep = 5000
        why_parts = []

        # дисциплина
        try:
            sr = str(profile.get("self_rate") or "").lower()
            if any(k in sr for k in ["низ", "плохо", "2", "3"]):
                dep += 1500
                why_parts.append("низкая дисциплина")
            elif any(k in sr for k in ["выс", "хорош", "8", "9", "10"]):
                dep -= 500
                why_parts.append("высокая дисциплина")
        except Exception:
            pass

        # длительность/частота
        try:
            per_day_duration = (profile.get("per_day_duration") or {}) if isinstance(profile, dict) else {}
            if per_day_duration:
                avg = sum(int(v) for v in per_day_duration.values()) / max(1, len(per_day_duration))
                if avg >= 75:
                    dep += 1000
                    why_parts.append("длительные тренировки")
                elif avg <= 30:
                    dep -= 500
                    why_parts.append("короткие тренировки")
            # количество дней
            days = (profile.get("per_day_time") or {}) if isinstance(profile, dict) else {}
            freq = len(days)
            if freq >= 5:
                dep += 1000
                why_parts.append("высокая частота")
            elif freq <= 2:
                dep -= 500
                why_parts.append("редкая частота")
        except Exception:
            pass

        # цена прошлой программы
        try:
            pp = _parse_money_to_int(profile.get("program_price"))
            if pp >= 10000:
                dep += 1000
                why_parts.append("дорогая прошлая программа")
            elif 0 < pp < 2000:
                dep -= 500
                why_parts.append("дешёвая прошлая программа")
        except Exception:
            pass

        dep = _clamp_deposit(dep)
        reason = ("Резервная эвристика: " + ", ".join(why_parts) if why_parts else "Резервная эвристика (ИИ недоступен)")[:200]
        return {"deposit": dep, "reason": reason}
