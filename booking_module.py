# booking_module.py
"""Логика бронирований (Patch 1.0+).

Содержит:
- обработку callback'ов бронирований (выбор количества мест);
- уведомления водителю/пассажиру.

Принцип: модуль НЕ импортирует handlers.py (во избежание циклов).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Session, Trip, Booking, BookingStatus
from config import PENDING_BOOKING_TTL_MINUTES
import keyboards
import notifications_module


def _as_utc_datetime(value) -> Optional[datetime]:
    """Best-effort parse for booking_time.

    In some SQLite/legacy rows, DateTime can come back as str.
    We parse common formats and return UTC naive datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Try ISO first
        try:
            return datetime.fromisoformat(s)
        except Exception:
            pass
        # Common SQLite format
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None

# Тип для answer_once из handlers.button_callback
AnswerOnce = Callable[[Optional[str]], Awaitable[None]]


# ========== PENDING TTL (Patch 2.1) ==========

async def expire_pending_bookings_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая задача: истекают неподтверждённые бронирования (PENDING) по TTL.

    В проекте места списываются сразу при создании PENDING.
    Поэтому при истечении брони мы возвращаем места в trip.seats_available.

    Задача максимально безопасная:
    - трогает только брони со статусом PENDING;
    - если водитель уже подтвердил/отклонил — мы не вмешиваемся;
    - работает без внешних зависимостей (SQLite).
    """

    try:
        ttl_minutes = int(PENDING_BOOKING_TTL_MINUTES or 15)
    except Exception:
        ttl_minutes = 15

    cutoff = datetime.utcnow() - timedelta(minutes=ttl_minutes)

    bot = getattr(context, "bot", None)
    expired_count = 0
    with Session() as session:
        # Берём кандидатов пакетно (простая стратегия для SQLite).
        # IMPORTANT: do not filter by booking_time at SQL-level.
        # In some SQLite setups/legacy rows, booking_time may be stored as TEXT,
        # and SQL comparison can behave unexpectedly. We filter in Python.
        bookings = (
            session.query(Booking)
            .filter(Booking.status == BookingStatus.PENDING.value)
            .all()
        )

        for b in bookings:
            bt = _as_utc_datetime(getattr(b, "booking_time", None))
            # If we cannot parse timestamp, do not expire automatically.
            if bt is None:
                continue
            if bt >= cutoff:
                continue
            # На всякий случай перепроверяем статус перед изменением
            if b.status != BookingStatus.PENDING.value:
                continue

            bt = _as_utc_datetime(getattr(b, "booking_time", None))
            # If we can't determine the time, do NOT expire (safe default)
            if bt is None or bt >= cutoff:
                continue

            trip = b.trip
            if trip is not None:
                try:
                    trip.seats_available = int(trip.seats_available or 0) + int(b.seats_booked or 0)
                except Exception:
                    pass

            b.status = BookingStatus.EXPIRED.value
            expired_count += 1

            # Уведомляем пассажира об истечении срока заявки (best-effort).
            if bot is not None:
                try:
                    await notify_passenger_booking_expired(bot, b, ttl_minutes=ttl_minutes)
                except Exception:
                    pass

        if expired_count:
            session.commit()

    if expired_count:
        logging.info(f"⌛ Expired pending bookings: {expired_count} (TTL={ttl_minutes}m)")


async def notify_passenger_booking_expired(bot, booking: Booking, ttl_minutes: int = 15) -> None:
    """Сообщение пассажиру: заявка истекла.

    Если в базе сохранён message_id карточки "ожидание подтверждения",
    пытаемся заменить её (edit_message_text). Иначе отправляем новое сообщение.
    """
    trip = getattr(booking, "trip", None)
    route = ""
    when = ""
    try:
        if trip is not None:
            route = f"📍 {trip.departure_point} → {trip.destination_point}"
            if getattr(trip, "date", None):
                when = trip.date.strftime("%d.%m.%Y %H:%M")
    except Exception:
        route = ""
        when = ""

    lines = ["⌛ Срок подтверждения заявки истёк."]
    if route:
        lines.append(route)
    if when:
        lines.append(f"🟢 {when}")
    lines.append("Заявка автоматически отменена.")

    text = "\n".join(lines)
    chat_id = int(getattr(booking, "passenger_id", 0) or 0)
    msg_id = getattr(booking, "passenger_request_msg_id", None)
    kb = keyboards.get_close_only_keyboard("close_booking_expired_notice")

    # 1) Пытаемся заменить существующую карточку ожидания (если message_id сохранён).
    if chat_id and isinstance(msg_id, int) and msg_id > 0:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=int(msg_id), text=text, reply_markup=kb)
            return
        except Exception:
            pass

    # 2) Fallback: отправляем отдельное сообщение.
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    except Exception:
        pass

def can_handle_callback(data: str) -> bool:
    return data.startswith("book_qty_") or data.startswith("book_choose_")


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    data: str,
    answer_once: Callable[..., Awaitable[None]],
) -> bool:
    """Возвращает True, если callback обработан."""

    if not can_handle_callback(data):
        return False

    query = update.callback_query
    if query is None:
        return False

    # формат: book_qty_{trip_id}_{seats} или book_choose_{trip_id}_{seats}
    try:
        parts = data.split("_")
        trip_id = int(parts[2])
        seats_requested = int(parts[3])
    except Exception:
        await answer_once("❌ Некорректная команда.", show_alert=True)
        return True

    with Session() as session:
        trip = session.query(Trip).get(trip_id)

        if (not trip) or (not getattr(trip, "is_active", False)) or int(getattr(trip, "seats_available", 0) or 0) <= 0:
            await answer_once("❌ Поездка недоступна.", show_alert=True)
            return True

        # нельзя бронировать свою поездку
        if int(trip.driver_id) == int(query.from_user.id):
            await answer_once("⚠️ Нельзя забронировать свою поездку.", show_alert=True)
            return True

        # поездка в прошлом
        if getattr(trip, "date", None) and trip.date < datetime.now():
            await answer_once("❌ Нельзя забронировать место на прошедшую поездку.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return True

        # защита от некорректного количества
        if seats_requested < 1 or seats_requested > int(trip.seats_available):
            await answer_once("⚠️ Недоступное количество мест.", show_alert=True)
            return True

        # уже есть активная бронь на эту поездку
        existing_booking = session.query(Booking).filter(
            Booking.trip_id == trip_id,
            Booking.passenger_id == query.from_user.id,
            Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value]),
        ).first()
        if existing_booking:
            await answer_once("⚠️ Вы уже забронировали место в этой поездке!", show_alert=True)
            return True

        # создаём бронь + уменьшаем места
        new_booking = Booking(
            trip_id=trip_id,
            passenger_id=query.from_user.id,
            passenger_name=query.from_user.full_name,
            seats_booked=seats_requested,
            booking_time=datetime.utcnow(),
            status=BookingStatus.PENDING.value,
        )
        trip.seats_available = int(trip.seats_available) - seats_requested
        session.add(new_booking)
        session.commit()
        session.refresh(new_booking)

        driver_id = int(trip.driver_id)
        booking_id = int(new_booking.id)

    # уведомление водителю
    await notify_driver_new_booking(
        context.bot,
        driver_id=driver_id,
        booking_id=booking_id,
        seats_booked=seats_requested,
        passenger=query.from_user,
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть", callback_data="close_booking_request")]])

    await answer_once()
    await query.edit_message_text(
        text=(
            "✅ Запрос на бронирование отправлен водителю.\n"
            "Ожидайте подтверждения.\n\n"
            f"💺 Мест: *{seats_requested}*"
        ),
                reply_markup=kb,
    )


    # Сохраняем message_id карточки ожидания подтверждения, чтобы при истечении TTL можно было заменить её (best-effort).
    try:
        with Session() as _s:
            _b = _s.query(Booking).get(booking_id)
            if _b is not None:
                _b.passenger_request_msg_id = int(query.message.message_id)
                _s.commit()
    except Exception:
        pass


    try:
        notifications_module.track_ui_message(context, query.message.chat_id, query.message.message_id)
    except Exception:
        pass

    return True


# ========== УВЕДОМЛЕНИЯ ==========

async def notify_driver_new_booking(bot, *, driver_id: int, booking_id: int, seats_booked: int, passenger):
    """Уведомляет водителя о новом бронировании (без ORM объектов)."""
    try:
        contact = f"@{passenger.username}" if getattr(passenger, "username", None) else "скрыт"
        full_name = getattr(passenger, "full_name", None) or "—"
        passenger_id = getattr(passenger, "id", None) or "—"

        message_text = (
            "🔔 *НОВОЕ БРОНИРОВАНИЕ!*\n\n"
            f"👤 *Пассажир:* {full_name}\n"
            f"📞 *Контакт:* {contact}\n"
            f"💺 *Мест:* *{seats_booked}*"
        )

        await bot.send_message(
            chat_id=driver_id,
            text=message_text,
                        reply_markup=keyboards.get_booking_management_keyboard(booking_id),
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления водителя: {e}")


async def notify_passenger_booking_confirmed(bot, booking, driver=None):
    """Уведомляет пассажира о подтверждении бронирования."""
    try:
        trip = booking.trip
        driver_info = f"@{driver.username}" if driver and driver.username else trip.driver_name

        message_text = (
            f"✅ *БРОНИРОВАНИЕ ПОДТВЕРЖДЕНО!*\n\n"
            f"🚗 *Поездка:* {trip.departure_point} → {trip.destination_point}\n"
            f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y %H:%M')}\n"
            f"👤 *Водитель:* {trip.driver_name}\n"
            f"📞 *Контакты водителя:* {driver_info}\n"
            f"💺 *Мест:* {booking.seats_booked}\n"
            f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n\n"
        )

        keyboard = [
            [InlineKeyboardButton("⭐ Оценить поездку", callback_data=f"passenger_open_trip_rating_{booking.id}")],
            [InlineKeyboardButton("❌ Отменить бронирование", callback_data=f"cancel_booking_{booking.id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await bot.send_message(
            chat_id=booking.passenger_id,
            text=message_text,
                        reply_markup=reply_markup,
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления пассажира: {e}")


async def notify_passenger_booking_rejected(bot, booking):
    """Уведомляет пассажира об отклонении бронирования."""
    try:
        trip = booking.trip
        message_text = (
            f"❌ *БРОНИРОВАНИЕ ОТКЛОНЕНО*\n\n"
            f"Водитель отклонил вашу заявку на поездку:\n"
            f"🚗 {trip.departure_point} → {trip.destination_point}\n"
            f"📅 {trip.date.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Место возвращено в общий доступ. Вы можете найти другие поездки."
        )

        keyboard = [[InlineKeyboardButton("🔍 Найти другие поездки", callback_data="search_new_trips")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await bot.send_message(
            chat_id=booking.passenger_id,
            text=message_text,
                        reply_markup=reply_markup,
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления об отклонении: {e}")


async def notify_driver_booking_cancelled(bot, booking):
    """Уведомляет водителя об отмене бронирования (компактно) + кнопка Закрыть."""
    try:
        trip = booking.trip
        if not trip:
            return

        passenger_username = None
        try:
            user_chat = await bot.get_chat(booking.passenger_id)
            if user_chat and getattr(user_chat, "username", None):
                passenger_username = user_chat.username
        except Exception as e:
            logging.error(f"Не удалось получить username для пользователя {booking.passenger_id}: {e}")

        contact = f"@{passenger_username}" if passenger_username else "скрыт"

        message_text = (
            "⚠️ *Бронирование отменено*\n\n"
            f"👤 *Пассажир:* {booking.passenger_name}\n"
            f"📞 *Контакт:* {contact}\n"
            f"💺 *Отменено мест:* {booking.seats_booked}"
        )

        kb = keyboards.get_driver_cancel_notice_keyboard(passenger_username=passenger_username, passenger_id=booking.passenger_id)

        await bot.send_message(
            chat_id=trip.driver_id,
            text=message_text,
                        reply_markup=kb,
        )

    except Exception as e:
        logging.error(f"Ошибка notify_driver_booking_cancelled: {e}")
