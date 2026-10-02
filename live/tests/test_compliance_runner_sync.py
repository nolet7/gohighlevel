from datetime import datetime, timedelta

import pytest

from hvac_engine.compliance import Verdict
from hvac_engine.domain import Channel, ContactRecord, FPState
from tests.conftest import TZ
from tests.helpers import make_lead, sent_to


def _at(h, m=0, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


# ------------------------------------------------------------- compliance policy
@pytest.mark.parametrize("hour,minute,quiet", [(7, 59, True), (8, 0, False), (20, 59, False), (21, 0, True), (23, 30, True), (3, 0, True)])
def test_quiet_hours_boundaries(engine, hour, minute, quiet):
    assert engine.deps.policy.in_quiet_hours(_at(hour, minute)) is quiet


def test_policy_verdicts(engine):
    p = engine.deps.policy
    c = ContactRecord("c1", "A B", "+14045550101", "a@b.co", sms_consent=True)
    assert p.evaluate(c, Channel.SMS, _at(12)).verdict is Verdict.ALLOW
    assert p.evaluate(c, Channel.SMS, _at(22)).verdict is Verdict.DEFER
    c.dnd = True
    assert p.evaluate(c, Channel.EMAIL, _at(12)).verdict is Verdict.STOP
    c.dnd, c.sms_consent = False, False
    assert p.evaluate(c, Channel.SMS, _at(12)).verdict is Verdict.SKIP          # no text consent
    assert p.evaluate(c, Channel.EMAIL, _at(12)).verdict is Verdict.ALLOW       # email still fine
    c.email = None
    assert p.evaluate(c, Channel.EMAIL, _at(12)).verdict is Verdict.SKIP


def test_weekly_marketing_cap_defers(engine):
    c = ContactRecord("c1", "A B", "+14045550101", None, sms_consent=True)
    now = engine.deps.clock.now()
    for i in range(engine.deps.settings.marketing_cap_per_week):
        engine.store.record_message(f"k{i}", "c1", "out", "sms", "marketing", "x", now)
    d = engine.deps.policy.evaluate(c, Channel.SMS, now, marketing=True)
    assert (d.verdict, d.reason) == (Verdict.DEFER, "weekly_cap")
    assert engine.deps.policy.evaluate(c, Channel.SMS, now, marketing=False).verdict is Verdict.ALLOW


# ------------------------------------------------------------------ sequences
def test_speed_to_lead_fires_immediately_on_both_channels(engine, crm):
    cid = make_lead(engine)
    assert {m["channel"] for m in sent_to(crm, cid)} == {"sms", "email"}
    assert crm.stages[cid] == "New Lead"


def test_followups_honour_delays(engine, crm, clock):
    cid = make_lead(engine)
    base = len(sent_to(crm, cid))
    clock.advance(hours=3); engine.tick()
    assert len(sent_to(crm, cid)) == base               # +4h step not due yet
    clock.advance(hours=2); engine.tick()
    assert len(sent_to(crm, cid)) == base + 1
    clock.advance(hours=20); engine.tick()
    assert len(sent_to(crm, cid)) == base + 2


def test_each_step_sent_exactly_once_across_repeated_ticks(engine, crm, clock):
    cid = make_lead(engine)
    clock.advance(hours=5)
    for _ in range(5):
        engine.tick()
    keys = [m["key"] for m in sent_to(crm, cid)]
    assert len(keys) == len(set(keys))


def test_lead_without_text_consent_gets_email_only(engine, crm):
    cid = make_lead(engine, consent=False)
    assert [m["channel"] for m in sent_to(crm, cid)] == ["email"]


def test_quiet_hours_defer_then_release_next_morning(engine, crm, clock):
    clock.set(_at(22, 30))
    cid = make_lead(engine)
    assert sent_to(crm, cid) == []
    clock.set(_at(8, 5, day=3)); engine.tick()
    assert len(sent_to(crm, cid)) == 2                    # welcome SMS + email only, no catch-up burst
    clock.set(_at(12, 10, day=3)); engine.tick()
    assert len(sent_to(crm, cid)) == 3                    # the +4h nudge is measured from 8:05, not from 22:30


def test_booking_stops_all_followups(engine, crm, clock):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "Yes please book me") == "booked"
    n = len(sent_to(crm, cid))
    clock.advance(days=10); engine.tick()
    assert len(sent_to(crm, cid)) == n
    assert not engine.store.is_enrolled(cid)


