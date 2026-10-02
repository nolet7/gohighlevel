from datetime import timedelta

import pytest

from tests.conftest import TZ
from tests.helpers import make_lead, sent_to


def last_text(crm, cid):
    return sent_to(crm, cid)[-1]["text"]


# ------------------------------------------------------------- opt-out handling
@pytest.mark.parametrize("word", ["STOP", "stop.", "Unsubscribe", "  quit  ", "STOPALL", "cancel"])
def test_stop_keywords_opt_out(engine, crm, word):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, word) == "opted_out"
    c = engine.store.get_contact(cid)
    assert c.dnd and crm.dnd[cid] and not engine.store.is_enrolled(cid)
    assert "unsubscribed" in last_text(crm, cid).lower()


def test_stop_inside_a_sentence_is_not_an_opt_out(engine):
    cid = make_lead(engine)
    outcome = engine.conversation.handle_inbound(cid, "Please stop by tomorrow, my AC is dead")
    assert outcome != "opted_out" and not engine.store.get_contact(cid).dnd


def test_no_messages_after_opt_out_until_start(engine, crm, clock):
    cid = make_lead(engine)
    engine.conversation.handle_inbound(cid, "STOP")
    n = len(sent_to(crm, cid))
    assert engine.conversation.handle_inbound(cid, "hello?") == "ignored_dnd"
    clock.advance(days=10); engine.tick()
    assert len(sent_to(crm, cid)) == n
    assert engine.conversation.handle_inbound(cid, "START") == "opted_in"
    assert not engine.store.get_contact(cid).dnd and not crm.dnd[cid]


def test_help_keyword(engine, crm):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "HELP") == "help"
    assert "STOP" in last_text(crm, cid)


def test_unknown_contact_raises(engine):
    with pytest.raises(KeyError):
        engine.conversation.handle_inbound("ghost", "hi")


# ---------------------------------------------------------------------- safety
def test_gas_smell_escalates_to_human_and_never_auto_books(engine, crm, fs):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "I smell gas near my furnace!") == "safety_escalation"
    assert {"needs-human", "safety"} <= crm.tags[cid]
    assert "911" in last_text(crm, cid)
    assert fs.jobs == []


# --------------------------------------------------------------------- booking
def test_non_urgent_inquiry_gets_a_question_not_a_booking(engine, fs, crm):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "how much is a tune-up?") == "qualifying"
    assert fs.jobs == []


def test_urgent_same_day_booking_is_in_the_future(engine, fs, clock):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "no heat at all") == "booked"
    job = fs.jobs[-1]
    assert job["start"] > clock.now() and job["type"] == "emergency"
    assert job["start"].astimezone(TZ).date() == clock.now().astimezone(TZ).date()


def test_late_in_day_urgent_rolls_to_next_open_day(engine, fs, clock):
    clock.set(clock.now().astimezone(TZ).replace(hour=16, minute=30))
    cid = make_lead(engine)
    engine.conversation.handle_inbound(cid, "no cool, emergency")
    assert fs.jobs[-1]["start"].astimezone(TZ).date() > clock.now().astimezone(TZ).date()


def test_non_urgent_booking_starts_tomorrow_or_later(engine, fs, clock):
    cid = make_lead(engine)
    engine.conversation.handle_inbound(cid, "yes book me")
    assert fs.jobs[-1]["start"].astimezone(TZ).date() > clock.now().astimezone(TZ).date()


def test_booking_updates_crm_and_stops_sequences(engine, crm):
    cid = make_lead(engine)
    engine.conversation.handle_inbound(cid, "book")
    assert crm.stages[cid] == "Booked" and "ai-booked" in crm.tags[cid]
    assert not engine.store.is_enrolled(cid)


def test_double_booking_is_prevented(engine, fs):
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "book") == "booked"
    assert engine.conversation.handle_inbound(cid, "yes book me again") == "already_booked"
    assert len([j for j in fs.jobs if j["customer_id"] == engine.store.get_contact(cid).fp_id]) == 1


def test_fieldpulse_outage_hands_off_to_human(engine, fs, crm):
    cid = make_lead(engine)
    fs.fail_job_creation = True
    assert engine.conversation.handle_inbound(cid, "book") == "booking_failed"
    assert "needs-human" in crm.tags[cid] and crm.stages[cid] == "New Lead"
    assert engine.metrics.value("booking_failures_total") == 1


def test_fully_booked_calendar_hands_off(engine, fs, clock, crm):
    today = clock.now().astimezone(TZ).date()
    for off in range(0, 16):
        fs.seed_load(today + timedelta(days=off), 12)
    cid = make_lead(engine)
    assert engine.conversation.handle_inbound(cid, "no heat") == "no_availability"
    assert "needs-human" in crm.tags[cid]


def test_inbound_text_grants_text_consent(engine):
    cid = make_lead(engine, consent=False)
    assert not engine.store.get_contact(cid).sms_consent
    engine.conversation.handle_inbound(cid, "hello")
    assert engine.store.get_contact(cid).sms_consent


# ------------------------------------------------------------------ missed call
def test_missed_call_text_back_with_implied_consent(engine, crm):
    cid = engine.conversation.missed_call("(404) 555-0199")
    assert len(sent_to(crm, cid)) == 1 and "STOP" in last_text(crm, cid)


def test_missed_call_without_implied_consent_sends_nothing(engine, crm):
    engine.deps.settings.missed_call_implied_consent = False
    cid = engine.conversation.missed_call("+14045550199")
    assert sent_to(crm, cid) == []


def test_missed_call_invalid_phone(engine):
    with pytest.raises(ValueError):
        engine.conversation.missed_call("123")


def test_missed_call_from_opted_out_number_is_silent(engine, crm):
    cid = make_lead(engine, phone="+14045550123")
    engine.conversation.handle_inbound(cid, "STOP")
    n = len(sent_to(crm, cid))
    engine.conversation.missed_call("+14045550123")
    assert len(sent_to(crm, cid)) == n
