import asyncio
import contextlib
import random
import sqlite3
import traceback
from datetime import timedelta, timezone
from time import monotonic
from typing import Any, Dict, List, Optional, Set

import aiohttp
import discord

from config import (
    CATEGORY_RECONCILE_SECONDS, GUILD_ID, HISTORY_BATCH_SIZE, HTTP_TIMEOUT_SECONDS,
    LIVE_IMPORT_DELAY_SECONDS, LIVE_IMPORT_RATE_SECONDS, MAX_SOURCE_EPUB_BYTES,
    SCAN_WATCHDOG_SECONDS, bot, get_configured_guild, is_configured_guild,
)
from db import (
    ARCHIVE, advance_channel_last_processed_message, get_watched_channel_row,
    hard_reset_channel, normalize_channel_effective_order, unix_now, update_channel_cursor,
    watched_categories, watched_channels,
)
from epub_tools import is_epub_attachment
from models import (
    DEBUG_LOGS, SESSIONS, ScanProgress, log_success, log_warning, now_utc,
    safe_log_text,
)
from config import MAX_SESSION_LIFETIME_SECONDS, SESSION_TIMEOUT_SECONDS
from notifications import ARCHIVE_FAILURE_NOTIFIER

SCAN_QUEUE: asyncio.Queue[tuple[discord.TextChannel, str, int]] = asyncio.Queue()
QUEUED_SCAN_GENERATIONS: Dict[int, int] = {}
ACTIVE_SCAN_CHANNEL_IDS: Set[int] = set()
ACTIVE_SCAN_HEARTBEATS: Dict[int, float] = {}
ACTIVE_SCAN_TASKS: Dict[int, asyncio.Task] = {}
CHANNEL_MAINTENANCE_IDS: Set[int] = set()
LIVE_IMPORT_QUEUE: asyncio.Queue[tuple[discord.Message, int, int]] = asyncio.Queue()
QUEUED_LIVE_IMPORT_KEYS: Set[tuple[int, int, int]] = set()
LIVE_IMPORT_PENDING_BY_MESSAGE: Dict[tuple[int, int], int] = {}
IMPORTING_ATTACHMENT_KEYS: Set[tuple[int, int, int]] = set()
IMPORTING_ATTACHMENT_LOCK = asyncio.Lock()
BACKGROUND_TASKS: Dict[str, asyncio.Task] = {}


def touch_scan_heartbeat(channel_id: int) -> None:
    ACTIVE_SCAN_HEARTBEATS[channel_id] = monotonic()


def is_eligible_watch_channel(channel: Any) -> bool:
    return isinstance(channel, discord.TextChannel) and getattr(channel, "type", None) in {
        discord.ChannelType.text,
        discord.ChannelType.news,
    }


