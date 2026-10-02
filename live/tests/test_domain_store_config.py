from datetime import UTC, datetime

import pytest

from hvac_engine.config import Settings
from hvac_engine.domain import normalize_email, normalize_phone
from hvac_engine.store import Store

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize("raw,expected", [
    ("(404) 555-0101", "+14045550101"), ("404-555-0101", "+14045550101"), ("+14045550101", "+14045550101"),
    ("1 404 555 0101", "+14045550101"), ("555-0101", None), ("", None), (None, None), ("abc", None),
    ("+44 20 7946 0958", "+442079460958"),
])
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("  Maria@Example.COM ", "maria@example.com"), ("nope", None), ("a@b", None), (None, None)])
def test_email_normalization(raw, expected):
    assert normalize_email(raw) == expected


def test_enrollment_unique_while_active_then_reusable():
    s = Store(":memory:")
    first = s.enroll("c1", "speed_to_lead", {}, NOW)
    assert first and s.enroll("c1", "speed_to_lead", {}, NOW) is None     # idempotent
    s.end_enrollment(first, "completed", "done", NOW)
    assert s.enroll("c1", "speed_to_lead", {}, NOW)                       # can re-enroll after it ended


def test_first_time_is_true_exactly_once():
    s = Store(":memory:")
    assert s.first_time("k", NOW) and not s.first_time("k", NOW)


def test_duplicate_message_key_suppressed_and_marketing_count():
    s = Store(":memory:")
    assert s.record_message("k1", "c1", "out", "sms", "marketing", "hi", NOW)
    assert not s.record_message("k1", "c1", "out", "sms", "marketing", "hi", NOW)
    s.record_message("k2", "c1", "out", "sms", "service", "reply", NOW)
    s.record_message(None, "c1", "in", "sms", "inbound", "yes", NOW)
    assert s.marketing_sent_since("c1", datetime(2026, 10, 1, tzinfo=UTC)) == 1   # only marketing/out


def test_cancel_enrollments_respects_sequence_filter():
    s = Store(":memory:")
    s.enroll("c1", "speed_to_lead", {}, NOW)
    s.enroll("c1", "review_request", {}, NOW)
    assert s.cancel_enrollments("c1", "x", NOW, {"speed_to_lead"}) == 1
    assert s.is_enrolled("c1", "review_request") and not s.is_enrolled("c1", "speed_to_lead")


def test_prod_rejects_placeholder_secrets():
    with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
        Settings(env="prod")
    assert Settings(env="prod", webhook_secret="a", admin_api_key="b", fieldpulse_webhook_token="c")


def test_live_mode_requires_credentials():
    with pytest.raises(ValueError, match="live mode requires"):
        Settings(mode="live")


def test_quiet_hour_bounds_validated():
    with pytest.raises(ValueError):
        Settings(quiet_hours_start=25)
