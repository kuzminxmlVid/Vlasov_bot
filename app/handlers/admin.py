from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.channel_access import (
    provision_tribute_channel_access,
    sync_expired_tribute_members,
)
from app.config import Config
from app.database import Database

router = Router(name="admin")
VALID_SLUGS = {"acoustic", "studio", "base"}


class LessonUpload(StatesGroup):
    waiting_for_media = State()


@dataclass(slots=True)
class IncomingMedia:
    media_type: str
    file_id: str


def _is_admin(message: Message, config: Config) -> bool:
    return bool(message.from_user and message.from_user.id in config.admin_ids)


def _extract_media(message: Message) -> IncomingMedia | None:
    if message.video:
        return IncomingMedia("video", message.video.file_id)
    if message.document:
        return IncomingMedia("document", message.document.file_id)
    if message.audio:
        return IncomingMedia("audio", message.audio.file_id)
    if message.photo:
        return IncomingMedia("photo", message.photo[-1].file_id)
    if message.animation:
        return IncomingMedia("animation", message.animation.file_id)
    return None


@router.message(Command("admin"))
async def admin_help(message: Message, config: Config) -> None:
    if not _is_admin(message, config):
        return
    await message.answer(
        "<b>Управление ботом</b>\n\n"
        "<code>/setlesson acoustic</code> — загрузить видео «Акустика 1»\n"
        "<code>/setlesson studio</code> — загрузить видео «Студия 1»\n"
        "<code>/setlesson base</code> — загрузить видео «База 1»\n\n"
        "После команды отправьте боту видео или файл. Подпись к медиа станет описанием урока.\n\n"
        "<code>/settext acoustic Новый текст</code> — изменить описание\n"
        "<code>/clearlesson acoustic</code> — убрать медиа\n"
        "<code>/setboosty https://...</code> — изменить ссылку Boosty\n"
        "<code>/settribute https://...</code> — изменить ссылку Tribute\n"
        "<code>/access 123456789</code> — проверить доступ пользователя\n"
        "<code>/channelinvite 123456789</code> — повторно выдать вход в Tribute-канал\n"
        "<code>/channelstatus</code> — проверить настройку закрытого канала\n"
        "<code>/syncchannel</code> — удалить пользователей с истёкшим доступом\n"
        "<code>/grant 123456789</code> — выдать ручной доступ в базе\n"
        "<code>/stats</code> — статистика\n"
        "<code>/cancel</code> — отменить загрузку"
    )


@router.message(Command("setlesson"))
async def set_lesson_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    slug = (command.args or "").strip().lower()
    if slug not in VALID_SLUGS:
        await message.answer("Укажите урок: acoustic, studio или base.")
        return
    await state.set_state(LessonUpload.waiting_for_media)
    await state.update_data(slug=slug)
    await message.answer(
        f"Отправьте видео или файл для урока <code>{slug}</code>.\n"
        "Подпись к сообщению, если она есть, заменит описание урока."
    )


