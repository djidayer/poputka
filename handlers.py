# handlers.py - ПОЛНЫЙ ФАЙЛ С ВСЕМИ ФУНКЦИЯМИ
import asyncio
import re
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import Session, Trip, Booking, BookingStatus
from datetime import datetime, timedelta
from sqlalchemy import func  # Импортируем для агрегатных функций
import keyboards
import locations

import settings_module
import notifications_module
import booking_module
from ui_render import render_trip_card, render_booking_card
from dotenv import load_dotenv
from keyboards import get_passenger_feedback_keyboard
from user_registry import BotUser

# Загружаем переменные окружения
load_dotenv()

# Получаем настройки из конфигурации
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '8'))
CLEANUP_OLD_TRIPS_DAYS = int(os.getenv('CLEANUP_OLD_TRIPS_DAYS', '7'))


SLOT_RANGES = {
    'morning': ('08:00', '11:59', '🌅 Утро'),
    'day':     ('12:00', '16:59', '🌞 День'),
    'evening': ('17:00', '20:00', '🌙 Вечер'),
}


def _trip_time_choice_kb():
    """Кнопки выбора времени при создании поездки."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🌅 Утро (08–12)', callback_data='trip_time_slot_morning'),
            InlineKeyboardButton('🌞 День (12–17)', callback_data='trip_time_slot_day'),
        ],
        [InlineKeyboardButton('🌙 Вечер (17–20)', callback_data='trip_time_slot_evening')],
        [InlineKeyboardButton('🕒 Точное время', callback_data='trip_time_exact')],
        [InlineKeyboardButton('❌ Отмена создания', callback_data='cancel_trip_creation')],
    ])




def _edit_trip_time_choice_kb(trip_id: int) -> InlineKeyboardMarkup:
    """Кнопки выбора времени при *редактировании* поездки (механика как при создании)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🌅 Утро (08–12)', callback_data=f'edit_trip_time_slot_morning_{trip_id}'),
            InlineKeyboardButton('🌞 День (12–17)', callback_data=f'edit_trip_time_slot_day_{trip_id}')
        ],
        [
            InlineKeyboardButton('🌙 Вечер (17–20)', callback_data=f'edit_trip_time_slot_evening_{trip_id}'),
            InlineKeyboardButton('🕒 Точное время', callback_data=f'edit_trip_time_exact_{trip_id}')
        ],
        [InlineKeyboardButton('⬅️ Назад', callback_data=f'edit_back_{trip_id}')]
    ])

def format_trip_time(trip) -> str:
    """Красивое отображение времени для карточек."""
    try:
        start_dt = getattr(trip, 'date', None)
        end_dt = getattr(trip, 'end_date', None) or start_dt
        if not start_dt:
            return ''
        start_t = start_dt.strftime('%H:%M')
        end_t = end_dt.strftime('%H:%M')
        if end_t != start_t:
            for _k, (a, b, label) in SLOT_RANGES.items():
                if start_t == a and end_t == b:
                    return f"{label} ({a}-{b})"
            return f"{start_t}-{end_t}"
        return start_t
    except Exception:
        return ''


def trip_end_dt(trip):
    """datetime окончания поездки (для актуальности/автоудаления)."""
    return getattr(trip, 'end_date', None) or getattr(trip, 'date', None)

async def send_tracked_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup=None,
    disable_web_page_preview: bool | None = None,
):
    """
    Отправляет сообщение и добавляет его message_id в глобальную историю карточек (bot_data),
    чтобы 🗑️ Очистить историю могла удалить и это.
    """
    # Markdown/HTML отключены: всегда plain text.
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=None,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
    )
    try:
        notifications_module.track_ui_message(context, chat_id, msg.message_id)
    except Exception:
        pass
    return msg


async def edit_tracked_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    parse_mode: str | None = None,
    reply_markup=None,
):
    """
    Редактирует сообщение (обычно callback) и добавляет его message_id в историю карточек.
    Полезно для edit_message_text() сценариев.
    """
    q = update.callback_query
    if not q or not q.message:
        return

    # Markdown/HTML отключены: всегда plain text.
    await q.edit_message_text(text, parse_mode=None, reply_markup=reply_markup)

    try:
        notifications_module.track_ui_message(context, q.message.chat_id, q.message.message_id)
    except Exception:
        pass

# ====== Валидация направлений (единый источник: locations.py) ======

# оставляем имя ALLOWED_LOCATIONS для совместимости со всем файлом
ALLOWED_LOCATIONS = locations.ALLOWED_LOCATIONS

def _norm(s: str) -> str:
    return locations.norm(s)

def is_allowed_location(s: str) -> bool:
    # точное совпадение с каноническим значением
    return locations.canonical(s) is not None

def allowed_locations_text() -> str:
    return "\n".join([f"• {x}" for x in ALLOWED_LOCATIONS])

def fuzzy_location_suggestions(user_input: str, limit: int = 8) -> list[str]:
    """Подсказки по направлениям (единый источник: locations.py)."""
    return locations.fuzzy(user_input, limit=limit)


def _creation_location_matches(user_input: str, limit: int = 12):
    """Возвращает (exact, suggestions, fuzzy_used) для ввода направлений в *создании поездки*."""
    raw = (user_input or "").strip()
    exact = locations.canonical(raw)
    if exact:
        return exact, [], False

    # Определяем, это обычные подсказки (подстрока/префикс) или fuzzy fallback
    ni = locations.norm(raw)
    prefix_hits = []
    if ni:
        for x in ALLOWED_LOCATIONS:
            nx = locations.norm(x)
            if nx.startswith(ni) or ni in nx:
                prefix_hits.append(x)

    suggestions = locations.suggestions(raw, limit=limit)
    fuzzy_used = bool(suggestions) and not bool(prefix_hits)
    return None, suggestions, fuzzy_used


def _creation_suggestions_keyboard(field: str, suggestions: list[str], *, trigger_hint: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора пункта (откуда/куда) при создании поездки."""
    # callback: tc_pick_<departure|destination>_<idx>
    buttons: list[list[InlineKeyboardButton]] = []
    for i, s in enumerate(suggestions[:12]):
        buttons.append([InlineKeyboardButton(f"{s}", callback_data=f"tc_pick_{field}_{i}")])

    # полезные действия
    if field == "departure":
        buttons.append([InlineKeyboardButton("📍 Доступные направления", callback_data="show_allowed_departure")])
    else:
        buttons.append([InlineKeyboardButton("📍 Доступные направления", callback_data="show_allowed_destination")])

    buttons.append([InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")])
    return InlineKeyboardMarkup(buttons)



def _edit_suggestions_keyboard(field: str, trip_id: int, suggestions: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура выбора пункта (откуда/куда) при редактировании поездки."""
    # callback: edit_pick_<dep|dst>_<trip_id>_<idx>
    buttons: list[list[InlineKeyboardButton]] = []
    prefix = "edit_pick_dep" if field == "departure" else "edit_pick_dst"
    for i, s in enumerate((suggestions or [])[:8]):
        buttons.append([InlineKeyboardButton(f"{s}", callback_data=f"{prefix}_{trip_id}_{i}")])
    buttons.append([InlineKeyboardButton("📍 Доступные направления", callback_data="show_allowed_locations")])
    return InlineKeyboardMarkup(buttons)

async def _creation_accept_departure(update: Update, context: ContextTypes.DEFAULT_TYPE, departure_value: str, *, raw_value: str | None = None):
    """Сохраняет departure и показывает ввод destination (единый путь для текста/выбора)."""
    # Сохраняем пункт отправления
    context.user_data['departure'] = departure_value
    context.user_data["creating_field"] = "destination"

    note = ""
    if raw_value and raw_value.strip() and raw_value.strip() != departure_value:
        note = f"\n\n✅ Исправлено: *{raw_value.strip()}* → *{departure_value}*"  # мягкая подсказка

    keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await update.effective_chat.send_message(
        f"📍 *Пункт отправления:* {departure_value}{note}\n\n"
        "Теперь введите пункт назначения:\n\n"
        "💡 *Подсказка:* Можно ввести несколько букв — я предложу варианты.",
        reply_markup=reply_markup
    )
    context.user_data.setdefault('creation_messages', []).append(msg.message_id)
    return INPUT_DESTINATION


async def _creation_accept_destination(update: Update, context: ContextTypes.DEFAULT_TYPE, destination_value: str, *, raw_value: str | None = None):
    """Сохраняет destination и показывает выбор даты (единый путь для текста/выбора)."""
    context.user_data['destination'] = destination_value

    note = ""
    if raw_value and raw_value.strip() and raw_value.strip() != destination_value:
        note = f"\n\n✅ Исправлено: *{raw_value.strip()}* → *{destination_value}*"

    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня", callback_data="trip_date_today"),
            InlineKeyboardButton("📅 Завтра", callback_data="trip_date_tomorrow"),
        ],
        [InlineKeyboardButton("📝 Другая дата", callback_data="trip_date_manual")],
        [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await update.effective_chat.send_message(
        f"🎯 *Пункт назначения:* {destination_value}{note}\n\n"
        "Теперь выберите дату поездки:",
        reply_markup=reply_markup
    )
    context.user_data.setdefault('creation_messages', []).append(msg.message_id)
    return INPUT_DATE_SELECT


async def creation_pick_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор варианта из списка подсказок при создании поездки."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    data = query.data or ""

    # Fallback: кнопки выбора мест в создании поездки (tc_seats_N)
    # Если ConversationHandler по какой-то причине не перехватил callback, обрабатываем здесь.
    if re.match(r"^tc_seats_\d+$", data or ""):
        return await creation_pick_seats(update, context)

    # tc_pick_<departure|destination>_<idx>
    m = re.match(r"^tc_pick_(departure|destination)_(\d+)$", data)
    if not m:
        return ConversationHandler.END

    field = m.group(1)
    idx = int(m.group(2))

    await query.answer()

    store = context.user_data.get("tc_suggestions") or {}
    options = store.get(field) or []
    if idx < 0 or idx >= len(options):
        # если контекст потерялся — просим ввести снова
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]])
        await query.edit_message_text(
            "⚠️ Список вариантов устарел. Пожалуйста, введите пункт ещё раз.",
            reply_markup=kb
        )
        return INPUT_DEPARTURE if field == "departure" else INPUT_DESTINATION

    chosen = options[idx]

    # Подчистим список, чтобы не копился
    try:
        context.user_data.get("tc_suggestions", {}).pop(field, None)
    except Exception:
        pass

    # Показываем следующий шаг
    if field == "departure":
        context.user_data["creating_field"] = "destination"
        # редактируем сообщение с выбором, чтобы не оставлять его «висячим»
        try:
            await query.edit_message_text(f"✅ Выбрано: *{chosen}*")
        except Exception:
            pass
        return await _creation_accept_departure(update, context, chosen)
    else:
        context.user_data["creating_field"] = "date"
        try:
            await query.edit_message_text(f"✅ Выбрано: *{chosen}*")
        except Exception:
            pass
        return await _creation_accept_destination(update, context, chosen)



async def edit_creation_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup):
    """
    Редактирует последнее 'служебное' сообщение создания поездки (чтобы не плодить мусор).
    """
    msg_id = None
    if isinstance(context.user_data.get("creation_messages"), list) and context.user_data["creation_messages"]:
        msg_id = context.user_data["creation_messages"][-1]

    if msg_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=reply_markup
        )
        return msg_id

    # fallback: если по какой-то причине не нашли msg_id — отправим новое
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup
    )
    context.user_data.setdefault("creation_messages", []).append(msg.message_id)
    return msg.message_id

# Функция для форматирования времени бронирования с поправкой +8
def format_booking_time(booking_time, timezone_offset=TIMEZONE_OFFSET):
    """Форматирует время бронирования с учетом часового пояса."""
    if booking_time is None:
        return "Не указано"
    
    try:
        # Добавляем смещение часового пояса
        local_time = booking_time + timedelta(hours=timezone_offset)
        return local_time.strftime('%d.%m.%Y %H:%M')
    except Exception as e:
        logging.error(f"Ошибка форматирования времени бронирования: {e}")
        return booking_time.strftime('%d.%m.%Y %H:%M') if hasattr(booking_time, 'strftime') else str(booking_time)

# Глобальный словарь для отслеживания активных диалогов
active_conversations = {}

# Состояния для диалога создания поездки (без авто)
(
    INPUT_DEPARTURE,
    INPUT_DESTINATION,
    INPUT_DATE_SELECT,   # выбор: сегодня/завтра/вручную
    INPUT_DATE_MANUAL,   # ввод даты вручную
    INPUT_TIME,          # ввод времени
    INPUT_SEATS,
    INPUT_PRICE,
) = range(7)

# Состояния для редактирования поездки (без авто)
(EDIT_DEPARTURE, EDIT_DESTINATION, EDIT_DATE,
 EDIT_SEATS, EDIT_PRICE) = range(7, 12)

async def handle_clear_understood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки "Понятно" """
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text(
        "✅ *Инструкция сохранена*\n\n"
        "Вы всегда можете очистить историю через настройки Telegram."
    )
    
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=keyboards.get_main_menu()
    )

# ========== СУЩЕСТВУЮЩИЕ ФУНКЦИИ (оставляем все как было) ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Добро пожаловать!\n\n",
        reply_markup=keyboards.get_main_menu()
    )

async def force_end_conversation(chat_id, context):
    """Принудительно завершает диалог для указанного chat_id."""
    # Очищаем user_data для этого чата
    if context.user_data:
        context.user_data.clear()
    
    # Завершаем диалог
    return ConversationHandler.END



def _creation_seats_keyboard(selected=None):
    """Клавиатура выбора количества мест (1–5) для *создания поездки*."""
    def btn(n: int):
        label = f"✅ {n}" if selected == n else str(n)
        return InlineKeyboardButton(label, callback_data=f"tc_seats_{n}")
    rows = [
        [btn(1), btn(2), btn(3)],
        [btn(4), btn(5)],
        [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")],
    ]
    return InlineKeyboardMarkup(rows)




def _edit_seats_keyboard(trip_id: int, selected: int | None = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора количества мест (1–5) для *редактирования поездки*."""
    def btn(n: int):
        label = f"✅ {n}" if selected == n else str(n)
        return InlineKeyboardButton(label, callback_data=f"edit_seats_pick_{trip_id}_{n}")
    rows = [
        [btn(1), btn(2), btn(3)],
        [btn(4), btn(5)],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")],
    ]
    return InlineKeyboardMarkup(rows)

async def creation_pick_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор количества мест инлайн-кнопками в создании поездки."""
    query = update.callback_query
    await query.answer()

    m = re.match(r"^tc_seats_(\d+)$", (query.data or ""))
    if not m:
        return INPUT_SEATS

    seats = int(m.group(1))
    kb = _creation_seats_keyboard(selected=seats)

    if seats < 1 or seats > 5:
        await query.message.edit_text(
            "❌ Неверный выбор. Выберите количество мест (1–5):",
            reply_markup=_creation_seats_keyboard(),
        )
        return INPUT_SEATS

    context.user_data['seats'] = seats

    # Переходим к вводу цены (без подсказок/примеров)
    await query.message.edit_text(
        f"👥 Места: {seats}\n\n💰 Цена за место:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
        ),
    )
    context.user_data.setdefault('creation_messages', []).append(query.message.message_id)
    return INPUT_PRICE

async def new_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс создания новой поездки."""
    # Очищаем предыдущие данные
    context.user_data.clear()
    context.user_data['creation_messages'] = []
    context.user_data['user_messages'] = []
    
    # Получаем информацию о чате и сообщении
    chat_id = None
    message_to_reply = None
    
    if update.callback_query:
        # Если это callback_query
        chat_id = update.callback_query.message.chat_id
        message_to_reply = update.callback_query.message
        await update.callback_query.answer()
    elif update.message:
        # Если это обычное сообщение
        chat_id = update.effective_chat.id
        message_to_reply = update.message

        # Сохраняем сообщение пользователя
        context.user_data['user_messages'].append(update.message.message_id)

        # ✅ Чистый чат: сразу удаляем триггер "🚗 Создать поездку"
        try:
            await update.message.delete()
        except Exception:
            pass
    
    if not chat_id:
        logging.error("Не удалось определить chat_id")
        return ConversationHandler.END
    
    created_trip_id = None
    with Session() as session:
        try:
            # Проверяем, есть ли у пользователя активные поездки
            user_id = update.effective_user.id
            active_trips_count = session.query(Trip).filter(
                Trip.driver_id == user_id,
                Trip.is_active == True,
                func.coalesce(Trip.end_date, Trip.date) >= datetime.now()  # Только будущие поездки
            ).count()
            
            if active_trips_count > 0:
                # Если есть активные поездки, показываем сообщение
                if active_trips_count == 1:
                    message = (
                        "⚠️ *У вас уже есть активная поездка!*\n\n"
                        "Вы не можете создать новую поездку, пока текущая активна.\n\n"
                        "Перейдите в 'Мои поездки', чтобы:\n"
                        "• Увидеть детали текущей поездки\n"
                        "• Отредактировать или отменить её\n"
                        "• Посмотреть бронирования\n\n"
                        "После завершения или отмены текущей поездки вы сможете создать новую."
                    )
                else:
                    message = (
                        f"⚠️ *У вас уже есть {active_trips_count} активных поездок!*\n\n"
                        "Вы не можете создать новую поездку, пока есть активные.\n\n"
                        "Перейдите в 'Мои поездки', чтобы:\n"
                        "• Увидеть все ваши поездки\n"
                        "• Управлять ими (редактировать/отменять)\n"
                        "• Посмотреть бронирования\n\n"
                        "После завершения или отмены активных поездок вы сможете создать новую."
                    )
                
                # УБРАТЬ КНОПКУ "Создать поездку" - оставить только "Мои поездки"
                # Передаём message_id триггера ("🚗 Создать поездку"), чтобы потом чисто удалить его вместе с карточкой
                trigger_id = update.message.message_id if update.message else 0
                keyboard = [[
                    InlineKeyboardButton("📋 Мои поездки", callback_data=f"show_my_trips_blocked_{trigger_id}")
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                if update.callback_query:
                    # Редактируем существующее сообщение
                    await update.callback_query.edit_message_text(
                        message,
                        reply_markup=reply_markup
                    )
                else:
                    # Отправляем новое сообщение
                    msg = await update.message.reply_text(
                        message,
                        reply_markup=reply_markup
                    )
                    context.user_data['creation_messages'].append(msg.message_id)
                
                return ConversationHandler.END
            
        except Exception as e:
            logging.error(f"Ошибка при проверке активных поездок: {e}")
    
    # Если активных поездок нет, начинаем процесс создания
    keyboard = [[
        InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Отправляем сообщение с помощью bot.send_message для единообразия
    msg_text = (
        "🚗 *Создание новой поездки*\n\n"
        "Введите пункт отправления:\n\n"
        "💡 *Подсказка:* Можно ввести город, район или конкретный адрес."
    )
    
    if update.callback_query:
        # Если это callback, редактируем существующее сообщение
        await update.callback_query.edit_message_text(
            msg_text,
            reply_markup=reply_markup
        )
        # Сохраняем ID сообщения
        msg_id = update.callback_query.message.message_id
    else:
        # Если это обычное сообщение, отправляем новое
        msg = await update.message.reply_text(
            msg_text,
            reply_markup=reply_markup
        )
        msg_id = msg.message_id
    
    context.user_data['creation_messages'].append(msg_id)
    context.user_data["creating_field"] = "departure"
    return INPUT_DEPARTURE

async def input_departure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод пункта отправления с автоподбором/подсказками."""
    raw_value = (update.message.text or "").strip()

    # Инициализируем списки, если их нет
    context.user_data.setdefault('user_messages', [])
    context.user_data.setdefault('creation_messages', [])

    # Проверяем, не нажата ли кнопка отмены (на случай если текстом)
    if raw_value == "❌ Отмена":
        return await cancel_creation(update, context)

    # Чистим чат: удаляем сообщение пользователя (ввод)
    try:
        await update.message.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id

    exact, suggestions, fuzzy_used = _creation_location_matches(raw_value, limit=12)

    # Нет совпадений
    if not exact and not suggestions:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Доступные направления", callback_data="show_allowed_departure")],
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")],
        ])
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Неизвестный пункт отправления.*\n\n"
            "Попробуйте ввести по-другому (например, первые 2–3 буквы)\n"
            "или нажмите *«Доступные направления»*.",
            kb
        )
        return INPUT_DEPARTURE

    # Один вариант — сразу подставляем
    if not exact and len(suggestions) == 1:
        chosen = suggestions[0]
        return await _creation_accept_departure(update, context, chosen, raw_value=raw_value)

    # Несколько вариантов — показываем список для выбора
    if not exact and suggestions:
        context.user_data['tc_suggestions'] = context.user_data.get('tc_suggestions', {})
        context.user_data['tc_suggestions']['departure'] = suggestions[:12]

        title = "📍 Выберите пункт отправления"
        hint = "\n\n💡 Похоже на опечатку — выберите правильный вариант:" if fuzzy_used else "\n\n💡 Введите ещё пару букв, если нужного нет в списке."
        kb = _creation_suggestions_keyboard('departure', suggestions)

        await edit_creation_message(
            context,
            chat_id,
            f"{title}\n\nВы ввели: *{raw_value}*{hint}",
            kb
        )
        return INPUT_DEPARTURE

    # Точное совпадение
    return await _creation_accept_departure(update, context, exact)


async def input_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод пункта назначения с автоподбором/подсказками."""
    raw_value = (update.message.text or "").strip()

    context.user_data.setdefault('user_messages', [])
    context.user_data.setdefault('creation_messages', [])

    if raw_value == "❌ Отмена":
        return await cancel_creation(update, context)

    # Чистим чат: удаляем сообщение пользователя (ввод)
    try:
        await update.message.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    context.user_data["creating_field"] = "destination"

    exact, suggestions, fuzzy_used = _creation_location_matches(raw_value, limit=12)

    # Нет совпадений
    if not exact and not suggestions:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Доступные направления", callback_data="show_allowed_destination")],
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")],
        ])
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Неизвестный пункт назначения.*\n\n"
            "Попробуйте ввести по-другому (например, первые 2–3 буквы)\n"
            "или нажмите *«Доступные направления»*.",
            kb
        )
        return INPUT_DESTINATION

    # Один вариант — сразу подставляем
    if not exact and len(suggestions) == 1:
        chosen = suggestions[0]
        return await _creation_accept_destination(update, context, chosen, raw_value=raw_value)

    # Несколько вариантов — показываем список для выбора
    if not exact and suggestions:
        context.user_data['tc_suggestions'] = context.user_data.get('tc_suggestions', {})
        context.user_data['tc_suggestions']['destination'] = suggestions[:12]

        title = "🎯 Выберите пункт назначения"
        hint = "\n\n💡 Похоже на опечатку — выберите правильный вариант:" if fuzzy_used else "\n\n💡 Введите ещё пару букв, если нужного нет в списке."
        kb = _creation_suggestions_keyboard('destination', suggestions)

        await edit_creation_message(
            context,
            chat_id,
            f"{title}\n\nВы ввели: *{raw_value}*{hint}",
            kb
        )
        return INPUT_DESTINATION

    # Точное совпадение
    return await _creation_accept_destination(update, context, exact)


async def select_trip_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор даты поездки: сегодня/завтра/вручную."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    today = datetime.now().date()

    if data == "trip_date_today":
        chosen = today
    elif data == "trip_date_tomorrow":
        chosen = today + timedelta(days=1)
    elif data == "trip_date_manual":
        # просим ввести дату вручную
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
        ])
        await query.edit_message_text(
            "📝 *Введите дату поездки*\n\n"
            "📅 Формат: *ДД.ММ.ГГГГ*\n"
            "💡 Пример: *25.12.2026*",
            reply_markup=kb
        )
        return INPUT_DATE_MANUAL
    else:
        # неизвестная кнопка — остаемся здесь
        return INPUT_DATE_SELECT

    # дата выбрана (сегодня/завтра) — выбираем время
    context.user_data["trip_date_only"] = chosen

    await query.edit_message_text(
        f"📅 *Дата:* {chosen.strftime('%d.%m.%Y')}\n\n"
        "⏰ *Выберите время поездки:*",
        reply_markup=_trip_time_choice_kb()
    )
    return INPUT_TIME




