from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import FastAPI, HTTPException, Request

from app.channel_access import (
    provision_tribute_channel_access,
    remove_from_tribute_channel,
)
from app.config import Config
from app.database import AccessStatus, Database

logger = logging.getLogger(__name__)


def create_web_app(db: Database, bot: Bot, config: Config) -> FastAPI:
    app = FastAPI(title="Radiotechnica Bot", docs_url=None, redoc_url=None)

    @app.get("/")
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/tribute")
    async def tribute_webhook(request: Request) -> dict[str, str]:
        raw_body = await request.body()
        signature = request.headers.get("trbt-signature", "")
        if not _verify_signature(raw_body, signature, config.tribute_api_key):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc

        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Invalid webhook data")
        event_name = data.get("name")
        payload = data.get("payload")
        if not isinstance(event_name, str) or not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid webhook data")

        event_key = hashlib.sha256(raw_body).hexdigest()

        if event_name in {
            "new_subscription",
            "renewed_subscription",
            "cancelled_subscription",
        }:
            if not _subscription_allowed(payload, config):
                return {"status": "ignored"}
            _require_fields(payload, "telegram_user_id", "subscription_id")
            processed, status = await db.apply_tribute_subscription_event(
                event_key,
                event_name,
                payload,
            )
            if processed:
                await _handle_subscription(
                    bot, db, config, payload, event_name, status
                )
            return {"status": "ok"}

        if event_name in {"new_digital_product", "digital_product_refunded"}:
            if not _product_allowed(payload, config):
                return {"status": "ignored"}
            _require_fields(payload, "telegram_user_id", "product_id", "purchase_id")
            processed, status = await db.apply_tribute_product_event(
                event_key,
                event_name,
                payload,
            )
            if processed:
                await _handle_product(bot, db, config, payload, event_name, status)
            return {"status": "ok"}

        return {"status": "ignored"}

    return app


def _verify_signature(raw_body: bytes, signature: str, api_key: str) -> bool:
    if not signature or not api_key:
        return False
    digest = hmac.new(api_key.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected_hex = digest.hex()
    expected_b64 = base64.b64encode(digest).decode("ascii")

    candidate = signature.strip()
    for prefix in ("sha256=", "v1="):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix):]
            break

    return hmac.compare_digest(candidate.lower(), expected_hex) or hmac.compare_digest(
        candidate,
        expected_b64,
    )


def _subscription_allowed(payload: dict[str, Any], config: Config) -> bool:
    if config.tribute_subscription_id is None:
        return config.tribute_product_id is None
    try:
        return int(payload.get("subscription_id")) == config.tribute_subscription_id
    except (TypeError, ValueError):
        return False


def _product_allowed(payload: dict[str, Any], config: Config) -> bool:
    if config.tribute_product_id is None:
        return config.tribute_subscription_id is None
    try:
        return int(payload.get("product_id")) == config.tribute_product_id
    except (TypeError, ValueError):
        return False


def _require_fields(payload: dict[str, Any], *fields: str) -> None:
    missing = [field for field in fields if payload.get(field) is None]
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Missing fields: " + ", ".join(missing),
        )


async def _handle_subscription(
    bot: Bot,
    db: Database,
    config: Config,
    payload: dict[str, Any],
    event_name: str,
    status: AccessStatus,
) -> None:
    telegram_id = int(payload["telegram_user_id"])

    if event_name in {"new_subscription", "renewed_subscription"}:
        expires = _format_datetime(status.expires_at)
        text = "<b>Оплата через Tribute подтверждена.</b>"
        if expires:
            text += f"\nДоступ действует до: <b>{expires}</b>."
        await provision_tribute_channel_access(
            bot=bot,
            db=db,
            config=config,
            telegram_id=telegram_id,
            access=status,
            intro_text=text,
        )
        return

    expires = _format_datetime(status.expires_at)
    if status.active and expires:
        await _safe_notify(
            bot,
            telegram_id,
            "<b>Автопродление Tribute отключено.</b>\n\n"
            f"Доступ к закрытому каналу сохранится до <b>{expires}</b>.",
        )
        return

    await remove_from_tribute_channel(
        bot=bot,
        db=db,
        config=config,
        telegram_id=telegram_id,
        notify=False,
    )
    await _safe_notify(
        bot,
        telegram_id,
        "<b>Подписка Tribute завершена.</b> Доступ к закрытому каналу отключён.",
    )


async def _handle_product(
    bot: Bot,
    db: Database,
    config: Config,
    payload: dict[str, Any],
    event_name: str,
    status: AccessStatus,
) -> None:
    telegram_id = int(payload["telegram_user_id"])
    if event_name == "new_digital_product":
        await provision_tribute_channel_access(
            bot=bot,
            db=db,
            config=config,
            telegram_id=telegram_id,
            access=status,
            intro_text="<b>Оплата через Tribute подтверждена.</b>",
        )
        return

    await remove_from_tribute_channel(
        bot=bot,
        db=db,
        config=config,
        telegram_id=telegram_id,
        notify=False,
    )
    await _safe_notify(
        bot,
        telegram_id,
        "<b>Платёж Tribute возвращён.</b> Доступ к закрытому каналу отключён.",
    )


async def _safe_notify(bot: Bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(telegram_id, text)
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить пользователю %s уведомление Tribute",
            telegram_id,
        )


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().strftime("%d.%m.%Y %H:%M")
