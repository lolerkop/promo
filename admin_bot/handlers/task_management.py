# admin_bot/handlers/task_management.py

import asyncio
import logging
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from ..bot_instance import dp
from ..utils import user_is_allowed
from shared import active_background_tasks

router = Router()


@dp.callback_query(F.data == "manage_tasks")
async def cb_manage_tasks(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    kb_buttons = []
    text = "📌 <b>Активные фоновые задачи:</b>\n\n"
    if not active_background_tasks:
        text += "Нет запущенных задач."
    else:
        for task_id, task_info in active_background_tasks.items():
            kb_buttons.append([
                InlineKeyboardButton(
                    text=f"❌ {task_info['description']} (ID: {task_id})",
                    callback_data=f"cancel_task:{task_id}"
                )
            ])

    kb_buttons.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="manage_tasks")])
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
    await callback.answer()


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Нет прав.")
        return

    kb_buttons = []
    text = "📌 <b>Активные фоновые задачи:</b>\n\n"
    if not active_background_tasks:
        text += "Нет запущенных задач."
    else:
        for task_id, task_info in active_background_tasks.items():
            kb_buttons.append([
                InlineKeyboardButton(
                    text=f"❌ {task_info['description']} (ID: {task_id})",
                    callback_data=f"cancel_task:{task_id}"
                )
            ])

    kb_buttons.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="manage_tasks")])
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))


@dp.callback_query(F.data.startswith("cancel_task:"))
async def cb_cancel_task(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    task_id = callback.data.split(":")[1]

    if task_id in active_background_tasks:
        task_info = active_background_tasks[task_id]
        task_info["task"].cancel()

        try:
            await task_info["task"]
        except asyncio.CancelledError:
            logging.info(
                f"Task {task_id} ('{task_info['description']}') was successfully cancelled by admin {callback.from_user.id}.")
            await callback.answer(f"Задача '{task_info['description']}' отменена.", show_alert=True)
    else:
        await callback.answer("Задача не найдена или уже завершена.", show_alert=True)

    await cb_manage_tasks(callback)
