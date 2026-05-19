import html
import random

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from db import (get_account_details, unassign_proxy_for_account,
                update_account_proxy_settings, upsert_proxy_for_account)
from userbot import reinitialize_telethon_client

from ..bot_instance import bot, dp
from ..keyboards import main_menu_keyboard
from ..states import AccountManagementStates
from ..utils import user_is_allowed


@dp.callback_query(F.data.startswith("proxy_manage_for_account:"))
async def cb_manage_proxy_for_account(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    await state.update_data(current_managing_account_id=account_id)
    acc_details = get_account_details(account_id)

    if not acc_details:
        await callback.message.edit_text("Аккаунт не найден.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К списку аккаунтов",
                                                                   callback_data="account_list")]
                                         ]))
        return

    text = f"Управление прокси для аккаунта: <b>{html.escape(acc_details['label'] or acc_details['session_name'])}</b> (ID: {account_id})\n\n"
    current_proxy_text = "Прокси не установлен."
    if acc_details['proxy_ip'] and acc_details['proxy_port']:
        current_proxy_text = f"Тип: {acc_details['proxy_type'] or 'socks5'}\n" \
                             f"IP: {acc_details['proxy_ip']}\n" \
                             f"Порт: {acc_details['proxy_port']}\n" \
                             f"Логин: {acc_details['proxy_username'] or '-'}\n" \
                             f"Пароль: {'********' if acc_details['proxy_password'] else '-'}"
    text += f"Текущие настройки:\n<pre>{html.escape(current_proxy_text)}</pre>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить/Добавить прокси", callback_data=f"edit_proxy_start:{account_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить прокси", callback_data=f"delete_proxy_confirm:{account_id}")],
        [InlineKeyboardButton(text="🚦 Перезапустить с текущим прокси", callback_data=f"check_proxy:{account_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к аккаунту", callback_data=f"manage_account:{account_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AccountManagementStates.ManagingProxy)
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_proxy_confirm:"), AccountManagementStates.ManagingProxy)
async def cb_delete_proxy_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Да, удалить прокси", callback_data=f"execute_delete_proxy:{account_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"proxy_manage_for_account:{account_id}")]
    ])
    await callback.message.edit_text(
        f"Вы уверены, что хотите удалить прокси для аккаунта ID {account_id}?\n"
        "Аккаунт будет перезапущен без прокси.", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("execute_delete_proxy:"), AccountManagementStates.ManagingProxy)
