# settings_module.py
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from database import Session
from user_registry import BotUser
import locations

logger = logging.getLogger(__name__)


def can_handle_callback(data: str) -> bool:
    return bool(data) and (data.startswith('settings_') or data.startswith('sf_') or data.startswith('notify_'))


def _parse_trigger_id(data: str) -> int:
    try:
        last = (data or '').split('_')[-1]
        return int(last) if last.isdigit() else 0
    except Exception:
        return 0


async def _safe_edit(query, text: str, reply_markup=None, parse_mode: str | None = None) -> None:
    try:
        # Markdown/HTML отключены: всегда plain text.
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=None)
    except BadRequest as e:
        if 'Message is not modified' in str(e):
            return
        raise


async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    text = '⚙️ Настройки\n\nВыберите раздел:'
    trigger_id = context.user_data.get('settings_trigger_msg_id') or 0

    keyboard = [
        [InlineKeyboardButton('👤 Мой профиль', callback_data=f'settings_profile_{trigger_id}')],
        [InlineKeyboardButton('🔔 Уведомления', callback_data=f'settings_notifications_{trigger_id}')],
        [InlineKeyboardButton('🔎 Фильтр поиска', callback_data=f'settings_search_filter_{trigger_id}')],
        [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await _safe_edit(update.callback_query, text, reply_markup=reply_markup)
    else:
        msg = await update.effective_chat.send_message(text=text, reply_markup=reply_markup)
        context.user_data['settings_msg_id'] = msg.message_id
        context.user_data['settings_trigger_msg_id'] = msg.message_id


async def show_notifications_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = True) -> None:
    user = update.effective_user
    if not user:
        return
    trigger_id = context.user_data.get('settings_trigger_msg_id') or 0

    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            bu = BotUser(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=bool(getattr(user, 'is_bot', False)),
                chat_id=update.effective_chat.id if update.effective_chat else None,
                created_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
            )
            session.add(bu)
            session.commit()
            session.refresh(bu)

        enabled = bool(getattr(bu, 'trips_notify_enabled', False))

    status = '✅ Включены' if enabled else '⛔ Выключены'
    text = (
        '🔔 *Уведомления*\n' + ('═' * 25) + '\n\n'
        f'*Статус:* {status}\n\n'
        'Когда уведомления включены, бот сообщит о *новых поездках*.\n'
        'Если у вас включён *фильтр поиска*, уведомления будут приходить *только по фильтру*.\n'
    )

    toggle_title = '🔕 Выключить уведомления' if enabled else '🔔 Включить уведомления'
    keyboard = [
        [InlineKeyboardButton(toggle_title, callback_data=f'notify_toggle_{trigger_id}')],
        [InlineKeyboardButton('🔙 Назад в настройки', callback_data=f'settings_back_{trigger_id}')],
        [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await _safe_edit(update.callback_query, text, reply_markup=reply_markup)
    else:
        await update.effective_chat.send_message(text=text, reply_markup=reply_markup)


async def show_search_filter_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = True) -> None:
    user = update.effective_user
    if not user:
        return

    trigger_id = context.user_data.get('settings_trigger_msg_id') or 0

    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            bu = BotUser(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=bool(getattr(user, 'is_bot', False)),
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

        enabled = bool(getattr(bu, 'search_filter_enabled', False))
        dep = getattr(bu, 'search_filter_departure', None) or '—'
        dest = getattr(bu, 'search_filter_destination', None) or '—'

    status = '✅ Включён' if enabled else '⛔ Выключен'
    text = (
        '🔎 *Фильтр поиска*\n' + ('═' * 25) + '\n\n'
        f'*Статус:* {status}\n'
        f'📍 *Откуда:* {dep}\n'
        f'🎯 *Куда:* {dest}\n\n'
        'Когда фильтр включён, поиск по дате будет показывать только поездки,\n'
        'которые совпадают с выбранными пунктами (откуда/куда).'
    )

    toggle_title = '🔴 Выключить фильтр' if enabled else '🟢 Включить фильтр'

    keyboard = [
        [InlineKeyboardButton(toggle_title, callback_data=f'sf_toggle_{trigger_id}')],
        [InlineKeyboardButton('✏️ Задать «Откуда»', callback_data=f'sf_set_dep_{trigger_id}')],
        [InlineKeyboardButton('✏️ Задать «Куда»', callback_data=f'sf_set_dest_{trigger_id}')],
        [InlineKeyboardButton('🧹 Сбросить маршрут', callback_data=f'sf_clear_{trigger_id}')],
        [InlineKeyboardButton('🔙 Назад в настройки', callback_data=f'settings_back_{trigger_id}')],
        [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit and update.callback_query:
        await _safe_edit(update.callback_query, text, reply_markup=reply_markup)
    else:
        await update.effective_chat.send_message(text=text, reply_markup=reply_markup)


async def _edit_search_filter_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, text: str, reply_markup) -> None:
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if 'Message is not modified' in str(e):
            return
        raise
    except Exception:
        # If message disappeared, ignore
        return


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    if not query:
        return False

    data = query.data or ''
    if not can_handle_callback(data):
        return False

    try:
        await query.answer()
    except Exception:
        pass

    trigger_id = _parse_trigger_id(data)

    # close
    if data.startswith('settings_close_'):
        try:
            await query.delete_message()
        except Exception:
            pass
        return True

    # back
    if data.startswith('settings_back_'):
        await show_settings_menu(update, context, edit=True)
        return True

    # notifications screen
    if data.startswith('settings_notifications_'):
        await show_notifications_settings(update, context, edit=True)
        return True

    if data.startswith('notify_toggle_'):
        await _toggle_notifications(update)
        await show_notifications_settings(update, context, edit=True)
        return True

    # filter screen
    if data.startswith('settings_search_filter_'):
        await show_search_filter_settings(update, context, edit=True)
        return True

    if data.startswith('sf_toggle_'):
        await _toggle_filter(update)
        await show_search_filter_settings(update, context, edit=True)
        return True

    if data.startswith('sf_set_dep_'):
        context.user_data['settings_filter_wait'] = 'departure'
        chat_id = update.effective_chat.id if update.effective_chat else None
        msg_id = context.user_data.get('settings_msg_id')
        if chat_id and msg_id:
            await _edit_search_filter_message(
                context, chat_id, msg_id,
                '✏️ Введите пункт *отправления* (можно первые 2–3 буквы):',
                InlineKeyboardMarkup([
                    [InlineKeyboardButton('🔙 Назад', callback_data=f'settings_search_filter_{trigger_id}')],
                    [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
                ])
            )
        return True

    if data.startswith('sf_set_dest_'):
        context.user_data['settings_filter_wait'] = 'destination'
        chat_id = update.effective_chat.id if update.effective_chat else None
        msg_id = context.user_data.get('settings_msg_id')
        if chat_id and msg_id:
            await _edit_search_filter_message(
                context, chat_id, msg_id,
                '✏️ Введите пункт *назначения* (можно первые 2–3 буквы):',
                InlineKeyboardMarkup([
                    [InlineKeyboardButton('🔙 Назад', callback_data=f'settings_search_filter_{trigger_id}')],
                    [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
                ])
            )
        return True

    if data.startswith('sf_clear_'):
        await _clear_filter(update)
        await show_search_filter_settings(update, context, edit=True)
        return True

    if data.startswith('sf_show_allowed_'):
        await _show_allowed_locations(update, context, trigger_id)
        return True

    if data.startswith('sf_pick_'):
        # sf_pick_{field}_{idx}_{trigger}
        parts = data.split('_')
        if len(parts) >= 5:
            field = parts[2]
            idx = int(parts[3])
            suggestions = (context.user_data.get('sf_suggestions') or {}).get(field) or []
            if 0 <= idx < len(suggestions):
                await _save_filter_value(update.effective_user.id, field, suggestions[idx])
        context.user_data.pop('settings_filter_wait', None)
        await show_search_filter_settings(update, context, edit=True)
        return True

    return False


async def _toggle_notifications(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            return
        cur = bool(getattr(bu, 'trips_notify_enabled', False))
        setattr(bu, 'trips_notify_enabled', (not cur))
        session.commit()


async def _toggle_filter(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            return
        cur = bool(getattr(bu, 'search_filter_enabled', False))
        setattr(bu, 'search_filter_enabled', (not cur))
        session.commit()


async def _clear_filter(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == user.id).one_or_none()
        if bu is None:
            return
        setattr(bu, 'search_filter_departure', None)
        setattr(bu, 'search_filter_destination', None)
        session.commit()


async def _show_allowed_locations(update: Update, context: ContextTypes.DEFAULT_TYPE, trigger_id: int) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    msg_id = context.user_data.get('settings_msg_id')
    if not chat_id or not msg_id:
        return

    # Telegram has 4096 char limit; keep it short
    items = locations.ALLOWED_LOCATIONS
    lines = [f'• {x}' for x in items[:200]]
    more = '' if len(items) <= 200 else f"\n\n…и ещё {len(items)-200} направлений."
    text = "*Доступные направления:*\n\n" + "\n".join(lines) + more

    await _edit_search_filter_message(
        context,
        chat_id,
        msg_id,
        text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton('🔙 Назад', callback_data=f'settings_search_filter_{trigger_id}')],
            [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
        ])
    )


async def _save_filter_value(telegram_id: int, field: str, value: str) -> None:
    with Session() as session:
        bu = session.query(BotUser).filter(BotUser.telegram_id == telegram_id).one_or_none()
        if bu is None:
            return
        if field == 'departure':
            setattr(bu, 'search_filter_departure', value)
        else:
            setattr(bu, 'search_filter_destination', value)
        session.commit()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # active only while waiting for filter field
    if context.user_data.get('settings_filter_wait') not in ('departure', 'destination'):
        return False

    chat_id = update.effective_chat.id if update.effective_chat else None
    msg_id = context.user_data.get('settings_msg_id')
    trigger_id = context.user_data.get('settings_trigger_msg_id') or 0
    if not chat_id or not msg_id:
        return True

    field = context.user_data.get('settings_filter_wait')
    raw_value = (update.message.text or '').strip()

    # clean chat: delete user's input
    try:
        await update.message.delete()
    except Exception:
        pass

    # 1) exact match (case-insensitive)
    exact = locations.canonical(raw_value)

    # 2) prefix/substring or fuzzy suggestions
    suggestions = []
    fuzzy_used = False
    if not exact:
        suggestions = locations.suggestions(raw_value, limit=12)
        if suggestions and (not any(locations.norm(x).startswith(locations.norm(raw_value)) or locations.norm(raw_value) in locations.norm(x) for x in suggestions)):
            fuzzy_used = True
        # If suggestions came from fuzzy fallback
        if not suggestions:
            fuzzy_used = True

    # No matches
    if not exact and not suggestions:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('📍 Доступные направления', callback_data=f'sf_show_allowed_{trigger_id}')],
            [InlineKeyboardButton('🔙 Назад', callback_data=f'settings_search_filter_{trigger_id}')],
            [InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')],
        ])
        await _edit_search_filter_message(
            context, chat_id, msg_id,
            '❌ *Неизвестное направление.*\n\n'
            'Попробуйте ввести по-другому (например, первые 2–3 буквы)\n'
            'или нажмите *«Доступные направления»*.',
            kb
        )
        return True

    # Multiple suggestions
    if not exact and suggestions:
        suggestions = suggestions[:12]
        context.user_data['sf_suggestions'] = context.user_data.get('sf_suggestions', {})
        context.user_data['sf_suggestions'][field] = suggestions

        title = '📍 Выберите «Откуда»' if field == 'departure' else '🎯 Выберите «Куда»'
        hint = '\n\n💡 Похоже на опечатку — выберите правильный вариант:' if fuzzy_used else ''
        buttons = []
        for idx, x in enumerate(suggestions):
            buttons.append([InlineKeyboardButton(x, callback_data=f'sf_pick_{field}_{idx}_{trigger_id}')])

        buttons.append([InlineKeyboardButton('📍 Показать весь список', callback_data=f'sf_show_allowed_{trigger_id}')])
        buttons.append([InlineKeyboardButton('🔙 Назад', callback_data=f'settings_search_filter_{trigger_id}')])
        buttons.append([InlineKeyboardButton('✖️ Закрыть', callback_data=f'settings_close_{trigger_id}')])

        await _edit_search_filter_message(
            context, chat_id, msg_id,
            f"{title}\n\nВы ввели: *{raw_value}*{hint}",
            InlineKeyboardMarkup(buttons)
        )
        return True

    # exact: save
    save_value = exact or raw_value
    await _save_filter_value(update.effective_user.id, field, save_value)
    context.user_data.pop('settings_filter_wait', None)
    await show_search_filter_settings(update, context, edit=True)
    return True
