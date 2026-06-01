import asyncio
import contextlib
import hashlib
import io
import mimetypes
import os
import posixpath
import random
import re
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from discord.ext import commands
from dotenv import load_dotenv
from xml.etree import ElementTree as ET

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

SESSION_TIMEOUT_SECONDS = 15 * 60
MAX_SESSION_LIFETIME_SECONDS = 60 * 60
INITIAL_LOAD_ATTACHMENTS = 100
PAGE_SIZE = 25

MAX_SOURCE_EPUB_BYTES = 50 * 1024 * 1024
MAX_SOURCE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_SINGLE_FILE_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
DEFAULT_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024        # I didn't confirm the upload limit for sure
MAX_OUTPUT_EPUB_BYTES = DEFAULT_UPLOAD_LIMIT_BYTES
EPUB_SHELL_OVERHEAD_BYTES = 48 * 1024
CHAPTER_ZIP_COMPRESSION_RATIO = 0.55
IMAGE_SIZE_ABORT_RATIO = 0.95
HTTP_TIMEOUT_SECONDS = 300
MAX_CONCURRENT_COMPILES = 2

ALLOWED_NAME_RE = re.compile(r"^[A-Za-z0-9 _.,'()\-]+$")
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._\-]")
SAFE_META_RE = re.compile(r"\s+")
EPUB_EXT_RE = re.compile(r"\.epub$", re.IGNORECASE)

CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
EPUB_NS = "http://www.idpf.org/2007/ops"

ET.register_namespace("", XHTML_NS)
ET.register_namespace("svg", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
ET.register_namespace("epub", EPUB_NS)

XML_PARSE_ERRORS = (
    ET.ParseError,
    DefusedXmlException,
    ValueError,
)

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

SESSIONS: Dict[Tuple[int, int], "CompileSession"] = {}
COMPILE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_COMPILES)

GUIDE_SKIP_TYPES = {
    "cover",
    "title-page",
    "titlepage",
    "toc",
}

TEXT_TAGS = {
    "p",
    "div",
    "span",
    "blockquote",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}

IMAGE_TAGS = {"img", "image"}


class OutputTooLargeError(ValueError):
    pass


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def sanitize_output_name(raw: str) -> str:
    raw = raw.strip().strip(".")
    if not raw:
        return "compiled"

    if not ALLOWED_NAME_RE.fullmatch(raw):
        raise ValueError(
            "Only letters, numbers, spaces, dash, underscore, comma, "
            "period, apostrophe, and parentheses are allowed."
        )

    return raw[:120] or "compiled"


def safe_default_output_name(raw: str) -> str:
    try:
        return sanitize_output_name(raw)
    except ValueError:
        clean = SAFE_FILE_RE.sub("_", raw or "").strip("._ ")
        return clean[:120] or "compiled"


def sanitize_author(raw: str) -> str:
    raw = SAFE_META_RE.sub(" ", raw or "").strip()
    raw = "".join(ch for ch in raw if ch.isprintable())
    return raw[:120] or "Discord Channel"


def sanitize_internal_name(name: str, default_stem: str) -> str:
    base = posixpath.basename(name.replace("\\", "/")).strip()

    if not base:
        base = default_stem

    if "." in base:
        stem, ext = base.rsplit(".", 1)
        ext = "." + SAFE_FILE_RE.sub("", ext.lower())
    else:
        stem, ext = base, ""

    stem = SAFE_FILE_RE.sub("_", stem).strip("._")

    if not stem:
        stem = default_stem

    return stem + ext


def safe_output_zip_path(prefix: str, filename: str) -> str:
    clean = sanitize_internal_name(filename, "file")

    if "/" in clean or "\\" in clean:
        raise ValueError(f"Unsafe output filename: {filename}")

    return f"{prefix.rstrip('/')}/{clean}"


def make_unique_name(name: str, used: Set[str]) -> str:
    if name not in used:
        used.add(name)
        return name

    if "." in name:
        stem, ext = name.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = name, ""

    i = 2

    while True:
        candidate = f"{stem}_{i}{ext}"

        if candidate not in used:
            used.add(candidate)
            return candidate

        i += 1


def resolve_href(base_path: str, href: str) -> str:
    clean = href.split("#", 1)[0].strip()

    if not clean:
        raise ValueError("Empty href")

    if "\x00" in clean:
        raise ValueError("Invalid href")

    clean = clean.replace("\\", "/")

    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(base_path), clean)
    )

    if resolved.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"Unsafe href: {href}")

    if any(part == ".." for part in resolved.split("/")):
        raise ValueError(f"Unsafe href: {href}")

    return resolved


def is_epub_attachment(att: discord.Attachment) -> bool:
    return bool(att.filename and EPUB_EXT_RE.search(att.filename))


def has_manifest_property(props: str, prop: str) -> bool:
    return prop in {p.strip().lower() for p in (props or "").split()}


def looks_like_structural_page_by_name(href: str, manifest_props: str) -> bool:
    name = posixpath.basename(href.lower())

    if has_manifest_property(manifest_props, "nav"):
        return True

    structural_names = {
        "nav.xhtml",
        "nav.html",
        "toc.xhtml",
        "toc.html",
        "toc.ncx",
        "cover.xhtml",
        "cover.html",
        "titlepage.xhtml",
        "titlepage.html",
        "title-page.xhtml",
        "title-page.html",
    }

    return name in structural_names


def resolve_upload_limit_bytes(interaction: discord.Interaction) -> int:
    limit = getattr(interaction, "attachment_size_limit", None)

    if isinstance(limit, int) and limit > 0:
        return limit

    return DEFAULT_UPLOAD_LIMIT_BYTES


def estimate_compiled_epub_bytes(
    final_chapters: List[Tuple[str, str, bytes]],
    final_images: Dict[str, bytes],
    remove_all_images: bool,
) -> int:
    chapter_bytes = sum(len(blob) for _, _, blob in final_chapters)
    chapter_part = int(chapter_bytes * CHAPTER_ZIP_COMPRESSION_RATIO)
    image_part = (
        0
        if remove_all_images
        else sum(len(data) for data in final_images.values())
    )

    return chapter_part + image_part + EPUB_SHELL_OVERHEAD_BYTES


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"

            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{size} B"


