# notifications_module.py
import logging
from telegram.ext import ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import Session, Trip

from ui_render import render_trip_card
from user_registry import BotUser

logger = logging.getLogger(__name__)

# Ключи хранения "истории карточек" в bot_data (общие для приложения)
BOTDATA_HISTORY_KEY = "ui_history_msg_ids"
HISTORY_CAP = 200  # ограничим, чтобы не раздувать память


def _norm(s: str | None) -> str:
    return " ".join((s or "").strip().split()).casefold()


def track_ui_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """
    Храним message_id карточек, чтобы потом можно было удалить кнопкой "Очистить историю".
    Храним в application.bot_data (глобально), потому что уведомления могут приходить
    вне контекста активного user_data.
    """
    try:
        app = getattr(context, "application", None)
        if app is None:
            return

        store = app.bot_data.setdefault(BOTDATA_HISTORY_KEY, {})
        ids = store.setdefault(int(chat_id), [])
        ids.append(int(message_id))

        # cap
        if len(ids) > HISTORY_CAP:
            store[int(chat_id)] = ids[-HISTORY_CAP:]
    except Exception:
        pass


SLOT_RANGES = {
    'morning': ('08:00', '11:59', '🌅 Утро'),
    'day':     ('12:00', '16:59', '🌞 День'),
    'evening': ('17:00', '20:00', '🌙 Вечер'),
}


def format_trip_time_for_card(trip) -> str:
    try:
        start_dt = getattr(trip, 'date', None)
        end_dt = getattr(trip, 'end_date', None) or start_dt
        if not start_dt:
            return ''
        start_t = start_dt.strftime('%H:%M')
        end_t = end_dt.strftime('%H:%M')
        if end_t != start_t:
            for _k,(a,b,label) in SLOT_RANGES.items():
                if start_t==a and end_t==b:
                    return f"{label} ({a}-{b})"
            return f"{start_t}-{end_t}"
        return start_t
    except Exception:
        return ''


def build_trip_search_card(trip) -> tuple[str, InlineKeyboardMarkup]:
    time_str = format_trip_time_for_card(trip)

    card_text = render_trip_card(
        title="🚗 Новая поездка",
        date=getattr(trip, "date", None),
        time_str=time_str,
        departure=getattr(trip, "departure_point", "—"),
        destination=getattr(trip, "destination_point", "—"),
        seats_available=int(getattr(trip, "seats_available", 0) or 0),
        price=getattr(trip, "price", None),
        action_hint=None,
        status=None,
    )

    # ✅ Кнопки как “из поиска”: можно сразу бронировать + посмотреть детали
    rows = []
    if getattr(trip, "seats_available", 0) and trip.seats_available > 0:
        rows.append([InlineKeyboardButton("✅ Забронировать", callback_data=f"book_{trip.id}")])
    rows.append([InlineKeyboardButton("ℹ️ Подробнее", callback_data=f"s_detail_{trip.id}")])

    kb = InlineKeyboardMarkup(rows)
    return card_text, kb

def _matches_filter(bu: BotUser, trip: Trip) -> bool:
    if not bool(getattr(bu, "search_filter_enabled", False)):
        return True

    dep = getattr(bu, "search_filter_departure", None)
    dest = getattr(bu, "search_filter_destination", None)

    trip_dep = _norm(getattr(trip, "departure_point", None))
    trip_dest = _norm(getattr(trip, "destination_point", None))

    if dep and (trip_dep != _norm(dep)):
        return False
    if dest and (trip_dest != _norm(dest)):
        return False
    return True


async def notify_new_trip(context: ContextTypes.DEFAULT_TYPE, trip_id: int) -> None:
    """
    Уведомления о новой поездке пользователям, у кого включены уведомления.
    Если у пользователя включен фильтр — уведомляем только если поездка соответствует фильтру.
    """
    if not trip_id:
        return

    with Session() as session:
        trip = session.query(Trip).filter(Trip.id == trip_id).one_or_none()
        if trip is None:
            return

        users = (
            session.query(BotUser)
            .filter(getattr(BotUser, "trips_notify_enabled") == True)  # noqa: E712
            .all()
        )

    if not users:
        return

    card_text, card_kb = build_trip_search_card(trip)

    for bu in users:
        try:
            # ❌ Не уведомляем создателя о собственной поездке
            try:
                if int(getattr(bu, "telegram_id", 0) or 0) == int(getattr(trip, "driver_id", 0) or 0):
                    continue
            except Exception:
                pass

            # 1) Фильтр маршрута (если включен)
            if not _matches_filter(bu, trip):
                continue

            # 2) Куда слать: chat_id -> telegram_id
            chat_id = getattr(bu, "chat_id", None) or getattr(bu, "telegram_id", None)
            if not chat_id:
                continue

            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=card_text,
                reply_markup=card_kb,
            )

            track_ui_message(context, int(chat_id), int(sent.message_id))

        except Exception as e:
            logger.debug(
                "notify send failed to %s: %s",
                getattr(bu, "chat_id", None) or getattr(bu, "telegram_id", None),
                e
            )