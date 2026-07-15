from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from app.channel_access import run_channel_sync_loop, validate_channel_setup
from app.config import load_config
from app.database import Database
from app.handlers import admin, channel, user
from app.web import create_web_app


async def set_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть главное меню")]
    )
    admin_commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="admin", description="Управление ботом"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="channelstatus", description="Проверить Tribute-канал"),
        BotCommand(command="syncchannel", description="Синхронизировать доступ"),
    ]
    for admin_id in admin_ids:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger(__name__)

    db = Database(config.database_url)
    await db.connect()
    await db.init_schema()
    await db.seed_defaults(config.boosty_url, config.tribute_url)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(admin.router)
    dispatcher.include_router(channel.router)
    dispatcher.include_router(user.router)

    await bot.delete_webhook(drop_pending_updates=False)
    await set_commands(bot, config.admin_ids)
    await validate_channel_setup(bot, config)

    web_app = create_web_app(db, bot, config)
    uvicorn_config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    logger.info(
        "Бот запущен: polling + Tribute webhook + канал %s на порту %s",
        config.tribute_channel_id,
        config.port,
    )
    sync_task = asyncio.create_task(
        run_channel_sync_loop(bot, db, config),
        name="tribute-channel-sync",
    )
    try:
        await asyncio.gather(
            dispatcher.start_polling(
                bot,
                db=db,
                config=config,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
            server.serve(),
        )
    finally:
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task
        server.should_exit = True
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