def get_text_content(elem: Optional[ET.Element]) -> str:
    if elem is None:
        return ""

    return " ".join("".join(elem.itertext()).split()).strip()


@dataclass
class EpubEntry:
    entry_id: str
    channel_id: int
    message_id: int
    attachment_index: int
    filename: str
    url: str
    proxy_url: str
    size: int
    created_at: datetime


@dataclass
class CompileSession:
    user_id: int
    channel_id: int
    channel_name: str
    entries: List[EpubEntry] = field(default_factory=list)
    selected_ids: Set[str] = field(default_factory=set)
    seen_attachments: Set[Tuple[int, int]] = field(default_factory=set)
    current_page: int = 0
    scan_before_message_id: Optional[int] = None
    scan_complete: bool = False
    scan_in_progress: bool = False
    remove_all_images: bool = False
    expired: bool = False
    created_at: datetime = field(default_factory=now_utc)
    last_accessed_at: datetime = field(default_factory=now_utc)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.last_accessed_at = now_utc()

    @property
    def page_count(self) -> int:
        if not self.entries:
            return 1

        return (len(self.entries) + PAGE_SIZE - 1) // PAGE_SIZE

    def get_page_entries(self, page: int) -> List[EpubEntry]:
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        return self.entries[start:end]

    def current_page_entries(self) -> List[EpubEntry]:
        return self.get_page_entries(self.current_page)

    def sort_selected_for_compile(self) -> List[EpubEntry]:
        selected = [e for e in self.entries if e.entry_id in self.selected_ids]
        selected.sort(key=lambda e: (e.created_at, e.message_id, e.attachment_index))
        return selected

    def all_selected_on_page(self) -> bool:
        page_entries = self.current_page_entries()

        return bool(page_entries) and all(
            entry.entry_id in self.selected_ids for entry in page_entries
        )

    def page_range_label(self) -> str:
        if not self.entries:
            return "00–00"

        start = self.current_page * PAGE_SIZE + 1
        end = min(len(self.entries), start + PAGE_SIZE - 1)

        return f"{start:02d}–{end:02d}"

    def scan_status(self) -> str:
        return "Complete" if self.scan_complete else "Partial"


def build_session_key(user_id: int, channel_id: int) -> Tuple[int, int]:
    return user_id, channel_id


def is_session_live(session: CompileSession) -> bool:
    key = build_session_key(session.user_id, session.channel_id)
    return SESSIONS.get(key) is session and not session.expired


