"""Dropdown wizard for buying a phone / donor: series -> model -> storage -> colour -> details.

Every choice is a dropdown filled from the catalogue, so storage and colour only ever offer what that model
actually came in. (discord.py 2.4 has no dropdowns inside modals, so the dropdown steps are a message and the
last step - the free-text details - is a modal where every piece of information has its own box.)

Anything not in the list can still be entered: each dropdown ends with an "Other (type it)" option."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Optional

import discord

from app.bot.embeds import error_embed
from app.db import session_scope
from app.services import config_service

logger = logging.getLogger(__name__)

OTHER = "__other__"
BACK_LABEL = "Back"


@dataclass(frozen=True)
class ModelInfo:
    id: int
    name: str
    series: str
    storage: tuple[str, ...]
    colours: tuple[str, ...]


def _info(m) -> ModelInfo:
    storage, colours = config_service.model_options(m)
    return ModelInfo(m.id, m.model, config_service.series_of(m), tuple(storage), tuple(colours))


async def load_models() -> list[ModelInfo]:
    async with session_scope() as session:
        return [_info(m) for m in await config_service.list_phone_models(session)]


async def resolve_model(name: str) -> ModelInfo:
    async with session_scope() as session:
        return _info(await config_service.resolve_phone_model(session, name))


def group_series(models: list[ModelInfo]) -> list[tuple[str, list[ModelInfo]]]:
    """[(series, [models])] in catalogue order (oldest first), from models already sorted."""
    groups: dict[str, list[ModelInfo]] = {}
    for m in models:
        groups.setdefault(m.series, []).append(m)
    return list(groups.items())


def parse_price(text: str) -> Decimal:
    """'250', '£250.00', '1,250' -> Decimal. Raises ValueError with a friendly message."""
    cleaned = (text or "").replace("£", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Couldn't read `{text}` as a price. Use e.g. `250.00`.")
    if value < 0:
        raise ValueError("The price can't be negative.")
    return value


def _short(name: str) -> str:
    return name.replace("iPhone ", "", 1) if name != "iPhone" else name


def series_options(models: list[ModelInfo]) -> list[discord.SelectOption]:
    """Newest series first (that's what's mostly bought), capped at Discord's 25."""
    out = []
    for series, ms in reversed(group_series(models)):
        names = " · ".join(_short(m.name) for m in ms)
        out.append(discord.SelectOption(label=series[:100], value=series, description=names[:100]))
    return out[:25]


def model_options(models: list[ModelInfo], series: str) -> list[discord.SelectOption]:
    out = []
    for m in dict(group_series(models)).get(series, []):
        bits = []
        if m.storage:
            bits.append(f"{m.storage[0]}-{m.storage[-1]}" if len(m.storage) > 1 else m.storage[0])
        if m.colours:
            bits.append(f"{len(m.colours)} colour{'s' if len(m.colours) != 1 else ''}")
        out.append(discord.SelectOption(label=m.name[:100], value=m.name, description=" · ".join(bits)[:100] or None))
    return out[:25]


def value_options(values: tuple[str, ...], current: Optional[str] = None) -> list[discord.SelectOption]:
    opts = [discord.SelectOption(label=v[:100], value=v, default=(v == current)) for v in values[:24]]
    opts.append(discord.SelectOption(label="Other (type it)", value=OTHER, emoji="✏️"))
    return opts


class TypeItModal(discord.ui.Modal):
    """A one-box modal for an option that isn't in the dropdown."""

    def __init__(self, title: str, label: str, placeholder: str,
                 on_value: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(title=title)
        self.on_value = on_value
        self.value = discord.ui.TextInput(label=label, placeholder=placeholder, required=True, max_length=40)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_value(interaction, self.value.value.strip())


class _StageSelect(discord.ui.Select):
    def __init__(self, view: "PhoneIntakeView", placeholder: str, options: list[discord.SelectOption]):
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)
        self.wizard = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.wizard.on_choice(interaction, self.values[0])


