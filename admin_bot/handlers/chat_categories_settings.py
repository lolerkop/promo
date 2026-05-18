import asyncio
import logging
import os
import random
import time
import math
import html
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ..bot_instance import dp, bot
from ..utils import user_is_allowed, get_entity_info_robust, subscribe_entity_logic
from db import (
    get_db_connection,
    add_category_keyword, delete_category_keyword, get_category_keywords
)
from userbot import get_active_clients
from ..states import CategoryRegularCommentStates

CHAT_CATEGORIES_DIR = "chat_categories"
MAX_CHATS_PER_PAGE_CAT = 9


class CategorySettingsStates(StatesGroup):
    WaitingForCategoryPrompt = State()
    WaitingForChatsToJoinCount = State()
    WaitingForJoinIntensity = State()
    WaitingForCategoryKeyword = State()
    WaitingForCategoryKeywordAnswerType = State()
    WaitingForCategoryKeywordPredefinedAnswer = State()
    DeletingCategoryKeyword = State()


def get_category_files():
    if not os.path.exists(CHAT_CATEGORIES_DIR):
        os.makedirs(CHAT_CATEGORIES_DIR, exist_ok=True)
        return []
    return [f for f in os.listdir(CHAT_CATEGORIES_DIR) if f.endswith(".txt")]


