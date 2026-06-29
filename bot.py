import asyncio
from typing import Optional

import discord
from discord import app_commands

from config import (
    GUILD_ID, TOKEN, bot, get_configured_guild, has_compile_action_permission,
    is_admin, is_configured_guild,
)
from db import ARCHIVE, get_watched_channel_row, update_channel_cursor
from ingestion import (
    category_reconcile_loop, cleanup_sessions, import_message_epubs,
    reconcile_watched_category, retry_channel_import_failures, start_historical_scan,
    startup_channel_work, upsert_watched_category, upsert_watched_channel,
)
from models import CompileSession, SESSIONS, build_session_key, log
from ui import CompileLayoutView


@bot.tree.command(name="compile", description="Compile, delete, or reorder archived EPUBs in this channel")
@app_commands.describe(action="Optional admin action")
@app_commands.choices(
    action=[
        app_commands.Choice(name="delete", value="delete"),
        app_commands.Choice(name="reorder", value="reorder"),
    ]
)
async def compile_command(
    interaction: discord.Interaction,
    action: Optional[app_commands.Choice[str]] = None,
) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "Use this command in a server channel.",
            ephemeral=True,
        )
        return
    if not is_configured_guild(interaction.guild):
        await interaction.response.send_message(
            "This bot is configured for a different server.",
            ephemeral=True,
        )
        return

    channel_name = getattr(interaction.channel, "name", "Discord Channel")
    selected_action = action.value if action else "select"
    log(f"/compile action={selected_action} run by {interaction.user} in #{channel_name}")

    if isinstance(interaction.channel, discord.TextChannel):
        me = interaction.guild.me
        if me is not None and not interaction.channel.permissions_for(me).attach_files:
            await interaction.response.send_message(
                "I can't upload files in this channel.",
                ephemeral=True,
            )
            return

    if selected_action in {"delete", "reorder"} and not has_compile_action_permission(interaction.user):
        await interaction.response.send_message(
            "You need Administrator or the configured special role for that action.",
            ephemeral=True,
        )
        return

    if selected_action == "reorder":
        watched = await get_watched_channel_row(interaction.channel.id)
        if watched is None or not watched["historical_scan_complete"]:
            await interaction.response.send_message(
                "Reorder is blocked until this channel's historical scan is complete.",
                ephemeral=True,
            )
            return

    key = build_session_key(interaction.user.id, interaction.channel.id)
    old_session = SESSIONS.pop(key, None)

    if old_session is not None:
        old_session.expired = True

    session = CompileSession(
        user_id=interaction.user.id,
        channel_id=interaction.channel.id,
        channel_name=channel_name,
    )
    if selected_action == "delete":
        session.flow_mode = "delete"
    elif selected_action == "reorder":
        session.flow_mode = "reorder_move"

    SESSIONS[key] = session

    await interaction.response.defer(ephemeral=True, thinking=True)

    entries = await ARCHIVE.list_channel_epubs(
        interaction.channel.id,
        include_deleted=selected_action == "delete",
    )
    session.entries = entries

    async with session.lock:
        found_count = len(session.entries)

    if not found_count:
        log(f"No EPUBs found in #{channel_name}")
        session.expired = True
        SESSIONS.pop(key, None)
        await interaction.followup.send(
            "No archived EPUBs found in this channel. Ask an admin to enable `/scan action:channel_enable` first.",
            ephemeral=True,
        )
        return

    log(f"Found {found_count} archived EPUB(s) in #{channel_name}")

    view = CompileLayoutView(session)

    msg = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
    )
    view.message = msg