class PhoneIntakeView(discord.ui.View):
    """series -> model -> storage -> colour -> [details modal]. `build_modal(model, storage, colour, wizard)`
    makes the final modal, which calls `wizard.complete(interaction, embed)` once saved."""

    def __init__(self, invoker_id: int, heading: str, models: list[ModelInfo],
                 build_modal: Callable[[ModelInfo, Optional[str], Optional[str], "PhoneIntakeView"], discord.ui.Modal],
                 model: Optional[ModelInfo] = None):
        super().__init__(timeout=840)
        self.invoker_id, self.heading, self.models, self.build_modal = invoker_id, heading, models, build_modal
        self.started_with_model = model is not None
        self.series: Optional[str] = model.series if model else None
        self.model: Optional[ModelInfo] = model
        self.storage: Optional[str] = None
        self.colour: Optional[str] = None
        self.stage = "storage" if model else "series"
        self.origin: Optional[discord.Interaction] = None
        self.render()

    # ------------------------------------------------------------ rendering
    def render(self) -> None:
        self.clear_items()
        if self.stage == "series":
            self.add_item(_StageSelect(self, "Which generation?", series_options(self.models)))
        elif self.stage == "model":
            self.add_item(_StageSelect(self, "Which model?", model_options(self.models, self.series)))
        elif self.stage == "storage":
            self.add_item(_StageSelect(self, "How much storage?", value_options(self.model.storage, self.storage)))
        elif self.stage == "colour":
            self.add_item(_StageSelect(self, "Which colour?", value_options(self.model.colours, self.colour)))
        else:
            go = discord.ui.Button(label="Enter details", style=discord.ButtonStyle.primary, emoji="📝")
            go.callback = self.open_details
            self.add_item(go)
        if self.stage != "series" and not (self.stage == "storage" and self.started_with_model):
            back = discord.ui.Button(label=BACK_LABEL, style=discord.ButtonStyle.secondary, emoji="◀", row=4)
            back.callback = self.go_back
            self.add_item(back)

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=self.heading, colour=discord.Colour.blue())
        picked = []
        if self.model:
            picked.append(f"**Model:** {self.model.name}")
        if self.storage:
            picked.append(f"**Storage:** {self.storage}")
        if self.colour:
            picked.append(f"**Colour:** {self.colour}")
        step = {"series": "Pick the generation", "model": f"Pick the model ({self.series})",
                "storage": "Pick the storage", "colour": "Pick the colour",
                "details": "Now the details"}[self.stage]
        e.description = "\n".join(picked + ([""] if picked else []) + [f"## {step}"])
        if self.stage in ("storage", "colour") and self.model:
            opts = self.model.storage if self.stage == "storage" else self.model.colours
            if not opts:
                e.set_footer(text="No options are listed for this model - choose Other and type it.")
        return e

    # ------------------------------------------------------------ interaction
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This isn't your purchase.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_choice(self, interaction: discord.Interaction, value: str) -> None:
        if self.stage == "series":
            self.series, self.stage = value, "model"
        elif self.stage == "model":
            self.model = next(m for m in self.models if m.name == value)
            self.storage = self.colour = None
            self.stage = "storage"
        elif self.stage == "storage":
            if value == OTHER:
                await interaction.response.send_modal(TypeItModal(
                    "Storage", "Storage size", "e.g. 128GB or 1TB", self._typed_storage))
                return
            self.storage, self.stage = value, "colour"
        elif self.stage == "colour":
            if value == OTHER:
                await interaction.response.send_modal(TypeItModal(
                    "Colour", "Colour", "e.g. Midnight", self._typed_colour))
                return
            self.colour, self.stage = value, "details"
        await self._show(interaction)

    async def _typed_storage(self, interaction: discord.Interaction, text: str) -> None:
        self.storage, self.stage = config_service.normalise_storage(text), "colour"
        await self._show(interaction)

    async def _typed_colour(self, interaction: discord.Interaction, text: str) -> None:
        self.colour, self.stage = text.title() if text.islower() else text, "details"
        await self._show(interaction)

    async def go_back(self, interaction: discord.Interaction) -> None:
        self.stage = {"model": "series", "storage": "model", "colour": "storage", "details": "colour"}[self.stage]
        await self._show(interaction)

    async def open_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(self.build_modal(self.model, self.storage, self.colour, self))

    async def complete(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        """Called by the details modal once the record is saved."""
        self.stop()
        await interaction.response.edit_message(content="✅ Saved.", embed=None, view=None)
        await interaction.followup.send(embed=embed)

    async def on_timeout(self) -> None:
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(content="⌛ That took too long - run the command again.",
                                                         embed=None, view=None)
            except discord.HTTPException:
                logger.debug("Could not edit timed-out intake message", exc_info=True)


async def start_intake(interaction: discord.Interaction, *, heading: str, model_name: Optional[str], build_modal) -> None:
    """Open the wizard (privately). With a model already named it starts at storage."""
    try:
        models = await load_models()
        model = await resolve_model(model_name) if model_name else None
    except config_service.UnknownPhoneModelError as e:
        await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
        return
    view = PhoneIntakeView(interaction.user.id, heading, models, build_modal, model)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
    view.origin = interaction