def channel_permission_issues(channel: discord.TextChannel) -> tuple[List[str], List[str]]:
    member = channel.guild.me
    if member is None:
        return ["Bot member is unavailable"], []
    perms = channel.permissions_for(member)
    blocking = []
    warnings = []
    if not perms.view_channel:
        blocking.append("View Channel")
    if not perms.read_message_history:
        blocking.append("Read Message History")
    if not perms.send_messages:
        warnings.append("Send Messages")
    if not perms.attach_files:
        warnings.append("Attach Files")
    return blocking, warnings


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
    archive_generation: int,
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
            import_key = (message.channel.id, message.id, idx)

            if await ARCHIVE.is_attachment_archived(
                message.channel.id,
                message.id,
                idx,
            ):
                continue

            async with IMPORTING_ATTACHMENT_LOCK:
                if import_key in IMPORTING_ATTACHMENT_KEYS:
                    continue

                IMPORTING_ATTACHMENT_KEYS.add(import_key)

            try:
                try:
                    data = await fetch_attachment_bytes(http, att)
                    result = await ARCHIVE.import_epub_bytes(
                        guild_id=GUILD_ID,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        attachment_index=idx,
                        filename=att.filename,
                        attachment_size=att.size,
                        message_created_at=message.created_at,
                        epub_bytes=data,
                        archive_generation=archive_generation,
                    )
                    if result == "imported":
                        imported_count += 1
                        if scan_progress is not None:
                            scan_progress.archived_epubs += 1
                            scan_progress.update(att.filename)
                        else:
                            channel_name = safe_log_text(getattr(message.channel, "name", message.channel.id), 80)
                            log_success(f"Archived {safe_log_text(att.filename)} from #{channel_name}")
                except Exception as exc:
                    log_warning(
                        f"Import failed for {safe_log_text(att.filename)} "
                        f"in message {message.id}: {safe_log_text(exc)}"
                    )
                    try:
                        should_notify = await ARCHIVE.record_import_failure(
                            guild_id=GUILD_ID,
                            channel_id=message.channel.id,
                            message_id=message.id,
                            attachment_index=idx,
                            filename=att.filename,
                            error_text=str(exc),
                            archive_generation=archive_generation,
                        )
                        if should_notify:
                            await ARCHIVE_FAILURE_NOTIFIER.queue_failure(
                                channel_id=message.channel.id,
                                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                                message_id=message.id,
                                attachment_index=idx,
                                filename=att.filename,
                            )
                    except Exception as record_exc:
                        log_warning(
                            f"Could not record import failure for {safe_log_text(att.filename)}: "
                            f"{safe_log_text(record_exc)}"
                        )
            finally:
                async with IMPORTING_ATTACHMENT_LOCK:
                    IMPORTING_ATTACHMENT_KEYS.discard(import_key)

    return imported_count


def enqueue_live_message_epubs(message: discord.Message, archive_generation: int) -> int:
    ensure_live_import_worker_started()
    queued = 0
    message_key = (message.channel.id, message.id)

    for idx, att in enumerate(message.attachments):
        if not is_epub_attachment(att):
            continue

        key = (message.channel.id, message.id, idx)

        if key in QUEUED_LIVE_IMPORT_KEYS:
            continue

        QUEUED_LIVE_IMPORT_KEYS.add(key)
        LIVE_IMPORT_PENDING_BY_MESSAGE[message_key] = (
            LIVE_IMPORT_PENDING_BY_MESSAGE.get(message_key, 0) + 1
        )
        LIVE_IMPORT_QUEUE.put_nowait((message, idx, archive_generation))
        queued += 1

    return queued


async def live_import_worker() -> None:
    while True:
        message, attachment_index, archive_generation = await LIVE_IMPORT_QUEUE.get()
        key = (message.channel.id, message.id, attachment_index)
        message_key = (message.channel.id, message.id)

        try:
            created_at = message.created_at

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            available_at = created_at + timedelta(seconds=LIVE_IMPORT_DELAY_SECONDS)
            wait_seconds = max(0.0, (available_at - now_utc()).total_seconds())

            if wait_seconds:
                await asyncio.sleep(wait_seconds)

            row = await get_watched_channel_row(message.channel.id)
            if row is None or row["archive_generation"] != archive_generation:
                continue

            try:
                fresh_message = await message.channel.fetch_message(message.id)
            except discord.NotFound:
                continue

            await import_message_epubs(
                fresh_message,
                archive_generation,
                only_attachment_indexes={attachment_index},
            )
        except Exception as exc:
            log_warning(f"Queued live import failed for message {message.id}: {safe_log_text(exc)}")
            if DEBUG_LOGS:
                traceback.print_exc()
        finally:
            QUEUED_LIVE_IMPORT_KEYS.discard(key)
            remaining = LIVE_IMPORT_PENDING_BY_MESSAGE.get(message_key, 0) - 1

            if remaining <= 0:
                LIVE_IMPORT_PENDING_BY_MESSAGE.pop(message_key, None)
                await advance_channel_last_processed_message(
                    message.channel.id,
                    message.id,
                    archive_generation,
                )
            else:
                LIVE_IMPORT_PENDING_BY_MESSAGE[message_key] = remaining

            LIVE_IMPORT_QUEUE.task_done()
            await asyncio.sleep(LIVE_IMPORT_RATE_SECONDS)


