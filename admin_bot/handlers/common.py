# C:\Users\27030\Desktop\TEMP\admin_bot\handlers\common.py
from aiogram import F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..bot_instance import dp
from ..utils import user_is_allowed
from ..keyboards import main_menu_keyboard
from db import get_config_value

from ..handlers.ai_config import ai_config_menu_keyboard
from ..handlers.analytics import analytics_menu_keyboard
from ..handlers.reporting_settings import reporting_settings_menu_keyboard
from ..handlers.chat_categories_settings import chat_categories_main_menu_keyboard
from ..handlers.general_settings import general_settings_menu_keyboard


@dp.message(Command("start"), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав для работы с этим ботом.")
        return
    await state.clear()
    await message.answer("Привет! Это главное меню:", reply_markup=main_menu_keyboard())


@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("\u0423 \u0432\u0430\u0441 \u043d\u0435\u0442 \u043f\u0440\u0430\u0432.")
        return
    await state.clear()
    await message.answer("\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e. \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e:", reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "cancel_action", StateFilter("*"))
async def cb_cancel_action(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("\u041d\u0435\u0442 \u043f\u0440\u0430\u0432.")
        return
    await state.clear()
    try:
        await callback.message.edit_text("\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e. \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e:", reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer("\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e. \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e:", reply_markup=main_menu_keyboard())
    await callback.answer("\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.")


@dp.callback_query(F.data == "back_to_main_menu")
async def cb_back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    try:
        await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.message(Command("accounts"))
async def cmd_accounts(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="account_add")],
        [InlineKeyboardButton(text="❌ Удалить аккаунт", callback_data="account_remove_info")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="account_list")],
        [InlineKeyboardButton(text="💬 Автоответчик ЛС", callback_data="auto_responder_menu")],
        [InlineKeyboardButton(text="🚀 Массовое обновление профилей", callback_data="mass_profile_update")],
        [InlineKeyboardButton(text="🚦 Проверить все сессии", callback_data="check_all_sessions")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await message.answer("⚙️ <b>Настройки аккаунтов</b>:", reply_markup=kb)


@dp.message(Command("groups"))
async def cmd_groups(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить группу(ы)", callback_data="add_group_options")],
        [InlineKeyboardButton(text="❌ Удалить группу (отписаться)", callback_data="remove_single_group_start")],
        [InlineKeyboardButton(text="🗑️ Удалить все группы", callback_data="delete_all_groups_confirm")],
        [InlineKeyboardButton(text="📜 Посмотреть список групп", callback_data="list_groups")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await message.answer("👥 <b>Управление группами</b>:", reply_markup=kb)


@dp.message(Command("channels"))
async def cmd_channels(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал(ы)", callback_data="add_channel_options")],
        [InlineKeyboardButton(text="❌ Удалить канал", callback_data="channel_remove_info")],
        [InlineKeyboardButton(text="🗑️ Удалить все каналы", callback_data="delete_all_channels_confirm")],
        [InlineKeyboardButton(text="📜 Список каналов", callback_data="channel_list")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await message.answer("📺 <b>Каналы</b>:", reply_markup=kb)


@dp.message(Command("responses"))
async def cmd_responses(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer("🔑 Введите ключевое слово (например, привет, заказ, помощь, впн). Регистр не важен.")
    await state.set_state("waiting_for_keyword")


@dp.message(Command("ai_settings"))
async def cmd_ai_settings(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer("🤖 <b>Настройки AI</b>\nВыберите параметр для изменения:",
                         reply_markup=ai_config_menu_keyboard())


@dp.message(Command("analytics"))
async def cmd_analytics(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer("📊 <b>Аналитика и Отчеты</b>\nВыберите период или действие:",
                         reply_markup=analytics_menu_keyboard())


@dp.message(Command("regular_comment"))
async def cmd_regular_comment(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    enabled = get_config_value("REGULAR_COMMENT_ENABLED", "0")
    icon = "🟢" if enabled == "1" else "🔴"
    interval_str = get_config_value("REGULAR_COMMENT_INTERVAL", "60")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Автокоммент: {icon}", callback_data="toggle_regular_comment")],
        [InlineKeyboardButton(text=f"Интервал: {interval_str} мин", callback_data="set_regular_comment_interval")],
        [InlineKeyboardButton(text="Отправить сейчас", callback_data="send_regular_comment_now_btn")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await message.answer(
        "<b>Регулярный комментарий</b>\n1) Автокоммент — периодически отправлять во все группы.\n2) Интервал — через сколько минут повторять.\n3) Отправить сейчас — сразу отправить во все группы.\n",
        reply_markup=kb
    )


@dp.message(Command("reporting"))
async def cmd_reporting(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer(
        "⚙️ <b>Настройки Отчетов</b>\nЗдесь вы можете настроить отправку отчетов о действиях бота.",
        reply_markup=reporting_settings_menu_keyboard()
    )


@dp.message(Command("chat_categories"))
async def cmd_chat_categories(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer(
        "🗂️ <b>Управление категориями чатов</b>\nВыберите категорию для настройки:",
        reply_markup=chat_categories_main_menu_keyboard(page=1)
    )


@dp.message(Command("general_settings"))
async def cmd_general_settings(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    await state.clear()
    await message.answer(
        "🛠️ <b>Общие настройки</b>\nЗдесь вы можете настроить общие параметры бота.",
        reply_markup=general_settings_menu_keyboard()
    )