async def cb_execute_delete_proxy(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    unassign_proxy_for_account(account_id)

    proxy_details_to_set = {
        'proxy_type': None, 'proxy_ip': None, 'proxy_port': None,
        'proxy_username': None, 'proxy_password': None
    }
    update_success = update_account_proxy_settings(account_id, proxy_details_to_set)
    if update_success:
        upsert_proxy_for_account(account_id, proxy_details_to_set)

    if update_success:
        await callback.message.edit_text(
            f"Прокси для аккаунта ID {account_id} удален из БД. Перезапускаю клиент...")
        reinit_success, reinit_message = await reinitialize_telethon_client(account_id)
        if reinit_success:
            await callback.message.answer(f"✅ {reinit_message}",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                    callback_data=f"proxy_manage_for_account:{account_id}")]
                                          ]))
        else:
            await callback.message.answer(f"⚠️ {reinit_message}",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                    callback_data=f"proxy_manage_for_account:{account_id}")]
                                          ]))
    else:
        await callback.message.edit_text("Не удалось удалить прокси из БД.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    await callback.answer()
    await state.set_state(AccountManagementStates.ShowingAccountMenu)


@dp.callback_query(F.data.startswith("check_proxy:"), AccountManagementStates.ManagingProxy)
async def cb_check_proxy(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    await callback.message.edit_text(
        f"Начинаю проверку/перезапуск клиента ID {account_id} с текущими настройками прокси...")
    reinit_success, reinit_message = await reinitialize_telethon_client(account_id)

    if reinit_success:
        await callback.message.edit_text(f"✅ Проверка/перезапуск успешна: {reinit_message}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    else:
        await callback.message.edit_text(f"⚠️ Проверка/перезапуск не удалась: {reinit_message}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    await callback.answer()
    await state.set_state(AccountManagementStates.ShowingAccountMenu)


@dp.callback_query(F.data.startswith("edit_proxy_start:"), AccountManagementStates.ManagingProxy)
async def cb_edit_proxy_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)

    await callback.message.edit_text(
        f"Изменение прокси для аккаунта ID {account_id}.\n"
        "Введите тип прокси (socks5, socks4, http). \nОтправьте /skip или 'пропустить', если не знаете (будет socks5).")
    await state.set_state(AccountManagementStates.WaitingForProxyTypeUpdate)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProxyTypeUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_type_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    proxy_type_input = message.text.strip().lower()

    if proxy_type_input in ["/skip", "пропустить"]:
        await state.update_data(proxy_type_update='socks5')
        await message.answer("Тип прокси 'socks5'. Введите IP-адрес прокси:")
    elif proxy_type_input in ['socks5', 'socks4', 'http']:
        await state.update_data(proxy_type_update=proxy_type_input)
        await message.answer(f"Тип прокси '{proxy_type_input}'. Введите IP-адрес прокси:")
    else:
        await message.answer("Неверный тип. Допустимые: socks5, socks4, http. Или /skip, 'пропустить'.")
        return
    await state.set_state(AccountManagementStates.WaitingForProxyIPUpdate)


@dp.message(AccountManagementStates.WaitingForProxyIPUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_ip_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await state.update_data(proxy_ip_update=message.text.strip())
    await message.answer("Введите порт прокси (число):")
    await state.set_state(AccountManagementStates.WaitingForProxyPortUpdate)


@dp.message(AccountManagementStates.WaitingForProxyPortUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_port_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    if not message.text.strip().isdigit():
        await message.answer("Порт должен быть числом. Попробуйте снова.")
        return
    await state.update_data(proxy_port_update=int(message.text.strip()))
    await message.answer("Введите имя пользователя для прокси (отправьте /skip или 'пропустить', если его нет):")
    await state.set_state(AccountManagementStates.WaitingForProxyUsernameUpdate)


@dp.message(AccountManagementStates.WaitingForProxyUsernameUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_username_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    username_input = message.text.strip()
    if username_input.lower() in ["/skip", "пропустить"]:
        await state.update_data(proxy_username_update=None)
    else:
        await state.update_data(proxy_username_update=username_input)
    await message.answer("Введите пароль для прокси (отправьте /skip или 'пропустить', если его нет):")
    await state.set_state(AccountManagementStates.WaitingForProxyPasswordUpdate)


@dp.message(AccountManagementStates.WaitingForProxyPasswordUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_password_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    password_input = message.text.strip()
    if password_input.lower() in ["/skip", "пропустить"]:
        await state.update_data(proxy_password_update=None)
    else:
        await state.update_data(proxy_password_update=password_input)

    fsm_data = await state.get_data()
    account_id = fsm_data.get("current_managing_account_id")
    if not account_id:
        await message.answer("Ошибка: ID аккаунта не найден в состоянии. Попробуйте снова.",
                             reply_markup=main_menu_keyboard())
        await state.clear()
        return

    unassign_proxy_for_account(account_id)

    proxy_details_to_set = {
        'proxy_type': fsm_data.get('proxy_type_update'),
        'proxy_ip': fsm_data.get('proxy_ip_update'),
        'proxy_port': fsm_data.get('proxy_port_update'),
        'proxy_username': fsm_data.get('proxy_username_update'),
        'proxy_password': fsm_data.get('proxy_password_update')
    }

    update_success = update_account_proxy_settings(account_id, proxy_details_to_set)

    if update_success:
        await message.answer(f"Настройки прокси для аккаунта ID {account_id} обновлены в БД. Перезапускаю клиент...")
        reinit_success, reinit_message = await reinitialize_telethon_client(account_id)
        if reinit_success:
            await message.answer(f"✅ {reinit_message}")
        else:
            await message.answer(f"⚠️ {reinit_message}")
    else:
        await message.answer(f"Не удалось обновить настройки прокси для аккаунта ID {account_id} в БД.")

    await state.set_state(AccountManagementStates.ShowingAccountMenu)
    mock_callback_message = await message.answer("Загрузка меню...")
    mock_callback = CallbackQuery(id=str(random.randint(1, 100000)), from_user=message.from_user,
                                  chat_instance=message.chat.id, message=mock_callback_message,
                                  data=f"proxy_manage_for_account:{account_id}")
    await cb_manage_proxy_for_account(mock_callback, state)
    try:
        await bot.delete_message(chat_id=mock_callback_message.chat.id, message_id=mock_callback_message.message_id)
    except:
        pass