@router.message(Command("cancel"))
async def cancel_upload(message: Message, state: FSMContext, config: Config) -> None:
    if not _is_admin(message, config):
        return
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(LessonUpload.waiting_for_media)
async def save_lesson_media(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    media = _extract_media(message)
    if media is None:
        await message.answer("Нужно отправить видео, аудио, фото, анимацию или документ.")
        return

    data = await state.get_data()
    slug = data.get("slug")
    if slug not in VALID_SLUGS:
        await state.clear()
        await message.answer("Состояние загрузки потеряно. Повторите /setlesson.")
        return

    description = message.caption.strip() if message.caption else None
    updated = await db.set_lesson_media(
        slug=slug,
        media_type=media.media_type,
        file_id=media.file_id,
        description=description,
    )
    await state.clear()
    if updated:
        await message.answer(f"Урок <code>{slug}</code> обновлён.")
    else:
        await message.answer("Урок не найден.")


@router.message(Command("settext"))
async def set_lesson_text(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    raw = (command.args or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() not in VALID_SLUGS:
        await message.answer(
            "Формат: <code>/settext acoustic Текст описания урока</code>"
        )
        return
    slug, description = parts[0].lower(), parts[1].strip()
    if not description:
        await message.answer("Описание не может быть пустым.")
        return
    updated = await db.set_lesson_text(slug, description)
    await message.answer("Описание обновлено." if updated else "Урок не найден.")


@router.message(Command("clearlesson"))
async def clear_lesson(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    slug = (command.args or "").strip().lower()
    if slug not in VALID_SLUGS:
        await message.answer("Укажите урок: acoustic, studio или base.")
        return
    updated = await db.clear_lesson_media(slug)
    await message.answer("Медиа удалено." if updated else "Урок не найден.")


@router.message(Command("setboosty"))
async def set_boosty(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    url = (command.args or "").strip()
    if not url.startswith(("https://", "http://")):
        await message.answer(
            "Формат: <code>/setboosty https://boosty.to/...</code>"
        )
        return
    await db.set_setting("boosty_url", url)
    await message.answer("Ссылка Boosty обновлена.")


@router.message(Command("settribute"))
async def set_tribute(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    url = (command.args or "").strip()
    if not url.startswith(("https://", "http://")):
        await message.answer(
            "Формат: <code>/settribute https://t.me/tribute/app?startapp=...</code>"
        )
        return
    await db.set_setting("tribute_url", url)
    await message.answer("Ссылка Tribute обновлена.")


@router.message(Command("access"))
async def access_status(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    raw = (command.args or "").strip()
    try:
        telegram_id = int(raw)
    except ValueError:
        await message.answer("Формат: <code>/access 123456789</code>")
        return
    status = await db.get_access_status(telegram_id)
    if not status.active:
        await message.answer("Активного доступа нет.")
        return
    if status.expires_at is None:
        await message.answer(f"Доступ активен. Источник: <b>{status.provider}</b>.")
        return
    await message.answer(
        f"Доступ активен до <b>{status.expires_at.astimezone():%d.%m.%Y %H:%M}</b>. "
        f"Источник: <b>{status.provider}</b>."
    )


@router.message(Command("channelstatus"))
async def channel_status(
    message: Message,
    bot: Bot,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    try:
        chat = await bot.get_chat(config.tribute_channel_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(config.tribute_channel_id, me.id)
    except TelegramAPIError as exc:
        await message.answer(
            "Не удалось открыть канал. Проверьте <code>TRIBUTE_CHANNEL_ID</code> "
            "и добавьте бота администратором.\n\n"
            f"Ошибка: <code>{type(exc).__name__}</code>"
        )
        return

    is_admin = member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }
    await message.answer(
        "<b>Tribute-канал</b>\n\n"
        f"Название: <b>{chat.title or 'без названия'}</b>\n"
        f"ID: <code>{config.tribute_channel_id}</code>\n"
        f"Бот — администратор: <b>{'да' if is_admin else 'нет'}</b>\n"
        f"Может приглашать: <b>{'да' if getattr(member, 'can_invite_users', False) else 'нет'}</b>\n"
        f"Может удалять: <b>{'да' if getattr(member, 'can_restrict_members', False) else 'нет'}</b>"
    )


@router.message(Command("channelinvite"))
async def channel_invite(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    try:
        telegram_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: <code>/channelinvite 123456789</code>")
        return
    access = await db.get_tribute_access_status(telegram_id)
    if not access.active:
        await message.answer("У пользователя нет активной оплаты Tribute.")
        return
    result = await provision_tribute_channel_access(
        bot=bot,
        db=db,
        config=config,
        telegram_id=telegram_id,
        access=access,
        intro_text="<b>Доступ к закрытому каналу активен.</b>",
    )
    await message.answer(
        "Ссылка отправлена пользователю." if result.ok else "Отправить ссылку не удалось."
    )


@router.message(Command("syncchannel"))
async def sync_channel(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    removed = await sync_expired_tribute_members(bot, db, config)
    await message.answer(f"Синхронизация завершена. Удалено: <b>{removed}</b>.")


@router.message(Command("grant"))
async def grant_access(
    message: Message,
    command: CommandObject,
    db: Database,
    config: Config,
) -> None:
    if not _is_admin(message, config):
        return
    raw = (command.args or "").strip()
    try:
        telegram_id = int(raw)
    except ValueError:
        await message.answer("Формат: <code>/grant 123456789</code>")
        return
    await db.grant_manual_access(telegram_id)
    await message.answer("Ручной доступ выдан.")


@router.message(Command("stats"))
async def stats(message: Message, db: Database, config: Config) -> None:
    if not _is_admin(message, config):
        return
    data = await db.get_stats()
    lines = [
        "<b>Статистика</b>",
        "",
        f"Пользователей: <b>{data['users_total']}</b>",
        f"Посмотрели все 3 урока: <b>{data['trial_completed']}</b>",
        f"Открытий экрана оплаты: <b>{data['purchase_screens']}</b>",
        f"Активных доступов Tribute: <b>{data['active_tribute']}</b>",
        "",
        "<b>Уроки</b>",
    ]
    for lesson in data["lessons"]:
        lines.append(
            f"{lesson['title']}: {lesson['unique_viewers']} пользователей, "
            f"{lesson['total_views']} просмотров"
        )
    await message.answer("\n".join(lines))