async def start_historical_scan(channel: discord.TextChannel, archive_generation: int) -> str:
    row = await get_watched_channel_row(channel.id)
    if row is None or row["archive_generation"] != archive_generation:
        return "skipped"

    if row["historical_scan_complete"]:
        return "skipped"

    progress = ScanProgress(channel.name)
    try:
        touch_scan_heartbeat(channel.id)
        progress.start()
        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_scan_started_at=unix_now(),
            last_error=None,
        )
        anchor = row["scan_anchor_message_id"]
        before_id = row["historical_before_message_id"]

        if anchor is None:
            latest = None
            async for msg in channel.history(limit=1):
                latest = msg
                touch_scan_heartbeat(channel.id)
                break
            if latest is None:
                await normalize_channel_effective_order(channel.id, archive_generation)
                await update_channel_cursor(
                    channel.id,
                    archive_generation,
                    historical_scan_complete=1,
                    last_scan_finished_at=unix_now(),
                    historical_before_message_id=None,
                )
                progress.finish()
                return "complete"
            anchor = latest.id
            before_id = latest.id + 1
            await update_channel_cursor(
                channel.id,
                archive_generation,
                scan_anchor_message_id=anchor,
                historical_before_message_id=before_id,
            )

        while True:
            current = await get_watched_channel_row(channel.id)
            if current is None or current["archive_generation"] != archive_generation:
                progress.stop_without_summary()
                return "stopped"
            before_id = current["historical_before_message_id"] or before_id
            batch = [
                msg
                async for msg in channel.history(
                    limit=HISTORY_BATCH_SIZE,
                    before=discord.Object(id=before_id),
                )
            ]
            touch_scan_heartbeat(channel.id)

            if not batch:
                await normalize_channel_effective_order(channel.id, archive_generation)
                await update_channel_cursor(
                    channel.id,
                    archive_generation,
                    historical_scan_complete=1,
                    historical_before_message_id=None,
                    last_scan_finished_at=unix_now(),
                    last_error=None,
                )
                progress.finish()
                return "complete"

            for msg in batch:
                progress.scanned_messages += 1
                touch_scan_heartbeat(channel.id)
                await import_message_epubs(msg, archive_generation, scan_progress=progress)
                touch_scan_heartbeat(channel.id)
                progress.update()

            await update_channel_cursor(
                channel.id,
                archive_generation,
                historical_before_message_id=batch[-1].id,
                last_processed_message_id=max(
                    batch[0].id,
                    current["last_processed_message_id"] or 0,
                ),
            )
            touch_scan_heartbeat(channel.id)
    except discord.Forbidden:
        progress.stop_without_summary()
        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_error="Missing permission to read message history",
        )
        log_warning(f"Cannot scan #{channel.name}: missing Read Message History")
        return "failed"
    except asyncio.CancelledError:
        progress.stop_without_summary()
        try:
            await update_channel_cursor(
                channel.id,
                archive_generation,
                last_error="Scan cancelled before completion",
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        progress.stop_without_summary()
        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_error=str(exc)[:1000],
        )
        log_warning(f"Historical scan failed for #{channel.name}: {exc}")
        if DEBUG_LOGS:
            traceback.print_exc()
        return "failed"


async def enqueue_historical_scan(
    channel: discord.TextChannel,
    source: str = "channel",
    archive_generation: Optional[int] = None,
) -> bool:
    row = await get_watched_channel_row(channel.id)

    if row is None or row["historical_scan_complete"]:
        return False

    generation = row["archive_generation"]
    if archive_generation is not None and archive_generation != generation:
        return False

    if QUEUED_SCAN_GENERATIONS.get(channel.id) == generation or channel.id in ACTIVE_SCAN_CHANNEL_IDS:
        return False

    QUEUED_SCAN_GENERATIONS[channel.id] = generation
    await SCAN_QUEUE.put((channel, source, generation))
    return True


def scan_queue_size() -> int:
    return len(QUEUED_SCAN_GENERATIONS)


