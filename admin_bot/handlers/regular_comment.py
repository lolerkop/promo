import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..bot_instance import bot
from ..utils import user_is_allowed
from ..keyboards import main_menu_keyboard
from db import get_config_value, set_config_value


def _get_main_app_schedule_regular_comment_job_func():
    """Безопасно импортирует функцию планировщика из main_app."""
    from main_app import main_app_schedule_regular_comment_job
    return main_app_schedule_regular_comment_job


async def _call_main_app_send_regular_comment_now_func(initiated_by_admin: bool = True):
    """Безопасно импортирует и вызывает основную функцию отправки комментария."""
    from main_app import main_app_send_regular_comment_now
    return await main_app_send_regular_comment_now(initiated_by_admin=initiated_by_admin)

# --- Создаем Router ---
router = Router()

@router.callback_query(F.data == "regular_comment_menu")
async def cb_regular_comment_menu(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    enabled = get_config_value("REGULAR_COMMENT_ENABLED", "0")
    icon = "🟢" if enabled == "1" else "🔴"
    interval_str = get_config_value("REGULAR_COMMENT_INTERVAL", "60")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Автокоммент: {icon}", callback_data="toggle_regular_comment")],
        [InlineKeyboardButton(text=f"Интервал: {interval_str} мин", callback_data="set_regular_comment_interval")],
        [InlineKeyboardButton(text="Отправить сейчас", callback_data="send_regular_comment_now_btn")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text(
        "<b>Регулярный комментарий</b>\nНастройки для автокомментария в ОБЩИЕ группы (не из категорий).\n1) Автокоммент — периодически отправлять во все группы.\n2) Интервал — через сколько минут повторять.\n3) Отправить сейчас — сразу отправить во все группы.\n",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_regular_comment")
async def cb_toggle_regular_comment(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    enabled = get_config_value("REGULAR_COMMENT_ENABLED", "0")
    new_enabled = "0" if enabled == "1" else "1"
    set_config_value("REGULAR_COMMENT_ENABLED", new_enabled)

    # Перезапускаем задачу в планировщике с новыми настройками
    try:
        schedule_func = _get_main_app_schedule_regular_comment_job_func()
        schedule_func()
    except ImportError:
        logging.critical("Не удалось импортировать main_app_schedule_regular_comment_job для обновления планировщика!")
        await callback.message.answer("⚠️ Ошибка! Не удалось обновить задачу в планировщике. Проверьте логи.")


    await cb_regular_comment_menu(callback)


@router.callback_query(F.data == "set_regular_comment_interval")
async def cb_set_regular_comment_interval(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await callback.message.answer("Введите новый интервал (в минутах):")
    await state.set_state("waiting_regular_comment_interval")
    await callback.answer()


@router.message(StateFilter("waiting_regular_comment_interval"))
async def handle_interval_input(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Нет прав.")
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Нужно число (минуты). Попробуйте ещё раз.")
        return
    interval_int = max(int(message.text.strip()), 1)
    set_config_value("REGULAR_COMMENT_INTERVAL", str(interval_int))

    # Перезапускаем задачу в планировщике с новым интервалом
    try:
        schedule_func = _get_main_app_schedule_regular_comment_job_func()
        schedule_func()
    except ImportError:
        logging.critical("Не удалось импортировать main_app_schedule_regular_comment_job для обновления планировщика!")
        await message.answer("⚠️ Ошибка! Не удалось обновить задачу в планировщике. Проверьте логи.")


    await message.answer(f"Интервал установлен: {interval_int} мин.", reply_markup=main_menu_keyboard())
    await state.clear()


async def _send_comment_now_and_notify_admin(chat_id: int, user_id: int):
    """
    Вызывает основную логику отправки и уведомляет админа о результате.
    """
    try:
        ok = await _call_main_app_send_regular_comment_now_func(initiated_by_admin=True)
        if ok:
            await bot.send_message(chat_id, "✅ Команда на отправку регулярного комментария выполнена. Смотрите детали в отчетах/логах.",
                                   reply_markup=main_menu_keyboard())
        else:
            await bot.send_message(chat_id,
                                   "❌ Не удалось выполнить команду на отправку регулярного комментария. Возможные причины: нет активных аккаунтов, нет включенных групп, AI не смог сгенерировать текст. Смотрите детали в отчетах/логах.",
                                   reply_markup=main_menu_keyboard())
    except Exception as e:
        logging.error(f"Критическая ошибка при вызове _send_comment_now_and_notify_admin: {e}", exc_info=True)
        await bot.send_message(chat_id, f"💥 Произошла критическая ошибка при запуске отправки: {e}")


@router.callback_query(F.data == "send_regular_comment_now_btn")
async def cb_send_regular_comment_now_btn(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await callback.answer("Запускаю отправку регулярного комментария...", show_alert=False)
    # Запускаем как фоновую задачу, чтобы не блокировать бота
    asyncio.create_task(_send_comment_now_and_notify_admin(callback.message.chat.id, callback.from_user.id))