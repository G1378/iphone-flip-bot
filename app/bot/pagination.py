from __future__ import annotations

from typing import Callable, Sequence

import discord


class Paginator(discord.ui.View):
    """Generic Previous/Next paginator. `render(items_on_page, page_index,
    total_pages) -> discord.Embed` builds the embed for a given page."""

    def __init__(self, items: Sequence, per_page: int, render: Callable[[Sequence, int, int], discord.Embed],
                 timeout: float = 180):
        super().__init__(timeout=timeout)
        self.items = items
        self.per_page = per_page
        self.render = render
        self.page = 0
        self.total_pages = max(1, (len(items) + per_page - 1) // per_page)
        self._update_button_state()

    def _update_button_state(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def current_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        page_items = self.items[start:start + self.per_page]
        return self.render(page_items, self.page, self.total_pages)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_button_state()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_button_state()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


async def send_paginated(interaction: discord.Interaction, items: Sequence, per_page: int,
                          render: Callable[[Sequence, int, int], discord.Embed], ephemeral: bool = False) -> None:
    if not items:
        await interaction.response.send_message(embed=render([], 0, 1), ephemeral=ephemeral)
        return
    view = Paginator(items, per_page, render)
    await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=ephemeral)