async def requeue_scan_after_watchdog(
    channel: discord.TextChannel,
    source: str,
    archive_generation: int,
) -> bool:
    row = await get_watched_channel_row(channel.id)

    if (
        row is None
        or row["historical_scan_complete"]
        or row["archive_generation"] != archive_generation
    ):
        return False

    if QUEUED_SCAN_GENERATIONS.get(channel.id) == archive_generation or channel.id in ACTIVE_SCAN_CHANNEL_IDS:
        return False

    QUEUED_SCAN_GENERATIONS[channel.id] = archive_generation
    await SCAN_QUEUE.put((channel, source, archive_generation))
    return True


async def run_scan_with_watchdog(channel: discord.TextChannel, archive_generation: int) -> str:
    touch_scan_heartbeat(channel.id)
    scan_task = asyncio.create_task(start_historical_scan(channel, archive_generation))
    ACTIVE_SCAN_TASKS[channel.id] = scan_task
    check_seconds = max(1, min(5, SCAN_WATCHDOG_SECONDS // 6 or 1))

    try:
        while True:
            done, _ = await asyncio.wait({scan_task}, timeout=check_seconds)

            if scan_task in done:
                try:
                    return scan_task.result()
                except asyncio.CancelledError:
                    return "stopped"
                except Exception as exc:
                    log_warning(
                        f"Historical scan task failed for #{channel.name}: "
                        f"{safe_log_text(exc)}"
                    )
                    return "failed"

            idle_seconds = monotonic() - ACTIVE_SCAN_HEARTBEATS.get(channel.id, monotonic())

            if idle_seconds < SCAN_WATCHDOG_SECONDS:
                continue

            message = (
                f"Scan watchdog timeout in #{channel.name} - "
                f"no progress for {int(idle_seconds)}s; cancelling and re-queueing"
            )
            log_warning(message)
            await update_channel_cursor(
                channel.id,
                archive_generation,
                last_error=message,
            )
            scan_task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await scan_task

            await update_channel_cursor(
                channel.id,
                archive_generation,
                last_error=message,
            )
            return "watchdog_timeout"
    except asyncio.CancelledError:
        scan_task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await scan_task

        raise
    finally:
        if ACTIVE_SCAN_TASKS.get(channel.id) is scan_task:
            ACTIVE_SCAN_TASKS.pop(channel.id, None)


async def historical_scan_worker() -> None:
    while True:
        channel, source, archive_generation = await SCAN_QUEUE.get()
        if QUEUED_SCAN_GENERATIONS.get(channel.id) != archive_generation:
            SCAN_QUEUE.task_done()
            continue
        QUEUED_SCAN_GENERATIONS.pop(channel.id, None)
        ACTIVE_SCAN_CHANNEL_IDS.add(channel.id)
        status = "failed"

        try:
            status = await run_scan_with_watchdog(channel, archive_generation)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            ACTIVE_SCAN_HEARTBEATS.pop(channel.id, None)
            SCAN_QUEUE.task_done()
            try:
                if channel.id in CHANNEL_MAINTENANCE_IDS:
                    await ARCHIVE_FAILURE_NOTIFIER.discard_channel(channel.id)
                else:
                    await ARCHIVE_FAILURE_NOTIFIER.flush_channel(channel.id)
            except Exception as exc:
                log_warning(f"Could not flush archive alerts for #{channel.name}: {safe_log_text(exc)}")
            finally:
                ACTIVE_SCAN_CHANNEL_IDS.discard(channel.id)

            if status == "complete":
                label = {
                    "category": "Category scan complete",
                    "startup": "Startup scan complete",
                }.get(source, "Channel scan complete")
                log_success(
                    f"{label} for #{channel.name} - "
                    f"Queue remaining: {scan_queue_size()}"
                )
            elif status in {"failed", "stopped"}:
                log_warning(
                    f"Scan stopped before completion for #{channel.name} - "
                    f"Queue remaining: {scan_queue_size()}"
                )
            elif status == "watchdog_timeout":
                requeued = await requeue_scan_after_watchdog(
                    channel,
                    source,
                    archive_generation,
                )
                if requeued:
                    log_warning(
                        f"Scan watchdog restarted #{channel.name} from saved cursor - "
                        f"Queue remaining: {scan_queue_size()}"
                    )
                else:
                    log_warning(
                        f"Scan watchdog stopped #{channel.name}; channel is no longer queued "
                        "or historical scan is already complete"
                    )


async def invalidate_channel_work(channel_id: int) -> None:
    QUEUED_SCAN_GENERATIONS.pop(channel_id, None)
    for name in (f"catch-up-{channel_id}", f"retry-imports-{channel_id}"):
        background = BACKGROUND_TASKS.get(name)
        if background is not None:
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background
    task = ACTIVE_SCAN_TASKS.get(channel_id)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    while channel_id in ACTIVE_SCAN_CHANNEL_IDS:
        await asyncio.sleep(0)


def is_channel_in_maintenance(channel_id: int) -> bool:
    return channel_id in CHANNEL_MAINTENANCE_IDS


async def reset_and_enqueue_channel(
    channel: discord.TextChannel,
    expected_generation: int,
) -> tuple[int, bool]:
    CHANNEL_MAINTENANCE_IDS.add(channel.id)
    try:
        await ARCHIVE_FAILURE_NOTIFIER.discard_channel(channel.id)
        await invalidate_channel_work(channel.id)
        for key, session in list(SESSIONS.items()):
            if session.channel_id == channel.id:
                session.expired = True
                SESSIONS.pop(key, None)
        await ARCHIVE_FAILURE_NOTIFIER.discard_channel(channel.id)
        new_generation = await hard_reset_channel(
            channel_id=channel.id,
            expected_generation=expected_generation,
            category_id=getattr(getattr(channel, "category", None), "id", None),
            channel_name=channel.name,
        )
        queued = await enqueue_historical_scan(
            channel,
            source="rescan",
            archive_generation=new_generation,
        )
        return new_generation, queued
    finally:
        CHANNEL_MAINTENANCE_IDS.discard(channel.id)


def ensure_background_task(name: str, factory, restart: bool = True) -> asyncio.Task:
    current = BACKGROUND_TASKS.get(name)
    if current is not None and not current.done():
        return current

    task = asyncio.create_task(factory(), name=name)
    BACKGROUND_TASKS[name] = task

    def completed(done: asyncio.Task) -> None:
        if BACKGROUND_TASKS.get(name) is done:
            BACKGROUND_TASKS.pop(name, None)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            log_warning(f"Background task {name} stopped: {safe_log_text(error)}")
            if DEBUG_LOGS:
                traceback.print_exception(error)
        if restart and not bot.is_closed():
            asyncio.get_running_loop().call_later(
                5,
                lambda: ensure_background_task(name, factory, restart=True),
            )

    task.add_done_callback(completed)
    return task


def ensure_scan_worker_started() -> None:
    ensure_background_task("historical-scan-worker", historical_scan_worker)


def ensure_live_import_worker_started() -> None:
    ensure_background_task("live-import-worker", live_import_worker)


def ensure_support_tasks_started() -> None:
    ensure_background_task("session-cleanup", cleanup_sessions)
    ensure_background_task("category-reconcile", category_reconcile_loop)


def start_startup_channel_work() -> None:
    ensure_background_task("startup-channel-work", startup_channel_work, restart=False)


async def catch_up_channel(channel: discord.TextChannel) -> None:
    row = await get_watched_channel_row(channel.id)
    if row is None:
        return
    archive_generation = row["archive_generation"]

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
            await import_message_epubs(msg, archive_generation)
            latest_seen = max(latest_seen, msg.id)

        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_processed_message_id=latest_seen or None,
            last_catchup_completed_at=unix_now(),
            last_error=None,
        )
    except discord.Forbidden:
        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_error="Missing permission to catch up channel history",
        )
        log_warning(f"Cannot catch up #{channel.name}: missing Read Message History")
    except Exception as exc:
        await update_channel_cursor(
            channel.id,
            archive_generation,
            last_error=str(exc)[:1000],
        )
        log_warning(f"Catch-up failed for #{channel.name}: {exc}")
        if DEBUG_LOGS:
            traceback.print_exc()
    finally:
        if channel.id in CHANNEL_MAINTENANCE_IDS:
            await ARCHIVE_FAILURE_NOTIFIER.discard_channel(channel.id)
        else:
            await ARCHIVE_FAILURE_NOTIFIER.flush_channel(channel.id)