async def select_trip_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок времени (Утро/День/Вечер/точное время) при создании поездки."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    date_only = context.user_data.get("trip_date_only")
    if not date_only:
        # потеряли состояние — вернём на выбор даты
        await query.edit_message_text("📅 *Сначала выберите дату поездки.*")
        return INPUT_DATE_SELECT

    if data == "trip_time_exact":
        # Просим ввести точное время вручную
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]])
        await query.edit_message_text(
            f"📅 *Дата:* {date_only.strftime('%d.%m.%Y')}\n\n"
            "⏰ *Введите точное время поездки*\n"
            "Формат: *ЧЧ:ММ*\n"
            "Пример: *14:30*",
            reply_markup=kb,
        )
        # дальше продолжает работать input_trip_time()
        return INPUT_TIME

    if data.startswith("trip_time_slot_"):
        slot = data.split("trip_time_slot_", 1)[1]
        if slot not in SLOT_RANGES:
            return INPUT_TIME

        start_s, end_s, label = SLOT_RANGES[slot]
        start_t = datetime.strptime(start_s, "%H:%M").time()
        end_t = datetime.strptime(end_s, "%H:%M").time()

        start_dt = datetime.combine(date_only, start_t)
        end_dt = datetime.combine(date_only, end_t)

        # проверяем актуальность по end_dt (чтобы слот не исчезал раньше конца)
        if end_dt < datetime.now():
            await query.edit_message_text(
                "❌ *Нельзя создать поездку в прошлом.*\n\n"
                "Выберите другое время.",
                reply_markup=_trip_time_choice_kb(),
            )
            return INPUT_TIME

        context.user_data["date"] = start_dt
        context.user_data["date_end"] = end_dt
        context.user_data["time_mode"] = "slot"
        context.user_data["time_slot"] = slot

        # Переходим к местам (как после ввода точного времени)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]])
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"📅 *Дата:* {date_only.strftime('%d.%m.%Y')}\n"
                f"⏰ *Время:* {label} ({start_s}-{end_s})\n\n"
                "Выберите количество свободных мест (1–5):\n\n"
                "Нажмите кнопку ниже."
            ),
            reply_markup=_creation_seats_keyboard(),
        )
        context.user_data.setdefault("creation_messages", []).append(msg.message_id)
        return INPUT_SEATS

    return INPUT_TIME
async def input_trip_date_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод даты вручную (ДД.ММ.ГГГГ), затем просим время."""
    text = (update.message.text or "").strip()

    # чистый чат: запомним и удалим ввод пользователя
    context.user_data.setdefault("user_messages", []).append(update.message.message_id)
    try:
        await update.message.delete()
    except Exception:
        pass

    if text == "❌ Отмена":
        return await cancel_creation(update, context)

    try:
        chosen = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
        ])
        # редактируем последнее "служебное" сообщение создания
        chat_id = update.effective_chat.id
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Неверный формат даты.*\n\n"
            "Введите дату в формате *ДД.ММ.ГГГГ*\n"
            "Пример: *25.12.2026*",
            kb
        )
        return INPUT_DATE_MANUAL

    today = datetime.now().date()
    if chosen < today:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
        ])
        chat_id = update.effective_chat.id
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Нельзя выбрать прошедшую дату.*\n\n"
            "Введите будущую дату в формате *ДД.ММ.ГГГГ*.",
            kb
        )
        return INPUT_DATE_MANUAL

    context.user_data["trip_date_only"] = chosen

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"📅 *Дата:* {chosen.strftime('%d.%m.%Y')}\n\n"
            "⏰ *Выберите время поездки:*"
        ),
        reply_markup=_trip_time_choice_kb()
    )
    context.user_data.setdefault("creation_messages", []).append(msg.message_id)
    return INPUT_TIME


async def input_trip_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод времени (ЧЧ:ММ), собираем datetime и переходим к местам."""
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # чистый чат: запомним и удалим ввод пользователя
    context.user_data.setdefault("user_messages", []).append(update.message.message_id)
    try:
        await update.message.delete()
    except Exception:
        pass

    if text == "❌ Отмена":
        return await cancel_creation(update, context)

    date_only = context.user_data.get("trip_date_only")
    if not date_only:
        # если потеряли состояние — вернём на выбор даты
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Сегодня", callback_data="trip_date_today"),
             InlineKeyboardButton("📅 Завтра", callback_data="trip_date_tomorrow")],
            [InlineKeyboardButton("📝 Другая дата", callback_data="trip_date_manual")],
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")],
        ])
        await edit_creation_message(context, chat_id, "📅 *Выберите дату поездки:*", kb)
        return INPUT_DATE_SELECT

    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
        ])
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Неверный формат времени.*\n\n"
            "Введите время в формате *ЧЧ:ММ*\n"
            "Пример: *14:30*",
            kb
        )
        return INPUT_TIME

    trip_dt = datetime.combine(date_only, t)
    if trip_dt < datetime.now():
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
        ])
        await edit_creation_message(
            context,
            chat_id,
            "❌ *Нельзя создать поездку в прошлом.*\n\n"
            "Введите будущее время (ЧЧ:ММ).",
            kb
        )
        return INPUT_TIME

    # ✅ как раньше: дальше код ожидает context.user_data["date"]
    context.user_data["date"] = trip_dt
    context.user_data["date_end"] = trip_dt
    context.user_data["time_mode"] = "exact"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]
    ])
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📅 Дата и время: {trip_dt.strftime('%d.%m.%Y %H:%M')}\n\n"
            "Выберите количество свободных мест (1–5):\n\n"
            "Нажмите кнопку ниже."
        ),
        reply_markup=_creation_seats_keyboard()
    )
    context.user_data.setdefault("creation_messages", []).append(msg.message_id)
    return INPUT_SEATS

async def input_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет дату и запрашивает количество мест."""
    text = update.message.text
    
    # Сохраняем ID сообщения пользователя
    if 'user_messages' not in context.user_data:
        context.user_data['user_messages'] = []
    context.user_data['user_messages'].append(update.message.message_id)
    
    # Проверяем, не нажата ли кнопка отмены
    if text == "❌ Отмена":
        return await cancel_creation(update, context)
    
    try:
        trip_date = datetime.strptime(text, "%d.%m.%Y %H:%M")
        
        # Проверяем, не введена ли прошедшая дата
        if trip_date < datetime.now():
            keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            msg = await update.message.reply_text(
                "❌ Нельзя создать поездку в прошлом!\n\n"
                "Введите будущую дату и время (ДД.ММ.ГГГГ ЧЧ:ММ):\n\n"
                "💡 *Пример:* 25.12.2024 14:30",
                reply_markup=_creation_seats_keyboard()
            )
            context.user_data['creation_messages'].append(msg.message_id)
            return INPUT_DATE
            
        context.user_data['date'] = trip_date
        
        keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = await update.message.reply_text(
            f"📅 *Дата и время:* {trip_date.strftime('%d.%m.%Y %H:%M')}\n\n"
            "Выберите количество свободных мест (1–5):\n\n"
            "Нажмите кнопку ниже.",
            reply_markup=_creation_seats_keyboard()
        )
        context.user_data['creation_messages'].append(msg.message_id)
        return INPUT_SEATS
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = await update.message.reply_text(
            "❌ Неверный формат даты!\n\n"
            "Пожалуйста, введите в формате *ДД.ММ.ГГГГ ЧЧ:ММ*\n\n"
            "💡 *Пример:* 25.12.2024 14:30",
            reply_markup=reply_markup
        )
        context.user_data['creation_messages'].append(msg.message_id)
        return INPUT_DATE


async def input_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет количество мест (1–5) и запрашивает цену. Основной ввод — инлайн-кнопками."""
    text = update.message.text

    # Сохраняем ID сообщения пользователя
    context.user_data.setdefault('user_messages', []).append(update.message.message_id)

    # Старый вариант отмены (на всякий случай)
    if text == "❌ Отмена":
        return await cancel_creation(update, context)

    try:
        seats = int((text or "").strip())
        if seats < 1 or seats > 5:
            raise ValueError

        context.user_data['seats'] = seats

        msg = await update.message.reply_text(
            (
                f"💺 *Количество мест:* {seats}\n\n"
                "Теперь введите цену за место:\n\n"
                "💰 Введите цену (число).\n"
                "0 — бесплатно.\n"
                ""
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
            )
        )
        context.user_data.setdefault('creation_messages', []).append(msg.message_id)
        return INPUT_PRICE

    except ValueError:
        msg = await update.message.reply_text(
            (
                "❌ Неверное количество мест.\n\n"
                "Выберите количество мест (1–5) кнопками ниже "
                "или введите число от 1 до 5."
            ),
            reply_markup=_creation_seats_keyboard()
        )
        context.user_data.setdefault('creation_messages', []).append(msg.message_id)
        return INPUT_SEATS


async def input_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет цену, создаёт поездку, очищает чат и показывает карточку успеха."""
    text = update.message.text
    chat_id = update.effective_chat.id

    # Инициализируем списки, если их нет
    context.user_data.setdefault("user_messages", [])
    context.user_data.setdefault("creation_messages", [])

    # Проверяем, не нажата ли кнопка отмены
    if text == "❌ Отмена":
        return await cancel_creation(update, context)

    # Парсим цену
    try:
        price = float(text)
        if price < 0:
            raise ValueError
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await update.message.reply_text(
            "❌ Неверный формат цены!\n\n"
            "Введите число (например: 500)\n"
            "0 — бесплатно.",
            reply_markup=reply_markup
        )
        context.user_data["creation_messages"].append(msg.message_id)
        return INPUT_PRICE

    # Сохраняем ID сообщения пользователя (ввод цены), чтобы тоже удалить при очистке
    context.user_data["user_messages"].append(update.message.message_id)
    context.user_data["price"] = price

    # Создаём поездку в БД
    user = update.effective_user
    trip = None

    with Session() as session:
        try:
            trip = Trip(
                driver_id=user.id,
                driver_name=user.full_name,
                departure_point=context.user_data.get("departure"),
                destination_point=context.user_data.get("destination"),
                date=context.user_data.get("date"),
                end_date=context.user_data.get("date_end") or context.user_data.get("date"),
                time_mode=context.user_data.get("time_mode"),
                seats_available=context.user_data.get("seats"),
                price=price,
                car_info=None,   # если колонка есть — оставляем None
                is_active=True
            )
            session.add(trip)
            session.commit()
            session.refresh(trip)
            created_trip_id = trip.id
        except Exception as e:
            logging.error(f"Ошибка при создании поездки: {e}")

            # На ошибке тоже чистим диалог, чтобы не оставлять мусор
            message_ids_to_delete = set(context.user_data.get("user_messages", []) + context.user_data.get("creation_messages", []))
            context.user_data.clear()

            for mid in message_ids_to_delete:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Не удалось создать поездку. Попробуйте ещё раз позже.",
            )
            return ConversationHandler.END

    # Удаляем сообщения диалога создания (пользователь + бот)
    # 🔔 Уведомления о новой поездке (если включены в настройках)
    if created_trip_id:
        try:
            await notifications_module.notify_new_trip(context, created_trip_id)
        except Exception as e:
            logging.warning(f"notify_new_trip failed: {e}")

    message_ids_to_delete = set(context.user_data.get("user_messages", []) + context.user_data.get("creation_messages", []))

    # Очищаем user_data перед отправкой итоговой карточки
    context.user_data.clear()

    for mid in message_ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            await asyncio.sleep(0.05)
        except Exception:
            pass

    # Итоговая карточка
    text_ok = render_trip_card(
        title="✅ Поездка создана",
        date=getattr(trip, "date", None),
        time_str=format_trip_time(trip),
        departure=getattr(trip, "departure_point", "—"),
        destination=getattr(trip, "destination_point", "—"),
        seats_available=int(getattr(trip, "seats_available", 0) or 0),
        price=getattr(trip, "price", None),
        action_hint="Управление поездкой — в разделе «Мои поездки»",
    )

    # (опционально) можно дать кнопку "Закрыть" — у тебя уже есть обработчик close_trip_created
    keyboard = [[InlineKeyboardButton("📋 Мои поездки", callback_data="driver_open_my_trips")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text_ok,
        reply_markup=reply_markup
    )

    # ✅ трекаем карточку "Поездка успешно создана" для кнопки 🗑️ Очистить историю
    try:
        notifications_module.track_ui_message(context, chat_id, sent.message_id)
    except Exception:
        pass

    return ConversationHandler.END

async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена создания поездки без сообщений (чистый чат)."""

    chat_id = update.effective_chat.id

    # Удаляем сообщения бота (карточки создания)
    for msg_id in context.user_data.get("creation_messages", []):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    # Удаляем сообщения пользователя (вводимые шаги)
    for msg_id in context.user_data.get("user_messages", []):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    # Если это callback-кнопка — удаляем и её сообщение
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass

    # Чистим данные сценария
    context.user_data.pop("creation_messages", None)
    context.user_data.pop("user_messages", None)
    context.user_data.pop("departure", None)
    context.user_data.pop("destination", None)
    context.user_data.pop("creating_field", None)

    return ConversationHandler.END

# ========== ПОИСК ПОЕЗДОК (обновленная версия) ==========

async def search_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает дату для поиска поездок с inline-клавиатурой"""
    message = "📅 Выберите дату для поиска поездок:"
    
    trigger_id = context.user_data.get("search_trigger_msg_id") or update.message.message_id
    reply_markup = keyboards.get_date_selection_keyboard(cancel_cb=f"date_cancel_{trigger_id}")

    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, reply_markup=reply_markup)

async def handle_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод даты для поиска."""
    text = update.message.text
    chat_id = update.effective_chat.id

    def _cleanup_custom_prompt():
        prompt_id = context.user_data.pop("search_custom_prompt_bot_msg_id", None)
        if prompt_id:
            return prompt_id
        return None

    try:
        search_date = datetime.strptime(text, "%d.%m.%Y").date()

        # запоминаем сообщение пользователя (дата) — на всякий случай
        context.user_data["search_user_msg_id"] = update.message.message_id

        # ✅ Чистый чат: удаляем введённую дату
        try:
            await update.message.delete()
        except Exception:
            pass

        # ✅ Удаляем хвост: сообщение бота "Введите дату..." (если был выбран date_custom)
        prompt_id = _cleanup_custom_prompt()
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass

        await show_trips_for_date(update, context, search_date)
        return

    except ValueError:
        # Если это не дата, проверяем, не команда ли меню
        if text in [
            "🚗 Создать поездку", "🔍 Найти поездку", "📋 Мои поездки",
            "🎫 Мои бронирования", "❓ Помощь", "⚙️ Настройки", "🔙 Назад"
        ]:
            return

        # ✅ Чистый чат: удаляем неверный ввод тоже
        try:
            await update.message.delete()
        except Exception:
            pass

        # ✅ Удаляем хвост: сообщение бота "Введите дату..." (даже если дата неверная)
        prompt_id = _cleanup_custom_prompt()
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass

        keyboard = [[InlineKeyboardButton("❌ Закрыть", callback_data="close_date_error")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        bot_msg = await update.effective_chat.send_message(
            "❗ Неверный формат даты.\nПример: 25.12.2026",
            reply_markup=reply_markup
        )

        # Сохраняем ID сообщений (на случай если удалить не удалось)
        context.user_data["date_error_user_msg_id"] = update.message.message_id
        context.user_data["date_error_bot_msg_id"] = bot_msg.message_id
        return

async def show_trips_for_date(update, context, search_date):
    """
    Показывает поездки на дату БЕЗ отдельной карточки "Найдено X".
    Каждая поездка — отдельной карточкой с кнопкой "Подробнее".
    """
    # ✅ Добиваем хвост: удаляем сообщение пользователя с датой (если осталось)
    user_msg_id = context.user_data.get("search_user_msg_id")
    if user_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=user_msg_id)
        except Exception:
            pass

    # ✅ Удаляем триггерное сообщение пользователя "🔍 Найти поездку" перед выводом результатов (чистый чат)
    trigger_msg_id = context.user_data.get("search_trigger_msg_id")
    if trigger_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=trigger_msg_id)
        except Exception:
            pass

    with Session() as session:
        # --- User search filter (optional) ---
        dep_filter = None
        dest_filter = None
        enabled_filter = False

        try:
            bu = session.query(BotUser).filter(BotUser.telegram_id == update.effective_user.id).one_or_none()
            if bu is not None:
                enabled_filter = bool(getattr(bu, "search_filter_enabled", False))
                dep_filter = (getattr(bu, "search_filter_departure", None) or "").strip() or None
                dest_filter = (getattr(bu, "search_filter_destination", None) or "").strip() or None
        except Exception:
            enabled_filter = False
            dep_filter = None
            dest_filter = None

        q = session.query(Trip).filter(
            Trip.date >= datetime.combine(search_date, datetime.min.time()),
            Trip.date < datetime.combine(search_date, datetime.max.time()),
            Trip.is_active == True,
            Trip.seats_available > 0,
            func.coalesce(Trip.end_date, Trip.date) >= datetime.now()
        )

        # Если фильтр включён — добавляем условия по маршруту (по одному из полей или по обоим)
        if enabled_filter:
            if dep_filter:
                q = q.filter(func.lower(Trip.departure_point) == func.lower(dep_filter))
            if dest_filter:
                q = q.filter(func.lower(Trip.destination_point) == func.lower(dest_filter))

        trips = q.order_by(Trip.date.asc()).all()

    formatted_date = search_date.strftime('%d.%m.%Y')

    # Если поездок нет — оставляем единое сообщение (как было), с кнопкой закрыть
    if not trips:
        text = (
            f"🔍 *Поиск поездок*\n"
            f"📅 Дата: `{formatted_date}`\n\n"
            "🚫 Поездки не найдены."
        )
        trigger_id = context.user_data.get("search_trigger_msg_id") or context.user_data.get("search_user_msg_id") or 0
        keyboard = [
            [InlineKeyboardButton("🔙 Назад к поиску", callback_data=f"search_back_{trigger_id}")],
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"close_search_results_{trigger_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        bot_msg = await send_tracked_message(
            context,
            update.effective_chat.id,
            text,
            reply_markup=reply_markup
        )

        # сохраняем как раньше (это полезно для точечного удаления результатов поиска)
        context.user_data["search_bot_msg_id"] = bot_msg.message_id

        # и добавим в накопительный список поиска (если используешь его для clear_search_results)
        context.user_data.setdefault("search_all_msg_ids", [])
        context.user_data["search_all_msg_ids"].append(bot_msg.message_id)

        return

    trips_to_show = trips[:10]

      # Список сообщений результатов
    context.user_data.setdefault("search_bot_msg_ids", [])
    context.user_data.setdefault("search_all_msg_ids", [])

    for trip in trips_to_show:
        card_text, reply_markup = notifications_module.build_trip_search_card(trip)

        msg = await update.message.reply_text(
            card_text,
            reply_markup=reply_markup
        )
        context.user_data["search_bot_msg_ids"].append(msg.message_id)
        context.user_data["search_all_msg_ids"].append(msg.message_id)

    # Если поездок больше, чем показали — короткое уведомление (ОДИН раз, вне цикла)
    if len(trips) > len(trips_to_show):
        info = f"ℹ️ Показано {len(trips_to_show)} из {len(trips)} поездок на {formatted_date}."
        msg = await send_tracked_message(context, update.effective_chat.id, info)
        context.user_data["search_bot_msg_ids"].append(msg.message_id)
        context.user_data["search_all_msg_ids"].append(msg.message_id)

async def clear_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищает только результаты последнего поиска поездок (без инструкции Telegram)."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    msg_ids: set[int] = set()
    
    # Все карточки поисков за сессию (накапливаем)
    for mid in context.user_data.get("search_all_msg_ids", []) or []:
        if isinstance(mid, int):
            msg_ids.add(mid)

    # Карточки результатов (списком)
    for mid in context.user_data.get("search_bot_msg_ids", []) or []:
        if isinstance(mid, int):
            msg_ids.add(mid)

    # Единичные сообщения результатов/ошибок
    for key in (
        "search_bot_msg_id",
        "search_custom_prompt_bot_msg_id",
        "date_error_bot_msg_id",
    ):
        mid = context.user_data.get(key)
        if isinstance(mid, int):
            msg_ids.add(mid)

    # Сообщения пользователя (дата/триггер/ошибка)
    for key in (
        "search_user_msg_id",
        "search_trigger_msg_id",
        "date_error_user_msg_id",
    ):
        mid = context.user_data.get(key)
        if isinstance(mid, int):
            msg_ids.add(mid)

    # Пытаемся удалить всё, что нашли
    for mid in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    # Чистим состояние поиска
    for k in list(context.user_data.keys()):
        if k.startswith("search_") or k.startswith("date_error_"):
            context.user_data.pop(k, None)
            
    context.user_data.pop("search_all_msg_ids", None)

