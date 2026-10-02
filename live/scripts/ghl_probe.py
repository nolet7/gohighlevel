#!/usr/bin/env python3
"""Day-1 check of the GHL private-integration token. Creates ONE test contact (tagged hge-probe) and
sends NO messages unless --send-sms-to is given.

    HGE_GHL_TOKEN=... HGE_GHL_LOCATION_ID=... python scripts/ghl_probe.py [--send-sms-to +1404...]

Tries the configured API Version header, then the alternatives, and reports which one the API accepts
(a docs page we read showed "v3"; the long-standing value is 2021-07-28).
"""
import os
import sys

import httpx

TOKEN, LOC = os.environ.get("HGE_GHL_TOKEN"), os.environ.get("HGE_GHL_LOCATION_ID")
BASE = os.environ.get("HGE_GHL_BASE_URL", "https://services.leadconnectorhq.com")
if not (TOKEN and LOC):
    sys.exit("set HGE_GHL_TOKEN and HGE_GHL_LOCATION_ID")

working = None
for version in (os.environ.get("HGE_GHL_API_VERSION", "2021-07-28"), "2021-07-28", "v3"):
    r = httpx.post(f"{BASE}/contacts/upsert", timeout=20,
                   headers={"Authorization": f"Bearer {TOKEN}", "Version": version, "Accept": "application/json"},
                   json={"locationId": LOC, "name": "HGE Probe", "email": "hge-probe@example.com", "tags": ["hge-probe"]})
    print(f"Version={version!r:14} upsert -> {r.status_code} {r.text[:120]}")
    if r.status_code in (200, 201):
        working, contact = version, r.json()["contact"]["id"]
        break
if not working:
    sys.exit("no Version header accepted: check token scopes (contacts.write, conversations/message.write)")
print(f"\nUse HGE_GHL_API_VERSION={working}   contact id={contact}")

if "--send-sms-to" in sys.argv:
    to = sys.argv[sys.argv.index("--send-sms-to") + 1]
    up = httpx.post(f"{BASE}/contacts/upsert", timeout=20,
                    headers={"Authorization": f"Bearer {TOKEN}", "Version": working},
                    json={"locationId": LOC, "name": "HGE Probe", "phone": to, "tags": ["hge-probe"]}).json()
    r = httpx.post(f"{BASE}/conversations/messages", timeout=20,
                   headers={"Authorization": f"Bearer {TOKEN}", "Version": working},
                   json={"type": "SMS", "contactId": up["contact"]["id"], "message": "HGE probe test message"})
    print("send SMS ->", r.status_code, r.text[:200])
