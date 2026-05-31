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
    user_is_allowed,
    get_entity_info_robust,
    subscribe_entity_logic,
    subscribe_channels_bulk_in_bg,
    mass_leave_entities_for_all_clients,
    send_ai_welcome_message_to_chat,
    auto_handle_linked_chat_and_add_to_groups,
    remove_group_from_db_and_leave,
    start_linked_discussion_sync_task
)
from ..keyboards import build_channels_keyboard, cancel_action_keyboard, main_menu_keyboard
from ..states import ChannelManagementStates
from db import get_db_connection, record_entity_memberships

CHANNELS_LIST_TITLE = "\U0001F4DC <b>\u0421\u043f\u0438\u0441\u043e\u043a \u043a\u0430\u043d\u0430\u043b\u043e\u0432</b>:"


@dp.callback_query(F.data == "channels_menu")
async def cb_channels_menu_main(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал(ы)", callback_data="add_channel_options")],
        [InlineKeyboardButton(text="❌ Удалить канал", callback_data="channel_remove_info")],
        [InlineKeyboardButton(text="🗑️ Удалить все каналы", callback_data="delete_all_channels_confirm")],
        [InlineKeyboardButton(text="🔁 Синхронизировать группы обсуждений", callback_data="sync_linked_discussion_groups")],
        [InlineKeyboardButton(text="📜 Список каналов", callback_data="channel_list")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text("📺 <b>Каналы</b>:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "sync_linked_discussion_groups")
async def cb_sync_linked_discussion_groups(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await state.clear()
    await callback.answer("Запускаю синхронизацию.")
    await start_linked_discussion_sync_task(callback.message.chat.id)


@dp.callback_query(F.data == "add_channel_options")
async def cb_add_channel_options(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Одиночное добавление", callback_data="single_channel_add_start")],
        [InlineKeyboardButton(text="🗂️ Массовое добавление (файл .txt)", callback_data="bulk_channel_add_start")],
        [InlineKeyboardButton(text="⬅️ Назад в меню каналов", callback_data="channels_menu")]
    ])
    await callback.message.edit_text("Выберите способ добавления каналов:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "single_channel_add_start")
async def cb_single_channel_add_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Введите username канала (например, @channelname) или инвайт-ссылку (t.me/+xxxx):",
        reply_markup=cancel_action_keyboard())
    await state.set_state(ChannelManagementStates.WaitingForChannelIdentifier)
    await callback.answer()


def _is_private_invite_link(identifier: str) -> bool:
    value = identifier.strip()
    return (
        value.startswith("https://t.me/+")
        or value.startswith("http://t.me/+")
        or value.startswith("t.me/+")
        or value.startswith("+")
        or "joinchat/" in value
    )


@dp.message(ChannelManagementStates.WaitingForChannelIdentifier, F.text, ~F.text.startswith('/'))
async def handle_channel_identifier(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("Access denied.")
        return

    channel_identifier = message.text.strip()
    admin_chat_id = message.chat.id
    await state.clear()

    await message.answer(f"Starting channel add flow: `{html.escape(channel_identifier)}`")

    channel_info = None
    channel_subs_stats = None
    assigned_account_id = None

    if _is_private_invite_link(channel_identifier):
        await message.answer("Invite-link detected. First joining the channel, then reading channel info...")
        channel_subs_stats, assigned_account_id = await subscribe_entity_logic(
            identifier=channel_identifier,
            entity_type="channel",
            admin_chat_id=admin_chat_id
        )
        if channel_subs_stats['success'] == 0:
            await message.answer(
                f"Channel '{html.escape(channel_identifier)}' was not added: no account could join it.")
            return

    try:
        channel_info = await get_entity_info_robust(channel_identifier, admin_chat_id, 'channel')
    except Exception as e:
        logging.error(f"Critical error getting channel info for {channel_identifier}: {e}")
        await message.answer(f"Could not get channel info after join attempt: {e}")
        return

    entity_title_display = channel_info.get('title') or channel_info.get('username') or channel_identifier

    if channel_subs_stats is None:
        await message.answer(f"Channel info for '{html.escape(entity_title_display)}' received. Starting subscription...")
        channel_subs_stats, assigned_account_id = await subscribe_entity_logic(
            identifier=channel_identifier,
            entity_type="channel",
            admin_chat_id=admin_chat_id
        )

    if channel_subs_stats['success'] > 0:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO channels (id, username, title, enabled, assigned_account_id) VALUES (?, ?, ?, 0, ?)",
            (
                channel_info["id"], channel_info.get("username"), channel_info.get("title"),
                assigned_account_id
            )
        )
        conn.commit()
        conn.close()
        record_entity_memberships(
            channel_subs_stats.get('joined_account_ids', []),
            'channel',
            channel_info["id"],
            channel_identifier
        )
        await message.answer(
            f"Channel '{html.escape(entity_title_display)}' (ID: {channel_info['id']}) added to database and disabled.")
        asyncio.create_task(
            send_ai_welcome_message_to_chat(
                admin_chat_id=admin_chat_id,
                target_chat_id=channel_info["id"],
                target_chat_name=entity_title_display,
                entity_type="channel"
            )
        )
    else:
        await message.answer(
            f"Channel '{html.escape(entity_title_display)}' was not added: no account could join it.")
        return

    await message.answer("Checking and processing linked discussion group, if one exists...")
    asyncio.create_task(
        auto_handle_linked_chat_and_add_to_groups(
            admin_chat_id=admin_chat_id,
            main_channel_info=channel_info,
            client_to_use=channel_info['client_obj']
        )
    )

    final_report = (
        f"<b>Channel add report for '{html.escape(entity_title_display)}':</b>\n\n"
        f"Subscribed accounts: {channel_subs_stats['success']}\n"
        f"Subscription errors: {channel_subs_stats['failed']}\n"
        f"Deleted inactive accounts: {channel_subs_stats['deleted']}"
    )

    await message.answer(final_report, reply_markup=main_menu_keyboard())


@dp.message(ChannelManagementStates.WaitingForLinkedChatManual, F.text, ~F.text.startswith('/'))
async def handle_linked_chat_input_manual(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return

    user_input = message.text.strip().lower()
    linked_chat_id_to_set = None
    reply_kb_chan_menu = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов", callback_data="channels_menu")]])

    if user_input not in ["нет", "пропустить", ""]:
        try:
            await message.answer(f"Пытаюсь получить информацию о чате: {message.text.strip()}")
            chat_info = await get_entity_info_robust(message.text.strip(), message.chat.id, 'group')
            linked_chat_id_to_set = chat_info["id"]
        except Exception as e:
            await message.answer(
                f"Не удалось определить связанный чат по '{html.escape(message.text.strip())}': {e}. ID связанного чата не будет изменен вручную.",
                reply_markup=reply_kb_chan_menu)
            await state.clear()
            return

    data = await state.get_data()
    channel_id = data.get("channel_id_for_linked_chat")
    if channel_id is None:
        await message.answer("Ошибка: не удалось определить ID канала для обновления связанного чата.",
                             reply_markup=reply_kb_chan_menu)
        await state.clear()
        return

    if linked_chat_id_to_set is not None:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE channels SET linked_chat_id = ? WHERE id = ?",
            (str(linked_chat_id_to_set), channel_id)
        )
        conn.commit()
        conn.close()
        await message.answer(
            f"ID связанного чата для канала обновлен на: {linked_chat_id_to_set}.",
            reply_markup=reply_kb_chan_menu
        )
    else:
        await message.answer(
            "ID связанного чата не изменен вручную.",
            reply_markup=reply_kb_chan_menu
        )
    await state.clear()


@dp.callback_query(F.data == "bulk_channel_add_start")
async def cb_bulk_channel_add_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Загрузите файл .txt, в котором каждая строка содержит ссылку или username канала.",
        reply_markup=cancel_action_keyboard()
    )
    await state.set_state(ChannelManagementStates.WaitingForBulkChannelFile)
    await callback.answer()


@dp.message(ChannelManagementStates.WaitingForBulkChannelFile, F.document)
async def handle_bulk_channel_file(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    if not message.document.file_name.endswith(".txt"):
        await message.answer("Пожалуйста, загрузите файл в формате .txt", reply_markup=cancel_action_keyboard())
        return

    file_path = f"bulk_channels_{message.from_user.id}.txt"
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

    await message.answer("Файл получен, начинаю фоновую обработку добавления каналов...",
                         reply_markup=InlineKeyboardMarkup(
                             inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов",
                                                                    callback_data="channels_menu")]]))
    await subscribe_channels_bulk_in_bg(message.chat.id, message.from_user.id, lines)
    await state.clear()


