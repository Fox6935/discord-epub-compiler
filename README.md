# Discord EPUB Compiler

A Discord bot that lets users select multiple EPUB attachments from a channel and compiles them into one clean, readable EPUB.

Built for webnovel discord communities.

## Features

- Scans discord channel for epubs
- Interactive epub selector UI with pagination
- Automatically skips covers, TOCs, title pages, and non-chapter content
- Preserves inline images only
- Final file delivered via DM

## Setup

1. Clone the repo
2. `pip install -r requirements.txt`
3. Create a `.env` file with your bot token:
```
DISCORD_TOKEN=your_token_here
```
4. Run: `python bot.py`

Invite the bot with `applications.commands` and `Read Message History` permissions.

## License

MIT © 2026
