from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_active_workspace_name, get_db_connection


GREEN_DOT = "\U0001F7E2"
RED_DOT = "\U0001F534"
BACK_ARROW = "\u2B05\uFE0F"
RIGHT_ARROW = "\u27A1\uFE0F"

ENABLE_ALL_TEXT = "\u2705 \u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0432\u0441\u0435"
DISABLE_ALL_TEXT = "\u23F8\uFE0F \u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0432\u0441\u0435"
BACK_TO_GROUPS_TEXT = BACK_ARROW + " \u041d\u0430\u0437\u0430\u0434 \u0432 \u043c\u0435\u043d\u044e \u0433\u0440\u0443\u043f\u043f"
BACK_TO_CHANNELS_TEXT = BACK_ARROW + " \u041d\u0430\u0437\u0430\u0434 \u0432 \u043c\u0435\u043d\u044e \u043a\u0430\u043d\u0430\u043b\u043e\u0432"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="\U0001F5C4\uFE0F \u041F\u0440\u043E\u0441\u0442\u0440\u0430\u043D\u0441\u0442\u0432\u043E: " + get_active_workspace_name(),
                callback_data="workspaces_menu"
            )
        ],
        [
            InlineKeyboardButton(text="\u2699\uFE0F \u0410\u043a\u043a\u0430\u0443\u043d\u0442\u044b Telethon", callback_data="account_settings"),
            InlineKeyboardButton(text="\U0001F916 \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 AI", callback_data="ai_config_menu")
        ],
        [
            InlineKeyboardButton(text="\U0001F4FA \u041a\u0430\u043d\u0430\u043b\u044b", callback_data="channels_menu"),
            InlineKeyboardButton(text="\U0001F465 \u0413\u0440\u0443\u043f\u043f\u044b", callback_data="groups_menu")
        ],
        [
            InlineKeyboardButton(text="\U0001F5C2\uFE0F \u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u0438 \u0447\u0430\u0442\u043e\u0432", callback_data="chat_categories_main_menu"),
            InlineKeyboardButton(text="\U0001F4AC \u041e\u0442\u0432\u0435\u0442\u044b \u043d\u0430 \u0442\u0440\u0438\u0433\u0433\u0435\u0440\u044b", callback_data="set_answer")
        ],
        [
            InlineKeyboardButton(text="\u2699\uFE0F \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u043e\u0442\u0447\u0435\u0442\u043e\u0432", callback_data="reporting_settings_menu"),
            InlineKeyboardButton(text="\U0001F4CA \u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430 \u0438 \u043e\u0442\u0447\u0435\u0442\u044b", callback_data="analytics_menu")
        ],
        [
            InlineKeyboardButton(text="\U0001F570\uFE0F \u0420\u0435\u0433\u0443\u043b\u044f\u0440\u043d\u044b\u0439 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439", callback_data="regular_comment_menu"),
            InlineKeyboardButton(text="\U0001F6E0\uFE0F \u041e\u0431\u0449\u0438\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438", callback_data="general_settings_menu")
        ],
        [
            InlineKeyboardButton(text="\U0001F4CC \u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0430\u043c\u0438", callback_data="manage_tasks")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u274C \u041E\u0442\u043C\u0435\u043D\u0430", callback_data="cancel_action")]
    ])


def build_groups_keyboard(page: int = 1, per_page: int = 9) -> InlineKeyboardMarkup:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM groups")
    total_count = cursor.fetchone()["cnt"]
    total_pages = (total_count + per_page - 1) // per_page or 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    cursor.execute(
        "SELECT id, username, title, enabled, assigned_account_id FROM groups ORDER BY id LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    groups = cursor.fetchall()
    conn.close()

    kb_rows = []
    row_of_buttons = []
    for index, group in enumerate(groups):
        group_id = group["id"]
        group_title = group["title"] or group["username"] or str(group_id)
        indicator = GREEN_DOT if group["enabled"] else RED_DOT
        assigned_acc_info = f" (Acc: {group['assigned_account_id']})" if group["assigned_account_id"] else ""
        btn_text = f"{group_title[:15]}{assigned_acc_info} {indicator}"
        row_of_buttons.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_group:{group_id}:{page}"))
        if (index + 1) % 2 == 0:
            kb_rows.append(row_of_buttons)
            row_of_buttons = []

    if row_of_buttons:
        kb_rows.append(row_of_buttons)

    kb_rows.append([
        InlineKeyboardButton(text=ENABLE_ALL_TEXT, callback_data=f"groups_set_all:1:{page}"),
        InlineKeyboardButton(text=DISABLE_ALL_TEXT, callback_data=f"groups_set_all:0:{page}")
    ])

    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton(text=BACK_ARROW, callback_data=f"group_list_page:{page - 1}"))
    pagination_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton(text=RIGHT_ARROW, callback_data=f"group_list_page:{page + 1}"))

    if pagination_buttons:
        kb_rows.append(pagination_buttons)
    kb_rows.append([InlineKeyboardButton(text=BACK_TO_GROUPS_TEXT, callback_data="groups_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def build_channels_keyboard(page: int = 1, per_page: int = 9) -> InlineKeyboardMarkup:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM channels")
    total_count = cursor.fetchone()["cnt"]
    total_pages = (total_count + per_page - 1) // per_page or 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    cursor.execute(
        "SELECT id, username, title, enabled, assigned_account_id FROM channels ORDER BY id LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    channels = cursor.fetchall()
    conn.close()

    kb_rows = []
    row_of_buttons = []
    for index, channel in enumerate(channels):
        channel_id = channel["id"]
        channel_title = channel["title"] or channel["username"] or str(channel_id)
        indicator = GREEN_DOT if channel["enabled"] else RED_DOT
        assigned_acc_info = f" (Acc: {channel['assigned_account_id']})" if channel["assigned_account_id"] else ""
        btn_text = f"{channel_title[:15]}{assigned_acc_info} {indicator}"

        row_of_buttons.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"toggle_channel:{channel_id}:{page}"
            )
        )
        if (index + 1) % 2 == 0:
            kb_rows.append(row_of_buttons)
            row_of_buttons = []

    if row_of_buttons:
        kb_rows.append(row_of_buttons)

    kb_rows.append([
        InlineKeyboardButton(text=ENABLE_ALL_TEXT, callback_data=f"channels_set_all:1:{page}"),
        InlineKeyboardButton(text=DISABLE_ALL_TEXT, callback_data=f"channels_set_all:0:{page}")
    ])

    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton(text=BACK_ARROW, callback_data=f"channel_list_page:{page - 1}"))
    pagination_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton(text=RIGHT_ARROW, callback_data=f"channel_list_page:{page + 1}"))

    if pagination_buttons:
        kb_rows.append(pagination_buttons)
    kb_rows.append([InlineKeyboardButton(text=BACK_TO_CHANNELS_TEXT, callback_data="channels_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)
