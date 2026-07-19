import contextlib
from typing import Optional

import discord
from discord import app_commands

from config import (
    GUILD_ID, TOKEN, bot, get_configured_guild, is_admin, is_configured_guild,
)
from db import (
    ARCHIVE, delete_import_failures_for_messages, get_compile_settings, get_watched_channel_row,
    has_compile_action_permission, replace_compile_settings,
)
from epub_tools import is_epub_attachment
from ingestion import (
    channel_permission_issues, enqueue_historical_scan,
    ensure_background_task,
    ensure_live_import_worker_started, ensure_scan_worker_started,
    ensure_support_tasks_started,
    enqueue_live_message_epubs, reconcile_watched_category, retry_channel_import_failures,
    is_channel_in_maintenance, reset_and_enqueue_channel, scan_queue_size,
    start_startup_channel_work, upsert_watched_category, upsert_watched_channel,
)
from models import CompileSession, SESSIONS, build_session_key, log, log_success, log_warning
from ui import CompileLayoutView


class CompileSettingsModal(discord.ui.Modal, title="Compile Settings"):
    def __init__(self, user_id: int, role_ids: list[int], alert_role_ids: list[int]):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.role_select = discord.ui.RoleSelect(
            placeholder="Delete, restore, and reorder roles",
            min_values=0,
            max_values=25,
            required=False,
            default_values=[discord.Object(id=role_id) for role_id in role_ids],
        )
        self.alert_role_select = discord.ui.RoleSelect(
            placeholder="Archive failure alert roles",
            min_values=0,
            max_values=25,
            required=False,
            default_values=[discord.Object(id=role_id) for role_id in alert_role_ids],
        )
        self.add_item(
            discord.ui.Label(
                text="Compile action roles",
                description="These roles can delete, restore, and reorder archived EPUBs.",
                component=self.role_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Archive failure alert roles",
                description="These roles are mentioned in-channel when EPUB archiving fails.",
                component=self.alert_role_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id or not is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator to change compile settings.",
                ephemeral=True,
            )
            return
        if interaction.guild is None or not is_configured_guild(interaction.guild):
            await interaction.response.send_message(
                "This bot is configured for a different server.",
                ephemeral=True,
            )
            return

        roles = list(self.role_select.values)
        alert_roles = list(self.alert_role_select.values)
        if any(role.is_default() for role in roles + alert_roles):
            await interaction.response.send_message(
                "The @everyone role cannot be used in compile settings.",
                ephemeral=True,
            )
            return

        await replace_compile_settings(
            [role.id for role in roles],
            [role.id for role in alert_roles],
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"Saved {len(roles)} compile role(s) and {len(alert_roles)} archive alert role(s).",
            ephemeral=True,
        )


class RescanConfirmView(discord.ui.View):
    def __init__(self, user_id: int, channel_id: int, archive_generation: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.channel_id = channel_id
        self.archive_generation = archive_generation
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This rescan confirmation isn't yours.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm Rescan", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("`/rescan` requires Discord Administrator.", ephemeral=True)
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.id != self.channel_id:
            await interaction.response.send_message("The rescan channel is no longer available.", ephemeral=True)
            return
        row = await get_watched_channel_row(channel.id)
        if row is None or row["archive_generation"] != self.archive_generation:
            await interaction.response.send_message(
                "This channel's archive state changed. Run `/rescan` again.",
                ephemeral=True,
            )
            return
        blocking, _ = channel_permission_issues(channel)
        if blocking:
            await interaction.response.send_message(
                "Rescan blocked. Missing: " + ", ".join(blocking),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            _, queued = await reset_and_enqueue_channel(channel, self.archive_generation)
        except Exception as exc:
            log_warning(f"Rescan reset failed in #{channel.name}: {exc}")
            await interaction.edit_original_response(
                content=f"Rescan failed before it could be queued: {exc}",
                view=None,
            )
            return

        queue_note = (
            f"Rescan queued. Queue size: {scan_queue_size()}."
            if queued
            else "The archive was reset, but the scan was not queued; startup recovery will retry it."
        )
        await interaction.edit_original_response(content=queue_note, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Rescan cancelled. No data was changed.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message is not None:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await self.message.edit(content="Rescan confirmation expired. No data was changed.", view=None)


@bot.tree.command(name="compile", description="Compile or manage archived EPUBs in this channel")
@app_commands.describe(
    action="Optional admin action",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="delete", value="delete"),
        app_commands.Choice(name="reorder", value="reorder"),
        app_commands.Choice(name="settings", value="settings"),
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

    if selected_action == "settings":
        if not is_admin(interaction):
            await interaction.response.send_message(
                "`/compile action:settings` requires Discord Administrator.",
                ephemeral=True,
            )
            return
        role_ids, alert_role_ids = await get_compile_settings()
        await interaction.response.send_modal(
            CompileSettingsModal(interaction.user.id, role_ids, alert_role_ids)
        )
        return

    if is_channel_in_maintenance(interaction.channel.id):
        await interaction.response.send_message(
            "This channel's archive is being reset. Try again after its rescan is queued.",
            ephemeral=True,
        )
        return

    if selected_action == "select" and isinstance(interaction.channel, discord.TextChannel):
        me = interaction.guild.me
        if me is not None:
            perms = interaction.channel.permissions_for(me)
        else:
            perms = None
        if perms is not None and not (perms.send_messages and perms.attach_files):
            missing = []
            if not perms.send_messages:
                missing.append("Send Messages")
            if not perms.attach_files:
                missing.append("Attach Files")
            await interaction.response.send_message(
                "I can't deliver compiled EPUBs in this channel. Missing: " + ", ".join(missing),
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


@bot.tree.command(name="rescan", description="Delete and rebuild this channel's EPUB archive")
async def rescan_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Use this command in a server channel.", ephemeral=True)
        return
    if not is_configured_guild(interaction.guild):
        await interaction.response.send_message("This bot is configured for a different server.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("`/rescan` requires Discord Administrator.", ephemeral=True)
        return
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "Use `/rescan` in a standard text or announcement channel.",
            ephemeral=True,
        )
        return
    if is_channel_in_maintenance(channel.id):
        await interaction.response.send_message("This channel is already being reset.", ephemeral=True)
        return
    row = await get_watched_channel_row(channel.id)
    if row is None:
        await interaction.response.send_message(
            "This channel is not actively watched. Use `/scan action:channel_enable` first.",
            ephemeral=True,
        )
        return
    blocking, warnings = channel_permission_issues(channel)
    if blocking:
        await interaction.response.send_message(
            "Rescan blocked. Missing: " + ", ".join(blocking),
            ephemeral=True,
        )
        return

    warning_text = (
        f"\n\nPermission warning: missing {', '.join(warnings)}. Archiving can proceed, "
        "but compiled EPUB delivery will not work until fixed."
        if warnings
        else ""
    )
    view = RescanConfirmView(
        interaction.user.id,
        channel.id,
        row["archive_generation"],
    )
    await interaction.response.send_message(
        "This WILL permanently delete every archived EPUB, soft-delete record, custom order, "
        "scan cursor, and import failure for this channel from SQLite before rescanning Discord "
        "history from scratch. Discord messages and attachments will not be deleted. Files no "
        "longer present in Discord cannot be recovered."
        + warning_text,
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


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

    permission_warnings: list[str] = []
    if action.value == "channel_enable":
        blocking, permission_warnings = channel_permission_issues(channel)
        if blocking:
            await interaction.response.send_message(
                "I cannot scan this channel. Missing: " + ", ".join(blocking),
                ephemeral=True,
            )
            return
    permission_note = (
        " Permission warning: missing " + ", ".join(permission_warnings) +
        "; archiving will work, but compiled EPUB delivery will not."
        if permission_warnings
        else ""
    )

    await interaction.response.defer(ephemeral=True, thinking=True)

    if action.value == "channel_enable":
        await upsert_watched_channel(channel, True)
        ensure_background_task(
            f"retry-imports-{channel.id}",
            lambda: retry_channel_import_failures(channel),
            restart=False,
        )
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
            f"This channel is now watched. Recorded archive failures were queued for retry. "
            f"{scan_note}{permission_note}",
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
        stats = await reconcile_watched_category(category)
        await interaction.followup.send(
            f"Category watching enabled for {category.name}. "
            f"Checked {stats['checked']}; queued {stats['queued']}; "
            f"new channels {stats['added']}; skipped {stats['skipped']}; "
            f"permission warnings {stats['warnings']}.",
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

    enqueue_live_message_epubs(message, row["archive_generation"])


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    if payload.guild_id != GUILD_ID:
        return
    await delete_import_failures_for_messages(payload.channel_id, [payload.message_id])


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent) -> None:
    if payload.guild_id != GUILD_ID:
        return
    await delete_import_failures_for_messages(payload.channel_id, payload.message_ids)


@bot.event
async def on_ready() -> None:
    log(f"Logged in as {bot.user}")
    if not getattr(bot, "_archive_bootstrapped", False):
        await ARCHIVE.bootstrap()
        bot._archive_bootstrapped = True
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

    ensure_scan_worker_started()
    ensure_live_import_worker_started()
    ensure_support_tasks_started()

    if guild is not None and not getattr(bot, "_guild_work_started", False):
        bot._guild_work_started = True
        start_startup_channel_work()
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