@dp.message(ChannelManagementStates.WaitingForBulkChannelFile, ~F.text.startswith('/'))
async def handle_bulk_channel_file_incorrect(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await message.answer("Ожидается документ (.txt). Попробуйте снова или отмените действие.", reply_markup=cancel_action_keyboard())


@dp.callback_query(F.data == "channel_list")
async def cb_channel_list(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    kb = build_channels_keyboard(page=1, per_page=9)
    await callback.message.edit_text(CHANNELS_LIST_TITLE, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("channel_list_page:"))
async def cb_channel_list_page(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        page = 1
    kb = build_channels_keyboard(page=page, per_page=9)
    await callback.message.edit_text(CHANNELS_LIST_TITLE, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_channel:"))
async def cb_toggle_channel(callback: CallbackQuery):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Access denied.")
        return
    try:
        parts = callback.data.split(":")
        channel_id = int(parts[1])
        current_page = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        await callback.answer("Invalid data.", show_alert=True)
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT enabled FROM channels WHERE id = ?", (channel_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await callback.answer("Channel not found.", show_alert=True)
        return
    new_status = 0 if row["enabled"] else 1
    cursor.execute("UPDATE channels SET enabled = ? WHERE id = ?", (new_status, channel_id))
    conn.commit()
    conn.close()

    kb = build_channels_keyboard(page=current_page, per_page=9)
    await callback.message.edit_text(CHANNELS_LIST_TITLE, reply_markup=kb)
    await callback.answer("Status updated.")


@dp.callback_query(F.data.startswith("channels_set_all:"))
async def cb_channels_set_all(callback: CallbackQuery):
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
    cursor.execute("UPDATE channels SET enabled = ?", (new_status,))
    updated_count = cursor.rowcount
    conn.commit()
    conn.close()

    kb = build_channels_keyboard(page=current_page, per_page=9)
    await callback.message.edit_text(CHANNELS_LIST_TITLE, reply_markup=kb)
    action = "enabled" if new_status else "disabled"
    await callback.answer(f"All channels {action}: {updated_count}.")

@dp.callback_query(F.data == "channel_remove_info")
async def cb_channel_remove_info(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Введите ID канала (число) или username/ссылку канала для удаления.",
        reply_markup=cancel_action_keyboard())
    await state.set_state(ChannelManagementStates.WaitingForChannelIdToRemove)
    await callback.answer()


@dp.message(ChannelManagementStates.WaitingForChannelIdToRemove, F.text, ~F.text.startswith('/'))
async def handle_channel_remove_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        await message.answer("У вас нет прав.")
        return
    channel_id_input = message.text.strip()
    reply_kb_chan_menu = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов", callback_data="channels_menu")]])

    channel_id_to_remove = None
    channel_info_for_log = None
    try:
        await message.answer(f"Получаю информацию для '{html.escape(channel_id_input)}'...")
        channel_info = await get_entity_info_robust(channel_id_input, message.chat.id, 'channel')
        channel_id_to_remove = channel_info["id"]
        channel_info_for_log = channel_info
    except Exception as e:
        await message.answer(
            f"Ошибка: не удалось получить информацию о канале &laquo;{html.escape(channel_id_input)}&raquo;.\n{e}",
            reply_markup=reply_kb_chan_menu)
        await state.clear()
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT linked_chat_id, title, username FROM channels WHERE id = ?", (channel_id_to_remove,))
    channel_data_from_db = cursor.fetchone()

    channel_display_name = (channel_info_for_log.get('title') or
                            channel_info_for_log.get('username') or
                            str(channel_id_to_remove))

    linked_chat_id_str = channel_data_from_db['linked_chat_id'] if channel_data_from_db else None
    if linked_chat_id_str:
        try:
            linked_chat_id = int(linked_chat_id_str)
            await message.answer(f"Найден связанный чат (ID: {linked_chat_id}). Удаляю его и отписываю аккаунты...")
            linked_chat_removal_result = await remove_group_from_db_and_leave(linked_chat_id)
            await message.answer(f"Результат удаления связанного чата:\n{linked_chat_removal_result}")
        except (ValueError, TypeError):
            await message.answer(
                f"Некорректный ID связанного чата в базе: '{linked_chat_id_str}'. Пропускаю его удаление.")
        except Exception as e_linked:
            await message.answer(f"Ошибка при удалении связанного чата ID {linked_chat_id_str}: {e_linked}")

    await message.answer(f"Отписываю все аккаунты от самого канала '{channel_display_name}'...")
    await mass_leave_entities_for_all_clients(
        admin_chat_id=message.chat.id,
        entity_ids=[channel_id_to_remove],
        entity_type="канала",
        entity_names_for_log=[channel_display_name]
    )

    cursor.execute("DELETE FROM channels WHERE id = ?", (channel_id_to_remove,))
    deleted_rows = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted_rows:
        await message.answer(f"Канал '{channel_display_name}' (ID={channel_id_to_remove}) окончательно удалён из базы.",
                             reply_markup=reply_kb_chan_menu)
    else:
        await message.answer("Такого канала не найдено в базе для удаления (возможно, уже удален).",
                             reply_markup=reply_kb_chan_menu)

    await state.clear()


@dp.callback_query(F.data == "delete_all_channels_confirm")
async def cb_delete_all_channels_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Да, удалить все каналы", callback_data="execute_delete_all_channels")],
        [InlineKeyboardButton(text="Отмена", callback_data="channels_menu")]
    ])
    await callback.message.edit_text(
        "<b>ВНИМАНИЕ!</b> Вы уверены, что хотите удалить ВСЕ каналы из базы данных?\n"
        "Это действие также запустит процесс отписки всех ваших аккаунтов от этих каналов (в фоновом режиме).\n"
        "<b>Это действие необратимо.</b>",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "execute_delete_all_channels")
async def cb_execute_delete_all_channels(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return

    await callback.message.edit_text("Начинаю удаление всех каналов...")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, username FROM channels")
    channels_to_leave = cursor.fetchall()

    if not channels_to_leave:
        conn.close()
        await callback.message.edit_text("В базе нет каналов для удаления.",
                                         reply_markup=InlineKeyboardMarkup(
                                             inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов",
                                                                                    callback_data="channels_menu")]]))
        await callback.answer()
        return

    channel_ids_to_leave = [row['id'] for row in channels_to_leave]
    channel_names_for_log = [row['title'] or row['username'] or str(row['id']) for row in channels_to_leave]

    try:
        cursor.execute("DELETE FROM channels")
        conn.commit()
        logging.info(
            f"Admin {callback.from_user.id}: All channels deleted from DB. Count: {len(channel_ids_to_leave)}")

        await callback.message.edit_text(
            f"Все ({len(channel_ids_to_leave)}) каналы удалены из базы данных.\n"
            "Запускаю фоновую задачу для отписки аккаунтов от этих каналов. Это может занять время...",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов", callback_data="channels_menu")]])
        )

        asyncio.create_task(mass_leave_entities_for_all_clients(
            admin_chat_id=callback.message.chat.id,
            entity_ids=channel_ids_to_leave,
            entity_type="каналов",
            entity_names_for_log=channel_names_for_log
        ))

    except Exception as e:
        conn.rollback()
        logging.error(f"Admin {callback.from_user.id}: Failed to delete all channels from DB: {e}")
        await callback.message.edit_text(f"Ошибка при удалении каналов из БД: {e}",
                                         reply_markup=InlineKeyboardMarkup(
                                             inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню каналов",
                                                                                    callback_data="channels_menu")]]))
    finally:
        conn.close()

    await callback.answer()

