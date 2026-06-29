# Discord EPUB Compiler

A single-guild Discord bot that archives EPUB attachments into SQLite, then lets users compile selected archived EPUBs into one clean, readable EPUB.

The bot imports EPUBs as messages arrive and during watched-channel backfills. Compile-time reads come from SQLite, not from Discord history or live attachment downloads.

## Features

- SQLite-backed archive for watched channels, watched categories, EPUB records, EPUB components, and import failures
- Single-guild operation configured by environment variable
- `/scan` commands for enabling or disabling channel/category watching
- Historical backfill with resumable scan cursors
- Startup catch-up for watched channels
- Live ingestion for new EPUB attachments in watched channels
- Interactive `/compile` selector with pagination and page jump buttons
- Compiles selected archived EPUBs in chronological reading order
- Automatically skips covers, TOCs, title pages, and non-chapter content
- Preserves referenced inline images, with an option to remove all images
- Admin/special-role delete and reorder flows
- Soft delete support, including restore from the delete menu
- Reorder support using neighbor placement

## Requirements

- Python 3.11 or newer recommended
- A Discord bot token
- A Discord application invited to exactly the guild configured for this bot
- Bot permissions for watched channels:
  - View Channel
  - Read Message History
  - Send Messages
  - Attach Files
- Privileged Gateway Intents enabled in the Discord Developer Portal:
  - Message Content Intent
- Application command scope:
  - `applications.commands`

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create a `.env` file:

```env
DISCORD_TOKEN=your_token_here
GUILD_ID=your_discord_server_id
```

Optional environment variables:

```env
EPUB_ARCHIVE_DB=epub_archive.sqlite3
SPECIAL_ROLE_ID=role_allowed_to_delete_and_reorder
```

`SPECIAL_ROLE_ID` is not an admin role. It only grants access to `/compile action:delete` and `/compile action:reorder`. Discord Administrators can use those actions automatically.

3. Run the bot:

```bash
python bot.py
```

## Commands

### `/scan`

Requires Discord Administrator.

- `channel_enable`: watch the current channel and start or resume historical backfill
- `channel_disable`: stop watching the current channel
- `category_enable`: watch eligible text/news channels in the current category
- `category_disable`: stop category reconciliation; existing watched channels remain watched

### `/compile`

Available to regular users in archived channels.

- No action: select archived EPUBs and compile them
- `action:delete`: soft-delete active EPUBs or restore already deleted EPUBs
- `action:reorder`: move one EPUB between adjacent neighbors

Delete and reorder require Discord Administrator or `SPECIAL_ROLE_ID`.

## Archive Files

By default the archive database is `epub_archive.sqlite3`.

SQLite may also create:

- `epub_archive.sqlite3-wal`: write-ahead log for recent changes
- `epub_archive.sqlite3-shm`: shared-memory coordination file for WAL mode

Keep all three files together while the bot is running. They are normal SQLite sidecar files.

## Notes

- The bot is single-guild only. Set `GUILD_ID` or `DISCORD_GUILD_ID`.
- Compile-time source retrieval is DB-backed.
- Live ingestion depends on Message Content Intent so Discord includes attachment metadata in message events.
- Deleted EPUBs are soft-deleted and can be restored from `/compile action:delete`.
- Reorder is blocked until a channel's historical scan is complete.

## License

MIT (c) 2026
