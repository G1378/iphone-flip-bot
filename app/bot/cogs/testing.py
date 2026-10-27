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
from app.services import config_service, cosmetics, phones as phones_service, testing as testing_service

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
                 on_finish: Optional[Callable[[], Awaitable[None]]] = None, record_condition: bool = True,
                 battery_retest: bool = False):
        super().__init__(timeout=900)
        self.record_condition = record_condition  # False for a re-test of failed tests only
        self.battery_retest = battery_retest      # a re-test that includes the battery: ask for the % again
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
        phone_now = await phones_service.get_phone(session, self.phone_internal_id)
        cond = [f"Frame **{phone_now.frame_grade or '—'}** · Back glass **{phone_now.back_glass_grade or '—'}**",
                "Battery " + (f"**{phone_now.battery_health}%**" if phone_now.battery_health is not None
                              else ("**n/a**" if phone_now.battery_health_na else "**not recorded**"))]
        e.add_field(name="📱 Condition", value=" · ".join(cond), inline=False)
        if report.condition_missing:
            e.add_field(name="⚠️ Still to record", value=", ".join(report.condition_missing) + " - run `/condition`", inline=False)
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
        self.stop()
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, self.phone_internal_id)
            stages = condition_stages(phone, full=self.record_condition) if phone else []
            if self.battery_retest and phone and "battery" not in stages:
                stages.append("battery")
        if stages:
            view = ConditionView(self.invoker_id, self.phone_internal_id, self.phone_title, stages, self._summary_and_hook)
            view.message = self.message
            await interaction.response.edit_message(embed=view.embed(), view=view)
            return
        await interaction.response.edit_message(embed=await self._summary_and_hook(), view=None)

    async def _summary_and_hook(self) -> discord.Embed:
        async with session_scope() as session:
            embed = await self.summary_embed(session)
        if self.on_finish is not None:
            try:
                await self.on_finish()
            except Exception:
                logger.exception("Walkthrough on_finish hook failed")
        return embed

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


# ---------------------------------------------------------------------- condition steps
GRADE_STYLE = {"A*": discord.ButtonStyle.success, "A": discord.ButtonStyle.success, "B": discord.ButtonStyle.primary,
               "C": discord.ButtonStyle.secondary, "D": discord.ButtonStyle.danger}


def condition_stages(phone, full: bool = True) -> list[str]:
    """Which condition steps to ask: all of them after a full walkthrough, otherwise only what's missing."""
    if phone is None or phone.current_status in testing_service.CLOSED_STATUSES:
        return []
    stages = []
    if full or phone.frame_grade not in cosmetics.GRADES:
        stages.append("frame")
    if full or phone.back_glass_grade not in cosmetics.GRADES:
        stages.append("back glass")
    if full or (phone.battery_health is None and not phone.battery_health_na):
        stages.append("battery")
    return stages


