"""Buttons: the results pager on `/news`, and the confirm dialog on `/newsbot run-now`.

Both views share one worry: a button click is its own interaction, and
Discord will happily let anyone in the channel click a button someone
else's ephemeral message put in front of them. `is_command_owner` is the
one-line check both views build on, pulled out on its own so it can be
tested without a fake `discord.Interaction` -- it's just "does this id
match that id", and testing it as anything fancier would be testing
discord.py, not us.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord


def is_command_owner(user_id: int, owner_id: int) -> bool:
    """True iff `user_id` is who originally ran the command that produced this view."""
    return user_id == owner_id


_OWNER_ONLY_MESSAGE = "Only the requester can do this."


class PagerView(discord.ui.View):
    """Prev/Next buttons over a paged story list.

    `render_page` does the actual DB query and embed rendering for a given
    page number (through `asyncio.to_thread` on the caller's side, since
    it's a coroutine) -- this view only owns the paging state and the
    owner check. A 600s timeout matches the plan; after that the buttons
    just stop responding rather than erroring, which is fine for a result
    list nobody's still reading ten minutes later.
    """

    def __init__(
        self,
        owner_id: int,
        *,
        render_page: Callable[[int], Awaitable[discord.Embed]],
        total_pages: int,
        page: int = 1,
    ) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self._render_page = render_page
        self.total_pages = max(total_pages, 1)
        self.page = page
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_command_owner(interaction.user.id, self.owner_id):
            await interaction.response.send_message(_OWNER_ONLY_MESSAGE, ephemeral=True)
            return False
        return True

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.total_pages

    async def _turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page += delta
        self._sync_buttons()
        embed = await self._render_page(self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn_page(interaction, 1)


class ConfirmView(discord.ui.View):
    """A yes/no dialog. `/newsbot run-now` uses it to double-check a re-post.

    `value` starts `None` (nobody's answered yet), then becomes `True` or
    `False` once a button's clicked; the caller does `await view.wait()`
    and then reads `value`. `None` after `wait()` returns means the
    600s -- sorry, this one's 60s, a confirmation nobody's answering in a
    minute isn't getting answered -- timeout hit instead.
    """

    def __init__(self, owner_id: int, *, timeout: float = 60) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_command_owner(interaction.user.id, self.owner_id):
            await interaction.response.send_message(_OWNER_ONLY_MESSAGE, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post again", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


__all__ = ["ConfirmView", "PagerView", "is_command_owner"]
