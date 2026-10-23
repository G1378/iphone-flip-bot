from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, success_embed
from app.db import session_scope
from app.models.enums import TestOutcome
from app.services import config_service, phones as phones_service, testing as testing_service

logger = logging.getLogger(__name__)

RESULT_EMOJI = {"PASS": "✅", "FAIL": "❌", "NOT_TESTED": "⬜", "NOT_APPLICABLE": "➖"}


@dataclass
class Step:
    """One test in the walkthrough, snapshotted when `/testing` starts."""
    test_definition_id: int
    name: str
    part_type_name: Optional[str]   # what gets flagged if it fails
    previous: str                   # last recorded outcome ("NOT_TESTED" if none)


def _failure_note(out: testing_service.ApplyOutcome, step: Step) -> str:
    """One-line description of what a FAIL just flagged."""
    bits = [f"**{step.name}** failed"]
    bits.append(f"needs **{out.part_type.name}**" if out.part_type else "no part mapped - needs inspection")
    return " - ".join(bits)


class WalkthroughView(discord.ui.View):
    """Steps through every test on the checklist, one at a time.

    Each answer is saved straight away (so stopping part-way keeps progress).
    A FAIL immediately flags the phone and the part(s) needed to fix it.
    """

    def __init__(self, invoker_id: int, phone_internal_id: str, phone_title: str,
                 steps: list[Step], start_status: str,
                 on_finish: Optional[Callable[[], Awaitable[None]]] = None):
        super().__init__(timeout=900)
        self.on_finish = on_finish  # e.g. a job channel redrawing its panel
        self.invoker_id = invoker_id
        self.phone_internal_id = phone_internal_id
        self.phone_title = phone_title
        self.steps = steps
        self.start_status = start_status
        self.index = 0
        self.counts = {"PASS": 0, "FAIL": 0, "NOT_APPLICABLE": 0, "SKIPPED": 0}
        self.last_note: Optional[str] = None
        self.was_listed = False
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

    # -- rendering -----------------------------------------------------
    def step_embed(self) -> discord.Embed:
        step = self.steps[self.index]
        e = discord.Embed(
            title=f"🧪 Testing - {self.phone_title}",
            description=f"## {step.name}\nTest **{self.index + 1}** of **{len(self.steps)}**",
            colour=discord.Colour.blue(),
        )
        if step.previous != "NOT_TESTED":
            e.add_field(name="Last result", value=f"{RESULT_EMOJI[step.previous]} {step.previous}", inline=True)
        e.add_field(
            name="If it fails",
            value=f"Flags **{step.part_type_name}** as needed" if step.part_type_name else "Flags the phone for inspection (no part mapped)",
            inline=True,
        )
        if self.last_note:
            e.add_field(name="Previous test", value=self.last_note, inline=False)
        c = self.counts
        e.set_footer(text=f"✅ {c['PASS']}  ❌ {c['FAIL']}  ➖ {c['NOT_APPLICABLE']}  ⏭ {c['SKIPPED']}")
        return e

    async def summary_embed(self, session) -> discord.Embed:
        phone = await phones_service.get_phone(session, self.phone_internal_id)
        report = await testing_service.finish_walkthrough(session, phone, self.invoker_id)

        failed = bool(report.failed)
        e = discord.Embed(
            title=f"{'❌' if failed else '✅'} Testing {'finished - faults found' if failed else 'finished'} - {self.phone_title}",
            colour=discord.Colour.red() if failed else discord.Colour.green(),
        )
        e.add_field(
            name="Results",
            value=f"✅ {report.passed} passed · ❌ {len(report.failed)} failed · ➖ {report.not_applicable} n/a · ⬜ {report.not_tested} not tested",
            inline=False,
        )
        if failed:
            e.add_field(
                name="Failed tests",
                value="\n".join(
                    f"• {d.name} → needs **{p.name}**" if p else f"• {d.name} → no part mapped, needs inspection"
                    for d, p in report.failed
                )[:1024],
                inline=False,
            )
        if report.parts_needed:
            e.add_field(name="🔧 Parts needed", value=", ".join(report.parts_needed)[:1024], inline=False)
        if report.status != self.start_status:
            e.add_field(name="Status", value=f"{self.start_status} → **{report.status}**", inline=True)
        else:
            e.add_field(name="Status", value=report.status, inline=True)
        if report.repair_id:
            e.add_field(name="Repair", value=f"`{report.repair_id}` - run `/repair {self.phone_internal_id}` to assign parts", inline=False)
        if self.was_listed:
            e.add_field(name="⚠️ Listing", value="This phone had a live listing - end or update it before it sells.", inline=False)
        if report.not_tested:
            e.set_footer(text="Skipped tests stay NOT_TESTED - run /testing again to finish them.")
        return e

    # -- flow ----------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This test run isn't yours.", ephemeral=True)
            return False
        return True

    async def _advance(self, interaction: discord.Interaction) -> None:
        if self.index < len(self.steps):
            await interaction.response.edit_message(embed=self.step_embed(), view=self)
            return
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            embed = await self.summary_embed(session)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()
        if self.on_finish is not None:
            try:
                await self.on_finish()
            except Exception:
                logger.exception("Walkthrough on_finish hook failed")

    async def _answer(self, interaction: discord.Interaction, outcome: TestOutcome) -> None:
        async with self._lock:
            if self.index >= len(self.steps):
                await interaction.response.defer()
                return
            step = self.steps[self.index]
            async with session_scope() as session:
                phone = await phones_service.get_phone(session, self.phone_internal_id)
                definitions = await config_service.list_test_definitions(session)
                definition = next((d for d in definitions if d.id == step.test_definition_id), None)
                if not phone or not definition:
                    await interaction.response.send_message(
                        embed=error_embed("Phone or test definition no longer exists."), ephemeral=True)
                    return
                out = await testing_service.record_and_apply(session, phone, definition, outcome, interaction.user.id)

            self.counts[outcome.value] += 1
            if outcome == TestOutcome.FAIL:
                self.last_note = "❌ " + _failure_note(out, step)
                self.was_listed = self.was_listed or out.was_listed
            else:
                self.last_note = f"{RESULT_EMOJI[outcome.value]} **{step.name}** {outcome.value.replace('_', ' ').lower()}"
            self.index += 1
            await self._advance(interaction)

    # -- buttons -------------------------------------------------------
    @discord.ui.button(label="PASS", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def pass_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, TestOutcome.PASS)

    @discord.ui.button(label="FAIL", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def fail_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, TestOutcome.FAIL)

    @discord.ui.button(label="N/A", style=discord.ButtonStyle.secondary, emoji="➖", row=0)
    async def na_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, TestOutcome.NOT_APPLICABLE)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭", row=1)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            if self.index >= len(self.steps):
                await interaction.response.defer()
                return
            self.counts["SKIPPED"] += 1
            self.last_note = f"⏭ **{self.steps[self.index].name}** skipped"
            self.index += 1
            await self._advance(interaction)

    @discord.ui.button(label="Finish now", style=discord.ButtonStyle.primary, emoji="🏁", row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            await self._finish(interaction)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⌛ Testing timed out - everything recorded so far is saved. Run `/testing` again to continue.",
                view=None,
            )
        except discord.HTTPException:
            logger.debug("Could not edit timed-out testing message", exc_info=True)


