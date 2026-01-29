# database.py
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, Float, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime
from enum import Enum as PyEnum

class BookingStatus(PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

Base = declarative_base()

class Trip(Base):
    __tablename__ = 'trips'
    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, nullable=False)
    driver_name = Column(String, nullable=False)
    departure_point = Column(String, nullable=False)
    destination_point = Column(String, nullable=False)
    date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)
    time_mode = Column(String, nullable=True)
    seats_available = Column(Integer, nullable=False)
    price = Column(Float, nullable=True)
    car_info = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    bookings = relationship('Booking', back_populates='trip')

    # ====== Оценки/статистика (кэш, опционально) ======
    driver_rating_avg = Column(Float, nullable=True)      # средняя оценка водителя по этой поездке (если захотите считать)
    driver_rating_count = Column(Integer, nullable=True)  # кол-во оценок водителя
    passenger_rating_avg = Column(Float, nullable=True)   # средняя оценка пассажиров (если водитель оценивает пассажиров)
    passenger_rating_count = Column(Integer, nullable=True)

class Booking(Base):
    __tablename__ = 'bookings'
    id = Column(Integer, primary_key=True)
    trip_id = Column(Integer, ForeignKey('trips.id'))
    passenger_id = Column(Integer, nullable=False)
    passenger_name = Column(String, nullable=False)
    seats_booked = Column(Integer, nullable=False)
    booking_time = Column(DateTime, default=datetime.datetime.utcnow)
    passenger_request_msg_id = Column(Integer, nullable=True)
    status = Column(String, default=BookingStatus.PENDING.value)
    trip = relationship('Trip', back_populates='bookings')

    # ====== Оценка/результат со стороны пассажира ======
    passenger_trip_result = Column(String, nullable=True)        # "completed" / "not_completed"
    passenger_rating_driver = Column(Integer, nullable=True)     # 1..5
    passenger_rating_comment = Column(String, nullable=True)
    passenger_rated_at = Column(DateTime, nullable=True)

    # ====== Оценка со стороны водителя ======
    driver_rating_passenger = Column(Integer, nullable=True)     # 1..5
    driver_rating_comment = Column(String, nullable=True)
    driver_rated_at = Column(DateTime, nullable=True)


class AdminLog(Base):
    __tablename__ = 'admin_logs'
    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    details = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

# Создаем базу данных SQLite
engine = create_engine('sqlite:///carpool.db')

def _get_existing_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table_name});")).fetchall()
    # row format: (cid, name, type, notnull, dflt_value, pk)
    return {r[1] for r in rows}

def _ensure_column(conn, table: str, column: str, ddl_type: str):
    existing = _get_existing_columns(conn, table)
    if column in existing:
        return
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type};"))

def ensure_schema():
    """
    Мягкая миграция: добавляет недостающие колонки (SQLite).
    Без Alembic, без удаления/пересоздания таблиц.
    """
    with engine.begin() as conn:
        # bookings
        _ensure_column(conn, "bookings", "passenger_trip_result", "TEXT")
        _ensure_column(conn, "bookings", "passenger_rating_driver", "INTEGER")
        _ensure_column(conn, "bookings", "passenger_rating_comment", "TEXT")
        _ensure_column(conn, "bookings", "passenger_rated_at", "DATETIME")

        _ensure_column(conn, "bookings", "driver_rating_passenger", "INTEGER")
        _ensure_column(conn, "bookings", "driver_rating_comment", "TEXT")
        _ensure_column(conn, "bookings", "driver_rated_at", "DATETIME")

        # trips интервальное время
        _ensure_column(conn, "trips", "end_date", "DATETIME")
        _ensure_column(conn, "trips", "time_mode", "TEXT")
        # backfill end_date for existing rows
        conn.execute(text("UPDATE trips SET end_date = date WHERE end_date IS NULL"))

        # trips (кэш)
        _ensure_column(conn, "trips", "driver_rating_avg", "REAL")
        _ensure_column(conn, "trips", "driver_rating_count", "INTEGER")
        _ensure_column(conn, "trips", "passenger_rating_avg", "REAL")
        _ensure_column(conn, "trips", "passenger_rating_count", "INTEGER")

        # bot_users
        _ensure_column(conn, "bot_users", "search_filter_enabled", "INTEGER")
        _ensure_column(conn, "bot_users", "search_filter_departure", "TEXT")
        _ensure_column(conn, "bot_users", "search_filter_destination", "TEXT")
        _ensure_column(conn, "bookings", "passenger_request_msg_id", "INTEGER")

# Создаем все таблицы (если их еще нет)
try:
    Base.metadata.create_all(engine)
    ensure_schema()
    print("✅ Таблицы базы данных созданы/проверены + схема обновлена")
except Exception as e:
    print(f"⚠️ Ошибка создания/обновления таблиц: {e}")

Session = sessionmaker(bind=engine)

def log_admin_action(admin_id: int, action: str, details: str = None):
    try:
        with Session() as session:
            log = AdminLog(
                admin_id=admin_id,
                action=action,
                details=details,
                timestamp=datetime.datetime.utcnow()
            )
            session.add(log)
            session.commit()
    except Exception as e:
        print(f"⚠️ Ошибка логирования: {e}")
