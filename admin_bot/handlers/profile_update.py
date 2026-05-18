import asyncio
import logging
import os

from aiogram import F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..bot_instance import dp, bot, TEMP_PHOTO_DIR
from ..states import MassProfileUpdateStates
from ..utils import user_is_allowed, execute_mass_profile_update


@dp.callback_query(F.data == "mass_profile_update")
async def cb_mass_profile_update(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return
    if not os.path.exists(TEMP_PHOTO_DIR):
        try:
            os.makedirs(TEMP_PHOTO_DIR)
        except OSError as e:
            await callback.message.answer(f"Не удалось создать временную директорию для фото: {e}")  # type: ignore
            await callback.answer()
            return

    await callback.message.edit_text("Введите новый <b>First Name</b> для всех аккаунтов:")  # type: ignore
    await state.set_state(MassProfileUpdateStates.WaitingForFirstName)
    await callback.answer()


@dp.message(MassProfileUpdateStates.WaitingForFirstName, F.text)
async def process_mass_first_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return  # type: ignore
    await state.update_data(first_name=message.text.strip())  # type: ignore
    await message.answer("Введите новый <b>Last Name</b> (или отправьте '<code>пропустить</code>', чтобы не менять):")
    await state.set_state(MassProfileUpdateStates.WaitingForLastName)


@dp.message(MassProfileUpdateStates.WaitingForLastName, F.text)
async def process_mass_last_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return  # type: ignore
    if message.text.strip().lower() == "пропустить":  # type: ignore
        await state.update_data(last_name=None)
    else:
        await state.update_data(last_name=message.text.strip())  # type: ignore
    await message.answer(
        "Введите новый <b>Bio/About</b> (описание профиля, до 70 символов. Или отправьте '<code>пропустить</code>', чтобы не менять):")
    await state.set_state(MassProfileUpdateStates.WaitingForBio)


@dp.message(MassProfileUpdateStates.WaitingForBio, F.text)
async def process_mass_bio(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return  # type: ignore
    bio_text = message.text.strip()  # type: ignore
    if bio_text.lower() == "пропустить":
        await state.update_data(bio=None)
    elif len(bio_text) > 70:
        await message.answer("Bio слишком длинное (макс 70 символов). Попробуйте снова или напишите 'пропустить'.")
        return
    else:
        await state.update_data(bio=bio_text)
    await message.answer(
        "Отправьте новую <b>фотографию профиля</b> (или отправьте '<code>пропустить</code>', чтобы не менять):")
    await state.set_state(MassProfileUpdateStates.WaitingForPhoto)


@dp.message(MassProfileUpdateStates.WaitingForPhoto, F.photo | (F.text.strip().lower() == "пропустить"))  # type: ignore
async def process_mass_photo(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return  # type: ignore
    user_data = await state.get_data()
    photo_path_to_set = user_data.get("photo_path")

    if message.photo:
        photo_file = message.photo[-1]
        if photo_path_to_set and os.path.exists(photo_path_to_set):
            try:
                os.remove(photo_path_to_set)
            except Exception as e:
                logging.error(f"Error removing old temp photo {photo_path_to_set}: {e}")

        new_photo_path = os.path.join(TEMP_PHOTO_DIR,
                                      f"{message.from_user.id}_{photo_file.file_unique_id}.jpg")  # type: ignore
        await bot.download(file=photo_file.file_id, destination=new_photo_path)
        await state.update_data(photo_path=new_photo_path)
        await message.answer(f"Фото получено.")
    elif message.text and message.text.strip().lower() == "пропустить":  # type: ignore
        if photo_path_to_set and os.path.exists(photo_path_to_set):  # type: ignore
            try:
                os.remove(photo_path_to_set)  # type: ignore
            except Exception as e:
                logging.error(f"Error removing temp photo on skip {photo_path_to_set}: {e}")  # type: ignore
        await state.update_data(photo_path=None)

    kb_username = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, попытаться установить случайные", callback_data="username_choice_random")],
        [InlineKeyboardButton(text="⏭️ Пропустить обновление username", callback_data="username_choice_skip")]
    ])
    await message.answer(
        "Хотите попытаться установить/обновить <b>Username</b> (@имяпользователя) для аккаунтов на случайные значения?",
        reply_markup=kb_username)
    await state.set_state(MassProfileUpdateStates.WaitingForUsernameChoice)


@dp.message(MassProfileUpdateStates.WaitingForPhoto)
async def process_mass_photo_invalid(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return  # type: ignore
    await message.answer("Пожалуйста, отправьте фото или напишите '<code>пропустить</code>'.")


@dp.callback_query(MassProfileUpdateStates.WaitingForUsernameChoice,
                   F.data.in_({"username_choice_random", "username_choice_skip"}))
async def process_username_choice(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return

    if callback.data == "username_choice_random":
        await state.update_data(update_username_strategy="random")
    else:  # username_choice_skip
        await state.update_data(update_username_strategy="skip")

    user_data_updated = await state.get_data()

    confirm_text = "<b>Подтвердите изменения:</b>\n"
    confirm_text += f"First Name: {user_data_updated.get('first_name')}\n"
    confirm_text += f"Last Name: {user_data_updated.get('last_name', '<i>Не меняется</i>')}\n"
    confirm_text += f"Bio: {user_data_updated.get('bio', '<i>Не меняется</i>')}\n"
    confirm_text += f"Photo: {'<i>Новое фото будет установлено</i>' if user_data_updated.get('photo_path') else '<i>Не меняется</i>'}\n"

    username_strategy = user_data_updated.get('update_username_strategy', 'skip')
    if username_strategy == "random":
        confirm_text += "Username: <i>Будет попытка установить случайные значения</i>\n"
    else:
        confirm_text += "Username: <i>Не меняется</i>\n"

    confirm_text += "\nПрименить эти изменения ко всем активным аккаунтам?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, применить", callback_data="mass_update_confirm_yes")],
        [InlineKeyboardButton(text="❌ Нет, отменить", callback_data="mass_update_confirm_no")]
    ])
    await callback.message.edit_text(confirm_text, reply_markup=kb)  # type: ignore
    await state.set_state(MassProfileUpdateStates.ConfirmUpdate)
    await callback.answer()


@dp.callback_query(MassProfileUpdateStates.ConfirmUpdate,
                   F.data.in_({"mass_update_confirm_yes", "mass_update_confirm_no"}))
async def process_mass_update_confirmation(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):  # type: ignore
        await callback.answer("Нет прав.")
        return

    user_data = await state.get_data()
    photo_p = user_data.get('photo_path')
    reply_kb_acc_settings = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]])

    if callback.data == "mass_update_confirm_yes":
        await callback.message.edit_text("Подтверждено. Запускаю фоновую задачу обновления профилей...")  # type: ignore
        asyncio.create_task(execute_mass_profile_update(
            admin_chat_id=callback.message.chat.id,  # type: ignore
            first_name=user_data.get('first_name'),
            last_name=user_data.get('last_name'),
            bio=user_data.get('bio'),
            photo_file_path=photo_p,
            username_update_strategy=user_data.get('update_username_strategy', 'skip')
        ))
    else:  # mass_update_confirm_no
        await callback.message.edit_text("Массовое обновление отменено.",
                                         reply_markup=reply_kb_acc_settings)  # type: ignore
        if photo_p and os.path.exists(photo_p):
            try:
                os.remove(photo_p)
            except Exception as e:
                logging.error(f"Не удалось удалить временный файл фото при отмене: {photo_p}: {e}")

    await state.clear()
    await callback.answer()