@bot.tree.command(name="scan", description="Enable or disable SQLite EPUB archiving for this channel or category")
@app_commands.describe(action="Watch management action")
@app_commands.choices(
    action=[
        app_commands.Choice(name="channel_enable", value="channel_enable"),
        app_commands.Choice(name="channel_disable", value="channel_disable"),
        app_commands.Choice(name="category_enable", value="category_enable"),
        app_commands.Choice(name="category_disable", value="category_disable"),
    ]
)
async def scan_command(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Use this command in a server channel.", ephemeral=True)
        return
    if not is_configured_guild(interaction.guild):
        await interaction.response.send_message(
            "This bot is configured for a different server.",
            ephemeral=True,
        )
        return

    if not is_admin(interaction):
        await interaction.response.send_message("`/scan` requires Discord Administrator.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Use `/scan` in a standard text or announcement channel.", ephemeral=True)
        return

    me = interaction.guild.me
    if me is not None:
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            await interaction.response.send_message(
                "I need View Channel and Read Message History to scan this channel.",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True, thinking=True)

    if action.value == "channel_enable":
        await upsert_watched_channel(channel, True)
        asyncio.create_task(retry_channel_import_failures(channel))
        watched = await get_watched_channel_row(channel.id)

        if watched is not None and not watched["historical_scan_complete"]:
            asyncio.create_task(start_historical_scan(channel))
            scan_note = "Historical backfill has started or resumed."
        else:
            scan_note = "Historical backfill is already complete."

        await interaction.followup.send(
            f"This channel is now watched. {scan_note}",
            ephemeral=True,
        )
        return

    if action.value == "channel_disable":
        await upsert_watched_channel(channel, False)
        category = channel.category
        warning = ""
        if category is not None:
            watched = await ARCHIVE.run(
                lambda conn: conn.execute(
                    "SELECT 1 FROM watched_category WHERE category_id = ? AND watch_enabled = 1",
                    (category.id,),
                ).fetchone()
            )
            if watched:
                warning = (
                    "\n\nWarning: this channel is still inside an enabled watched category. "
                    "It will be re-added on the next category reconciliation. "
                    "Move it out of that category or disable category watching to keep it disabled."
                )
        await interaction.followup.send(f"This channel is disabled now.{warning}", ephemeral=True)
        return

    category = channel.category
    if category is None:
        await interaction.followup.send("This channel is not inside a category.", ephemeral=True)
        return

    if action.value == "category_enable":
        await upsert_watched_category(category, True)
        added = await reconcile_watched_category(category)
        await interaction.followup.send(
            f"Category watching enabled for {category.name}. Added or refreshed eligible channels; {added} new backfill job(s) started.",
            ephemeral=True,
        )
        return

    if action.value == "category_disable":
        await upsert_watched_category(category, False)
        await interaction.followup.send(
            "Category watching disabled. Existing watched channels remain watched.",
            ephemeral=True,
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not is_configured_guild(message.guild):
        return

    row = await get_watched_channel_row(message.channel.id)
    if row is None:
        return

    await import_message_epubs(message)
    await update_channel_cursor(
        message.channel.id,
        last_processed_message_id=max(message.id, row["last_processed_message_id"] or 0),
    )


@bot.event
async def on_ready() -> None:
    log(f"Logged in as {bot.user}")
    await ARCHIVE.bootstrap()
    guild = get_configured_guild()

    if guild is None:
        log(
            f"Configured guild {GUILD_ID} is not available to this bot. "
            "Check that GUILD_ID is the server ID for the bot's installed server "
            "and that this application has been invited there with the bot scope."
        )
    else:
        log(f"Configured single guild: {guild.name} ({guild.id})")

    if not bot.intents.guilds:
        log("Intent warning: guilds intent is disabled")
    if not bot.intents.messages:
        log("Intent warning: guild messages intent is disabled")
    if not bot.intents.message_content:
        log(
            "Intent warning: message content intent is disabled; "
            "live attachment ingestion may not see new EPUB uploads"
        )

    if not getattr(bot, "_cleanup_started", False):
        bot._cleanup_started = True
        asyncio.create_task(cleanup_sessions())
        log("Cleanup task started")

    if guild is not None and not getattr(bot, "_guild_work_started", False):
        bot._guild_work_started = True
        asyncio.create_task(category_reconcile_loop())
        asyncio.create_task(startup_channel_work())
    elif guild is None and not getattr(bot, "_guild_work_started", False):
        log("Guild-dependent startup work skipped because configured guild is unavailable")

    if not getattr(bot, "_synced", False):
        if guild is None:
            log("Guild command sync skipped because configured guild is unavailable")
            return

        try:
            guild_object = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_object)
            synced = await bot.tree.sync(guild=guild_object)
            bot._synced = True
            log(f"Synced {len(synced)} guild command(s) for {GUILD_ID}")
        except discord.Forbidden:
            bot._synced = True
            log(
                f"Command sync failed: missing access to guild {GUILD_ID}. "
                "Reinvite the bot/application to that server with bot and applications.commands scopes."
            )
        except Exception as exc:
            log(f"Command sync failed: {exc}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN in your environment.")
    if not GUILD_ID:
        raise RuntimeError("Set DISCORD_GUILD_ID or GUILD_ID in your environment.")

    bot.run(TOKEN)