def get_category_status(category_name_no_ext: str) -> dict:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM chat_categories WHERE category_name = ?",
        (category_name_no_ext,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {"is_active": 0, "prompt": None, "chats_to_join_count": 5, "join_intensity_per_hour": 10,
            "regular_comment_enabled": 0, "regular_comment_interval_minutes": 60, "regular_comment_prompt": None}


def update_category_db(category_name_no_ext: str, data: dict):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO chat_categories (category_name, is_active, prompt, chats_to_join_count, join_intensity_per_hour, regular_comment_enabled, regular_comment_interval_minutes, regular_comment_prompt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category_name) DO UPDATE SET
        is_active = excluded.is_active,
        prompt = excluded.prompt,
        chats_to_join_count = excluded.chats_to_join_count,
        join_intensity_per_hour = excluded.join_intensity_per_hour,
        regular_comment_enabled = excluded.regular_comment_enabled,
        regular_comment_interval_minutes = excluded.regular_comment_interval_minutes,
        regular_comment_prompt = excluded.regular_comment_prompt
    """, (category_name_no_ext, data.get("is_active", 0), data.get("prompt"),
          data.get("chats_to_join_count", 5), data.get("join_intensity_per_hour", 10),
          data.get("regular_comment_enabled", 0), data.get("regular_comment_interval_minutes", 60),
          data.get("regular_comment_prompt")))
    conn.commit()
    conn.close()


def chat_categories_main_menu_keyboard(page: int = 1) -> InlineKeyboardMarkup:
    files = get_category_files()
    buttons = []

    start_index = (page - 1) * MAX_CHATS_PER_PAGE_CAT
    end_index = start_index + MAX_CHATS_PER_PAGE_CAT
    paginated_files = files[start_index:end_index]

    for filename in paginated_files:
        category_name = filename[:-4]
        status = get_category_status(category_name)
        active_emoji = "🟢" if status["is_active"] else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{active_emoji} {category_name} ({status['chats_to_join_count']}ч, {status['join_intensity_per_hour']}/ч)",
            callback_data=f"select_category:{category_name}"
        )])

    pagination_row = []
    total_pages = math.ceil(len(files) / MAX_CHATS_PER_PAGE_CAT) if files else 0
    if page > 1:
        pagination_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"cat_list_page:{page - 1}"))
    if total_pages > 1:
        pagination_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore_page_num"))
    if page < total_pages:
        pagination_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"cat_list_page:{page + 1}"))

    if pagination_row:
        buttons.append(pagination_row)

    buttons.append([InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "ignore_page_num")
async def cb_ignore_page_num(callback: CallbackQuery):
    await callback.answer()


def individual_category_menu_keyboard(category_name: str) -> InlineKeyboardMarkup:
    status = get_category_status(category_name)
    active_text = "Деактивировать" if status["is_active"] else "Активировать"
    prompt_set_text = "Установлен" if status.get("prompt") else "Не установлен"

    rc_enabled = status.get('regular_comment_enabled', 0)
    rc_status_emoji = "🟢" if rc_enabled else "🔴"
    rc_interval = status.get('regular_comment_interval_minutes', 60)
    rc_prompt_status = "Установлен" if status.get('regular_comment_prompt') else "Не установлен"

    buttons = [
        [InlineKeyboardButton(text=active_text, callback_data=f"toggle_category_activation:{category_name}")],
        [InlineKeyboardButton(text=f"Промпт (для триггеров): {prompt_set_text}",
                              callback_data=f"set_category_prompt:{category_name}")],
        [InlineKeyboardButton(text=f"Ключевые слова категории", callback_data=f"manage_cat_keywords:{category_name}")],
        [InlineKeyboardButton(text=f"Кол-во чатов для подписки: {status.get('chats_to_join_count', 5)}",
                              callback_data=f"set_cat_join_count:{category_name}")],
        [InlineKeyboardButton(text=f"Интенсивность подписок/час: {status.get('join_intensity_per_hour', 10)}",
                              callback_data=f"set_cat_join_intensity:{category_name}")],
        [InlineKeyboardButton(text="--- Авто-комментарий для категории ---", callback_data="ignore")],
        [InlineKeyboardButton(text=f"{rc_status_emoji} Статус авто-комментария",
                              callback_data=f"toggle_cat_rc_status:{category_name}")],
        [InlineKeyboardButton(text=f"Интервал: {rc_interval} мин",
                              callback_data=f"set_cat_rc_interval:{category_name}")],
        [InlineKeyboardButton(text=f"Промпт: {rc_prompt_status}", callback_data=f"set_cat_rc_prompt:{category_name}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку категорий", callback_data="chat_categories_main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.in_({"chat_categories_main_menu", "cat_list_page:1"}))
async def cb_chat_categories_main_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    page = 1
    if ":" in callback.data and callback.data.startswith("cat_list_page:"):
        try:
            page = int(callback.data.split(":")[1])
        except:
            page = 1

    await callback.message.edit_text(
        "🗂️ <b>Управление категориями чатов</b>\nВыберите категорию для настройки:",
        reply_markup=chat_categories_main_menu_keyboard(page=page)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cat_list_page:"))
async def cb_chat_categories_list_page(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        page = int(callback.data.split(":")[1])
    except:
        await callback.answer("Ошибка пагинации.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗂️ <b>Управление категориями чатов</b>\nВыберите категорию для настройки:",
        reply_markup=chat_categories_main_menu_keyboard(page=page)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("select_category:"))
async def cb_select_category(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)

    status = get_category_status(category_name)
    active_str = "Активна" if status["is_active"] else "Неактивна"
    prompt_str = status.get("prompt", "<i>Не установлен</i>")
    if prompt_str and len(prompt_str) > 100:
        prompt_str = html.escape(prompt_str[:100]) + "..."
    elif prompt_str:
        prompt_str = html.escape(prompt_str)

    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\nСтатус: {active_str}\n"
        f"Промпт (для триггеров): {prompt_str}\n"
        f"Подписаться на: {status.get('chats_to_join_count', 5)} чатов\n"
        f"Интенсивность: {status.get('join_intensity_per_hour', 10)} подписок/час\n\n"
        "Выберите действие:",
        reply_markup=individual_category_menu_keyboard(category_name),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_cat_rc_status:"))
async def cb_toggle_cat_rc_status(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    category_data = get_category_status(category_name)
    category_data['regular_comment_enabled'] = 1 - category_data.get('regular_comment_enabled', 0)
    update_category_db(category_name, category_data)
    await callback.answer(f"Статус авто-комментария изменен.")
    await callback.message.edit_reply_markup(reply_markup=individual_category_menu_keyboard(category_name))


@dp.callback_query(F.data.startswith("set_cat_rc_interval:"))
async def cb_set_cat_rc_interval(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)
    current_val = get_category_status(category_name).get('regular_comment_interval_minutes', 60)
    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\nТекущий интервал авто-комментария: {current_val} мин.\nВведите новое значение (в минутах):")
    await state.set_state(CategoryRegularCommentStates.WaitingForInterval)
    await callback.answer()


@dp.message(CategoryRegularCommentStates.WaitingForInterval, F.text, ~F.text.startswith('/'))
async def process_cat_rc_interval(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    if not category_name:
        await message.answer("Ошибка: категория не выбрана.", reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return
    try:
        interval = int(message.text.strip())
        if interval <= 0:
            await message.answer("Интервал должен быть больше нуля.")
            return
        category_data = get_category_status(category_name)
        category_data["regular_comment_interval_minutes"] = interval
        update_category_db(category_name, category_data)
        await message.answer(f"Интервал для '{category_name}' установлен: {interval} мин.",
                             reply_markup=individual_category_menu_keyboard(category_name))
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")


@dp.callback_query(F.data.startswith("set_cat_rc_prompt:"))
async def cb_set_cat_rc_prompt(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)
    current_prompt = get_category_status(category_name).get('regular_comment_prompt', "")
    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\n"
        f"Текущий промпт авто-комментария:\n<pre>{html.escape(current_prompt) if current_prompt else 'Не установлен'}</pre>\n\n"
        "Введите новый промпт или отправьте '-' для его удаления.",
        parse_mode="HTML"
    )
    await state.set_state(CategoryRegularCommentStates.WaitingForPrompt)
    await callback.answer()


@dp.message(CategoryRegularCommentStates.WaitingForPrompt, F.text, ~F.text.startswith('/'))
async def process_cat_rc_prompt(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    if not category_name:
        await message.answer("Ошибка: категория не выбрана.", reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return

    new_prompt = message.text.strip() if message.text else ""
    if new_prompt == "-": new_prompt = ""

    category_data = get_category_status(category_name)
    category_data["regular_comment_prompt"] = new_prompt
    update_category_db(category_name, category_data)

    await message.answer(f"Промпт авто-комментария для '{category_name}' обновлен.",
                         reply_markup=individual_category_menu_keyboard(category_name))
    await state.clear()


@dp.callback_query(F.data.startswith("set_category_prompt:"))
async def cb_set_category_prompt(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)

    current_prompt = get_category_status(category_name).get("prompt", "")
    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\n"
        f"Текущий промпт (для триггеров):\n<pre>{html.escape(current_prompt) if current_prompt else 'Не установлен'}</pre>\n\n"
        "Введите новый промпт для этой категории или отправьте '-' для его удаления.",
        parse_mode="HTML"
    )
    await state.set_state(CategorySettingsStates.WaitingForCategoryPrompt)
    await callback.answer()


@dp.message(CategorySettingsStates.WaitingForCategoryPrompt, F.text, ~F.text.startswith('/'))
async def process_category_prompt(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    if not category_name:
        await message.answer("Ошибка: категория не выбрана. Вернитесь в меню категорий.",
                             reply_markup=chat_categories_main_menu_keyboard())
        await state.clear()
        return

    new_prompt = message.text.strip() if message.text else ""
    if new_prompt == "-": new_prompt = ""

    category_data = get_category_status(category_name)
    category_data["prompt"] = new_prompt
    update_category_db(category_name, category_data)

    await message.answer(f"Промпт для категории '{category_name}' обновлен.",
                         reply_markup=individual_category_menu_keyboard(category_name))
    await state.clear()


@dp.callback_query(F.data.startswith("set_cat_join_count:"))
async def cb_set_cat_join_count(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)
    current_val = get_category_status(category_name).get('chats_to_join_count', 5)
    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\nТекущее кол-во чатов для подписки: {current_val}\nВведите новое число:")
    await state.set_state(CategorySettingsStates.WaitingForChatsToJoinCount)
    await callback.answer()


@dp.message(CategorySettingsStates.WaitingForChatsToJoinCount, F.text, ~F.text.startswith('/'))
async def process_cat_join_count(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    if not category_name:
        await message.answer("Ошибка: категория не выбрана.", reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return

    try:
        count = int(message.text.strip())
        if count <= 0:
            await message.answer("Количество должно быть больше нуля.")
            return
        category_data = get_category_status(category_name)
        category_data["chats_to_join_count"] = count
        update_category_db(category_name, category_data)
        await message.answer(f"Кол-во чатов для подписки для '{category_name}' установлено: {count}.",
                             reply_markup=individual_category_menu_keyboard(category_name))
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")


@dp.callback_query(F.data.startswith("set_cat_join_intensity:"))
async def cb_set_cat_join_intensity(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]
    await state.update_data(current_category_name=category_name)
    current_val = get_category_status(category_name).get('join_intensity_per_hour', 10)
    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\nТекущая интенсивность (подписок аккаунтов в час): {current_val}\nВведите новое число:")
    await state.set_state(CategorySettingsStates.WaitingForJoinIntensity)
    await callback.answer()


@dp.message(CategorySettingsStates.WaitingForJoinIntensity, F.text, ~F.text.startswith('/'))
async def process_cat_join_intensity(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    if not category_name:
        await message.answer("Ошибка: категория не выбрана.", reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return

    try:
        intensity = int(message.text.strip())
        if intensity <= 0:
            await message.answer("Интенсивность должна быть больше нуля.")
            return
        category_data = get_category_status(category_name)
        category_data["join_intensity_per_hour"] = intensity
        update_category_db(category_name, category_data)
        await message.answer(f"Интенсивность для '{category_name}' установлена: {intensity} подписок/час.",
                             reply_markup=individual_category_menu_keyboard(category_name))
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")


active_category_tasks = {}


async def _task_process_category_activation(category_name: str, admin_chat_id: int):
    userbot_clients_list = get_active_clients()
    await bot.send_message(admin_chat_id, f"🚀 Начинаю активацию и подписку на чаты категории '{category_name}'...")

    category_data = get_category_status(category_name)
    num_to_join_total = category_data.get('chats_to_join_count', 1)
    intensity_per_hour_total = category_data.get('join_intensity_per_hour', 1)

    if not userbot_clients_list:
        await bot.send_message(admin_chat_id,
                               f"⚠️ Нет активных аккаунтов. Активация категории '{category_name}' прервана.")
        current_status = get_category_status(category_name)
        current_status["is_active"] = 0
        update_category_db(category_name, current_status)
        if category_name in active_category_tasks: active_category_tasks.pop(category_name, None)
        return

    category_file_path = os.path.join(CHAT_CATEGORIES_DIR, f"{category_name}.txt")
    if not os.path.exists(category_file_path):
        await bot.send_message(admin_chat_id, f"⚠️ Файл для категории '{category_name}' не найден. Активация прервана.")
        current_status = get_category_status(category_name)
        current_status["is_active"] = 0
        update_category_db(category_name, current_status)
        if category_name in active_category_tasks: active_category_tasks.pop(category_name, None)
        return

    try:
        with open(category_file_path, 'r', encoding='utf-8') as f:
            all_chat_links = [line.strip() for line in f if line.strip()]
    except Exception as e:
        await bot.send_message(admin_chat_id,
                               f"⚠️ Ошибка чтения файла категории '{category_name}': {e}. Активация прервана.")
        current_status = get_category_status(category_name)
        current_status["is_active"] = 0
        update_category_db(category_name, current_status)
        if category_name in active_category_tasks: active_category_tasks.pop(category_name, None)
        return

    random.shuffle(all_chat_links)
    links_to_process = all_chat_links[:num_to_join_total]
    delay_between_actions = 3600.0 / intensity_per_hour_total if intensity_per_hour_total > 0 else 3600.0

    total_stats = {'success': 0, 'failed': 0, 'deleted': 0, 'db_added': 0}
    conn = get_db_connection()
    c = conn.cursor()

    for link_idx, chat_link in enumerate(links_to_process):
        if not get_category_status(category_name)["is_active"]:
            await bot.send_message(admin_chat_id,
                                   f"ℹ️ Категория '{category_name}' была деактивирована. Процесс остановлен.")
            break

        await bot.send_message(admin_chat_id,
                               f"Категория '{category_name}': обрабатываю чат {link_idx + 1}/{len(links_to_process)}: `{html.escape(chat_link)}`")

        try:
            group_info = await get_entity_info_robust(chat_link, admin_chat_id, 'group')

            c.execute("SELECT 1 FROM category_joined_chats WHERE category_name = ? AND chat_id = ?",
                      (category_name, group_info['id']))
            if c.fetchone():
                logging.info(f"Чат {group_info['id']} уже обработан для категории {category_name}.")
                continue

            stats, assigned_id = await subscribe_entity_logic(chat_link, 'group', admin_chat_id)

            total_stats['success'] += stats['success']
            total_stats['failed'] += stats['failed']
            total_stats['deleted'] += stats['deleted']

            if stats['success'] > 0:
                c.execute(
                    "INSERT INTO category_joined_chats (category_name, chat_id, account_db_id, chat_link) VALUES (?, ?, ?, ?)",
                    (category_name, group_info["id"], assigned_id or group_info['client_data']['id'], chat_link)
                )
                conn.commit()
                total_stats['db_added'] += 1

        except asyncio.CancelledError:
            await bot.send_message(admin_chat_id, f"Задача для категории '{category_name}' отменена.")
            raise
        except Exception as e:
            await bot.send_message(admin_chat_id, f"❌ Ошибка при обработке чата {chat_link}: {e}")

        await asyncio.sleep(max(5.0, delay_between_actions))

    conn.close()

    summary_report = (
        f"🏁 Активация категории '{category_name}' завершена!\n\n"
        f"🔗 Всего ссылок обработано: {len(links_to_process)}\n"
        f"✅ Чатов успешно добавлено в категорию: {total_stats['db_added']}\n\n"
        f"📈 <b><u>Общая статистика по аккаунтам:</u></b>\n"
        f"👍 Успешных подписок: {total_stats['success']}\n"
        f"👎 Ошибок подписки: {total_stats['failed']}\n"
        f"🗑️ Аккаунтов удалено (бан): {total_stats['deleted']}"
    )
    await bot.send_message(admin_chat_id, summary_report)

    if category_name in active_category_tasks:
        active_category_tasks.pop(category_name, None)


async def _task_process_category_deactivation(category_name: str, admin_chat_id: int):
    userbot_clients_list = get_active_clients()
    await bot.send_message(admin_chat_id, f"🧹 Начинаю деактивацию категории '{category_name}' и отписку аккаунтов...")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id, account_db_id, chat_link FROM category_joined_chats WHERE category_name = ?",
              (category_name,))
    chats_to_leave = c.fetchall()

    unsubscribed_count = 0
    if not chats_to_leave:
        await bot.send_message(admin_chat_id,
                               f"ℹ️ Для категории '{category_name}' нет записанных подписок. Отписываться не от чего.")
    else:
        for chat_entry in chats_to_leave:
            chat_id_to_leave = chat_entry["chat_id"]
            account_db_id_to_use = chat_entry["account_db_id"]
            chat_link_display = chat_entry["chat_link"] or chat_id_to_leave

            client_to_use = None
            client_label = f"ID {account_db_id_to_use}"
            for cl, data in userbot_clients_list:
                if data["id"] == account_db_id_to_use:
                    client_to_use = cl
                    client_label = data["label"]
                    break

            if client_to_use and client_to_use.is_connected():
                await bot.send_message(admin_chat_id,
                                       f"Категория '{category_name}': аккаунт {client_label} отписывается от {chat_link_display}...")
                try:
                    await subscribe_entity_logic(chat_id_to_leave, 'group', admin_chat_id, action="leave",
                                                 client_override=client_to_use)
                    unsubscribed_count += 1
                except Exception as e:
                    await bot.send_message(admin_chat_id,
                                           f"⚠️ Ошибка отписки аккаунта {client_label} от {chat_link_display}: {e}")
                    logging.error(
                        f"Ошибка отписки от {chat_link_display} для категории {category_name} аккаунтом {client_label}: {e}")
                await asyncio.sleep(random.uniform(1, 3))
            else:
                await bot.send_message(admin_chat_id,
                                       f"⚠️ Не найден активный/подключенный клиент для аккаунта ID {account_db_id_to_use} для отписки от {chat_link_display}.")

    c.execute("DELETE FROM category_joined_chats WHERE category_name = ?", (category_name,))
    conn.commit()
    conn.close()

    await bot.send_message(admin_chat_id,
                           f"🏁 Деактивация категории '{category_name}' завершена. Попыток отписки: {unsubscribed_count}. Записи о подписках удалены.")
    if category_name in active_category_tasks: active_category_tasks.pop(category_name, None)


@dp.callback_query(F.data.startswith("toggle_category_activation:"))
async def cb_toggle_category_activation(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":")[1]

    current_status = get_category_status(category_name)
    new_is_active = 0 if current_status["is_active"] else 1

    current_status["is_active"] = new_is_active
    update_category_db(category_name, current_status)

    if new_is_active:
        if category_name in active_category_tasks and not active_category_tasks[category_name].done():
            await callback.answer(f"Задача для категории '{category_name}' уже запущена.", show_alert=True)
        else:
            if not current_status.get('chats_to_join_count') or current_status.get('chats_to_join_count', 0) <= 0 or \
                    not current_status.get('join_intensity_per_hour') or current_status.get('join_intensity_per_hour',
                                                                                            0) <= 0:
                await callback.message.answer(
                    f"⚠️ Для активации категории '{category_name}' необходимо сначала задать положительные значения для 'Кол-во чатов для подписки' и 'Интенсивность'.",
                    reply_markup=individual_category_menu_keyboard(category_name))
                current_status["is_active"] = 0
                update_category_db(category_name, current_status)
                await callback.answer("Настройте параметры подписки.", show_alert=True)
                return

            task = asyncio.create_task(_task_process_category_activation(category_name, callback.message.chat.id))
            active_category_tasks[category_name] = task
            await callback.answer(f"Запущена активация категории '{category_name}' в фоновом режиме.")
    else:
        if category_name in active_category_tasks and not active_category_tasks[category_name].done():
            active_category_tasks[category_name].cancel()
            try:
                await active_category_tasks[category_name]
            except asyncio.CancelledError:
                logging.info(f"Задача активации для категории '{category_name}' успешно отменена.")
            active_category_tasks.pop(category_name, None)
            await callback.answer(f"Текущая задача активации для '{category_name}' отменена.", show_alert=True)

        task_deactivation = asyncio.create_task(
            _task_process_category_deactivation(category_name, callback.message.chat.id))
        active_category_tasks[category_name] = task_deactivation
        await callback.answer(f"Запущена деактивация категории '{category_name}' и отписка в фоновом режиме.")

    new_category_status = get_category_status(category_name)
    active_str = "Активна" if new_category_status["is_active"] else "Неактивна"
    prompt_str = new_category_status.get("prompt", "<i>Не установлен</i>")
    if prompt_str and len(prompt_str) > 100:
        prompt_str = html.escape(prompt_str[:100]) + "..."
    elif prompt_str:
        prompt_str = html.escape(prompt_str)

    await callback.message.edit_text(
        f"<b>Категория: {category_name}</b>\nСтатус: {active_str}\n"
        f"Промпт: {prompt_str}\n"
        f"Подписаться на: {new_category_status.get('chats_to_join_count', 5)} чатов\n"
        f"Интенсивность: {new_category_status.get('join_intensity_per_hour', 10)} подписок/час\n\n"
        "Статус обновлен. Фоновая задача запущена/остановлена.",
        reply_markup=individual_category_menu_keyboard(category_name),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("manage_cat_keywords:"))
async def cb_manage_cat_keywords(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":", 1)[1]
    await state.update_data(current_category_name=category_name)

    keywords = get_category_keywords(category_name)
    text = f"🔑 <b>Ключевые слова для категории: {category_name}</b>\n\n"
    if not keywords:
        text += "Ключевые слова еще не добавлены.\n"
    else:
        for kw_data in keywords:
            text += f"- <code>{html.escape(kw_data['keyword'])}</code> (Тип: {kw_data['response_type']})\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ключевое слово",
                              callback_data=f"add_cat_keyword_start:{category_name}")],
        [InlineKeyboardButton(text="➖ Удалить ключевое слово", callback_data=f"del_cat_keyword_start:{category_name}")],
        [InlineKeyboardButton(text="⬅️ Назад к категории", callback_data=f"select_category:{category_name}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("add_cat_keyword_start:"))
async def cb_add_cat_keyword_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":", 1)[1]
    await state.update_data(current_category_name=category_name)
    await callback.message.edit_text(f"<b>Категория: {category_name}</b>\nВведите новое ключевое слово (фразу):")
    await state.set_state(CategorySettingsStates.WaitingForCategoryKeyword)
    await callback.answer()


@dp.message(CategorySettingsStates.WaitingForCategoryKeyword, F.text, ~F.text.startswith('/'))
async def process_category_keyword_input(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    keyword = message.text.strip().lower()
    if not keyword:
        await message.answer("Ключевое слово не может быть пустым. Попробуйте снова.")
        return
    await state.update_data(new_category_keyword=keyword)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Предустановленная фраза", callback_data="cat_kw_choice_predefined")],
        [InlineKeyboardButton(text="🤖 OpenAI/G4F генерация", callback_data="cat_kw_choice_openai")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_cat_kw_add")]
    ])
    await message.answer(f"Ключевое слово для категории: <b>{keyword}</b>. Выберите тип ответа:", reply_markup=kb)
    await state.set_state(CategorySettingsStates.WaitingForCategoryKeywordAnswerType)


@dp.callback_query(F.data == "cancel_cat_kw_add", CategorySettingsStates.WaitingForCategoryKeywordAnswerType)
async def cb_cancel_cat_kw_add(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    category_name = data.get("current_category_name")
    await state.clear()
    await cb_manage_cat_keywords(callback, state)


@dp.callback_query(F.data == "cat_kw_choice_predefined", CategorySettingsStates.WaitingForCategoryKeywordAnswerType)
async def cb_cat_kw_choice_predefined(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    await callback.message.edit_text("Введите предустановленный ответ для этого ключевого слова категории:")
    await state.set_state(CategorySettingsStates.WaitingForCategoryKeywordPredefinedAnswer)
    await state.update_data(new_category_keyword_response_type="predefined")
    await callback.answer()


@dp.message(CategorySettingsStates.WaitingForCategoryKeywordPredefinedAnswer, F.text, ~F.text.startswith('/'))
async def process_cat_kw_predefined_answer(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    keyword = data.get("new_category_keyword")
    response_type = data.get("new_category_keyword_response_type")
    answer = message.text.strip()

    if not all([category_name, keyword, response_type]):
        await message.answer("Произошла ошибка, не все данные были сохранены. Попробуйте снова.",
                             reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return

    add_category_keyword(category_name, keyword, answer, response_type)
    await message.answer(f"Ключевое слово '{keyword}' с ответом добавлено для категории '{category_name}'.")
    await state.clear()
    fake_callback_query_message = message
    fake_callback_query_message.data = f"manage_cat_keywords:{category_name}"
    await message.answer("Возврат в меню управления ключевыми словами категории...",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="Перейти к ключам категории",
                                                   callback_data=f"manage_cat_keywords:{category_name}")]
                         ]))


@dp.callback_query(F.data == "cat_kw_choice_openai", CategorySettingsStates.WaitingForCategoryKeywordAnswerType)
async def cb_cat_kw_choice_openai(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    data = await state.get_data()
    category_name = data.get("current_category_name")
    keyword = data.get("new_category_keyword")

    if not all([category_name, keyword]):
        await callback.message.edit_text("Произошла ошибка, не все данные были сохранены. Попробуйте снова.",
                                         reply_markup=chat_categories_main_menu_keyboard())
        await state.clear();
        return

    add_category_keyword(category_name, keyword, "", "openai")
    await callback.message.edit_text(f"Ключевое слово '{keyword}' (тип AI) добавлено для категории '{category_name}'.")
    await state.clear()
    await callback.message.answer("Возврат в меню управления ключевыми словами категории...",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                      [InlineKeyboardButton(text="Перейти к ключам категории",
                                                            callback_data=f"manage_cat_keywords:{category_name}")]
                                  ]))
    await callback.answer()


@dp.callback_query(F.data.startswith("del_cat_keyword_start:"))
async def cb_del_cat_keyword_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    category_name = callback.data.split(":", 1)[1]
    await state.update_data(current_category_name=category_name)

    keywords = get_category_keywords(category_name)
    if not keywords:
        await callback.answer("У этой категории нет ключевых слов для удаления.", show_alert=True)
        return

    kb_buttons = []
    for kw_data in keywords:
        kb_buttons.append([InlineKeyboardButton(text=f"Удалить: {html.escape(kw_data['keyword'])}",
                                                callback_data=f"confirm_del_cat_kw:{category_name}:{kw_data['keyword']}")])
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"manage_cat_keywords:{category_name}")])

    await callback.message.edit_text(f"<b>Категория: {category_name}</b>\nВыберите ключевое слово для удаления:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
    await state.set_state(CategorySettingsStates.DeletingCategoryKeyword)
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_del_cat_kw:"), CategorySettingsStates.DeletingCategoryKeyword)
async def cb_confirm_del_cat_kw(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    try:
        _, category_name, keyword_to_delete = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Ошибка данных для удаления.", show_alert=True)
        await state.clear()
        return

    delete_category_keyword(category_name, keyword_to_delete)
    await callback.answer(f"Ключевое слово '{keyword_to_delete}' удалено из категории '{category_name}'.",
                          show_alert=True)
    await state.clear()
    fake_callback_query_message = callback.message
    fake_callback_query_message.data = f"manage_cat_keywords:{category_name}"
    await cb_manage_cat_keywords(callback, state)
