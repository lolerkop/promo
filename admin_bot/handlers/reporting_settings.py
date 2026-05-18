import logging
import os
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ..bot_instance import dp, bot
from ..keyboards import cancel_action_keyboard
from ..reporting_utils import clear_auto_comment_report_file, get_auto_comment_report_file_path
from ..utils import user_is_allowed
from db import get_config_value, set_config_value


class ReportingStates(StatesGroup):
    WaitingForTargetID = State()


def reporting_settings_menu_keyboard() -> InlineKeyboardMarkup:
    enabled = get_config_value("reporting_enabled", "False").lower() == 'true'
    target_id = get_config_value("reporting_target_id", "")
    display_target_id = target_id if target_id else "Не задан"

    status_text = "🟢 Включены" if enabled else "🔴 Выключены"

    buttons = [
        [InlineKeyboardButton(text="Download auto-comment report", callback_data="download_auto_comment_report")],
        [InlineKeyboardButton(text="Clear auto-comment report", callback_data="clear_auto_comment_report_confirm")],
        [InlineKeyboardButton(text=f"Статус отчетов: {status_text}", callback_data="toggle_reporting")],
        [InlineKeyboardButton(text=f"ID для отчетов: {display_target_id}", callback_data="set_reporting_target_id")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "reporting_settings_menu")
async def cb_reporting_settings_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text(
        "⚙️ <b>Настройки Отчетов</b>\nЗдесь вы можете настроить отправку отчетов о действиях бота.",
        reply_markup=reporting_settings_menu_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "toggle_reporting")
async def cb_toggle_reporting(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    enabled = get_config_value("reporting_enabled", "False").lower() == 'true'
    new_status_str = "False" if enabled else "True"
    set_config_value("reporting_enabled", new_status_str)

    logging.info(f"Reporting status changed to: {new_status_str} by admin {callback.from_user.id}")
    await callback.message.edit_text(
        f"Статус отчетов изменен на: {'Включены' if not enabled else 'Выключены'}.",
        reply_markup=reporting_settings_menu_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "download_auto_comment_report")
async def cb_download_auto_comment_report(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return

    report_path = get_auto_comment_report_file_path()
    if not os.path.exists(report_path) or os.path.getsize(report_path) == 0:
        await callback.answer("Auto-comment report is empty.", show_alert=True)
        return

    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=FSInputFile(report_path, filename="auto_comment_reports.txt"),
        caption="Auto-comment report file."
    )
    await callback.answer()


@dp.callback_query(F.data == "clear_auto_comment_report_confirm")
async def cb_clear_auto_comment_report_confirm(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Yes, clear report", callback_data="clear_auto_comment_report_execute")],
        [InlineKeyboardButton(text="Cancel", callback_data="reporting_settings_menu")]
    ])
    await callback.message.edit_text(
        "Clear auto-comment report file? Old entries will be deleted, new comments will be logged from scratch.",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "clear_auto_comment_report_execute")
async def cb_clear_auto_comment_report_execute(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return

    clear_auto_comment_report_file()
    await callback.message.edit_text(
        "Auto-comment report file cleared.",
        reply_markup=reporting_settings_menu_keyboard()
    )
    await callback.answer("Report cleared.")


@dp.callback_query(F.data == "set_reporting_target_id")
async def cb_set_reporting_target_id(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    current_target_id = get_config_value("reporting_target_id", "")
    display_target_id = current_target_id if current_target_id else "Не задан"
    await callback.message.edit_text(
        f"Текущий ID для отчетов: <code>{display_target_id}</code>\n"
        "Введите новый ID канала (например, -100XXXXXXXXXX) или ID пользователя (например, XXXXXXXXXX).\n"
        "Убедитесь, что у бота есть права на отправку сообщений в указанный чат/канал, или что пользователь начал диалог с ботом.",
        parse_mode="HTML",
        reply_markup=cancel_action_keyboard()
    )
    await state.set_state(ReportingStates.WaitingForTargetID)
    await callback.answer()


@dp.message(ReportingStates.WaitingForTargetID, F.text, ~F.text.startswith('/'))
async def process_reporting_target_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Нет прав.")
        return

    target_id_input = message.text.strip() if message.text else ""

    if not target_id_input:
        set_config_value("reporting_target_id", "")  # Clear if empty
        logging.info(f"Reporting target ID cleared by admin {message.from_user.id}")
        await message.answer(
            "ID для отчетов очищен.",
            reply_markup=reporting_settings_menu_keyboard()
        )
        await state.clear()
        return

    try:
        int(target_id_input)
        set_config_value("reporting_target_id", target_id_input)
        logging.info(f"Reporting target ID set to: {target_id_input} by admin {message.from_user.id}")
        await message.answer(
            f"ID для отчетов установлен: <code>{target_id_input}</code>",
            reply_markup=reporting_settings_menu_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
    except ValueError:
        await message.answer("Неверный формат ID. ID должен быть числом (или пустым для очистки). Попробуйте снова.")
