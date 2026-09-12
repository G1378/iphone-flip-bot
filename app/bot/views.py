from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord


class ConfirmView(discord.ui.View):
    """[Confirm] [Cancel] - the person who invoked the interaction is the
    only one allowed to press the buttons. `on_confirm`/`on_cancel` are
    async callbacks invoked with the confirming interaction."""

    def __init__(
        self,
        invoker_id: int,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        on_cancel: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.confirm_button.label = confirm_label
        self.cancel_button.label = cancel_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who started this action can respond.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        if self.on_cancel:
            await self.on_cancel(interaction)
        else:
            await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()