class BatteryModal(discord.ui.Modal, title="Battery health"):
    percent = discord.ui.TextInput(label="Battery health % (Settings > Battery)", placeholder="87", max_length=4)

    def __init__(self, view: "ConditionView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pct = int(self.percent.value.strip().rstrip("%"))
        except ValueError:
            await interaction.response.send_message(embed=error_embed("Battery health must be a whole number, e.g. 87."), ephemeral=True)
            return
        await self.view.record_battery(interaction, percent=pct)


class GradeButton(discord.ui.Button):
    def __init__(self, grade: str):
        super().__init__(label=grade, style=GRADE_STYLE[grade])
        self.grade = grade

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.record_grade(interaction, self.grade)


class ConditionView(discord.ui.View):
    """The last part of testing: grade the frame, grade the back glass, record battery health.

    Each answer is saved straight away. A D raises a repair for that part; a C marks a replacement
    if one is in stock (and never orders one)."""

    def __init__(self, invoker_id: int, phone_internal_id: str, phone_title: str, stages: list[str],
                 done: Callable[[], Awaitable[discord.Embed]]):
        super().__init__(timeout=900)
        self.invoker_id, self.phone_internal_id, self.phone_title = invoker_id, phone_internal_id, phone_title
        self.stages, self.done = stages, done
        self.index = 0
        self.notes: list[str] = []
        self.low: Optional[int] = None      # a battery % under the minimum, waiting for override / continue
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self._build_buttons()

    @property
    def stage(self) -> str:
        return self.stages[self.index]

    def _build_buttons(self) -> None:
        self.clear_items()
        if self.low is not None:
            keep = discord.ui.Button(label="Override: keep battery", style=discord.ButtonStyle.primary, emoji="🔋")
            keep.callback = self._override_low
            cont = discord.ui.Button(label=f"Continue: order battery", style=discord.ButtonStyle.danger, emoji="❌")
            cont.callback = self._accept_low
            self.add_item(keep)
            self.add_item(cont)
            return
        if self.stage == "battery":
            na = discord.ui.Button(label="Not applicable", style=discord.ButtonStyle.secondary, emoji="➖")
            na.callback = lambda i: self.record_battery(i, not_applicable=True)
            enter = discord.ui.Button(label="Enter %", style=discord.ButtonStyle.primary, emoji="🔋")
            enter.callback = lambda i: i.response.send_modal(BatteryModal(self))
            self.add_item(enter)
            self.add_item(na)
        else:
            for g in cosmetics.GRADES:
                self.add_item(GradeButton(g))
        skip = discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭", row=1)
        skip.callback = self._skip
        self.add_item(skip)

    def embed(self) -> discord.Embed:
        what = {"frame": "Frame", "back glass": "Back glass", "battery": "Battery health"}[self.stage]
        e = discord.Embed(title=f"📱 Condition - {self.phone_title}", colour=discord.Colour.blue(),
                          description=f"## {what}\nStep **{self.index + 1}** of **{len(self.stages)}**")
        if self.low is not None:
            e.add_field(name=f"⚠️ {self.low}% is under {cosmetics.BATTERY_MIN_HEALTH}%",
                        value="That counts as a **failed battery test**: a Battery is needed (ordered if you have none in stock). "
                              "Press **Override** to keep this battery instead.", inline=False)
        elif self.stage == "battery":
            e.add_field(name="Record", value="The % from Settings > Battery > Battery Health. "
                                             f"Under **{cosmetics.BATTERY_MIN_HEALTH}%** fails the battery test (a Battery is needed); "
                                             "you can override that on the next screen. If the phone doesn't show a % "
                                             "(e.g. a non-genuine battery) press **Not applicable**.", inline=False)
        else:
            e.add_field(name="Grades", value="\n".join(f"**{g}** {cosmetics.GRADE_DESCRIPTIONS[g]}" for g in cosmetics.GRADES), inline=False)
            e.add_field(name="What happens", value="**D** must be replaced before sale (added to the repair). "
                                                    "**C** is sellable as is; a matching one is reserved if you have it in stock, "
                                                    "but nothing is ever ordered.", inline=False)
        if self.notes:
            e.add_field(name="So far", value="\n".join(self.notes)[-1000:], inline=False)
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This isn't your test run.", ephemeral=True)
            return False
        return True

    async def _next(self, interaction: discord.Interaction) -> None:
        self.index += 1
        if self.index < len(self.stages):
            self._build_buttons()
            await interaction.response.edit_message(embed=self.embed(), view=self)
            return
        self.stop()
        await interaction.response.edit_message(embed=await self.done(), view=None)

    async def _skip(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            self.notes.append(f"⏭ {self.stage} skipped")
            await self._next(interaction)

    async def record_grade(self, interaction: discord.Interaction, grade: str) -> None:
        async with self._lock:
            role = cosmetics.FRAME if self.stage == "frame" else cosmetics.GLASS
            async with session_scope() as session:
                phone = await phones_service.get_phone(session, self.phone_internal_id)
                out = await cosmetics.set_grade(session, phone, role, grade, interaction.user.id)
            self.notes.append("✅ " + out.summary())
            await self._next(interaction)

    async def record_battery(self, interaction: discord.Interaction, *, percent: Optional[int] = None,
                             not_applicable: bool = False) -> None:
        async with self._lock:
            async with session_scope() as session:
                phone = await phones_service.get_phone(session, self.phone_internal_id)
                try:
                    out = await testing_service.apply_battery_health(
                        session, phone, interaction.user.id, percent=percent, not_applicable=not_applicable)
                except cosmetics.CosmeticError as e:
                    await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                    return
            if out.failed:   # under the minimum: let them override before moving on
                self.low = percent
                self._build_buttons()
                await interaction.response.edit_message(embed=self.embed(), view=self)
                return
            self.notes.append("✅ Battery health " + ("not applicable" if not_applicable else f"**{percent}%**"))
            await self._next(interaction)

    async def _override_low(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            pct = self.low
            async with session_scope() as session:
                phone = await phones_service.get_phone(session, self.phone_internal_id)
                await testing_service.apply_battery_health(session, phone, interaction.user.id, percent=pct, override=True)
            self.low = None
            self.notes.append(f"✅ Battery health **{pct}%** - under {cosmetics.BATTERY_MIN_HEALTH}% but **overridden**, battery kept")
            await self._next(interaction)

    async def _accept_low(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            pct, self.low = self.low, None
            self.notes.append(f"❌ Battery health **{pct}%** - under {cosmetics.BATTERY_MIN_HEALTH}%: **Battery needed**")
            await self._next(interaction)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="⌛ Condition timed out - what you recorded is saved. Run `/condition` to finish.", view=None)
            except discord.HTTPException:
                logger.debug("Could not edit timed-out condition message", exc_info=True)


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

    # The battery has no PASS/FAIL question of its own: the detailed battery health step (under 80% = fail,
    # with an override button) records that test's result.
    battery_retest = any(d.code == testing_service.BATTERY_TEST_CODE for d, _ in summary)
    summary = [(d, r) for d, r in summary if d.code != testing_service.BATTERY_TEST_CODE]
    steps = [
        Step(
            test_definition_id=d.id,
            name=d.name,
            part_type_name=d.part_type.name if d.part_type else None,
            previous=r.result.value if r else "NOT_TESTED",
        )
        for d, r in summary
    ]
    view = WalkthroughView(interaction.user.id, internal_id.upper(), title, steps, start_status, on_finish=on_finish,
                           record_condition=only_definition_ids is None, battery_retest=battery_retest)
    if not steps:   # only the battery was asked for (e.g. a job channel re-test of a failed battery)
        cview = ConditionView(interaction.user.id, internal_id.upper(), title, ["battery"], view._summary_and_hook)
        await interaction.response.send_message(embed=cview.embed(), view=cview)
        cview.message = await interaction.original_response()
        return
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

    # ------------------------------------------------------------------ condition
    @app_commands.command(name="condition", description="Record frame / back glass grade and battery health for a phone")
    @app_commands.describe(
        frame="Frame (housing) grade", back_glass="Back glass grade", battery="Battery health % (leave blank if unchanged)",
        battery_not_applicable="Phone shows no battery health (e.g. non-genuine battery)",
        battery_override="Keep the battery even though it is under 80% (otherwise a Battery is needed)")
    @app_commands.choices(
        frame=[app_commands.Choice(name=f"{g} - {cosmetics.GRADE_DESCRIPTIONS[g]}"[:100], value=g) for g in cosmetics.GRADES],
        back_glass=[app_commands.Choice(name=f"{g} - {cosmetics.GRADE_DESCRIPTIONS[g]}"[:100], value=g) for g in cosmetics.GRADES])
    @require_authorized()
    async def condition(self, interaction: discord.Interaction, internal_id: str,
                        frame: Optional[app_commands.Choice[str]] = None,
                        back_glass: Optional[app_commands.Choice[str]] = None,
                        battery: Optional[int] = None, battery_not_applicable: bool = False,
                        battery_override: bool = False):
        lines: list[str] = []
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            try:
                for role, choice in ((cosmetics.FRAME, frame), (cosmetics.GLASS, back_glass)):
                    if choice is not None:
                        lines.append("• " + (await cosmetics.set_grade(session, phone, role, choice.value, interaction.user.id)).summary())
                if battery is not None or battery_not_applicable:
                    b = await testing_service.apply_battery_health(
                        session, phone, interaction.user.id, percent=battery, not_applicable=battery_not_applicable,
                        override=battery_override)
                    text = "not applicable" if battery_not_applicable else f"**{battery}%**"
                    if b.failed:
                        text += f" - under {cosmetics.BATTERY_MIN_HEALTH}%: **Battery needed** (use `battery_override` to keep it)"
                    elif b.overridden:
                        text += f" - under {cosmetics.BATTERY_MIN_HEALTH}% but overridden, battery kept"
                    lines.append("• Battery health " + text)
            except cosmetics.CosmeticError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            missing = cosmetics.missing_condition(phone)
            title = f"{phone.internal_id} {phone.display_name()}"
            snapshot = (phone.frame_grade or "—", phone.back_glass_grade or "—", phone.battery_health, phone.battery_health_na)
        if not lines:
            lines.append(f"Frame **{snapshot[0]}** · Back glass **{snapshot[1]}** · Battery "
                         + (f"**{snapshot[2]}%**" if snapshot[2] is not None else ("**n/a**" if snapshot[3] else "**not recorded**")))
        if missing:
            lines.append(f"Still to record: {', '.join(missing)}")
        await interaction.response.send_message(embed=success_embed(f"Condition - {title}", "\n".join(lines)))

    @app_commands.command(name="condition-keep", description="Sell a grade C part as it is: release the replacement that was set aside")
    @app_commands.choices(part=[app_commands.Choice(name="Frame", value="frame"), app_commands.Choice(name="Back glass", value="back glass")])
    @require_authorized()
    async def condition_keep(self, interaction: discord.Interaction, internal_id: str, part: app_commands.Choice[str]):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            try:
                freed = await cosmetics.waive_replacement(session, phone, part.value, interaction.user.id)
            except cosmetics.CosmeticError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
        msg = f"`{freed.internal_id}` is back in stock." if freed else "There was no replacement set aside."
        await interaction.response.send_message(embed=success_embed(f"{part.name} stays as graded on {internal_id}", msg))

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
