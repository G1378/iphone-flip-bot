from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, success_embed
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.models.commerce_models import Expense, Sale
from app.services import config_service
from app.services import finance as finance_service
from app.services import ids as ids_service


def _period_range(period: str, today: dt.date) -> tuple[dt.date, dt.date, str]:
    if period == "daily":
        return today, today, "Today"
    if period == "weekly":
        start = today - dt.timedelta(days=today.weekday())
        return start, today, "This week"
    if period == "monthly":
        start = today.replace(day=1)
        return start, today, "This month"
    raise ValueError("period must be daily, weekly, or monthly")


class FinanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="report", description="Business performance report")
    @app_commands.choices(period=[
        app_commands.Choice(name="Daily", value="daily"),
        app_commands.Choice(name="Weekly", value="weekly"),
        app_commands.Choice(name="Monthly", value="monthly"),
    ])
    @require_authorized()
    async def report(self, interaction: discord.Interaction, period: app_commands.Choice[str]):
        today = dt.date.today()
        start, end, label = _period_range(period.value, today)

        async with session_scope() as session:
            r = await finance_service.period_report(session, start, end)
            stock = await finance_service.stock_value(session)

            from app.services.phones import list_stock
            from app.services.donors import list_donors
            from app.services.orders import parts_needed
            awaiting_parts = len(await list_stock(session, status="AWAITING_PARTS"))
            donors_awaiting = len(await list_donors(session, status="AWAITING_TEARDOWN"))
            needed = await parts_needed(session)
            currency_symbol = await config_service.get_setting(session, "currency_symbol", "£")

        e = discord.Embed(title=f"📊 {label} report ({start} to {end})", colour=discord.Colour.blue())
        e.add_field(name="Revenue", value=f"{currency_symbol}{r.revenue}", inline=True)
        e.add_field(name="Total cost", value=f"{currency_symbol}{r.total_cost}", inline=True)
        e.add_field(name="Net profit", value=f"**{currency_symbol}{r.net_profit}**", inline=True)
        e.add_field(name="ROI", value=f"{r.roi_pct}%", inline=True)
        e.add_field(name="Phones sold", value=str(r.phones_sold), inline=True)
        e.add_field(name="Phones purchased", value=str(r.phones_purchased), inline=True)
        e.add_field(name="Avg profit/phone", value=f"{currency_symbol}{r.avg_profit_per_phone}", inline=True)
        e.add_field(name="Phones awaiting parts", value=str(awaiting_parts), inline=True)
        e.add_field(name="Donors awaiting teardown", value=str(donors_awaiting), inline=True)
        if needed:
            order_needed = [n for n in needed if n.to_order_qty > 0]
            if order_needed:
                e.add_field(name="Parts to order", value="\n".join(f"{n.part_type.name} × {n.to_order_qty}" for n in order_needed[:10]), inline=False)
        e.add_field(name="Current stock value (phones+parts+donors)", value=f"{currency_symbol}{stock['total']}", inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="stock-value", description="Total value of current inventory")
    @require_authorized()
    async def stock_value(self, interaction: discord.Interaction):
        async with session_scope() as session:
            stock = await finance_service.stock_value(session)
        e = discord.Embed(title="💰 Stock value", colour=discord.Colour.blue())
        e.add_field(name="Phones", value=f"£{stock['phones']}", inline=True)
        e.add_field(name="Parts", value=f"£{stock['parts']}", inline=True)
        e.add_field(name="Donors awaiting teardown", value=f"£{stock['donors_awaiting_teardown']}", inline=True)
        e.add_field(name="Total", value=f"**£{stock['total']}**", inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="profit", description="Profit breakdown, optionally filtered by model or month")
    @require_authorized()
    async def profit(self, interaction: discord.Interaction, model: Optional[str] = None, month: Optional[str] = None):
        async with session_scope() as session:
            stmt = select(Sale).options(selectinload(Sale.phone), selectinload(Sale.profit_breakdown))
            sales = (await session.execute(stmt)).scalars().all()

        if model:
            sales = [s for s in sales if model.lower() in s.phone.display_name().lower()]
        if month:
            try:
                year, mon = (int(x) for x in month.split("-"))
                sales = [s for s in sales if s.sold_at.year == year and s.sold_at.month == mon]
            except ValueError:
                await interaction.response.send_message(embed=error_embed("month must be in YYYY-MM format, e.g. 2026-09"), ephemeral=True)
                return

        if not sales:
            await interaction.response.send_message(embed=error_embed("No matching sales found."), ephemeral=True)
            return

        total_revenue = sum((s.sale_price for s in sales), Decimal("0"))
        total_profit = sum((s.profit_breakdown.net_profit for s in sales if s.profit_breakdown), Decimal("0"))
        e = discord.Embed(title=f"💷 Profit — {model or 'all models'} {month or ''}".strip(), colour=discord.Colour.blue())
        e.add_field(name="Phones sold", value=str(len(sales)), inline=True)
        e.add_field(name="Revenue", value=f"£{total_revenue}", inline=True)
        e.add_field(name="Net profit", value=f"£{total_profit}", inline=True)
        e.add_field(name="Avg profit/phone", value=f"£{(total_profit / len(sales)):.2f}", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="sales", description="List recent sales")
    @require_authorized()
    async def sales(self, interaction: discord.Interaction):
        async with session_scope() as session:
            stmt = select(Sale).options(selectinload(Sale.phone), selectinload(Sale.profit_breakdown)).order_by(Sale.sold_at.desc())
            sales = (await session.execute(stmt)).scalars().all()
            items = [(s.internal_id, s.phone.internal_id, s.phone.display_name(), s.sale_price,
                      s.profit_breakdown.net_profit if s.profit_breakdown else None, s.sold_at) for s in sales]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"💷 Sales ({len(items)} total)", colour=discord.Colour.blue())
            for internal_id, phone_id, name, price, profit, sold_at in page_items:
                e.add_field(name=f"{internal_id} — {phone_id} {name}", value=f"£{price} · profit £{profit} · {sold_at.date()}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="expenses", description="Record or list business expenses")
    @app_commands.describe(action="add or list")
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="list", value="list"),
    ])
    @require_authorized()
    async def expenses(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                        category: Optional[str] = None, amount: Optional[float] = None,
                        description: Optional[str] = None, phone_id: Optional[str] = None):
        if action.value == "add":
            if not category or amount is None:
                await interaction.response.send_message(embed=error_embed("category and amount are required to add an expense."), ephemeral=True)
                return
            async with session_scope() as session:
                from app.services.config_service import list_expense_categories
                categories = await list_expense_categories(session)
                match = next((c for c in categories if c.name.lower() == category.lower()), None)
                if not match:
                    from app.models.config_models import ExpenseCategory
                    match = ExpenseCategory(name=category)
                    session.add(match)
                    await session.flush()

                phone = None
                if phone_id:
                    from app.services.phones import get_phone
                    phone = await get_phone(session, phone_id)

                internal_id = await ids_service.next_internal_id(session, "expense")
                expense = Expense(
                    internal_id=internal_id, category_id=match.id, amount=Decimal(str(amount)),
                    description=description, phone_id=phone.id if phone else None,
                    incurred_at=dt.date.today(), created_by_discord_id=interaction.user.id,
                )
                session.add(expense)
                await session.flush()

                from app.models.enums import InventoryEventType
                from app.services.events import log_event
                await log_event(
                    session, event_type=InventoryEventType.EXPENSE_ADDED.value, entity_type="EXPENSE",
                    entity_id=expense.internal_id, actor_discord_id=interaction.user.id,
                    new_state={"category": match.name, "amount": str(amount)},
                )
            await interaction.response.send_message(embed=success_embed(f"Expense {internal_id} recorded: £{amount} ({category})"))
        else:
            async with session_scope() as session:
                stmt = select(Expense).options(selectinload(Expense.category)).order_by(Expense.incurred_at.desc()).limit(50)
                expense_list = (await session.execute(stmt)).scalars().all()
                items = [(e.internal_id, e.category.name if e.category else "?", e.amount, e.description, e.incurred_at) for e in expense_list]

            def render(page_items, page, total_pages):
                e = discord.Embed(title=f"🧾 Expenses ({len(items)} shown)", colour=discord.Colour.blue())
                for internal_id, cat, amt, desc, incurred in page_items:
                    e.add_field(name=f"{internal_id} — {cat}", value=f"£{amt} · {incurred} · {desc or ''}", inline=False)
                e.set_footer(text=f"Page {page + 1}/{total_pages}")
                return e

            await send_paginated(interaction, items, per_page=10, render=render)


async def setup(bot: commands.Bot):
    await bot.add_cog(FinanceCog(bot))
