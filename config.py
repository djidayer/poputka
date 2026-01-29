# config.py - Конфигурационный файл
import os
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Токен бота из переменных окружения
TOKEN = os.getenv('BOT_TOKEN', '')

# Настройки базы данных
DB_PATH = os.getenv('DB_PATH', 'sqlite:///carpool.db')

# Настройки часового пояса
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '8'))

# Настройки очистки старых данных
CLEANUP_OLD_TRIPS_DAYS = int(os.getenv('CLEANUP_OLD_TRIPS_DAYS', '7'))

# TTL для неподтверждённых бронирований (PENDING), в минутах
PENDING_BOOKING_TTL_MINUTES = int(os.getenv('PENDING_BOOKING_TTL_MINUTES', '15'))

# ID администратора
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID', '')