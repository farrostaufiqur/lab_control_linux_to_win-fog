from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from labserver import policy

TZ = ZoneInfo("Asia/Jakarta")
WEEKDAYS = [0, 1, 2, 3, 4]
OFFICE = [{"days": WEEKDAYS, "start": "08:00", "end": "16:00"}]


def ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


# 2026-09-28 is a Monday
def test_open_inside_window_and_closes_at_end():
    now = ts(2026, 9, 28, 10, 0)
    state = policy.evaluate(now, None, None, True, OFFICE, TZ)
    assert state["locked"] is False and state["source"] == "schedule"
    assert state["changes_in"] == 6 * 3600


def test_locked_outside_window_and_opens_next_morning():
    now = ts(2026, 9, 28, 17, 0)
    state = policy.evaluate(now, None, None, True, OFFICE, TZ)
    assert state["locked"] is True
    assert state["changes_at"] == ts(2026, 9, 29, 8, 0)


def test_friday_evening_opens_monday():
    now = ts(2026, 10, 2, 17, 0)  # Friday
    state = policy.evaluate(now, None, None, True, OFFICE, TZ)
    assert state["locked"] and state["changes_at"] == ts(2026, 10, 5, 8, 0)


def test_window_crossing_midnight():
    rules = [{"days": [4], "start": "22:00", "end": "02:00"}]  # Friday night
    assert policy.evaluate(ts(2026, 10, 3, 1, 0), None, None, True, rules, TZ)["locked"] is False
    assert policy.evaluate(ts(2026, 10, 3, 3, 0), None, None, True, rules, TZ)["locked"] is True
    assert policy.evaluate(ts(2026, 10, 2, 23, 0), None, None, True, rules, TZ)["locked"] is False


def test_adjacent_windows_do_not_flicker():
    rules = [
        {"days": WEEKDAYS, "start": "08:00", "end": "12:00"},
        {"days": WEEKDAYS, "start": "12:00", "end": "16:00"},
    ]
    state = policy.evaluate(ts(2026, 9, 28, 10, 0), None, None, True, rules, TZ)
    assert state["changes_at"] == ts(2026, 9, 28, 16, 0)


def test_schedule_disabled_means_unlocked_forever():
    state = policy.evaluate(ts(2026, 9, 28, 3, 0), None, None, False, OFFICE, TZ)
    assert state["locked"] is False and state["changes_in"] is None


def test_enabled_but_empty_schedule_is_always_locked():
    state = policy.evaluate(ts(2026, 9, 28, 10, 0), None, None, True, [], TZ)
    assert state["locked"] is True and state["changes_in"] is None


def test_pc_override_beats_global_and_schedule():
    now = ts(2026, 9, 28, 10, 0)
    lock = {"mode": "lock", "until": now + 1800}
    state = policy.evaluate(now, lock, {"mode": "unlock", "until": None}, True, OFFICE, TZ)
    assert state["locked"] and state["source"] == "manual"
    assert state["changes_in"] == 1800  # then global unlock applies


def test_lock_override_expiring_into_locked_schedule_reports_real_unlock_time():
    now = ts(2026, 9, 28, 17, 0)
    override = {"mode": "lock", "until": now + 600}
    state = policy.evaluate(now, override, None, True, OFFICE, TZ)
    assert state["locked"] and state["changes_at"] == ts(2026, 9, 29, 8, 0)


def test_unlock_override_outside_hours_relocks_when_it_ends():
    now = ts(2026, 9, 28, 20, 0)
    override = {"mode": "unlock", "until": now + 3600}
    state = policy.evaluate(now, override, None, True, OFFICE, TZ)
    assert state["locked"] is False and state["changes_in"] == 3600


def test_expired_override_is_ignored():
    now = ts(2026, 9, 28, 17, 0)
    state = policy.evaluate(now, {"mode": "unlock", "until": now - 1}, None, True, OFFICE, TZ)
    assert state["locked"] and state["source"] == "schedule"


def test_indefinite_override_never_changes():
    state = policy.evaluate(ts(2026, 9, 28, 10, 0), {"mode": "lock", "until": None}, None, False, [], TZ)
    assert state["locked"] and state["changes_in"] is None


