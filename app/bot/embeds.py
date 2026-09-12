from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

import discord

BRAND_COLOUR = discord.Colour.blue()
GOOD_COLOUR = discord.Colour.green()
WARN_COLOUR = discord.Colour.gold()
BAD_COLOUR = discord.Colour.red()


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="⚠️ Error", description=message, colour=BAD_COLOUR)


def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description, colour=GOOD_COLOUR)


def phone_embed(phone, faults=None, test_summary=None) -> discord.Embed:
    e = discord.Embed(
        title=f"{phone.internal_id} — {phone.display_name()}",
        colour=BRAND_COLOUR,
    )
    e.add_field(name="Status", value=phone.current_status, inline=True)
    e.add_field(name="Acquisition", value=phone.acquisition_type.value, inline=True)
    e.add_field(name="Location", value=phone.location.code if phone.location else "—", inline=True)
    e.add_field(name="IMEI", value=phone.imei or "—", inline=True)
    e.add_field(name="Serial", value=phone.serial_number or "—", inline=True)
    e.add_field(name="Lock status", value=phone.lock_status.value, inline=True)
    e.add_field(name="Battery health", value=f"{phone.battery_health}%" if phone.battery_health else "—", inline=True)
    e.add_field(name="Purchase price", value=f"£{phone.purchase_price}", inline=True)
    e.add_field(name="Purchase date", value=str(phone.purchase_date or "—"), inline=True)
    if phone.seller_source:
        e.add_field(name="Source", value=phone.seller_source, inline=True)
    if faults:
        open_faults = [f.description for f in faults if f.status.value == "OPEN"]
        if open_faults:
            e.add_field(name="Open faults", value="\n".join(f"• {f}" for f in open_faults), inline=False)
    if test_summary:
        lines = []
        for definition, result in test_summary:
            symbol = {"PASS": "✅", "FAIL": "❌", "NOT_APPLICABLE": "➖"}.get(
                result.result.value if result else "NOT_TESTED", "⬜"
            )
            lines.append(f"{symbol} {definition.name}")
        if lines:
            e.add_field(name="Testing", value="\n".join(lines[:15]), inline=False)
    if phone.notes:
        e.add_field(name="Notes", value=phone.notes[:500], inline=False)
    return e


def donor_embed(donor, recovered_count: int = 0, discarded_count: int = 0) -> discord.Embed:
    e = discord.Embed(title=f"{donor.internal_id} — {donor.display_name()}", colour=BRAND_COLOUR)
    e.add_field(name="Status", value=donor.current_status, inline=True)
    e.add_field(name="Purchase price", value=f"£{donor.purchase_price}", inline=True)
    e.add_field(name="Purchase date", value=str(donor.purchase_date or "—"), inline=True)
    if donor.fault_description:
        e.add_field(name="Fault", value=donor.fault_description, inline=False)
    if donor.current_status == "TORN_DOWN":
        e.add_field(name="Parts recovered", value=str(recovered_count), inline=True)
        e.add_field(name="Parts discarded", value=str(discarded_count), inline=True)
    return e


def part_embed(part) -> discord.Embed:
    e = discord.Embed(title=f"{part.internal_id} — {part.part_type.name}", colour=BRAND_COLOUR)
    e.add_field(name="Status", value=part.status, inline=True)
    e.add_field(name="Source", value=part.source_type.value, inline=True)
    if part.source_donor:
        e.add_field(name="Donor", value=part.source_donor.internal_id, inline=True)
    e.add_field(name="Cost", value=f"£{part.cost}", inline=True)
    e.add_field(name="Grade", value=part.grade_code or "—", inline=True)
    e.add_field(name="Test result", value=part.testing_status.value, inline=True)
    e.add_field(name="Location", value=part.location.code if part.location else "—", inline=True)
    if part.installed_phone_id:
        e.add_field(name="Installed in", value=str(part.installed_phone_id), inline=True)
    if part.notes:
        e.add_field(name="Notes", value=part.notes[:400], inline=False)
    return e


