import asyncio
import random
import sqlite3
import traceback
from typing import Any, Dict, Optional, Set

import aiohttp
import discord

from config import (
    CATEGORY_RECONCILE_SECONDS, GUILD_ID, HISTORY_BATCH_SIZE, HTTP_TIMEOUT_SECONDS,
    MAX_SOURCE_EPUB_BYTES, get_configured_guild, is_configured_guild,
)
from db import (
    ARCHIVE, get_watched_channel_row, normalize_channel_effective_order,
    unix_now, update_channel_cursor, watched_categories, watched_channels,
)
from epub_tools import is_epub_attachment
from models import DEBUG_LOGS, SESSIONS, ScanProgress, log_success, log_warning, now_utc
from config import MAX_SESSION_LIFETIME_SECONDS, SESSION_TIMEOUT_SECONDS

SCAN_QUEUE: asyncio.Queue[discord.TextChannel] = asyncio.Queue()
QUEUED_SCAN_CHANNEL_IDS: Set[int] = set()
ACTIVE_SCAN_CHANNEL_IDS: Set[int] = set()
SCAN_WORKER_STARTED = False


def is_eligible_watch_channel(channel: Any) -> bool:
    return isinstance(channel, discord.TextChannel) and getattr(channel, "type", None) in {
        discord.ChannelType.text,
        discord.ChannelType.news,
    }


async def upsert_watched_channel(channel: discord.abc.GuildChannel, enabled: bool) -> None:
    def sync(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO watched_channel(
              channel_id, guild_id, category_id, channel_name, watch_enabled,
              historical_scan_complete, scan_anchor_message_id, historical_before_message_id
            ) VALUES (?, ?, ?, ?, ?, 0, NULL, NULL)
            ON CONFLICT(channel_id) DO UPDATE SET
              guild_id = excluded.guild_id,
              category_id = excluded.category_id,
              channel_name = excluded.channel_name,
              watch_enabled = excluded.watch_enabled,
              last_error = NULL
            """,
            (
                channel.id,
                GUILD_ID,
                getattr(getattr(channel, "category", None), "id", None),
                getattr(channel, "name", str(channel.id)),
                1 if enabled else 0,
            ),
        )

    await ARCHIVE.run(sync)


async def upsert_watched_category(category: discord.CategoryChannel, enabled: bool) -> None:
    def sync(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO watched_category(category_id, guild_id, category_name, watch_enabled, last_error)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(category_id) DO UPDATE SET
              guild_id = excluded.guild_id,
              category_name = excluded.category_name,
              watch_enabled = excluded.watch_enabled,
              last_error = NULL
            """,
            (category.id, GUILD_ID, category.name, 1 if enabled else 0),
        )

    await ARCHIVE.run(sync)

async def cleanup_sessions() -> None:
    while True:
        await asyncio.sleep(300)

        now_ts = now_utc().timestamp()
        idle_cutoff = now_ts - SESSION_TIMEOUT_SECONDS
        lifetime_cutoff = now_ts - MAX_SESSION_LIFETIME_SECONDS

        stale = [
            key
            for key, session in SESSIONS.items()
            if (
                session.last_accessed_at.timestamp() < idle_cutoff
                or session.created_at.timestamp() < lifetime_cutoff
            )
        ]

        for key in stale:
            session = SESSIONS.pop(key, None)

            if session is not None:
                session.expired = True


async def fetch_attachment_bytes(http: aiohttp.ClientSession, att: discord.Attachment) -> bytes:
    last_error: Optional[Exception] = None

    for candidate in [att.url, att.proxy_url]:
        if not candidate:
            continue
        try:
            return await fetch_bytes(http, candidate)
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise ValueError("No valid attachment URL available")


