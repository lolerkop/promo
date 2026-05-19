import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from db import get_config_value, set_config_value

from ..bot_instance import dp
from ..states import AccountSettingsStates
from ..utils import user_is_allowed


@dp.callback_query(F.data == "auto_responder_menu")
async def cb_auto_responder_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    current_message = get_config_value("dm_auto_reply_message", "Сообщение автоответчика не установлено.")
    is_enabled = get_config_value("dm_auto_reply_enabled", "False").lower() == 'true'
    status_text = "🟢 Включен" if is_enabled else "🔴 Выключен"

    text = f"<b>Автоответчик для личных сообщений</b>\n\n"
    text += f"Статус: {status_text}\n\n"
    text += f"Текущее сообщение:\n<pre>{html.escape(current_message if current_message else 'Не установлено')}</pre>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить сообщение", callback_data="set_dm_auto_reply_message")],
        [InlineKeyboardButton(text=f"{'Выключить' if is_enabled else 'Включить'}",
                              callback_data="toggle_dm_auto_reply")],
        [InlineKeyboardButton(text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "toggle_dm_auto_reply")
async def cb_toggle_dm_auto_reply(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    is_enabled = get_config_value("dm_auto_reply_enabled", "False").lower() == 'true'
    new_status_str = "False" if is_enabled else "True"
    set_config_value("dm_auto_reply_enabled", new_status_str)

    await cb_auto_responder_menu(callback, state)
    await callback.answer(f"Автоответчик {'выключен' if is_enabled else 'включен'}.")


@dp.callback_query(F.data == "set_dm_auto_reply_message")
async def cb_set_dm_auto_reply_message(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await callback.message.edit_text("Введите новое сообщение для автоответчика:")
    await state.set_state(AccountSettingsStates.WaitingForDMAutoReplyMessage)
    await callback.answer()


@dp.message(AccountSettingsStates.WaitingForDMAutoReplyMessage, F.text, ~F.text.startswith('/'))
async def process_dm_auto_reply_message(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return

    new_reply_message = message.text.strip() if message.text else ""
    set_config_value("dm_auto_reply_message", new_reply_message)

    await message.answer(f"Сообщение автоответчика установлено:\n<pre>{html.escape(new_reply_message)}</pre>",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ К настройкам автоответчика",
                                                   callback_data="auto_responder_menu")]
                         ]))
    await state.clear()

