import asyncio
import logging
import os
import html

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Document
from aiogram.fsm.context import FSMContext

from ..bot_instance import dp, bot
from ..utils import (
    user_is_allowed, get_entity_info_robust, subscribe_entity_logic,
    subscribe_groups_bulk_in_bg, remove_group_from_db_and_leave,
    mass_leave_entities_for_all_clients,
    send_ai_welcome_message_to_chat
)
from ..keyboards import build_groups_keyboard, cancel_action_keyboard, main_menu_keyboard
from ..states import GroupManagementStates
from db import get_db_connection, record_entity_memberships

GROUPS_LIST_TITLE = "\U0001F4DC <b>\u0421\u043f\u0438\u0441\u043e\u043a \u0433\u0440\u0443\u043f\u043f</b>:"


@dp.callback_query(F.data == "groups_menu")
async def cb_groups_menu_main(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить группу(ы)", callback_data="add_group_options")],
        [InlineKeyboardButton(text="❌ Удалить группу (отписаться)", callback_data="remove_single_group_start")],
        [InlineKeyboardButton(text="🗑️ Удалить все группы", callback_data="delete_all_groups_confirm")],
        [InlineKeyboardButton(text="📜 Посмотреть список групп", callback_data="list_groups")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text("👥 <b>Управление группами</b>:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "add_group_options")
async def cb_add_group_options(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Одиночное добавление", callback_data="single_group_add_start")],
        [InlineKeyboardButton(text="🗂️ Массовое добавление (файл .txt)", callback_data="bulk_group_add_start")],
        [InlineKeyboardButton(text="⬅️ Назад в меню групп", callback_data="groups_menu")]
    ])
    await callback.message.edit_text("Выберите способ добавления групп:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "single_group_add_start")
async def cb_single_group_add_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Введите username группы (например, @groupname) или инвайт-ссылку (t.me/joinchat/xxxx):",
        reply_markup=cancel_action_keyboard())
    await state.set_state(GroupManagementStates.WaitingForAddGroup)
    await callback.answer()


@dp.message(GroupManagementStates.WaitingForAddGroup, F.text, ~F.text.startswith('/'))
async def handle_add_group(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    group_identifier = message.text.strip()
    admin_chat_id = message.chat.id
    await state.clear()

    await message.answer(f"▶️ Начинаю процесс добавления группы: `{html.escape(group_identifier)}`")

    group_info = None
    try:
        group_info = await get_entity_info_robust(group_identifier, admin_chat_id, 'group')
    except Exception as e:
        logging.error(f"Критическая ошибка получения информации о группе {group_identifier}: {e}")
        await message.answer(f"❌ Не удалось получить информацию о группе: {e}")
        return

    entity_title_display = group_info.get('title') or group_info.get('username') or group_identifier

    stats, assigned_id = await subscribe_entity_logic(
        group_identifier, 'group', admin_chat_id
    )

    if stats['success'] > 0:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO groups (id, username, title, enabled, assigned_account_id) VALUES (?, ?, ?, 1, ?)",
            (group_info["id"], group_info.get("username"), group_info.get("title"), assigned_id))
        conn.commit()
        conn.close()
        record_entity_memberships(
            stats.get('joined_account_ids', []),
            'group',
            group_info["id"],
            group_identifier
        )

    final_report = (
        f"<b>🏁 Итоговый отчет по добавлению '{html.escape(entity_title_display)}':</b>\n\n"
        f"✅ Успешных подписок: {stats['success']}\n"
        f"❌ Ошибок подписки: {stats['failed']}\n"
        f"🗑️ Аккаунтов удалено (бан/фриз): {stats['deleted']}\n\n"
    )
    if stats['success'] > 0:
        final_report += "Группа успешно добавлена в базу данных."
        asyncio.create_task(
            send_ai_welcome_message_to_chat(
                admin_chat_id=message.chat.id,
                target_chat_id=group_info["id"],
                target_chat_name=entity_title_display,
                entity_type="группу"
            )
        )
    else:
        final_report += "Группа не была добавлена в базу, так как ни один аккаунт не смог на нее подписаться."

    await message.answer(final_report, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "bulk_group_add_start")
async def cb_bulk_group_add_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Загрузите файл .txt, в котором каждая строка содержит ссылку или username группы.",
        reply_markup=cancel_action_keyboard()
    )
    await state.set_state(GroupManagementStates.WaitingForBulkGroupFile)
    await callback.answer()


@dp.message(GroupManagementStates.WaitingForBulkGroupFile, F.document)
async def handle_bulk_group_file(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    if not message.document.file_name.endswith(".txt"):
        await message.answer("Пожалуйста, загрузите файл в формате .txt", reply_markup=cancel_action_keyboard())
        return

    file_path = f"bulk_groups_{message.from_user.id}.txt"
    await bot.download(message.document.file_id, destination=file_path)

    lines = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        await message.answer(f"Ошибка чтения файла: {e}")
        await state.clear()
        return
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    if not lines:
        await message.answer("Файл пуст или не содержит ссылок. Попробуйте снова.", reply_markup=cancel_action_keyboard())
        return

    await message.answer("Файл получен, начинаю фоновую обработку добавления групп...",
                         reply_markup=InlineKeyboardMarkup(
                             inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Назад в меню групп", callback_data="groups_menu")]]))
    await subscribe_groups_bulk_in_bg(message.chat.id, message.from_user.id, lines)
    await state.clear()


@dp.message(GroupManagementStates.WaitingForBulkGroupFile, ~F.text.startswith('/'))
async def handle_bulk_group_file_incorrect(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await message.answer("Ожидается документ (.txt). Попробуйте снова или отмените действие.", reply_markup=cancel_action_keyboard())


@dp.callback_query(F.data == "list_groups")
async def cb_list_groups(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    kb = build_groups_keyboard(page=1, per_page=9)
    await callback.message.edit_text(GROUPS_LIST_TITLE, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("group_list_page:"))
async def cb_group_list_page(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        page = 1
    kb = build_groups_keyboard(page=page, per_page=9)
    await callback.message.edit_text(GROUPS_LIST_TITLE, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_group:"))
async def cb_toggle_group(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    try:
        parts = callback.data.split(":")
        group_id = int(parts[1])
        current_page = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        await callback.answer("Invalid data.", show_alert=True)
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM groups WHERE id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await callback.answer("Group not found.", show_alert=True)
        return
    new_status = 0 if row["enabled"] else 1
    cursor.execute("UPDATE groups SET enabled = ? WHERE id = ?", (new_status, group_id))
    conn.commit()
    conn.close()

    kb = build_groups_keyboard(page=current_page, per_page=9)
    await callback.message.edit_text(GROUPS_LIST_TITLE, reply_markup=kb)
    await callback.answer("Status updated.")


@dp.callback_query(F.data.startswith("groups_set_all:"))
async def cb_groups_set_all(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    try:
        parts = callback.data.split(":")
        new_status = int(parts[1])
        current_page = int(parts[2]) if len(parts) > 2 else 1
        if new_status not in (0, 1):
            raise ValueError("Invalid status")
    except Exception:
        await callback.answer("Invalid data.", show_alert=True)
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE groups SET enabled = ?", (new_status,))
    updated_count = cursor.rowcount
    conn.commit()
    conn.close()

    kb = build_groups_keyboard(page=current_page, per_page=9)
    await callback.message.edit_text(GROUPS_LIST_TITLE, reply_markup=kb)
    action = "enabled" if new_status else "disabled"
    await callback.answer(f"All groups {action}: {updated_count}.")

@dp.callback_query(F.data == "remove_single_group_start")
async def cb_remove_single_group_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Введите ID группы (число) или username/ссылку группы для удаления (включая отписку аккаунтов).",
        reply_markup=cancel_action_keyboard())
    await state.set_state(GroupManagementStates.WaitingForRemoveSingleGroup)
    await callback.answer()


@dp.message(GroupManagementStates.WaitingForRemoveSingleGroup, F.text, ~F.text.startswith('/'))
async def handle_remove_single_group_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    group_id_input = message.text.strip()
    group_id_to_remove = None
    reply_kb_group_menu = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню групп", callback_data="groups_menu")]])

    try:
        if group_id_input.isdigit() or (group_id_input.startswith("-") and group_id_input[1:].isdigit()):
            group_id_to_remove = int(group_id_input)
        else:
            group_info = await get_entity_info_robust(group_id_input, message.chat.id, 'group')
            group_id_to_remove = group_info["id"]
    except Exception as e:
        await message.answer(
            f"Ошибка: не удалось получить информацию о группе &laquo;{html.escape(message.text.strip())}&raquo;.\n{e}",
            reply_markup=reply_kb_group_menu)
        await state.clear()
        return

    if group_id_to_remove is None:
        await message.answer(f"Не удалось определить ID для {html.escape(group_id_input)}",
                             reply_markup=reply_kb_group_menu)
        await state.clear()
        return

    result_text = await remove_group_from_db_and_leave(group_id_to_remove)
    await message.answer(result_text, reply_markup=reply_kb_group_menu)
    await state.clear()


@dp.callback_query(F.data == "delete_all_groups_confirm")
async def cb_delete_all_groups_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Да, удалить все группы", callback_data="execute_delete_all_groups")],
        [InlineKeyboardButton(text="Отмена", callback_data="groups_menu")]
    ])
    await callback.message.edit_text(
        "<b>ВНИМАНИЕ!</b> Вы уверены, что хотите удалить ВСЕ группы из базы данных?\n"
        "Это действие также запустит процесс отписки всех ваших аккаунтов от этих групп (в фоновом режиме).\n"
        "<b>Это действие необратимо.</b>",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "execute_delete_all_groups")
async def cb_execute_delete_all_groups(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return

    await callback.message.edit_text("Начинаю удаление всех групп...")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, username FROM groups")
    groups_to_leave = cursor.fetchall()

    if not groups_to_leave:
        conn.close()
        await callback.message.edit_text("В базе нет групп для удаления.",
                                         reply_markup=InlineKeyboardMarkup(
                                             inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню групп",
                                                                                    callback_data="groups_menu")]]))
        await callback.answer()
        return

    group_ids_to_leave = [row['id'] for row in groups_to_leave]
    group_names_for_log = [row['title'] or row['username'] or str(row['id']) for row in groups_to_leave]

    try:
        cursor.execute("DELETE FROM groups")
        conn.commit()
        logging.info(f"Admin {callback.from_user.id}: All groups deleted from DB. Count: {len(group_ids_to_leave)}")

        await callback.message.edit_text(
            f"Все ({len(group_ids_to_leave)}) группы удалены из базы данных.\n"
            "Запускаю фоновую задачу для отписки аккаунтов от этих групп. Это может занять время...",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню групп", callback_data="groups_menu")]]))

        asyncio.create_task(mass_leave_entities_for_all_clients(
            admin_chat_id=callback.message.chat.id,
            entity_ids=group_ids_to_leave,
            entity_type="групп",
            entity_names_for_log=group_names_for_log
        ))

    except Exception as e:
        conn.rollback()
        logging.error(f"Admin {callback.from_user.id}: Failed to delete all groups from DB: {e}")
        await callback.message.edit_text(f"Ошибка при удалении групп из БД: {e}",
                                         reply_markup=InlineKeyboardMarkup(
                                             inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню групп",
                                                                                    callback_data="groups_menu")]]))
    finally:
        conn.close()

    await callback.answer()

