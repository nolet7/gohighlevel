"""Contract tests for the live adapters using httpx.MockTransport (no network)."""
import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from hvac_engine.adapters.fieldpulse import FieldMap, FieldPulseClient, parse_ts
from hvac_engine.adapters.ghl import GHLClient
from hvac_engine.adapters.http import UpstreamError, request_with_retry
from hvac_engine.domain import Channel, FPState
from tests.conftest import TZ


# ----------------------------------------------------------------------- retries
def _client(handler):
    return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler))


def test_retries_429_and_5xx_with_backoff_then_succeeds():
    calls, slept = [], []
    seq = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503), httpx.Response(200, json={"ok": 1})])

    def handler(req):
        calls.append(1)
        return next(seq)
    r = request_with_retry(_client(handler), "svc", "GET", "/a", sleep=slept.append)
    assert r.json() == {"ok": 1} and len(calls) == 3
    assert slept[0] == 2.0 and 0.5 <= slept[1] < 1.0 + 0.3


def test_honours_ratelimit_reset_epoch_header():
    slept = []
    seq = iter([httpx.Response(429, headers={"RateLimit-Reset": "1000"}), httpx.Response(200, json={})])
    request_with_retry(_client(lambda r: next(seq)), "fp", "GET", "/a", sleep=slept.append, now=lambda: 997.0)
    assert slept == [3.0]


def test_client_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, text="bad")
    with pytest.raises(UpstreamError) as e:
        request_with_retry(_client(handler), "svc", "GET", "/a", sleep=lambda s: None)
    assert e.value.status == 400 and len(calls) == 1


