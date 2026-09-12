from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, success_embed
from app.db import session_scope
from app.models.enums import TestOutcome
from app.services import config_service, phones as phones_service, testing as testing_service

RESULT_EMOJI = {"PASS": "✅", "FAIL": "❌", "NOT_TESTED": "⬜", "NOT_APPLICABLE": "➖"}


class ResultButtons(discord.ui.View):
    """Shown after picking a test from the dropdown - one tap to record PASS/FAIL/N/A."""

    def __init__(self, invoker_id: int, phone_internal_id: str, test_definition_id: int, test_name: str):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.phone_internal_id = phone_internal_id
        self.test_definition_id = test_definition_id
        self.test_name = test_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    async def _record(self, interaction: discord.Interaction, outcome: TestOutcome):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, self.phone_internal_id)
            definitions = await config_service.list_test_definitions(session)
            definition = next((d for d in definitions if d.id == self.test_definition_id), None)
            if not phone or not definition:
                await interaction.response.send_message(embed=error_embed("Phone or test definition no longer exists."), ephemeral=True)
                return
            await testing_service.record_result(session, phone, definition, outcome, interaction.user.id)
        await interaction.response.edit_message(
            content=f"{RESULT_EMOJI[outcome.value]} **{self.test_name}** recorded as **{outcome.value}** for `{self.phone_internal_id}`.\nPick another test below or run `/testing` again.",
            view=None,
        )

    @discord.ui.button(label="PASS", style=discord.ButtonStyle.success, emoji="✅")
    async def pass_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, TestOutcome.PASS)

    @discord.ui.button(label="FAIL", style=discord.ButtonStyle.danger, emoji="❌")
    async def fail_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, TestOutcome.FAIL)

    @discord.ui.button(label="N/A", style=discord.ButtonStyle.secondary, emoji="➖")
    async def na_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, TestOutcome.NOT_APPLICABLE)


class TestSelect(discord.ui.Select):
    def __init__(self, invoker_id: int, phone_internal_id: str, options: list[discord.SelectOption], id_by_value: dict[str, int]):
        super().__init__(placeholder="Choose a test to record...", options=options, min_values=1, max_values=1)
        self.invoker_id = invoker_id
        self.phone_internal_id = phone_internal_id
        self.id_by_value = id_by_value

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This checklist isn't yours to fill in.", ephemeral=True)
            return
        test_name = self.values[0]
        test_definition_id = self.id_by_value[test_name]
        view = ResultButtons(self.invoker_id, self.phone_internal_id, test_definition_id, test_name)
        await interaction.response.send_message(f"Recording **{test_name}** for `{self.phone_internal_id}` — pick a result:", view=view, ephemeral=True)


class TestSelectView(discord.ui.View):
    def __init__(self, invoker_id: int, phone_internal_id: str, options: list[discord.SelectOption], id_by_value: dict[str, int]):
        super().__init__(timeout=300)
        self.add_item(TestSelect(invoker_id, phone_internal_id, options, id_by_value))


class TestingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="testing", description="Interactive testing checklist for a phone")
    @require_authorized()
    async def testing(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            summary = await testing_service.summary(session, phone)

        options, id_by_value = [], {}
        for definition, result in summary[:25]:
            outcome = result.result.value if result else "NOT_TESTED"
            options.append(discord.SelectOption(
                label=definition.name[:100], value=definition.name,
                description=f"Current: {outcome}", emoji=RESULT_EMOJI.get(outcome, "⬜"),
            ))
            id_by_value[definition.name] = definition.id

        if not options:
            await interaction.response.send_message(embed=error_embed("No test checklist items configured. Ask an admin to run `/config tests add`."), ephemeral=True)
            return

        view = TestSelectView(interaction.user.id, phone.internal_id, options, id_by_value)
        e = discord.Embed(title=f"🧪 Testing checklist — {phone.internal_id} {phone.display_name()}", colour=discord.Colour.blue())
        lines = [f"{RESULT_EMOJI.get(r.result.value if r else 'NOT_TESTED', '⬜')} {d.name}" for d, r in summary]
        e.description = "\n".join(lines[:25])
        await interaction.response.send_message(embed=e, view=view)

    @app_commands.command(name="test", description="Quickly record a single test result")
    @app_commands.choices(result=[
        app_commands.Choice(name="PASS", value="PASS"),
        app_commands.Choice(name="FAIL", value="FAIL"),
        app_commands.Choice(name="NOT_APPLICABLE", value="NOT_APPLICABLE"),
        app_commands.Choice(name="NOT_TESTED", value="NOT_TESTED"),
    ])
    @require_authorized()
    async def test(self, interaction: discord.Interaction, internal_id: str, test_name: str, result: app_commands.Choice[str], notes: Optional[str] = None):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            definitions = await config_service.list_test_definitions(session)
            definition = next((d for d in definitions if d.name.lower() == test_name.lower() or d.code == test_name.lower()), None)
            if not definition:
                names = ", ".join(d.name for d in definitions)
                await interaction.response.send_message(embed=error_embed(f"Unknown test `{test_name}`. Configured tests: {names}"), ephemeral=True)
                return
            await testing_service.record_result(session, phone, definition, TestOutcome(result.value), interaction.user.id, notes=notes)
            if TestOutcome(result.value) == TestOutcome.FAIL and phone.current_status in ("PURCHASED", "AWAITING_TEST"):
                await phones_service.set_status(session, phone, "REPAIR_REQUIRED", interaction.user.id, notes=f"{definition.name} failed testing")
            elif phone.current_status in ("PURCHASED", "AWAITING_TEST") and TestOutcome(result.value) == TestOutcome.PASS:
                await phones_service.set_status(session, phone, "TESTED", interaction.user.id)
        await interaction.response.send_message(embed=success_embed(f"{definition.name} = {result.value} recorded for {internal_id}"))

    @test.autocomplete("test_name")
    async def test_name_autocomplete(self, interaction: discord.Interaction, current: str):
        async with session_scope() as session:
            definitions = await config_service.list_test_definitions(session)
        return [
            app_commands.Choice(name=d.name, value=d.name)
            for d in definitions if current.lower() in d.name.lower()
        ][:25]

    @app_commands.command(name="test-result", description="Show recorded test results for a phone")
    @require_authorized()
    async def test_result(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            summary = await testing_service.summary(session, phone)
        e = discord.Embed(title=f"🧪 Test results — {phone.internal_id} {phone.display_name()}", colour=discord.Colour.blue())
        for definition, result in summary:
            outcome = result.result.value if result else "NOT_TESTED"
            value = RESULT_EMOJI.get(outcome, "⬜") + " " + outcome
            if result and result.notes:
                value += f"\n_{result.notes}_"
            e.add_field(name=definition.name, value=value, inline=True)
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(TestingCog(bot))
