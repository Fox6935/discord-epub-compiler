import asyncio
import hashlib
import io
import os
import posixpath
import re
import sqlite3
import zlib
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import DB_PATH, GUILD_ID
from epub_tools import (
    find_container_rootfile, get_text_content, guess_media_type, local_name, parse_opf,
    parse_xml, safe_zip_read, validate_epub_basics, validate_zip_member_names,
    validate_zip_sizes,
)
from models import EpubEntry


def unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def dt_to_unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def unix_to_dt(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def normalize_key(raw: str) -> str:
    raw = re.sub(r"\b(ch|chapter|chapters)\s*\d+([\s._-]*(to|-)\s*\d+)?\b", "", raw, flags=re.I)
    raw = re.sub(r"\bv\d+\b", "", raw, flags=re.I)
    raw = re.sub(r"\.epub$", "", raw, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip() or "unknown"


def validate_internal_zip_path(path: str) -> str:
    clean = path.replace("\\", "/")

    if "\x00" in clean or clean.startswith("/"):
        raise ValueError(f"Unsafe EPUB path: {path}")

    parts = clean.split("/")
    if any(part == ".." for part in parts):
        raise ValueError(f"Unsafe EPUB path traversal: {path}")

    return clean


TEXT_LIKE_EXTS = {".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx", ".css", ".svg"}
TEXT_LIKE_MEDIA = {
    "application/xhtml+xml",
    "text/html",
    "application/xml",
    "text/xml",
    "application/oebps-package+xml",
    "application/x-dtbncx+xml",
    "text/css",
    "image/svg+xml",
}
COMPRESSED_MEDIA = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


def is_text_like_component(path: str, media_type: str) -> bool:
    ext = posixpath.splitext(path.lower())[1]
    return ext in TEXT_LIKE_EXTS or media_type.lower() in TEXT_LIKE_MEDIA


def store_blob_payload(path: str, media_type: str, data: bytes) -> Tuple[str, bytes]:
    if media_type.lower() in COMPRESSED_MEDIA:
        return "none", data

    if not is_text_like_component(path, media_type):
        return "none", data

    compressed = zlib.compress(data, level=9)
    if len(compressed) < len(data):
        return "deflate", compressed

    return "none", data


def load_blob_payload(compression: str, data: bytes) -> bytes:
    if compression == "none":
        return data
    if compression == "deflate":
        return zlib.decompress(data)
    raise ValueError(f"Unsupported blob compression: {compression}")


def parse_opf_metadata_from_zip(
    zf: zipfile.ZipFile,
) -> Tuple[str, str, Dict[str, str], Dict[str, int], Set[str]]:
    manifest_types: Dict[str, str] = {}
    spine_orders: Dict[str, int] = {}
    cover_image_paths: Set[str] = set()
    title = ""
    creator = ""

    try:
        opf_path = find_container_rootfile(zf)
        opf_dir, manifest, spine, _ = parse_opf(zf, opf_path)
        root = parse_xml(safe_zip_read(zf, opf_path))
    except Exception:
        return title, creator, manifest_types, spine_orders, cover_image_paths

    cover_item_ids: Set[str] = set()

    for elem in root.iter():
        lname = local_name(elem.tag).lower()
        if lname == "title" and not title:
            title = get_text_content(elem)
        elif lname in {"creator", "author"} and not creator:
            creator = get_text_content(elem)
        elif lname == "meta" and (elem.get("name") or "").lower() == "cover":
            content = elem.get("content")

            if content:
                cover_item_ids.add(content)

    for item_id, item in manifest.items():
        manifest_types[item["href"]] = item.get("media_type") or guess_media_type(item["href"])

        if (
            "image/" in manifest_types[item["href"]]
            and (
                "cover-image" in {p.strip().lower() for p in item.get("properties", "").split()}
                or item_id in cover_item_ids
            )
        ):
            cover_image_paths.add(item["href"])

    for index, item_id in enumerate(spine, start=1):
        item = manifest.get(item_id)
        if item:
            spine_orders[item["href"]] = index

    return title, creator, manifest_types, spine_orders, cover_image_paths


def compute_epub_fingerprint(components: List[Tuple[str, bytes]]) -> bytes:
    outer = hashlib.sha256()

    for internal_path, blob_hash in sorted(components, key=lambda item: item[0]):
        outer.update(internal_path.encode("utf-8"))
        outer.update(b"\x00")
        outer.update(blob_hash)

    return outer.digest()


class ArchiveDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def run(self, func, *args):
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, func, *args)

    def _run_sync(self, func, *args):
        with self.connect() as conn:
            return func(conn, *args)

    async def bootstrap(self) -> None:
        new_db = not os.path.exists(self.path)
        await self.run(self._bootstrap_sync, new_db)

    def _bootstrap_sync(self, conn: sqlite3.Connection, new_db: bool) -> None:
        if new_db:
            conn.execute("PRAGMA page_size=32768")
            conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS book (
              id INTEGER PRIMARY KEY,
              canonical_key TEXT,
              first_seen_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS epub_version (
              id INTEGER PRIMARY KEY,
              book_id INTEGER,
              original_filename TEXT NOT NULL,
              imported_at INTEGER NOT NULL,
              source_size INTEGER,
              epub_fingerprint BLOB,
              component_count INTEGER,
              FOREIGN KEY(book_id) REFERENCES book(id)
            );
            CREATE TABLE IF NOT EXISTS blob (
              hash BLOB PRIMARY KEY,
              media_type TEXT,
              size_uncompressed INTEGER NOT NULL,
              size_stored INTEGER NOT NULL,
              compression TEXT NOT NULL,
              data BLOB NOT NULL,
              refcount INTEGER NOT NULL DEFAULT 0,
              first_seen_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS epub_component (
              epub_version_id INTEGER NOT NULL,
              internal_path TEXT NOT NULL,
              media_type TEXT,
              blob_hash BLOB NOT NULL,
              size_uncompressed INTEGER NOT NULL,
              spine_order INTEGER,
              is_manifest_item INTEGER NOT NULL DEFAULT 1,
              is_cover_image INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(epub_version_id, internal_path),
              FOREIGN KEY(epub_version_id) REFERENCES epub_version(id),
              FOREIGN KEY(blob_hash) REFERENCES blob(hash)
            );
            CREATE TABLE IF NOT EXISTS discord_epub (
              id INTEGER PRIMARY KEY,
              guild_id INTEGER NOT NULL,
              channel_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              attachment_index INTEGER NOT NULL,
              discord_filename TEXT NOT NULL,
              attachment_size INTEGER,
              message_created_at INTEGER NOT NULL,
              epub_version_id INTEGER NOT NULL,
              effective_order INTEGER NOT NULL,
              is_deleted INTEGER NOT NULL DEFAULT 0,
              deleted_at INTEGER,
              deleted_by_user_id INTEGER,
              delete_reason TEXT,
              created_at INTEGER NOT NULL,
              FOREIGN KEY(epub_version_id) REFERENCES epub_version(id)
            );
            CREATE TABLE IF NOT EXISTS watched_channel (
              channel_id INTEGER PRIMARY KEY,
              guild_id INTEGER NOT NULL,
              category_id INTEGER,
              channel_name TEXT NOT NULL,
              watch_enabled INTEGER NOT NULL,
              historical_scan_complete INTEGER NOT NULL,
              scan_anchor_message_id INTEGER,
              historical_before_message_id INTEGER,
              last_processed_message_id INTEGER,
              last_scan_started_at INTEGER,
              last_scan_finished_at INTEGER,
              last_catchup_completed_at INTEGER,
              last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS watched_category (
              category_id INTEGER PRIMARY KEY,
              guild_id INTEGER NOT NULL,
              category_name TEXT NOT NULL,
              watch_enabled INTEGER NOT NULL,
              last_reconciled_at INTEGER,
              last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS scan_job (
              id INTEGER PRIMARY KEY,
              scope_type TEXT NOT NULL,
              guild_id INTEGER NOT NULL,
              category_id INTEGER,
              channel_id INTEGER,
              requested_by_user_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              queued_at INTEGER NOT NULL,
              started_at INTEGER,
              finished_at INTEGER,
              error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS import_failure (
              channel_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              attachment_index INTEGER NOT NULL,
              guild_id INTEGER NOT NULL,
              filename TEXT,
              error_text TEXT NOT NULL,
              first_failed_at INTEGER NOT NULL,
              last_failed_at INTEGER NOT NULL,
              attempt_count INTEGER NOT NULL,
              PRIMARY KEY(channel_id, message_id, attachment_index)
            );
            CREATE INDEX IF NOT EXISTS epub_version_filename_idx ON epub_version(original_filename);
            CREATE INDEX IF NOT EXISTS epub_component_blob_idx ON epub_component(blob_hash);
            CREATE INDEX IF NOT EXISTS epub_component_version_idx ON epub_component(epub_version_id);
            CREATE UNIQUE INDEX IF NOT EXISTS discord_epub_unique_idx ON discord_epub(channel_id, message_id, attachment_index);
            CREATE INDEX IF NOT EXISTS discord_epub_channel_effective_order_idx ON discord_epub(channel_id, is_deleted, effective_order);
            CREATE INDEX IF NOT EXISTS watched_channel_watch_idx ON watched_channel(watch_enabled, historical_scan_complete);
            CREATE INDEX IF NOT EXISTS watched_category_watch_idx ON watched_category(watch_enabled);
            CREATE INDEX IF NOT EXISTS import_failure_channel_idx ON import_failure(channel_id, message_id);
            """
        )
        added_cover_column = self._ensure_column_sync(
            conn,
            "epub_component",
            "is_cover_image",
            "INTEGER NOT NULL DEFAULT 0",
        )

        if added_cover_column:
            self._backfill_cover_image_flags_sync(conn)

    def _ensure_column_sync(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> bool:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

        if column in columns:
            return False

        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True

    def _backfill_cover_image_flags_sync(self, conn: sqlite3.Connection) -> None:
        version_rows = conn.execute("SELECT id FROM epub_version").fetchall()

        for row in version_rows:
            try:
                epub_bytes = self._reconstruct_epub_sync(conn, row["id"])

                with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
                    _, _, _, _, cover_image_paths = parse_opf_metadata_from_zip(zf)
            except Exception:
                continue

            if not cover_image_paths:
                continue

            placeholders = ",".join("?" for _ in cover_image_paths)
            conn.execute(
                f"""
                UPDATE epub_component
                SET is_cover_image = 1
                WHERE epub_version_id = ? AND internal_path IN ({placeholders})
                """,
                [row["id"], *cover_image_paths],
            )

    async def list_channel_epubs(
        self,
        channel_id: int,
        include_deleted: bool = False,
    ) -> List[EpubEntry]:
        return await self.run(
            self._list_channel_epubs_sync,
            channel_id,
            include_deleted,
        )

    async def is_discord_epub_imported(
        self,
        channel_id: int,
        message_id: int,
        attachment_index: int,
    ) -> bool:
        return await self.run(
            self._is_discord_epub_imported_sync,
            channel_id,
            message_id,
            attachment_index,
        )

    def _is_discord_epub_imported_sync(
        self,
        conn: sqlite3.Connection,
        channel_id: int,
        message_id: int,
        attachment_index: int,
    ) -> bool:
        return (
            conn.execute(
                """
                SELECT 1 FROM discord_epub
                WHERE channel_id = ? AND message_id = ? AND attachment_index = ?
                """,
                (channel_id, message_id, attachment_index),
            ).fetchone()
            is not None
        )

    def _list_channel_epubs_sync(
        self,
        conn: sqlite3.Connection,
        channel_id: int,
        include_deleted: bool,
    ) -> List[EpubEntry]:
        deleted_filter = "" if include_deleted else "AND is_deleted = 0"
        rows = conn.execute(
            f"""
            SELECT
              d.id,
              d.channel_id,
              d.message_id,
              d.attachment_index,
              d.discord_filename,
              d.attachment_size,
              d.message_created_at,
              d.epub_version_id,
              d.effective_order,
              d.is_deleted,
              COALESCE(
                SUM(
                  CASE
                    WHEN c.spine_order IS NOT NULL
                     AND c.media_type IN ('application/xhtml+xml', 'text/html', 'application/xml')
                    THEN c.size_uncompressed
                    ELSE 0
                  END
                ),
                0
              ) AS estimated_chapter_bytes
            FROM discord_epub d
            LEFT JOIN epub_component c ON c.epub_version_id = d.epub_version_id
            WHERE d.guild_id = ? AND d.channel_id = ? {deleted_filter}
            GROUP BY d.id
            ORDER BY d.effective_order DESC
            """,
            (GUILD_ID, channel_id),
        ).fetchall()

        image_blob_sizes_by_version: Dict[int, List[Tuple[str, int]]] = {}
        version_ids = [row["epub_version_id"] for row in rows]

        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            image_rows = conn.execute(
                f"""
                SELECT epub_version_id, hex(blob_hash) AS blob_hash, MAX(size_uncompressed) AS size_uncompressed
                FROM epub_component
                WHERE epub_version_id IN ({placeholders})
                  AND media_type LIKE 'image/%'
                  AND media_type != 'image/svg+xml'
                  AND is_cover_image = 0
                GROUP BY epub_version_id, blob_hash
                """,
                version_ids,
            ).fetchall()

            for image_row in image_rows:
                image_blob_sizes_by_version.setdefault(
                    image_row["epub_version_id"],
                    [],
                ).append(
                    (
                        image_row["blob_hash"],
                        image_row["size_uncompressed"],
                    )
                )

        return [
            EpubEntry(
                entry_id=str(row["id"]),
                discord_epub_id=row["id"],
                epub_version_id=row["epub_version_id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                attachment_index=row["attachment_index"],
                filename=row["discord_filename"],
                attachment_size=row["attachment_size"],
                created_at=unix_to_dt(row["message_created_at"]),
                effective_order=row["effective_order"],
                is_deleted=bool(row["is_deleted"]),
                estimated_chapter_bytes=row["estimated_chapter_bytes"],
                estimated_image_bytes=sum(
                    size
                    for _, size in image_blob_sizes_by_version.get(
                        row["epub_version_id"],
                        [],
                    )
                ),
                image_blob_sizes=tuple(
                    image_blob_sizes_by_version.get(row["epub_version_id"], [])
                ),
            )
            for row in rows
        ]

    async def reconstruct_epub(self, epub_version_id: int) -> bytes:
        return await self.run(self._reconstruct_epub_sync, epub_version_id)

    def _reconstruct_epub_sync(self, conn: sqlite3.Connection, epub_version_id: int) -> bytes:
        rows = conn.execute(
            """
            SELECT c.internal_path, b.compression, b.data
            FROM epub_component c
            JOIN blob b ON b.hash = c.blob_hash
            WHERE c.epub_version_id = ?
            ORDER BY CASE WHEN c.internal_path = 'mimetype' THEN 0 ELSE 1 END, c.internal_path
            """,
            (epub_version_id,),
        ).fetchall()

        if not rows:
            raise ValueError("Archived EPUB has no components")

        with io.BytesIO() as out:
            with zipfile.ZipFile(out, "w") as zf:
                for row in rows:
                    data = load_blob_payload(row["compression"], row["data"])
                    if row["internal_path"] == "mimetype":
                        info = zipfile.ZipInfo("mimetype")
                        info.compress_type = zipfile.ZIP_STORED
                        zf.writestr(info, data)
                    else:
                        zf.writestr(row["internal_path"], data, compress_type=zipfile.ZIP_DEFLATED)
            return out.getvalue()

    async def import_epub_bytes(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        attachment_index: int,
        filename: str,
        attachment_size: Optional[int],
        message_created_at: datetime,
        epub_bytes: bytes,
    ) -> bool:
        return await self.run(
            self._import_epub_bytes_sync,
            guild_id,
            channel_id,
            message_id,
            attachment_index,
            filename,
            attachment_size,
            message_created_at,
            epub_bytes,
        )

    def _import_epub_bytes_sync(
        self,
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
        attachment_index: int,
        filename: str,
        attachment_size: Optional[int],
        message_created_at: datetime,
        epub_bytes: bytes,
    ) -> bool:
        if conn.execute(
            "SELECT 1 FROM discord_epub WHERE channel_id = ? AND message_id = ? AND attachment_index = ?",
            (channel_id, message_id, attachment_index),
        ).fetchone():
            return False

        now = unix_now()
        with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
            validate_zip_member_names(zf)
            validate_zip_sizes(zf)
            validate_epub_basics(zf)
            title, creator, manifest_types, spine_orders, cover_image_paths = parse_opf_metadata_from_zip(zf)
            canonical_key = normalize_key(f"{title} {creator}" if title else filename)
            book_row = conn.execute(
                "SELECT id FROM book WHERE canonical_key = ?",
                (canonical_key,),
            ).fetchone()
            if book_row:
                book_id = book_row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO book(canonical_key, first_seen_at) VALUES (?, ?)",
                    (canonical_key, now),
                )
                book_id = cur.lastrowid

            components: List[Tuple[str, str, bytes, str, bytes, int, Optional[int], int, int]] = []
            fingerprint_parts: List[Tuple[str, bytes]] = []

            for info in zf.infolist():
                if info.is_dir():
                    continue
                internal_path = validate_internal_zip_path(info.filename)
                data = safe_zip_read(zf, info.filename)
                blob_hash = hashlib.sha256(data).digest()
                media_type = manifest_types.get(internal_path) or guess_media_type(internal_path)
                compression, stored = store_blob_payload(internal_path, media_type, data)
                spine_order = spine_orders.get(internal_path)
                is_manifest_item = 1 if internal_path in manifest_types or internal_path == "mimetype" else 0
                is_cover_image = 1 if internal_path in cover_image_paths else 0
                components.append((internal_path, media_type, blob_hash, compression, stored, len(data), spine_order, is_manifest_item, is_cover_image))
                fingerprint_parts.append((internal_path, blob_hash))

            fingerprint = compute_epub_fingerprint(fingerprint_parts)
            cur = conn.execute(
                """
                INSERT INTO epub_version(book_id, original_filename, imported_at, source_size, epub_fingerprint, component_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (book_id, filename, now, attachment_size, fingerprint, len(components)),
            )
            epub_version_id = cur.lastrowid

            for internal_path, media_type, blob_hash, compression, stored, size_uncompressed, spine_order, is_manifest_item, is_cover_image in components:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO blob(hash, media_type, size_uncompressed, size_stored, compression, data, first_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (blob_hash, media_type, size_uncompressed, len(stored), compression, stored, now),
                )
                conn.execute("UPDATE blob SET refcount = refcount + 1 WHERE hash = ?", (blob_hash,))
                conn.execute(
                    """
                    INSERT INTO epub_component(epub_version_id, internal_path, media_type, blob_hash, size_uncompressed, spine_order, is_manifest_item, is_cover_image)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (epub_version_id, internal_path, media_type, blob_hash, size_uncompressed, spine_order, is_manifest_item, is_cover_image),
                )

            watch_row = conn.execute(
                "SELECT historical_scan_complete FROM watched_channel WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            historical_complete = bool(
                watch_row and watch_row["historical_scan_complete"]
            )

            if historical_complete:
                effective_order = (
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(effective_order), 0)
                        FROM discord_epub
                        WHERE channel_id = ? AND is_deleted = 0
                        """,
                        (channel_id,),
                    ).fetchone()[0]
                    + 1
                )
            else:
                effective_order = 0

            conn.execute(
                """
                INSERT INTO discord_epub(
                  guild_id, channel_id, message_id, attachment_index, discord_filename,
                  attachment_size, message_created_at, epub_version_id, effective_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    attachment_index,
                    filename,
                    attachment_size,
                    dt_to_unix(message_created_at),
                    epub_version_id,
                    effective_order,
                    now,
                ),
            )

            if not historical_complete:
                rows = conn.execute(
                    """
                    SELECT id FROM discord_epub
                    WHERE channel_id = ? AND is_deleted = 0
                    ORDER BY message_id ASC, attachment_index ASC
                    """,
                    (channel_id,),
                ).fetchall()
                for order_index, row in enumerate(rows, start=1):
                    conn.execute(
                        "UPDATE discord_epub SET effective_order = ? WHERE id = ?",
                        (order_index, row["id"]),
                    )

            conn.execute(
                "DELETE FROM import_failure WHERE channel_id = ? AND message_id = ? AND attachment_index = ?",
                (channel_id, message_id, attachment_index),
            )
            return True

    async def record_import_failure(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        attachment_index: int,
        filename: str,
        error_text: str,
    ) -> None:
        await self.run(
            self._record_import_failure_sync,
            guild_id,
            channel_id,
            message_id,
            attachment_index,
            filename,
            error_text[:1000],
        )

    def _record_import_failure_sync(
        self,
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
        attachment_index: int,
        filename: str,
        error_text: str,
    ) -> None:
        now = unix_now()
        conn.execute(
            """
            INSERT INTO import_failure(channel_id, message_id, attachment_index, guild_id, filename, error_text, first_failed_at, last_failed_at, attempt_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(channel_id, message_id, attachment_index) DO UPDATE SET
              error_text = excluded.error_text,
              last_failed_at = excluded.last_failed_at,
              attempt_count = import_failure.attempt_count + 1
            """,
            (
                channel_id,
                message_id,
                attachment_index,
                guild_id,
                filename,
                error_text,
                now,
                now,
            ),
        )


ARCHIVE = ArchiveDB(DB_PATH)


async def get_watched_channel_row(channel_id: int) -> Optional[sqlite3.Row]:
    return await ARCHIVE.run(
        lambda conn: conn.execute(
            "SELECT * FROM watched_channel WHERE guild_id = ? AND channel_id = ? AND watch_enabled = 1",
            (GUILD_ID, channel_id),
        ).fetchone()
    )


async def watched_channels() -> List[sqlite3.Row]:
    return await ARCHIVE.run(
        lambda conn: conn.execute(
            "SELECT * FROM watched_channel WHERE guild_id = ? AND watch_enabled = 1",
            (GUILD_ID,),
        ).fetchall()
    )


async def watched_categories() -> List[sqlite3.Row]:
    return await ARCHIVE.run(
        lambda conn: conn.execute(
            "SELECT * FROM watched_category WHERE guild_id = ? AND watch_enabled = 1",
            (GUILD_ID,),
        ).fetchall()
    )


async def update_channel_cursor(channel_id: int, **fields: Any) -> None:
    if not fields:
        return

    allowed = {
        "historical_scan_complete",
        "scan_anchor_message_id",
        "historical_before_message_id",
        "last_processed_message_id",
        "last_scan_started_at",
        "last_scan_finished_at",
        "last_catchup_completed_at",
        "last_error",
    }
    assignments = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unsupported watched_channel field: {key}")
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(channel_id)

    await ARCHIVE.run(
        lambda conn: conn.execute(
            f"UPDATE watched_channel SET {', '.join(assignments)} WHERE channel_id = ?",
            values,
        )
    )


async def advance_channel_last_processed_message(channel_id: int, message_id: int) -> None:
    await ARCHIVE.run(
        lambda conn: conn.execute(
            """
            UPDATE watched_channel
            SET last_processed_message_id = CASE
              WHEN last_processed_message_id IS NULL OR last_processed_message_id < ?
              THEN ?
              ELSE last_processed_message_id
            END
            WHERE channel_id = ?
            """,
            (message_id, message_id, channel_id),
        )
    )


async def normalize_channel_effective_order(channel_id: int) -> None:
    def sync(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id FROM discord_epub
            WHERE channel_id = ? AND is_deleted = 0
            ORDER BY message_id ASC, attachment_index ASC
            """,
            (channel_id,),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE discord_epub SET effective_order = ? WHERE id = ?",
                (index, row["id"]),
            )

    await ARCHIVE.run(sync)


async def soft_delete_epubs(
    channel_id: int,
    ids: Iterable[int],
    actor_id: int,
    reason: str,
) -> int:
    clean_ids = [int(value) for value in ids]
    if not clean_ids:
        return 0

    def sync(conn: sqlite3.Connection) -> int:
        placeholders = ",".join("?" for _ in clean_ids)
        cur = conn.execute(
            f"""
            UPDATE discord_epub
            SET is_deleted = 1, deleted_at = ?, deleted_by_user_id = ?, delete_reason = ?
            WHERE guild_id = ? AND channel_id = ? AND id IN ({placeholders}) AND is_deleted = 0
            """,
            [unix_now(), actor_id, reason[:500], GUILD_ID, channel_id, *clean_ids],
        )
        return cur.rowcount

    return await ARCHIVE.run(sync)


async def undelete_epubs(
    channel_id: int,
    ids: Iterable[int],
) -> int:
    clean_ids = [int(value) for value in ids]
    if not clean_ids:
        return 0

    def sync(conn: sqlite3.Connection) -> int:
        placeholders = ",".join("?" for _ in clean_ids)
        cur = conn.execute(
            f"""
            UPDATE discord_epub
            SET is_deleted = 0, deleted_at = NULL, deleted_by_user_id = NULL, delete_reason = NULL
            WHERE guild_id = ? AND channel_id = ? AND id IN ({placeholders}) AND is_deleted = 1
            """,
            [GUILD_ID, channel_id, *clean_ids],
        )
        return cur.rowcount

    return await ARCHIVE.run(sync)


async def move_epub_after(channel_id: int, moving_id: int, target_id: Optional[int]) -> None:
    def sync(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id FROM discord_epub
            WHERE channel_id = ? AND is_deleted = 0
            ORDER BY effective_order ASC
            """,
            (channel_id,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if moving_id not in ids:
            raise ValueError("Selected EPUB is no longer available")
        ids.remove(moving_id)
        if target_id is None:
            ids.insert(0, moving_id)
        else:
            if target_id not in ids:
                raise ValueError("Placement target is no longer available")
            ids.insert(ids.index(target_id) + 1, moving_id)
        for index, row_id in enumerate(ids, start=1):
            conn.execute(
                "UPDATE discord_epub SET effective_order = ? WHERE id = ?",
                (index, row_id),
            )

    await ARCHIVE.run(sync)