class EpubPickerSelect(discord.ui.Select):
    def __init__(self, session: CompileSession):
        page_entries = session.current_page_entries()
        options = []

        for entry in page_entries:
            created = entry.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
            label = entry.filename[:100]
            description = f"{created} • msg {entry.message_id}"

            options.append(
                discord.SelectOption(
                    label=label,
                    value=entry.entry_id,
                    description=description[:100],
                    default=entry.entry_id in session.selected_ids,
                )
            )

        super().__init__(
            placeholder=(
                f"Select EPUBs {session.page_range_label()} "
                f"of {len(session.entries):02d}"
            ),
            min_values=0,
            max_values=max(1, len(options)),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None or not isinstance(view, CompileLayoutView):
            return

        session = view.session

        async with session.lock:
            session.touch()
            page_entries = session.current_page_entries()
            page_ids = {e.entry_id for e in page_entries}

            session.selected_ids.difference_update(page_ids)
            session.selected_ids.update(self.values)

            new_view = CompileLayoutView(session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class PrevPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()

            if view.session.current_page > 0:
                view.session.current_page -= 1

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class NextPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        channel = interaction.channel

        if channel is None:
            await interaction.response.send_message(
                "Channel is unavailable.",
                ephemeral=True,
            )
            return

        async with view.session.lock:
            view.session.touch()

            if view.session.current_page < view.session.page_count - 1:
                view.session.current_page += 1
                new_view = CompileLayoutView(view.session)
                new_view.message = view.message

                await interaction.response.edit_message(view=new_view)
                return

            if view.session.scan_complete:
                await interaction.response.defer()
                return

        await interaction.response.defer()

        ok, error = await load_more_entries(channel, view.session)

        if not ok:
            await interaction.followup.send(error, ephemeral=True)
            return

        async with view.session.lock:
            if view.session.current_page < view.session.page_count - 1:
                view.session.current_page += 1

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        try:
            if view.message is not None:
                await view.message.edit(view=new_view)
            else:
                await interaction.edit_original_response(view=new_view)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Couldn't update picker: {exc}",
                ephemeral=True,
            )


class ToggleRemoveImagesButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, enabled: bool):
        super().__init__(
            label="☑ Remove all images" if enabled else "☐ Remove all images",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            view.session.remove_all_images = not view.session.remove_all_images

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class SelectPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, all_selected: bool, disabled: bool):
        super().__init__(
            label="Deselect Page" if all_selected else "Select Page",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            page_entries = view.session.current_page_entries()
            page_ids = {e.entry_id for e in page_entries}

            if all(e.entry_id in view.session.selected_ids for e in page_entries):
                view.session.selected_ids.difference_update(page_ids)
            else:
                view.session.selected_ids.update(page_ids)

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class ClearPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Clear Page",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            page_ids = {e.entry_id for e in view.session.current_page_entries()}
            view.session.selected_ids.difference_update(page_ids)

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class OpenCompileModalButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self):
        super().__init__(
            label="Compile",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            has_selection = bool(view.session.selected_ids)

        if not has_selection:
            await interaction.response.send_message(
                "Select at least one EPUB first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(CompileNameModal(view.session))


class CompileLayoutView(discord.ui.LayoutView):
    def __init__(self, session: CompileSession):
        super().__init__(timeout=SESSION_TIMEOUT_SECONDS)

        self.session = session
        self.message: Optional[discord.Message] = None

        scan_status = session.scan_status()
        page_entries = session.current_page_entries()

        stats = discord.ui.Container(
            discord.ui.TextDisplay("# 📖 EPUB Compiler"),
            discord.ui.TextDisplay(f"Select EPUBs from <#{session.channel_id}>"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"**EPUBs**: {len(session.entries)}\n"
                f"**Selected**: {len(session.selected_ids)}\n"
                f"**Page**: {session.current_page + 1}/{session.page_count}"
            ),
            accent_colour=discord.Colour.blurple(),
        )
        self.add_item(stats)

        if scan_status == "Partial":
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(
                        "**Scan**: Partial — older results not loaded yet."
                    ),
                    accent_colour=discord.Colour.orange(),
                )
            )
        else:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("**Scan**: Complete — all results loaded."),
                    accent_colour=discord.Colour.green(),
                )
            )

        self.add_item(
            discord.ui.ActionRow(ToggleRemoveImagesButton(session.remove_all_images))
        )

        if page_entries:
            self.add_item(discord.ui.ActionRow(EpubPickerSelect(session)))

        can_advance_loaded = session.current_page < session.page_count - 1
        next_disabled = not (can_advance_loaded or not session.scan_complete)

        self.add_item(
            discord.ui.ActionRow(
                PrevPageButton(disabled=session.current_page == 0),
                NextPageButton(disabled=next_disabled),
            )
        )

        self.add_item(
            discord.ui.ActionRow(
                SelectPageButton(
                    session.all_selected_on_page(),
                    disabled=not bool(page_entries),
                ),
                ClearPageButton(disabled=not bool(page_entries)),
            )
        )

        self.add_item(discord.ui.ActionRow(OpenCompileModalButton()))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "This picker isn't yours.",
                ephemeral=True,
            )
            return False

        if not is_session_live(self.session):
            await interaction.response.send_message(
                "This compile session expired. Run `/compile` again.",
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        self.session.expired = True
        disable_view_items(list(self.children))

        if self.message is not None:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await self.message.edit(view=self)


class CompileNameModal(discord.ui.Modal, title="Compile EPUB"):
    output_name = discord.ui.TextInput(
        label="Output name",
        placeholder="Example: My Compiled Book",
        min_length=1,
        max_length=120,
        required=True,
    )

    def __init__(self, session: CompileSession):
        super().__init__(timeout=300)

        self.session = session
        self.output_name.default = safe_default_output_name(session.channel_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "This modal isn't yours.",
                ephemeral=True,
            )
            return

        if not is_session_live(self.session):
            await interaction.response.send_message(
                "This compile session expired. Run `/compile` again.",
                ephemeral=True,
            )
            return

        try:
            output_name = sanitize_output_name(str(self.output_name))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        async with self.session.lock:
            self.session.touch()
            selected = self.session.sort_selected_for_compile()
            remove_all_images = self.session.remove_all_images

        if not selected:
            await interaction.response.send_message(
                "You haven't selected any EPUBs.",
                ephemeral=True,
            )
            return

        queue_hint = ""

        if COMPILE_SEMAPHORE.locked():
            queue_hint = "\nAnother compile is running, so yours may wait briefly."

        log(
            f"Compiling {len(selected)} EPUB(s) "
            f"for {interaction.user} in #{self.session.channel_name} "
            f"(remove_all_images={remove_all_images})"
        )

        upload_limit = resolve_upload_limit_bytes(interaction)

        await interaction.response.send_message(
            (
                f"Compiling {len(selected)} EPUB(s)...\n"
                f"Remove all images: {'yes' if remove_all_images else 'no'}"
                f"{queue_hint}"
            ),
            ephemeral=True,
        )

        try:
            async with COMPILE_SEMAPHORE:
                output_bytes, skipped = await compile_selected_epubs(
                    selected=selected,
                    title=output_name,
                    author=sanitize_author(self.session.channel_name),
                    remove_all_images=remove_all_images,
                    max_output_bytes=upload_limit,
                )
        except OutputTooLargeError as exc:
            log(f"Compile too large for {interaction.user}: {exc}")
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log(f"Compile failed for {interaction.user}: {exc}")
            traceback.print_exc()
            await interaction.followup.send(
                f"Compile failed: {exc}",
                ephemeral=True,
            )
            return

        if output_bytes is None:
            log(f"All selected EPUBs failed for {interaction.user}")
            detail = "\n".join(f"- {name}: {reason}" for name, reason in skipped)
            msg = "All selected EPUBs failed."

            if detail:
                msg += f"\n{detail}"

            await interaction.followup.send(msg[:1900], ephemeral=True)
            return

        message_lines = [f"Here is your compiled EPUB: {output_name}.epub"]

        if skipped:
            message_lines.append("")
            message_lines.append("Skipped source EPUBs:")
            message_lines.extend(f"- {name}: {reason}" for name, reason in skipped)

        try:
            await interaction.followup.send(
                "\n".join(message_lines)[:1900],
                file=discord.File(
                    io.BytesIO(output_bytes),
                    filename=f"{output_name}.epub",
                ),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log(f"Couldn't send compiled EPUB to {interaction.user}: {exc}")
            await interaction.followup.send(
                (
                    "I couldn't send the compiled EPUB through Discord.\n"
                    f"Compiled size: {format_bytes(len(output_bytes))}\n"
                    f"Upload limit: {format_bytes(upload_limit)}\n"
                    "Try selecting fewer EPUBs or enable `Remove all images`."
                ),
                ephemeral=True,
            )
            return

        log(f"{len(selected)} EPUB(s) compiled and sent to {interaction.user}")


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


async def scan_for_epubs(
    channel: discord.abc.Messageable,
    session: CompileSession,
    target_new_count: int,
) -> Tuple[bool, Optional[str]]:
    async with session.lock:
        if session.scan_in_progress or session.scan_complete:
            return True, None

        session.scan_in_progress = True
        start_count = len(session.entries)

    try:
        while True:
            async with session.lock:
                if session.scan_complete:
                    break

                if len(session.entries) - start_count >= target_new_count:
                    break

                before_id = session.scan_before_message_id

            history_kwargs = {"limit": 100}

            if before_id:
                history_kwargs["before"] = discord.Object(id=before_id)

            try:
                batch = [m async for m in channel.history(**history_kwargs)]
            except discord.Forbidden:
                log(f"Couldn't read message history in #{session.channel_name}")
                return False, "I can't read message history in this channel."
            except discord.HTTPException as exc:
                log(f"Failed to read message history in #{session.channel_name}: {exc}")
                return False, f"Failed to read channel history: {exc}"

            if not batch:
                async with session.lock:
                    session.scan_complete = True
                break

            async with session.lock:
                for msg in batch:
                    for idx in range(len(msg.attachments) - 1, -1, -1):
                        att = msg.attachments[idx]

                        if not is_epub_attachment(att):
                            continue

                        dedupe_key = (msg.id, idx)

                        if dedupe_key in session.seen_attachments:
                            continue

                        session.seen_attachments.add(dedupe_key)
                        session.entries.append(
                            EpubEntry(
                                entry_id=str(uuid.uuid4()),
                                channel_id=msg.channel.id,
                                message_id=msg.id,
                                attachment_index=idx,
                                filename=att.filename,
                                url=att.url,
                                proxy_url=att.proxy_url,
                                size=att.size,
                                created_at=msg.created_at,
                            )
                        )

                session.scan_before_message_id = batch[-1].id

                if len(batch) < 100:
                    session.scan_complete = True
                    break

        return True, None
    finally:
        async with session.lock:
            session.scan_in_progress = False
            session.touch()


async def load_more_entries(
    channel: discord.abc.Messageable,
    session: CompileSession,
) -> Tuple[bool, Optional[str]]:
    return await scan_for_epubs(
        channel=channel,
        session=session,
        target_new_count=INITIAL_LOAD_ATTACHMENTS,
    )


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
                log(f"Ignoring malformed Content-Length: {content_length!r}")
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


async def fetch_epub_bytes(http: aiohttp.ClientSession, entry: EpubEntry) -> bytes:
    last_error: Optional[Exception] = None

    for candidate in [entry.url, entry.proxy_url]:
        if not candidate:
            continue

        try:
            return await fetch_bytes(http, candidate)
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    raise ValueError("No valid attachment URL available")


def parse_xml(data: bytes) -> ET.Element:
    return SafeET.fromstring(data)


def safe_zip_read(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)

    if info.file_size > MAX_SINGLE_FILE_UNCOMPRESSED_BYTES:
        raise ValueError(f"File too large inside EPUB: {name}")

    return zf.read(name)


def validate_zip_member_names(zf: zipfile.ZipFile) -> None:
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")

        if "\x00" in name:
            raise ValueError(f"Invalid EPUB path: {info.filename}")

        if name.startswith("/"):
            raise ValueError(f"Unsafe absolute EPUB path: {info.filename}")

        parts = name.split("/")

        if any(part == ".." for part in parts):
            raise ValueError(f"Unsafe EPUB path traversal: {info.filename}")


def validate_zip_sizes(zf: zipfile.ZipFile) -> None:
    total = 0

    for info in zf.infolist():
        if info.file_size > MAX_SINGLE_FILE_UNCOMPRESSED_BYTES:
            raise ValueError(f"EPUB member too large: {info.filename}")

        total += info.file_size

        if total > MAX_SOURCE_UNCOMPRESSED_BYTES:
            raise ValueError("EPUB uncompressed content is too large")


def validate_epub_basics(zf: zipfile.ZipFile) -> None:
    names = set(zf.namelist())

    if "mimetype" not in names:
        raise ValueError("Not a valid EPUB: missing mimetype")

    try:
        mimetype_data = zf.read("mimetype")
    except KeyError:
        raise ValueError("Not a valid EPUB: missing mimetype")

    if mimetype_data.strip() != b"application/epub+zip":
        raise ValueError("Not a valid EPUB: bad mimetype")

    if "META-INF/container.xml" not in names:
        raise ValueError("Not a valid EPUB: missing container.xml")


def find_container_rootfile(zf: zipfile.ZipFile) -> str:
    data = safe_zip_read(zf, "META-INF/container.xml")
    root = parse_xml(data)
    rootfile = root.find(".//c:rootfile", CONTAINER_NS)

    if rootfile is None:
        raise ValueError("Missing rootfile in container.xml")

    path = rootfile.get("full-path")

    if not path:
        raise ValueError("container.xml rootfile missing full-path")

    return path


def parse_opf(
    zf: zipfile.ZipFile,
    opf_path: str,
) -> Tuple[str, Dict[str, dict], List[str], Set[str]]:
    data = safe_zip_read(zf, opf_path)
    root = parse_xml(data)
    opf_dir = posixpath.dirname(opf_path)

    manifest = {}
    spine = []
    structural_hrefs_to_skip: Set[str] = set()

    manifest_elem = None
    spine_elem = None
    guide_elem = None

    for child in root:
        lname = local_name(child.tag)

        if lname == "manifest":
            manifest_elem = child
        elif lname == "spine":
            spine_elem = child
        elif lname == "guide":
            guide_elem = child

    if manifest_elem is None or spine_elem is None:
        raise ValueError("OPF missing manifest or spine")

    for item in manifest_elem:
        if local_name(item.tag) != "item":
            continue

        item_id = item.get("id")
        href = item.get("href")
        media_type = item.get("media-type", "")
        props = item.get("properties", "")

        if not item_id or not href:
            continue

        full_path = resolve_href(opf_path, href)
        manifest[item_id] = {
            "href": full_path,
            "media_type": media_type,
            "properties": props,
        }

    for itemref in spine_elem:
        if local_name(itemref.tag) != "itemref":
            continue

        idref = itemref.get("idref")

        if idref:
            spine.append(idref)

    if guide_elem is not None:
        for ref in guide_elem:
            if local_name(ref.tag) != "reference":
                continue

            ref_type = (ref.get("type") or "").strip().lower()
            href = ref.get("href")

            if ref_type not in GUIDE_SKIP_TYPES or not href:
                continue

            full_path = posixpath.normpath(
                posixpath.join(opf_dir, href.split("#", 1)[0])
            )

            if not (
                full_path.startswith("../")
                or full_path == ".."
                or full_path.startswith("/")
            ):
                structural_hrefs_to_skip.add(full_path)

    return opf_dir, manifest, spine, structural_hrefs_to_skip


def find_first_by_local_name(root: ET.Element, name: str) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag) == name:
            return elem

    return None


def get_document_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{") and "}" in root.tag:
        return root.tag[1:].split("}", 1)[0]

    return XHTML_NS


def find_head(root: ET.Element) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag) == "head":
            return elem

    return None


