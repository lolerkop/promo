import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from shared import active_background_tasks

from ..bot_instance import dp
from ..utils import user_is_allowed

router = Router()


def _task_progress_bar(done: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _format_task_stats(stats: dict) -> list[str]:
    if not stats:
        return []

    labels = {
        "channels_processed": "Каналов",
        "channels_synced": "Синхр.",
        "channels_skipped": "Пропущ.",
        "channels_failed": "Ошибок",
        "join_ok": "Вступл. OK",
        "join_failed": "Вступл. ошибок",
        "success": "OK",
        "failed": "Ошибок",
        "deleted": "Удалено",
    }
    lines = []
    for key, value in stats.items():
        label = labels.get(key, key)
        lines.append(f"{html.escape(str(label))}: <b>{html.escape(str(value))}</b>")
    return lines


def _format_task_block(task_id: str, task_info: dict) -> str:
    description = html.escape(str(task_info.get("description") or "Фоновая задача"))
    progress = task_info.get("progress") or {}
    total = int(progress.get("total") or 0)
    completed = int(progress.get("completed") or 0)
    percent = int((completed / total) * 100) if total else 0
    status = html.escape(str(progress.get("status") or "В работе"))
    current = html.escape(str(progress.get("current") or "-"))

    lines = [
        f"<b>{description}</b>",
        f"ID: <code>{html.escape(task_id)}</code>",
    ]
    if total:
        lines.append(f"<code>{_task_progress_bar(completed, total)}</code> {percent}%")
        lines.append(f"Прогресс: <b>{completed}/{total}</b>")
    lines.extend([
        f"Текущее: <code>{current}</code>",
        f"Статус: {status}",
    ])

    stats_lines = _format_task_stats(progress.get("stats") or {})
    if stats_lines:
        lines.append("Показатели: " + " | ".join(stats_lines))
    return "\n".join(lines)


def _tasks_text_and_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    kb_buttons = []
    if not active_background_tasks:
        text = "<b>Активные фоновые задачи</b>\n\nНет запущенных задач."
    else:
        blocks = ["<b>Активные фоновые задачи</b>"]
        for task_id, task_info in active_background_tasks.items():
            blocks.append(_format_task_block(task_id, task_info))
            description = str(task_info.get("description") or "задача")
            button_text = f"Отменить: {description[:40]}"
            kb_buttons.append([
                InlineKeyboardButton(text=button_text, callback_data=f"cancel_task:{task_id}")
            ])
        text = "\n\n".join(blocks)

    kb_buttons.append([InlineKeyboardButton(text="Обновить список", callback_data="manage_tasks")])
    kb_buttons.append([InlineKeyboardButton(text="Назад в главное меню", callback_data="back_to_main_menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_buttons)


@dp.callback_query(F.data == "manage_tasks")
async def cb_manage_tasks(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    text, keyboard = _tasks_text_and_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Нет прав.")
        return

    text, keyboard = _tasks_text_and_keyboard()
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("cancel_task:"))
async def cb_cancel_task(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    task_id = callback.data.split(":", 1)[1]
    task_info = active_background_tasks.get(task_id)
    if not task_info:
        await callback.answer("Задача не найдена или уже завершена.", show_alert=True)
        text, keyboard = _tasks_text_and_keyboard()
        await callback.message.edit_text(text, reply_markup=keyboard)
        return

    task_info["task"].cancel()
    try:
        await task_info["task"]
    except asyncio.CancelledError:
        logging.info(
            "Task %s ('%s') was cancelled by admin %s.",
            task_id,
            task_info.get("description"),
            callback.from_user.id,
        )
        await callback.answer("Задача отменена.", show_alert=True)

    text, keyboard = _tasks_text_and_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
