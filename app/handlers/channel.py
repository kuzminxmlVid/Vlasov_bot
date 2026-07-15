from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from app.config import Config
from app.database import Database

router = Router(name="tribute_channel")
logger = logging.getLogger(__name__)


@router.chat_join_request()
async def handle_join_request(
    request: ChatJoinRequest,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if request.chat.id != config.tribute_channel_id:
        return

    telegram_id = request.from_user.id
    await db.upsert_user(request.from_user)
    access = await db.get_tribute_access_status(telegram_id)

    if not access.active:
        try:
            await bot.decline_chat_join_request(
                chat_id=config.tribute_channel_id,
                user_id=telegram_id,
            )
        except TelegramAPIError:
            logger.exception("Не удалось отклонить заявку пользователя %s", telegram_id)
        await db.mark_channel_denied(telegram_id, config.tribute_channel_id)
        await db.record_event(
            telegram_id,
            "tribute_channel_join_declined",
            metadata={"channel_id": config.tribute_channel_id},
        )
        try:
            await bot.send_message(
                request.user_chat_id,
                "Заявка отклонена: активная оплата Tribute не найдена. "
                "Вернитесь в бот и нажмите «Проверить оплату Tribute».",
            )
        except TelegramAPIError:
            logger.debug("Не удалось уведомить пользователя %s", telegram_id)
        return

    try:
        await bot.approve_chat_join_request(
            chat_id=config.tribute_channel_id,
            user_id=telegram_id,
        )
    except TelegramAPIError:
        logger.exception("Не удалось одобрить заявку пользователя %s", telegram_id)
        return

    await db.mark_channel_joined(telegram_id, config.tribute_channel_id)
    await db.record_event(
        telegram_id,
        "tribute_channel_joined",
        metadata={"channel_id": config.tribute_channel_id},
    )

    if request.invite_link is not None:
        try:
            await bot.revoke_chat_invite_link(
                chat_id=config.tribute_channel_id,
                invite_link=request.invite_link.invite_link,
            )
        except TelegramAPIError:
            logger.debug("Не удалось отозвать использованную ссылку", exc_info=True)

    try:
        await bot.send_message(
            request.user_chat_id,
            "<b>Готово.</b> Заявка одобрена, закрытый канал уже доступен.",
        )
    except TelegramAPIError:
        logger.debug("Не удалось уведомить пользователя %s", telegram_id)


@router.chat_member()
async def track_channel_membership(
    update: ChatMemberUpdated,
    db: Database,
    config: Config,
) -> None:
    if update.chat.id != config.tribute_channel_id:
        return

    telegram_id = update.new_chat_member.user.id
    membership = await db.get_channel_membership(
        telegram_id, config.tribute_channel_id
    )
    access = await db.get_tribute_access_status(telegram_id)
    if membership is None and not access.active:
        return

    new_status = update.new_chat_member.status
    if new_status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }:
        await db.mark_channel_joined(telegram_id, config.tribute_channel_id)
    elif new_status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await db.mark_channel_removed(telegram_id, config.tribute_channel_id)