def extract_title_from_xhtml(root: ET.Element) -> str:
    for name in ("h1", "h2", "title"):
        elem = find_first_by_local_name(root, name)
        text = get_text_content(elem)

        if text:
            return text[:120]

    return ""


def remove_stylesheet_links_and_add_main(root: ET.Element) -> None:
    ns = get_document_namespace(root)
    head = find_head(root)

    if head is None:
        head = ET.Element(f"{{{ns}}}head")
        root.insert(0, head)

    to_remove = []

    for child in list(head):
        if local_name(child.tag) != "link":
            continue

        rel = (child.get("rel") or "").lower()

        if "stylesheet" in rel:
            to_remove.append(child)

    for child in to_remove:
        head.remove(child)

    link = ET.Element(f"{{{ns}}}link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", "../styles/main.css")
    head.append(link)


def gather_and_rewrite_images(
    root: ET.Element,
    chapter_path: str,
    zf: zipfile.ZipFile,
    local_image_path_map: Dict[str, str],
    image_hash_to_name: Dict[str, str],
    used_image_names: Set[str],
) -> Dict[str, bytes]:
    collected: Dict[str, bytes] = {}

    for elem in root.iter():
        tag = local_name(elem.tag)
        attrs = []

        if tag == "img":
            attrs.append("src")
        elif tag == "image":
            attrs.extend([f"{{{XLINK_NS}}}href", "href"])

        for attr in attrs:
            val = elem.get(attr)

            if not val:
                continue

            lower_val = val.strip().lower()

            if (
                lower_val.startswith("data:")
                or lower_val.startswith("http://")
                or lower_val.startswith("https://")
                or lower_val.startswith("//")
            ):
                continue

            try:
                full = resolve_href(chapter_path, val)
            except ValueError:
                continue

            if full not in local_image_path_map:
                try:
                    image_bytes = safe_zip_read(zf, full)
                except KeyError:
                    continue

                digest = hashlib.sha256(image_bytes).hexdigest()

                if digest in image_hash_to_name:
                    new_name = image_hash_to_name[digest]
                else:
                    original_name = sanitize_internal_name(
                        posixpath.basename(full),
                        "image",
                    )
                    new_name = make_unique_name(original_name, used_image_names)
                    image_hash_to_name[digest] = new_name
                    collected[new_name] = image_bytes

                local_image_path_map[full] = new_name

            elem.set(attr, f"../images/{local_image_path_map[full]}")

    return collected


