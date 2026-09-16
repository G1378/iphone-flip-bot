"""
Well-known codes that application logic looks for.

These are *seeded* into the configurable tables (workflow_statuses,
part_types, test_definitions, condition_grades, expense_categories) on
first run / via `/config reset-defaults`, but every one of them can be
renamed, deactivated, or added-to from Discord afterwards. Code only
depends on the `code` field (stable) never on `label` (display only).
"""
from __future__ import annotations

PHONE_STATUSES = [
    ("PURCHASED", "Purchased", False),
    ("AWAITING_TEST", "Awaiting Test", False),
    ("TESTED", "Tested", False),
    ("REPAIR_REQUIRED", "Repair Required", False),
    ("AWAITING_PARTS", "Awaiting Parts", False),
    ("READY_FOR_LISTING", "Ready For Listing", False),
    ("LISTED", "Listed", False),
    ("SOLD", "Sold", False),
    ("SHIPPED", "Shipped", False),
    ("COMPLETE", "Complete", True),
    ("SCRAPPED", "Scrapped", True),
]

DONOR_STATUSES = [
    ("AWAITING_TEARDOWN", "Awaiting Teardown", False),
    ("TORN_DOWN", "Torn Down", True),
]

PART_STATUSES = [
    ("AVAILABLE", "Available", False),
    ("RESERVED", "Reserved", False),
    ("INSTALLED", "Installed", False),
    ("FAULTY", "Faulty", False),
    ("SCRAPPED", "Scrapped", True),
    ("SOLD", "Sold", True),
    ("LOST", "Lost", True),
]

REPAIR_STATUSES = [
    ("PLANNED", "Planned", False),
    ("PARTS_RESERVED", "Parts Reserved", False),
    ("IN_PROGRESS", "In Progress", False),
    ("COMPLETE", "Complete", True),
    ("CANCELLED", "Cancelled", True),
]

ORDER_STATUSES = [
    ("DRAFT", "Draft", False),
    ("ORDERED", "Ordered", False),
    ("SHIPPED", "Shipped", False),
    ("RECEIVED", "Received", True),
    ("CANCELLED", "Cancelled", True),
]

LISTING_STATUSES = [
    ("DRAFT", "Draft", False),
    ("ACTIVE", "Active", False),
    ("ENDED", "Ended", True),
    ("SOLD", "Sold", True),
    ("CANCELLED", "Cancelled", True),
]

DEFAULT_TEST_DEFINITIONS = [
    "touchscreen", "display", "face_id_touch_id", "front_camera", "rear_cameras",
    "microphone", "speakers", "charging", "wireless_charging", "wifi", "bluetooth",
    "mobile_signal", "buttons", "vibration", "proximity_sensor", "ambient_light_sensor",
    "true_tone", "battery_health", "sim_esim", "water_damage_indicator",
]
DEFAULT_TEST_LABELS = {
    "touchscreen": "Touchscreen",
    "display": "Display",
    "face_id_touch_id": "Face ID / Touch ID",
    "front_camera": "Front Camera",
    "rear_cameras": "Rear Camera(s)",
    "microphone": "Microphone",
    "speakers": "Speakers",
    "charging": "Charging",
    "wireless_charging": "Wireless Charging",
    "wifi": "WiFi",
    "bluetooth": "Bluetooth",
    "mobile_signal": "Mobile Signal",
    "buttons": "Buttons",
    "vibration": "Vibration",
    "proximity_sensor": "Proximity Sensor",
    "ambient_light_sensor": "Ambient Light Sensor",
    "true_tone": "True Tone",
    "battery_health": "Battery Health",
    "sim_esim": "SIM / eSIM",
    "water_damage_indicator": "Water Damage Indicator",
}

DEFAULT_PART_TYPES = [
    "Screen", "Battery", "Rear Camera", "Front Camera", "Housing", "Back Glass",
    "Charging Port / Flex", "Speaker", "Earpiece", "Vibration Motor", "SIM Tray",
    "Logic Board", "Face ID Module", "Home Button",
]

DEFAULT_CONDITION_GRADES = [
    ("A", "Excellent - like new", 1),
    ("A-", "Very good - minor wear", 2),
    ("B", "Good - visible wear", 3),
    ("B-", "Fair - noticeable wear", 4),
    ("C", "Heavy wear / cosmetic damage", 5),
    ("SCRAP", "Not usable", 99),
]

DEFAULT_EXPENSE_CATEGORIES = [
    "Postage In", "Postage Out", "Packaging", "Tools", "Software/Subscriptions",
    "Travel", "Listing Fees", "Misc",
]

DEFAULT_BUSINESS_SETTINGS = [
    ("business_name", "My Phone Business", "string", "Shown on listings/reports"),
    ("currency", "GBP", "string", "ISO currency code"),
    ("currency_symbol", "£", "string", "Symbol used in Discord output"),
    ("default_postage_cost", "6.00", "number", "Default outbound shipping cost"),
    ("default_packaging_cost", "1.00", "number", "Default packaging cost per sale"),
    ("ebay_fee_pct_assumption", "12.8", "number", "Estimated eBay final value fee %, used before actual fee is known"),
    ("ebay_fixed_fee_assumption", "0.30", "number", "Estimated fixed per-order eBay fee"),
    ("min_profit_gbp", "20.00", "number", "Global minimum acceptable net profit"),
    ("min_roi_pct", "15.00", "number", "Global minimum acceptable ROI %"),
    ("target_margin_pct", "25.00", "number", "Global target margin %"),
    ("default_repair_cost_assumption", "60.00", "number",
     "Fallback repair cost estimate used for faulty-phone buy price recommendations when a model has no repair history yet"),
]
