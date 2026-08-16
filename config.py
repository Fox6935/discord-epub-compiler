import os
import re

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB_PATH = "/compileSQL/epub_archive.sqlite3"

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
DB_PATH = os.environ.get("EPUB_ARCHIVE_DB", DEFAULT_DB_PATH)
EXTERNAL_UPLOAD_URL = os.environ.get("API_URL", "")
EXTERNAL_UPLOAD_KEY = os.environ.get("API_KEY", "")

SESSION_TIMEOUT_SECONDS = 15 * 60
MAX_SESSION_LIFETIME_SECONDS = 60 * 60
PAGE_SIZE = 25
HISTORY_BATCH_SIZE = 100
CATEGORY_RECONCILE_SECONDS = 30 * 60
SCAN_WATCHDOG_SECONDS = 60
LIVE_IMPORT_DELAY_SECONDS = 10
LIVE_IMPORT_RATE_SECONDS = 10
ARCHIVE_FAILURE_ALERT_DELAY_SECONDS = 15

MAX_SOURCE_EPUB_BYTES = 100 * 1024 * 1024
MAX_SOURCE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_SINGLE_FILE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000
DEFAULT_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_EPUB_BYTES = DEFAULT_UPLOAD_LIMIT_BYTES
MAX_EXTERNAL_OUTPUT_EPUB_BYTES = 200 * 1024 * 1024
EPUB_BASE_OVERHEAD_BYTES = 8 * 1024
EPUB_PER_CHAPTER_OVERHEAD_BYTES = 160
EPUB_PER_IMAGE_OVERHEAD_BYTES = 80
IMAGE_SIZE_ABORT_RATIO = 0.95
HTTP_TIMEOUT_SECONDS = 300
MAX_CONCURRENT_COMPILES = 2

ILLEGAL_OUTPUT_NAME_RE = re.compile(r"[^A-Za-z0-9 _.,'()\-]")
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._\-]")
SAFE_META_RE = re.compile(r"\s+")
EPUB_EXT_RE = re.compile(r"\.epub$", re.IGNORECASE)

CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
EPUB_NS = "http://www.idpf.org/2007/ops"

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def is_configured_guild_id(guild_id: int | None) -> bool:
    return bool(GUILD_ID and guild_id == GUILD_ID)


def is_configured_guild(guild: discord.Guild | None) -> bool:
    return guild is not None and is_configured_guild_id(guild.id)


def get_configured_guild() -> discord.Guild | None:
    if not GUILD_ID:
        return None
    return bot.get_guild(GUILD_ID)


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)
