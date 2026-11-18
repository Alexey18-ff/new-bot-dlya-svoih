import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")  # https://your-domain
PORT = int(os.getenv("PORT", "8080"))

CAFE_NAME = os.getenv("CAFE_NAME", "Для Своих")
CAFE_PHONE = os.getenv("CAFE_PHONE", "+375447600041")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@")  # для прямой связи (необязательно)
MIN_DELIVERY = float(os.getenv("MIN_DELIVERY", "7"))
CURRENCY = os.getenv("CURRENCY", "руб.")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Minsk"))

# DEV_MODE=1: можно открыть приложение в обычном браузере без Telegram (для тестов)
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "1"))
DB_PATH = os.getenv("DB_PATH", "cafe.db")
