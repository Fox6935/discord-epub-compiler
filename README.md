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
- Administrator/configured-role delete and reorder flows
- Administrator settings modal for compile-action roles and archive-failure alert roles
- Batched in-channel archive-failure alerts with a 15-second per-channel debounce
- Verified SHA-256 checksums for retained EPUB components and archives
- Destructive, confirmation-gated channel rescans
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
EPUB_ARCHIVE_DB=/compileSQL/epub_archive.sqlite3
```

Delete/reorder roles and archive-failure alert roles are configured with
`/compile action:settings`.

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

- `/compile` No action: select archived EPUBs and compile them
- `/compile action:delete`: soft-delete active EPUBs or restore already deleted EPUBs
- `/compile action:reorder`: move one EPUB between adjacent neighbors
- `/compile action:settings`: open an Administrator-only modal for compile-action roles and
  archive-failure alert roles
Saved entries are preselected; deselecting them removes access or notifications.
Delete and reorder require Discord Administrator or one configured role.

### `/rescan`

Requires Discord Administrator and an actively watched channel.

The command opens an ephemeral confirmation. Confirming permanently deletes that
channel's archived EPUBs, history, custom ordering, scan cursors, and
import failures from SQLite, then queues a full scan of Discord history. It never
deletes Discord messages or attachments. Files no longer present in Discord cannot
be recovered.

If the same channel is already scanning, that scan is cancelled and restarted.
If another channel is scanning, the rescan waits in the queue.

### Archive failure notifications

After an EPUB fails to archive, the configured roles are mentioned in that channel
after 15 seconds of quiet time. Additional failures reset the delay. Finishing a
channel scan flushes its pending failures immediately. Messages are formatted as:

```text
@Role
Archive Failed: Filename1.epub
Archive Failed: Filename2.epub
```

Long batches are split to respect Discord's 2,000-character message limit, with the
role mentions repeated in each message. An unresolved failure is marked notified
only after its channel message is sent successfully.

## Archive Files

By default the archive database is `/compileSQL/epub_archive.sqlite3`.

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
- View Channel and Read Message History are required to archive a watched channel.
- Missing Send Messages or Attach Files is reported when watching/rescanning and at
  startup. Archiving may continue, but compilation delivery remains blocked.
- Stored EPUB content is verified using per-component SHA-256 hashes and an aggregate
  checksum over the retained archive components.

## License

MIT (c) 2026
