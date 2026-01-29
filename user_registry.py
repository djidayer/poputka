# user_registry.py
import logging
import os
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, ContextTypes, TypeHandler, ApplicationHandlerStop

from sqlalchemy import Column, Integer, String, DateTime, Boolean, text
from sqlalchemy.exc import SQLAlchemyError

from database import Base, Session, engine

logger = logging.getLogger(__name__)


class BotUser(Base):
    __tablename__ = "bot_users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)

    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)

    is_bot = Column(Boolean, default=False)
    chat_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)

    # Фильтр поиска (персистентно)
    search_filter_enabled = Column(Boolean, default=False)
    search_filter_departure = Column(String, nullable=True)
    search_filter_destination = Column(String, nullable=True)

    # Уведомления о новых поездках
    trips_notify_enabled = Column(Boolean, default=False)

    # Бан пользователей
    is_banned = Column(Boolean, default=False)
    banned_until = Column(DateTime, nullable=True)
    ban_reason = Column(String, nullable=True)
    banned_at = Column(DateTime, nullable=True)
    banned_by = Column(Integer, nullable=True)


def _ensure_schema() -> None:
    """Мягкая миграция: добавляем недостающие колонки в bot_users."""
    with engine.connect() as conn:
        # SQLite: проверяем PRAGMA table_info
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(bot_users)")).fetchall()}
        alters = []
        if "search_filter_enabled" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN search_filter_enabled BOOLEAN DEFAULT 0")
        if "search_filter_departure" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN search_filter_departure VARCHAR")
        if "search_filter_destination" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN search_filter_destination VARCHAR")
        if "trips_notify_enabled" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN trips_notify_enabled BOOLEAN DEFAULT 0")
        if "is_banned" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN is_banned BOOLEAN DEFAULT 0")
        if "banned_until" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN banned_until DATETIME")
        if "ban_reason" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN ban_reason VARCHAR")
        if "banned_at" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN banned_at DATETIME")
        if "banned_by" not in cols:
            alters.append("ALTER TABLE bot_users ADD COLUMN banned_by INTEGER")

        for stmt in alters:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        conn.commit()


def init_user_table() -> None:
    """Создаёт таблицу bot_users (если её нет) и делает мягкую миграцию."""
    Base.metadata.create_all(engine)
    try:
        _ensure_schema()
    except Exception as e:
        logger.warning("schema ensure failed: %s", e)


def upsert_user(update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    chat = update.effective_chat
    now = datetime.utcnow()

    try:
        with Session() as session:
            existing = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
            if existing is None:
                existing = BotUser(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    is_bot=bool(getattr(user, "is_bot", False)),
                    chat_id=chat.id if chat else None,
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(existing)
            else:
                existing.username = user.username
                existing.first_name = user.first_name
                existing.last_name = user.last_name
                existing.is_bot = bool(getattr(user, "is_bot", False))
                existing.chat_id = chat.id if chat else existing.chat_id
                existing.last_seen_at = now

            session.commit()
    except SQLAlchemyError as e:
        logger.error("upsert_user failed: %s", e)


def _get_admin_ids() -> set[int]:
    """Список админов из ADMIN_USER_ID (поддерживает список через запятую)."""
    raw = os.getenv("ADMIN_USER_ID", "") or ""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


async def ban_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Блокирует забаненных пользователей на уровне всех апдейтов."""
    user = update.effective_user
    if not user:
        return

    # Админов не баним этим механизмом (чтобы не заблокировать себе доступ).
    if user.id in _get_admin_ids():
        return

    # На всякий случай гарантируем, что пользователь существует в БД
    try:
        upsert_user(update)
    except Exception:
        pass

    now = datetime.utcnow()
    try:
        with Session() as session:
            u = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
            if not u or not getattr(u, "is_banned", False):
                return

            # Авто-разбан по истечению срока
            if u.banned_until and u.banned_until <= now:
                u.is_banned = False
                u.banned_until = None
                u.ban_reason = None
                u.banned_at = None
                u.banned_by = None
                session.commit()
                return

            # Ограничим частоту уведомлений, чтобы забаненный не мог "ддосить" ответами
            last_map = context.bot_data.setdefault("ban_notice_ts", {})
            last_ts = last_map.get(user.id)
            if not last_ts or (now - last_ts).total_seconds() >= 60:
                last_map[user.id] = now

                reason = (u.ban_reason or "").strip()
                until_txt = "навсегда" if not u.banned_until else u.banned_until.strftime("%Y-%m-%d %H:%M UTC")
                msg = "🚫 Вы заблокированы"
                if reason:
                    msg += f"\nПричина: {reason}"
                msg += f"\nСрок: {until_txt}"

                # Если это callback — лучше алерт
                if update.callback_query:
                    try:
                        await update.callback_query.answer(msg, show_alert=True)
                    except Exception:
                        pass
                else:
                    try:
                        # по возможности удаляем сообщение пользователя, чтобы чат оставался чистым
                        if update.message:
                            await update.message.delete()
                    except Exception:
                        pass
                    try:
                        if update.effective_chat:
                            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
                    except Exception:
                        pass

            raise ApplicationHandlerStop

    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logger.error("ban_guard failed: %s", e)

async def capture_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает на любой update, ничего не отвечает, только пишет/обновляет пользователя."""
    try:
        upsert_user(update)
    except Exception as e:
        logger.error("capture_update failed: %s", e)


def register(app: Application) -> None:
    """Подключает модуль к приложению."""
    init_user_table()

    # ВАЖНО: ban_guard должен срабатывать максимально рано, чтобы забаненный
    # пользователь не мог пройти ни в какие другие handler'ы.
    # В PTB handlers обрабатываются по group по возрастанию: чем меньше число,
    # тем раньше. Поэтому ставим ban_guard в более раннюю группу, чем любые
    # прочие роутеры/хендлеры.
    app.add_handler(TypeHandler(Update, ban_guard), group=-10)

    # Затем — обычная регистрация/обновление пользователя.
    app.add_handler(TypeHandler(Update, capture_update), group=-9)
