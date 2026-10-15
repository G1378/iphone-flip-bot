
import os
import requests
from dotenv import load_dotenv

load_dotenv()

client_id = os.getenv("EBAY_CLIENT_ID")
client_secret = os.getenv("EBAY_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit(
        "Missing EBAY_CLIENT_ID or EBAY_CLIENT_SECRET in .env"
    )

url = "https://api.ebay.com/identity/v1/oauth2/token"

try:
    response = requests.post(
        url,
        auth=(client_id, client_secret),
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )

    print("HTTP status:", response.status_code)

    if response.ok:
        print("SUCCESS: Production OAuth token received.")
    else:
        print("Response:", response.text[:1500])

except requests.RequestException as exc:
    print("Network error:", type(exc).__name__, str(exc))