def _botdata_get_history_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[int]:
    try:
        app = getattr(context, "application", None)
        if app is None:
            return []
        store = app.bot_data.get(getattr(notifications_module, "BOTDATA_HISTORY_KEY", "ui_history_msg_ids"), {})
        ids = store.get(int(chat_id), [])
        return [int(x) for x in ids if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]
    except Exception:
        return []


def _botdata_clear_history_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        app = getattr(context, "application", None)
        if app is None:
            return
        key = getattr(notifications_module, "BOTDATA_HISTORY_KEY", "ui_history_msg_ids")
        store = app.bot_data.get(key)
        if isinstance(store, dict) and int(chat_id) in store:
            store.pop(int(chat_id), None)
    except Exception:
        pass


async def clear_tracked_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Удаляет все “карточки” (поиск + уведомления) для текущего чата.
    Возвращает True, если реально было что удалять.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return False

    msg_ids: set[int] = set()

    # 1) Поиск (то, что уже хранили в user_data)
    for mid in (context.user_data.get("search_all_msg_ids") or []):
        if isinstance(mid, int):
            msg_ids.add(mid)
    for mid in (context.user_data.get("search_bot_msg_ids") or []):
        if isinstance(mid, int):
            msg_ids.add(mid)

    for key in ("search_bot_msg_id", "search_custom_prompt_bot_msg_id", "date_error_bot_msg_id"):
        mid = context.user_data.get(key)
        if isinstance(mid, int):
            msg_ids.add(mid)

    # 2) Уведомления (то, что сохранили в bot_data)
    for mid in _botdata_get_history_ids(context, chat_id):
        msg_ids.add(mid)

    if not msg_ids:
        return False

    # Пытаемся удалить всё
    for mid in sorted(msg_ids):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    # Чистим состояние поиска
    for k in list(context.user_data.keys()):
        if k.startswith("search_") or k.startswith("date_error_"):
            context.user_data.pop(k, None)
    context.user_data.pop("search_all_msg_ids", None)

    # Чистим историю уведомлений
    _botdata_clear_history_ids(context, chat_id)

    return True

