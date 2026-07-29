import asyncio
import contextlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import discord

from config import ARCHIVE_FAILURE_ALERT_DELAY_SECONDS, bot
from db import (
    list_archive_failure_role_ids,
    mark_import_failures_notified,
    unresolved_import_failures,
)
from models import log_warning, safe_log_text


FailureKey = Tuple[int, int, int]


@dataclass
class PendingFailureBatch:
    channel_name: str
    failures: "OrderedDict[FailureKey, str]" = field(default_factory=OrderedDict)
    task: asyncio.Task | None = None


class ArchiveFailureNotifier:
    def __init__(self) -> None:
        self._pending: Dict[int, PendingFailureBatch] = {}
        self._lock = asyncio.Lock()

    async def queue_failure(
        self,
        channel_id: int,
        channel_name: str,
        message_id: int,
        attachment_index: int,
        filename: str,
    ) -> None:
        key = (channel_id, message_id, attachment_index)
        async with self._lock:
            batch = self._pending.setdefault(
                channel_id,
                PendingFailureBatch(channel_name=channel_name),
            )
            batch.channel_name = channel_name
            batch.failures[key] = filename
            if batch.task is not None:
                batch.task.cancel()
            batch.task = asyncio.create_task(self._delayed_flush(channel_id))

    async def _delayed_flush(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(ARCHIVE_FAILURE_ALERT_DELAY_SECONDS)
            await self.flush_channel(channel_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_warning(f"Could not send archive failure batch: {safe_log_text(exc)}")

    async def flush_channel(self, channel_id: int) -> None:
        async with self._lock:
            batch = self._pending.pop(channel_id, None)

        if batch is None:
            return

        current = asyncio.current_task()
        if batch.task is not None and batch.task is not current:
            batch.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await batch.task

        rows = await unresolved_import_failures(batch.failures.keys())
        if not rows:
            return

        filenames = {
            (row["channel_id"], row["message_id"], row["attachment_index"]): row["filename"]
            for row in rows
        }
        ordered = [
            (key, filenames[key])
            for key in batch.failures
            if key in filenames
        ]
        alert_role_ids = await list_archive_failure_role_ids()

        if not alert_role_ids:
            log_warning(
                f"Archive failures in #{safe_log_text(batch.channel_name, 80)} "
                "could not be announced because no alert roles are configured"
            )
            return

        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log_warning(f"Could not resolve archive alert channel {channel_id}: {safe_log_text(exc)}")
                return

        guild = getattr(channel, "guild", None)
        if guild is None or not hasattr(channel, "send"):
            log_warning(f"Archive alert destination {channel_id} is not a server message channel")
            return

        roles = [role for role_id in alert_role_ids if (role := guild.get_role(role_id)) is not None]
        if not roles:
            log_warning(
                f"Archive failures in #{safe_log_text(batch.channel_name, 80)} "
                "could not be announced because none of the configured alert roles exist"
            )
            return

        role_mentions = " ".join(role.mention for role in roles)
        chunks = build_failure_channel_chunks(ordered, role_mentions, guild.id)

        for content, keys in chunks:
            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=False,
                        roles=roles,
                    ),
                )
                await mark_import_failures_notified(keys)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log_warning(
                    f"Could not send archive alert in channel {channel_id}: {safe_log_text(exc)}"
                )

    async def discard_channel(self, channel_id: int) -> None:
        async with self._lock:
            batch = self._pending.pop(channel_id, None)
        if batch is not None and batch.task is not None:
            batch.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await batch.task


def build_failure_channel_chunks(
    failures: List[Tuple[FailureKey, str]],
    role_mentions: str,
    guild_id: int,
) -> List[Tuple[str, List[FailureKey]]]:
    grouped: "OrderedDict[Tuple[int, int], List[Tuple[FailureKey, str]]]" = OrderedDict()
    for key, filename in failures:
        grouped.setdefault((key[0], key[1]), []).append((key, filename))

    chunks: List[Tuple[str, List[FailureKey]]] = []
    lines: List[str] = []
    keys: List[FailureKey] = []
    header = f"{role_mentions} \n"

    def content_length(candidate_lines: List[str]) -> int:
        return len(header) + len("\n".join(candidate_lines))

    def flush() -> None:
        nonlocal lines, keys
        if lines:
            chunks.append((header + "\n".join(lines), keys))
            lines = []
            keys = []

    for (channel_id, message_id), group in grouped.items():
        link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        archive_line = f"Archive Failed: {link}"
        max_filename = max(1, 2000 - len(header) - len(archive_line) - len("\n  - "))
        filename_rows: List[Tuple[FailureKey, str]] = []

        for key, filename in group:
            safe_filename = filename.replace("\r", "_").replace("\n", "_")
            if len(safe_filename) > max_filename:
                safe_filename = (
                    safe_filename[: max_filename - 3] + "..."
                    if max_filename > 3
                    else safe_filename[:max_filename]
                )
            filename_rows.append((key, f"  - {safe_filename}"))

        complete_block = [archive_line, *(line for _, line in filename_rows)]
        if content_length(lines + complete_block) <= 2000:
            lines.extend(complete_block)
            keys.extend(key for key, _ in filename_rows)
            continue

        flush()
        if content_length(complete_block) <= 2000:
            lines.extend(complete_block)
            keys.extend(key for key, _ in filename_rows)
            continue

        lines.append(archive_line)
        for key, filename_line in filename_rows:
            if content_length(lines + [filename_line]) > 2000:
                flush()
                lines.append(archive_line)
            lines.append(filename_line)
            keys.append(key)

    flush()

    return chunks


ARCHIVE_FAILURE_NOTIFIER = ArchiveFailureNotifier()
