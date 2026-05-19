from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputUserSelf


async def get_account_about(client: TelegramClient) -> str:
	full_user = await client(GetFullUserRequest(InputUserSelf()))
	return getattr(full_user.full_user, "about", None) or "-"


async def update_first_name(client: TelegramClient, first_name: str):
	return await client(UpdateProfileRequest(first_name=(first_name or "").strip()))


async def update_last_name(client: TelegramClient, last_name: str):
	return await client(UpdateProfileRequest(last_name=last_name or ""))


async def update_bio(client: TelegramClient, bio: str):
	return await client(UpdateProfileRequest(about=bio or ""))


async def update_profile_photo(client: TelegramClient, photo_path: str):
	uploaded_file = await client.upload_file(photo_path)
	return await client(UploadProfilePhotoRequest(file=uploaded_file))