# ========== НОВЫЕ ФУНКЦИИ ДЛЯ МЕНЮ ==========

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает инструкцию и контакты поддержки."""

    help_text = (
        "❓ Помощь\n\n"
        "🚗 Создать поездку\n"
        "1) Выберите маршрут (откуда → куда)\n"
        "2) Дата и время\n"
        "3) Места и цена\n\n"
        "🔍 Найти поездку\n"
        "1) Выберите дату поиска\n"
        "2) Откройте карточку и нажмите «Забронировать»\n\n"
        "🎫 Мои бронирования — статус заявок\n"
        "📋 Мои поездки — управление поездками и заявками\n\n"
        "✉️ Связь: djidayex@yandex.ru"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✖️ Закрыть", callback_data="help_close")]
    ])

    await update.message.reply_text(
        help_text,
        reply_markup=kb,
    )

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False):
    """Показывает меню настроек с inline-кнопками."""
    text = "⚙️ Настройки\n\nВыберите раздел:"

    trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

    keyboard = [
        [InlineKeyboardButton("👤 Мой профиль", callback_data=f"settings_profile_{trigger_id}")],
        [InlineKeyboardButton("🔎 Фильтр поиска", callback_data=f"settings_search_filter_{trigger_id}")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup
        )
    else:
        # оставляем главное ReplyKeyboard как раньше — чтобы структура не ломалась
        await update.message.reply_text(
            text,
            reply_markup=reply_markup
        )

async def show_my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = True):
    """Показывает профиль пользователя (ID, имя, username, статистика поездок/броней)."""
    user = update.effective_user
    if not user:
        return

    with Session() as session:
        trips_count = session.query(Trip).filter(Trip.driver_id == user.id).count()
        bookings_count = session.query(Booking).filter(Booking.passenger_id == user.id).count()

    username = f"@{user.username}" if user.username else "—"
    full_name = user.full_name if user.full_name else "—"

    line = "═" * 25
    text = (
        "👤 Мой профиль\n"
        f"{line}\n\n"
        f"👋 Имя: {full_name}\n"
        f"🔗 Username: {username}\n\n"
        f"🚗 Поездок создано: {trips_count}\n"
        f"🎫 Бронирований сделано: {bookings_count}\n"
    )

    trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

    keyboard = [
        [InlineKeyboardButton("🔙 Назад в настройки", callback_data=f"settings_back_{trigger_id}")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup
        )


async def show_search_filter_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = True):
    """Экран настроек фильтра поиска (вкл/выкл + пункты отправления/назначения)."""
    user = update.effective_user
    if not user:
        return

    trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            bu = BotUser(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=bool(getattr(user, "is_bot", False)),
                chat_id=update.effective_chat.id if update.effective_chat else None,
                created_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
                search_filter_enabled=False,
                search_filter_departure=None,
                search_filter_destination=None,
            )
            session.add(bu)
            session.commit()
            session.refresh(bu)

        enabled = bool(getattr(bu, "search_filter_enabled", False))
        dep = getattr(bu, "search_filter_departure", None) or "—"
        dest = getattr(bu, "search_filter_destination", None) or "—"

    status = "✅ Включён" if enabled else "⛔ Выключен"

    text = (
        "🔎 Фильтр поиска\n\n"
        f"📌 Статус: {status}\n"
        f"📍 Откуда: {dep}\n"
        f"🎯 Куда: {dest}"
    )

    toggle_title = "🔴 Выключить фильтр" if enabled else "🟢 Включить фильтр"

    keyboard = [
        [InlineKeyboardButton(toggle_title, callback_data=f"sf_toggle_{trigger_id}")],
        [InlineKeyboardButton("✏️ Задать «Откуда»", callback_data=f"sf_set_dep_{trigger_id}")],
        [InlineKeyboardButton("✏️ Задать «Куда»", callback_data=f"sf_set_dest_{trigger_id}")],
        [InlineKeyboardButton("🧹 Сбросить маршрут", callback_data=f"sf_clear_{trigger_id}")],
        [InlineKeyboardButton("🔙 Назад в настройки", callback_data=f"settings_back_{trigger_id}")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def _edit_search_filter_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup):
    """Безопасное редактирование одного сообщения (используем для экрана фильтра)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        pass


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда настроек"""
    await show_settings_menu(update, context, edit=False)

# ========== ОБНОВЛЕННЫЕ СУЩЕСТВУЮЩИЕ ФУНКЦИИ С EMOJI ==========

async def send_my_trips_cards(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет карточки 'Мои поездки' в чат (работает и для callback, и для обычных сообщений)."""

    # --- Singleton-поведение для экрана "Мои поездки":
    # Перед показом нового списка удаляем предыдущие сообщения этого экрана,
    # чтобы карточки не плодились при переходах из разных мест.
    prev_ids = context.user_data.get("my_trips_msg_ids")
    if isinstance(prev_ids, list) and prev_ids:
        for mid in list(prev_ids):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=int(mid))
            except Exception:
                pass
        context.user_data["my_trips_msg_ids"] = []
    with Session() as session:
        all_trips = (
            session.query(Trip)
            .filter(Trip.driver_id == user_id)
            .order_by(Trip.date.asc())
            .all()
        )

    if not all_trips:
        # Кнопка "Закрыть" для чистого чата: удаляем это сообщение и запрос пользователя "Мои поездки"
        user_msg_id = context.user_data.get("last_user_msg_id")
        close_id = user_msg_id if isinstance(user_msg_id, int) else 0
        keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_no_active_trips_{close_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await send_tracked_message(
            context,
            chat_id,
            "📭 У вас нет созданных поездок.",
            reply_markup=reply_markup
        )
        context.user_data.setdefault("my_trips_msg_ids", []).append(msg.message_id)
        return

    active_trips = [t for t in all_trips if t.is_active and t.date >= datetime.now()]

    # Подсчёт уже забронированных мест (pending+confirmed) по каждой поездке
    trip_ids = [t.id for t in active_trips]
    booked_map: dict[int, int] = {}
    if trip_ids:
        with Session() as session:
            rows = (
                session.query(Booking.trip_id, func.coalesce(func.sum(Booking.seats_booked), 0))
                .filter(
                    Booking.trip_id.in_(trip_ids),
                    Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
                )
                .group_by(Booking.trip_id)
                .all()
            )
        booked_map = {int(tid): int(cnt or 0) for tid, cnt in rows}


    if not active_trips:
        # Кнопка "Закрыть" для чистого чата: удаляем это сообщение и запрос пользователя "Мои поездки"
        user_msg_id = context.user_data.get("last_user_msg_id")
        close_id = user_msg_id if isinstance(user_msg_id, int) else 0

        keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_no_active_trips_{close_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await send_tracked_message(
            context,
            chat_id,
            "📭 У вас нет активных поездок.",
            reply_markup=reply_markup
        )
        context.user_data.setdefault("my_trips_msg_ids", []).append(msg.message_id)
        return

    for trip in active_trips:
        is_expired = trip.date < datetime.now()
        message = render_trip_card(
            title="🚗 Поездка",
            status="🟢 Активна",
            date=getattr(trip, "date", None),
            time_str=format_trip_time(trip),
            departure=getattr(trip, "departure_point", "—"),
            destination=getattr(trip, "destination_point", "—"),
            seats_available=int(getattr(trip, "seats_available", 0) or 0),
            price=getattr(trip, "price", None),
        )
        booked = int(booked_map.get(trip.id, 0) or 0)
        total = booked + int(getattr(trip, "seats_available", 0) or 0)
        message += f"\n👥 Забронировано: {booked} из {total}"


        if is_expired:
            keyboard = [
                [InlineKeyboardButton("👥 Бронирования", callback_data=f"trip_bookings_{trip.id}")],
                [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_trip_{trip.id}")]
            ]
            message += "\n\n⚠️ Поездка уже прошла, отмена невозможна"
        else:
            trigger_id = context.user_data.get("my_trips_trigger_msg_id") or 0


            # ID сообщения пользователя-триггера ("📋 Мои поездки") — для чистого чата
            close_id = context.user_data.get("last_user_msg_id")
            close_id = close_id if isinstance(close_id, int) else 0
            keyboard = [
                [InlineKeyboardButton("👥 Бронирования", callback_data=f"trip_bookings_{trip.id}_{trigger_id}")],
                [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_trip_{trip.id}_{trigger_id}")],
                [InlineKeyboardButton("❌ Отменить поездку", callback_data=f"cancel_trip_{trip.id}_{trigger_id}")],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_my_trip_card_{close_id}")],
            ]

        msg = await send_tracked_message(
            context,
            chat_id,
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data.setdefault("my_trips_msg_ids", []).append(msg.message_id)

async def my_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает активные поездки пользователя."""
    try:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        await send_my_trips_cards(chat_id, user_id, context)
    except Exception as e:
        logging.error(f"Ошибка в my_trips: {str(e)}")
        if update.message:
            await update.message.reply_text("❌ Произошла ошибка при загрузке поездок.")
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Произошла ошибка при загрузке поездок.")

# ========== ОБНОВЛЕННАЯ ФУНКЦИЯ МОИ БРОНИРОВАНИЯ ==========

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """..."""
    chat_id = update.effective_chat.id

    # --- Singleton-поведение для экрана "Мои бронирования":
    # Перед показом нового списка удаляем предыдущие сообщения этого экрана,
    # чтобы карточки не плодились при переходах.
    prev_ids = context.user_data.get("my_bookings_msg_ids")
    if isinstance(prev_ids, list) and prev_ids:
        for mid in list(prev_ids):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=int(mid))
            except Exception:
                pass
        context.user_data["my_bookings_msg_ids"] = []

    with Session() as session:
        try:
            bookings = (
                session.query(Booking)
                .filter(
                    Booking.passenger_id == update.effective_user.id,
                    Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
                )
                .order_by(Booking.booking_time.desc())
                .all()
            )

            # ✅ ВАЖНО: даже если в БД есть записи PENDING/CONFIRMED, мы показываем ТОЛЬКО активные брони
            # (поездка активна и не в прошлом). Иначе пользователь нажимает "Мои бронирования" и видит "ничего".
            now = datetime.now()
            active_bookings: list[Booking] = []
            for b in bookings:
                try:
                    t = b.trip
                    if not t:
                        continue
                    if not getattr(t, "is_active", False):
                        continue
                    if getattr(t, "date", None) and t.date < now:
                        continue
                    active_bookings.append(b)
                except Exception:
                    continue

            if not active_bookings:
                user_msg_id = context.user_data.get("last_user_msg_id")
                close_id = user_msg_id if isinstance(user_msg_id, int) else 0

                keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_no_bookings_{close_id}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                msg = await send_tracked_message(
                    context,
                    chat_id,
                    "📭 У вас нет активных бронирований.",
                    reply_markup=reply_markup
                )
                context.user_data.setdefault("my_bookings_msg_ids", []).append(msg.message_id)
                return

            # message_id триггера "🎫 Мои бронирования" — для чистого чата
            trigger_id = context.user_data.get("last_user_msg_id")
            trigger_id = trigger_id if isinstance(trigger_id, int) else 0

            for booking in active_bookings:
                trip = booking.trip
                # trip уже проверен выше, но оставим безопасный guard
                if not trip or not trip.is_active or (trip.date and trip.date < now):
                    continue

                status_map = {
                    BookingStatus.PENDING.value: "⏳ Ожидает подтверждения",
                    BookingStatus.CONFIRMED.value: "✅ Подтверждено",
                    BookingStatus.EXPIRED.value: "⌛ Истекло",
                }
                status = status_map.get(booking.status, booking.status)

                # Пытаемся получить username водителя (если доступно)
                driver_username = None
                try:
                    driver_chat = await context.bot.get_chat(trip.driver_id)
                    if driver_chat and getattr(driver_chat, "username", None):
                        driver_username = driver_chat.username
                except Exception:
                    driver_username = None

                message = render_booking_card(
                    title="🎫 Бронирование",
                    date=getattr(trip, "date", None),
                    time_str=format_trip_time(trip),
                    departure=getattr(trip, "departure_point", "—"),
                    destination=getattr(trip, "destination_point", "—"),
                    seats_booked=int(getattr(booking, "seats_booked", 0) or 0),
                    price=getattr(trip, "price", None),
                    status=status,
                    driver_name=getattr(trip, "driver_name", None),
                    driver_username=driver_username,
                )

                keyboard_rows = [
                    [InlineKeyboardButton("❌ Отменить бронирование", callback_data=f"cancel_booking_{booking.id}")]
                ]

                if booking.status == BookingStatus.CONFIRMED.value:
                    keyboard_rows.append(
                        [InlineKeyboardButton("⭐ Оценить поездку", callback_data=f"passenger_open_trip_rating_{booking.id}")]
                    )

                keyboard_rows.append(
                    [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_my_booking_card_{trigger_id}")]
                )

                msg = await send_tracked_message(
                    context,
                    chat_id,
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
                context.user_data.setdefault("my_bookings_msg_ids", []).append(msg.message_id)

        except Exception as e:
            logging.error(f"Ошибка в my_bookings: {e}")
            await context.bot.send_message(chat_id=chat_id, text="❌ Произошла ошибка при загрузке бронирований.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения и нажатия кнопок меню"""
    text = update.message.text
    chat_id = update.effective_chat.id

    context.user_data["last_user_msg_id"] = update.message.message_id

    # Проверяем команды меню - они ВСЕГДА должны иметь приоритет
    menu_commands = ["🚗 Создать поездку", "🔍 Найти поездку", "📋 Мои поездки",
                     "🎫 Мои бронирования", "❓ Помощь", "⚙️ Настройки",
                     "🗑️ Очистить историю", "❌ Отмена"]

    # Если это команда меню - обрабатываем её, независимо от состояния
    if text in menu_commands:
        # Очищаем все временные состояния при переходе в меню
        for k in ("cancelling_booking_id", "cancelling_booking",
                  "cancelling_trip_id", "editing_field", "editing_trip_id"):
            if k in context.user_data:
                del context.user_data[k]

        # Обрабатываем команду меню
        if text == "🚗 Создать поездку":
            try:
                await update.message.delete()
            except Exception:
                pass
            await new_trip(update, context)

        elif text == "🔍 Найти поездку":
            context.user_data["search_trigger_msg_id"] = update.message.message_id
            try:
                await update.message.delete()
            except Exception:
                pass
            await search_trips(update, context)

        elif text == "📋 Мои поездки":
            context.user_data["my_trips_trigger_msg_id"] = update.message.message_id
            try:
                await update.message.delete()
            except Exception:
                pass
            await my_trips(update, context)

        elif text == "🎫 Мои бронирования":
            context.user_data["my_bookings_trigger_msg_id"] = update.message.message_id
            try:
                await update.message.delete()
            except Exception:
                pass
            await my_bookings(update, context)

        elif text == "❓ Помощь":
            try:
                await update.message.delete()
            except Exception:
                pass
            await help_command(update, context)

        elif text == "⚙️ Настройки":
            context.user_data["settings_trigger_msg_id"] = update.message.message_id
            try:
                await update.message.delete()
            except Exception:
                pass
            await show_settings_menu(update, context, edit=False)

        elif text == "🗑️ Очистить историю":
            try:
                await update.message.delete()
            except Exception:
                pass

            removed = await clear_tracked_cards(update, context)
            if not removed:
                await clear_chat_history_simple(update, context)

        elif text == "❌ Отмена":
            try:
                await update.message.delete()
            except Exception:
                pass
            await cancel_creation(update, context)

        return

    # ====== ВАЖНО: модуль настроек (текстовый ввод) должен иметь приоритет над остальным ======
    # Это нужно для сценария: Настройки -> Фильтр -> "Введите Откуда/Куда" -> пользователь пишет текст
    if await settings_module.handle_text(update, context):
        return

    # Проверяем, не в процессе ли редактирования (только если не команда меню)
    if 'editing_field' in context.user_data and 'editing_trip_id' in context.user_data:
        await handle_edit_input(update, context)
        return

    # Проверяем, не в процессе ли отмены поездки (только если не команда меню)
    if 'cancelling_trip_id' in context.user_data:
        await handle_trip_cancellation(update, context)
        return

    # Проверяем, не в процессе ли отмены бронирования (только если не команда меню)
    if 'cancelling_booking_id' in context.user_data:
        booking_id = context.user_data['cancelling_booking_id']
        text_lower = text.lower()

        if text_lower in ['да', 'yes', 'ок', 'ok', 'подтвердить']:
            with Session() as session:
                try:
                    booking = session.query(Booking).get(booking_id)
                    if booking and booking.passenger_id == update.effective_user.id:
                        trip = booking.trip
                        trip.seats_available += booking.seats_booked
                        booking.status = BookingStatus.CANCELLED.value
                        session.commit()

                        await booking_module.notify_driver_booking_cancelled(context.bot, booking)

                        await update.message.reply_text(
                            "✅ Бронирование отменено. Место возвращено в общий доступ."
                        )
                except Exception as e:
                    logging.error(f"Ошибка при отмене бронирования: {e}")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="❌ Произошла ошибка при загрузке бронирований."
                    )

        elif text_lower in ['нет', 'no', 'не', 'отмена']:
            await update.message.reply_text("✅ Отмена бронирования отменена.")

        else:
            await update.message.reply_text(
                "❓ Вы подтверждаете отмену бронирования?\n"
                "Напишите 'да' для подтверждения или 'нет' для отмены."
            )
            return  # ждём корректный ответ

        # Очищаем данные
        context.user_data.pop('cancelling_booking_id', None)
        context.user_data.pop('cancelling_booking', None)
        return

    # Если это не команда меню и не ответ на диалог, пробуем обработать как поиск
    await handle_search_input(update, context)
        
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на инлайн-кнопки."""
    query = update.callback_query
    data = query.data

    # ====== МОДУЛЬ: настройки / фильтр / уведомления ======
    if settings_module.can_handle_callback(data):
        handled = await settings_module.handle_callback(update, context)
        if handled:
            return

    
    async def deny(text: str) -> None:
        # Единый стиль ошибок: системный alert, без мусора в чате
        await _answer_once(text, show_alert=True)

    # Telegram позволяет ответить на callback только 1 раз.
    # Поэтому НЕ отвечаем глобально в начале — иначе show_alert=True не сработает.
    answered = False

    async def _answer_once(text=None, *, show_alert: bool = False):
        nonlocal answered
        if answered:
            return
        try:
            await query.answer(text=text, show_alert=show_alert)
        except Exception:
            pass
        answered = True

    # ====== HELP: закрыть сообщение помощи ======
    if data == "help_close":
        await _answer_once()
        try:
            await query.message.delete()
        except Exception:
            pass
        return


    # ====== DRIVER: быстрый переход к "Мои поездки" из уведомлений ======
    if data == "driver_open_my_trips":
        await _answer_once()
        chat_id = query.message.chat_id if query.message else query.from_user.id
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            await send_my_trips_cards(chat_id, query.from_user.id, context)
        except Exception:
            # если что-то пошло не так — не роняем обработчик
            pass
        return
    # ====== ПАССАЖИР: подтверждение бронирования после выбора количества мест ======
    # (Patch 1.0+) Вынесено в booking_module.py
    if booking_module.can_handle_callback(data):
        handled = await booking_module.handle_callback(
            update,
            context,
            data=data,
            answer_once=_answer_once,
        )
        if handled:
            return

    # ====== ПАССАЖИР: открыть меню "Оценить поездку" (доступно только после времени выезда) ======
    if data.startswith("passenger_open_trip_rating_"):
        booking_id = int(data.split("_")[-1])
        passenger_id = query.from_user.id
        now = datetime.now()

        with Session() as session:
            booking = session.query(Booking).get(booking_id)
            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                return
            if booking.passenger_id != passenger_id:
                await _answer_once("⚠️ Это бронирование не принадлежит вам.", show_alert=True)
                return

            # 🚫 Нельзя оценивать отменённые/отклонённые/неподтверждённые бронирования (защита от устаревших карточек)
            if booking.status != BookingStatus.CONFIRMED.value:
                msg = "⚠️ Нельзя оценить поездку: бронирование не подтверждено или уже отменено."
                # более точные тексты (опционально)
                if booking.status == BookingStatus.CANCELLED.value:
                    msg = "⚠️ Нельзя оценить отменённую поездку."
                elif booking.status == BookingStatus.REJECTED.value:
                    msg = "⚠️ Нельзя оценить: бронирование было отклонено."
                elif booking.status == BookingStatus.PENDING.value:
                    msg = "⚠️ Нельзя оценить: бронирование ещё не подтверждено."

                await _answer_once(msg, show_alert=True)

                # чтобы не висела устаревшая карточка — можно удалить её (в твоём стиле «чистый чат»)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                return

            trip = booking.trip
            if not trip:
                await _answer_once("❌ Поездка не найдена.", show_alert=True)
                return

            # анти-повтор: если уже поставил оценку — сразу сообщаем
            if getattr(booking, "passenger_rating_driver", None) is not None:
                await _answer_once()
                await query.edit_message_text(
                    text="✅ Вы уже оставили оценку по этой поездке.",
                    reply_markup=keyboards.get_close_only_keyboard(
                        f"close_passenger_rate_driver_{booking_id}"
                    )
                )
                return

            # если время выезда ещё не наступило — запрещаем открытие оценки
            if trip.date and trip.date > now:
                when = trip.date.strftime("%d.%m.%Y %H:%M")
                await _answer_once(
                    f"⏳ Оценка будет доступна после времени выезда: {when}",
                    show_alert=True
                )
                return

        # время выезда наступило -> показываем выбор исхода
        await _answer_once()
        await query.edit_message_text(
            text=(
                "⭐ *Оценить поездку*\n\n"
                "Выберите исход поездки:"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Поездка состоялась", callback_data=f"passenger_trip_completed_{booking_id}")],
                [InlineKeyboardButton("❌ Поездка не состоялась", callback_data=f"passenger_trip_not_completed_{booking_id}")],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_passenger_rate_driver_{booking_id}")]
            ])
        )
        return

    # ====== ПАССАЖИР: выйти из поездки (до выезда = отмена с возвратом мест и уведомлением водителя) ======
    if data.startswith("exit_trip_"):
        booking_id = int(data.split("_")[-1])
        passenger_id = query.from_user.id

        with Session() as session:
            booking = session.query(Booking).get(booking_id)
            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                try:
                    await _answer_once()
                    await query.message.delete()
                except Exception:
                    pass
                return

            if booking.passenger_id != passenger_id:
                await _answer_once("⚠️ Это бронирование не принадлежит вам.", show_alert=True)
                return

            trip = booking.trip
            now = datetime.now()

            # Если поездка уже неактивна или отсутствует — просто убираем бронь из активных
            if not trip or not trip.is_active:
                booking.status = BookingStatus.CANCELLED.value
                session.commit()
                try:
                    await _answer_once()
                    await query.message.delete()
                except Exception:
                    pass
                await _answer_once("✅ Готово.")
                return

            # До времени выезда — отмена участия по аналогии с cancel_booking
            if trip.date and trip.date > now:
                # возвращаем места
                trip.seats_available += booking.seats_booked

                # статус брони
                booking.status = BookingStatus.CANCELLED.value
                session.commit()

                # уведомляем водителя (как при отмене бронирования)
                try:
                    await booking_module.notify_driver_booking_cancelled(context.bot, booking)
                except Exception:
                    pass

                try:
                    await _answer_once()
                    await query.message.delete()
                except Exception:
                    pass

                await _answer_once("✅ Бронирование отменено, место возвращено.")
                return

            # После времени выезда — просто убираем бронь из активных, без возврата и уведомлений
            booking.status = BookingStatus.CANCELLED.value
            session.commit()

        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        await _answer_once("✅ Готово.")
        return


     # ====== ПАССАЖИР: по итогам поездки — "состоялась" ======
    if data.startswith("passenger_trip_completed_"):
        booking_id = int(data.split("_")[-1])
        passenger_id = query.from_user.id

        with Session() as session:
            booking = session.query(Booking).get(booking_id)
            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                return
            if booking.passenger_id != passenger_id:
                await _answer_once("⚠️ Это бронирование не принадлежит вам.", show_alert=True)
                return

            # анти-повтор: уже оценил водителя
            if getattr(booking, "passenger_rating_driver", None) is not None:
                await _answer_once()
                await query.edit_message_text(
                    text="✅ Оценка уже стоит",
                    reply_markup=keyboards.get_close_only_keyboard(
                        f"close_passenger_rate_driver_{booking_id}"
                    )
                )
                return

            # не перезаписываем результат, если он уже есть
            if not getattr(booking, "passenger_trip_result", None):
                booking.passenger_trip_result = "completed"
                session.commit()

        await _answer_once()

        await query.edit_message_text(
            text=(
                "⭐ *Оцените водителя*\n\n"
                "Поставьте оценку от 1 до 5 звёзд.\n"
                "Это поможет улучшить сервис."
            ),
            reply_markup=keyboards.get_driver_rating_keyboard(booking_id)
        )
        return


    # ====== ПАССАЖИР: по итогам поездки — "не состоялась" ======
    if data.startswith("passenger_trip_not_completed_"):
        booking_id = int(data.split("_")[-1])
        passenger_id = query.from_user.id

        with Session() as session:
            booking = session.query(Booking).get(booking_id)
            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                return
            if booking.passenger_id != passenger_id:
                await _answer_once("⚠️ Это бронирование не принадлежит вам.", show_alert=True)
                return

            # анти-повтор: уже оценил водителя
            if getattr(booking, "passenger_rating_driver", None) is not None:
                await _answer_once()
                await query.edit_message_text(
                    text="✅ Вы уже оставили оценку по этой поездке.",
                    reply_markup=keyboards.get_close_only_keyboard(
                        f"close_passenger_rate_driver_{booking_id}"
                    )
                )
                return

            # не перезаписываем результат, если он уже есть
            if not getattr(booking, "passenger_trip_result", None):
                booking.passenger_trip_result = "not_completed"
                session.commit()

        await _answer_once()

        await query.edit_message_text(
            text=(
                "⭐ *Оцените совместную поездку*\n\n"
                "Пожалуйста, поставьте оценку водителю.\n"
                "Если поездка не состоялась — это тоже важно."
            ),
            reply_markup=keyboards.get_driver_rating_keyboard(booking_id)
        )
        return


    # ====== ПАССАЖИР: оценка водителя (1..5) ======
    if data.startswith("passenger_rate_driver_"):
        # формат: passenger_rate_driver_{booking_id}_{stars}
        parts = data.split("_")
        booking_id = int(parts[-2])
        stars = int(parts[-1])
        passenger_id = query.from_user.id

        if stars < 1 or stars > 5:
            await _answer_once("❌ Некорректная оценка.", show_alert=True)
            return

        with Session() as session:
            booking = session.query(Booking).get(booking_id)
            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                return
            if booking.passenger_id != passenger_id:
                await _answer_once("⚠️ Это бронирование не принадлежит вам.", show_alert=True)
                return

            if booking.status != BookingStatus.CONFIRMED.value:
                await _answer_once("⚠️ Нельзя оценить: бронирование отменено или не подтверждено.", show_alert=True)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                return

            # анти-повтор: если уже стоит оценка — не перезаписываем
            if getattr(booking, "passenger_rating_driver", None) is not None:
                await _answer_once()
                await query.edit_message_text(
                    text="✅ Оценка уже сохранена. Спасибо!",
                    reply_markup=keyboards.get_close_only_keyboard(
                        f"close_passenger_rate_driver_{booking_id}"
                    )
                )
                return

            booking.passenger_rating_driver = stars
            booking.passenger_rated_at = datetime.utcnow()
            session.commit()

        await _answer_once()

        await query.edit_message_text(
            text="✅ Спасибо! Оценка сохранена.",
            reply_markup=keyboards.get_passenger_rating_saved_keyboard(booking_id)
        )
        return


    # ====== ПАССАЖИР: закрыть карточку оценки водителя ======
    if data.startswith("close_passenger_rate_driver_"):
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass
        return


        # ====== ПАССАЖИР: ДЕТАЛИ ПОЕЗДКИ ======
    if data.startswith("s_detail_"):
        trip_id = int(data.split("_")[2])

        with Session() as session:
            trip = session.query(Trip).get(trip_id)

        if not trip or not trip.is_active or trip.seats_available <= 0:
            await _answer_once("❌ Поездка недоступна.", show_alert=True)
            return

        text = render_trip_card(
            title="ℹ️ Детали поездки",
            date=getattr(trip, "date", None),
            time_str=format_trip_time(trip),
            departure=getattr(trip, "departure_point", "—"),
            destination=getattr(trip, "destination_point", "—"),
            seats_available=int(getattr(trip, "seats_available", 0) or 0),
            price=getattr(trip, "price", None),
            action_hint="Выберите действие кнопками ниже",
        )

        keyboard = [
            [InlineKeyboardButton("✅ Забронировать", callback_data=f"book_{trip.id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"s_back_{trip.id}")]
        ]

        await _answer_once()

        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return


    if data.startswith("s_back_"):
        trip_id = int(data.split("_")[2])

        with Session() as session:
            trip = session.query(Trip).get(trip_id)

        if not trip or not trip.is_active:
            await _answer_once()
            await query.message.delete()
            return

        card_text = render_trip_card(
            title="🚗 Поездка",
            date=getattr(trip, "date", None),
            time_str=format_trip_time(trip),
            departure=getattr(trip, "departure_point", "—"),
            destination=getattr(trip, "destination_point", "—"),
            seats_available=int(getattr(trip, "seats_available", 0) or 0),
            price=getattr(trip, "price", None),
        )

        keyboard = [[InlineKeyboardButton("ℹ️ Подробнее", callback_data=f"s_detail_{trip.id}")]]

        await _answer_once()

        await query.edit_message_text(
            text=card_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ====== НАСТРОЙКИ: Мой профиль (анти-спам) ======
    if data.startswith("settings_profile"):
        # data может быть "settings_profile" (старое) или "settings_profile_<id>" (новое)
        trigger_id = None
        if data.startswith("settings_profile_"):
            try:
                trigger_id = int(data.split("_")[-1])
            except Exception:
                trigger_id = None

        # fallback для старых кнопок
        if not trigger_id:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        # фиксируем trigger_id на весь этот экран
        context.user_data["settings_trigger_msg_id"] = trigger_id

        await show_my_profile(update, context, edit=True)
        return



    # ====== НАСТРОЙКИ: Фильтр поиска ======
    if data.startswith("settings_search_filter"):
        trigger_id = None
        if data.startswith("settings_search_filter_"):
            try:
                trigger_id = int(data.split("_")[-1])
            except Exception:
                trigger_id = None

        if not trigger_id:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        context.user_data["settings_trigger_msg_id"] = trigger_id
        context.user_data["settings_filter_msg_id"] = query.message.message_id

        await _answer_once()
        await show_search_filter_settings(update, context, edit=True)
        return

    if data.startswith("sf_toggle_"):
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        with Session() as session:
            bu = session.query(BotUser).filter(BotUser.telegram_id == query.from_user.id).one_or_none()
            if bu is None:
                bu = BotUser(
                    telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                    last_name=query.from_user.last_name,
                    is_bot=bool(getattr(query.from_user, "is_bot", False)),
                    chat_id=query.message.chat_id,
                    created_at=datetime.utcnow(),
                    last_seen_at=datetime.utcnow(),
                    search_filter_enabled=False,
                    search_filter_departure=None,
                    search_filter_destination=None,
                )
                session.add(bu)

            bu.search_filter_enabled = not bool(getattr(bu, "search_filter_enabled", False))
            session.commit()

        context.user_data["settings_trigger_msg_id"] = trigger_id
        context.user_data["settings_filter_msg_id"] = query.message.message_id

        await _answer_once()
        await show_search_filter_settings(update, context, edit=True)
        return

    if data.startswith("sf_set_dep_") or data.startswith("sf_set_dest_"):
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        is_dep = data.startswith("sf_set_dep_")
        context.user_data["settings_trigger_msg_id"] = trigger_id
        context.user_data["settings_filter_msg_id"] = query.message.message_id
        context.user_data["settings_filter_wait"] = "departure" if is_dep else "destination"

        prompt = "Введите пункт отправления (*откуда*):" if is_dep else "Введите пункт назначения (*куда*):"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Доступные направления", callback_data=f"sf_show_allowed_{trigger_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"settings_search_filter_{trigger_id}")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")],
        ])

        await _answer_once()
        await query.edit_message_text(
            "✏️ *Настройка фильтра*\n\n" + prompt + "\n\n"
            "💡 Введите пункт *точно как в списке*.",
            reply_markup=kb
        )
        return

    
    if data.startswith("sf_pick_"):
        # формат: sf_pick_<departure|destination>_<idx>_<trigger>
        parts = data.split("_")
        # ["sf", "pick", field, idx, trigger]
        try:
            field = parts[2]
            idx = int(parts[3])
            trigger_id = int(parts[4])
        except Exception:
            await _answer_once("⚠️ Не удалось выбрать направление.", show_alert=True)
            return

        suggestions = (context.user_data.get("sf_suggestions") or {}).get(field) or []
        if idx < 0 or idx >= len(suggestions):
            await _answer_once("⚠️ Список направлений устарел. Попробуйте снова.", show_alert=True)
            return

        value = suggestions[idx]
        chat_id = query.message.chat_id
        settings_msg_id = query.message.message_id

        # сохраняем в БД
        with Session() as session:
            bu = session.query(BotUser).filter(BotUser.telegram_id == query.from_user.id).one_or_none()
            if bu is None:
                bu = BotUser(
                    telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                    last_name=query.from_user.last_name,
                    is_bot=bool(getattr(query.from_user, "is_bot", False)),
                    chat_id=chat_id,
                    created_at=datetime.utcnow(),
                    last_seen_at=datetime.utcnow(),
                    search_filter_enabled=False,
                    search_filter_departure=None,
                    search_filter_destination=None,
                )
                session.add(bu)

            if field == "departure":
                bu.search_filter_departure = value
            else:
                bu.search_filter_destination = value

            session.commit()

        # очистим ожидание ввода
        context.user_data.pop("settings_filter_wait", None)
        context.user_data["settings_trigger_msg_id"] = trigger_id
        context.user_data["settings_filter_msg_id"] = settings_msg_id

        await _answer_once()
        await show_search_filter_settings(update, context, edit=True)
        return

    if data.startswith("sf_clear_"):
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        with Session() as session:
            bu = session.query(BotUser).filter(BotUser.telegram_id == query.from_user.id).one_or_none()
            if bu is not None:
                bu.search_filter_departure = None
                bu.search_filter_destination = None
                session.commit()

        context.user_data["settings_trigger_msg_id"] = trigger_id
        context.user_data["settings_filter_msg_id"] = query.message.message_id

        await _answer_once("🧹 Маршрут сброшен.")
        await show_search_filter_settings(update, context, edit=True)
        return

    if data.startswith("sf_show_allowed_"):
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data=f"sf_back_input_{trigger_id}")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")],
        ])

        await _answer_once()
        await query.edit_message_text(
            "📍 *Доступные направления:*\n\n" + allowed_locations_text(),
            reply_markup=kb
        )
        return

    if data.startswith("sf_back_input_"):
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        field = context.user_data.get("settings_filter_wait")
        is_dep = field != "destination"
        prompt = "Введите пункт отправления (*откуда*):" if is_dep else "Введите пункт назначения (*куда*):"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Доступные направления", callback_data=f"sf_show_allowed_{trigger_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"settings_search_filter_{trigger_id}")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=f"settings_close_{trigger_id}")],
        ])

        await _answer_once()
        await query.edit_message_text(
            "✏️ *Настройка фильтра*\n\n" + prompt + "\n\n"
            "💡 Введите пункт *точно как в списке*.",
            reply_markup=kb
        )
        return

    # ====== НАСТРОЙКИ: Назад в настройки (анти-спам) ======
    if data.startswith("settings_back"):
        trigger_id = None
        if data.startswith("settings_back_"):
            try:
                trigger_id = int(data.split("_")[-1])
            except Exception:
                trigger_id = None

        if not trigger_id:
            trigger_id = context.user_data.get("settings_trigger_msg_id") or 0

        context.user_data["settings_trigger_msg_id"] = trigger_id

        await show_settings_menu(update, context, edit=True)
        return

        # ====== НАСТРОЙКИ: Закрыть (анти-спам, удаляем сообщение бота + сообщение пользователя) ======
    if data.startswith("settings_close_"):
        chat_id = query.message.chat_id

        # 1) достаем message_id триггера из callback_data
        try:
            user_msg_id = int(data.split("_")[-1])
        except Exception:
            user_msg_id = None

        # 2) удаляем сообщение бота (экран настроек/профиля)
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        # 3) удаляем сообщение пользователя "⚙️ Настройки"
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        return

    # ====== Закрыть карточку "нет активных поездок" ======
    if data.startswith("close_no_active_trips_"):
        chat_id = query.message.chat_id
        try:
            user_msg_id = int(data.split("_")[-1])
        except Exception:
            user_msg_id = 0

        # Удаляем сообщение бота (карточку)
        try:
            await _answer_once()
            await query.message.delete()
        except Exception as e:
            logging.debug(f"Не удалось удалить сообщение бота: {e}")

        # Удаляем сообщение пользователя "Мои поездки" (если есть)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception as e:
                logging.debug(f"Не удалось удалить сообщение пользователя: {e}")

        return

        # ====== Закрыть карточку "нет бронирований" (удаляем карточку + триггер) ======
    if data.startswith("close_no_bookings_"):
        chat_id = query.message.chat_id

        try:
            user_msg_id = int(data.split("_")[-1])
        except Exception:
            user_msg_id = 0

        # удалить карточку (сообщение бота)
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        # удалить сообщение пользователя "🎫 Мои бронирования"
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        return

    # ====== Закрыть карточку "Мои бронирования" (чистый чат) ======
    if data.startswith("close_my_booking_card_"):
        chat_id = query.message.chat_id
        try:
            trigger_id = int(data.split("_")[-1])
        except Exception:
            trigger_id = 0

        # удаляем карточку
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        # удаляем сообщение пользователя "🎫 Мои бронирования" (если есть)
        if trigger_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=trigger_id)
            except Exception:
                pass
        return

    # ====== Назад в "Мои поездки" ======
    # ====== Назад в "Мои поездки" ======
    if data == "back_to_my_trips":
        chat_id = query.message.chat_id
        user_id = query.from_user.id

        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        await send_my_trips_cards(chat_id, user_id, context)
        return



        # ====== Закрыть сообщение "Поездка успешно создана" ======
    if data == "close_trip_created":
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            try:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id
                )
            except Exception:
                pass
        return

    # ====== Закрыть подтверждение бронирования (чистый чат + подчистить поиск) ======
    if data == "close_booking_request":
        chat_id = query.message.chat_id

        # Собираем, что можно удалить (и не падаем, если чего-то нет)
        msg_ids = set()

        # 1) результаты поиска (списком)
        for mid in (context.user_data.get("search_bot_msg_ids") or []):
            if isinstance(mid, int):
                msg_ids.add(mid)

        # 2) одиночные сообщения поиска (если где-то используются)
        for key in ("search_bot_msg_id", "search_custom_prompt_bot_msg_id", "date_error_bot_msg_id"):
            mid = context.user_data.get(key)
            if isinstance(mid, int):
                msg_ids.add(mid)

        # 3) триггерные сообщения пользователя/меню
        for key in ("search_user_msg_id", "search_trigger_msg_id", "date_error_user_msg_id", "last_user_msg_id"):
            mid = context.user_data.get(key)
            if isinstance(mid, int):
                msg_ids.add(mid)

        # 4) текущее сообщение (сама карточка "запрос отправлен")
        msg_ids.add(query.message.message_id)

        # Удаляем всё, что нашли
        for mid in msg_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

        # Чистим состояние поиска/ошибок/служебное
        for k in list(context.user_data.keys()):
            if k.startswith("search_") or k.startswith("date_error_"):
                context.user_data.pop(k, None)
        context.user_data.pop("booking_confirm_bot_msg_id", None)

        return
    
    # ====== Закрыть "Бронирование сохранено" (удаляем карточку + триггер "🎫 Мои бронирования") ======
    if data.startswith("close_booking_saved_"):
        chat_id = query.message.chat_id

        # 1) достаем id триггера из callback_data
        try:
            user_msg_id = int(data.split("_")[-1])
        except Exception:
            user_msg_id = None

        # 2) удаляем карточку
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except Exception:
                pass
    
    elif data == "close_booking_request":
        await _answer_once()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif data == "close_edit_menu":
        await _answer_once()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif data.startswith("search_back_"):
        await query.answer()

        # тот же trigger_id, чтобы "Отмена" могла удалять триггерное сообщение пользователя
        trigger_id = data.replace("search_back_", "", 1) or "0"

        reply_markup = keyboards.get_date_selection_keyboard(
            cancel_cb=f"date_cancel_{trigger_id}"
        )

        await query.edit_message_text(
            "📅 Выберите дату для поиска поездок:",
            reply_markup=reply_markup
        )
        return

    elif data == "close_driver_cancel_notice":
        await _answer_once()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif data.startswith("close_booking_cancelled_"):
        chat_id = query.message.chat_id

        # id сообщения пользователя-триггера ("🎫 Мои бронирования")
        try:
            user_msg_id = int(data.split("_")[-1])
        except Exception:
            user_msg_id = None

        # удалить карточку
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except Exception:
                pass

        # удалить триггер
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        return

        # 3) удаляем сообщение пользователя "🎫 Мои бронирования"
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        return

        # 1) удаляем последнее сообщение пользователя (обычно введённая дата)
        user_msg_id = context.user_data.pop("search_user_msg_id", None) or context.user_data.pop("last_user_msg_id", None)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        # 2) удаляем сообщение со списком результатов поиска (если сохраняли)
        search_bot_msg_id = context.user_data.pop("search_bot_msg_id", None)
        if search_bot_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=search_bot_msg_id)
            except Exception:
                pass

        # 3) удаляем текущее сообщение (подтверждение)
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except Exception:
                pass

        # 4) подчистим служебное (не обязательно)
        context.user_data.pop("booking_confirm_bot_msg_id", None)

        return

        # ====== Закрыть "Детали поездки" (удаляем ответ бота + последнее сообщение пользователя) ======
    if data == "close_trip_details":
        chat_id = query.message.chat_id

        # 1) Пытаемся удалить последнее сообщение пользователя:
        # - для поиска по дате у тебя оно обычно сохранено как search_user_msg_id
        user_msg_id = context.user_data.pop("search_user_msg_id", None)

        # (на всякий случай) fallback — если ты где-то сохраняешь last_user_msg_id
        if user_msg_id is None:
            user_msg_id = context.user_data.pop("last_user_msg_id", None)

        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        # 2) Удаляем текущее сообщение бота с деталями
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except Exception:
                pass

        return

            # ====== Заглушка для noop ======
    if data == "noop":
        await _answer_once()
        return

    # ====== Закрыть результаты поиска (удаляем ввод даты/меню + сообщение бота) ======
# ====== Закрыть результаты поиска (удаляем "запрос" + сообщение бота) ======
    if data == "close_search_results" or data.startswith("close_search_results_"):
        chat_id = query.message.chat_id

        # 1) message_id сообщения пользователя берём ИЗ callback_data (самый надёжный вариант)
        user_msg_id = None
        if data.startswith("close_search_results_"):
            try:
                user_msg_id = int(data.split("_")[-1])
            except Exception:
                user_msg_id = None

        # 2) fallback для старых сообщений (если кнопка была без суффикса)
        if not user_msg_id:
            user_msg_id = context.user_data.pop("search_user_msg_id", None) or context.user_data.pop("last_user_msg_id", None)

        # (не обязательно, но пусть будет запасной вариант)
        bot_msg_id = context.user_data.pop("search_bot_msg_id", None)

        # удаляем сообщение пользователя (если можем)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        # удаляем сообщение бота (то, где нажали кнопку)
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            if bot_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
                except Exception:
                    pass

        return

        # ====== Закрыть сообщение об ошибке ввода даты ======
    if data == "close_date_error":
        user_msg_id = context.user_data.pop("date_error_user_msg_id", None)
        bot_msg_id = context.user_data.pop("date_error_bot_msg_id", None)

        chat_id = query.message.chat_id

        # удаляем сообщение пользователя (то, где он ввёл неправильную дату)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        # удаляем сообщение бота с кнопкой
        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            if bot_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
                except Exception:
                    pass

        return
    
        # ====== НАСТРОЙКИ / ПРОФИЛЬ ======
    if data == "settings_profile":
        await show_my_profile(update, context, edit=True)
        return

    if data == "settings_back":
        await show_settings_menu(update, context, edit=True)
        return

    if data == "settings_back_main":
        chat_id = query.message.chat_id
        user_msg_id = context.user_data.get("settings_trigger_msg_id")

        try:
            await _answer_once()
            await query.message.delete()
        except Exception:
            pass

        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        return

    try:
        if data.startswith("book_"):
            # Бронирование места пассажиром
            trip_id = int(data.split("_")[1])
            
            with Session() as session:
                try:
                    trip = session.query(Trip).get(trip_id)

                    if not trip or trip.seats_available <= 0 or not trip.is_active:
                        await _answer_once()
                        await query.edit_message_text("❌ Извините, места уже заняты или поездка неактивна.")
                        return

                  # 🚫 Нельзя бронировать свою же поездку
                    if trip.driver_id == query.from_user.id:
                        await _answer_once(
                            "⚠️ Нельзя забронировать свою поездку.",
                            show_alert=True
                        )
                        return

                    # Проверяем, не просрочена ли поездка
                    if trip_end_dt(trip) < datetime.now():
                        await deny("❌ Нельзя забронировать место на уже прошедшую поездку.")
                        # чтобы не оставлять “битую” карточку с кнопкой брони — удаляем её
                        try:
                            await query.message.delete()
                        except Exception:
                            pass
                        return

                    # Проверяем, не забронировал ли уже пользователь место в этой поездке
                    existing_booking = session.query(Booking).filter(
                        Booking.trip_id == trip_id,
                        Booking.passenger_id == query.from_user.id,
                        Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
                    ).first()
                    
                    if existing_booking:
                        await _answer_once("⚠️ Вы уже забронировали место в этой поездке!", show_alert=True)
                        return

                    # ✅ Шаг выбора количества мест
                    max_btn = min(5, int(trip.seats_available))  # не больше 5 кнопок в ряд/экране
                    rows = []

                    # кнопки 1..max_btn (по одной в ряд или по 3 в ряд — на твой вкус)
                    row = []
                    for n in range(1, max_btn + 1):
                        row.append(InlineKeyboardButton(f"{n}", callback_data=f"book_qty_{trip_id}_{n}"))
                        if len(row) == 5:  # можно 5 в ряд, раз максимум 5
                            rows.append(row)
                            row = []
                    if row:
                        rows.append(row)

                    # навигация
                    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=f"s_back_{trip_id}")])
                    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data="close_booking_request")])

                    reply_markup = InlineKeyboardMarkup(rows)

                    await _answer_once()
                    try:
                        await query.edit_message_text(
                            text=(
                                "💺 *Сколько мест забронировать?*\n\n"
                                f"Свободных мест: *{trip.seats_available}*"
                            ),
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        if "Message is not modified" not in str(e):
                            raise
                    return

                except Exception as e:
                    logging.error(f"Ошибка при бронировании: {e}")
                    await _answer_once()
                    await query.edit_message_text("❌ Произошла ошибка при бронировании.")
        
        elif data == "close_deleted_trip" or data.startswith("close_deleted_trip_"):
            chat_id = query.message.chat_id

            # 1) удаляем сообщение бота
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass

            # 2) если есть trigger_id — удаляем и сообщение пользователя
            user_msg_id = 0
            if data.startswith("close_deleted_trip_"):
                try:
                    user_msg_id = int(data.split("_")[-1])
                except Exception:
                    user_msg_id = 0

            if user_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
                except Exception:
                    pass

            return

        elif data == "show_allowed_departure":
            chat_id = query.message.chat_id
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_departure_input")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _answer_once()

            await query.edit_message_text(
                "📍 *Доступные направления:*\n\n"
                f"{allowed_locations_text()}",
                reply_markup=reply_markup
            )

        elif data == "back_to_departure_input":
            chat_id = query.message.chat_id
            context.user_data["creating_field"] = "departure"
            keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _answer_once()

            await query.edit_message_text(
                "🚗 *Создание новой поездки*\n\n"
                "Введите пункт отправления:\n\n"
                "💡 *Подсказка:* Можно ввести город, район или конкретный адрес.\n\n"
                "Если не уверены — нажмите «Доступные направления» после неверного ввода.",
                reply_markup=reply_markup
            )

        elif data == "show_allowed_destination":
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_destination_input")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _answer_once()

            await query.edit_message_text(
                "📍 *Доступные направления:*\n\n"
                f"{allowed_locations_text()}",
                reply_markup=reply_markup
            )

        elif data == "back_to_destination_input":
            context.user_data["creating_field"] = "destination"
            keyboard = [[InlineKeyboardButton("❌ Отмена создания", callback_data="cancel_trip_creation")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _answer_once()

            await query.edit_message_text(
                "🚗 *Создание новой поездки*\n\n"
                "Введите пункт назначения:\n\n"
                "💡 *Подсказка:* Можно ввести город, район или конкретный адрес.\n\n"
                "Если не уверены — нажмите «Доступные направления» после неверного ввода.",
                reply_markup=reply_markup
            )
        
        elif data.startswith("close_trip_canceled_"):
            chat_id = query.message.chat_id

            # 1) удаляем сообщение бота (карточку)
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass

            # 2) удаляем сообщение пользователя (триггер "📋 Мои поездки")
            try:
                user_msg_id = int(data.split("_")[-1])
            except Exception:
                user_msg_id = 0

            if user_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
                except Exception:
                    pass

            return


        elif data.startswith("close_my_trip_card_"):
            chat_id = query.message.chat_id

            # 1) удаляем сообщение бота (карточку поездки)
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass

            # 2) удаляем сообщение пользователя (триггер "📋 Мои поездки")
            try:
                user_msg_id = int(data.split("_")[-1])
            except Exception:
                user_msg_id = 0

            if user_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
                except Exception:
                    pass

            return


        elif data.startswith("close_new_booking_"):
            # Закрыть карточку "Новое бронирование" (после подтверждения)
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass
            return

        elif data.startswith("close_driver_cancel_notice_"):
            # Шаг 6: закрыть уведомление водителю об отмене бронирования пассажиром
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass
            return

                # ====== ВОДИТЕЛЬ: исход поездки (состоялась) -> оценить пассажира ======
        if data.startswith("trip_done_"):
            booking_id = int(data.split("_")[-1])
            driver_id = query.from_user.id

            with Session() as session:
                booking = session.query(Booking).get(booking_id)
                if not booking:
                    await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                    return

                trip = booking.trip
                if not trip or trip.driver_id != driver_id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return

                # (опционально) фиксируем общий исход в passenger_trip_result,
                # чтобы не плодить новые поля. Если уже записано — не трогаем.
                if not getattr(booking, "passenger_trip_result", None):
                    booking.passenger_trip_result = "completed"
                    session.commit()

            text = (
                "⭐ *Оцените пассажира*\n\n"
                "Поставьте оценку от 1 до 5 звёзд.\n"
                "Это поможет улучшить сервис."
            )
            await _answer_once()
            await query.edit_message_text(
                text=text,
                reply_markup=keyboards.get_passenger_rating_keyboard(booking_id)
            )
            return


        # ====== ВОДИТЕЛЬ: исход поездки (не состоялась) -> оценить пассажира ======
        if data.startswith("trip_failed_"):
            booking_id = int(data.split("_")[-1])
            driver_id = query.from_user.id

            with Session() as session:
                booking = session.query(Booking).get(booking_id)
                if not booking:
                    await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                    return

                trip = booking.trip
                if not trip or trip.driver_id != driver_id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return

                # если уже оценивал водителя — не даём повторно
                if getattr(booking, "passenger_rating_driver", None) is not None:
                    await _answer_once()
                    await query.edit_message_text(
                        text="✅ Вы уже оценили водителя по этой поездке.",
                        reply_markup=keyboards.get_close_only_keyboard(
                            f"close_passenger_rate_driver_{booking_id}"
                        )
                    )
                    return
                    
                # не перезаписываем результат поездки повторно
                if getattr(booking, "passenger_trip_result", None):
                    # просто показываем оценку (если ещё не оценивал — он попадёт на экран со звёздами)
                    pass
                else:
                    booking.passenger_trip_result = "completed"  # или "not_completed" в другом обработчике
                    session.commit()

                if not getattr(booking, "passenger_trip_result", None):
                    booking.passenger_trip_result = "not_completed"
                    session.commit()

            text = (
                "⭐ *Оцените пассажира*\n\n"
                "Даже если поездка не состоялась — оценка важна.\n"
                "Поставьте от 1 до 5 звёзд."
            )
            await _answer_once()
            await query.edit_message_text(
                text=text,
                reply_markup=keyboards.get_passenger_rating_keyboard(booking_id)
            )
            return

        elif data.startswith("close_rate_passenger_"):
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass
            return

        elif data.startswith("rate_passenger_"):
            # формат: rate_passenger_{booking_id}_{stars}
            parts = data.split("_")
            booking_id = int(parts[-2])
            stars = int(parts[-1])
            driver_id = query.from_user.id

            if stars < 1 or stars > 5:
                await _answer_once("❌ Некорректная оценка.", show_alert=True)
                return

            with Session() as session:
                booking = session.query(Booking).get(booking_id)
                if not booking:
                    await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                    return

                trip = booking.trip
                if not trip or trip.driver_id != driver_id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return

                # сохраняем оценку водителя пассажиру
                booking.driver_rating_passenger = stars
                booking.driver_rated_at = datetime.utcnow()
                session.commit()

            await _answer_once()

            await query.edit_message_text(
                text="✅ Спасибо! Оценка пассажира сохранена.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_rate_passenger_{booking_id}")]
                ])
            )
            return

        elif data == "clear_understood":
            # Обработка кнопки "Понятно" из инструкции по очистке
            await handle_clear_understood(update, context)
    
        elif data == "show_all_my_trips_blocked":
            # Показать все поездки (включая завершенные)
            await show_all_my_trips_from_blocked(query, context)

        elif data == "cancel_trip_creation":
            await cancel_creation(update, context)
            return ConversationHandler.END

        elif data.startswith("date_"):
            # Обработка выбора даты
            await handle_date_selection(query, context)
            
        elif data.startswith("confirm_booking_"):
            # Водитель подтверждает бронирование
            booking_id = int(data.split("_")[2])

            with Session() as session:
                booking = session.query(Booking).get(booking_id)

                if not booking:
                    await _answer_once("❌ Заявка не найдена.", show_alert=True)
                    return

                if booking.status == BookingStatus.EXPIRED.value:
                    await _answer_once("⌛ Заявка уже истекла.", show_alert=True)
                    await edit_tracked_message(
                        update,
                        context,
                        text="⌛ Заявка истекла. Подтвердить её нельзя.",
                        reply_markup=keyboards.get_close_only_keyboard("close_driver_booking_notice")
                    )
                    return

                # Запрещаем подтверждение, если заявка уже истекла по TTL
                if booking.status == BookingStatus.PENDING.value:
                    try:
                        from datetime import timedelta
                        from config import PENDING_BOOKING_TTL_MINUTES
                        ttl_minutes = int(PENDING_BOOKING_TTL_MINUTES or 15)
                    except Exception:
                        ttl_minutes = 15

                    is_expired = False
                    try:
                        bt = getattr(booking, "booking_time", None)
                        # booking_time may be str in some SQLite setups
                        if isinstance(bt, str):
                            s = bt.strip()
                            try:
                                bt = datetime.fromisoformat(s)
                            except Exception:
                                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                                    try:
                                        bt = datetime.strptime(s, fmt)
                                        break
                                    except Exception:
                                        bt = None
                        if bt is not None:
                            is_expired = bt < (datetime.utcnow() - timedelta(minutes=ttl_minutes))
                    except Exception:
                        is_expired = False

                    if is_expired:
                        # Истекаем прямо сейчас, чтобы нельзя было подтвердить даже до работы job
                        trip = booking.trip
                        if trip is not None:
                            try:
                                trip.seats_available = int(trip.seats_available or 0) + int(booking.seats_booked or 0)
                            except Exception:
                                pass
                        booking.status = BookingStatus.EXPIRED.value
                        session.commit()

                        # Уведомляем пассажира
                        try:
                            await booking_module.notify_passenger_booking_expired(context.bot, booking, ttl_minutes=ttl_minutes)
                        except Exception:
                            pass

                        await _answer_once("⌛ Заявка истекла. Подтвердить нельзя.", show_alert=True)
                        await edit_tracked_message(
                            update,
                            context,
                            text=(
                                "⌛ Заявка истекла.\n"
                                "Подтвердить её уже нельзя."
                            ),
                            reply_markup=keyboards.get_close_only_keyboard("close_driver_booking_notice")
                        )
                        return

                    booking.status = BookingStatus.CONFIRMED.value
                    session.commit()

                    # Уведомляем пассажира с кнопкой связи
                    await booking_module.notify_passenger_booking_confirmed(context.bot, booking, query.from_user)

                    # сообщение водителю: только данные пассажира + Закрыть
                    passenger_username = None
                    try:
                        pchat = await context.bot.get_chat(booking.passenger_id)
                        if pchat and getattr(pchat, "username", None):
                            passenger_username = pchat.username
                    except Exception:
                        passenger_username = None

                    contact = f"@{passenger_username}" if passenger_username else "скрыт"

                    await edit_tracked_message(
                        update,
                        context,
                        text=(
                            "✅ *БРОНИРОВАНИЕ ПОДТВЕРЖДЕНО!*\n\n"
                            f"👤 *Пассажир:* {booking.passenger_name}\n"
                            f"📞 *Контакт:* {contact}\n"
                        ),
                        reply_markup=keyboards.get_close_only_keyboard("close_driver_booking_notice")
                    )
                    
        elif data == "close_driver_booking_notice":
            await _answer_once()
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        elif data == "close_booking_expired_notice":
            await _answer_once()
            try:
                await query.message.delete()
            except Exception:
                pass
            return


        elif data.startswith("reject_booking_"):
            # Водитель отклоняет бронирование
            booking_id = int(data.split("_")[2])

            with Session() as session:
                booking = session.query(Booking).get(booking_id)

                if booking and booking.status == BookingStatus.PENDING.value:
                    # Возвращаем место
                    trip = booking.trip
                    trip.seats_available += booking.seats_booked
                    booking.status = BookingStatus.REJECTED.value
                    session.commit()

                    # Уведомляем пассажира
                    await booking_module.notify_passenger_booking_rejected(context.bot, booking)

                    # Обновляем сообщение у водителя
                    await _answer_once()
                    await query.edit_message_text(
                        text=query.message.text + "\n\n❌ Бронирование отклонено.",
                        reply_markup=None
                    )


        elif data.startswith("cancel_booking_"):
            # Пассажир хочет отменить бронирование
            booking_id = int(data.split("_")[2])
            
            with Session() as session:
                try:
                    booking = session.query(Booking).get(booking_id)
                    
                    if not booking:
                        await deny("❌ Бронирование не найдено.")
                        return

                    if booking.passenger_id != query.from_user.id:
                        await deny("⚠️ Вы не можете отменить это бронирование.")
                        return

                    trip = booking.trip
                    if not trip:
                        await deny("⚠️ Поездка не найдена.")
                        return

                    if trip_end_dt(trip) < datetime.now():
                        await deny("⚠️ Нельзя отменить бронирование на уже прошедшую поездку.")
                        return
                    
                    context.user_data['cancelling_booking_id'] = booking_id
                    context.user_data['cancelling_booking'] = booking
                    
                    keyboard = [
                        [InlineKeyboardButton("✅ Да, отменить", callback_data=f"confirm_cancel_{booking_id}")],
                        [InlineKeyboardButton("❌ Нет, оставить", callback_data=f"keep_booking_{booking_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await _answer_once()
                    
                    await query.edit_message_text(
                        text="❓ Вы уверены, что хотите отменить бронирование?",
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logging.error(f"Ошибка при проверке бронирования: {e}")
                    await _answer_once("❌ Произошла ошибка.")

        elif data.startswith("confirm_cancel_"):
            # Пассажир подтверждает отмену бронирования
            booking_id = int(data.split("_")[2])

            with Session() as session:
                booking = session.query(Booking).get(booking_id)

                if booking.status in (BookingStatus.CANCELLED.value, BookingStatus.REJECTED.value, BookingStatus.EXPIRED.value):
                    await deny("✅ Уже отменено.")
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    return

                if booking and booking.passenger_id == query.from_user.id:
                    # Возвращаем место
                    trip = booking.trip
                    trip.seats_available += booking.seats_booked
                    booking.status = BookingStatus.CANCELLED.value
                    session.commit()

                    # Уведомляем водителя об отмене
                    await booking_module.notify_driver_booking_cancelled(context.bot, booking)

                    # Берём message_id триггера "🎫 Мои бронирования"
                    trigger_id = context.user_data.get("last_user_msg_id") or 0

                    keyboard = [
                        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_booking_cancelled_{trigger_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await _answer_once()
                    await edit_tracked_message(
                        update,
                        context,
                        text="✅ Бронирование отменено. Место возвращено в общий доступ.",
                        reply_markup=reply_markup
                    )
                    return
                    
                    # анти-повтор: если бронь уже отменена/отклонена — ничего не делаем, убираем карточку
                    if booking.status in (BookingStatus.CANCELLED.value, BookingStatus.REJECTED.value, BookingStatus.EXPIRED.value):
                        await deny("✅ Это бронирование уже отменено.")
                        try:
                            await query.message.delete()
                        except Exception:
                            pass
                        return

        elif data.startswith("keep_booking_"):
            # Пассажир передумал отменять -> возвращаемся в предыдущую карточку бронирования
            context.user_data.pop("cancelling_booking_id", None)
            context.user_data.pop("cancelling_booking", None)

            booking_id = int(data.split("_")[2])

            trigger_id = context.user_data.get("last_user_msg_id")
            trigger_id = trigger_id if isinstance(trigger_id, int) else 0

            with Session() as session:
                booking = session.query(Booking).get(booking_id)
                if not booking:
                    await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                    return

                if booking.passenger_id != query.from_user.id:
                    await _answer_once("⚠️ Вы не можете управлять этим бронированием.", show_alert=True)
                    return

                trip = booking.trip
                now = datetime.now()
                if (not trip) or (not getattr(trip, "is_active", False)) or (trip.date and trip.date < now):
                    await _answer_once()
                    await query.edit_message_text(
                        text="⚠️ Эта поездка больше не активна.",
                        reply_markup=keyboards.get_close_only_keyboard(f"close_my_booking_card_{trigger_id}")
                    )
                    return

                status_map = {
                    BookingStatus.PENDING.value: "⏳ Ожидает подтверждения",
                    BookingStatus.CONFIRMED.value: "✅ Подтверждено",
                    BookingStatus.EXPIRED.value: "⌛ Истекло",
                }
                status = status_map.get(booking.status, booking.status)

                driver_username = None
                try:
                    driver_chat = await context.bot.get_chat(trip.driver_id)
                    if driver_chat and getattr(driver_chat, "username", None):
                        driver_username = driver_chat.username
                except Exception:
                    driver_username = None

                driver_line = f"@{driver_username}" if driver_username else "—"

                message = (
                    f"🚗 *Поездка:* {trip.departure_point} -> {trip.destination_point}\n"
                    f"⏰ *Время:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n\n"
                    f"👤 *Водитель:* {trip.driver_name}\n"
                    f"🔗 *Username:* {driver_line}\n\n"
                    f"💺 *Мест:* {booking.seats_booked}\n"
                    f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
                    f"📊 *Статус:* {status}\n"
                )

                keyboard_rows = [
                    [InlineKeyboardButton("❌ Отменить бронирование", callback_data=f"cancel_booking_{booking.id}")]
                ]
                if booking.status == BookingStatus.CONFIRMED.value:
                    keyboard_rows.append(
                        [InlineKeyboardButton("⭐ Оценить поездку", callback_data=f"passenger_open_trip_rating_{booking.id}")]
                    )
                keyboard_rows.append(
                    [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_my_booking_card_{trigger_id}")]
                )

                await _answer_once()
                await query.edit_message_text(
                    text=message,
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
                return

        # ========== НОВЫЕ ОБРАБОТЧИКИ ==========
        elif data.startswith("trip_bookings_"):
            parts = data.split("_")
            trip_id = int(parts[2])
            trigger_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

            await show_trip_bookings(query, context, trip_id, trigger_id)
            return
            
        elif data.startswith("edit_trip_") and len(data.split("_")) >= 3 and data.split("_")[2].isdigit():
            parts = data.split("_")
            trip_id = int(parts[2])
            trigger_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            context.user_data["edit_trigger_id"] = trigger_id

            now = datetime.now()
            driver_id = query.from_user.id

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip:
                    await _answer_once("❌ Поездка не найдена.", show_alert=True)
                    return

                # защита: нельзя редактировать чужую поездку
                if trip.driver_id != driver_id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return

                # защита: нельзя редактировать прошедшую поездку (и нельзя “воскресить”)
                if trip.date < now:
                    await _answer_once("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    return

                # защита: нельзя редактировать, если есть подтверждённые бронирования
                if _trip_has_confirmed_bookings(session, trip_id):
                    await _answer_once(
                        "⚠️ У этой поездки есть *подтверждённые бронирования*.\n"
                        "Редактирование недоступно. Сначала отмените подтверждённые брони.",
                        show_alert=True
                    )
                    return

            await show_edit_menu(query, context, trip_id, trigger_id)
            return
            
        

        elif data.startswith("edit_trip_date_"):
            # edit_trip_date_today_<trip_id> / edit_trip_date_tomorrow_<trip_id> / edit_trip_date_manual_<trip_id>
            parts = data.split("_")
            if len(parts) < 5:
                await _answer_once()
                return
            kind = parts[3]
            trip_id = int(parts[4])

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return
                if trip.driver_id != query.from_user.id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await _answer_once("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await _answer_once("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

            today = datetime.now().date()
            if kind == "today":
                chosen = today
            elif kind == "tomorrow":
                chosen = today + timedelta(days=1)
            else:
                # manual
                context.user_data['editing_trip_id'] = trip_id
                context.user_data['editing_field'] = 'edit_date_manual'
                context.user_data["edit_menu_msg_id"] = query.message.message_id
                await query.edit_message_text(
                    "📝 *Введите дату поездки*\n"
                    "Формат: *ДД.ММ.ГГГГ*\n"
                    "Пример: *25.01.2026*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")]])
                )
                return

            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'edit_time_select'
            context.user_data['edit_date_only'] = chosen
            context.user_data["edit_menu_msg_id"] = query.message.message_id

            await query.edit_message_text(
                f"📅 *Дата:* {chosen.strftime('%d.%m.%Y')}\n\n"
                "⏰ *Выберите время поездки:*",
                reply_markup=_edit_trip_time_choice_kb(trip_id)
            )
            return

        elif data.startswith("edit_trip_time_slot_"):
            # edit_trip_time_slot_<morning|day|evening>_<trip_id>  (пример: edit_trip_time_slot_morning_123)
            parts = data.split("_")
            if len(parts) < 6:
                await _answer_once()
                return
            slot = parts[4]
            trip_id = int(parts[5])
            date_only = context.user_data.get("edit_date_only")
            if not date_only:
                await _answer_once("📅 Сначала выберите дату поездки.", show_alert=True)
                return
            if slot not in SLOT_RANGES:
                await _answer_once()
                return

            start_s, end_s, _label = SLOT_RANGES[slot]
            start_t = datetime.strptime(start_s, "%H:%M").time()
            end_t = datetime.strptime(end_s, "%H:%M").time()
            start_dt = datetime.combine(date_only, start_t)
            end_dt = datetime.combine(date_only, end_t)

            if end_dt < datetime.now():
                await query.edit_message_text(
                    "❌ *Нельзя поставить поездку в прошлом.*\n\n"
                    "Выберите другое время.",
                    reply_markup=_edit_trip_time_choice_kb(trip_id),
                )
                return

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return
                if trip.driver_id != query.from_user.id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await _answer_once("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

                trip.date = start_dt
                trip.end_date = end_dt
                trip.time_mode = "slot"
                session.commit()

            context.user_data.pop("edit_date_only", None)
            context.user_data.pop("editing_field", None)

            trigger_id = int(context.user_data.get("edit_trigger_id", 0) or 0)
            await show_edit_menu(query, context, trip_id, trigger_id)
            return

        elif data.startswith("edit_trip_time_exact_"):
            # Просим ввести точное время (ЧЧ:ММ)
            parts = data.split("_")
            if len(parts) < 5:
                await _answer_once()
                return
            trip_id = int(parts[4])
            date_only = context.user_data.get("edit_date_only")
            if not date_only:
                await _answer_once("📅 Сначала выберите дату поездки.", show_alert=True)
                return

            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'edit_time_manual'
            context.user_data["edit_menu_msg_id"] = query.message.message_id

            await query.edit_message_text(
                f"📅 *Дата:* {date_only.strftime('%d.%m.%Y')}\n\n"
                "⏰ *Введите точное время поездки*\n"
                "Формат: *ЧЧ:ММ*\n"
                "Пример: *14:30*",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")]])
            )
            return

        elif data.startswith("edit_seats_pick_"):
            # edit_seats_pick_<trip_id>_<n>
            parts = data.split("_")
            if len(parts) < 5:
                await _answer_once()
                return
            trip_id = int(parts[3])
            n = int(parts[4])
            if n < 1 or n > 5:
                await _answer_once()
                return

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return
                if trip.driver_id != query.from_user.id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await _answer_once("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await _answer_once("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

                trip.seats_available = n
                session.commit()

            trigger_id = int(context.user_data.get("edit_trigger_id", 0) or 0)
            await show_edit_menu(query, context, trip_id, trigger_id)
            return

        elif data.startswith("edit_departure_"):
            # Начать редактирование пункта отправления
            trip_id = int(data.split("_")[2])

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return

                # защита: нельзя редактировать чужую/прошедшую поездку и поездку с подтверждёнными бронями
                if trip.driver_id != query.from_user.id:
                    await query.answer("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await query.answer("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await query.answer("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return


            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'departure'
            context.user_data["edit_menu_msg_id"] = query.message.message_id

            nav_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")]
            ])

            await _answer_once()
            await query.edit_message_text("✏️ Введите новый пункт отправления:", reply_markup=nav_kb)

        elif data.startswith("edit_destination_"):
            # Начать редактирование пункта назначения
            trip_id = int(data.split("_")[2])
            
            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return

                # защита: нельзя редактировать чужую/прошедшую поездку и поездку с подтверждёнными бронями
                if trip.driver_id != query.from_user.id:
                    await query.answer("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await query.answer("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await query.answer("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

                    
            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'destination'
            context.user_data["edit_menu_msg_id"] = query.message.message_id
            await _answer_once()
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")]])
            await query.edit_message_text("✏️ Введите новый пункт назначения:", reply_markup=nav_kb)
            

        elif data.startswith("edit_pick_dep_") or data.startswith("edit_pick_dst_"):
            parts = data.split("_")
            # edit_pick_dep_<trip_id>_<idx> | edit_pick_dst_<trip_id>_<idx>
            if len(parts) < 5:
                await _answer_once()
                return
            kind = parts[2]  # dep|dst
            trip_id = int(parts[3])
            idx = int(parts[4])
            field = "departure" if kind == "dep" else "destination"
            key = f"{trip_id}:{field}"
            suggs = (context.user_data.get("edit_suggestions") or {}).get(key) or []
            if idx < 0 or idx >= len(suggs):
                await _answer_once("⚠️ Подсказки устарели. Введите пункт ещё раз.", show_alert=True)
                return
            chosen = suggs[idx]

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return
                if trip.driver_id != query.from_user.id:
                    await _answer_once("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await _answer_once("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if field == "departure":
                    if locations.norm(chosen) == locations.norm(trip.destination_point):
                        await _answer_once("⚠️ Откуда и куда не могут совпадать.", show_alert=True)
                        return
                    trip.departure_point = chosen
                else:
                    if locations.norm(chosen) == locations.norm(trip.departure_point):
                        await _answer_once("⚠️ Откуда и куда не могут совпадать.", show_alert=True)
                        return
                    trip.destination_point = chosen
                session.commit()

            # очистим подсказки
            try:
                context.user_data.get("edit_suggestions", {}).pop(key, None)
            except Exception:
                pass

            await _answer_once("✅ Обновлено")
            trigger_id = context.user_data.get("edit_trigger_id", 0) or 0
            await show_edit_menu(query, context, trip_id, trigger_id)
            return
        elif data.startswith("edit_date_"):
            # Редактирование даты/времени — по той же механике, что и при создании (выбор кнопками).
            trip_id = int(data.split("_")[2])

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return

                # защита: нельзя редактировать чужую/прошедшую поездку и поездку с подтверждёнными бронями
                if trip.driver_id != query.from_user.id:
                    await query.answer("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await query.answer("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await query.answer("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'edit_date_select'
            context.user_data["edit_menu_msg_id"] = query.message.message_id

            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📅 Сегодня", callback_data=f"edit_trip_date_today_{trip_id}"),
                    InlineKeyboardButton("📅 Завтра", callback_data=f"edit_trip_date_tomorrow_{trip_id}"),
                ],
                [InlineKeyboardButton("📝 Другая дата", callback_data=f"edit_trip_date_manual_{trip_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")],
            ])

            await query.edit_message_text(
                "📅 *Выберите дату поездки:*",
                reply_markup=kb
            )
            return
        elif data.startswith("edit_seats_"):
            # Редактирование мест — как при создании: выбор кнопками 1–5.
            trip_id = int(data.split("_")[2])

            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return

                # защита: нельзя редактировать чужую/прошедшую поездку и поездку с подтверждёнными бронями
                if trip.driver_id != query.from_user.id:
                    await query.answer("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await query.answer("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await query.answer("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'edit_seats_pick'
            context.user_data["edit_menu_msg_id"] = query.message.message_id

            await query.edit_message_text(
                "💺 *Выберите количество свободных мест (1–5):*",
                reply_markup=_edit_seats_keyboard(trip_id)
            )
            return

            
        elif data.startswith("edit_price_"):
            # Начать редактирование цены
            trip_id = int(data.split("_")[2])
            
            with Session() as session:
                trip = session.query(Trip).get(trip_id)
                if not trip or not trip.is_active:
                    await show_trip_deleted_card(query)
                    return

                # защита: нельзя редактировать чужую/прошедшую поездку и поездку с подтверждёнными бронями
                if trip.driver_id != query.from_user.id:
                    await query.answer("⚠️ Это не ваша поездка.", show_alert=True)
                    return
                if trip.date < datetime.now():
                    await query.answer("⚠️ Поездка уже прошла. Редактирование недоступно.", show_alert=True)
                    return
                if _trip_has_confirmed_bookings(session, trip_id):
                    await query.answer("⚠️ Есть подтверждённые бронирования. Редактирование недоступно.", show_alert=True)
                    return

            
            context.user_data['editing_trip_id'] = trip_id
            context.user_data['editing_field'] = 'price'
            context.user_data["edit_menu_msg_id"] = query.message.message_id
            await _answer_once()
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_back_{trip_id}")]])
            await query.edit_message_text("✏️ Введите новую цену за место (или '0' для бесплатно):", reply_markup=nav_kb)
                        
        elif data.startswith("edit_back_"):
            trip_id = int(data.split("_")[2])
            await _answer_once()
            await show_edit_menu(query, context, trip_id, 0)
            return

        elif data.startswith("edit_exit_"):
            # Выход из меню редактирования (без "сохранить/отменить" — изменения
            # и так применяются по мере ввода, поэтому просто возвращаемся в "Мои поездки").
            trip_id = int(data.split("_")[2])
            chat_id = query.message.chat_id
            user_id = query.from_user.id

            context.user_data.pop("editing_trip_id", None)
            context.user_data.pop("editing_field", None)
            context.user_data.pop("edit_menu_msg_id", None)

            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                # если не удалось удалить (например, уже удалено) — не критично
                await _answer_once()

            await send_my_trips_cards(chat_id, user_id, context)
                
        elif data.startswith("cancel_trip_"):
            # Водитель хочет отменить поездку
            try:
                # Проверяем, это отмена создания или отмена существующей поездки
                if data == "cancel_trip_creation":
                    # Это кнопка отмены создания новой поездки
                    await cancel_creation(query, context)
                    return
                    
                # Это отмена существующей поездки
                parts = data.split("_")
                trip_id = int(parts[2])
                trigger_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

                # Сохраняем trigger_id на время процесса отмены
                context.user_data["cancel_trip_trigger_msg_id"] = trigger_id
                
                with Session() as session:
                    try:
                        trip = session.query(Trip).get(trip_id)
                        
                        if not trip or not trip.is_active:
                            await show_trip_deleted_card(query, trigger_id)
                            return
                            
                        # Проверяем права доступа
                        if trip.driver_id != query.from_user.id:
                            await _answer_once("⚠️ Вы не можете отменить эту поездку.", show_alert=True)
                            return
                        
                        # Проверяем, не просрочена ли поездка
                        if trip_end_dt(trip) < datetime.now():
                            await _answer_once("⚠️ Нельзя отменить уже прошедшую поездку.", show_alert=True)
                            return
                        
                        # Создаем клавиатуру для подтверждения
                        keyboard = [
                            [InlineKeyboardButton(
                                "✅ Да, отменить поездку",
                                callback_data=f"confirm_trip_cancel_{trip_id}_{trigger_id}"
                            )],
                            [InlineKeyboardButton(
                                "❌ Нет, оставить",
                                callback_data=f"keep_trip_{trip_id}_{trigger_id}"
                            )]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        # Отправляем сообщение с подтверждением
                        await _answer_once()
                        await query.edit_message_text(
                            text=(
                                f"❓ *Вы уверены, что хотите отменить поездку?*\n\n"
                                f"📍 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
                                f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n\n"
                                f"⚠️ *Внимание:* Все бронирования будут отменены, и пассажиры получат уведомления."
                            ),
                            reply_markup=reply_markup
                        )
                        
                    except Exception as e:
                        logging.error(f"Ошибка при проверке поездки: {e}")
                        await _answer_once("❌ Произошла ошибка.", show_alert=True)
                        
            except ValueError as e:
                # Если не удалось преобразовать в число, логируем ошибку
                logging.error(f"Ошибка парсинга callback_data {data}: {e}")
                await _answer_once("⚠️ Неверный формат команды.", show_alert=True)
                
        elif data.startswith("trip_done_") or data.startswith("trip_failed_"):
            # Открываем карточку оценки пассажира
            try:
                booking_id = int(data.split("_")[-1])
            except Exception:
                await _answer_once("❌ Некорректные данные.", show_alert=True)
                return

            with Session() as session:
                booking = session.query(Booking).get(booking_id)

            if not booking:
                await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                return

            passenger_name = booking.passenger_name or "пассажира"

            rate_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⭐ 1", callback_data=f"rate_star_{booking_id}_1"),
                    InlineKeyboardButton("⭐ 2", callback_data=f"rate_star_{booking_id}_2"),
                    InlineKeyboardButton("⭐ 3", callback_data=f"rate_star_{booking_id}_3"),
                    InlineKeyboardButton("⭐ 4", callback_data=f"rate_star_{booking_id}_4"),
                    InlineKeyboardButton("⭐ 5", callback_data=f"rate_star_{booking_id}_5"),
                ],
                [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_rate_card_{booking_id}")]
            ])

            await _answer_once()

            await query.edit_message_text(
                text=f"⭐ *Оценить пассажира*\n\nПассажир: *{passenger_name}*\n\nВыберите оценку:",
                reply_markup=rate_kb
            )
            return
        
        elif data.startswith("confirm_trip_cancel_"):
            # confirm_trip_cancel_<trip_id>_<trigger_id>
            parts = data.split("_")
            trip_id = int(parts[3])
            trigger_id = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

            # сохраняем trigger_id, чтобы cancel_trip сделал кнопку "Закрыть" с удалением user-сообщения
            context.user_data["cancel_trip_trigger_msg_id"] = trigger_id

            await cancel_trip(query, context, trip_id)
            return
        
        elif data.startswith("rate_star_"):
            # Заглушка сохранения оценки (пока без БД)
            parts = data.split("_")
            # format: rate_star_<booking_id>_<stars>
            try:
                booking_id = int(parts[2])
                stars = int(parts[3])
            except Exception:
                await _answer_once("❌ Некорректные данные.", show_alert=True)
                return

            # Можно просто подтвердить и оставить карточку с кнопкой закрыть
            close_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_rate_card_{booking_id}")]
            ])

            await _answer_once()

            await query.edit_message_text(
                text=f"✅ Спасибо! Оценка сохранена: *{stars}⭐*\n\n(пока без записи в базу — следующий шаг)",
                reply_markup=close_kb
            )
            return

        elif data.startswith("close_rate_card_"):
            # Закрыть карточку оценки
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass
            return

        
        elif data.startswith("keep_trip_"):
            # keep_trip_<trip_id>_<trigger_id>
            # Отказ от отмены: безопасно возвращаем в "Мои поездки" без проверки статуса поездки.
            await _answer_once()
            chat_id = query.message.chat_id if query.message else query.from_user.id
            try:
                await query.message.delete()
            except Exception:
                pass
            try:
                await send_my_trips_cards(chat_id, query.from_user.id, context)
            except Exception:
                pass
            return


        elif data.startswith("contact_passenger_"):
            # Водитель хочет связаться с пассажиром
            parts = data.split("_")
            passenger_id = int(parts[2])
            booking_id = int(parts[3])
            await contact_passenger(query, context, passenger_id, booking_id)
            
        elif data.startswith("copy_id_"):
            # Копирование ID пассажира
            passenger_id = data.split("_")[2]
            await _answer_once(f"ID пассажира: {passenger_id}\nID скопирован. Используйте его для поиска в Telegram.", show_alert=True)
            
        elif data.startswith("cancel_driver_booking_"):
            # Водитель хочет отменить бронирование пассажира
            booking_id = int(data.split("_")[3])
            
            with Session() as session:
                try:
                    booking = session.query(Booking).get(booking_id)
                    
                    if not booking:
                        await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                        return
                    
                    if booking.status in (BookingStatus.CANCELLED.value, BookingStatus.REJECTED.value, BookingStatus.EXPIRED.value):
                        await _answer_once("✅ Это бронирование уже отменено.", show_alert=True)
                        try:
                            await query.message.delete()
                        except Exception:
                            pass
                        return

                    # Проверяем, что пользователь - водитель этой поездки
                    trip = booking.trip
                    if trip.driver_id != query.from_user.id:
                        await _answer_once("⚠️ Вы не можете отменять бронирования этой поездки.", show_alert=True)
                        return
                    
                    # Проверяем, не просрочена ли поездка
                    if trip_end_dt(trip) < datetime.now():
                        await _answer_once("⚠️ Нельзя отменить бронирование на уже прошедшую поездку.", show_alert=True)
                        return
                    
                    # Подтверждение отмены
                    keyboard = [
                        [InlineKeyboardButton("✅ Да, отменить", callback_data=f"confirm_driver_cancel_{booking_id}")],
                        [InlineKeyboardButton("❌ Нет, оставить", callback_data=f"keep_driver_booking_{booking_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await _answer_once()
                    
                    await query.edit_message_text(
                        text="❓ Вы уверены, что хотите отменить бронирование пассажира? Пассажир получит уведомление об отмене.",
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logging.error(f"Ошибка в cancel_booking_by_driver: {e}")
                    await _answer_once("❌ Произошла ошибка.", show_alert=True)
            
        elif data.startswith("contact_driver_"):
            # Пассажир хочет связаться с водителем
            parts = data.split("_")
            driver_id = int(parts[2])
            booking_id = int(parts[3])
            await contact_driver(query, context, driver_id, booking_id)
            
        elif data.startswith("show_my_trips_blocked_"):
            # Нажата кнопка "📋 Мои поездки" из карточки блокировки создания.
            # Нужно: закрыть карточку, удалить триггер "🚗 Создать поездку" и показать активные поездки.
            chat_id = query.message.chat_id

            # 1) удаляем сообщение бота (карточку блокировки)
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass

            # 2) удаляем сообщение пользователя-триггер ("🚗 Создать поездку")
            try:
                user_msg_id = int(data.split("_")[-1])
            except Exception:
                user_msg_id = 0

            if user_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
                except Exception:
                    pass

            # 3) показываем активные поездки (как в "Мои поездки")
            await send_my_trips_cards(chat_id, query.from_user.id, context)
            return

        elif data == "show_my_trips_blocked":
            # Показывает поездки пользователя при блокировке создания новой
            await show_blocked_my_trips(query, context)

        elif data.startswith("confirm_driver_cancel_"):
            # Водитель подтверждает отмену бронирования пассажира
            booking_id = int(data.split("_")[3])
            
            with Session() as session:
                try:
                    booking = session.query(Booking).get(booking_id)
                    
                    if not booking:
                        await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                        return
                    
                    # ✅ Анти-повтор: если бронь уже отменена/отклонена — НЕ шлём пассажиру уведомление и НЕ трогаем места
                    if booking.status in (BookingStatus.CANCELLED.value, BookingStatus.REJECTED.value, BookingStatus.EXPIRED.value):
                        await _answer_once("✅ Это бронирование уже отменено.", show_alert=True)
                        try:
                            await query.message.delete()  # убираем устаревшую карточку у водителя
                        except Exception:
                            pass
                        return

                    # Проверяем, что пользователь - водитель этой поездки
                    trip = booking.trip
                    if trip.driver_id != query.from_user.id:
                        await _answer_once("⚠️ Вы не можете отменять бронирования этой поездки.", show_alert=True)
                        return
                    
                    # Возвращаем место
                    trip.seats_available += booking.seats_booked
                    booking.status = BookingStatus.CANCELLED.value
                    session.commit()
                    
                    # Уведомляем пассажира
                    await booking_module.notify_passenger_booking_rejected(context.bot, booking)
                    
                    keyboard = [
                        [InlineKeyboardButton("📝 Добавить причину отмены", callback_data=f"add_cancel_reason_{booking_id}")],
                        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_cancelled_booking_card_{booking_id}")]
                    ]
                    await _answer_once()
                    await query.edit_message_text(
                        text="✅ Бронирование пассажира отменено.",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )                    
                except Exception as e:
                    logging.error(f"Ошибка при отмене бронирования: {e}")
                    await _answer_once("❌ Произошла ошибка.", show_alert=True)
        
        elif data.startswith("close_cancelled_booking_card_"):
            # Закрыть карточку "Бронирование пассажира отменено"
            try:
                await _answer_once()
                await query.message.delete()
            except Exception:
                pass
            return

        elif data.startswith("add_cancel_reason_"):
            # Заглушка: причина отмены
            await _answer_once("🛠️ Скоро добавим возможность указать причину отмены.", show_alert=True)
            return

        elif data.startswith("keep_driver_booking_"):
            # Водитель передумал отменять бронирование -> возвращаемся в предыдущую карточку брони
            booking_id = int(data.split("_")[3])

            with Session() as session:
                booking = session.query(Booking).get(booking_id)
                if not booking:
                    await _answer_once("❌ Бронирование не найдено.", show_alert=True)
                    return

                trip = booking.trip
                if not trip or not getattr(trip, "is_active", False):
                    await _answer_once("⚠️ Поездка не активна.", show_alert=True)
                    return

                if trip.driver_id != query.from_user.id:
                    await _answer_once("⚠️ Вы не можете управлять бронированиями этой поездки.", show_alert=True)
                    return

                status_map = {
                    BookingStatus.PENDING.value: "⏳ Ожидает подтверждения",
                    BookingStatus.CONFIRMED.value: "✅ Подтверждено",
                    BookingStatus.EXPIRED.value: "⌛ Истекло",
                }
                status = status_map.get(booking.status, booking.status)

                passenger_username = None
                try:
                    user_chat = await context.bot.get_chat(booking.passenger_id)
                    if user_chat and getattr(user_chat, "username", None):
                        passenger_username = f"@{user_chat.username}"
                except Exception:
                    passenger_username = None

                passenger_info = f"👤 *Пассажир:* {booking.passenger_name}"
                if passenger_username:
                    passenger_info += f" ({passenger_username})"

                booking_info = (
                    f"{passenger_info}\n"
                    f"💺 *Мест:* {booking.seats_booked}\n"
                    f"📅 *Забронировано:* {format_booking_time(booking.booking_time, 8)}\n"
                    f"📊 *Статус:* {status}\n"
                )

                keyboard_buttons = []
                if booking.status == BookingStatus.PENDING.value:
                    keyboard_buttons.append(
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_booking_{booking.id}")
                    )
                    keyboard_buttons.append(
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_booking_{booking.id}")
                    )
                else:
                    contact_button_text = "📞 Связаться"
                    if passenger_username:
                        contact_button_text = f"📞 {passenger_username}"

                    keyboard_buttons.append(
                        InlineKeyboardButton(contact_button_text, callback_data=f"contact_passenger_{booking.passenger_id}_{booking.id}")
                    )
                    keyboard_buttons.append(
                        InlineKeyboardButton("🚫 Отменить бронь", callback_data=f"cancel_driver_booking_{booking.id}")
                    )

                await _answer_once()
                await query.edit_message_text(
                    text=booking_info,
                    reply_markup=InlineKeyboardMarkup([keyboard_buttons])
                )
                return                                          

        elif data == "search_new_trips":
            # Поиск новых поездок
            await search_trips_from_callback(query, context)

        elif data == "back_to_main":
            # Возврат в главное меню
            await _answer_once()
            await query.edit_message_text(
                "Главное меню:",
                reply_markup=keyboards.get_main_menu()
            )


        elif data.startswith("s_detail_"):
            # Поиск поездок: открыть детали с кнопками "Забронировать" + "Назад"
            try:
                trip_id = int(data.split("_")[2])
            except Exception:
                await _answer_once("❌ Не удалось открыть поездку.", show_alert=True)
                return

            with Session() as session:
                trip = session.query(Trip).get(trip_id)

            if not trip:
                await _answer_once()
                await query.edit_message_text("❌ Поездка не найдена.")
                return

            price_display = f"{int(trip.price)}₽" if trip.price and trip.price > 0 else "Бесплатно"

            details_text = (
                "🚗 *Детали поездки*\\n"
                "────────────────────\\n\\n"
                f"📍 *Маршрут:* {trip.departure_point} → {trip.destination_point}\\n"
                f"📅 *Дата и время:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\\n"
                f"💺 *Свободных мест:* {trip.seats_available}\\n"
                f"💰 *Цена:* {price_display}\\n"
                f"👤 *Водитель:* {trip.driver_name}\\n"
                "────────────────────"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Забронировать", callback_data=f"book_{trip.id}")],
                [InlineKeyboardButton("🔙 Назад", callback_data=f"s_back_{trip.id}")],
            ])

            await _answer_once()

            await query.edit_message_text(details_text, reply_markup=kb)
            return

        elif data.startswith("s_back_"):
            # Поиск поездок: вернуться к карточке-списку с кнопкой "Подробнее"
            try:
                trip_id = int(data.split("_")[2])
            except Exception:
                await _answer_once()
                return

            with Session() as session:
                trip = session.query(Trip).get(trip_id)

            if not trip:
                await _answer_once()
                await query.edit_message_text("❌ Поездка не найдена.")
                return

            time_str = format_trip_time(trip)
            price_display = f"{int(trip.price)}₽" if trip.price and trip.price > 0 else "Бесплатно"

            card_text, kb = notifications_module.build_trip_search_card(trip)

            await _answer_once()
            await query.edit_message_text(card_text, reply_markup=kb)
            return

        elif data.startswith("trip_details_"):
            # Показать детали поездки
            trip_id = int(data.split("_")[2])
            await show_trip_details(query, context, trip_id)

        elif data == "create_new_from_search":
            # Создать новую поездку из поиска
            await new_trip(query, context)

    except Exception as e:
        logging.error(f"Ошибка в button_callback: {e}")
        await _answer_once("⚠️ Произошла ошибка. Попробуйте позже.")

    # если ни одна ветка не сделала answer, закрываем «часики»
    await _answer_once()

async def search_trips_from_callback(query, context):
    """Обработка поиска поездок из callback."""
    message = "📅 Выберите дату для поиска поездок:"
    
    trigger_id = context.user_data.get("search_trigger_msg_id") or 0
    reply_markup = keyboards.get_date_selection_keyboard(cancel_cb=f"date_cancel_{trigger_id}")

    await query.edit_message_text(
        message,
        reply_markup=reply_markup
    )

async def show_trip_details(query, context, trip_id):
    """Показать детали поездки."""
    with Session() as session:
        try:
            trip = session.query(Trip).get(trip_id)
            
            if not trip:
                await query.edit_message_text("❌ Поездка не найдена.")
                return
            
            price_display = f"{trip.price} ₽" if trip.price and trip.price > 0 else "🎁 Бесплатно"
            status = "🟢 Активна" if trip.is_active and trip.date >= datetime.now() else "🔴 Завершена"
            
            message_text = f"""
<b>🚗 Детали поездки</b>
<code>────────────────────</code>

<b>📍 Маршрут:</b> {trip.departure_point} → {trip.destination_point}
<b>📅 Дата и время:</b> {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}
<b>💺 Свободных мест:</b> {trip.seats_available}
<b>💰 Цена:</b> {price_display}
<b>👤 Водитель:</b> {trip.driver_name}
<b>📊 Статус:</b> {status}
<code>────────────────────</code>
"""
            
            keyboard = [[
                InlineKeyboardButton("✅ Забронировать", callback_data=f"book_{trip.id}"),
                InlineKeyboardButton("❌ Закрыть", callback_data="close_trip_details")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logging.error(f"Ошибка в show_trip_details: {e}")
            await query.edit_message_text("❌ Произошла ошибка при загрузке деталей поездки.")

async def show_all_my_trips_from_blocked(query, context):
    """Показать все поездки пользователя."""
    try:
        with Session() as session:
            user_id = query.from_user.id
            
            # Получаем все поездки пользователя
            all_trips = session.query(Trip).filter(
                Trip.driver_id == user_id
            ).order_by(Trip.date.desc()).all()
        
        if not all_trips:
            await query.edit_message_text("📭 У вас нет созданных поездок.")
            return
        
        await query.edit_message_text(f"📋 Все ваши поездки ({len(all_trips)}):")
        
        for trip in all_trips:
            status = "🟢 Активна" if trip.is_active and trip.date >= datetime.now() else "🔴 Завершена"
            seats_icon = "💺" if trip.seats_available > 0 else "⛔"
            price_icon = "💰" if trip.price else "🎁"
            
            message = (
                f"{status}\n"
                f"📍 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
                f"⏰ *Время:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"{seats_icon} *Места:* {trip.seats_available}\n"
                f"{price_icon} *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
            )
            
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=message
            )
            
    except Exception as e:
        logging.error(f"Ошибка в show_all_my_trips_from_blocked: {str(e)}")
        await query.edit_message_text("❌ Произошла ошибка при загрузке поездок.")


async def notify_passengers_trip_cancelled(bot, trip):
    """Уведомляет всех пассажиров об отмене поездки."""
    try:
        with Session() as session:
            bookings = session.query(Booking).filter(
                Booking.trip_id == trip.id,
                Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
            ).all()
        
        message_text = (
            f"🚫 *ПОЕЗДКА ОТМЕНЕНА*\n\n"
            f"Водитель отменил поездку:\n"
            f"🚗 {trip.departure_point} → {trip.destination_point}\n"
            f"📅 {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n\n"
            f"Ваше бронирование автоматически отменено.\n"
            f"Вы можете найти другие поездки."
        )
        
        keyboard = [[
            InlineKeyboardButton("🔍 Найти другие поездки", callback_data="search_new_trips")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        for booking in bookings:
            try:
                await bot.send_message(
                    chat_id=booking.passenger_id,
                    text=message_text,
                    reply_markup=reply_markup
                )
                
                # Обновляем статус бронирования
                with Session() as session:
                    db_booking = session.query(Booking).get(booking.id)
                    if db_booking:
                        db_booking.status = BookingStatus.CANCELLED.value
                        session.commit()
                    
            except Exception as e:
                logging.error(f"Ошибка уведомления пассажира {booking.passenger_id}: {e}")
                
    except Exception as e:
        logging.error(f"Ошибка уведомления пассажиров: {e}")


async def handle_date_selection(query, context):
    """Обрабатывает выбор даты через inline-кнопки (с тем же UX, что и при ручном вводе даты)."""
    if query.data == "date_today":
        search_date = datetime.now().date()
    elif query.data == "date_tomorrow":
        search_date = datetime.now().date() + timedelta(days=1)
    elif query.data == "date_day_after":
        search_date = datetime.now().date() + timedelta(days=2)
    elif query.data == "date_custom":
        # Запоминаем message_id сообщения бота с клавиатурой выбора даты,
        # чтобы потом удалить его после ввода даты пользователем.
        context.user_data["search_custom_prompt_bot_msg_id"] = query.message.message_id
        await query.edit_message_text(
            "📝 Введите дату в формате ДД.ММ.ГГГГ:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="date_back")]
            ])
        )
        return
    elif query.data == "date_back":
        trigger_id = context.user_data.get("search_trigger_msg_id")
        cancel_cb = f"date_cancel_{trigger_id}" if trigger_id else "date_cancel"
        reply_markup = keyboards.get_date_selection_keyboard(cancel_cb=cancel_cb)
        await query.edit_message_text("📅 Выберите дату для поиска поездок:", reply_markup=reply_markup)
        return
    elif query.data == "date_cancel" or query.data.startswith("date_cancel_"):
        chat_id = query.message.chat_id

        # 1) Пытаемся взять id сообщения пользователя прямо из callback_data (анти-спам)
        user_msg_id = None
        if query.data.startswith("date_cancel_"):
            try:
                user_msg_id = int(query.data.split("_")[-1])
            except Exception:
                user_msg_id = None

        # 2) Fallback: если кнопка старая (без суффикса), используем прежнюю логику
        if not user_msg_id:
            user_msg_id = (
                context.user_data.pop("search_user_msg_id", None)
                or context.user_data.pop("search_trigger_msg_id", None)
                or context.user_data.pop("last_user_msg_id", None)
            )

        # 3) Удаляем сообщение пользователя (если можем)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

        # 4) Удаляем сообщение бота с выбором даты (то, где нажали "Отмена")
        try:
            await query.message.delete()
        except Exception:
            pass

        # 5) Чистим служебные поля поиска
        context.user_data.pop("search_bot_msg_id", None)
        context.user_data.pop("search_bot_msg_ids", None)
        return

    # ✅ Удаляем триггерное сообщение пользователя "🔍 Найти поездку" перед выводом результатов (чистый чат)
    trigger_msg_id = context.user_data.get("search_trigger_msg_id")
    if trigger_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=trigger_msg_id)
        except Exception:
            pass

    # ====== Дальше: показываем результаты так же, как при ручном вводе даты ======
    with Session() as session:
        user_id = query.from_user.id  # если это внутри callback'а
        # если это не callback, а update.message — тогда:
        # user_id = update.effective_user.id

        # 1) базовый запрос (как было)
        q = session.query(Trip).filter(
            Trip.date >= datetime.combine(search_date, datetime.min.time()),
            Trip.date < datetime.combine(search_date, datetime.max.time()),
            Trip.is_active == True,
            Trip.seats_available > 0,
            func.coalesce(Trip.end_date, Trip.date) >= datetime.now()
        )

        # 2) подтягиваем фильтр пользователя
        u = session.query(BotUser).filter(BotUser.telegram_id == user_id).one_or_none()

        # 3) если фильтр включён — добавляем условия
        if u and getattr(u, "search_filter_enabled", False):
            if getattr(u, "search_filter_departure", None):
                q = q.filter(Trip.departure_point == u.search_filter_departure)
            if getattr(u, "search_filter_destination", None):
                q = q.filter(Trip.destination_point == u.search_filter_destination)

        trips = q.order_by(Trip.date.asc()).all()

    # Если поездок нет — редактируем текущее сообщение (выбор даты) и даем "Закрыть"
    if not trips:
        trigger_id = context.user_data.get("search_trigger_msg_id") or 0
        keyboard = [
            [InlineKeyboardButton("🔙 Назад к поиску", callback_data=f"search_back_{trigger_id}")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_search_results_{trigger_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            (
                f"📭 На {search_date.strftime('%d.%m.%Y')} активных поездок не найдено.\n\n"
                "💡 Попробуйте:\n"
                "• Выбрать другую дату\n"
                "• Проверить позже"
            ),
            reply_markup=reply_markup
        )
        return

    # Удаляем сообщение с выбором даты (чтобы не оставлять карточку "найдено X")
    try:
        await query.message.delete()
    except Exception:
        pass

    # Показ поездок: каждая отдельной карточкой + "Подробнее"
    trips_to_show = trips[:10]
    context.user_data.setdefault("search_bot_msg_ids", [])
    context.user_data.setdefault("search_all_msg_ids", [])

    # список для текущего поиска — очищаем, но общий — НЕ трогаем
    context.user_data["search_bot_msg_ids"] = []

    for trip in trips_to_show:
        time_str = format_trip_time(trip)
        card_text = render_trip_card(
            title="🚗 Поездка",
            date=getattr(trip, "date", None),
            time_str=time_str,
            departure=getattr(trip, "departure_point", "—"),
            destination=getattr(trip, "destination_point", "—"),
            seats_available=int(getattr(trip, "seats_available", 0) or 0),
            price=getattr(trip, "price", None),
        )

        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("ℹ️ Подробнее", callback_data=f"s_detail_{trip.id}")]
        ])

        msg = await send_tracked_message(
            context,
            chat_id=query.message.chat_id,
            text=card_text,
            reply_markup=reply_markup
        )
        context.user_data["search_bot_msg_ids"].append(msg.message_id)
        context.user_data["search_all_msg_ids"].append(msg.message_id)

    if len(trips) > len(trips_to_show):
        info = f"ℹ️ Показано {len(trips_to_show)} из {len(trips)} поездок на {search_date.strftime('%d.%m.%Y')}."
        msg = await send_tracked_message(context, query.message.chat_id, info)
        context.user_data["search_bot_msg_ids"].append(msg.message_id)
        context.user_data["search_all_msg_ids"].append(msg.message_id)

# ========== НОВЫЕ ФУНКЦИИ ДЛЯ ОБРАБОТКИ КНОПОК ==========

async def show_trip_bookings(query, context, trip_id, trigger_id: int = 0):
    """Показывает активные бронирования для конкретной поездки."""
    with Session() as session:
        try:
            # Получаем поездку
            trip = session.query(Trip).get(trip_id)
            
            if not trip or not trip.is_active:
                await show_trip_deleted_card(query, trigger_id)
                return
                
            # Проверяем, что пользователь - владелец поездки
            if trip.driver_id != query.from_user.id:
                await query.answer("⚠️ Вы не можете просматривать бронирования этой поездки.")
                return
            
            # Проверяем, не просрочена ли поездка
            if trip_end_dt(trip) < datetime.now():
                await query.edit_message_text(
                    f"🚗 *{trip.departure_point} → {trip.destination_point}*\n"
                    f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n\n"
                    f"⚠️ *Поездка уже прошла. Бронирования недоступны.*"
                )
                return
                
            # Получаем только АКТИВНЫЕ бронирования для этой поездки
            bookings = session.query(Booking).filter(
                Booking.trip_id == trip_id,
                Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
            ).order_by(Booking.booking_time.desc()).all()
            
            if not bookings:
                message = (
                    f"🚗 *{trip.departure_point} → {trip.destination_point}*\n"
                    f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                    f"💺 *Свободно мест:* {trip.seats_available}\n"
                    f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n\n"
                    f"📭 На эту поездку пока нет активных бронирований."
                )

                keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_trips")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    reply_markup=reply_markup
                )
                return
            # ✅ Чистый чат (шаг 3):
            # - удаляем карточку, из которой открыли "Активные бронирования" (текущее сообщение бота)
            # - удаляем триггерное сообщение пользователя "📋 Мои поездки" (last_user_msg_id)
            try:
                await query.message.delete()
            except Exception:
                pass

            try:
                chat_id = query.message.chat_id
                user_msg_id = context.user_data.get("last_user_msg_id")
                if isinstance(user_msg_id, int) and user_msg_id:
                    await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception:
                pass

            # ❗ Не выводим карточку "📋 Активные бронирования" — оставляем только карточки пассажиров
# Отправляем отдельное сообщение для каждого активного бронирования
            for booking in bookings:
                status_map = {
                    BookingStatus.PENDING.value: '⏳ Ожидает подтверждения',
                    BookingStatus.CONFIRMED.value: '✅ Подтверждено',
                    BookingStatus.EXPIRED.value: '⌛ Истекло',
                }
                
                status = status_map.get(booking.status, booking.status)
                
                # Пытаемся получить username пассажира из Telegram
                passenger_username = None
                try:
                    # Получаем информацию о пользователе из Telegram
                    user_chat = await context.bot.get_chat(booking.passenger_id)
                    if user_chat.username:
                        passenger_username = f"@{user_chat.username}"
                except Exception as e:
                    logging.error(f"Не удалось получить username для пользователя {booking.passenger_id}: {e}")
                    passenger_username = None
                
                # Формируем информацию о пассажире
                passenger_info = f"👤 *Пассажир:* {booking.passenger_name}"
                if passenger_username:
                    passenger_info += f" ({passenger_username})"
                
                booking_info = (
                    f"{passenger_info}\n"
                    f"💺 *Мест:* {booking.seats_booked}\n"
                    f"📅 *Забронировано:* {format_booking_time(booking.booking_time, 8)}\n"
                    f"📊 *Статус:* {status}\n"
                )
                
                # Создаем кнопки в зависимости от статуса бронирования
                keyboard_buttons = []
                
                if booking.status == BookingStatus.PENDING.value:
                    # Кнопки для ожидающих подтверждения бронирований
                    keyboard_buttons.append(
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_booking_{booking.id}")
                    )
                    keyboard_buttons.append(
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_booking_{booking.id}")
                    )
                else:
                    # Кнопки для подтвержденных бронирований
                    keyboard_buttons.append(
                        InlineKeyboardButton("🚫 Отменить бронь", callback_data=f"cancel_driver_booking_{booking.id}")
                    )

                keyboard = [keyboard_buttons, [InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_trips")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=booking_info,
                    reply_markup=reply_markup
                )
                    
        except Exception as e:
            logging.error(f"Ошибка в show_trip_bookings: {e}")
            await query.edit_message_text("❌ Произошла ошибка при загрузке бронирований.")



def _trip_has_confirmed_bookings(session: Session, trip_id: int) -> bool:
    """True если по поездке есть подтверждённые бронирования."""
    try:
        return session.query(Booking).filter(
            Booking.trip_id == trip_id,
            Booking.status == BookingStatus.CONFIRMED.value
        ).count() > 0
    except Exception:
        return True


async def show_edit_menu(query, context, trip_id, trigger_id: int = 0):
    """Показывает меню редактирования поездки."""
    with Session() as session:
        try:
            trip = session.query(Trip).get(trip_id)
            
            if not trip or not trip.is_active:
                await show_trip_deleted_card(query, trigger_id)
                return
                
            # Проверяем, что пользователь - владелец поездки
            if trip.driver_id != query.from_user.id:
                await query.answer("⚠️ Вы не можете редактировать эту поездку.")
                return
                

            # защита: если есть подтверждённые бронирования — редактирование запрещено
            if _trip_has_confirmed_bookings(session, trip_id):
                await query.answer(
                    "⚠️ Есть подтверждённые бронирования. Редактирование недоступно.",
                    show_alert=True
                )
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_exit_{trip_id}")]])
                await query.edit_message_text(
                    "⚠️ *Редактирование запрещено*\n\n"
                    "По этой поездке уже есть подтверждённые бронирования.\n"
                    "Сначала отмените подтверждённые брони, затем можно редактировать поездку.",
                    reply_markup=kb
                )
                return

            # Создаем клавиатуру для редактирования
            keyboard = [
                [
                    InlineKeyboardButton("📍 Пункт отправления", callback_data=f"edit_departure_{trip_id}"),
                    InlineKeyboardButton("🎯 Пункт назначения", callback_data=f"edit_destination_{trip_id}")
                ],
                [
                    InlineKeyboardButton("📅 Дата и время", callback_data=f"edit_date_{trip_id}"),
                    InlineKeyboardButton("💺 Количество мест", callback_data=f"edit_seats_{trip_id}")
                ],
                [
                    InlineKeyboardButton("💰 Цена", callback_data=f"edit_price_{trip_id}")
                ],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_exit_{trip_id}")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Информация о поездке
            trip_info = (
                f"✏️ *Редактирование поездки:*\n\n"
                f"🚗 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
                f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"💺 *Места:* {trip.seats_available}\n"
                f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
                f"👇 Выберите что хотите изменить:"
            )
            
            await query.edit_message_text(
                text=trip_info,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logging.error(f"Ошибка в show_edit_menu: {e}")
            await query.edit_message_text("❌ Произошла ошибка.")
            
async def show_trip_deleted_card(query, trigger_id: int = 0):
    keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_deleted_trip_{trigger_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🗑️ *Поездка была удалена.*",
        reply_markup=reply_markup
    )

async def show_edit_menu_by_message_id(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE, trip_id: int):
    """То же что show_edit_menu, но редактирует конкретное сообщение по id (для чистого чата)."""
    with Session() as session:
        trip = session.query(Trip).get(trip_id)
        if not trip:
            return

    keyboard = [
        [InlineKeyboardButton("📍 Пункт отправления", callback_data=f"edit_departure_{trip_id}")],
        [InlineKeyboardButton("🎯 Пункт назначения", callback_data=f"edit_destination_{trip_id}")],
        [InlineKeyboardButton("📅 Дата и время", callback_data=f"edit_date_{trip_id}")],
        [InlineKeyboardButton("💺 Количество мест", callback_data=f"edit_seats_{trip_id}")],
        [InlineKeyboardButton("💰 Цена", callback_data=f"edit_price_{trip_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_exit_{trip_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    trip_info = (
        f"✏️ *Редактирование поездки:*\n\n"
        f"🚗 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
        f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
        f"💺 *Места:* {trip.seats_available}\n"
        f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
        f"👇 Выберите что хотите изменить:"
    )

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=trip_info,
        reply_markup=reply_markup
    )

async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод при редактировании поездки."""
    trip_id = context.user_data.get("editing_trip_id")
    field = context.user_data.get("editing_field")
    value = update.message.text

    # ✅ Чистый чат: удаляем ввод пользователя при редактировании (как в создании)
    try:
        await update.message.delete()
    except Exception:
        pass


    async def _deny_edit_text():
        # удаляем ввод пользователя (чистый чат)
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        # перерисовываем меню редактирования (если оно есть) в "недоступно"
        edit_menu_msg_id = context.user_data.get("edit_menu_msg_id")
        if edit_menu_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=edit_menu_msg_id,
                    text="⚠️ *Редактирование недоступно:* поездка уже прошла.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✖️ Закрыть", callback_data="close_edit_menu")]
                    ])
                )
            except Exception:
                pass

        # сбрасываем состояние редактирования
        context.user_data.pop("editing_field", None)
        context.user_data.pop("editing_trip_id", None)
        # edit_menu_msg_id можно оставить, он нужен для закрытия (или убрать — на твой вкус)

    if not trip_id or not field:
        return

    with Session() as session:
        trip = session.query(Trip).get(trip_id)
        if not trip:
            return

        # Проверяем права доступа
        if trip.driver_id != update.effective_user.id:
            return

        # 🚫 Второй замок: прошедшую поездку редактировать нельзя
        now = datetime.now()
        if trip.date < now:
            await _deny_edit_text()
            return


        # 🚫 Третий замок: если есть подтверждённые бронирования — редактирование запрещено
        if _trip_has_confirmed_bookings(session, trip_id):
            await update.message.reply_text(
                "⚠️ По этой поездке есть *подтверждённые бронирования*.\n"
                "Редактирование недоступно. Сначала отмените подтверждённые брони."
            )
            return

        # Обрабатываем разные типы полей
        if field == "departure":
            exact, suggestions, _fuzzy = _creation_location_matches(value, limit=8)
            if not exact:
                # Сохраняем подсказки для кнопок выбора
                context.user_data.setdefault("edit_suggestions", {})[f"{trip_id}:departure"] = suggestions[:8]
                kb = _edit_suggestions_keyboard("departure", trip_id, suggestions[:8])
                await update.message.reply_text(
                    "❌ *Неизвестный пункт отправления.*\n\n"                    "Выберите из подсказок ниже или введите по-другому (2–3 буквы).",
                    reply_markup=kb,
                )
                return
            # Нельзя делать "откуда" == "куда"
            if locations.norm(exact) == locations.norm(trip.destination_point):
                await update.message.reply_text("❌ Пункт отправления не может совпадать с пунктом назначения.")
                return
            trip.departure_point = exact

        elif field == "destination":
            exact, suggestions, _fuzzy = _creation_location_matches(value, limit=8)
            if not exact:
                context.user_data.setdefault("edit_suggestions", {})[f"{trip_id}:destination"] = suggestions[:8]
                kb = _edit_suggestions_keyboard("destination", trip_id, suggestions[:8])
                await update.message.reply_text(
                    "❌ *Неизвестный пункт назначения.*\n\n"                    "Выберите из подсказок ниже или введите по-другому (2–3 буквы).",
                    reply_markup=kb,
                )
                return
            if locations.norm(exact) == locations.norm(trip.departure_point):
                await update.message.reply_text("❌ Пункт назначения не может совпадать с пунктом отправления.")
                return
            trip.destination_point = exact


        elif field == "edit_date_manual":
            # Ввод даты вручную (ДД.ММ.ГГГГ) при редактировании, затем выбор времени кнопками
            try:
                chosen = datetime.strptime(value, "%d.%m.%Y").date()
            except ValueError:
                await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ")
                return

            if chosen < datetime.now().date():
                await update.message.reply_text("❌ Нельзя установить дату поездки в прошлом.")
                return

            context.user_data["edit_date_only"] = chosen
            context.user_data["editing_field"] = "edit_time_select"

            edit_menu_msg_id = context.user_data.get("edit_menu_msg_id")
            if edit_menu_msg_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=edit_menu_msg_id,
                    text=(
                        f"📅 *Дата:* {chosen.strftime('%d.%m.%Y')}\n\n"
                        "⏰ *Выберите время поездки:*"
                    ),
                    reply_markup=_edit_trip_time_choice_kb(trip_id),
                )
            else:
                await update.message.reply_text(
                    f"📅 *Дата:* {chosen.strftime('%d.%m.%Y')}\n\n"
                    "⏰ *Выберите время поездки:*",
                    reply_markup=_edit_trip_time_choice_kb(trip_id),
                )
            return

        elif field == "edit_time_manual":
            # Ввод точного времени (ЧЧ:ММ) при редактировании
            date_only = context.user_data.get("edit_date_only")
            if not date_only:
                await update.message.reply_text("❌ Сначала выберите дату.")
                return

            try:
                t = datetime.strptime(value, "%H:%M").time()
            except ValueError:
                await update.message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:ММ")
                return

            trip_dt = datetime.combine(date_only, t)
            if trip_dt < datetime.now():
                await update.message.reply_text("❌ Нельзя установить время поездки в прошлом.")
                return

            trip.date = trip_dt
            trip.end_date = trip_dt
            trip.time_mode = "exact"
            context.user_data.pop("edit_date_only", None)
            context.user_data["editing_field"] = None

        elif field == "date":
            try:
                new_date = datetime.strptime(value, "%d.%m.%Y %H:%M")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
                return

            if new_date < datetime.now():
                await update.message.reply_text("❌ Нельзя установить дату поездки в прошлом.")
                return

            trip.date = new_date

        elif field == "seats":
            try:
                seats = int(value)
                if seats <= 0:
                    await update.message.reply_text("❌ Количество мест должно быть больше 0")
                    return
            except ValueError:
                await update.message.reply_text("❌ Введите корректное число")
                return

            total_booked = session.query(func.sum(Booking.seats_booked)).filter(
                Booking.trip_id == trip_id,
                Booking.status.in_([BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value])
            ).scalar() or 0

            if seats < total_booked:
                await update.message.reply_text(
                    f"❌ Нельзя установить меньше мест ({seats}) чем уже забронировано ({total_booked})"
                )
                return

            trip.seats_available = seats - total_booked

        elif field == "price":
            try:
                trip.price = float(value)
            except ValueError:
                await update.message.reply_text("❌ Введите корректное число")
                return

        session.commit()

    # Удаляем сообщение пользователя с вводом (чистый чат)
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
    except Exception:
        pass

    # очищаем только поле (trip_id оставим)
    context.user_data.pop("editing_field", None)

    # Обновляем меню редактирования в том же сообщении
    edit_menu_msg_id = context.user_data.get("edit_menu_msg_id")
    if edit_menu_msg_id:
        await show_edit_menu_by_message_id(update.effective_chat.id, edit_menu_msg_id, context, trip_id)
    else:
        await update.message.reply_text("✅ Обновлено.")
                
