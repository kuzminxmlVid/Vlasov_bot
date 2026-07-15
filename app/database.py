from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram.types import User


@dataclass(slots=True)
class Lesson:
    slug: str
    title: str
    description: str
    media_type: str | None
    file_id: str | None
    updated_at: datetime


@dataclass(slots=True)
class AccessStatus:
    active: bool
    provider: str | None = None
    expires_at: datetime | None = None
    status: str | None = None


@dataclass(slots=True)
class ChannelMembership:
    telegram_id: int
    channel_id: int
    source: str
    status: str
    invite_link: str | None
    invite_expires_at: datetime | None
    invited_at: datetime | None
    joined_at: datetime | None
    removed_at: datetime | None
    updated_at: datetime


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            dsn=self.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Соединение с PostgreSQL ещё не создано")
        return self.pool

    async def init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT,
            trial_completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS lessons (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            media_type TEXT,
            file_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS lesson_progress (
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            lesson_slug TEXT NOT NULL REFERENCES lessons(slug) ON DELETE CASCADE,
            first_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            view_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (telegram_id, lesson_slug)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS events (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            lesson_slug TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS access_grants (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            offer_type TEXT NOT NULL,
            offer_id BIGINT,
            status TEXT NOT NULL,
            expires_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider, external_id)
        );

        CREATE TABLE IF NOT EXISTS webhook_events (
            id BIGSERIAL PRIMARY KEY,
            provider TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider, event_key)
        );

        CREATE TABLE IF NOT EXISTS channel_memberships (
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            channel_id BIGINT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            invite_link TEXT,
            invite_expires_at TIMESTAMPTZ,
            invited_at TIMESTAMPTZ,
            joined_at TIMESTAMPTZ,
            removed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (telegram_id, channel_id, source)
        );

        CREATE INDEX IF NOT EXISTS idx_events_type_created
            ON events(event_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_user_created
            ON events(telegram_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_access_grants_user
            ON access_grants(telegram_id, status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_channel_memberships_status
            ON channel_memberships(channel_id, source, status, updated_at);
        """
        async with self._pool().acquire() as conn:
            await conn.execute(schema)

    async def seed_defaults(self, boosty_url: str, tribute_url: str) -> None:
        default_lessons = (
            (
                "acoustic",
                "Акустика 1",
                "Это первый разговор об акустике помещений и строительстве студии.",
            ),
            (
                "studio",
                "Студия 1",
                "Это введение в тему студии, оснащения, аппаратуры и подключения.",
            ),
            (
                "base",
                "База 1",
                "Это первый базовый урок, с которого мы начинаем.",
            ),
        )
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO lessons(slug, title, description)
                    VALUES($1, $2, $3)
                    ON CONFLICT (slug) DO NOTHING
                    """,
                    default_lessons,
                )
                await conn.executemany(
                    """
                    INSERT INTO settings(key, value)
                    VALUES($1, $2)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (("boosty_url", boosty_url), ("tribute_url", tribute_url)),
                )

    async def upsert_user(self, user: User) -> None:
        await self._pool().execute(
            """
            INSERT INTO users(telegram_id, username, first_name, last_name)
            VALUES($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                updated_at = NOW()
            """,
            user.id,
            user.username,
            user.first_name or "",
            user.last_name,
        )

    async def upsert_external_user(self, telegram_id: int, username: str | None) -> None:
        await self._pool().execute(
            """
            INSERT INTO users(telegram_id, username, first_name)
            VALUES($1, $2, '')
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                updated_at = NOW()
            """,
            telegram_id,
            username,
        )

    async def record_event(
        self,
        telegram_id: int | None,
        event_type: str,
        lesson_slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        await self._pool().execute(
            """
            INSERT INTO events(telegram_id, event_type, lesson_slug, metadata)
            VALUES($1, $2, $3, $4::jsonb)
            """,
            telegram_id,
            event_type,
            lesson_slug,
            payload,
        )

    async def get_lesson(self, slug: str) -> Lesson | None:
        row = await self._pool().fetchrow(
            """
            SELECT slug, title, description, media_type, file_id, updated_at
            FROM lessons
            WHERE slug = $1
            """,
            slug,
        )
        if row is None:
            return None
        return Lesson(**dict(row))

    async def list_lessons(self) -> list[Lesson]:
        rows = await self._pool().fetch(
            """
            SELECT slug, title, description, media_type, file_id, updated_at
            FROM lessons
            ORDER BY CASE slug
                WHEN 'acoustic' THEN 1
                WHEN 'studio' THEN 2
                WHEN 'base' THEN 3
                ELSE 99
            END
            """
        )
        return [Lesson(**dict(row)) for row in rows]

    async def mark_lesson_viewed(self, telegram_id: int, slug: str) -> tuple[int, bool]:
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO lesson_progress(telegram_id, lesson_slug)
                    VALUES($1, $2)
                    ON CONFLICT (telegram_id, lesson_slug) DO UPDATE SET
                        last_viewed_at = NOW(),
                        view_count = lesson_progress.view_count + 1
                    """,
                    telegram_id,
                    slug,
                )
                viewed_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)::INTEGER
                    FROM lesson_progress
                    WHERE telegram_id = $1
                    """,
                    telegram_id,
                )
                trial_completed_at = await conn.fetchval(
                    "SELECT trial_completed_at FROM users WHERE telegram_id = $1 FOR UPDATE",
                    telegram_id,
                )
                newly_completed = bool(viewed_count >= 3 and trial_completed_at is None)
                if newly_completed:
                    await conn.execute(
                        """
                        UPDATE users
                        SET trial_completed_at = NOW(), updated_at = NOW()
                        WHERE telegram_id = $1
                        """,
                        telegram_id,
                    )
        return int(viewed_count), newly_completed

    async def set_lesson_media(
        self,
        slug: str,
        media_type: str,
        file_id: str,
        description: str | None = None,
    ) -> bool:
        if description is None:
            result = await self._pool().execute(
                """
                UPDATE lessons
                SET media_type = $2, file_id = $3, updated_at = NOW()
                WHERE slug = $1
                """,
                slug,
                media_type,
                file_id,
            )
        else:
            result = await self._pool().execute(
                """
                UPDATE lessons
                SET media_type = $2, file_id = $3, description = $4, updated_at = NOW()
                WHERE slug = $1
                """,
                slug,
                media_type,
                file_id,
                description,
            )
        return result.endswith("1")

    async def clear_lesson_media(self, slug: str) -> bool:
        result = await self._pool().execute(
            """
            UPDATE lessons
            SET media_type = NULL, file_id = NULL, updated_at = NOW()
            WHERE slug = $1
            """,
            slug,
        )
        return result.endswith("1")

    async def set_lesson_text(self, slug: str, description: str) -> bool:
        result = await self._pool().execute(
            """
            UPDATE lessons
            SET description = $2, updated_at = NOW()
            WHERE slug = $1
            """,
            slug,
            description,
        )
        return result.endswith("1")

    async def get_setting(self, key: str) -> str | None:
        return await self._pool().fetchval(
            "SELECT value FROM settings WHERE key = $1",
            key,
        )

    async def set_setting(self, key: str, value: str) -> None:
        await self._pool().execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            key,
            value,
        )

    async def apply_tribute_subscription_event(
        self,
        event_key: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> tuple[bool, AccessStatus]:
        telegram_id = int(payload["telegram_user_id"])
        subscription_id = int(payload["subscription_id"])
        username = payload.get("telegram_username")
        expires_at_raw = payload.get("expires_at")
        expires_at = _parse_datetime(expires_at_raw)
        external_id = f"subscription:{subscription_id}:{telegram_id}"
        status = "cancelled" if event_name == "cancelled_subscription" else "active"
        payload_json = json.dumps(payload, ensure_ascii=False)

        async with self._pool().acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO webhook_events(provider, event_key, event_type, payload)
                    VALUES('tribute', $1, $2, $3::jsonb)
                    ON CONFLICT (provider, event_key) DO NOTHING
                    RETURNING id
                    """,
                    event_key,
                    event_name,
                    payload_json,
                )
                if inserted is None:
                    current = await self._get_access_status_conn(conn, telegram_id)
                    return False, current

                await conn.execute(
                    """
                    INSERT INTO users(telegram_id, username, first_name)
                    VALUES($1, $2, '')
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        updated_at = NOW()
                    """,
                    telegram_id,
                    username,
                )
                await conn.execute(
                    """
                    INSERT INTO access_grants(
                        telegram_id, provider, external_id, offer_type, offer_id,
                        status, expires_at, metadata
                    )
                    VALUES($1, 'tribute', $2, 'subscription', $3, $4, $5, $6::jsonb)
                    ON CONFLICT (provider, external_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    telegram_id,
                    external_id,
                    subscription_id,
                    status,
                    expires_at,
                    payload_json,
                )
                current = await self._get_access_status_conn(conn, telegram_id)
        return True, current

    async def apply_tribute_product_event(
        self,
        event_key: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> tuple[bool, AccessStatus]:
        telegram_id = int(payload["telegram_user_id"])
        product_id = int(payload["product_id"])
        purchase_id = int(payload["purchase_id"])
        username = payload.get("telegram_username")
        external_id = f"product:{purchase_id}"
        status = "refunded" if event_name == "digital_product_refunded" else "active"
        payload_json = json.dumps(payload, ensure_ascii=False)

        async with self._pool().acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO webhook_events(provider, event_key, event_type, payload)
                    VALUES('tribute', $1, $2, $3::jsonb)
                    ON CONFLICT (provider, event_key) DO NOTHING
                    RETURNING id
                    """,
                    event_key,
                    event_name,
                    payload_json,
                )
                if inserted is None:
                    current = await self._get_access_status_conn(conn, telegram_id)
                    return False, current

                await conn.execute(
                    """
                    INSERT INTO users(telegram_id, username, first_name)
                    VALUES($1, $2, '')
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        updated_at = NOW()
                    """,
                    telegram_id,
                    username,
                )
                await conn.execute(
                    """
                    INSERT INTO access_grants(
                        telegram_id, provider, external_id, offer_type, offer_id,
                        status, expires_at, metadata
                    )
                    VALUES($1, 'tribute', $2, 'digital_product', $3, $4, NULL, $5::jsonb)
                    ON CONFLICT (provider, external_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    telegram_id,
                    external_id,
                    product_id,
                    status,
                    payload_json,
                )
                current = await self._get_access_status_conn(conn, telegram_id)
        return True, current

    async def grant_manual_access(self, telegram_id: int) -> None:
        await self.upsert_external_user(telegram_id, None)
        await self._pool().execute(
            """
            INSERT INTO access_grants(
                telegram_id, provider, external_id, offer_type, status
            )
            VALUES($1, 'manual', $2, 'manual', 'active')
            ON CONFLICT (provider, external_id) DO UPDATE SET
                status = 'active', expires_at = NULL, updated_at = NOW()
            """,
            telegram_id,
            f"manual:{telegram_id}",
        )

    async def get_access_status(self, telegram_id: int) -> AccessStatus:
        async with self._pool().acquire() as conn:
            return await self._get_access_status_conn(conn, telegram_id)

    async def _get_access_status_conn(
        self,
        conn: asyncpg.Connection,
        telegram_id: int,
    ) -> AccessStatus:
        row = await conn.fetchrow(
            """
            SELECT provider, expires_at, status
            FROM access_grants
            WHERE telegram_id = $1
              AND (
                    (status = 'active' AND (expires_at IS NULL OR expires_at > NOW()))
                 OR (status = 'cancelled' AND expires_at > NOW())
              )
            ORDER BY
                CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END DESC,
                expires_at DESC NULLS LAST,
                updated_at DESC
            LIMIT 1
            """,
            telegram_id,
        )
        if row is None:
            return AccessStatus(active=False)
        return AccessStatus(
            active=True,
            provider=row["provider"],
            expires_at=row["expires_at"],
            status=row["status"],
        )

    async def get_tribute_access_status(self, telegram_id: int) -> AccessStatus:
        row = await self._pool().fetchrow(
            """
            SELECT provider, expires_at, status
            FROM access_grants
            WHERE telegram_id = $1
              AND provider = 'tribute'
              AND (
                    (status = 'active' AND (expires_at IS NULL OR expires_at > NOW()))
                 OR (status = 'cancelled' AND expires_at > NOW())
              )
            ORDER BY
                CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END DESC,
                expires_at DESC NULLS LAST,
                updated_at DESC
            LIMIT 1
            """,
            telegram_id,
        )
        if row is None:
            return AccessStatus(active=False)
        return AccessStatus(
            active=True,
            provider=row["provider"],
            expires_at=row["expires_at"],
            status=row["status"],
        )

    async def save_channel_invite(
        self,
        telegram_id: int,
        channel_id: int,
        invite_link: str,
        invite_expires_at: datetime,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO channel_memberships(
                telegram_id, channel_id, source, status, invite_link,
                invite_expires_at, invited_at, joined_at, removed_at, updated_at
            )
            VALUES($1, $2, 'tribute', 'invited', $3, $4, NOW(), NULL, NULL, NOW())
            ON CONFLICT (telegram_id, channel_id, source) DO UPDATE SET
                status = 'invited',
                invite_link = EXCLUDED.invite_link,
                invite_expires_at = EXCLUDED.invite_expires_at,
                invited_at = NOW(),
                removed_at = NULL,
                updated_at = NOW()
            """,
            telegram_id,
            channel_id,
            invite_link,
            invite_expires_at,
        )

    async def mark_channel_joined(self, telegram_id: int, channel_id: int) -> None:
        await self._pool().execute(
            """
            INSERT INTO channel_memberships(
                telegram_id, channel_id, source, status, joined_at, removed_at, updated_at
            )
            VALUES($1, $2, 'tribute', 'joined', NOW(), NULL, NOW())
            ON CONFLICT (telegram_id, channel_id, source) DO UPDATE SET
                status = 'joined',
                joined_at = COALESCE(channel_memberships.joined_at, NOW()),
                removed_at = NULL,
                updated_at = NOW()
            """,
            telegram_id,
            channel_id,
        )

    async def mark_channel_removed(self, telegram_id: int, channel_id: int) -> None:
        await self._pool().execute(
            """
            INSERT INTO channel_memberships(
                telegram_id, channel_id, source, status, removed_at, updated_at
            )
            VALUES($1, $2, 'tribute', 'removed', NOW(), NOW())
            ON CONFLICT (telegram_id, channel_id, source) DO UPDATE SET
                status = 'removed',
                removed_at = NOW(),
                invite_link = NULL,
                invite_expires_at = NULL,
                updated_at = NOW()
            """,
            telegram_id,
            channel_id,
        )

    async def mark_channel_denied(self, telegram_id: int, channel_id: int) -> None:
        await self._pool().execute(
            """
            INSERT INTO channel_memberships(
                telegram_id, channel_id, source, status, updated_at
            )
            VALUES($1, $2, 'tribute', 'denied', NOW())
            ON CONFLICT (telegram_id, channel_id, source) DO UPDATE SET
                status = 'denied',
                updated_at = NOW()
            """,
            telegram_id,
            channel_id,
        )

    async def get_channel_membership(
        self, telegram_id: int, channel_id: int
    ) -> ChannelMembership | None:
        row = await self._pool().fetchrow(
            """
            SELECT telegram_id, channel_id, source, status, invite_link,
                   invite_expires_at, invited_at, joined_at, removed_at, updated_at
            FROM channel_memberships
            WHERE telegram_id = $1 AND channel_id = $2 AND source = 'tribute'
            """,
            telegram_id,
            channel_id,
        )
        return ChannelMembership(**dict(row)) if row is not None else None

    async def list_inactive_tribute_users_for_channel(
        self, channel_id: int, limit: int = 500
    ) -> list[int]:
        rows = await self._pool().fetch(
            """
            SELECT DISTINCT ag.telegram_id
            FROM access_grants ag
            LEFT JOIN channel_memberships cm
              ON cm.telegram_id = ag.telegram_id
             AND cm.channel_id = $1
             AND cm.source = 'tribute'
            WHERE ag.provider = 'tribute'
              AND NOT EXISTS (
                    SELECT 1
                    FROM access_grants active_grant
                    WHERE active_grant.telegram_id = ag.telegram_id
                      AND active_grant.provider = 'tribute'
                      AND (
                            (active_grant.status = 'active' AND
                             (active_grant.expires_at IS NULL OR active_grant.expires_at > NOW()))
                         OR (active_grant.status = 'cancelled' AND
                             active_grant.expires_at > NOW())
                      )
              )
              AND (cm.status IS NULL OR cm.status <> 'removed')
            ORDER BY ag.telegram_id
            LIMIT $2
            """,
            channel_id,
            limit,
        )
        return [int(row["telegram_id"]) for row in rows]

    async def get_stats(self) -> dict[str, Any]:
        async with self._pool().acquire() as conn:
            users_total = await conn.fetchval("SELECT COUNT(*)::INTEGER FROM users")
            trial_completed = await conn.fetchval(
                """
                SELECT COUNT(*)::INTEGER
                FROM users
                WHERE trial_completed_at IS NOT NULL
                """
            )
            purchase_screens = await conn.fetchval(
                """
                SELECT COUNT(*)::INTEGER
                FROM events
                WHERE event_type = 'purchase_screen_opened'
                """
            )
            active_tribute = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT telegram_id)::INTEGER
                FROM access_grants
                WHERE provider = 'tribute'
                  AND (
                        (status = 'active' AND (expires_at IS NULL OR expires_at > NOW()))
                     OR (status = 'cancelled' AND expires_at > NOW())
                  )
                """
            )
            lesson_rows = await conn.fetch(
                """
                SELECT
                    l.slug,
                    l.title,
                    COUNT(lp.telegram_id)::INTEGER AS unique_viewers,
                    COALESCE(SUM(lp.view_count), 0)::INTEGER AS total_views
                FROM lessons l
                LEFT JOIN lesson_progress lp ON lp.lesson_slug = l.slug
                GROUP BY l.slug, l.title
                ORDER BY CASE l.slug
                    WHEN 'acoustic' THEN 1
                    WHEN 'studio' THEN 2
                    WHEN 'base' THEN 3
                    ELSE 99
                END
                """
            )
        return {
            "users_total": int(users_total),
            "trial_completed": int(trial_completed),
            "purchase_screens": int(purchase_screens),
            "active_tribute": int(active_tribute),
            "lessons": [dict(row) for row in lesson_rows],
        }


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
