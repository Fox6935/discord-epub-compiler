import contextlib
import io
import traceback
from datetime import timezone
from typing import List, Optional

import discord

from compiler import compile_selected_epubs
from config import SESSION_TIMEOUT_SECONDS, has_compile_action_permission
from db import move_epub_after, soft_delete_epubs, undelete_epubs
from epub_tools import (
    disable_view_items, format_bytes, resolve_upload_limit_bytes, safe_default_output_name,
    sanitize_author, sanitize_output_name,
)
from filter import FilenameFilter
from models import (
    COMPILE_SEMAPHORE, CompileSession, DEBUG_LOGS, OutputTooLargeError,
    is_session_live, log, log_success, log_warning,
)


WARNING_MARK = "\u26a0\ufe0f"


def format_compact_estimate(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    unit_index = 0

    while value >= 999.5 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1

    unit = units[unit_index]

    if unit == "B" or value >= 10:
        return f"{round(value):.0f}{unit}"

    return f"{value:.1f}{unit}"


class EpubPickerSelect(discord.ui.Select):
    def __init__(self, session: CompileSession):
        page_entries = session.current_page_entries()
        options = []
        selected = (
            session.placement_ids
            if session.flow_mode == "reorder_place"
            else session.selected_ids
        )

        for entry in page_entries:
            created = entry.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
            if session.flow_mode == "delete" and entry.is_deleted:
                label = f"{WARNING_MARK}{entry.filename}{WARNING_MARK}"
            else:
                label = entry.filename
            label = label[:100]
            size = format_bytes(entry.attachment_size or 0)
            description = f"{created} - {size}"
            if session.flow_mode == "delete" and entry.is_deleted:
                description = f"Deleted - {description}"

            options.append(
                discord.SelectOption(
                    label=label,
                    value=entry.entry_id,
                    description=description[:100],
                    default=entry.entry_id in selected,
                )
            )

        if session.flow_mode == "reorder_move":
            placeholder = f"Select EPUB to reorder {session.page_range_label()}"
            min_values = 1
            max_values = 1
        elif session.flow_mode == "reorder_place":
            placeholder = f"Select placement neighbor(s) {session.page_range_label()}"
            min_values = 1
            max_values = min(2, max(1, len(options)))
        else:
            verb = "delete" if session.flow_mode == "delete" else "compile"
            placeholder = (
                f"Select EPUBs to {verb} {session.page_range_label()} "
                f"of {len(session.display_entries()):02d}"
            )
            min_values = 0
            max_values = max(1, len(options))

        super().__init__(
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
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

            if session.flow_mode == "reorder_place":
                session.placement_ids = set(self.values)
            elif session.flow_mode == "reorder_move":
                session.selected_ids = set(self.values)
                session.reorder_moving_id = self.values[0] if self.values else None
            else:
                session.selected_ids.difference_update(page_ids)
                session.selected_ids.update(self.values)

            new_view = CompileLayoutView(session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class FirstPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="First",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            view.session.current_page = 0

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class LastPageButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Last",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            view.session.current_page = view.session.page_count - 1

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


def visible_page_indexes(current_page: int, page_count: int, max_buttons: int = 5) -> List[int]:
    if page_count <= max_buttons:
        return list(range(page_count))

    half = max_buttons // 2
    start = current_page - half
    end = start + max_buttons

    if start < 0:
        start = 0
        end = max_buttons
    elif end > page_count:
        end = page_count
        start = page_count - max_buttons

    return list(range(start, end))


class PageJumpButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, page_index: int, current_page: int):
        super().__init__(
            label=str(page_index + 1),
            style=(
                discord.ButtonStyle.primary
                if page_index == current_page
                else discord.ButtonStyle.secondary
            ),
            disabled=page_index == current_page,
        )
        self.page_index = page_index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()

            if 0 <= self.page_index < view.session.page_count:
                view.session.current_page = self.page_index

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class ToggleRemoveImagesButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, enabled: bool):
        super().__init__(
            label="\u2611 Remove images" if enabled else "\u2610 Remove images",
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
    def __init__(self, disabled: bool):
        super().__init__(
            label="Select Page",
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


class OpenSearchModalButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, active: bool):
        super().__init__(
            label="Edit Search" if active else "Search",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        await interaction.response.send_modal(FilenameSearchModal(view.session))


class ClearSearchButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Clear Search",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if view is None:
            return

        async with view.session.lock:
            view.session.touch()
            view.session.filename_filter = FilenameFilter()
            view.session.current_page = 0

            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class OpenCompileModalButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self, estimate_text: Optional[str]):
        label = "Compile"

        if estimate_text is not None:
            label = f"Compile - {estimate_text}"

        super().__init__(
            label=label,
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

        page_entries = session.current_page_entries()
        page_selected_count = sum(
            1 for entry in page_entries if entry.entry_id in session.selected_ids
        )
        display_total = len(session.display_entries())
        title = {
            "compile": "# EPUB Compiler",
            "delete": "# Delete or Restore Archived EPUBs",
            "reorder_move": "# Reorder Archived EPUBs",
            "reorder_place": "# Choose Reorder Placement",
        }.get(session.flow_mode, "# EPUB Compiler")
        selected_count = (
            len(session.placement_ids)
            if session.flow_mode == "reorder_place"
            else len(session.selected_ids)
        )
        status_line = (
            f"**Selected:** {selected_count}/{display_total} | "
            f"**Page:** {session.current_page + 1}/{session.page_count}"
        )

        stats = discord.ui.Container(
            discord.ui.TextDisplay(title),
            discord.ui.TextDisplay(f"Select EPUBs from <#{session.channel_id}>"),
            accent_colour=discord.Colour.blurple(),
        )
        self.add_item(stats)

        if page_entries:
            self.add_item(discord.ui.ActionRow(EpubPickerSelect(session)))

        self.add_item(discord.ui.TextDisplay(status_line))

        page_buttons = [
            PageJumpButton(page_index, session.current_page)
            for page_index in visible_page_indexes(
                session.current_page,
                session.page_count,
            )
        ]

        if len(page_buttons) > 1:
            self.add_item(discord.ui.ActionRow(*page_buttons))

        if session.page_count > 5:
            self.add_item(
                discord.ui.ActionRow(
                    FirstPageButton(disabled=session.current_page == 0),
                    LastPageButton(disabled=session.current_page >= session.page_count - 1),
                )
            )

        if session.flow_mode in {"compile", "delete"}:
            self.add_item(
                discord.ui.ActionRow(
                    SelectPageButton(
                        disabled=not bool(page_entries)
                        or page_selected_count == len(page_entries),
                    ),
                    ClearPageButton(disabled=page_selected_count == 0),
                    OpenSearchModalButton(session.filename_filter.is_active),
                    ClearSearchButton(disabled=not session.filename_filter.is_active),
                )
            )
        else:
            self.add_item(
                discord.ui.ActionRow(
                    OpenSearchModalButton(session.filename_filter.is_active),
                    ClearSearchButton(disabled=not session.filename_filter.is_active),
                )
            )

        if session.flow_mode == "compile":
            estimated_size = session.estimated_output_bytes()
            estimate_text = (
                format_compact_estimate(estimated_size)
                if estimated_size is not None
                else None
            )
            compile_controls = []

            if session.selected_image_bytes() > 0:
                compile_controls.append(ToggleRemoveImagesButton(session.remove_all_images))

            compile_controls.append(OpenCompileModalButton(estimate_text))
            self.add_item(discord.ui.ActionRow(*compile_controls))
        elif session.flow_mode == "delete":
            self.add_item(discord.ui.ActionRow(DeleteConfirmButton()))
        elif session.flow_mode == "reorder_move":
            self.add_item(discord.ui.ActionRow(OpenPlacementPickerButton()))
        elif session.flow_mode == "reorder_place":
            self.add_item(discord.ui.ActionRow(ReorderApplyButton()))

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

        if (
            self.session.flow_mode in {"delete", "reorder_move", "reorder_place"}
            and not has_compile_action_permission(interaction.user)
        ):
            await interaction.response.send_message(
                "You need Administrator or the configured special role.",
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


class FilenameSearchModal(discord.ui.Modal, title="Advanced Filename Search"):
    help_text = discord.ui.TextDisplay(
        "Search is case-insensitive. Use capitalized AND/OR as operators; "
        "otherwise the whole input is matched as one phrase."
    )
    include = discord.ui.TextInput(
        label="Includes",
        placeholder="include EPUBs with...",
        required=False,
        max_length=200,
    )
    exclude = discord.ui.TextInput(
        label="Excludes",
        placeholder="exclude EPUBs with...",
        required=False,
        max_length=200,
    )

    def __init__(self, session: CompileSession):
        super().__init__(timeout=300)

        self.session = session
        self.include.default = session.filename_filter.include
        self.exclude.default = session.filename_filter.exclude

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "This modal isn't yours.",
                ephemeral=True,
            )
            return

        if not is_session_live(self.session):
            await interaction.response.send_message(
                "This search session expired. Run `/compile` again.",
                ephemeral=True,
            )
            return

        async with self.session.lock:
            self.session.touch()
            self.session.filename_filter = FilenameFilter(
                include=str(self.include or "").strip(),
                exclude=str(self.exclude or "").strip(),
            )
            self.session.current_page = 0

            new_view = CompileLayoutView(self.session)

        await interaction.response.edit_message(view=new_view)


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
                f"Remove images: {'yes' if remove_all_images else 'no'}"
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
            log_warning(f"Compile too large for {interaction.user}: {exc}")
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log_warning(f"Compile failed for {interaction.user}: {exc}")
            if DEBUG_LOGS:
                traceback.print_exc()
            await interaction.followup.send(
                f"Compile failed: {exc}",
                ephemeral=True,
            )
            return

        if output_bytes is None:
            log_warning(f"All selected EPUBs failed for {interaction.user}")
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
            log_warning(f"Couldn't send compiled EPUB to {interaction.user}: {exc}")
            await interaction.followup.send(
                (
                    "I couldn't send the compiled EPUB through Discord.\n"
                    f"Compiled size: {format_bytes(len(output_bytes))}\n"
                    f"Upload limit: {format_bytes(upload_limit)}\n"
                    "Try selecting fewer EPUBs or enable `Remove images`."
                ),
                ephemeral=True,
            )
            return

        log_success(f"{len(selected)} EPUB(s) compiled and sent to {interaction.user}")


class OpenPlacementPickerButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self):
        super().__init__(
            label="Reorder",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if not isinstance(view, CompileLayoutView):
            return

        if len(view.session.selected_ids) != 1:
            await interaction.response.send_message(
                "Select exactly one EPUB to reorder first.",
                ephemeral=True,
            )
            return

        async with view.session.lock:
            view.session.reorder_moving_id = next(iter(view.session.selected_ids))
            view.session.placement_ids.clear()
            view.session.flow_mode = "reorder_place"
            view.session.current_page = 0
            view.session.touch()
            new_view = CompileLayoutView(view.session)
            new_view.message = view.message

        await interaction.response.edit_message(view=new_view)


class DeleteConfirmButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self):
        super().__init__(label="Apply", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CompileLayoutView):
            return
        if not view.session.selected_ids:
            await interaction.response.send_message("Select at least one EPUB first.", ephemeral=True)
            return
        await interaction.response.send_modal(DeleteReasonModal(view.session))


class DeleteReasonModal(discord.ui.Modal, title="Delete or Restore EPUBs"):
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=500)

    def __init__(self, session: CompileSession):
        super().__init__(timeout=300)
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "This modal isn't yours.",
                ephemeral=True,
            )
            return

        if not is_session_live(self.session):
            await interaction.response.send_message(
                "This delete session expired. Run `/compile action:delete` again.",
                ephemeral=True,
            )
            return

        if not has_compile_action_permission(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or the configured special role.",
                ephemeral=True,
            )
            return

        selected_entries = [
            entry
            for entry in self.session.entries
            if entry.entry_id in self.session.selected_ids
        ]
        delete_ids = [
            entry.discord_epub_id
            for entry in selected_entries
            if not entry.is_deleted
        ]
        undelete_ids = [
            entry.discord_epub_id
            for entry in selected_entries
            if entry.is_deleted
        ]

        deleted_count = await soft_delete_epubs(
            self.session.channel_id,
            delete_ids,
            interaction.user.id,
            str(self.reason or ""),
        )
        restored_count = await undelete_epubs(
            self.session.channel_id,
            undelete_ids,
        )

        await interaction.response.send_message(
            f"Soft-deleted {deleted_count} EPUB(s). Restored {restored_count} EPUB(s).",
            ephemeral=True,
        )


