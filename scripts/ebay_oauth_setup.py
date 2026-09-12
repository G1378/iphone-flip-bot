#!/usr/bin/env python3
"""One-time eBay OAuth setup.

eBay's Sell APIs require a user-consented OAuth token (a plain
client-credentials app token cannot create listings or read orders on your
behalf). This step needs a real browser login to eBay, which is why the
spec allows a minimal one-time setup step outside Discord - everything
*after* this runs entirely inside Discord/the background jobs.

Usage:
    python scripts/ebay_oauth_setup.py

Run this from any machine with a browser (it does not need to be the
Raspberry Pi itself) with EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and
EBAY_REDIRECT_URI (your eBay app's RuName / redirect URL) set as
environment variables, or in a local .env file.

The script will:
  1. Print a consent URL for you to open in a browser and log into eBay.
  2. Ask you to paste the redirect URL (or just the `code` query param)
     you land on after granting consent.
  3. Exchange that code for a refresh token and print it.

Paste the printed EBAY_REFRESH_TOKEN value into your .env file. It is the
only long-lived eBay credential the bot needs; it is never displayed again
and never sent to Discord.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.parse

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

EBAY_ENV = os.environ.get("EBAY_ENV", "SANDBOX").upper()
CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("EBAY_REDIRECT_URI", "")

AUTHORIZE_BASE = (
    "https://auth.ebay.com/oauth2/authorize" if EBAY_ENV == "PRODUCTION"
    else "https://auth.sandbox.ebay.com/oauth2/authorize"
)
TOKEN_URL = (
    "https://api.ebay.com/identity/v1/oauth2/token" if EBAY_ENV == "PRODUCTION"
    else "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
)
SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])


def main() -> int:
    if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI):
        print("ERROR: set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and EBAY_REDIRECT_URI first "
              "(in your shell environment or a local .env file).", file=sys.stderr)
        return 1

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
    }
    url = f"{AUTHORIZE_BASE}?{urllib.parse.urlencode(params)}"

    print(f"\nEnvironment: {EBAY_ENV}")
    print("\n1. Open this URL in a browser and log in / grant consent:\n")
    print(f"   {url}\n")
    print("2. After granting consent, eBay redirects you to your redirect URI with a "
          "'?code=...' query parameter.\n")

    pasted = input("Paste the full redirect URL (or just the code value) here: ").strip()

    if pasted.startswith("http"):
        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
    else:
        code = pasted

    if not code:
        print("ERROR: couldn't find a 'code' value in what you pasted.", file=sys.stderr)
        return 1

    code = urllib.parse.unquote(code)

    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = httpx.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20.0,
    )

    if resp.status_code != 200:
        print(f"ERROR: token exchange failed (HTTP {resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    expires_in_days = int(payload.get("refresh_token_expires_in", 0)) // 86400

    print("\n✅ Success! Add this to your .env file:\n")
    print(f"EBAY_REFRESH_TOKEN={refresh_token}")
    print(f"\nThis refresh token is valid for about {expires_in_days} days and will be used by the "
          "bot to mint short-lived access tokens automatically. You will not need to run this "
          "script again unless it expires or you revoke access in your eBay account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
