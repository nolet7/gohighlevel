import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from hvac_engine.api import create_app
from hvac_engine.security import sign
from tests.helpers import busy_calendar, make_lead, seed_customers

SECRET = "s3cret"
LEAD = {"name": "Maria Lopez", "phone": "(404) 555-0101", "email": "maria@example.com",
        "issue": "AC not cooling", "sms_consent": True}


@pytest.fixture
def client(engine, settings):
    return TestClient(create_app(engine, settings))


def post(client, engine, path, payload, *, secret=SECRET, ts_offset=0, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    ts = str(int(engine.deps.clock.now().timestamp()) + ts_offset)
    h = {"X-Timestamp": ts, "X-Signature": sign(secret, ts, body), "Content-Type": "application/json"}
    h.update(headers or {})
    return client.post(path, content=body, headers=h)


ADMIN = {"Authorization": "Bearer admin-key"}


# ------------------------------------------------------------ signature security
def test_valid_signed_lead_is_processed(client, engine, crm):
    r = post(client, engine, "/webhooks/leads/website", LEAD)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert {m["channel"] for m in crm.sent} == {"sms", "email"}


def test_wrong_secret_rejected(client, engine, crm):
    assert post(client, engine, "/webhooks/leads/website", LEAD, secret="wrong").status_code == 401
    assert crm.sent == []


def test_missing_signature_headers_rejected(client):
    assert client.post("/webhooks/leads/website", json=LEAD).status_code == 401


def test_stale_timestamp_rejected_replay_protection(client, engine):
    assert post(client, engine, "/webhooks/leads/website", LEAD, ts_offset=-3600).status_code == 401
    assert post(client, engine, "/webhooks/leads/website", LEAD, ts_offset=3600).status_code == 401


def test_tampered_body_rejected(client, engine):
    body = json.dumps(LEAD).encode()
    ts = str(int(engine.deps.clock.now().timestamp()))
    sig = sign(SECRET, ts, body)
    r = client.post("/webhooks/leads/website", content=body.replace(b"Maria", b"Mallory"),
                    headers={"X-Timestamp": ts, "X-Signature": sig})
    assert r.status_code == 401


def test_oversized_payload_rejected(client, engine):
    assert post(client, engine, "/webhooks/leads/website", None, raw=b"x" * 70_000).status_code == 413


# ------------------------------------------------------------------ idempotency
def test_duplicate_event_id_processed_once(client, engine, crm):
    h = {"X-Event-Id": "evt-1"}
    assert post(client, engine, "/webhooks/leads/website", LEAD, headers=h).json()["status"] == "ok"
    n = len(crm.sent)
    assert post(client, engine, "/webhooks/leads/website", LEAD, headers=h).json()["status"] == "duplicate"
    assert len(crm.sent) == n and len(engine.store.all_contacts()) == 1


def test_identical_body_without_event_id_deduplicated_by_hash(client, engine):
    post(client, engine, "/webhooks/leads/website", LEAD)
    assert post(client, engine, "/webhooks/leads/website", LEAD).json()["status"] == "duplicate"


# ------------------------------------------------------------------- validation
def test_invalid_payloads_are_422(client, engine):
    assert post(client, engine, "/webhooks/leads/website", {"phone": "404"}).status_code == 422      # no name
    assert post(client, engine, "/webhooks/leads/website", {"name": "No Contact"}).status_code == 422  # no phone/email
    assert post(client, engine, "/webhooks/leads/website", None, raw=b"not json").status_code == 422


def test_unknown_source_404(client, engine):
    assert post(client, engine, "/webhooks/leads/tiktok", LEAD).status_code == 404


def test_source_mapping_tags_contact(client, engine, crm):
    cid = post(client, engine, "/webhooks/leads/meta", LEAD).json()["contact_id"]
    assert "source:meta_lead_ad" in crm.tags[cid]


# ---------------------------------------------------------------- inbound / call
def test_inbound_webhook_books_and_unknown_contact_404(client, engine, crm):
    cid = post(client, engine, "/webhooks/leads/website", LEAD).json()["contact_id"]
    r = post(client, engine, "/webhooks/ghl/inbound", {"contact_id": cid, "message": "yes book me"})
    assert r.json()["outcome"] == "booked" and crm.stages[cid] == "Booked"
    assert post(client, engine, "/webhooks/ghl/inbound", {"contact_id": "ghost", "message": "hi"}).status_code == 404


def test_missed_call_webhook(client, engine, crm):
    r = post(client, engine, "/webhooks/ghl/missed-call", {"phone": "404-555-0188"})
    assert r.status_code == 200 and len(crm.sent) == 1
    assert post(client, engine, "/webhooks/ghl/missed-call", {"phone": "12"}).status_code == 422


# ----------------------------------------------------------- FieldPulse webhook
def test_fieldpulse_webhook_requires_token(client):
    assert client.post("/webhooks/fieldpulse/wrong", json={}).status_code == 404


def test_fieldpulse_webhook_is_a_hint_and_reconciles_from_source(client, engine, crm, fs, clock):
    from datetime import timedelta
    cid = make_lead(engine)
    fp = engine.store.get_contact(cid).fp_id
    # a FORGED event claiming "paid" changes nothing: truth is re-read from FieldPulse
    r = client.post("/webhooks/fieldpulse/fp-token", json={"customer_id": fp, "status": "paid"})
    assert r.status_code == 200 and r.json()["changed"] is False and crm.stages[cid] == "New Lead"
    fs.create_job(fp, clock.now() + timedelta(days=1), "service")                       # real change
    r = client.post("/webhooks/fieldpulse/fp-token", json={"data": {"customer_id": fp}})
    assert r.json()["changed"] is True and crm.stages[cid] == "Booked"


def test_fieldpulse_webhook_tolerates_garbage(client):
    assert client.post("/webhooks/fieldpulse/fp-token", content=b"\xff\xfe not json").status_code == 200


# ------------------------------------------------------------------------ admin
def test_admin_requires_bearer_token(client):
    assert client.post("/admin/jobs/tick").status_code == 401
    assert client.post("/admin/jobs/tick", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/admin/jobs/tick", headers=ADMIN).status_code == 200


def test_admin_dry_run_fill_calendar(client, engine, fs, clock, crm):
    seed_customers(fs, 30)
    busy_calendar(fs, clock)
    r = client.post("/admin/jobs/fill-calendar?dry_run=true", headers=ADMIN)
    body = r.json()
    assert r.status_code == 200 and body["dry_run"] and body["enrolled"] > 0 and crm.sent == []
    assert len(body["slow_days"]) == 2


def test_admin_unknown_job_and_audit(client, engine):
    assert client.post("/admin/jobs/nuke", headers=ADMIN).status_code == 404
    client.post("/admin/jobs/sync", headers=ADMIN)
    rows = client.get("/admin/audit", headers=ADMIN).json()
    assert any(r["action"] == "job:sync" for r in rows)
    assert client.get("/admin/audit").status_code == 401


# ------------------------------------------------------------------- operations
def test_health_ready_metrics(client, engine):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["status"] == "ready"
    post(client, engine, "/webhooks/leads/website", LEAD)
    text = client.get("/metrics").text
    assert 'hge_leads_ingested_total{source="website"} 1' in text
    assert "hge_messages_sent_total" in text
    assert "maria" not in text.lower() and "404" not in text           # metrics carry no PII


# ------------------------------------------------- static-token mode (GHL native webhook)
@pytest.fixture
def static_client(engine, settings):
    settings.webhook_static_token = SecretStr("ghl-static")
    return TestClient(create_app(engine, settings))


def test_static_token_accepted_when_configured(static_client):
    r = static_client.post("/webhooks/ghl/missed-call", json={"phone": "404-555-0166"},
                           headers={"X-Webhook-Token": "ghl-static"})
    assert r.status_code == 200


def test_wrong_static_token_rejected(static_client):
    r = static_client.post("/webhooks/ghl/missed-call", json={"phone": "404-555-0166"},
                           headers={"X-Webhook-Token": "nope"})
    assert r.status_code == 401


def test_static_token_header_ignored_when_mode_not_configured(client):
    r = client.post("/webhooks/ghl/missed-call", json={"phone": "404-555-0166"},
                    headers={"X-Webhook-Token": "anything"})
    assert r.status_code == 401


def test_hmac_still_works_alongside_static_mode(static_client, engine):
    assert post(static_client, engine, "/webhooks/ghl/missed-call", {"phone": "404-555-0167"}).status_code == 200
