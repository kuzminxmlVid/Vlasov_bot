from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config
from app.database import AccessStatus, Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProvisionResult:
    ok: bool
    already_member: bool = False
    invite_link: str | None = None


def _is_member_status(status: ChatMemberStatus | str) -> bool:
    return status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }


async def validate_channel_setup(bot: Bot, config: Config) -> None:
    """Проверяет канал при запуске и пишет понятную ошибку в лог."""
    try:
        chat = await bot.get_chat(config.tribute_channel_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(config.tribute_channel_id, me.id)
    except TelegramAPIError:
        logger.exception(
            "Не удалось открыть Tribute-канал %s. Проверьте TRIBUTE_CHANNEL_ID "
            "и добавьте бота администратором.",
            config.tribute_channel_id,
        )
        return

    if member.status not in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }:
        logger.error(
            "Бот не является администратором Tribute-канала %s (%s).",
            config.tribute_channel_id,
            getattr(chat, "title", "без названия"),
        )
        return

    can_invite = bool(getattr(member, "can_invite_users", False))
    can_restrict = bool(getattr(member, "can_restrict_members", False))
    if not can_invite:
        logger.error("У бота нет права приглашать пользователей в Tribute-канал.")
    if not can_restrict:
        logger.warning(
            "У бота нет права блокировать пользователей. Он сможет выдавать доступ, "
            "но не сможет автоматически удалять людей после окончания подписки."
        )

    logger.info(
        "Tribute-канал подключён: %s (%s). invite=%s, restrict=%s",
        getattr(chat, "title", "без названия"),
        config.tribute_channel_id,
        can_invite,
        can_restrict,
    )


async def provision_tribute_channel_access(
    bot: Bot,
    db: Database,
    config: Config,
    telegram_id: int,
    access: AccessStatus,
    intro_text: str | None = None,
) -> ProvisionResult:
    """Отправляет плательщику защищённую ссылку-заявку в отдельный канал."""
    if not access.active:
        return ProvisionResult(ok=False)

    try:
        member = await bot.get_chat_member(config.tribute_channel_id, telegram_id)
        if _is_member_status(member.status):
            await db.mark_channel_joined(telegram_id, config.tribute_channel_id)
            text = intro_text or "<b>Доступ к закрытому каналу активен.</b>"
            if "канал" not in text.lower():
                text += "\n\nВы уже состоите в закрытом канале."
            await _safe_send(bot, telegram_id, text)
            return ProvisionResult(ok=True, already_member=True)
    except TelegramAPIError:
        # Для пользователя, который ещё не вступал, Telegram может вернуть ошибку.
        logger.debug("Пользователь %s пока не состоит в канале", telegram_id)

    try:
        # Если пользователь раньше был удалён, разрешаем ему вступить снова после новой оплаты.
        await bot.unban_chat_member(
            chat_id=config.tribute_channel_id,
            user_id=telegram_id,
            only_if_banned=True,
        )
    except TelegramAPIError:
        logger.debug("Не удалось снять возможный бан с %s", telegram_id, exc_info=True)

    expires_at = datetime.now(timezone.utc) + timedelta(hours=config.tribute_invite_hours)
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=config.tribute_channel_id,
            name=f"tribute-{str(telegram_id)[-20:]}",
            expire_date=expires_at,
            creates_join_request=True,
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось создать ссылку в Tribute-канал для пользователя %s",
            telegram_id,
        )
        await _safe_send(
            bot,
            telegram_id,
            "<b>Оплата подтверждена, но ссылку в канал создать не удалось.</b>\n\n"
            "Администратор уже получил информацию об ошибке. Попробуйте нажать "
            "«Проверить оплату Tribute» чуть позже.",
        )
        return ProvisionResult(ok=False)

    await db.save_channel_invite(
        telegram_id=telegram_id,
        channel_id=config.tribute_channel_id,
        invite_link=invite.invite_link,
        invite_expires_at=expires_at,
    )
    await db.record_event(
        telegram_id,
        "tribute_channel_invite_created",
        metadata={"channel_id": config.tribute_channel_id},
    )

    text = intro_text or "<b>Оплата через Tribute подтверждена.</b>"
    text += (
        "\n\nНажмите кнопку ниже и отправьте заявку на вступление. "
        "Бот проверит оплату и одобрит её автоматически.\n\n"
        f"Ссылка действует {config.tribute_invite_hours} ч."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вступить в закрытый канал", url=invite.invite_link)]
        ]
    )
    await _safe_send(bot, telegram_id, text, reply_markup=keyboard)
    return ProvisionResult(ok=True, invite_link=invite.invite_link)


async def remove_from_tribute_channel(
    bot: Bot,
    db: Database,
    config: Config,
    telegram_id: int,
    notify: bool = False,
) -> bool:
    """Удаляет пользователя из канала и сразу снимает бан для будущей оплаты."""
    try:
        member = await bot.get_chat_member(config.tribute_channel_id, telegram_id)
        if not _is_member_status(member.status):
            await db.mark_channel_removed(telegram_id, config.tribute_channel_id)
            return True
    except TelegramAPIError:
        logger.exception(
            "Не удалось проверить пользователя %s в Tribute-канале",
            telegram_id,
        )
        return False

    try:
        await bot.ban_chat_member(
            chat_id=config.tribute_channel_id,
            user_id=telegram_id,
            revoke_messages=False,
        )
        await bot.unban_chat_member(
            chat_id=config.tribute_channel_id,
            user_id=telegram_id,
            only_if_banned=True,
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось удалить пользователя %s из Tribute-канала",
            telegram_id,
        )
        return False

    await db.mark_channel_removed(telegram_id, config.tribute_channel_id)
    await db.record_event(
        telegram_id,
        "tribute_channel_access_removed",
        metadata={"channel_id": config.tribute_channel_id},
    )
    if notify:
        await _safe_send(
            bot,
            telegram_id,
            "Срок доступа через Tribute закончился. Вы удалены из закрытого канала.",
        )
    return True


async def sync_expired_tribute_members(
    bot: Bot,
    db: Database,
    config: Config,
) -> int:
    telegram_ids = await db.list_inactive_tribute_users_for_channel(
        config.tribute_channel_id
    )
    removed = 0
    for telegram_id in telegram_ids:
        if await remove_from_tribute_channel(
            bot, db, config, telegram_id, notify=True
        ):
            removed += 1
    return removed


async def run_channel_sync_loop(bot: Bot, db: Database, config: Config) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            removed = await sync_expired_tribute_members(bot, db, config)
            if removed:
                logger.info("Из Tribute-канала удалено пользователей: %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой синхронизации Tribute-канала")
        await asyncio.sleep(config.channel_sync_minutes * 60)


async def _safe_send(
    bot: Bot,
    telegram_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await bot.send_message(telegram_id, text, reply_markup=reply_markup)
    except TelegramAPIError:
        logger.exception("Не удалось отправить сообщение пользователю %s", telegram_id)