async def import_message_epubs(
    message: discord.Message,
    only_attachment_indexes: Optional[Set[int]] = None,
    scan_progress: Optional[ScanProgress] = None,
) -> int:
    if not is_configured_guild(message.guild):
        return 0

    epub_attachments = [
        (idx, att)
        for idx, att in enumerate(message.attachments)
        if is_epub_attachment(att)
        and (
            only_attachment_indexes is None
            or idx in only_attachment_indexes
        )
    ]

    if not epub_attachments:
        return 0

    imported_count = 0
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for idx, att in epub_attachments:
            if await ARCHIVE.is_discord_epub_imported(
                message.channel.id,
                message.id,
                idx,
            ):
                continue

            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    data = await fetch_attachment_bytes(http, att)
                    imported = await ARCHIVE.import_epub_bytes(
                        guild_id=GUILD_ID,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        attachment_index=idx,
                        filename=att.filename,
                        attachment_size=att.size,
                        message_created_at=message.created_at,
                        epub_bytes=data,
                    )
                    if imported:
                        imported_count += 1
                        if scan_progress is not None:
                            scan_progress.archived_epubs += 1
                            scan_progress.update(att.filename)
                        else:
                            log_success(f"Archived {att.filename} from #{getattr(message.channel, 'name', message.channel.id)}")
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.75 * (2**attempt) + random.uniform(0, 0.25))

            if last_error is not None:
                log_warning(f"Import failed for {att.filename} in message {message.id}: {last_error}")
                await ARCHIVE.record_import_failure(
                    guild_id=GUILD_ID,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    attachment_index=idx,
                    filename=att.filename,
                    error_text=str(last_error),
                )

    return imported_count


async def start_historical_scan(channel: discord.TextChannel) -> None:
    row = await get_watched_channel_row(channel.id)
    if row is None:
        return

    if row["historical_scan_complete"]:
        return

    progress = ScanProgress(channel.name)
    try:
        progress.start()
        await update_channel_cursor(
            channel.id,
            last_scan_started_at=unix_now(),
            last_error=None,
        )
        anchor = row["scan_anchor_message_id"]
        before_id = row["historical_before_message_id"]

        if anchor is None:
            latest = None
            async for msg in channel.history(limit=1):
                latest = msg
                break
            if latest is None:
                await normalize_channel_effective_order(channel.id)
                await update_channel_cursor(
                    channel.id,
                    historical_scan_complete=1,
                    last_scan_finished_at=unix_now(),
                    historical_before_message_id=None,
                )
                progress.finish()
                return
            anchor = latest.id
            before_id = latest.id + 1
            await update_channel_cursor(
                channel.id,
                scan_anchor_message_id=anchor,
                historical_before_message_id=before_id,
            )

        while True:
            current = await get_watched_channel_row(channel.id)
            if current is None:
                progress.stop_without_summary()
                return
            before_id = current["historical_before_message_id"] or before_id
            batch = [
                msg
                async for msg in channel.history(
                    limit=HISTORY_BATCH_SIZE,
                    before=discord.Object(id=before_id),
                )
            ]

            if not batch:
                await normalize_channel_effective_order(channel.id)
                await update_channel_cursor(
                    channel.id,
                    historical_scan_complete=1,
                    historical_before_message_id=None,
                    last_scan_finished_at=unix_now(),
                    last_error=None,
                )
                progress.finish()
                return

            for msg in batch:
                progress.scanned_messages += 1
                await import_message_epubs(msg, scan_progress=progress)
                progress.update()

            await update_channel_cursor(
                channel.id,
                historical_before_message_id=batch[-1].id,
                last_processed_message_id=max(
                    batch[0].id,
                    current["last_processed_message_id"] or 0,
                ),
            )
    except discord.Forbidden:
        progress.stop_without_summary()
        await update_channel_cursor(channel.id, last_error="Missing permission to read message history")
        log_warning(f"Cannot scan #{channel.name}: missing Read Message History")
    except Exception as exc:
        progress.stop_without_summary()
        await update_channel_cursor(channel.id, last_error=str(exc)[:1000])
        log_warning(f"Historical scan failed for #{channel.name}: {exc}")
        if DEBUG_LOGS:
            traceback.print_exc()