class ReorderApplyButton(discord.ui.Button["CompileLayoutView"]):
    def __init__(self):
        super().__init__(label="Apply", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if not isinstance(view, CompileLayoutView):
            return

        session = view.session
        moving_entry = next(
            (entry for entry in session.entries if entry.entry_id == session.reorder_moving_id),
            None,
        )

        if moving_entry is None:
            await interaction.response.send_message(
                "Select an EPUB to reorder first.",
                ephemeral=True,
            )
            return

        ordered = [
            entry
            for entry in sorted(session.entries, key=lambda e: e.effective_order)
            if entry.entry_id != moving_entry.entry_id
        ]
        selected = [
            entry for entry in ordered if entry.entry_id in session.placement_ids
        ]

        if len(selected) not in {1, 2}:
            await interaction.response.send_message(
                "Select either the first/last EPUB, or two adjacent EPUBs to move between.",
                ephemeral=True,
            )
            return

        target_id: Optional[int]

        if len(selected) == 1:
            only = selected[0]

            if ordered and only.entry_id == ordered[0].entry_id:
                target_id = None
            elif ordered and only.entry_id == ordered[-1].entry_id:
                target_id = only.discord_epub_id
            else:
                await interaction.response.send_message(
                    "A single placement selection is only valid for the first or last position.",
                    ephemeral=True,
                )
                return
        else:
            first, second = sorted(selected, key=lambda e: e.effective_order)
            first_index = ordered.index(first)

            if first_index + 1 >= len(ordered) or ordered[first_index + 1] is not second:
                await interaction.response.send_message(
                    "Select two EPUBs that are directly next to each other.",
                    ephemeral=True,
                )
                return

            target_id = first.discord_epub_id

        await move_epub_after(
            session.channel_id,
            moving_entry.discord_epub_id,
            target_id,
        )
        await interaction.response.send_message("Reorder saved.", ephemeral=True)


