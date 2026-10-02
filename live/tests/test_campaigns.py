from datetime import timedelta

from hvac_engine.domain import ContactRecord
from tests.helpers import busy_calendar, seed_customers


def test_detects_only_slow_days_inside_window(engine, fs, clock):
    seed_customers(fs)
    today = busy_calendar(fs, clock, slow_offsets=(3, 8, 11))     # +3 is outside the 7-14 day window
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert set(plan.slow_days) == {(today + timedelta(days=8)).isoformat(), (today + timedelta(days=11)).isoformat()}
    assert plan.open_slots == 18


def test_closed_days_are_never_slow(engine, fs, clock):
    seed_customers(fs)
    today = busy_calendar(fs, clock, slow_offsets=())
    sunday = next(today + timedelta(days=o) for o in range(7, 15) if (today + timedelta(days=o)).weekday() == 6)
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert sunday.isoformat() not in plan.slow_days and plan.slow_days == {}
    assert "no slow days" in plan.note


def test_dry_run_plans_but_changes_nothing(engine, fs, clock, crm):
    seed_customers(fs)
    busy_calendar(fs, clock)
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert plan.dry_run and plan.enrolled > 0 and plan.targets
    assert crm.sent == [] and crm.contacts == {} and engine.store.all_contacts() == []
    assert all(set(t) == {"fp_customer_id", "first_name"} for t in plan.targets)     # no contact PII in the plan


def test_dry_run_does_not_consume_the_daily_run_guard(engine, fs, clock):
    seed_customers(fs)
    busy_calendar(fs, clock)
    engine.campaigns.fill_calendar(dry_run=True)
    assert engine.campaigns.fill_calendar().enrolled > 0


def test_budget_formula_and_hard_cap(engine, fs, clock):
    seed_customers(fs, 300)
    busy_calendar(fs, clock)                                       # 18 open slots on slow days
    engine.deps.settings.fill_max_targets = 1000
    assert engine.campaigns.fill_calendar(dry_run=True).budget == 113     # ceil(ceil(18*0.5)/0.08)
    engine.deps.settings.fill_max_targets = 25
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert plan.budget == 25 and plan.enrolled == 25


def test_targets_longest_lapsed_customers_first(engine, fs, clock):
    ids = seed_customers(fs, 60)
    busy_calendar(fs, clock)
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert plan.targets[0]["fp_customer_id"] == ids[-1]            # i=59 has the oldest last_service


def test_recently_serviced_customers_are_excluded(engine, fs, clock):
    seed_customers(fs, 5)
    fs.add_customer("Recent Ron", "+14045550444", "ron@example.com", last_service=clock.now().date() - timedelta(days=30))
    busy_calendar(fs, clock)
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert all(t["first_name"] != "Recent" for t in plan.targets) and plan.candidates == 5


def test_skip_reasons_are_enforced_and_counted(engine, fs, clock, crm):
    ids = seed_customers(fs, 10)
    fs.add_customer("No Optin", "+14045550001", "n@example.com", last_service=clock.now().date() - timedelta(days=400), opt_in=False)
    fs.add_customer("No Contact", None, None, last_service=clock.now().date() - timedelta(days=400))
    busy_calendar(fs, clock)
    # DND contact
    cid, _ = crm.upsert_contact("Customer0 Smith", "+14045551000", "c0@example.com", set())
    engine.store.save_contact(ContactRecord(cid, "Customer0 Smith", "+14045551000", "c0@example.com", ids[0], dnd=True), clock.now())
    # customer with open work in FieldPulse
    fs.create_job(ids[1], clock.now() + timedelta(days=2), "service")
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert plan.skipped["no_marketing_opt_in"] == 1 and plan.skipped["no_contact_info"] == 1
    assert plan.skipped["dnd"] == 1 and plan.skipped["open_work_in_fieldpulse"] == 1
    assert ids[0] not in {t["fp_customer_id"] for t in plan.targets}


def test_already_enrolled_and_weekly_cap_skipped(engine, fs, clock, crm):
    ids = seed_customers(fs, 6)
    busy_calendar(fs, clock)
    engine.campaigns.reactivate_database(limit=1)                  # enrolls the oldest customer
    plan = engine.campaigns.fill_calendar(dry_run=True)
    assert plan.targets[0]["fp_customer_id"] != ids[-1] or plan.skipped.get("already_enrolled")
    # cap
    rec = engine.store.contact_by_fp(ids[-1]) or engine.store.contact_by_fp(ids[-2])
    for i in range(4):
        engine.store.record_message(f"cap{i}", rec.id, "out", "sms", "marketing", "x", clock.now())
    plan2 = engine.campaigns.fill_calendar(dry_run=True)
    assert plan2.skipped.get("weekly_cap", 0) + plan2.skipped.get("already_enrolled", 0) >= 1