def test_defense_in_depth_when_sync_is_missed(engine, crm, clock):
    cid = make_lead(engine)
    c = engine.store.get_contact(cid)
    c.fp_state = FPState.BOOKED                          # state changed but sync/stop never ran
    engine.store.save_contact(c, clock.now())
    n = len(sent_to(crm, cid))
    clock.advance(hours=5); engine.runner.run_due()      # runner alone, no sync in between
    assert len(sent_to(crm, cid)) == n and not engine.store.is_enrolled(cid)


def test_provider_outage_retries_then_recovers(engine, crm):
    crm.fail_next_sends = 1
    cid = make_lead(engine)
    assert sent_to(crm, cid) == []
    engine.runner.run_due()
    assert len(sent_to(crm, cid)) == 2                   # sms + email now delivered


def test_gives_up_after_repeated_failures(engine, crm):
    crm.fail_next_sends = 1000
    make_lead(engine)
    for _ in range(8):
        engine.runner.run_due()
    assert engine.store.active_enrollments() == []
    assert engine.metrics.value("send_failures_total", channel="sms") >= 5


# ----------------------------------------------------------------------- sync
def test_full_lifecycle_sync_and_stages(engine, crm, fs, clock):
    cid = make_lead(engine)
    fp = engine.store.get_contact(cid).fp_id
    fs.create_job(fp, clock.now() + timedelta(days=1), "service")
    assert engine.sync.sync_contact(cid) and crm.stages[cid] == "Booked"
    fs.set_estimate_status(fs.add_estimate(fp, 5000, clock.now(), "approved"), "approved")
    engine.sync.sync_contact(cid); assert crm.stages[cid] == "Won - Install"
    fs.complete_jobs(fp); engine.sync.sync_contact(cid); assert crm.stages[cid] == "Job Complete"
    fs.mark_paid(fp); engine.sync.sync_contact(cid); assert crm.stages[cid] == "Paid / Review Request"
    assert "fp:paid" in crm.tags[cid]


def test_sync_is_idempotent(engine, fs, clock):
    cid = make_lead(engine)
    fs.create_job(engine.store.get_contact(cid).fp_id, clock.now() + timedelta(days=1), "service")
    assert engine.sync.sync_contact(cid) is True
    assert engine.sync.sync_contact(cid) is False


def test_review_request_survives_paid_state_and_waits_two_hours(engine, crm, fs, clock):
    cid = make_lead(engine)
    fp = engine.store.get_contact(cid).fp_id
    fs.mark_paid(fp); engine.sync.sync_contact(cid)
    assert engine.store.is_enrolled(cid, "review_request")        # NOT cancelled by the paid state
    assert not any("review" in m["text"].lower() for m in sent_to(crm, cid))
    clock.advance(hours=2, minutes=5); engine.tick()
    assert any("review" in m["text"].lower() for m in sent_to(crm, cid))


def test_review_request_deferred_through_quiet_hours(engine, crm, fs, clock):
    clock.set(_at(20, 0))
    cid = make_lead(engine)
    fs.mark_paid(engine.store.get_contact(cid).fp_id); engine.sync.sync_contact(cid)
    clock.set(_at(22, 30)); engine.tick()
    assert not any("review" in m["text"].lower() for m in sent_to(crm, cid))
    clock.set(_at(8, 30, day=3)); engine.tick()
    assert any("review" in m["text"].lower() for m in sent_to(crm, cid))


def test_sync_by_fp_id_and_unknown(engine, fs, clock):
    cid = make_lead(engine)
    fp = engine.store.get_contact(cid).fp_id
    fs.create_job(fp, clock.now() + timedelta(days=1), "service")
    assert engine.sync.sync_by_fp_id(fp) is True
    assert engine.sync.sync_by_fp_id("nope") is False


def test_existing_customer_with_open_work_not_enrolled_as_new_lead(engine, crm, fs, clock):
    fp = fs.add_customer("Ray Open", "+14045550150", "ray@example.com")
    fs.create_job(fp, clock.now() + timedelta(days=2), "service")
    cid = make_lead(engine, name="Ray Open", phone="+14045550150", email="ray@example.com")
    assert not engine.store.is_enrolled(cid) and sent_to(crm, cid) == []
