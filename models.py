import asyncio
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from config import (
    CHAPTER_ZIP_COMPRESSION_RATIO,
    EPUB_SHELL_OVERHEAD_BYTES,
    MAX_CONCURRENT_COMPILES,
    PAGE_SIZE,
)
from filter import FilenameFilter


class OutputTooLargeError(ValueError):
    pass


DEBUG_LOGS = os.getenv("DEBUG_LOGS", "").lower() in {"1", "true", "yes", "on"}
USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")
USE_PROGRESS = sys.stdout.isatty()

GREEN = "\033[32m"
ORANGE = "\033[33m"
RESET = "\033[0m"

if not DEBUG_LOGS:
    for logger_name in ("discord", "discord.client", "discord.gateway", "aiohttp"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.DEBUG if DEBUG_LOGS else logging.WARNING,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_ACTIVE_PROGRESS: Optional["ScanProgress"] = None
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_log_text(value: object, limit: int = 180) -> str:
    clean = CONTROL_CHAR_RE.sub(" ", str(value))
    clean = " ".join(clean.split())

    if len(clean) > limit:
        if limit <= 3:
            return clean[:limit]

        return clean[: limit - 3] + "..."

    return clean


def _terminal_width() -> int:
    return max(shutil.get_terminal_size((100, 20)).columns, 40)


def _colorize(msg: str, level: str) -> str:
    if not USE_COLOR:
        return msg

    if level == "success":
        return f"{GREEN}{msg}{RESET}"

    if level == "warning":
        return f"{ORANGE}{msg}{RESET}"

    return msg


def _clear_progress_line() -> None:
    if USE_PROGRESS:
        sys.stdout.write("\r" + (" " * max(_terminal_width() - 4, 1)) + "\r")
        sys.stdout.flush()


def _write_progress_line(msg: str) -> None:
    if not USE_PROGRESS:
        return

    width = max(_terminal_width() - 4, 1)
    clean = safe_log_text(msg, width)
    sys.stdout.write("\r" + clean.ljust(width))
    sys.stdout.flush()


def _write_log(msg: str, level: str = "info") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {_colorize(safe_log_text(msg, 2000), level)}", flush=True)


def log(msg: str, level: str = "info") -> None:
    active = _ACTIVE_PROGRESS

    if active is not None and active.started:
        _clear_progress_line()

    _write_log(msg, level)

    if active is not None and active.started:
        active.render()


def log_success(msg: str) -> None:
    log(msg, "success")


def log_warning(msg: str) -> None:
    log(msg, "warning")


class ScanProgress:
    def __init__(self, channel_name: str):
        self.channel_name = safe_log_text(channel_name, 80)
        self.scanned_messages = 0
        self.archived_epubs = 0
        self.started = False
        self.current_filename: Optional[str] = None

    def start(self) -> None:
        global _ACTIVE_PROGRESS
        _ACTIVE_PROGRESS = self
        self.started = True
        log(f"Starting scan in #{self.channel_name}")
        self.render()

    def update(self, current_filename: Optional[str] = None) -> None:
        if current_filename:
            self.current_filename = safe_log_text(current_filename)
        self.render()

    def render(self) -> None:
        if not self.started:
            return

        line = (
            f"Scanning #{self.channel_name} - "
            f"Scanned {self.scanned_messages} messages - "
            f"Archived {self.archived_epubs} EPUBs"
        )

        if self.current_filename:
            line += f" - {self.current_filename}"

        _write_progress_line(_colorize(line, "info"))

    def finish(self) -> None:
        global _ACTIVE_PROGRESS

        if self.started:
            _clear_progress_line()
            self.started = False

        if _ACTIVE_PROGRESS is self:
            _ACTIVE_PROGRESS = None

        log_success(
            f"Scan complete in #{self.channel_name} - "
            f"Scanned {self.scanned_messages} messages - "
            f"Archived {self.archived_epubs} EPUBs"
        )

    def stop_without_summary(self) -> None:
        global _ACTIVE_PROGRESS

        if self.started:
            _clear_progress_line()
            self.started = False

        if _ACTIVE_PROGRESS is self:
            _ACTIVE_PROGRESS = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EpubEntry:
    entry_id: str
    discord_epub_id: int
    epub_version_id: int
    channel_id: int
    message_id: int
    attachment_index: int
    filename: str
    attachment_size: Optional[int]
    created_at: datetime
    effective_order: int
    is_deleted: bool = False
    estimated_chapter_bytes: int = 0
    estimated_image_bytes: int = 0
    image_blob_sizes: Tuple[Tuple[str, int], ...] = ()


@dataclass
class CompileSession:
    user_id: int
    channel_id: int
    channel_name: str
    entries: List[EpubEntry] = field(default_factory=list)
    selected_ids: Set[str] = field(default_factory=set)
    placement_ids: Set[str] = field(default_factory=set)
    current_page: int = 0
    flow_mode: str = "compile"
    reorder_moving_id: Optional[str] = None
    filename_filter: FilenameFilter = field(default_factory=FilenameFilter)
    remove_all_images: bool = False
    expired: bool = False
    created_at: datetime = field(default_factory=now_utc)
    last_accessed_at: datetime = field(default_factory=now_utc)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.last_accessed_at = now_utc()

    @property
    def page_count(self) -> int:
        entries = self.display_entries()

        if not entries:
            return 1

        if self.flow_mode == "reorder_place":
            step = PAGE_SIZE - 1
            return max(1, (len(entries) - 1 + step - 1) // step)

        return (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE

    def display_entries(self) -> List[EpubEntry]:
        if self.flow_mode in {"reorder_move", "reorder_place"}:
            entries = sorted(
                self.entries,
                key=lambda e: e.effective_order,
                reverse=True,
            )

            if self.flow_mode == "reorder_place" and self.reorder_moving_id:
                entries = [e for e in entries if e.entry_id != self.reorder_moving_id]

            return self._apply_filename_filter(entries)

        return self._apply_filename_filter(self.entries)

    def _apply_filename_filter(self, entries: List[EpubEntry]) -> List[EpubEntry]:
        if not self.filename_filter.is_active:
            return entries

        return [
            entry
            for entry in entries
            if self.filename_filter.matches(entry.filename)
        ]

    def get_page_entries(self, page: int) -> List[EpubEntry]:
        entries = self.display_entries()

        if self.flow_mode == "reorder_place":
            start = page * (PAGE_SIZE - 1)
            end = start + PAGE_SIZE
        else:
            start = page * PAGE_SIZE
            end = start + PAGE_SIZE

        return entries[start:end]

    def current_page_entries(self) -> List[EpubEntry]:
        return self.get_page_entries(self.current_page)

    def sort_selected_for_compile(self) -> List[EpubEntry]:
        selected = [e for e in self.entries if e.entry_id in self.selected_ids]
        selected.sort(key=lambda e: e.effective_order)
        return selected

    def estimated_output_bytes(self) -> Optional[int]:
        selected = self.sort_selected_for_compile()

        if not selected:
            return None

        chapter_bytes = sum(entry.estimated_chapter_bytes for entry in selected)
        image_bytes = 0

        if not self.remove_all_images:
            image_sizes_by_hash: Dict[str, int] = {}

            for entry in selected:
                for blob_hash, blob_size in entry.image_blob_sizes:
                    image_sizes_by_hash.setdefault(blob_hash, blob_size)

            image_bytes = sum(image_sizes_by_hash.values())

        return (
            int(chapter_bytes * CHAPTER_ZIP_COMPRESSION_RATIO)
            + image_bytes
            + EPUB_SHELL_OVERHEAD_BYTES
        )

    def selected_image_bytes(self) -> int:
        image_sizes_by_hash: Dict[str, int] = {}

        for entry in self.sort_selected_for_compile():
            for blob_hash, blob_size in entry.image_blob_sizes:
                image_sizes_by_hash.setdefault(blob_hash, blob_size)

        return sum(image_sizes_by_hash.values())

    def page_range_label(self) -> str:
        if not self.display_entries():
            return "00-00"

        if self.flow_mode == "reorder_place":
            start = self.current_page * (PAGE_SIZE - 1) + 1
        else:
            start = self.current_page * PAGE_SIZE + 1

        end = min(len(self.display_entries()), start + PAGE_SIZE - 1)

        return f"{start:02d}-{end:02d}"

SESSIONS: Dict[Tuple[int, int], CompileSession] = {}
COMPILE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_COMPILES)


def build_session_key(user_id: int, channel_id: int) -> Tuple[int, int]:
    return user_id, channel_id


def is_session_live(session: CompileSession) -> bool:
    key = build_session_key(session.user_id, session.channel_id)
    return SESSIONS.get(key) is session and not session.expired
