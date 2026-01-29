# admin_handlers.py
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop
from sqlalchemy import func

from database import Session, Trip, Booking, BookingStatus
from user_registry import BotUser
import broadcast

from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# --- Admin logs (правильный импорт + fallback) ---
try:
    from admin_database import AdminLog, log_admin_action
except Exception:
    AdminLog = None

    def log_admin_action(admin_id: int, action: str, details: str = ""):
        try:
            logger.warning("Admin logging fallback: %s | %s | %s", admin_id, action, details)
        except Exception:
            pass


# =========================
# Helpers: admin access
# =========================
def get_admin_ids() -> list[int]:
    """Получает список ID администраторов из переменной окружения ADMIN_USER_ID (поддерживает список через запятую)."""
    admin_ids_str = os.getenv("ADMIN_USER_ID", "")
    if not admin_ids_str:
        return []

    ids: list[int] = []
    for part in admin_ids_str.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()


def admin_only(func_handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else None

        if not user_id or not is_admin(user_id):
            # молча игнорируем
            return

        # Логируем сам факт вызова админ-функции (без деталей)
        try:
            log_admin_action(admin_id=user_id, action=func_handler.__name__, details="")
        except Exception:
            pass

        return await func_handler(update, context, *args, **kwargs)

    return wrapper


# =========================
# Router for admin text
# =========================
async def admin_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловит текст админа для рассылки и админ-команд (бан/разбан).
    ВАЖНО: этот хендлер должен стоять ПЕРЕД обычными текстовыми хендлерами.
    """
    try:
        user = update.effective_user
        if not user or not is_admin(user.id):
            return

        # --- Broadcast text ---
        if broadcast.is_broadcast_waiting_text(context):
            await broadcast.handle_broadcast_text(update, context)
            raise ApplicationHandlerStop  # стопаем дальнейшие handlers

        # --- Ban / Unban text input ---
        if context.user_data.get("admin_state") == "ban_waiting":
            mode = context.user_data.get("admin_ban_mode")
            text_in = (update.message.text or "").strip() if update.message else ""
            if not text_in:
                return

            parts = text_in.split()
            try:
                target_id = int(parts[0])
            except Exception:
                await update.message.reply_text("❗️Нужно указать user_id числом. Пример: 123456789 7d спам")
                raise ApplicationHandlerStop

            now = datetime.utcnow()

            if mode == "unban":
                with Session() as session:
                    u = session.query(BotUser).filter(BotUser.telegram_id == target_id).one_or_none()
                    if not u:
                        await update.message.reply_text("Пользователь не найден в базе.")
                    else:
                        u.is_banned = False
                        u.banned_until = None
                        u.ban_reason = None
                        u.banned_at = None
                        u.banned_by = None
                        session.commit()
                        await update.message.reply_text(f"✅ Пользователь {target_id} разбанен.")
                context.user_data.pop("admin_state", None)
                context.user_data.pop("admin_ban_mode", None)
                raise ApplicationHandlerStop

            # mode == ban
            dur = None
            reason = ""
            if len(parts) >= 2:
                # если второй токен похож на срок — парсим, иначе считаем это причиной
                try:
                    dur = _parse_ban_duration(parts[1])
                    reason = " ".join(parts[2:]).strip()
                except Exception:
                    dur = None
                    reason = " ".join(parts[1:]).strip()
            banned_until = None if dur is None else now + dur

            with Session() as session:
                u = session.query(BotUser).filter(BotUser.telegram_id == target_id).one_or_none()
                if not u:
                    # создаём запись, чтобы бан работал
                    u = BotUser(
                        telegram_id=target_id,
                        username=None,
                        first_name=None,
                        last_name=None,
                        is_bot=False,
                        chat_id=None,
                        created_at=now,
                        last_seen_at=now,
                    )
                    session.add(u)

                u.is_banned = True
                u.banned_until = banned_until
                u.ban_reason = reason or None
                u.banned_at = now
                u.banned_by = user.id
                session.commit()

            until_txt = "навсегда" if not banned_until else banned_until.strftime("%Y-%m-%d %H:%M UTC")
            await update.message.reply_text(f"🚫 Пользователь {target_id} забанен. Срок: {until_txt}")
            context.user_data.pop("admin_state", None)
            context.user_data.pop("admin_ban_mode", None)
            raise ApplicationHandlerStop

    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logger.exception("Ошибка в admin_text_router: %s", e)

def _admin_main_kb() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("🚗 Поездки", callback_data="admin_trips"),
            InlineKeyboardButton("🧾 Логи", callback_data="admin_logs"),
        ],
        [
            InlineKeyboardButton("🚫 Баны", callback_data="admin_bans"),
            InlineKeyboardButton("📣 Рассылка", callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("❌ Выход", callback_data="admin_exit"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _admin_bans_kb() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🚫 Забанить", callback_data="admin_ban_prompt"),
         InlineKeyboardButton("✅ Разбанить", callback_data="admin_unban_prompt")],
        [InlineKeyboardButton("📄 Список банов", callback_data="admin_bans_list")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


@admin_only
async def admin_bans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚫 *Баны пользователей*\n\n"
        "Выберите действие:"
    )
    reply_markup = _admin_bans_kb()

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


def _parse_ban_duration(token: str):
    """Парсит 15m/2h/7d. Возвращает timedelta или None (perma)."""
    token = (token or "").strip().lower()
    if not token or token in ("perma", "perm", "forever", "навсегда"):
        return None
    m = re.fullmatch(r"(\d+)([mhd])", token)
    if not m:
        raise ValueError("Неверный формат срока. Примеры: 15m, 2h, 7d, perma")
    val = int(m.group(1))
    unit = m.group(2)
    if val <= 0:
        raise ValueError("Срок должен быть > 0")
    if unit == "m":
        return timedelta(minutes=val)
    if unit == "h":
        return timedelta(hours=val)
    if unit == "d":
        return timedelta(days=val)
    raise ValueError("Неверная единица времени")


@admin_only
async def admin_bans_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session() as session:
        items = (
            session.query(BotUser)
            .filter(BotUser.is_banned == True)  # noqa: E712
            .order_by(BotUser.banned_at.desc().nullslast(), BotUser.id.desc())
            .limit(30)
            .all()
        )

    if not items:
        text = "✅ Сейчас нет забаненных пользователей."
    else:
        lines = ["🚫 *Список банов (последние 30)*\n"]
        for u in items:
            until_txt = "навсегда" if not u.banned_until else u.banned_until.strftime("%Y-%m-%d %H:%M UTC")
            reason = (u.ban_reason or "").strip() or "—"
            lines.append(f"• `{u.telegram_id}` — {until_txt} — {reason}")
        text = "\n".join(lines)

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_bans")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


@admin_only
async def admin_ban_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["admin_state"] = "ban_waiting"
    context.user_data["admin_ban_mode"] = "ban"
    text = (
        "🚫 *Бан пользователя*\n\n"
        "Отправьте одной строкой:\n"
        "`<user_id> [срок] [причина]`\n\n"
        "Примеры:\n"
        "`123456789 perma спам`\n"
        "`123456789 7d реклама`\n"
        "`123456789 12h токсичность`\n\n"
        "Срок: `perma` или `15m/2h/7d`"
    )
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_bans")]])
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


@admin_only
async def admin_unban_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["admin_state"] = "ban_waiting"
    context.user_data["admin_ban_mode"] = "unban"
    text = (
        "✅ *Разбан пользователя*\n\n"
        "Отправьте одной строкой:\n"
        "`<user_id>`"
    )
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_bans")]])
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

# =========================
# Admin screens
# =========================
@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔧 *Админ-панель*\n\n"
        "Выберите раздел:"
    )
    reply_markup = _admin_main_kb()

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


@admin_only
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика: поездки по дате поездки + бронирования по времени бронирования (сегодня/7д/30д)."""
    with Session() as session:
        try:
            now = datetime.now()

            # Границы "сегодня" (локальное время сервера)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)

            # Скользящие периоды
            week_start = now - timedelta(days=7)
            month_start = now - timedelta(days=30)

            # --- Trips: считаем по Trip.date (это дата/время поездки) ---
            trips_today = session.query(Trip).filter(
                Trip.date >= today_start,
                Trip.date < today_end
            ).count()

            trips_7d = session.query(Trip).filter(
                Trip.date >= week_start,
                Trip.date <= now
            ).count()

            trips_30d = session.query(Trip).filter(
                Trip.date >= month_start,
                Trip.date <= now
            ).count()

            # --- Bookings: считаем по Booking.booking_time (время бронирования) ---
            bookings_today = session.query(Booking).filter(
                Booking.booking_time >= today_start,
                Booking.booking_time < today_end
            ).count()

            bookings_7d = session.query(Booking).filter(
                Booking.booking_time >= week_start,
                Booking.booking_time <= now
            ).count()

            bookings_30d = session.query(Booking).filter(
                Booking.booking_time >= month_start,
                Booking.booking_time <= now
            ).count()

            # --- (Опционально) общие итоги для справки ---
            total_users = session.query(BotUser).count()
            total_trips = session.query(Trip).count()
            total_bookings = session.query(Booking).count()

            divider = "═" * 30
            text = (
                "📊 *Статистика активности*\n"
                f"{divider}\n\n"
                f"👥 Пользователей всего: `{total_users}`\n\n"
                "🚗 *Поездки (по дате поездки)*\n"
                f"• Сегодня: `{trips_today}`\n"
                f"• 7 дней: `{trips_7d}`\n"
                f"• 30 дней: `{trips_30d}`\n"
                f"• Всего в базе: `{total_trips}`\n\n"
                "🎫 *Бронирования (по времени бронирования)*\n"
                f"• Сегодня: `{bookings_today}`\n"
                f"• 7 дней: `{bookings_7d}`\n"
                f"• 30 дней: `{bookings_30d}`\n"
                f"• Всего в базе: `{total_bookings}`\n\n"
                f"🕒 Обновлено: {now.strftime('%d.%m.%Y %H:%M')}"
            )

            keyboard = [
                [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=reply_markup
                )

        except Exception as e:
            logger.exception("admin_stats error")  # полный traceback в консоли
            msg = "❌ Ошибка получения статистики."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)

@admin_only
async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователи: общее число + активность + топы."""
    with Session() as session:
        try:
            now = datetime.utcnow()
            day_ago = now - timedelta(hours=24)
            week_ago = now - timedelta(days=7)

            total_users = session.query(BotUser).count()
            active_24h = session.query(BotUser).filter(BotUser.last_seen_at >= day_ago).count()
            active_7d = session.query(BotUser).filter(BotUser.last_seen_at >= week_ago).count()

            top_drivers = (
                session.query(
                    Trip.driver_id,
                    Trip.driver_name,
                    func.count(Trip.id).label("trips_count"),
                )
                .group_by(Trip.driver_id, Trip.driver_name)
                .order_by(func.count(Trip.id).desc())
                .limit(10)
                .all()
            )

            top_passengers = (
                session.query(
                    Booking.passenger_id,
                    Booking.passenger_name,
                    func.count(Booking.id).label("bookings_count"),
                )
                .group_by(Booking.passenger_id, Booking.passenger_name)
                .order_by(func.count(Booking.id).desc())
                .limit(10)
                .all()
            )

            text = "👥 *Пользователи*\n"
            text += "═" * 30 + "\n\n"
            text += f"• Всего пользователей: `{total_users}`\n"
            text += f"• Активны за 24ч: `{active_24h}`\n"
            text += f"• Активны за 7д: `{active_7d}`\n\n"

            text += "🏆 *Топ активности*\n\n"
            text += "🚗 *Топ водителей:*\n"
            if top_drivers:
                for i, (driver_id, driver_name, trips_count) in enumerate(top_drivers, 1):
                    name = driver_name or "Без имени"
                    text += f"{i}. {name} (ID: `{driver_id}`) — {trips_count} поездок\n"
            else:
                text += "— нет данных\n"

            text += "\n👤 *Топ пассажиров:*\n"
            if top_passengers:
                for i, (pid, pname, cnt) in enumerate(top_passengers, 1):
                    name = pname or "Без имени"
                    text += f"{i}. {name} (ID: `{pid}`) — {cnt} бронирований\n"
            else:
                text += "— нет данных\n"

            keyboard = [
                [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
                [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)

        except Exception as e:
            logger.error("admin_users error: %s", e)
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Ошибка загрузки пользователей.")
            else:
                await update.message.reply_text("❌ Ошибка загрузки пользователей.")


@admin_only
async def admin_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран управления поездками (минимальный, но рабочий)."""
    keyboard = [
        [InlineKeyboardButton("🧹 Очистить старые поездки", callback_data="admin_cleanup_trips")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "🚗 *Поездки*\n\nВыберите действие:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


@admin_only
async def admin_cleanup_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пример: деактивировать прошлые поездки (простая уборка)."""
    with Session() as session:
        try:
            now = datetime.now()
            q = session.query(Trip).filter(func.coalesce(Trip.end_date, Trip.date) < now, Trip.is_active == True)
            count = q.count()
            q.update({Trip.is_active: False})
            session.commit()

            text = f"🧹 Уборка завершена.\n\nДеактивировано поездок: `{count}`"
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_trips")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)

        except Exception as e:
            logger.error("admin_cleanup_trips error: %s", e)
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Ошибка очистки поездок.")
            else:
                await update.message.reply_text("❌ Ошибка очистки поездок.")


@admin_only
async def admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ последних логов админа."""
    if AdminLog is None:
        text = "🧾 *Логи*\n\nЛоги недоступны (ошибка импорта admin_database.py)."
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
        return

    with Session() as session:
        try:
            logs = session.query(AdminLog).order_by(AdminLog.timestamp.desc()).limit(20).all()

            text = "🧾 *Логи админа (последние 20)*\n\n"
            if not logs:
                text += "— нет записей\n"
            else:
                for row in logs:
                    ts = row.timestamp.strftime("%d.%m.%Y %H:%M") if row.timestamp else "-"
                    text += f"• `{ts}` | `{row.admin_id}` | {row.action}\n"
                    if row.details:
                        text += f"  _{row.details}_\n"

            keyboard = [
                [InlineKeyboardButton("🗑 Очистить логи", callback_data="admin_clear_logs")],
                [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)

        except Exception as e:
            logger.error("admin_logs error: %s", e)
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Ошибка загрузки логов.")
            else:
                await update.message.reply_text("❌ Ошибка загрузки логов.")


@admin_only
async def admin_clear_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AdminLog is None:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Логи недоступны.")
        else:
            await update.message.reply_text("❌ Логи недоступны.")
        return

    with Session() as session:
        try:
            deleted = session.query(AdminLog).delete()
            session.commit()

            text = f"🗑 Логи очищены. Удалено записей: `{deleted}`"
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_logs")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)

        except Exception as e:
            logger.error("admin_clear_logs error: %s", e)
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Ошибка очистки логов.")
            else:
                await update.message.reply_text("❌ Ошибка очистки логов.")


# =========================
# Admin callback handler
# =========================
@admin_only
async def admin_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""

    # --- Broadcast callbacks (из broadcast.py) ---
    if data == "admin_broadcast":
        await broadcast.start_broadcast(update, context)
        return

    if data == "admin_broadcast_send":
        await broadcast.send_broadcast(update, context)
        return

    if data == "admin_broadcast_cancel":
        await broadcast.cancel_broadcast(update, context)
        return

    # --- Navigation ---
    if data == "admin_back":
        await admin_panel(update, context)
        return

    if data == "admin_stats":
        await admin_stats(update, context)
        return

    if data == "admin_users":
        await admin_users(update, context)
        return

    if data == "admin_bans":
        await admin_bans(update, context)
        return

    if data == "admin_ban_prompt":
        await admin_ban_prompt(update, context)
        return

    if data == "admin_unban_prompt":
        await admin_unban_prompt(update, context)
        return

    if data == "admin_bans_list":
        await admin_bans_list(update, context)
        return


    if data == "admin_trips":
        await admin_trips(update, context)
        return

    if data == "admin_cleanup_trips":
        await admin_cleanup_trips(update, context)
        return

    if data == "admin_logs":
        await admin_logs(update, context)
        return

    if data == "admin_clear_logs":
        await admin_clear_logs(update, context)
        return

    if data == "admin_exit":
        try:
            await query.edit_message_text("✅ Выход из админ-панели.", reply_markup=None)
        except Exception:
            pass
        return

    # --- Unknown admin callback ---
    try:
        await query.edit_message_text("⚠️ Неизвестная команда админки.", reply_markup=_admin_main_kb())
    except Exception:
        pass