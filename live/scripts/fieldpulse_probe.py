#!/usr/bin/env python3
"""Day-1 discovery against a REAL FieldPulse key. Read-only (GET only).

    HGE_FIELDPULSE_API_KEY=... python scripts/fieldpulse_probe.py

Prints the real JSON keys for jobs/customers/estimates/invoices, then checks every field the
adapter's FieldMap expects. Fix mismatches by editing FieldMap (src/hvac_engine/adapters/fieldpulse.py)
and re-running until it prints ALL CHECKS PASSED.
"""
import os
import sys
from collections import Counter

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from hvac_engine.adapters.fieldpulse import FieldMap, parse_ts  # noqa: E402

BASE = os.environ.get("HGE_FIELDPULSE_BASE_URL", "https://ywe3crmpll.execute-api.us-east-2.amazonaws.com/stage")
KEY = os.environ.get("HGE_FIELDPULSE_API_KEY")
if not KEY:
    sys.exit("set HGE_FIELDPULSE_API_KEY (request one from support@fieldpulse.com)")

m = FieldMap()
EXPECT = {
    "/customers": [m.customer_id, m.first_name, m.last_name, m.email, m.phone],
    "/jobs": [m.job_id, m.job_customer_id, m.job_status, m.job_start],
    "/estimates": [m.estimate_id, m.estimate_customer_id, m.estimate_status, m.estimate_created],
    "/invoices": [m.invoice_customer_id, m.invoice_status],
}
problems = 0
with httpx.Client(base_url=BASE, headers={"x-api-key": KEY}, timeout=20) as c:
    for path, fields in EXPECT.items():
        r = c.get(path, params={"page": 1, "limit": 5})
        print(f"\n== GET {path} -> {r.status_code}  rate-limit headers: "
              f"{ {k: v for k, v in r.headers.items() if 'ratelimit' in k.lower()} }")
        if r.status_code != 200:
            problems += 1
            print("   FAILED:", r.text[:200])
            continue
        body = r.json()
        rows = body.get("response") or []
        print(f"   total_count={body.get('total_count')} sample_keys={sorted(rows[0]) if rows else 'EMPTY'}")
        for f in fields:
            seen = sum(1 for row in rows if row.get(f) not in (None, ""))
            ok = seen > 0 if rows else None
            print(f"   field {f!r:24} present in {seen}/{len(rows)} rows  {'OK' if ok else 'MISSING' if ok is False else '?'}")
            problems += ok is False
        if path == "/jobs" and rows:
            print("   job status values seen:", dict(Counter(str(x.get(m.job_status)) for x in rows)))
            print("   start parses:", [str(parse_ts(x.get(m.job_start))) for x in rows[:3]])
            print("   -> set completed/cancelled status vocab in FieldMap to match the values above")
print("\nALL CHECKS PASSED" if not problems else f"\n{problems} PROBLEM(S): adjust FieldMap")
sys.exit(1 if problems else 0)
