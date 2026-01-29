# broadcast.py
import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from database import Session  # твой sessionmaker
from user_registry import BotUser  # модель таблицы пользователей

logger = logging.getLogger(__name__)

STATE_KEY = "admin_broadcast_state"
TEXT_KEY = "admin_broadcast_text"

STATE_WAIT_TEXT = "wait_text"
STATE_WAIT_CONFIRM = "wait_confirm"


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: str, text: str | None = None) -> None:
    context.user_data[STATE_KEY] = state
    if text is not None:
        context.user_data[TEXT_KEY] = text


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(STATE_KEY, None)
    context.user_data.pop(TEXT_KEY, None)


def is_broadcast_waiting_text(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get(STATE_KEY) == STATE_WAIT_TEXT


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старт рассылки: просим текст."""
    _set_state(context, STATE_WAIT_TEXT)
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_broadcast_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "📣 *Рассылка*\n\nВведите текст рассылки:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "📣 *Рассылка*\n\nВведите текст рассылки:",
            reply_markup=reply_markup
        )


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловим текст рассылки от админа и показываем превью в конечном виде (HTML)."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❗ Текст пустой. Введите текст рассылки.")
        return

    _set_state(context, STATE_WAIT_CONFIRM, text=text)

    keyboard = [
        [InlineKeyboardButton("✅ Отправить", callback_data="admin_broadcast_send")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="admin_broadcast")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_broadcast_cancel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 1) Служебное сообщение с кнопками
    await update.message.reply_text(
        "✅ *Предпросмотр рассылки (как увидят пользователи):*",
        reply_markup=reply_markup
    )

    # 2) Само превью в “боевом” виде (HTML)
    try:
        await update.message.reply_text(
            text,
            disable_web_page_preview=True
        )
    except Exception:
        # Если HTML битый — показываем понятную ошибку и оставляем кнопку "Изменить"
        await update.message.reply_text(
            "❌ Не смог отрендерить HTML (ошибка разметки).\n"
            "Проверь теги (<b>, <i>, <a href=...>) и попробуй ещё раз.",
        )
        # Возвращаем в режим ввода текста, чтобы админ мог сразу поправить
        _set_state(context, STATE_WAIT_TEXT)



async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Рассылаем всем пользователям из bot_users.chat_id."""
    text = context.user_data.get(TEXT_KEY)
    if not text:
        await update.callback_query.edit_message_text("❌ Текст рассылки не найден. Начни заново.")
        _clear_state(context)
        return

    # Сообщение о старте
    await update.callback_query.edit_message_text(
        "🚀 Рассылка запущена…"
    )

    sent = 0
    failed = 0

    # Достаём chat_id получателей
    with Session() as session:
        rows = session.execute(
            select(BotUser.chat_id).where(BotUser.chat_id.isnot(None))
        ).all()

    chat_ids = [r[0] for r in rows if r[0] is not None]

    # Ограничим скорость, чтобы не упереться в лимиты Telegram
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
    chat_id=chat_id,
    text=text,
    disable_web_page_preview=True
)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("Broadcast send failed to %s: %s", chat_id, e)
        await asyncio.sleep(0.05)

    _clear_state(context)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "📣 *Рассылка завершена*\n\n"
            f"✅ Отправлено: `{sent}`\n"
            f"⚠️ Ошибок: `{failed}`\n"
        )
    )


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(context)
    if update.callback_query:
        await update.callback_query.edit_message_text("❌ Рассылка отменена.")
    else:
        await update.message.reply_text("❌ Рассылка отменена.")