def remove_all_images_from_xhtml(root: ET.Element) -> None:
    def build_parent_map(root_elem: ET.Element) -> Dict[ET.Element, ET.Element]:
        return {child: parent for parent in root_elem.iter() for child in parent}

    def has_meaningful_text(elem: ET.Element) -> bool:
        if (elem.text or "").strip():
            return True

        for child in elem:
            if (child.tail or "").strip():
                return True

        return False

    def svg_has_non_image_content(elem: ET.Element) -> bool:
        if has_meaningful_text(elem):
            return True

        for child in elem:
            child_name = local_name(child.tag)

            if child_name == "image":
                continue

            if child_name in {"title", "desc"}:
                if "".join(child.itertext()).strip():
                    return True

                continue

            return True

        return False

    parent_map = build_parent_map(root)

    for elem in list(root.iter()):
        if local_name(elem.tag) != "img":
            continue

        parent = parent_map.get(elem)

        if parent is not None:
            parent.remove(elem)

    parent_map = build_parent_map(root)

    for elem in list(root.iter()):
        if local_name(elem.tag) != "image":
            continue

        parent = parent_map.get(elem)

        if parent is not None:
            parent.remove(elem)

    changed = True

    while changed:
        changed = False
        parent_map = build_parent_map(root)

        for elem in list(root.iter()):
            if local_name(elem.tag) != "svg":
                continue

            if svg_has_non_image_content(elem):
                continue

            parent = parent_map.get(elem)

            if parent is not None:
                parent.remove(elem)
                changed = True


def disable_view_items(items: List[discord.ui.Item]) -> None:
    for item in items:
        if hasattr(item, "disabled"):
            item.disabled = True

        children = getattr(item, "children", None)

        if children:
            disable_view_items(list(children))


def strip_dangerous_elements(root: ET.Element) -> None:
    dangerous = {
        "script",
        "iframe",
        "object",
        "embed",
    }

    for parent in root.iter():
        for child in list(parent):
            if local_name(child.tag) in dangerous:
                parent.remove(child)


def strip_dangerous_attributes(root: ET.Element) -> None:
    for elem in root.iter():
        for attr in list(elem.attrib):
            attr_lname = local_name(attr).lower()
            value = (elem.get(attr) or "").strip().lower()

            if attr_lname.startswith("on"):
                elem.attrib.pop(attr, None)
                continue

            if attr_lname in {"href", "src"} and value.startswith("javascript:"):
                elem.attrib.pop(attr, None)
                continue


def get_meaningful_text_length(root: ET.Element) -> int:
    parts = []

    for elem in root.iter():
        if local_name(elem.tag) in TEXT_TAGS:
            text = " ".join("".join(elem.itertext()).split())

            if text:
                parts.append(text)

    return len(" ".join(parts))


