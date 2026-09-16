"""
Fixed enums.

Statuses that the business owner should be able to configure from Discord
(phone status, part status, repair status, order status, listing status,
donor status) are deliberately NOT Python enums - they are rows in the
`workflow_status` table (see config_models.WorkflowStatus) so they can be
renamed/added/removed without a code change or migration.

Everything in *this* file is structural/protocol-level and is safe to keep
as a fixed enum because changing it would require code changes anyway
(e.g. how eBay fees are categorised, how a part's provenance is recorded).
"""
from __future__ import annotations

import enum


class AcquisitionType(str, enum.Enum):
    RESALE = "RESALE"
    REPAIR = "REPAIR"
    DONOR = "DONOR"


class LockStatus(str, enum.Enum):
    UNLOCKED = "UNLOCKED"
    LOCKED = "LOCKED"
    UNKNOWN = "UNKNOWN"


class PartSourceType(str, enum.Enum):
    DONOR = "DONOR"
    PURCHASED = "PURCHASED"
    EXISTING_STOCK = "EXISTING_STOCK"
    OTHER = "OTHER"


class TestOutcome(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTED = "NOT_TESTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AllocationMethod(str, enum.Enum):
    MANUAL = "MANUAL"
    PRO_RATA = "PRO_RATA"


class RepairPartLineStatus(str, enum.Enum):
    REQUIRED = "REQUIRED"       # identified as needed, no part chosen yet
    RESERVED = "RESERVED"       # a specific part reserved but not installed
    INSTALLED = "INSTALLED"
    REMOVED = "REMOVED"         # installed then later removed (swap/RMA)
    CANCELLED = "CANCELLED"     # no longer needed


class FaultStatus(str, enum.Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    WONT_FIX = "WONT_FIX"


class SaleStatus(str, enum.Enum):
    PENDING_SHIPMENT = "PENDING_SHIPMENT"
    SHIPPED = "SHIPPED"
    COMPLETE = "COMPLETE"
    REFUNDED = "REFUNDED"


class SaleSource(str, enum.Enum):
    EBAY_SYNC = "EBAY_SYNC"
    MANUAL = "MANUAL"


class PriceConditionBucket(str, enum.Enum):
    USED = "USED"       # working, resale-grade (eBay 'Used' condition)
    FAULTY = "FAULTY"   # eBay 'For parts or not working'


class PriceDataSource(str, enum.Enum):
    SOLD = "SOLD"                               # eBay Marketplace Insights (actual sold comps)
    ACTIVE_LISTING_ESTIMATE = "ACTIVE_LISTING_ESTIMATE"  # eBay Browse API (asking prices - fallback)


class EntityType(str, enum.Enum):
    PHONE = "PHONE"
    DONOR = "DONOR"
    PART = "PART"
    REPAIR = "REPAIR"
    ORDER = "ORDER"
    LISTING = "LISTING"
    SALE = "SALE"
    EXPENSE = "EXPENSE"
    SYSTEM = "SYSTEM"


class InventoryEventType(str, enum.Enum):
    PHONE_PURCHASED = "PHONE_PURCHASED"
    PHONE_UPDATED = "PHONE_UPDATED"
    PHONE_STATUS_CHANGED = "PHONE_STATUS_CHANGED"
    PHONE_MOVED = "PHONE_MOVED"
    PHONE_TESTED = "PHONE_TESTED"
    PHONE_FAULT_LOGGED = "PHONE_FAULT_LOGGED"
    DONOR_PURCHASED = "DONOR_PURCHASED"
    DONOR_DISMANTLED = "DONOR_DISMANTLED"
    PART_CREATED = "PART_CREATED"
    PART_ALLOCATED_COST = "PART_ALLOCATED_COST"
    PART_RESERVED = "PART_RESERVED"
    PART_UNRESERVED = "PART_UNRESERVED"
    PART_INSTALLED = "PART_INSTALLED"
    PART_REMOVED = "PART_REMOVED"
    PART_SCRAPPED = "PART_SCRAPPED"
    PART_MOVED = "PART_MOVED"
    PART_STATUS_CHANGED = "PART_STATUS_CHANGED"
    REPAIR_CREATED = "REPAIR_CREATED"
    REPAIR_COMPLETED = "REPAIR_COMPLETED"
    REPAIR_CANCELLED = "REPAIR_CANCELLED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_RECEIVED = "ORDER_RECEIVED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    LISTING_CREATED = "LISTING_CREATED"
    LISTING_PUBLISHED = "LISTING_PUBLISHED"
    LISTING_UPDATED = "LISTING_UPDATED"
    LISTING_ENDED = "LISTING_ENDED"
    PHONE_SOLD = "PHONE_SOLD"
    PHONE_SHIPPED = "PHONE_SHIPPED"
    EXPENSE_ADDED = "EXPENSE_ADDED"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    SHEETS_SYNCED = "SHEETS_SYNCED"
    PRICE_WATCHLIST_ADDED = "PRICE_WATCHLIST_ADDED"
    PRICE_WATCHLIST_REMOVED = "PRICE_WATCHLIST_REMOVED"
    PRICE_OBSERVED = "PRICE_OBSERVED"
