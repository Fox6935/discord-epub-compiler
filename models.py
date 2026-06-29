import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from config import MAX_CONCURRENT_COMPILES, PAGE_SIZE


class OutputTooLargeError(ValueError):
    pass


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


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

            return entries

        return self.entries

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

    def all_selected_on_page(self) -> bool:
        page_entries = self.current_page_entries()
        selected = self.placement_ids if self.flow_mode == "reorder_place" else self.selected_ids

        return bool(page_entries) and all(
            entry.entry_id in selected for entry in page_entries
        )

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