async def enqueue_historical_scan(channel: discord.TextChannel) -> bool:
    row = await get_watched_channel_row(channel.id)

    if row is None or row["historical_scan_complete"]:
        return False

    if channel.id in QUEUED_SCAN_CHANNEL_IDS or channel.id in ACTIVE_SCAN_CHANNEL_IDS:
        return False

    QUEUED_SCAN_CHANNEL_IDS.add(channel.id)
    await SCAN_QUEUE.put(channel)
    return True


def scan_queue_size() -> int:
    return SCAN_QUEUE.qsize()


async def historical_scan_worker() -> None:
    while True:
        channel = await SCAN_QUEUE.get()
        QUEUED_SCAN_CHANNEL_IDS.discard(channel.id)
        ACTIVE_SCAN_CHANNEL_IDS.add(channel.id)

        try:
            await start_historical_scan(channel)
        finally:
            ACTIVE_SCAN_CHANNEL_IDS.discard(channel.id)
            SCAN_QUEUE.task_done()
            log_success(
                f"Category scan complete for #{channel.name} - "
                f"Queue remaining: {scan_queue_size()}"
            )


def ensure_scan_worker_started() -> None:
    global SCAN_WORKER_STARTED

    if SCAN_WORKER_STARTED:
        return

    SCAN_WORKER_STARTED = True
    asyncio.create_task(historical_scan_worker())


async def catch_up_channel(channel: discord.TextChannel) -> None:
    row = await get_watched_channel_row(channel.id)
    if row is None:
        return

    after_id = row["last_processed_message_id"]

    if not row["historical_scan_complete"]:
        anchor_id = row["scan_anchor_message_id"]

        if anchor_id is None:
            return

        after_id = max(after_id or 0, anchor_id)

    try:
        kwargs: Dict[str, Any] = {"limit": None, "oldest_first": True}
        if after_id:
            kwargs["after"] = discord.Object(id=after_id)

        latest_seen = after_id or 0
        async for msg in channel.history(**kwargs):
            await import_message_epubs(msg)
            latest_seen = max(latest_seen, msg.id)

        await update_channel_cursor(
            channel.id,
            last_processed_message_id=latest_seen or None,
            last_catchup_completed_at=unix_now(),
            last_error=None,
        )
    except discord.Forbidden:
        await update_channel_cursor(channel.id, last_error="Missing permission to catch up channel history")
        log_warning(f"Cannot catch up #{channel.name}: missing Read Message History")
    except Exception as exc:
        await update_channel_cursor(channel.id, last_error=str(exc)[:1000])
        log_warning(f"Catch-up failed for #{channel.name}: {exc}")
        if DEBUG_LOGS:
            traceback.print_exc()


async def retry_channel_import_failures(channel: discord.TextChannel) -> None:
    rows = await ARCHIVE.run(
        lambda conn: conn.execute(
            """
            SELECT message_id, attachment_index
            FROM import_failure
            WHERE guild_id = ? AND channel_id = ?
            ORDER BY message_id ASC, attachment_index ASC
            """,
            (GUILD_ID, channel.id),
        ).fetchall()
    )

    by_message: Dict[int, Set[int]] = {}

    for row in rows:
        by_message.setdefault(row["message_id"], set()).add(row["attachment_index"])

    for message_id, attachment_indexes in by_message.items():
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        except discord.Forbidden:
            log_warning(f"Cannot retry failed imports in #{channel.name}: missing message access")
            return
        except discord.HTTPException as exc:
            log_warning(f"Failed to fetch message {message_id} for retry: {exc}")
            continue

        await import_message_epubs(message, only_attachment_indexes=attachment_indexes)


