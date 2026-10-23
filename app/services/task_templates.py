"""Seed data for the GENERAL (business-level) tickets.

Phone tickets are not templated: they are derived from each phone's real state
(test results, faults, stock, orders, status) by app.services.phone_tickets.

Everything here is editable from Discord afterwards (`/tasks template-add`,
`/tasks template-toggle`); this module is only the starting point.
recur=None -> one-off (opened by `/tasks setup-general`);
recur=N    -> reopened every N days after the last one is closed.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class GeneralTpl(NamedTuple):
    code: str
    title: str
    description: str
    category: str
    priority: str = "NORMAL"
    recur: Optional[int] = None


GENERAL_TEMPLATES: list[GeneralTpl] = [
    # ---------------------------------------------------------------- BOT / TECH (one-off)
    GeneralTpl("setup_bot_env", "Finish bot setup: .env, database and Discord token",
               "See README sections 3 to 5.", "Bot & Tech", "HIGH"),
    GeneralTpl("setup_channels", "Run /setup-channels and /home",
               "Registers every channel, including this one.", "Bot & Tech", "HIGH"),
    GeneralTpl("setup_users", "Authorize everyone who will use the bot (/config users)",
               "", "Bot & Tech"),
    GeneralTpl("setup_locations", "Add storage locations (/config locations)",
               "Shelf and bin codes that match physical labels.", "Bot & Tech"),
    GeneralTpl("setup_business", "Set business settings (/config business)",
               "Postage, packaging, fee assumptions, minimum profit and ROI.", "Bot & Tech"),
    GeneralTpl("setup_phone_models", "Add the iPhone models you buy (/config phone-models)",
               "", "Bot & Tech"),
    GeneralTpl("setup_ebay", "Connect eBay (/config ebay)",
               "Needed for listings, sales sync and price data.", "Bot & Tech"),
    GeneralTpl("setup_sheets", "Connect Google Sheets (optional)",
               "", "Bot & Tech", "LOW"),
    GeneralTpl("setup_backups", "Set up automated database backups and test a restore",
               "A backup you have never restored is not a backup. See README section 12.", "Bot & Tech", "HIGH"),
    GeneralTpl("setup_ssd", "Move the Pi to an SSD (if running from an SD card)",
               "SD cards fail. See README section 12.", "Bot & Tech", "LOW"),
    GeneralTpl("setup_demo_clear", "Clear demo data before going live",
               "", "Bot & Tech"),

    # ---------------------------------------------------------------- BUSINESS ADMIN (one-off)
    GeneralTpl("biz_sole_trader", "Check HMRC registration requirements for trading",
               "Check whether you need to register as self-employed. Official guidance on gov.uk.", "Business Admin", "HIGH"),
    GeneralTpl("biz_bank", "Open a separate bank account for the business",
               "Makes bookkeeping and tax far easier.", "Business Admin"),
    GeneralTpl("biz_insurance", "Get quotes for stock and public liability insurance",
               "", "Business Admin", "LOW"),
    GeneralTpl("biz_ebay_account", "Set up the eBay seller account and business details",
               "Payments, payouts, store, returns policy, postage policies.", "Business Admin", "HIGH"),
    GeneralTpl("biz_returns_policy", "Write returns and warranty policy",
               "Decide on 30-day returns and any warranty and keep it consistent across listings.", "Business Admin"),
    GeneralTpl("biz_weee", "Check WEEE / battery recycling obligations",
               "", "Business Admin", "LOW"),
    GeneralTpl("biz_bookkeeping", "Choose a bookkeeping method",
               "Sheets sync, spreadsheet or accounting software.", "Business Admin"),
    GeneralTpl("biz_receipts", "Create a system for storing receipts",
               "", "Business Admin"),

    # ---------------------------------------------------------------- WORKSHOP (one-off)
    GeneralTpl("ws_tools", "Buy the core tool kit",
               "Precision drivers incl. pentalobe, spudgers, suction cup, tweezers, heat mat or hot air station, opening picks, magnetic mat.", "Workshop", "HIGH"),
    GeneralTpl("ws_esd", "Set up an anti-static work area",
               "ESD mat and wrist strap, good lighting, magnification.", "Workshop"),
    GeneralTpl("ws_test_devices", "Get known-good test devices and cables",
               "A working phone to bench-test parts, spare SIMs, chargers, cables.", "Workshop"),
    GeneralTpl("ws_supplies", "Stock consumables",
               "IPA, adhesive strips, gaskets, screws, anti-static bags, labels, microfibre cloths.", "Workshop"),
    GeneralTpl("ws_battery_storage", "Set up safe storage for batteries",
               "Fire-safe box, away from stock.", "Workshop", "HIGH"),
    GeneralTpl("ws_photo_setup", "Build a listing photo setup",
               "Light box or plain background, tripod, consistent lighting.", "Workshop", "LOW"),
    GeneralTpl("ws_postage_kit", "Stock packaging",
               "Boxes, bubble wrap, tape, labels, label printer.", "Workshop"),

    # ---------------------------------------------------------------- SOURCING (one-off)
    GeneralTpl("src_suppliers", "Shortlist 3+ parts suppliers",
               "Compare price, quality grade, warranty and delivery.", "Sourcing"),
    GeneralTpl("src_sellers", "Decide where to buy phones",
               "eBay, Facebook Marketplace, Vinted, CeX, auctions, job lots. Note the pros and cons.", "Sourcing"),
    GeneralTpl("src_saved_searches", "Set up saved searches and alerts",
               "For faulty and cracked iPhones in your target models.", "Sourcing"),
    GeneralTpl("src_buy_rules", "Write your buy rules",
               "Max price per model and condition, what you never buy (iCloud locked, blacklisted).", "Sourcing", "HIGH"),
    GeneralTpl("src_safety", "Write a safe collection checklist",
               "Meet in public, check IMEI and iCloud before paying, never pay first.", "Sourcing"),

    # ---------------------------------------------------------------- RECURRING
    GeneralTpl("rec_daily_ebay", "Daily eBay check: messages, offers and orders",
               "", "Routine", "NORMAL", 1),
    GeneralTpl("rec_pricing_review", "Review the pricing watchlist (/pricing)",
               "Adjust buy-price targets.", "Routine", "NORMAL", 7),
    GeneralTpl("rec_stock_check", "Weekly stock check",
               "Physically count phones and parts on the shelves and compare with /stock and /parts.", "Routine", "NORMAL", 7),
    GeneralTpl("rec_stale_listings", "Review listings older than 14 days",
               "", "Routine", "NORMAL", 7),
    GeneralTpl("rec_stuck_phones", "Look for phones stuck in one status for over a week",
               "", "Routine", "NORMAL", 7),
    GeneralTpl("rec_open_orders", "Chase overdue part orders",
               "", "Routine", "NORMAL", 7),
    GeneralTpl("rec_restock", "Reorder consumables",
               "Adhesive, IPA, screws, packaging.", "Routine", "LOW", 14),
    GeneralTpl("rec_backup_verify", "Check the latest backup actually exists",
               "", "Routine", "HIGH", 7),
    GeneralTpl("rec_pi_health", "Check Pi health: disk space, temperature, updates",
               "", "Routine", "LOW", 14),
    GeneralTpl("rec_bot_update", "Check for bot updates",
               "See README section 13.", "Routine", "LOW", 30),
    GeneralTpl("rec_monthly_report", "Monthly report and profit review (/report)",
               "Which models make money? Which suppliers fail?", "Finance", "NORMAL", 30),
    GeneralTpl("rec_expenses", "Log all expenses for the month (/expenses)",
               "Tools, postage, packaging, fees, subscriptions.", "Finance", "NORMAL", 30),
    GeneralTpl("rec_reconcile", "Reconcile bank account with sales and expenses",
               "", "Finance", "NORMAL", 30),
    GeneralTpl("rec_tax_set_aside", "Set aside money for tax",
               "", "Finance", "NORMAL", 30),
    GeneralTpl("rec_tax_records", "Check records are ready for the tax return",
               "", "Finance", "LOW", 90),
    GeneralTpl("rec_returns_review", "Review returns and complaints for patterns",
               "Same fault or model? Fix the process.", "Routine", "LOW", 30),
    GeneralTpl("rec_tool_check", "Check tools and equipment for wear",
               "", "Workshop", "LOW", 90),
    GeneralTpl("rec_tidy_workshop", "Tidy workshop and clear scrap pile",
               "Dispose of dead batteries safely.", "Workshop", "LOW", 14),
]