async def retry_channel_import_failures(channel: discord.TextChannel) -> None:
    watch = await get_watched_channel_row(channel.id)
    if watch is None:
        return
    archive_generation = watch["archive_generation"]
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

        await import_message_epubs(
            message,
            archive_generation,
            only_attachment_indexes=attachment_indexes,
        )

    await ARCHIVE_FAILURE_NOTIFIER.flush_channel(channel.id)


async def reconcile_watched_category(category: discord.CategoryChannel) -> Dict[str, int]:
    added = 0
    queued = 0
    checked = 0
    skipped = 0
    warnings = 0
    for channel in category.channels:
        if not is_eligible_watch_channel(channel):
            continue
        checked += 1
        blocking, permission_warnings = channel_permission_issues(channel)
        if blocking:
            skipped += 1
            log_warning(f"Skipping #{channel.name}: missing {', '.join(blocking)}")
            continue
        if permission_warnings:
            warnings += 1
            log_warning(
                f"Permission warning in #{channel.name}: missing {', '.join(permission_warnings)}"
            )
        before = await get_watched_channel_row(channel.id)
        await upsert_watched_channel(channel, True)
        if before is None:
            if await enqueue_historical_scan(channel, source="category"):
                added += 1
                queued += 1
        elif not before["historical_scan_complete"]:
            if await enqueue_historical_scan(channel, source="category"):
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
    if queued:
        log_success(
            f"Category scan initialized for {category.name} - "
            f"Checked {checked} channels - Added {queued} channel(s) to queue - "
            f"New channels: {added} - Total in queue: {scan_queue_size()}"
        )
    return {
        "added": added,
        "queued": queued,
        "checked": checked,
        "skipped": skipped,
        "warnings": warnings,
    }


