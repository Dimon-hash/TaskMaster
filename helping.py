from datetime import datetime
from zoneinfo import ZoneInfo
from config import settings

TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))
now = datetime.now(TZ)

# Получаем название и номер дня
weekday_name = now.strftime("%A")  # Monday, Tuesday...
weekday_number = now.weekday()     # 0=Monday, 6=Sunday

print(f"Сейчас {weekday_name} (номер {weekday_number})")
