# main.py
import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler
import handlers
import admin_handlers
import user_registry
import booking_module

# --- sanitize all outgoing text (plain text UI; parse_mode disabled) ---
import re as _re
from telegram import Message as _TGMessage
from telegram import Bot as _TGBot

_HINT_WORDS = ("Подсказка", "Пример", "Например")

def _sanitize_plain_text(s: str) -> str:
    if s is None:
        return s
    s = str(s)

    # Remove common Markdown/MarkdownV2 markers that would show as literal characters.
    s = _re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = _re.sub(r"\*(.*?)\*", r"\1", s)
    s = _re.sub(r"_(.*?)_", r"\1", s)
    s = s.replace("`", "")

    # Drop hint/example lines globally (minimalistic UI).
    out_lines = []
    for ln in s.splitlines():
        t = ln.strip()
        if not t:
            out_lines.append(ln)
            continue
        if t.startswith("💡") or any(w in t for w in _HINT_WORDS):
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)

# Patch Message helpers (reply_text/edit_text)
_orig_reply_text = _TGMessage.reply_text
async def _reply_text_plain(self, text, *args, **kwargs):
    kwargs.pop("parse_mode", None)
    return await _orig_reply_text(self, _sanitize_plain_text(text), *args, **kwargs)
_TGMessage.reply_text = _reply_text_plain

_orig_edit_text = _TGMessage.edit_text
async def _edit_text_plain(self, text, *args, **kwargs):
    kwargs.pop("parse_mode", None)
    return await _orig_edit_text(self, _sanitize_plain_text(text), *args, **kwargs)
_TGMessage.edit_text = _edit_text_plain

# Patch Bot methods (covers context.bot.send_message, query.edit_message_text internals, etc.)
_orig_send_message = _TGBot.send_message
async def _send_message_plain(self, chat_id, text, *args, **kwargs):
    kwargs.pop("parse_mode", None)
    return await _orig_send_message(self, chat_id, _sanitize_plain_text(text), *args, **kwargs)
_TGBot.send_message = _send_message_plain

_orig_edit_message_text = _TGBot.edit_message_text
async def _edit_message_text_plain(self, text, *args, **kwargs):
    kwargs.pop("parse_mode", None)
    return await _orig_edit_message_text(self, _sanitize_plain_text(text), *args, **kwargs)
_TGBot.edit_message_text = _edit_message_text_plain



logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO'))
)
logger = logging.getLogger(__name__)

def main():
    """Запуск бота"""
    # Получаем токен из переменных окружения
    TOKEN = os.getenv('BOT_TOKEN')
    
    if not TOKEN:
        logger.error("BOT_TOKEN не найден в переменных окружения!")
        return
    
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    user_registry.register(application)

    # ========== Patch 2.1: авто-истечение PENDING бронирований ==========
    # Раз в минуту помечаем старые PENDING как EXPIRED и возвращаем места.
    try:
        application.job_queue.run_repeating(
            booking_module.expire_pending_bookings_job,
            interval=60,
            first=60,
            name="expire_pending_bookings",
        )
    except Exception as e:
        logger.error(f"Не удалось запустить задачу expire_pending_bookings: {e}")

    # ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
    
    # Создаем ConversationHandler для создания поездок
    trip_creation_handler = ConversationHandler(
        entry_points=[
            CommandHandler('new_trip', handlers.new_trip),
            MessageHandler(filters.Regex("^🚗 Создать поездку$"), handlers.new_trip)
        ],
        states={
            handlers.INPUT_DEPARTURE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_departure),
                CallbackQueryHandler(handlers.creation_pick_location, pattern=r"^tc_pick_(departure|destination)_\d+$"),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_DESTINATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_destination),
                CallbackQueryHandler(handlers.creation_pick_location, pattern=r"^tc_pick_(departure|destination)_\d+$"),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_DATE_SELECT: [
                CallbackQueryHandler(handlers.select_trip_date, pattern="^trip_date_(today|tomorrow|manual)$"),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_DATE_MANUAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_trip_date_manual),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_TIME: [
                CallbackQueryHandler(handlers.select_trip_time_choice, pattern="^trip_time_(slot_(morning|day|evening)|exact)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_trip_time),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_SEATS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_seats),
                CallbackQueryHandler(handlers.creation_pick_seats, pattern=r"^tc_seats_\d+$"),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
            handlers.INPUT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.input_price),
                CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
            ],
        },
        fallbacks=[
            CommandHandler('cancel', handlers.cancel_creation),
            MessageHandler(filters.Regex("^❌ Отмена$"), handlers.cancel_creation),
            CallbackQueryHandler(handlers.cancel_creation, pattern="^cancel_trip_creation$")
        ],
        per_message=False
    )
    
    # Добавляем основные обработчики
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("search", handlers.search_trips))
    application.add_handler(CommandHandler("my_trips", handlers.my_trips))
    application.add_handler(CommandHandler("my_bookings", handlers.my_bookings))
        
    application.add_handler(trip_creation_handler)
        
    # ========== АДМИН-ОБРАБОТЧИКИ ==========
    
    # Команды админки
    application.add_handler(CommandHandler("admin", admin_handlers.admin_panel))
    
    # Обработчик кнопок админки
    application.add_handler(CallbackQueryHandler(
        admin_handlers.admin_button_callback,
        pattern="^admin_"
    ))
    
    # ========== ОБЩИЕ ОБРАБОТЧИКИ ==========
    
    # Обработчик текстовых сообщений (для кнопок меню)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handlers.admin_text_router), group=-2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    
    # Обработчик нажатий на inline-кнопки (основные)
    application.add_handler(CallbackQueryHandler(handlers.button_callback))
    
    # Запуск бота
    logger.info("✅ Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()