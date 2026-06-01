import html
import logging
import os

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..bot_instance import bot, dp
from ..keyboards import cancel_action_keyboard
from ..reporting_utils import (
    clear_auto_comment_report_file,
    clear_trigger_reply_report_file,
    get_auto_comment_report_file_path,
    get_recent_trigger_reply_reports,
    get_trigger_reply_report_file_path,
)
from ..utils import user_is_allowed
from db import get_config_value, set_config_value


class ReportingStates(StatesGroup):
    WaitingForTargetID = State()


def reporting_settings_menu_keyboard() -> InlineKeyboardMarkup:
    enabled = get_config_value("reporting_enabled", "False").lower() == "true"
    target_id = get_config_value("reporting_target_id", "")
    display_target_id = target_id if target_id else "Не задан"
    status_text = "Включены" if enabled else "Выключены"

    buttons = [
        [InlineKeyboardButton(text="Скачать отчет автокомментов", callback_data="download_auto_comment_report")],
        [InlineKeyboardButton(text="Очистить отчет автокомментов", callback_data="clear_auto_comment_report_confirm")],
        [InlineKeyboardButton(text="Скачать отчет триггеров", callback_data="download_trigger_reply_report")],
        [InlineKeyboardButton(text="Последние 20 trigger-ответов", callback_data="show_recent_trigger_replies")],
        [InlineKeyboardButton(text="Очистить отчет триггеров", callback_data="clear_trigger_reply_report_confirm")],
        [InlineKeyboardButton(text=f"Статус отчетов: {status_text}", callback_data="toggle_reporting")],
        [InlineKeyboardButton(text=f"ID для отчетов: {display_target_id}", callback_data="set_reporting_target_id")],
        [InlineKeyboardButton(text="Назад в главное меню", callback_data="back_to_main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "reporting_settings_menu")
async def cb_reporting_settings_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text(
        "Настройки отчетов\n\n"
        "Здесь можно включить отправку live-отчетов и забрать файлы логов по автокомментам и trigger-ответам.",
        reply_markup=reporting_settings_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "toggle_reporting")
async def cb_toggle_reporting(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    enabled = get_config_value("reporting_enabled", "False").lower() == "true"
    new_status_str = "False" if enabled else "True"
    set_config_value("reporting_enabled", new_status_str)

    logging.info("Reporting status changed to: %s by admin %s", new_status_str, callback.from_user.id)
    await callback.message.edit_text(
        f"Статус live-отчетов изменен: {'Включены' if not enabled else 'Выключены'}.",
        reply_markup=reporting_settings_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "download_auto_comment_report")
async def cb_download_auto_comment_report(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    report_path = get_auto_comment_report_file_path()
    if not os.path.exists(report_path) or os.path.getsize(report_path) == 0:
        await callback.answer("Отчет автокомментов пуст.", show_alert=True)
        return

    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=FSInputFile(report_path, filename="auto_comment_reports.txt"),
        caption="Файл отчета по автокомментариям.",
    )
    await callback.answer()


@dp.callback_query(F.data == "download_trigger_reply_report")
async def cb_download_trigger_reply_report(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    report_path = get_trigger_reply_report_file_path()
    if not os.path.exists(report_path) or os.path.getsize(report_path) == 0:
        await callback.answer("Отчет trigger-ответов пуст.", show_alert=True)
        return

    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=FSInputFile(report_path, filename="trigger_reply_reports.txt"),
        caption="Файл отчета по trigger-ответам.",
    )
    await callback.answer()


@dp.callback_query(F.data == "show_recent_trigger_replies")
async def cb_show_recent_trigger_replies(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    reports = get_recent_trigger_reply_reports(limit=20)
    if not reports:
        await callback.answer("Отчет trigger-ответов пуст.", show_alert=True)
        return

    lines = ["<b>Последние 20 trigger-ответов</b>"]
    for index, item in enumerate(reports, start=1):
        reply_link = item.get("reply_link") or ""
        link_html = (
            f'<a href="{html.escape(reply_link, quote=True)}">ответ бота</a>'
            if reply_link.startswith("https://")
            else html.escape(reply_link or "ссылка недоступна")
        )
        lines.append(
            "\n".join([
                f"\n<b>{index}. {html.escape(item.get('timestamp') or '')}</b>",
                f"Чат: <code>{html.escape(item.get('chat') or item.get('chat_id') or 'N/A')}</code>",
                f"Триггер: <code>{html.escape(item.get('trigger') or 'trigger_reply')}</code>",
                f"Аккаунт: <code>{html.escape(item.get('account') or 'N/A')}</code>",
                f"Ссылка: {link_html}",
            ])
        )

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "clear_auto_comment_report_confirm")
async def cb_clear_auto_comment_report_confirm(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, очистить", callback_data="clear_auto_comment_report_execute")],
        [InlineKeyboardButton(text="Отмена", callback_data="reporting_settings_menu")],
    ])
    await callback.message.edit_text(
        "Очистить файл отчета автокомментов? Старые записи будут удалены.",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "clear_auto_comment_report_execute")
async def cb_clear_auto_comment_report_execute(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    clear_auto_comment_report_file()
    await callback.message.edit_text(
        "Отчет автокомментов очищен.",
        reply_markup=reporting_settings_menu_keyboard(),
    )
    await callback.answer("Очищено.")


@dp.callback_query(F.data == "clear_trigger_reply_report_confirm")
async def cb_clear_trigger_reply_report_confirm(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, очистить", callback_data="clear_trigger_reply_report_execute")],
        [InlineKeyboardButton(text="Отмена", callback_data="reporting_settings_menu")],
    ])
    await callback.message.edit_text(
        "Очистить файл отчета trigger-ответов? Старые записи будут удалены.",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "clear_trigger_reply_report_execute")
async def cb_clear_trigger_reply_report_execute(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    clear_trigger_reply_report_file()
    await callback.message.edit_text(
        "Отчет trigger-ответов очищен.",
        reply_markup=reporting_settings_menu_keyboard(),
    )
    await callback.answer("Очищено.")


@dp.callback_query(F.data == "set_reporting_target_id")
async def cb_set_reporting_target_id(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    current_target_id = get_config_value("reporting_target_id", "")
    display_target_id = current_target_id if current_target_id else "Не задан"
    await callback.message.edit_text(
        f"Текущий ID для live-отчетов: <code>{html.escape(display_target_id)}</code>\n"
        "Введите новый ID канала/чата или пользователя. Можно отправить пустое сообщение, чтобы очистить.",
        parse_mode="HTML",
        reply_markup=cancel_action_keyboard(),
    )
    await state.set_state(ReportingStates.WaitingForTargetID)
    await callback.answer()


@dp.message(ReportingStates.WaitingForTargetID, F.text, ~F.text.startswith("/"))
async def process_reporting_target_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Нет прав.")
        return

    target_id_input = message.text.strip() if message.text else ""

    if not target_id_input:
        set_config_value("reporting_target_id", "")
        logging.info("Reporting target ID cleared by admin %s", message.from_user.id)
        await message.answer(
            "ID для live-отчетов очищен.",
            reply_markup=reporting_settings_menu_keyboard(),
        )
        await state.clear()
        return

    try:
        int(target_id_input)
    except ValueError:
        await message.answer(
            "Неверный формат ID. ID должен быть числом. Попробуйте снова или нажмите отмену.",
            reply_markup=cancel_action_keyboard(),
        )
        return

    set_config_value("reporting_target_id", target_id_input)
    logging.info("Reporting target ID set to: %s by admin %s", target_id_input, message.from_user.id)
    await message.answer(
        f"ID для live-отчетов установлен: <code>{html.escape(target_id_input)}</code>",
        reply_markup=reporting_settings_menu_keyboard(),
        parse_mode="HTML",
    )
    await state.clear()