async def begin_walkthrough(interaction: discord.Interaction, internal_id: str, *,
                            only_definition_ids: Optional[set[int]] = None,
                            on_finish: Optional[Callable[[], Awaitable[None]]] = None) -> None:
    """Start the PASS / FAIL walkthrough in the channel the interaction came from.

    Shared by `/testing` and the job channel's buttons. `only_definition_ids`
    limits it to specific tests (the job channel's Re-test uses the failed ones)."""
    async with session_scope() as session:
        phone = await phones_service.get_phone(session, internal_id)
        if not phone:
            await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
            return
        summary = await testing_service.summary(session, phone)
        title = f"{phone.internal_id} {phone.display_name()}"
        start_status = phone.current_status

    if only_definition_ids is not None:
        summary = [(d, r) for d, r in summary if d.id in only_definition_ids]
    if not summary:
        await interaction.response.send_message(embed=error_embed("No test checklist items configured. Ask an admin to run `/config tests add`."), ephemeral=True)
        return

    steps = [
        Step(
            test_definition_id=d.id,
            name=d.name,
            part_type_name=d.part_type.name if d.part_type else None,
            previous=r.result.value if r else "NOT_TESTED",
        )
        for d, r in summary
    ]
    view = WalkthroughView(interaction.user.id, internal_id.upper(), title, steps, start_status, on_finish=on_finish)
    await interaction.response.send_message(embed=view.step_embed(), view=view)
    view.message = await interaction.original_response()


class TestingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="testing", description="Run through every test for a phone, one by one")
    @require_authorized()
    async def testing(self, interaction: discord.Interaction, internal_id: str):
        await begin_walkthrough(interaction, internal_id)

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
            outcome = TestOutcome(result.value)
            out = await testing_service.record_and_apply(session, phone, definition, outcome, interaction.user.id, notes=notes)
            if outcome == TestOutcome.PASS and phone.current_status in ("PURCHASED", "AWAITING_TEST"):
                await phones_service.set_status(session, phone, "TESTED", interaction.user.id)

        detail = ""
        if out.flagged:
            detail = f"Needs **{out.part_type.name}**." if out.part_type else "No part mapped - needs inspection."
            if out.status_changed:
                detail += f" Status: {out.previous_status} → {out.new_status}."
            if out.was_listed:
                detail += " ⚠️ This phone had a live listing."
        elif out.fault_resolved:
            detail = "Earlier failure cleared."
        await interaction.response.send_message(embed=success_embed(f"{definition.name} = {result.value} recorded for {internal_id}", detail))

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