def repair_plan_embed(phone, plan) -> discord.Embed:
    e = discord.Embed(title=f"🔧 Repair plan — {phone.internal_id} {phone.display_name()}", colour=BRAND_COLOUR)
    if not plan:
        e.description = "No outstanding parts required — this repair can be completed."
        return e
    total = Decimal("0")
    for item in plan:
        rp = item.repair_part
        if item.candidates:
            best = item.candidates[0]
            lines = []
            for c in item.candidates[:3]:
                donor_bit = f" · from {c.source_donor.internal_id}" if c.source_donor else ""
                reserved_bit = " · **RESERVED**" if c.status == "RESERVED" else ""
                lines.append(
                    f"`{c.internal_id}` £{c.cost} · {c.testing_status.value} · "
                    f"{c.location.code if c.location else 'no location'}{donor_bit}{reserved_bit}"
                )
            e.add_field(
                name=f"{rp.required_part_type.name} × {rp.quantity} ({len(item.candidates)} candidate(s))",
                value="\n".join(lines), inline=False,
            )
            total += best.cost
        else:
            e.add_field(
                name=f"{rp.required_part_type.name} × {rp.quantity}",
                value="⚠️ No matching AVAILABLE part in stock — consider `/order`.",
                inline=False,
            )
    e.add_field(name="Estimated parts cost (best candidates)", value=f"£{total}", inline=False)
    return e


def analysis_embed(phone, analysis) -> discord.Embed:
    icon = {"GOOD": "🟢", "MARGINAL": "🟡", "NOT_WORTH": "🔴"}[analysis.recommendation]
    label = {"GOOD": "GOOD REPAIR", "MARGINAL": "MARGINAL", "NOT_WORTH": "NOT WORTH REPAIRING"}[analysis.recommendation]
    e = discord.Embed(
        title=f"{icon} {label} — {phone.internal_id} {phone.display_name()}",
        colour={"GOOD": GOOD_COLOUR, "MARGINAL": WARN_COLOUR, "NOT_WORTH": BAD_COLOUR}[analysis.recommendation],
    )
    b = analysis.breakdown
    e.add_field(name="Purchase cost", value=f"£{b.purchase_cost}", inline=True)
    e.add_field(name="Donor-allocated parts", value=f"£{b.donor_allocated_cost}", inline=True)
    e.add_field(name="Purchased parts", value=f"£{b.purchased_part_cost}", inline=True)
    e.add_field(name="Labour", value=f"£{b.repair_labour_cost}", inline=True)
    e.add_field(name="External repair", value=f"£{b.external_repair_cost}", inline=True)
    e.add_field(name="Other expenses", value=f"£{b.other_expenses + b.shipping_in_cost}", inline=True)
    e.add_field(name="True cost so far", value=f"**£{b.total}**", inline=False)
    e.add_field(name="Expected sale price", value=f"£{analysis.expected_sale_price}", inline=True)
    e.add_field(name="Est. eBay fees", value=f"£{analysis.estimated_ebay_fees}", inline=True)
    e.add_field(name="Est. postage+packaging", value=f"£{analysis.estimated_postage + analysis.estimated_packaging}", inline=True)
    e.add_field(name="Expected profit", value=f"**£{analysis.expected_profit}**", inline=True)
    e.add_field(name="ROI", value=f"{analysis.roi_pct}%", inline=True)
    e.add_field(name="Margin", value=f"{analysis.margin_pct}%", inline=True)
    e.set_footer(text=f"Thresholds: min profit £{analysis.min_profit_threshold}, min ROI {analysis.min_roi_threshold}%, target margin {analysis.target_margin_threshold}%")
    return e


def sale_notification_embed(phone, sale, breakdown) -> discord.Embed:
    e = discord.Embed(title=f"🎉 PHONE SOLD — {phone.internal_id}", colour=GOOD_COLOUR,
                       description=phone.display_name())
    e.add_field(name="Sale price", value=f"£{sale.sale_price}", inline=True)
    e.add_field(name="Purchase cost", value=f"£{breakdown.purchase_cost}", inline=True)
    e.add_field(name="Parts cost", value=f"£{breakdown.donor_allocated_cost + breakdown.purchased_part_cost}", inline=True)
    e.add_field(name="eBay fees", value=f"£{breakdown.ebay_fees}", inline=True)
    e.add_field(name="Postage", value=f"£{breakdown.shipping_out_cost}", inline=True)
    e.add_field(name="NET PROFIT", value=f"**£{breakdown.net_profit}**", inline=True)
    e.add_field(name="ROI", value=f"{breakdown.roi_pct}%", inline=True)
    if sale.ebay_order_id:
        e.add_field(name="eBay order", value=sale.ebay_order_id, inline=True)
    return e
