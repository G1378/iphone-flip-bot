"""What a job channel's control panel shows and which buttons it offers.

A pure function of the phone's situation (the same data the ticket text comes
from), so a button only exists when the thing it does is actually possible and
relevant right now:

  * Reserve / Fitted   only for a part you really have (in stock / reserved)
  * Order              only for a part that is in stock nowhere and on no order
  * Received / Placed  only for an order that is on its way / not yet placed
  * Re-test, Complete  only once every part is fitted
  * Run tests          only while testing is outstanding

No Discord imports: the Discord layer turns an ActionSpec into a button.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.models.enums import RepairPartLineStatus
from app.services.phone_tickets import Context, Situation, spec_for


@dataclass(frozen=True)
class ActionSpec:
    act: str          # what to do (see JobsCog dispatch)
    ref: int          # what to do it to: a repair line / order / phone id
    label: str
    style: str = "secondary"   # primary | success | danger | secondary
    group: str = "tool"        # "line" actions (one per part) sit above the "tool" row(s)


@dataclass
class PanelSpec:
    stage: str
    header: str
    title: str
    body: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)

    def content_hash(self) -> str:
        raw = "\x1e".join([self.stage, self.header, self.title, self.body,
                           "|".join(f"{n}={v}" for n, v in self.fields),
                           "|".join(f"{a.act}:{a.ref}:{a.label}:{a.style}:{a.group}" for a in self.actions)])
        return hashlib.sha1(raw.encode()).hexdigest()


def _clip(text: str, n: int = 78) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def build_panel(sit: Situation, ctx: Context) -> PanelSpec:
    phone = sit.phone
    spec = spec_for(sit, ctx)
    fields = [
        ("Status", phone.current_status.replace("_", " ").title()),
        ("Location", phone.location.code if phone.location else "—"),
        ("IMEI", phone.imei or "—"),
        ("Battery", f"{phone.battery_health}%" if phone.battery_health else "—"),
        ("Cost", f"£{phone.purchase_price}"),
    ]
    return PanelSpec(
        stage=sit.stage, header=f"{phone.internal_id} · {phone.display_name()}", title=spec.title, body=spec.body,
        fields=fields, actions=actions_for(sit, ctx),
    )


def actions_for(sit: Situation, ctx: Context) -> list[ActionSpec]:
    pid = sit.phone.id
    out: list[ActionSpec] = []

    def tool(act, label, style="secondary"):
        out.append(ActionSpec(act, pid, label, style, "tool"))

    if sit.stage == "TEST":
        tool("walk", "▶️ Run the tests" if not sit.recorded else f"▶️ Continue tests ({sit.recorded}/{sit.total})", "primary")
        tool("one", "🧪 Single test")
        tool("fault", "➕ Log a fault")
        tool("edit", "✏️ Edit details")
        tool("move", "📍 Move")
        tool("skip", "⏭ Skip testing")
    elif sit.stage == "REPAIR":
        out += _line_actions(sit, ctx)
        if sit.repair is None and sit.open_faults:
            tool("plan", "🧩 Plan the repair", "primary")
        tool("fault", "➕ Log a fault")
        tool("find", "🔎 Find a part")
        tool("analyse", "📊 Is it worth it?")
        if _has_fitted(sit):
            tool("unfit", "↩️ Remove a fitted part")
    elif sit.stage == "VERIFY":
        tool("retest", "🔁 Re-test", "primary")
        tool("complete", "✅ Complete repair", "success")
        tool("fault", "➕ Log a fault")
        tool("edit", "✏️ Edit details")
        if _has_fitted(sit):
            tool("unfit", "↩️ Remove a fitted part")
    elif sit.stage in ("PREP", "LIST"):
        tool("done", "✅ Done" if sit.stage == "PREP" else "✅ Mark as listed", "success")
        tool("fault", "➕ Log a fault")
    tool("details", "📱 Details")
    return out


def _has_fitted(sit: Situation) -> bool:
    return sit.repair is not None and any(rp.status == RepairPartLineStatus.INSTALLED for rp in sit.repair.parts)


def _line_actions(sit: Situation, ctx: Context) -> list[ActionSpec]:
    """One button per part that needs something doing to it, in the order the
    work happens: fit what's reserved, reserve what's in stock, receive what's
    arrived, place what's drafted, order what's missing."""
    fit, reserve, receive, place, order, alt = [], [], [], [], [], []
    seen_orders: set[int] = set()
    for rp in sit.outstanding:
        name = rp.required_part_type.name
        units = ctx.supply.get(rp.id, [])
        if not units:
            continue
        unit = units[0]  # a line fits one part at a time
        if unit.kind == "RESERVED":
            fit.append(ActionSpec("fit", rp.id, _clip(f"🔧 Fitted: {name} ({unit.part.internal_id})"), "success", "line"))
        elif unit.kind == "IN_STOCK":
            reserve.append(ActionSpec("reserve", rp.id, _clip(f"📥 Reserve {name} ({unit.part.internal_id})"), "primary", "line"))
            if len(ctx.available.get(rp.required_part_type_id, [])) > 1:
                alt.append(ActionSpec("alt", rp.id, _clip(f"🔍 Other {name} options"), "secondary", "line"))
        elif unit.kind == "ON_ORDER":
            if unit.order.id not in seen_orders:
                seen_orders.add(unit.order.id)
                receive.append(ActionSpec("receive", unit.order.id, _clip(f"📦 Received {unit.order.internal_id} ({name})"), "success", "line"))
        elif unit.kind == "DRAFTED":
            if unit.order.id not in seen_orders:
                seen_orders.add(unit.order.id)
                place.append(ActionSpec("placed", unit.order.id, _clip(f"✅ {unit.order.internal_id} placed ({name})"), "primary", "line"))
        else:  # TO_ORDER
            order.append(ActionSpec("order", rp.id, _clip(f"🛒 Order {name}"), "primary", "line"))
    return fit + reserve + alt + receive + place + order