async def reconcile_all_categories_once() -> None:
    guild = get_configured_guild()
    if guild is None:
        return

    for row in await watched_categories():
        try:
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
            stats = await reconcile_watched_category(category)
            if stats["added"]:
                log_success(
                    f"Category reconciliation added {stats['added']} channel(s) from {category.name}"
                )
        except Exception as exc:
            log_warning(
                f"Category reconciliation failed for {row['category_id']}: {safe_log_text(exc)}"
            )
            if DEBUG_LOGS:
                traceback.print_exc()


async def category_reconcile_loop() -> None:
    while True:
        try:
            if get_configured_guild() is not None:
                await reconcile_all_categories_once()
        except Exception as exc:
            log_warning(f"Category reconciliation failed: {safe_log_text(exc)}")
            if DEBUG_LOGS:
                traceback.print_exc()
        await asyncio.sleep(CATEGORY_RECONCILE_SECONDS)


async def startup_channel_work() -> None:
    guild = get_configured_guild()
    if guild is None:
        return
    await reconcile_all_categories_once()

    for row in await watched_channels():
        try:
            channel = guild.get_channel(row["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                continue
            log_channel_permission_diagnostics(channel)
            ensure_background_task(
                f"retry-imports-{channel.id}",
                lambda channel=channel: retry_channel_import_failures(channel),
                restart=False,
            )
            ensure_background_task(
                f"catch-up-{channel.id}",
                lambda channel=channel: catch_up_channel(channel),
                restart=False,
            )
            if not row["historical_scan_complete"]:
                await enqueue_historical_scan(channel, source="startup")
        except Exception as exc:
            log_warning(f"Startup work failed for channel {row['channel_id']}: {safe_log_text(exc)}")
            if DEBUG_LOGS:
                traceback.print_exc()


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