def count_inline_images(root: ET.Element) -> int:
    count = 0

    for elem in root.iter():
        if local_name(elem.tag) in IMAGE_TAGS:
            count += 1

    return count


def count_links(root: ET.Element) -> int:
    count = 0

    for elem in root.iter():
        if local_name(elem.tag) == "a" and elem.get("href"):
            count += 1

    return count


def has_epub_type(root: ET.Element, wanted: Set[str]) -> bool:
    epub_type_attr = f"{{{EPUB_NS}}}type"

    for elem in root.iter():
        raw = elem.get(epub_type_attr) or elem.get("epub:type") or ""
        values = {part.strip().lower() for part in raw.split()}

        if values & wanted:
            return True

    return False


def has_nav_toc_element(root: ET.Element) -> bool:
    for elem in root.iter():
        if local_name(elem.tag) != "nav":
            continue

        epub_type = (
            elem.get(f"{{{EPUB_NS}}}type") or elem.get("epub:type") or ""
        ).lower()
        role = (elem.get("role") or "").lower()

        if "toc" in epub_type or role in {"doc-toc", "navigation"}:
            return True

    return False


def is_probably_toc_page(root: ET.Element) -> bool:
    text_len = get_meaningful_text_length(root)
    link_count = count_links(root)

    if link_count < 12:
        return False

    if text_len >= 1500:
        return False

    link_density = link_count / max(text_len / 500, 1)

    return link_density >= 6


def is_probably_cover_or_title_page(root: ET.Element, href: str) -> bool:
    text_len = get_meaningful_text_length(root)
    image_count = count_inline_images(root)
    name = posixpath.basename(href.lower())

    structural_name_parts = (
        "cover",
        "titlepage",
        "title-page",
    )

    if any(part in name for part in structural_name_parts) and text_len < 500:
        return True

    if has_epub_type(root, {"cover", "titlepage", "title-page"}):
        return True

    if image_count > 0 and text_len < 40:
        return True

    return False


def is_probably_non_chapter_page(root: ET.Element, href: str) -> bool:
    text_len = get_meaningful_text_length(root)
    name = posixpath.basename(href.lower())

    if text_len == 0 and count_inline_images(root) == 0:
        return True

    if has_nav_toc_element(root):
        return True

    if has_epub_type(root, {"toc", "nav", "landmarks", "page-list"}):
        return True

    if is_probably_toc_page(root):
        return True

    if is_probably_cover_or_title_page(root, href):
        return True

    if any(part in name for part in ("toc", "nav")) and text_len < 800:
        return True

    return False


def chapter_media_type(media_type: str) -> bool:
    return media_type in {
        "application/xhtml+xml",
        "text/html",
        "application/xml",
    }


def build_simple_stylesheet() -> bytes:
    css = """
body {
  font-family: serif;
  line-height: 1.45;
  margin: 5%;
}
h1, h2, h3, h4, h5, h6 {
  margin-top: 1.4em;
  margin-bottom: 0.6em;
}
p {
  margin: 0 0 0.9em 0;
}
img, svg {
  max-width: 100%;
}
"""
    return css.strip().encode("utf-8")


def make_nav_xhtml(chapters: List[Tuple[str, str]]) -> bytes:
    ET.register_namespace("", XHTML_NS)
    ET.register_namespace("epub", EPUB_NS)

    html = ET.Element(f"{{{XHTML_NS}}}html")
    html.set(f"{{{XML_NS}}}lang", "en")
    html.set("lang", "en")

    head = ET.SubElement(html, f"{{{XHTML_NS}}}head")
    title = ET.SubElement(head, f"{{{XHTML_NS}}}title")
    title.text = "Table of Contents"

    body = ET.SubElement(html, f"{{{XHTML_NS}}}body")
    nav = ET.SubElement(body, f"{{{XHTML_NS}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", "toc")
    nav.set("id", "toc")

    h1 = ET.SubElement(nav, f"{{{XHTML_NS}}}h1")
    h1.text = "Table of Contents"

    ol = ET.SubElement(nav, f"{{{XHTML_NS}}}ol")

    for href, label in chapters:
        li = ET.SubElement(ol, f"{{{XHTML_NS}}}li")
        a = ET.SubElement(li, f"{{{XHTML_NS}}}a")
        a.set("href", href)
        a.text = label

    ET.indent(html, space="  ")

    return ET.tostring(
        html,
        encoding="utf-8",
        xml_declaration=True,
        method="xml",
    )


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def make_toc_ncx(chapters: List[Tuple[str, str]], uid: str, title_text: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
        "  <head>",
        f'    <meta name="dtb:uid" content="{_xml_escape(uid)}" />',
        "  </head>",
        "  <docTitle>",
        f"    <text>{_xml_escape(title_text)}</text>",
        "  </docTitle>",
        "  <navMap>",
    ]

    for i, (href, label) in enumerate(chapters, start=1):
        lines.extend(
            [
                f'    <navPoint id="navPoint-{i}" playOrder="{i}">',
                "      <navLabel>",
                f"        <text>{_xml_escape(label)}</text>",
                "      </navLabel>",
                f'      <content src="{_xml_escape(href)}" />',
                "    </navPoint>",
            ]
        )

    lines.extend(
        [
            "  </navMap>",
            "</ncx>",
            "",
        ]
    )

    return "\n".join(lines).encode("utf-8")


def guess_media_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    known = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
    }

    if ext in known:
        return known[ext]

    guessed, _ = mimetypes.guess_type(filename)

    return guessed or "application/octet-stream"


