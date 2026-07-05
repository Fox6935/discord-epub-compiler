import asyncio
from typing import Optional

import discord
from discord import app_commands

from config import (
    GUILD_ID, TOKEN, bot, get_configured_guild, is_admin, is_configured_guild,
)
from db import (
    ARCHIVE, add_special_role_id, delete_special_role_id, get_watched_channel_row,
    has_compile_action_permission, list_special_role_ids,
)
from epub_tools import is_epub_attachment
from ingestion import (
    category_reconcile_loop, cleanup_sessions, enqueue_historical_scan,
    ensure_live_import_worker_started, ensure_scan_worker_started,
    enqueue_live_message_epubs, reconcile_watched_category, retry_channel_import_failures,
    startup_channel_work, upsert_watched_category, upsert_watched_channel,
)
from models import CompileSession, SESSIONS, build_session_key, log, log_success, log_warning
from ui import CompileLayoutView


def choice_value(value: object) -> str:
    if isinstance(value, app_commands.Choice):
        return str(value.value)

    if value is None:
        return ""

    return str(value)


def parse_role_id(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None

    text = value.strip()

    if text.startswith("<@&") and text.endswith(">"):
        text = text[3:-1]

    if not text.isdecimal():
        return None

    return int(text)


async def compile_role_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None or not is_configured_guild(interaction.guild):
        return []

    selected_action = choice_value(getattr(interaction.namespace, "action", None))

    if selected_action not in {"role_add", "role_delete"}:
        return []

    current = current.lower().strip()
    configured_ids = set(await list_special_role_ids())
    choices: list[app_commands.Choice[str]] = []

    if selected_action == "role_delete":
        role_ids = sorted(configured_ids)

        for role_id in role_ids:
            guild_role = interaction.guild.get_role(role_id)
            label = guild_role.name if guild_role is not None else f"Missing role {role_id}"

            if current and current not in label.lower() and current not in str(role_id):
                continue

            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=str(role_id),
                )
            )

            if len(choices) >= 25:
                break

        return choices

    roles = [
        role
        for role in interaction.guild.roles
        if not role.is_default() and role.id not in configured_ids
    ]
    roles.sort(key=lambda role: (-role.position, role.name.lower()))

    for guild_role in roles:
        if current and current not in guild_role.name.lower():
            continue

        choices.append(
            app_commands.Choice(
                name=guild_role.name[:100],
                value=str(guild_role.id),
            )
        )

        if len(choices) >= 25:
            break

    return choices


@bot.tree.command(name="compile", description="Compile or manage archived EPUBs in this channel")
@app_commands.describe(
    action="Optional admin action",
    role="Role to add or delete",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="delete", value="delete"),
        app_commands.Choice(name="reorder", value="reorder"),
        app_commands.Choice(name="role_add", value="role_add"),
        app_commands.Choice(name="role_delete", value="role_delete"),
    ]
)
@app_commands.autocomplete(role=compile_role_autocomplete)
async def compile_command(
    interaction: discord.Interaction,
    action: Optional[app_commands.Choice[str]] = None,
    role: Optional[str] = None,
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

    if selected_action in {"role_add", "role_delete"}:
        if not is_admin(interaction):
            await interaction.response.send_message(
                "`/compile action:role_add` and `action:role_delete` require Discord Administrator.",
                ephemeral=True,
            )
            return

        role_id = parse_role_id(role)

        if role_id is None:
            await interaction.response.send_message(
                "Choose a role from the role autocomplete.",
                ephemeral=True,
            )
            return

        guild_role = interaction.guild.get_role(role_id)

        if selected_action == "role_add":
            if guild_role is None or guild_role.is_default():
                await interaction.response.send_message(
                    "Choose an existing server role.",
                    ephemeral=True,
                )
                return

            added = await add_special_role_id(role_id, interaction.user.id)
            message = (
                f"{guild_role.mention} can now use `/compile action:delete` and `/compile action:reorder`."
                if added
                else f"{guild_role.mention} is already configured."
            )
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        deleted = await delete_special_role_id(role_id)

        if guild_role is not None:
            role_label = guild_role.mention
        else:
            role_label = f"`{role_id}`"

        message = (
            f"{role_label} removed from compile delete/reorder access."
            if deleted
            else "That role is not configured."
        )

        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if role is not None:
        await interaction.response.send_message(
            "Role options are only used with `/compile action:role_add` or `action:role_delete`.",
            ephemeral=True,
        )
        return

    if isinstance(interaction.channel, discord.TextChannel):
        me = interaction.guild.me
        if me is not None and not interaction.channel.permissions_for(me).attach_files:
            await interaction.response.send_message(
                "I can't upload files in this channel.",
                ephemeral=True,
            )
            return

    if selected_action in {"delete", "reorder"} and not await has_compile_action_permission(interaction.user):
        await interaction.response.send_message(
            "You need Administrator or a configured role for that action.",
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
        log_warning(f"No EPUBs found in #{channel_name}")
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
            queued = await enqueue_historical_scan(channel)
            scan_note = (
                "Historical backfill was queued."
                if queued
                else "Historical backfill is already queued or running."
            )
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
    if not is_configured_guild(message.guild):
        return

    row = await get_watched_channel_row(message.channel.id)
    if row is None:
        return

    if not any(is_epub_attachment(att) for att in message.attachments):
        return

    enqueue_live_message_epubs(message)


@bot.event
async def on_ready() -> None:
    log(f"Logged in as {bot.user}")
    await ARCHIVE.bootstrap()
    guild = get_configured_guild()

    if guild is None:
        log_warning(
            f"Configured guild {GUILD_ID} is not available to this bot. "
            "Check that GUILD_ID is the server ID for the bot's installed server "
            "and that this application has been invited there with the bot scope."
        )
    else:
        log_success(f"Configured single guild: {guild.name} ({guild.id})")

    if not bot.intents.guilds:
        log_warning("Intent warning: guilds intent is disabled")
    if not bot.intents.messages:
        log_warning("Intent warning: guild messages intent is disabled")
    if not bot.intents.message_content:
        log_warning(
            "Intent warning: message content intent is disabled; "
            "live attachment ingestion may not see new EPUB uploads"
        )

    if not getattr(bot, "_cleanup_started", False):
        bot._cleanup_started = True
        asyncio.create_task(cleanup_sessions())
        ensure_scan_worker_started()
        ensure_live_import_worker_started()
        log("Cleanup task started")

    if guild is not None and not getattr(bot, "_guild_work_started", False):
        bot._guild_work_started = True
        asyncio.create_task(category_reconcile_loop())
        asyncio.create_task(startup_channel_work())
    elif guild is None and not getattr(bot, "_guild_work_started", False):
        log_warning("Guild-dependent startup work skipped because configured guild is unavailable")

    if not getattr(bot, "_synced", False):
        if guild is None:
            log_warning("Guild command sync skipped because configured guild is unavailable")
            return

        try:
            guild_object = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_object)
            synced = await bot.tree.sync(guild=guild_object)
            bot._synced = True
            log_success(f"Synced {len(synced)} guild command(s) for {GUILD_ID}")
        except discord.Forbidden:
            bot._synced = True
            log_warning(
                f"Command sync failed: missing access to guild {GUILD_ID}. "
                "Reinvite the bot/application to that server with bot and applications.commands scopes."
            )
        except Exception as exc:
            log_warning(f"Command sync failed: {exc}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN in your environment.")
    if not GUILD_ID:
        raise RuntimeError("Set DISCORD_GUILD_ID or GUILD_ID in your environment.")

    bot.run(TOKEN)
