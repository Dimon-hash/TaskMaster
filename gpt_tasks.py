import json
import base64
import mimetypes
import backoff
import logging
from config import settings
from openai import OpenAI
from pathlib import Path

logger = logging.getLogger(__name__)
client = OpenAI(api_key=settings.OPENAI_API_KEY, base_url=str(settings.OPENAI_BASE_URL))


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def generate_gpt_task():
    """Генерирует короткое задание для спортзала, подтверждаемое фото."""
    prompt = (
        """
        Придумай простое задание для спортзала, которое можно подтвердить фото.
        Условия:
        1. Один вид инвентаря.
        2. Только одна деталь .
        3. Формат: "Сделай [действие] с [инвентарь] + [деталь]".
        4. Избегай сложных формулировок.
        5. Примеры:
           - "Фото гантелей"
           - "Фото жима штанги"
        6. Одно короткое предложение.
        """
    )
    if training_program:
        prompt += f"\nУчитывай программу тренировок пользователя: {training_program}."
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        return text.replace("*", "").replace("_", "").strip()
    except Exception as e:
        logger.exception("Ошибка обращения к GPT: %s", e)
        return "Фото гантелей"


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def verify_task_with_gpt(task_text: str, image_path: str | Path) -> dict:
    """
    Проверка фото с помощью GPT.
    image_path — путь к JPG/PNG файлу.
    """
    try:
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Файл {image_path} не найден.")

        # Определяем MIME-тип (по расширению файла)
        mime_type, _ = mimetypes.guess_type(image_path)
        if mime_type not in {"image/jpeg", "image/png"}:
            raise ValueError(f"Неподдерживаемый формат файла: {mime_type}")

        # Кодируем фото в base64
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Ты спортивный тренер. Твоя задача — проверить, соответствует ли фото заданию "
                            "и действительно ли оно сделано вживую, а не смонтировано. "
                            "Фото должно быть оригинальным, без признаков монтажа, подмены лица, "
                            "использования чужого изображения, экрана телефона или монитора. "
                            "Ответ верни строго в формате JSON: {\"success\": bool, \"reason\": string}."
                           )
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Задание: {task_text}. Соответствует ли фото?"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                ]
            }
        ]

        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0,
            max_tokens=300
        )

        result_text = resp.choices[0].message.content.strip()

        # Пробуем распарсить JSON
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            logger.error("GPT вернул некорректный JSON: %s", result_text)
            return {"success": False, "reason": "Некорректный ответ GPT"}

    except Exception as e:
        logger.exception("Ошибка проверки у GPT: %s", e)
        return {"success": False, "reason": "Ошибка верификации"}