def make_content_opf(
    uid: str,
    title_text: str,
    author: str,
    chapters: List[Tuple[str, str]],
    image_names: List[str],
) -> bytes:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'version="3.0" unique-identifier="bookid">',
        "  <metadata>",
        f'    <dc:identifier id="bookid">{_xml_escape(uid)}</dc:identifier>',
        f"    <dc:title>{_xml_escape(title_text)}</dc:title>",
        f"    <dc:creator>{_xml_escape(author)}</dc:creator>",
        "    <dc:language>en</dc:language>",
        f'    <meta property="dcterms:modified">{modified}</meta>',
        "  </metadata>",
        "  <manifest>",
        '    <item id="nav" href="nav.xhtml" '
        'media-type="application/xhtml+xml" properties="nav" />',
        '    <item id="ncx" href="toc.ncx" '
        'media-type="application/x-dtbncx+xml" />',
        '    <item id="style" href="styles/main.css" media-type="text/css" />',
    ]

    for i, (href, _) in enumerate(chapters, start=1):
        lines.append(
            f'    <item id="chap{i}" href="{_xml_escape(href)}" '
            'media-type="application/xhtml+xml" />'
        )

    for i, image_name in enumerate(image_names, start=1):
        media_type = guess_media_type(image_name)
        lines.append(
            f'    <item id="img{i}" href="images/{_xml_escape(image_name)}" '
            f'media-type="{media_type}" />'
        )

    lines.append("  </manifest>")
    lines.append('  <spine toc="ncx">')

    for i in range(1, len(chapters) + 1):
        lines.append(f'    <itemref idref="chap{i}" />')

    lines.extend(
        [
            "  </spine>",
            "</package>",
            "",
        ]
    )

    return "\n".join(lines).encode("utf-8")


def make_container_xml() -> bytes:
    data = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0"
  xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    return data.encode("utf-8")


def extract_book_content(
    epub_bytes: bytes,
    used_chapter_names: Set[str],
    used_image_names: Set[str],
    image_hash_to_name: Dict[str, str],
    remove_all_images: bool = False,
) -> Tuple[List[Tuple[str, str, bytes]], Dict[str, bytes]]:
    chapters_out: List[Tuple[str, str, bytes]] = []
    images_out: Dict[str, bytes] = {}
    local_image_path_map: Dict[str, str] = {}

    with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
        validate_zip_member_names(zf)
        validate_zip_sizes(zf)
        validate_epub_basics(zf)

        opf_path = find_container_rootfile(zf)
        _, manifest, spine, structural_hrefs_to_skip = parse_opf(zf, opf_path)

        chapter_index = 1

        for idref in spine:
            item = manifest.get(idref)

            if not item:
                continue

            href = item["href"]
            media_type = item["media_type"]
            props = item["properties"]

            if not chapter_media_type(media_type):
                continue

            if has_manifest_property(props, "nav"):
                continue

            if has_manifest_property(props, "cover-image"):
                continue

            if href in structural_hrefs_to_skip:
                continue

            if looks_like_structural_page_by_name(href, props):
                continue

            try:
                raw = safe_zip_read(zf, href)
            except KeyError:
                continue

            try:
                root = parse_xml(raw)
            except XML_PARSE_ERRORS:
                continue

            if local_name(root.tag) != "html":
                continue

            strip_dangerous_elements(root)
            strip_dangerous_attributes(root)

            if is_probably_non_chapter_page(root, href):
                continue

            remove_stylesheet_links_and_add_main(root)

            if remove_all_images:
                remove_all_images_from_xhtml(root)
            else:
                found_images = gather_and_rewrite_images(
                    root=root,
                    chapter_path=href,
                    zf=zf,
                    local_image_path_map=local_image_path_map,
                    image_hash_to_name=image_hash_to_name,
                    used_image_names=used_image_names,
                )
                images_out.update(found_images)

            chapter_title = extract_title_from_xhtml(root)

            if not chapter_title:
                chapter_title = f"Chapter {len(chapters_out) + 1}"

            original_name = sanitize_internal_name(
                posixpath.basename(href),
                f"chapter_{chapter_index}",
            )

            if not original_name.lower().endswith((".xhtml", ".html", ".htm")):
                original_name += ".xhtml"

            if original_name.lower().endswith((".html", ".htm")):
                original_name = re.sub(r"\.html?$", ".xhtml", original_name)

            unique_name = make_unique_name(original_name, used_chapter_names)
            chapter_zip_path = safe_output_zip_path("text", unique_name)

            ET.register_namespace("", XHTML_NS)
            ET.register_namespace("epub", EPUB_NS)
            ET.register_namespace("svg", SVG_NS)
            ET.register_namespace("xlink", XLINK_NS)

            chapter_bytes = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
                method="xml",
            )

            chapters_out.append((chapter_zip_path, chapter_title, chapter_bytes))
            chapter_index += 1

    return chapters_out, images_out


