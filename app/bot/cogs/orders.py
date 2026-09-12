from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, success_embed
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.services import config_service
from app.services import orders as orders_service


def _parse_lines(raw: str) -> list[tuple[str, int, Decimal]]:
    """'Screen,2,35.00;Battery,1,20.00' -> [("Screen", 2, Decimal("35.00")), ...]"""
    parsed = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = [b.strip() for b in chunk.split(",")]
        if len(bits) != 3:
            raise ValueError(f"Couldn't parse line `{chunk}` — expected `PartType,Qty,UnitCost`.")
        part_type_name, qty_str, cost_str = bits
        try:
            qty = int(qty_str)
            cost = Decimal(cost_str)
        except (ValueError, InvalidOperation):
            raise ValueError(f"Couldn't parse quantity/cost on line `{chunk}`.")
        parsed.append((part_type_name, qty, cost))
    return parsed


class OrdersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="order", description="Create a purchase order for parts")
    @app_commands.describe(
        supplier="Supplier name", lines="e.g. 'Screen,2,35.00;Battery,1,20.00'",
        shipping_cost="Shipping cost for the whole order", notes="Notes (optional)",
    )
    @require_authorized()
    async def order(self, interaction: discord.Interaction, supplier: str, lines: str,
                     shipping_cost: float = 0.0, notes: Optional[str] = None):
        try:
            parsed_lines = _parse_lines(lines)
        except ValueError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return

        async with session_scope() as session:
            line_inputs = []
            for part_type_name, qty, cost in parsed_lines:
                part_type = await config_service.get_or_create_part_type(session, part_type_name)
                line_inputs.append(orders_service.OrderLineInput(part_type_id=part_type.id, quantity=qty, unit_cost=cost))

            order = await orders_service.create_order(
                session, supplier, line_inputs, interaction.user.id,
                shipping_cost=Decimal(str(shipping_cost)), notes=notes,
            )
            await orders_service.mark_ordered(session, order, interaction.user.id)

        e = discord.Embed(title=f"✅ Order created — {order.internal_id}", colour=discord.Colour.green())
        e.add_field(name="Supplier", value=supplier, inline=True)
        e.add_field(name="Lines", value="\n".join(f"{n} × {q} @ £{c}" for n, q, c in parsed_lines), inline=False)
        e.add_field(name="Total (inc. shipping)", value=f"£{order.total_cost()}", inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="orders", description="List purchase orders")
    @require_authorized()
    async def orders(self, interaction: discord.Interaction, status: Optional[str] = None):
        async with session_scope() as session:
            order_list = await orders_service.list_orders(session, status=status.upper() if status else None)
            items = [(o.internal_id, o.supplier, o.status, o.total_cost()) for o in order_list]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"📦 Orders ({len(items)} total)", colour=discord.Colour.blue())
            for internal_id, supplier, status_, total in page_items:
                e.add_field(name=f"{internal_id} — {supplier or 'Unknown supplier'}", value=f"{status_} · £{total}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="receive", description="Mark a purchase order as received and create part stock")
    @require_authorized()
    async def receive(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            order = await orders_service.get_order(session, internal_id)
            if not order:
                await interaction.response.send_message(embed=error_embed(f"No order found with ID `{internal_id}`."), ephemeral=True)
                return
            try:
                created = await orders_service.receive_order(session, order, interaction.user.id)
            except orders_service.OrderError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return

        e = discord.Embed(title=f"✅ Received — {order.internal_id}", colour=discord.Colour.green())
        e.add_field(name="Parts created", value="\n".join(f"`{p.internal_id}`" for p in created)[:1024] or "none", inline=False)
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(OrdersCog(bot))
