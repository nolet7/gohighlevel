#!/usr/bin/env python3
"""Send a correctly signed webhook (for smoke tests and for configuring Make/Zapier/n8n).

    python scripts/sign_webhook.py http://localhost:8000/webhooks/leads/website \
        '{"name":"Test Lead","phone":"404-555-0100","sms_consent":true}'
Env: HGE_WEBHOOK_SECRET
"""
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from hvac_engine.security import sign  # noqa: E402

url, body = sys.argv[1], sys.argv[2].encode()
ts = str(int(time.time()))
r = httpx.post(url, content=body, headers={
    "X-Timestamp": ts, "X-Signature": sign(os.environ["HGE_WEBHOOK_SECRET"], ts, body),
    "Content-Type": "application/json"})
print(r.status_code, r.text)
