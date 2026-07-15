from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.channel_access import provision_tribute_channel_access
from app.config import Config
from app.database import AccessStatus, Database, Lesson
from app.keyboards import (
    lesson_keyboard,
    menu_keyboard,
    purchase_keyboard,
    start_keyboard,
    trial_finished_keyboard,
)

router = Router(name="user")
logger = logging.getLogger(__name__)

START_TEXT = (
    "<b>Добро пожаловать!</b>\n\n"
    "Здесь доступны три бесплатных вводных урока: об акустике, студии "
    "и базовых принципах работы со звуком.\n\n"
    "После просмотра можно оформить полный доступ через Tribute внутри Telegram "
    "или подписаться через Boosty."
)

MENU_TEXT = (
    "<b>Доступные видео</b>\n\n"
    "Три бесплатных урока можно смотреть в любом порядке и возвращаться "
    "к ним в любое время.\n\n"
    "Что хотите посмотреть?"
)

PURCHASE_TEXT = (
    "<b>Получить полный доступ</b>\n\n"
    "Выберите удобный вариант:\n\n"
    "• <b>Tribute</b> — оплата внутри Telegram и доступ в отдельный закрытый канал. "
    "После оплаты бот пришлёт кнопку для вступления.\n"
    "• <b>Boosty</b> — подписка через Boosty с доступом в уже привязанный канал или чат."
)

TRIAL_FINISHED_TEXT = (
    "<b>Вы посмотрели все три бесплатных урока.</b>\n\n"
    "Полный доступ можно оформить через Tribute внутри Telegram или через Boosty."
)


async def _safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _ensure_user(message: Message, db: Database) -> None:
    if message.from_user is not None:
        await db.upsert_user(message.from_user)


def _format_access(status: AccessStatus) -> str:
    if not status.active:
        return (
            "<b>Оплаченный доступ Tribute пока не найден.</b>\n\n"
            "Если вы только что оплатили, подождите несколько секунд и нажмите проверку ещё раз."
        )
    if status.expires_at is None:
        return "<b>Доступ активен.</b> Оплата через Tribute подтверждена."
    local = status.expires_at.astimezone()
    return (
        "<b>Доступ активен.</b>\n\n"
        f"Оплачен до: <b>{local:%d.%m.%Y %H:%M}</b>."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    await _ensure_user(message, db)
    if message.from_user is not None:
        await db.record_event(message.from_user.id, "bot_started")
    await message.answer(START_TEXT, reply_markup=start_keyboard())


@router.callback_query(F.data == "menu:open")
async def open_menu(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return
    await db.upsert_user(callback.from_user)
    await db.record_event(callback.from_user.id, "menu_opened")
    await _safe_delete(callback.message)
    await callback.bot.send_message(
        callback.from_user.id,
        MENU_TEXT,
        reply_markup=menu_keyboard(),
    )


async def _send_lesson(
    bot: Bot,
    chat_id: int,
    lesson: Lesson,
) -> None:
    title = html.escape(lesson.title)
    description = html.escape(lesson.description)
    caption = f"<b>{title}</b>\n\n{description}"
    keyboard = lesson_keyboard(lesson.slug)

    if not lesson.media_type or not lesson.file_id:
        await bot.send_message(chat_id, caption, reply_markup=keyboard)
        return

    senders = {
        "video": bot.send_video,
        "document": bot.send_document,
        "audio": bot.send_audio,
        "photo": bot.send_photo,
        "animation": bot.send_animation,
    }
    sender = senders.get(lesson.media_type)
    if sender is None:
        logger.warning("Неизвестный тип медиа %s для урока %s", lesson.media_type, lesson.slug)
        await bot.send_message(chat_id, caption, reply_markup=keyboard)
        return

    media_arg_names = {
        "video": "video",
        "document": "document",
        "audio": "audio",
        "photo": "photo",
        "animation": "animation",
    }
    media_kwargs = {media_arg_names[lesson.media_type]: lesson.file_id}

    if len(caption) <= 900:
        await sender(
            chat_id=chat_id,
            caption=caption,
            reply_markup=keyboard,
            **media_kwargs,
        )
    else:
        await sender(chat_id=chat_id, **media_kwargs)
        await bot.send_message(chat_id, caption, reply_markup=keyboard)


@router.callback_query(F.data.startswith("lesson:"))
async def open_lesson(callback: CallbackQuery, db: Database) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    slug = callback.data.split(":", maxsplit=1)[1]
    lesson = await db.get_lesson(slug)
    if lesson is None:
        await callback.answer("Урок не найден", show_alert=True)
        return

    await callback.answer()
    await db.upsert_user(callback.from_user)
    await db.record_event(callback.from_user.id, "lesson_opened", lesson_slug=slug)
    _, newly_completed = await db.mark_lesson_viewed(callback.from_user.id, slug)

    await _safe_delete(callback.message)
    await _send_lesson(callback.bot, callback.from_user.id, lesson)

    if newly_completed:
        await db.record_event(callback.from_user.id, "trial_completed")
        await callback.bot.send_message(
            callback.from_user.id,
            TRIAL_FINISHED_TEXT,
            reply_markup=trial_finished_keyboard(),
        )


@router.callback_query(F.data == "purchase:open")
async def open_purchase(callback: CallbackQuery, db: Database) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    boosty_url = await db.get_setting("boosty_url")
    tribute_url = await db.get_setting("tribute_url")
    if not boosty_url or not tribute_url:
        await callback.answer("Ссылки оплаты ещё не настроены", show_alert=True)
        return

    await callback.answer()
    await db.upsert_user(callback.from_user)
    await db.record_event(callback.from_user.id, "purchase_screen_opened")
    await _safe_delete(callback.message)
    await callback.bot.send_message(
        callback.from_user.id,
        PURCHASE_TEXT,
        reply_markup=purchase_keyboard(boosty_url, tribute_url),
    )


@router.callback_query(F.data == "access:check")
async def check_access(
    callback: CallbackQuery,
    db: Database,
    config: Config,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    await callback.answer()
    await db.upsert_user(callback.from_user)
    status = await db.get_tribute_access_status(callback.from_user.id)
    await db.record_event(
        callback.from_user.id,
        "access_checked",
        metadata={"active": status.active, "provider": status.provider},
    )
    if not status.active:
        await callback.bot.send_message(callback.from_user.id, _format_access(status))
        return
    await provision_tribute_channel_access(
        bot=callback.bot,
        db=db,
        config=config,
        telegram_id=callback.from_user.id,
        access=status,
        intro_text=_format_access(status),
    )


@router.message()
async def fallback(message: Message, db: Database) -> None:
    await _ensure_user(message, db)
    await message.answer(
        "Используйте кнопки ниже, чтобы открыть бесплатные уроки.",
        reply_markup=menu_keyboard(),
    )