async def show_updated_trip_info(update, context, trip_id):
    """Показывает обновленную информацию о поездке."""
    with Session() as session:
        try:
            trip = session.query(Trip).get(trip_id)
            
            if not trip:
                return
                
            status = "🟢 Активна" if trip.is_active else "🔴 Завершена"
            seats_icon = "💺" if trip.seats_available > 0 else "⛔"
            price_icon = "💰" if trip.price else "🎁"
            
            message = (
                f"{status}\n"
                f"📍 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
                f"⏰ *Время:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"{seats_icon} *Места:* {trip.seats_available}\n"
                f"{price_icon} *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
            )
            
            trigger_id = context.user_data.get("my_trips_trigger_msg_id") or 0

            keyboard = [
                [InlineKeyboardButton("👥 Бронирования", callback_data=f"trip_bookings_{trip.id}_{trigger_id}")],
                [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_trip_{trip.id}_{trigger_id}")],
                [InlineKeyboardButton("❌ Отменить поездку", callback_data=f"cancel_trip_{trip.id}_{trigger_id}")]
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(message, reply_markup=reply_markup)
            
        except Exception as e:
            logging.error(f"Ошибка в show_updated_trip_info: {e}")


async def cancel_trip(query, context, trip_id):
    """Отменяет поездку и уведомляет всех пассажиров."""
    with Session() as session:
        try:
            trip = session.query(Trip).get(trip_id)

            if not trip or not trip.is_active:
                await show_trip_deleted_card(query)
                return

            # Проверяем права доступа
            if trip.driver_id != query.from_user.id:
                await query.answer("⚠️ Вы не можете отменить эту поездку.")
                return

            # Отмечаем поездку как неактивную
            trip.is_active = False
            session.commit()

            # Уведомляем всех пассажиров
            await notify_passengers_trip_cancelled(context.bot, trip)

            trigger_id = context.user_data.pop("cancel_trip_trigger_msg_id", 0) or 0
            keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_trip_canceled_{trigger_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                text="❌ *Поездка отменена!*\n\nВсе пассажиры получили уведомления.",
                reply_markup=reply_markup
            )

        except Exception as e:
            logging.error(f"Ошибка в cancel_trip: {e}")
            try:
                await query.edit_message_text("❌ Произошла ошибка при отмене поездки.")
            except Exception:
                pass

async def handle_trip_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает подтверждение отмены поездки."""
    text = update.message.text.lower()
    trip_id = context.user_data['cancelling_trip_id']
    
    if text in ['да', 'yes', 'ок', 'ok', 'подтвердить']:
        with Session() as session:
            try:
                trip = session.query(Trip).get(trip_id)
                
                if trip and trip.driver_id == update.effective_user.id:
                    trip.is_active = False
                    session.commit()
                    
                    # Уведомляем всех пассажиров
                    await notify_passengers_trip_cancelled(context.bot, trip)
                    
                    await update.message.reply_text("✅ Поездка отменена! Все пассажиры получили уведомления.")
            except Exception as e:
                logging.error(f"Ошибка при отмене поездки: {e}")
                await update.message.reply_text("❌ Произошла ошибка при отмене поездки.")
    else:
        await update.message.reply_text("❌ Отмена поездки отменена.")
    
    # Очищаем данные
    if 'cancelling_trip_id' in context.user_data:
        del context.user_data['cancelling_trip_id']


async def contact_driver(query, context, driver_id, booking_id):
    """Помогает пассажиру связаться с водителем."""
    with Session() as session:
        try:
            # Получаем информацию о водителе
            trip = session.query(Trip).filter(
                Trip.driver_id == driver_id,
                Trip.id == session.query(Booking.trip_id).filter(Booking.id == booking_id).scalar()
            ).first()
            
            if not trip:
                await query.answer("❌ Информация о водителе не найдена.")
                return
                
            # Получаем информацию о бронировании
            booking = session.query(Booking).get(booking_id)
            
            if not booking or booking.passenger_id != query.from_user.id:
                await query.answer("❌ Бронирование не найдено.")
                return
                
            # Создаем сообщение с контактами
            contact_info = (
                f"📞 *Контакты водителя:*\n\n"
                f"👤 *Имя:* {trip.driver_name}\n"
            )
            
            # Проверяем, есть ли у водителя username
            try:
                driver_chat = await context.bot.get_chat(trip.driver_id)
                if driver_chat.username:
                    contact_info += f"👤 *Username:* @{driver_chat.username}\n"
                    contact_info += f"💬 *Ссылка:* https://t.me/{driver_chat.username}\n\n"
                else:
                    contact_info += f"👤 *Username:* не указан\n\n"
            except:
                contact_info += f"👤 *Username:* не удалось получить\n\n"
            
            contact_info += (
                f"🚗 *Поездка:* {trip.departure_point} → {trip.destination_point}\n"
                f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"💺 *Ваши места:* {booking.seats_booked}\n"
                f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n\n"
                f"💡 *Совет:* Напишите водителю в личные сообщения, представьтесь и уточните детали поездки."
            )
            
            await query.edit_message_text(
                text=contact_info
            )
            
            # Отправляем уведомление водителю
            try:
                notification_text = (
                    f"👤 *Пассажир хочет связаться с вами:*\n\n"
                    f"*Имя:* {booking.passenger_name}\n"
                    f"*Поездка:* {trip.departure_point} → {trip.destination_point}\n"
                    f"*Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                    f"*Мест:* {booking.seats_booked}\n\n"
                    f"Пассажир получил ваши контактные данные."
                )
                
                await context.bot.send_message(
                    chat_id=trip.driver_id,
                    text=notification_text
                )
            except Exception as e:
                logging.error(f"Ошибка уведомления водителя: {e}")
                
        except Exception as e:
            logging.error(f"Ошибка в contact_driver: {e}")
            await query.answer("❌ Произошла ошибка при получении контактов.")


async def cleanup_old_trips():
    """Очищает старые и завершенные поездки."""
    with Session() as session:
        try:
            # Находим просроченные поездки (более 7 дней назад)
            cutoff_date = datetime.now() - timedelta(days=7)
            
            old_trips = session.query(Trip).filter(
                func.coalesce(Trip.end_date, Trip.date) < cutoff_date
            ).all()
            
            # Удаляем связанные бронирования
            for trip in old_trips:
                # Удаляем все бронирования для этой поездки
                session.query(Booking).filter(Booking.trip_id == trip.id).delete()
                # Удаляем саму поездку
                session.delete(trip)
            
            session.commit()
            logging.info(f"Очищено {len(old_trips)} старых поездок")
            
        except Exception as e:
            logging.error(f"Ошибка при очистке старых поездок: {e}")

async def contact_passenger(query, context, passenger_id, booking_id):
    """Помогает водителю связаться с пассажиром."""
    with Session() as session:
        try:
            # Получаем информацию о бронировании
            booking = session.query(Booking).get(booking_id)
            
            if not booking:
                await query.answer("❌ Бронирование не найдено.", show_alert=True)
                return
                
            # Проверяем, что пользователь - водитель этой поездки
            trip = booking.trip
            if trip.driver_id != query.from_user.id:
                await query.answer("⚠️ Вы не можете связываться с пассажирами этой поездки.", show_alert=True)
                return
            
            # Пытаемся получить username пассажира из Telegram
            passenger_username = None
            try:
                user_chat = await context.bot.get_chat(passenger_id)
                passenger_username = user_chat.username
            except Exception as e:
                logging.error(f"Не удалось получить username для пассажира {passenger_id}: {e}")
            
            # Получаем информацию о пассажире
            passenger_name = booking.passenger_name
            
            # Создаем сообщение с контактами
            contact_info = (
                f"📞 *Контакты пассажира:*\n\n"
                f"👤 *Имя:* {passenger_name}\n"
            )
            
            if passenger_username:
                contact_info += f"👤 *Username:* @{passenger_username}\n"
                contact_info += f"💬 *Ссылка:* https://t.me/{passenger_username}\n\n"
            else:
                contact_info += f"👤 *Username:* не указан\n\n"
            
            contact_info += (
                f"🚗 *Поездка:* {trip.departure_point} → {trip.destination_point}\n"
                f"📅 *Дата:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"💺 *Мест:* {booking.seats_booked}\n"
                f"💰 *Цена:* {trip.price if trip.price else 'Бесплатно'}\n\n"
            )
            
            if passenger_username:
                contact_info += (
                    f"💡 *Как связаться:*\n"
                    f"1. Нажмите кнопку ниже для перехода в чат\n"
                    f"2. Или откройте Telegram и введите: @{passenger_username}"
                )
                
                # Кнопки для связи
                keyboard = [
                    [InlineKeyboardButton(f"💬 Написать @{passenger_username}", 
                                         url=f"https://t.me/{passenger_username}")],
                    [InlineKeyboardButton("📋 Скопировать ID", callback_data=f"copy_id_{passenger_id}")],
                    [InlineKeyboardButton("🔙 Назад к бронированиям", callback_data=f"trip_bookings_{trip.id}")]
                ]
            else:
                contact_info += (
                    f"💡 *Как связаться:*\n"
                    f"1. Откройте Telegram\n"
                    f"2. В поиске введите ID: `{passenger_id}`\n"
                    f"3. Или попробуйте найти по имени: `{passenger_name}`\n\n"
                    f"📌 *Совет:* Нажмите кнопку ниже, чтобы скопировать ID."
                )
                
                # Кнопки для связи
                keyboard = [
                    [InlineKeyboardButton("📋 Скопировать ID", callback_data=f"copy_id_{passenger_id}")],
                    [InlineKeyboardButton("🔙 Назад к бронированиям", callback_data=f"trip_bookings_{trip.id}")]
                ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text=contact_info,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logging.error(f"Ошибка в contact_passenger: {e}")
            await query.answer("❌ Произошла ошибка при получении контактов.")
        
async def show_blocked_my_trips(query, context):
    """Показывает активные поездки пользователя при блокировке создания новой поездки."""
    try:
        with Session() as session:
            user_id = query.from_user.id
            
            # Получаем только АКТИВНЫЕ поездки пользователя
            active_trips = session.query(Trip).filter(
                Trip.driver_id == user_id,
                Trip.is_active == True,
                func.coalesce(Trip.end_date, Trip.date) >= datetime.now()  # Только будущие поездки
            ).order_by(Trip.date.asc()).all()
        
        if not active_trips:
            # Если активных поездок нет
            await query.edit_message_text(
                "✅ Отлично! Активных поездок не обнаружено.\n\n"
                "Теперь вы можете создать новую поездку, используя кнопку '🚗 Создать поездку' в главном меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_main")]
                ])
            )
            return
        
        # Отправляем каждую активную поездку отдельным сообщением
        for trip in active_trips:
            status = "🟢 Активна"
            seats_icon = "💺" if trip.seats_available > 0 else "⛔"
            price_icon = "💰" if trip.price else "🎁"
            
            message = (
                f"{status}\n"
                f"📍 *Маршрут:* {trip.departure_point} → {trip.destination_point}\n"
                f"⏰ *Время:* {trip.date.strftime('%d.%m.%Y')} {format_trip_time(trip)}\n"
                f"{seats_icon} *Места:* {trip.seats_available}\n"
                f"{price_icon} *Цена:* {trip.price if trip.price else 'Бесплатно'}\n"
            )
            
            # Для активных поездок показываем полный набор кнопок
            keyboard = [
                [
                    [InlineKeyboardButton("👥 Бронирования", callback_data=f"trip_bookings_{trip.id}")],
                    [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_trip_{trip.id}")],
                    [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_trip_{trip.id}")]
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Отправляем сообщение
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=message,
                reply_markup=reply_markup
            )

        # Простая клавиатура с одной кнопкой
        keyboard = [
            [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=instruction_text,
            reply_markup=reply_markup
        )
            
    except Exception as e:
        logging.error(f"Ошибка в show_blocked_my_trips: {str(e)}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке поездок.",
            reply_markup=keyboards.get_main_menu()
        )
        
async def handle_passenger_trip_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    booking_id = int(update.callback_query.data.split('_')[-1])

    # Обновляем статус бронирования в базе данных
    with Session() as session:
        booking = session.query(Booking).filter(Booking.id == booking_id).first()
        if booking:
            booking.status = BookingStatus.CONFIRMED.value  # Устанавливаем статус "поездка состоялась"
            session.commit()
    
    # Ответ пользователю
    await update.callback_query.answer("Поездка отмечена как завершенная.")
    await update.callback_query.message.edit_text("Спасибо за обратную связь! 🚗✨")

async def handle_passenger_trip_not_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    booking_id = int(update.callback_query.data.split('_')[-1])

    # Обновляем статус бронирования в базе данных
    with Session() as session:
        booking = session.query(Booking).filter(Booking.id == booking_id).first()
        if booking:
            booking.status = BookingStatus.CANCELLED.value  # Устанавливаем статус "поездка не состоялась"
            session.commit()

    # Ответ пользователю
    await update.callback_query.answer("Поездка не состоялась.")
    await update.callback_query.message.edit_text("Мы сожалеем, что поездка не состоялась. 🚫")


# ====== МОДУЛЬНЫЕ НАСТРОЙКИ (override) ======
# Используем модуль settings_module для UI настроек/фильтра/уведомлений
show_settings_menu = settings_module.show_settings_menu
show_search_filter_settings = settings_module.show_search_filter_settings
