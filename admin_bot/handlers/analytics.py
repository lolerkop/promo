import logging
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..bot_instance import dp, bot
from ..utils import user_is_allowed
from ..keyboards import main_menu_keyboard
from db import get_analytics_summary, clear_analytics_logs, add_analytics_log


def analytics_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📈 Сводка за сегодня", callback_data="analytics_today")],
        [InlineKeyboardButton(text="📉 Сводка за вчера", callback_data="analytics_yesterday")],
        [InlineKeyboardButton(text="📊 Сводка за 7 дней", callback_data="analytics_7_days")],
        [InlineKeyboardButton(text="🗑️ Очистить логи аналитики", callback_data="analytics_clear_logs")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "analytics_menu")
async def cb_analytics_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text("📊 <b>Аналитика и Отчеты</b>\nВыберите период или действие:",  # type: ignore
                                     reply_markup=analytics_menu_keyboard())
    await callback.answer()


async def send_analytics_summary(callback_or_message, days_offset: int, period_name: str):
    now_utc = datetime.now(timezone.utc)
    end_date = now_utc - timedelta(days=days_offset)
    start_date = end_date - timedelta(days=(1 if days_offset < 7 else days_offset))  # For daily, or N days for period
    if period_name == "сегодня":
        start_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now_utc  # Current time for "today"
    elif period_name == "вчера":
        end_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)
        start_date = end_date - timedelta(days=1) + timedelta(microseconds=1)
    elif period_name == "7 дней":
        end_date = now_utc
        start_date = now_utc - timedelta(days=7)

    summary_data = get_analytics_summary(start_date, end_date)  # type: ignore

    if not summary_data:
        text = f"Нет данных аналитики за {period_name} ({start_date.strftime('%Y-%m-%d %H:%M')} - {end_date.strftime('%Y-%m-%d %H:%M')} UTC)."
    else:
        text_lines = [f"📊 <b>Сводка аналитики за {period_name}</b>"]
        text_lines.append(
            f"Период: {start_date.strftime('%Y-%m-%d %H:%M')} - {end_date.strftime('%Y-%m-%d %H:%M')} UTC\n")

        formatted_summary = {}
        for item in summary_data:
            action = item['action_type']
            status = "✅ Успех" if item['success'] else "❌ Ошибка"
            count = item['count']
            if action not in formatted_summary:
                formatted_summary[action] = {}
            formatted_summary[action][status] = count

        for action, statuses in formatted_summary.items():
            text_lines.append(f"<b>Действие: {action}</b>")
            for status, count in statuses.items():
                text_lines.append(f"  {status}: {count}")
            text_lines.append("")  # newline
        text = "\n".join(text_lines)

    if isinstance(callback_or_message, CallbackQuery):
        try:
            await callback_or_message.message.edit_text(text, reply_markup=analytics_menu_keyboard())  # type: ignore
        except Exception:  # If message is not modified or other error
            await callback_or_message.message.answer(text, reply_markup=analytics_menu_keyboard())  # type: ignore
        await callback_or_message.answer()
    else:  # For future use if called from a message context
        await callback_or_message.answer(text, reply_markup=analytics_menu_keyboard())


@dp.callback_query(F.data == "analytics_today")
async def cb_analytics_today(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return  # type: ignore
    await send_analytics_summary(callback, 0, "сегодня")


@dp.callback_query(F.data == "analytics_yesterday")
async def cb_analytics_yesterday(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return  # type: ignore
    await send_analytics_summary(callback, 1, "вчера")


@dp.callback_query(F.data == "analytics_7_days")
async def cb_analytics_7_days(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return  # type: ignore
    await send_analytics_summary(callback, 7, "7 дней")


@dp.callback_query(F.data == "analytics_clear_logs")
async def cb_analytics_clear_logs(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return

    # Confirmation keyboard
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, очистить логи", callback_data="analytics_confirm_clear")],
        [InlineKeyboardButton(text="❌ Нет, отмена", callback_data="analytics_menu")]
    ])
    await callback.message.edit_text("Вы уверены, что хотите очистить ВСЕ логи аналитики? Это действие необратимо.",
                                     reply_markup=kb)  # type: ignore
    await callback.answer()


@dp.callback_query(F.data == "analytics_confirm_clear")
async def cb_analytics_confirm_clear(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return

    success = clear_analytics_logs()  # type: ignore
    user_id = callback.from_user.id  # type: ignore

    if success:
        log_message = f"Логи аналитики очищены администратором ID: {user_id}"
        logging.info(log_message)
        add_analytics_log(action_type="analytics_cleared", details=f"Cleared by admin {user_id}",
                          success=True)  # type: ignore
        await callback.message.edit_text("Логи аналитики успешно очищены.",
                                         reply_markup=analytics_menu_keyboard())  # type: ignore
    else:
        log_message = f"Ошибка при очистке логов аналитики администратором ID: {user_id}"
        logging.error(log_message)
        add_analytics_log(action_type="analytics_clear_fail", details=f"Failed clear by admin {user_id}",
                          success=False)  # type: ignore
        await callback.message.edit_text("Произошла ошибка при очистке логов аналитики.",
                                         reply_markup=analytics_menu_keyboard())  # type: ignore
    await callback.answer()
