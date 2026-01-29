# admin_database.py - УПРОЩЕННАЯ ВЕРСИЯ
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from database import Base, Session, engine
from datetime import datetime

class AdminLog(Base):
    __tablename__ = 'admin_logs'
    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    details = Column(String)
    timestamp = Column(DateTime, default=datetime.now)

# Создаем таблицу при запуске
try:
    Base.metadata.create_all(engine)
    print("✅ Таблицы базы данных созданы/проверены")
except Exception as e:
    print(f"⚠️ Ошибка создания таблиц: {e}")

def log_admin_action(admin_id: int, action: str, details: str = None):
    """Логирование действий администратора"""
    try:
        with Session() as session:
            log = AdminLog(
                admin_id=admin_id,
                action=action,
                details=details,
                timestamp=datetime.now()
            )
            session.add(log)
            session.commit()
    except Exception as e:
        print(f"⚠️ Ошибка логирования: {e}")