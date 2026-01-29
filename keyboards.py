# keyboards.py
from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup

def get_main_menu():
    """Возвращает главное меню"""
    keyboard = [
        ["🚗 Создать поездку", "🔍 Найти поездку"],
        ["📋 Мои поездки", "🎫 Мои бронирования"],
        ["❓ Помощь", "⚙️ Настройки"],
        ["🗑️ Очистить историю"]  # Заменили "🔙 Назад" на "🗑️ Очистить историю"
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_date_selection_keyboard(cancel_cb: str = "date_cancel"):
    """Клавиатура для выбора даты поиска"""
    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня", callback_data="date_today"),
            InlineKeyboardButton("📅 Завтра", callback_data="date_tomorrow"),
            InlineKeyboardButton("📅 Послезавтра", callback_data="date_day_after")
        ],
        [
            InlineKeyboardButton("📝 Ввести дату", callback_data="date_custom"),
            InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_search_results_keyboard(trip_id):
    """Клавиатура для результатов поиска"""
    keyboard = [[
        InlineKeyboardButton("✅ Забронировать место", callback_data=f"book_{trip_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_booking_management_keyboard(booking_id):
    """Клавиатура для управления бронированием (для водителя)"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_booking_{booking_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_booking_{booking_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_passenger_booking_keyboard(booking_id):
    """Клавиатура для пассажира (управление бронированием)"""
    keyboard = [[
        InlineKeyboardButton("❌ Отменить бронирование", callback_data=f"cancel_booking_{booking_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)
    
def get_clear_history_confirm_keyboard():
    """Клавиатура для подтверждения очистки чата"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, очистить чат", callback_data="clear_chat_confirm"),
            InlineKeyboardButton("❌ Нет, отменить", callback_data="clear_chat_cancel")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
    
def get_passenger_feedback_keyboard(booking_id):
    """Клавиатура для пассажира с кнопками 'Поездка состоялась' и 'Поездка не состоялась'."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Поездка состоялась", callback_data=f"passenger_trip_completed_{booking_id}"),
            InlineKeyboardButton("❌ Поездка не состоялась", callback_data=f"passenger_trip_not_completed_{booking_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
    
def get_driver_rating_keyboard(booking_id: int):
    """Клавиатура оценки водителя пассажиром: 1..5 ⭐ + Закрыть."""
    keyboard = [
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"passenger_rate_driver_{booking_id}_1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"passenger_rate_driver_{booking_id}_2"),
            InlineKeyboardButton("⭐ 3", callback_data=f"passenger_rate_driver_{booking_id}_3"),
            InlineKeyboardButton("⭐ 4", callback_data=f"passenger_rate_driver_{booking_id}_4"),
            InlineKeyboardButton("⭐ 5", callback_data=f"passenger_rate_driver_{booking_id}_5"),
        ],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_passenger_rate_driver_{booking_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_close_only_keyboard(cb: str = "noop"):
    """Простая клавиатура с одной кнопкой Закрыть."""
    keyboard = [[InlineKeyboardButton("✖️ Закрыть", callback_data=cb)]]
    return InlineKeyboardMarkup(keyboard)

def get_passenger_rating_keyboard(booking_id: int):
    """Клавиатура оценки пассажира водителем: 1..5 ⭐ + Закрыть."""
    keyboard = [
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_passenger_{booking_id}_1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate_passenger_{booking_id}_2"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate_passenger_{booking_id}_3"),
            InlineKeyboardButton("⭐ 4", callback_data=f"rate_passenger_{booking_id}_4"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate_passenger_{booking_id}_5"),
        ],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=f"close_rate_passenger_{booking_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_passenger_rating_saved_keyboard(booking_id: int):
    keyboard = [
        [InlineKeyboardButton("🚪 Выйти из поездки", callback_data=f"exit_trip_{booking_id}")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data="close_passenger_rating_saved")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_driver_cancel_notice_keyboard(*, passenger_username: str | None = None, passenger_id: int | None = None):
    """Кнопки для уведомления водителю об отмене бронирования.

        - Добавляем кнопку "Мои поездки" (inline), чтобы водитель мог быстро вернуться к списку.
    - Добавляем "Закрыть".
    """
    keyboard = []

    keyboard.append([
        InlineKeyboardButton("📋 Мои поездки", callback_data="driver_open_my_trips")
    ])

    keyboard.append([
        InlineKeyboardButton("✖️ Закрыть", callback_data="close_driver_cancel_notice")
    ])

    return InlineKeyboardMarkup(keyboard)