def build_compiled_epub_bytes(
    title: str,
    author: str,
    final_chapters: List[Tuple[str, str, bytes]],
    final_images: Dict[str, bytes],
    remove_all_images: bool,
) -> bytes:
    chapter_toc = [(href, label) for href, label, _ in final_chapters]
    uid = f"urn:uuid:{uuid.uuid4()}"

    with io.BytesIO() as out:
        with zipfile.ZipFile(out, "w") as zf:
            mimetype_info = zipfile.ZipInfo("mimetype")
            mimetype_info.compress_type = zipfile.ZIP_STORED
            zf.writestr(mimetype_info, b"application/epub+zip")

            zf.writestr(
                "META-INF/container.xml",
                make_container_xml(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/styles/main.css",
                build_simple_stylesheet(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/nav.xhtml",
                make_nav_xhtml(chapter_toc),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/toc.ncx",
                make_toc_ncx(chapter_toc, uid, title),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(
                "OEBPS/content.opf",
                make_content_opf(
                    uid=uid,
                    title_text=title,
                    author=author,
                    chapters=chapter_toc,
                    image_names=[] if remove_all_images else sorted(final_images.keys()),
                ),
                compress_type=zipfile.ZIP_DEFLATED,
            )

            for href, _, chapter_bytes in final_chapters:
                zf.writestr(
                    f"OEBPS/{href}",
                    chapter_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )

            if not remove_all_images:
                for image_name, image_bytes in final_images.items():
                    zf.writestr(
                        f"OEBPS/{safe_output_zip_path('images', image_name)}",
                        image_bytes,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )

        return out.getvalue()


async def compile_selected_epubs(
    selected: List[EpubEntry],
    title: str,
    author: str,
    remove_all_images: bool = False,
    max_output_bytes: int = MAX_OUTPUT_EPUB_BYTES,
) -> Tuple[Optional[bytes], List[Tuple[str, str]]]:
    used_chapter_names: Set[str] = set()
    used_image_names: Set[str] = set()
    image_hash_to_name: Dict[str, str] = {}

    final_chapters: List[Tuple[str, str, bytes]] = []
    final_images: Dict[str, bytes] = {}
    skipped: List[Tuple[str, str]] = []

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for entry_index, entry in enumerate(selected, start=1):
            try:
                epub_bytes = await fetch_epub_bytes(http, entry)
                chapters, images = await asyncio.to_thread(
                    extract_book_content,
                    epub_bytes=epub_bytes,
                    used_chapter_names=used_chapter_names,
                    used_image_names=used_image_names,
                    image_hash_to_name=image_hash_to_name,
                    remove_all_images=remove_all_images,
                )

                if not chapters:
                    skipped.append((entry.filename, "No usable chapter files found"))
                    log(f"Skipped {entry.filename}: No usable chapter files found")
                    continue

                final_chapters.extend(chapters)

                if not remove_all_images:
                    final_images.update(images)

                    image_payload_size = sum(
                        len(data) for data in final_images.values()
                    )

                    if image_payload_size > int(
                        max_output_bytes * IMAGE_SIZE_ABORT_RATIO
                    ):
                        raise OutputTooLargeError(
                            "Compilation aborted before downloading remaining EPUBs.\n"
                            "Images alone are near or above the Discord upload limit.\n"
                            f"Image payload: {format_bytes(image_payload_size)}\n"
                            f"Limit: {format_bytes(max_output_bytes)}\n"
                            "Try again with `Remove all images` enabled."
                        )

                estimated_size = estimate_compiled_epub_bytes(
                    final_chapters=final_chapters,
                    final_images=final_images,
                    remove_all_images=remove_all_images,
                )

                if estimated_size > max_output_bytes:
                    raise OutputTooLargeError(
                        "Compilation aborted before downloading remaining EPUBs.\n"
                        f"Estimated output size after EPUB "
                        f"{entry_index}/{len(selected)}: {entry.filename}\n"
                        f"Estimated size: {format_bytes(estimated_size)}\n"
                        f"Limit: {format_bytes(max_output_bytes)}\n"
                        "Try selecting fewer EPUBs or enable `Remove all images`."
                    )

            except OutputTooLargeError:
                raise
            except zipfile.BadZipFile:
                skipped.append((entry.filename, "Invalid EPUB/ZIP"))
                log(f"Skipped {entry.filename}: Invalid EPUB/ZIP")
            except KeyError as exc:
                skipped.append((entry.filename, f"Missing file: {exc}"))
                log(f"Skipped {entry.filename}: Missing file: {exc}")
            except Exception as exc:
                skipped.append((entry.filename, str(exc)[:200]))
                log(f"Skipped {entry.filename}: {exc}")
                traceback.print_exc()

    if not final_chapters:
        return None, skipped

    output = await asyncio.to_thread(
        build_compiled_epub_bytes,
        title=title,
        author=author,
        final_chapters=final_chapters,
        final_images=final_images,
        remove_all_images=remove_all_images,
    )

    if len(output) > max_output_bytes:
        raise OutputTooLargeError(
            "The compiled EPUB is too large to send through Discord.\n"
            f"Compiled size: {format_bytes(len(output))}\n"
            f"Limit: {format_bytes(max_output_bytes)}\n"
            "Try selecting fewer EPUBs or enable `Remove all images`."
        )

    return output, skipped


@bot.tree.command(name="compile", description="Select EPUBs from this channel")
async def compile_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "Use this command in a server channel.",
            ephemeral=True,
        )
        return

    channel_name = getattr(interaction.channel, "name", "Discord Channel")
    log(f"/compile run by {interaction.user} in #{channel_name}")

    key = build_session_key(interaction.user.id, interaction.channel.id)
    old_session = SESSIONS.pop(key, None)

    if old_session is not None:
        old_session.expired = True

    session = CompileSession(
        user_id=interaction.user.id,
        channel_id=interaction.channel.id,
        channel_name=channel_name,
    )
    SESSIONS[key] = session

    await interaction.response.defer(ephemeral=True, thinking=True)

    ok, error = await scan_for_epubs(
        channel=interaction.channel,
        session=session,
        target_new_count=INITIAL_LOAD_ATTACHMENTS,
    )

    if not ok:
        log(f"Couldn't scan #{channel_name}")
        session.expired = True
        SESSIONS.pop(key, None)
        await interaction.followup.send(
            error or "Failed to scan channel.",
            ephemeral=True,
        )
        return

    async with session.lock:
        found_count = len(session.entries)

    if not found_count:
        log(f"No EPUBs found in #{channel_name}")
        session.expired = True
        SESSIONS.pop(key, None)
        await interaction.followup.send(
            "No EPUB attachments found in this channel.",
            ephemeral=True,
        )
        return

    log(f"Found {found_count} EPUB(s) in #{channel_name}")

    view = CompileLayoutView(session)
    msg = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
    )
    view.message = msg


@bot.event
async def on_ready() -> None:
    log(f"Logged in as {bot.user}")

    if not getattr(bot, "_cleanup_started", False):
        bot._cleanup_started = True
        asyncio.create_task(cleanup_sessions())
        log("Cleanup task started")

    if not getattr(bot, "_synced", False):
        bot._synced = True

        try:
            synced = await bot.tree.sync()
            log(f"Synced {len(synced)} command(s)")
        except Exception as exc:
            log(f"Command sync failed: {exc}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN in your environment.")

    bot.run(TOKEN)