@pytest.mark.parametrize("rule", [
    {"days": [], "start": "08:00", "end": "09:00"},
    {"days": [7], "start": "08:00", "end": "09:00"},
    {"days": [1], "start": "25:00", "end": "09:00"},
    {"days": [1], "start": "08:00", "end": "08:00"},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(ValueError):
        policy.validate_rules([rule])


def test_daily_rule_opens_every_day_including_weekend():
    rules = [{"type": "daily", "start": "10:00", "end": "12:00"}]
    assert policy.evaluate(ts(2026, 10, 3, 11, 0), None, None, True, rules, TZ)["locked"] is False  # Saturday
    state = policy.evaluate(ts(2026, 10, 3, 13, 0), None, None, True, rules, TZ)
    assert state["locked"] and state["changes_at"] == ts(2026, 10, 4, 10, 0)


def test_once_rule_only_on_that_date():
    rules = [{"type": "once", "date": "2026-10-04", "start": "09:00", "end": "11:00"}]  # a Sunday
    assert policy.evaluate(ts(2026, 10, 4, 10, 0), None, None, True, rules, TZ)["locked"] is False
    assert policy.evaluate(ts(2026, 10, 11, 10, 0), None, None, True, rules, TZ)["locked"] is True  # a week later
    state = policy.evaluate(ts(2026, 10, 1, 10, 0), None, None, True, rules, TZ)
    assert state["locked"] and state["changes_at"] == ts(2026, 10, 4, 9, 0)


def test_once_rule_far_in_the_future_is_still_found():
    rules = [{"type": "once", "date": "2026-12-25", "start": "09:00", "end": "11:00"}]
    state = policy.evaluate(ts(2026, 9, 28, 10, 0), None, None, True, rules, TZ)
    assert state["locked"] and state["changes_at"] == ts(2026, 12, 25, 9, 0)


def test_once_rule_past_midnight_and_expired_once_rule():
    rules = [{"type": "once", "date": "2026-10-04", "start": "22:00", "end": "01:00"}]
    assert policy.evaluate(ts(2026, 10, 5, 0, 30), None, None, True, rules, TZ)["locked"] is False
    state = policy.evaluate(ts(2026, 10, 6, 10, 0), None, None, True, rules, TZ)
    assert state["locked"] and state["changes_in"] is None


def test_rules_without_type_still_mean_weekly():
    assert policy.evaluate(ts(2026, 9, 28, 10, 0), None, None, True, OFFICE, TZ)["locked"] is False
    assert policy.evaluate(ts(2026, 10, 3, 10, 0), None, None, True, OFFICE, TZ)["locked"] is True


def test_schedule_follows_chosen_timezone():
    rules = [{"type": "daily", "start": "08:00", "end": "16:00"}]
    moment = datetime(2026, 9, 28, 1, 30, tzinfo=ZoneInfo("UTC")).timestamp()   # 08:30 WIB, 09:30 WITA
    assert policy.evaluate(moment, None, None, True, rules, ZoneInfo("Asia/Jakarta"))["locked"] is False
    late = datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("UTC")).timestamp()      # 16:00 WIB, 17:00 WITA
    assert policy.evaluate(late, None, None, True, rules, ZoneInfo("Asia/Jakarta"))["locked"] is True
    assert policy.evaluate(late, None, None, True, rules, ZoneInfo("Asia/Makassar"))["locked"] is True
    early = datetime(2026, 9, 28, 0, 30, tzinfo=ZoneInfo("UTC")).timestamp()    # 07:30 WIB, 08:30 WITA
    assert policy.evaluate(early, None, None, True, rules, ZoneInfo("Asia/Jakarta"))["locked"] is True
    assert policy.evaluate(early, None, None, True, rules, ZoneInfo("Asia/Makassar"))["locked"] is False


def test_lock_message_shows_date_when_far_away():
    now = ts(2026, 9, 28, 10, 0)
    state = {"source": "schedule", "changes_at": ts(2026, 12, 25, 9, 0)}
    assert "25/12/2026 09:00" in policy.lock_message(state, TZ, now)
    soon = {"source": "schedule", "changes_at": ts(2026, 9, 29, 8, 0)}
    assert "Sel 08:00" in policy.lock_message(soon, TZ, now)


@pytest.mark.parametrize("rule", [
    {"type": "once", "start": "08:00", "end": "09:00"},
    {"type": "once", "date": "2026-02-30", "start": "08:00", "end": "09:00"},
    {"type": "yearly", "start": "08:00", "end": "09:00"},
])
def test_invalid_new_rule_types_rejected(rule):
    with pytest.raises(ValueError):
        policy.validate_rules([rule])