def test_live_run_sends_and_second_run_same_day_is_guarded(engine, fs, clock, crm):
    seed_customers(fs, 60)
    busy_calendar(fs, clock)
    plan = engine.campaigns.fill_calendar()
    assert plan.enrolled == plan.budget == 40
    sms = [m for m in crm.sent if "openings on" in m["text"] and m["channel"] == "sms"]
    assert len(sms) == 40 and all("STOP" in m["text"] for m in sms)
    again = engine.campaigns.fill_calendar()
    assert again.enrolled == 0 and "already ran" in again.note
    assert engine.campaigns.fill_calendar(force=True).note == ""    # explicit override works


def test_campaign_message_names_the_slow_days_and_offer(engine, fs, clock, crm):
    seed_customers(fs, 3)
    busy_calendar(fs, clock)
    engine.campaigns.fill_calendar()
    text = next(m["text"] for m in crm.sent if "openings on" in m["text"])
    assert "Sat Oct 10" in text and "Tue Oct 13" in text and "$40 off" in text and "fall furnace" in text


def test_audit_trail_recorded_for_live_runs(engine, fs, clock):
    seed_customers(fs, 5)
    busy_calendar(fs, clock)
    engine.campaigns.fill_calendar()
    assert len(engine.store.audit_rows("campaign_fill_calendar")) == 1


# -------------------------------------------------------------------- estimates
def test_estimate_reactivation_age_and_status_filters(engine, fs, clock):
    young = fs.add_customer("Young Est", "+14045550301", "y@example.com")
    old = fs.add_customer("Old Est", "+14045550302", "o@example.com")
    won = fs.add_customer("Won Est", "+14045550303", "w@example.com")
    fs.add_estimate(young, 5000, clock.now() - timedelta(days=3))
    fs.add_estimate(old, 5000, clock.now() - timedelta(days=9))
    fs.add_estimate(won, 5000, clock.now() - timedelta(days=9), "approved")
    plan = engine.campaigns.reactivate_estimates(dry_run=True)
    assert [t["first_name"] for t in plan.targets] == ["Old"]


def test_estimate_sequence_stops_when_customer_books(engine, fs, clock):
    cust = fs.add_customer("Ken Ford", "+14045550201", "ken@example.com")
    fs.add_estimate(cust, 9800, clock.now() - timedelta(days=9))
    engine.campaigns.reactivate_estimates()
    cid = engine.store.contact_by_fp(cust).id
    assert engine.store.is_enrolled(cid, "estimate_reactivation")
    fs.create_job(cust, clock.now() + timedelta(days=3), "install")
    engine.tick()
    assert not engine.store.is_enrolled(cid)


# --------------------------------------------------------------------- database
def test_database_reactivation_respects_limit_and_age(engine, fs, clock):
    seed_customers(fs, 80)
    plan = engine.campaigns.reactivate_database(dry_run=True, limit=10)
    assert plan.enrolled == 10 and plan.candidates == 80 - 0
    fs.add_customer("Fresh Fred", "+14045550555", "f@example.com", last_service=clock.now().date() - timedelta(days=60))
    assert engine.campaigns.reactivate_database(dry_run=True, limit=500).candidates == 80


# ------------------------------------------------------------ safety guard
def test_empty_looking_calendar_is_treated_as_bad_data_not_a_sales_opportunity(engine, fs, clock, crm):
    seed_customers(fs, 60)                                         # no jobs at all => every workday "slow"
    plan = engine.campaigns.fill_calendar()
    assert plan.enrolled == 0 and "anomaly" in plan.note and crm.sent == []
    assert engine.store.audit_rows("campaign_blocked_anomaly")
    assert engine.metrics.value("campaign_blocked_total", reason="calendar_anomaly") == 1


def test_anomaly_guard_can_be_overridden_deliberately(engine, fs, clock, crm):
    seed_customers(fs, 60)
    plan = engine.campaigns.fill_calendar(force=True)
    assert plan.enrolled > 0 and crm.sent


def test_genuinely_slow_week_below_threshold_still_runs(engine, fs, clock):
    seed_customers(fs, 60)
    busy_calendar(fs, clock, slow_offsets=(8, 9, 10, 11))          # 3 workdays slow of 6 (50%): legitimate lull
    assert engine.campaigns.fill_calendar(dry_run=True).enrolled > 0
