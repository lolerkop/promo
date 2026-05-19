import html
import logging
import os

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from services.account_profile import (get_account_about, update_bio,
                                      update_first_name, update_last_name,
                                      update_profile_photo)
from userbot import get_active_clients

from ..bot_instance import TEMP_PHOTO_DIR, bot, dp
from ..states import AccountManagementStates
from ..utils import user_is_allowed

logger = logging.getLogger(__name__)


def _get_runtime_client_for_account(account_id: int):
    for client, client_data in get_active_clients():
        if client_data.get("id") == account_id and client.is_connected():
            return client, client_data
    return None, None


def _account_back_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back to account", callback_data=f"manage_account:{account_id}")]
    ])

@dp.callback_query(F.data.startswith("profile_edit_menu:"))
async def cb_profile_edit_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("No access.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid account id.", show_alert=True)
        return

    client, client_data = _get_runtime_client_for_account(account_id)
    if not client:
        await callback.message.edit_text(
            "This account is not connected. Restart/check the session first.",
            reply_markup=_account_back_keyboard(account_id)
        )
        await callback.answer()
        return

    me = await client.get_me()
    display_name = " ".join(filter(None, [me.first_name, me.last_name])) or client_data.get("label", str(account_id))
    try:
        about = await get_account_about(client)
    except Exception as e:
        logger.warning(f"Failed to load account bio: {e}")
        about = "-"
    await state.update_data(current_managing_account_id=account_id)

    text = (
        f"Edit profile for <b>{html.escape(display_name)}</b> (ID: {account_id})\n"
        f"Username: @{html.escape(me.username) if me.username else '-'}\n"
        f"Bio: <pre>{html.escape(about)}</pre>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="First name", callback_data=f"profile_set_first:{account_id}")],
        [InlineKeyboardButton(text="Last name", callback_data=f"profile_set_last:{account_id}")],
        [InlineKeyboardButton(text="Bio / description", callback_data=f"profile_set_bio:{account_id}")],
        [InlineKeyboardButton(text="Avatar", callback_data=f"profile_set_photo:{account_id}")],
        [InlineKeyboardButton(text="Back to account", callback_data=f"manage_account:{account_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("profile_set_first:"))
async def cb_profile_set_first(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new first name.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileFirstName)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileFirstName, F.text, ~F.text.startswith('/'))
async def process_profile_first_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    try:
        await update_first_name(client, message.text)
        await message.answer("First name updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update first name: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_last:"))
async def cb_profile_set_last(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new last name. Send '-' to clear it.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileLastName)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileLastName, F.text, ~F.text.startswith('/'))
async def process_profile_last_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    last_name = "" if message.text.strip() == "-" else message.text.strip()
    try:
        await update_last_name(client, last_name)
        await message.answer("Last name updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update last name: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_bio:"))
async def cb_profile_set_bio(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new bio/description. Send '-' to clear it.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileBio)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileBio, F.text, ~F.text.startswith('/'))
async def process_profile_bio(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    bio = "" if message.text.strip() == "-" else message.text.strip()
    if len(bio) > 70:
        await message.answer("Bio is too long. Telegram user bio is usually limited to 70 characters.",
                             reply_markup=_account_back_keyboard(account_id))
        return
    try:
        await update_bio(client, bio)
        await message.answer("Bio updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update bio: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_photo:"))
async def cb_profile_set_photo(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send a photo for the new avatar.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfilePhoto)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfilePhoto, F.photo)
async def process_profile_photo(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return

    os.makedirs(TEMP_PHOTO_DIR, exist_ok=True)
    photo_path = os.path.join(TEMP_PHOTO_DIR, f"single_profile_{account_id}_{message.message_id}.jpg")
    try:
        await bot.download(message.photo[-1], destination=photo_path)
        await update_profile_photo(client, photo_path)
        await message.answer("Avatar updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update avatar: {e}", reply_markup=_account_back_keyboard(account_id))
    finally:
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception:
                pass
        await state.clear()


@dp.message(AccountManagementStates.WaitingForProfilePhoto, ~F.text.startswith('/'))
async def process_profile_photo_invalid(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    await message.answer("Please send an image/photo.", reply_markup=_account_back_keyboard(account_id))