async def reconcile_watched_category(category: discord.CategoryChannel) -> int:
    added = 0
    queued = 0
    checked = 0
    for channel in category.channels:
        if not is_eligible_watch_channel(channel):
            continue
        checked += 1
        before = await get_watched_channel_row(channel.id)
        await upsert_watched_channel(channel, True)
        if before is None:
            if await enqueue_historical_scan(channel):
                added += 1
                queued += 1
        elif not before["historical_scan_complete"]:
            if await enqueue_historical_scan(channel):
                queued += 1

    await ARCHIVE.run(
        lambda conn: conn.execute(
            """
            UPDATE watched_category
            SET category_name = ?, last_reconciled_at = ?, last_error = NULL
            WHERE category_id = ?
            """,
            (category.name, unix_now(), category.id),
        )
    )
    log_success(
        f"Category scan initialized for {category.name} - "
        f"Checked {checked} channels - Added {queued} channel(s) to queue - "
        f"New channels: {added} - Total in queue: {scan_queue_size()}"
    )
    return added


async def reconcile_all_categories_once() -> None:
    guild = get_configured_guild()
    if guild is None:
        return

    for row in await watched_categories():
        category = guild.get_channel(row["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await ARCHIVE.run(
                lambda conn, row=row: conn.execute(
                    "UPDATE watched_category SET watch_enabled = 0, last_error = ? WHERE category_id = ?",
                    ("Category no longer exists or is inaccessible", row["category_id"]),
                )
            )
            log_warning(f"Category {row['category_id']} is no longer available; disabled category watch")
            continue
        added = await reconcile_watched_category(category)
        if added:
            log_success(f"Category reconciliation added {added} channel(s) from {category.name}")


async def category_reconcile_loop() -> None:
    while True:
        if get_configured_guild() is not None:
            await reconcile_all_categories_once()
        await asyncio.sleep(CATEGORY_RECONCILE_SECONDS)


async def startup_channel_work() -> None:
    guild = get_configured_guild()
    if guild is None:
        return
    await reconcile_all_categories_once()

    for row in await watched_channels():
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        log_channel_permission_diagnostics(channel)
        asyncio.create_task(retry_channel_import_failures(channel))
        asyncio.create_task(catch_up_channel(channel))
        if not row["historical_scan_complete"]:
            await enqueue_historical_scan(channel)


def log_channel_permission_diagnostics(channel: discord.TextChannel) -> None:
    member = channel.guild.me
    if member is None:
        return
    perms = channel.permissions_for(member)
    required = {
        "View Channel": perms.view_channel,
        "Read Message History": perms.read_message_history,
        "Send Messages": perms.send_messages,
        "Attach Files": perms.attach_files,
    }
    for label, ok in required.items():
        if not ok:
            log_warning(f"Permission warning in #{channel.name}: missing {label}")
    if not perms.embed_links:
        log_warning(f"Permission note in #{channel.name}: missing Embed Links")



async def fetch_bytes_once(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        if resp.status in {429, 500, 502, 503, 504}:
            raise aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=f"Transient HTTP status {resp.status}",
                headers=resp.headers,
            )

        resp.raise_for_status()

        content_length = resp.headers.get("Content-Length")

        if content_length:
            try:
                parsed_length = int(content_length)
            except ValueError:
                log_warning(f"Ignoring malformed Content-Length: {content_length!r}")
            else:
                if parsed_length > MAX_SOURCE_EPUB_BYTES:
                    raise ValueError("Attachment is too large")

        data = bytearray()

        async for chunk in resp.content.iter_chunked(256 * 1024):
            data.extend(chunk)

            if len(data) > MAX_SOURCE_EPUB_BYTES:
                raise ValueError("Attachment is too large")

        return bytes(data)


async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    attempts = 3
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        try:
            return await fetch_bytes_once(session, url)
        except aiohttp.ClientResponseError as exc:
            last_error = exc

            if exc.status not in {429, 500, 502, 503, 504}:
                raise

            retry_after = exc.headers.get("Retry-After") if exc.headers else None

            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 0.75 * (2**attempt)
            else:
                delay = 0.75 * (2**attempt)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            delay = 0.75 * (2**attempt)

        if attempt < attempts - 1:
            delay += random.uniform(0, 0.25)
            await asyncio.sleep(min(delay, 5))

    if last_error is not None:
        raise last_error

    raise ValueError("Failed to fetch attachment")