def test_persistent_5xx_raises_after_bounded_attempts():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)
    with pytest.raises(UpstreamError):
        request_with_retry(_client(handler), "svc", "GET", "/a", attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_transport_errors_are_retried():
    seq = iter([httpx.ConnectError("boom"), httpx.Response(200, json={"ok": 1})])

    def handler(req):
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return x
    assert request_with_retry(_client(handler), "svc", "GET", "/a", sleep=lambda s: None).status_code == 200


# --------------------------------------------------------------------------- GHL
def _ghl(requests, stage_ids=None, pipeline=None):
    def handler(req):
        body = json.loads(req.content) if req.content else None
        requests.append((req.method, req.url.path, body, dict(req.headers)))
        if req.url.path == "/contacts/upsert":
            return httpx.Response(200, json={"new": True, "contact": {"id": "c-123"}})
        return httpx.Response(200, json={})
    http = httpx.Client(base_url="https://ghl.test", transport=httpx.MockTransport(handler),
                        headers={"Authorization": "Bearer tok", "Version": "2021-07-28"})
    return GHLClient("tok", "LOC1", "https://ghl.test", "2021-07-28", pipeline, stage_ids, http=http)


def test_ghl_upsert_contact_contract():
    reqs = []
    cid, created = _ghl(reqs).upsert_contact("Maria Lopez", "+14045550101", "m@x.co", {"b", "a"})
    assert (cid, created) == ("c-123", True)
    method, path, body, headers = reqs[0]
    assert (method, path) == ("POST", "/contacts/upsert")
    assert body == {"locationId": "LOC1", "name": "Maria Lopez", "tags": ["a", "b"], "phone": "+14045550101", "email": "m@x.co"}
    assert headers["authorization"] == "Bearer tok" and headers["version"] == "2021-07-28"


def test_ghl_send_sms_and_email_and_dedupe():
    reqs = []
    g = _ghl(reqs)
    assert g.send("c1", Channel.SMS, "hello", "k1") and g.send("c1", Channel.SMS, "hello", "k1")
    assert len(reqs) == 1 and reqs[0][1] == "/conversations/messages"
    assert reqs[0][2] == {"type": "SMS", "contactId": "c1", "message": "hello"}
    g.send("c1", Channel.EMAIL, "Thanks. More text", "k2")
    assert reqs[1][2]["type"] == "Email" and reqs[1][2]["subject"] == "Thanks"


def test_ghl_rvm_and_imessage_use_workflow_trigger_tags():
    reqs = []
    g = _ghl(reqs)
    g.send("c1", Channel.RVM, "x", "k1")
    g.send("c1", Channel.IMESSAGE, "x", "k2")
    assert [(r[1], r[2]) for r in reqs] == [("/contacts/c1/tags", {"tags": ["trigger:rvm"]}),
                                            ("/contacts/c1/tags", {"tags": ["trigger:imessage"]})]


def test_ghl_stage_falls_back_to_tag_then_uses_pipeline_when_mapped():
    reqs = []
    _ghl(reqs).set_stage("c1", "Booked")
    assert reqs[0][1:3] == ("/contacts/c1/tags", {"tags": ["stage:Booked"]})
    reqs2 = []
    _ghl(reqs2, {"Booked": "stg9"}, "pipe1").set_stage("c1", "Booked")
    assert reqs2[0][1] == "/opportunities/upsert" and reqs2[0][2]["pipelineStageId"] == "stg9"


def test_ghl_dnd_update():
    reqs = []
    _ghl(reqs).set_dnd("c1", True)
    assert reqs[0][:3] == ("PUT", "/contacts/c1", {"dnd": True})


# ---------------------------------------------------------------------- FieldPulse
class FakeFP:
    """Serves paged /jobs /customers /estimates /invoices in the documented envelope."""
    def __init__(self, data):
        self.data, self.calls = data, []

    def __call__(self, req):
        self.calls.append((req.url.path, dict(req.url.params)))
        assert req.headers["x-api-key"] == "KEY"
        rows = self.data.get(req.url.path.strip("/"), [])
        page, limit = int(req.url.params.get("page", 1)), int(req.url.params.get("limit", 20))
        assert limit <= 100
        chunk = rows[(page - 1) * limit: page * limit]
        return httpx.Response(200, json={"error": False, "total_count": len(rows), "response": chunk})


def _ts(d: datetime) -> int:
    return int(d.timestamp())


@pytest.fixture
def fp_env(clock):
    base = datetime(2026, 10, 14, 10, 0, tzinfo=TZ)      # a Wednesday inside the 7-14 day window
    data = {
        "customers": [
            {"id": 1, "first_name": "Ann", "last_name": "Lee", "email": "ANN@x.co", "phone": "(404) 555-0001"},
            {"id": 2, "first_name": "Bob", "last_name": "Ray", "email": "bob@x.co", "phone": "404-555-0002", "sms_opt_in": False},
            {"id": 3, "display_name": "Cy Co", "email": None, "phone": None},
        ],
        "jobs": [{"id": j, "customer_id": 1, "status": "Scheduled", "start_time": _ts(base)} for j in range(100, 105)]
                + [{"id": 200, "customer_id": 2, "status": "Cancelled", "start_time": _ts(base)},
                   {"id": 201, "customer_id": 3, "status": "Completed", "start_time": _ts(datetime(2025, 8, 1, 9, tzinfo=TZ))}],
        "estimates": [
            {"id": 9, "customer_id": 2, "status": "Sent", "total": 5000, "created_at": _ts(clock.now() - timedelta(days=9))},
            {"id": 10, "customer_id": 1, "status": "Approved", "total": 100, "created_at": _ts(clock.now() - timedelta(days=9))},
            {"id": 11, "customer_id": 3, "status": "Sent", "total": 1, "created_at": _ts(clock.now() - timedelta(days=1))}],
        "invoices": [{"customer_id": 3, "status": "Paid"}],
    }
    server = FakeFP(data)
    http = httpx.Client(base_url="https://fp.test", transport=httpx.MockTransport(server), headers={"x-api-key": "KEY"})
    client = FieldPulseClient("KEY", "https://fp.test", clock, "America/New_York", http=http)
    return client, server, base


def test_fieldpulse_availability_is_derived_from_jobs_minus_capacity(fp_env):
    c, _, base = fp_env
    av = c.availability(base.date() - timedelta(days=1), base.date() + timedelta(days=4))
    day = av[base.date()]
    assert day.capacity == 12 and day.open == 12 - 5          # 5 scheduled; the cancelled job doesn't count
    assert av[date(2026, 10, 18)].capacity == 0               # Sunday closed


def test_fieldpulse_state_priority(fp_env):
    c, *_ = fp_env
    assert c.customer_state("3") is FPState.PAID              # paid invoice beats completed job
    assert c.customer_state("1") is FPState.CONVERTED         # approved estimate beats scheduled job
    assert c.customer_state("2") is FPState.LEAD              # only a cancelled job -> still a lead
    assert c.customer_state("999") is FPState.LEAD


def test_fieldpulse_customers_normalized(fp_env):
    c, *_ = fp_env
    ann, bob, cy = (c.get_customer(i) for i in ("1", "2", "3"))
    assert ann.name == "Ann Lee" and ann.phone == "+14045550001" and ann.email == "ann@x.co"
    assert bob.marketing_opt_in is False and ann.marketing_opt_in is True
    assert cy.name == "Cy Co" and cy.phone is None
    assert cy.last_service == date(2025, 8, 1)                # derived from latest completed job


def test_fieldpulse_find_customer_by_phone_or_email(fp_env):
    c, *_ = fp_env
    assert c.find_customer("+14045550002", None).id == "2"
    assert c.find_customer(None, "ann@x.co").id == "1"
    assert c.find_customer("+19999999999", None) is None


def test_fieldpulse_unsold_estimates_filters_status_and_age(fp_env):
    c, *_ = fp_env
    assert [e.id for e in c.unsold_estimates(5)] == ["9"]


def test_fieldpulse_pagination_collects_every_page(clock):
    rows = [{"id": i, "customer_id": 1, "status": "Scheduled", "start_time": 1760000000} for i in range(250)]
    server = FakeFP({"jobs": rows})
    http = httpx.Client(base_url="https://fp.test", transport=httpx.MockTransport(server), headers={"x-api-key": "KEY"})
    c = FieldPulseClient("KEY", "https://fp.test", clock, "America/New_York", http=http)
    assert len(c.snapshot().jobs) == 250
    assert [p["page"] for path, p in server.calls if path == "/jobs"] == ["1", "2", "3"]


def test_snapshot_cached_until_ttl_or_forced(fp_env, clock):
    c, server, _ = fp_env
    c.customer_state("1"); c.customer_state("2"); c.availability(date(2026, 10, 1), date(2026, 10, 3))
    first = len(server.calls)
    c.customer_state("3")
    assert len(server.calls) == first                         # served from cache
    clock.advance(seconds=61); c.customer_state("1")
    assert len(server.calls) == 2 * first                     # TTL expired -> one refresh
    c.snapshot(force=True)
    assert len(server.calls) == 3 * first


def test_create_job_invalidates_cache_and_posts_epoch(fp_env, clock):
    c, server, _ = fp_env
    c.snapshot()
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(201, json={"error": False, "response": {"id": 777}})
        return server(req)
    c.http = httpx.Client(base_url="https://fp.test", transport=httpx.MockTransport(handler), headers={"x-api-key": "KEY"})
    start = datetime(2026, 10, 20, 9, tzinfo=TZ)
    assert c.create_job("1", start, "service") == "777"
    assert posted[0]["customer_id"] == "1" and posted[0]["start_time"] == int(start.timestamp())
    assert c._snap is None


@pytest.mark.parametrize("v,expected_year", [(1760000000, 2025), ("1760000000", 2025), (1760000000000, 2025),
                                             ("2026-03-04T10:00:00Z", 2026), ("2026-03-04T10:00:00", 2026)])
def test_parse_ts_accepts_epoch_millis_and_iso(v, expected_year):
    assert parse_ts(v).year == expected_year


def test_parse_ts_rejects_garbage():
    assert parse_ts("nope") is None and parse_ts(None) is None


def test_fieldmap_is_the_single_place_to_adapt_field_names(clock):
    fm = FieldMap(job_start="scheduled_start", job_status="state", job_customer_id="cust")
    server = FakeFP({"jobs": [{"id": 1, "cust": 5, "state": "Scheduled", "scheduled_start": 1760000000}]})
    http = httpx.Client(base_url="https://fp.test", transport=httpx.MockTransport(server), headers={"x-api-key": "KEY"})
    c = FieldPulseClient("KEY", "https://fp.test", clock, "America/New_York", fmap=fm, http=http)
    assert c.customer_state("5") is FPState.BOOKED
