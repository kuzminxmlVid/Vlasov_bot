from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    database_url: str
    admin_ids: frozenset[int]
    boosty_url: str
    tribute_url: str
    tribute_api_key: str
    tribute_channel_id: int
    tribute_subscription_id: int | None
    tribute_product_id: int | None
    tribute_invite_hours: int = 24
    channel_sync_minutes: int = 15
    port: int = 8080
    log_level: str = "INFO"


def _parse_admin_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.add(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                "ADMIN_IDS должен содержать Telegram ID через запятую."
            ) from exc
    return frozenset(result)


def _parse_optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом.") from exc


def _parse_required_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом.") from exc


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} должен быть больше нуля.")
    return value


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    boosty_url = os.getenv("BOOSTY_URL", "").strip()
    tribute_url = os.getenv("TRIBUTE_URL", "").strip()
    tribute_api_key = os.getenv("TRIBUTE_API_KEY", "").strip()
    tribute_channel_id = _parse_required_int("TRIBUTE_CHANNEL_ID")
    tribute_subscription_id = _parse_optional_int("TRIBUTE_SUBSCRIPTION_ID")
    tribute_product_id = _parse_optional_int("TRIBUTE_PRODUCT_ID")
    tribute_invite_hours = _parse_positive_int("TRIBUTE_INVITE_HOURS", 24)
    channel_sync_minutes = _parse_positive_int("CHANNEL_SYNC_MINUTES", 15)
    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError as exc:
        raise RuntimeError("PORT должен быть целым числом.") from exc

    missing: list[str] = []
    if not bot_token:
        missing.append("BOT_TOKEN")
    if not database_url:
        missing.append("DATABASE_URL")
    if not boosty_url:
        missing.append("BOOSTY_URL")
    if not tribute_url:
        missing.append("TRIBUTE_URL")
    if not tribute_api_key:
        missing.append("TRIBUTE_API_KEY")

    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )

    for name, url in (("BOOSTY_URL", boosty_url), ("TRIBUTE_URL", tribute_url)):
        if not url.startswith(("https://", "http://")):
            raise RuntimeError(f"{name} должен начинаться с https:// или http://")

    return Config(
        bot_token=bot_token,
        database_url=database_url,
        admin_ids=admin_ids,
        boosty_url=boosty_url,
        tribute_url=tribute_url,
        tribute_api_key=tribute_api_key,
        tribute_channel_id=tribute_channel_id,
        tribute_subscription_id=tribute_subscription_id,
        tribute_product_id=tribute_product_id,
        tribute_invite_hours=tribute_invite_hours,
        channel_sync_minutes=channel_sync_minutes,
        port=port,
        log_level=log_level,
    )
