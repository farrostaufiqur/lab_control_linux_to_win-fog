"""Decides whether a PC should be locked, and when that will next change.

Three layers, strongest first:
  1. per-PC override   {"mode": "lock"|"unlock", "until": epoch|None}
  2. global override   (same shape, applies to every PC)
  3. schedule          list of open windows, each {"type", "start": "HH:MM", "end": "HH:MM", ...}:
                         "weekly" (default) + "days": [0-6 Mon..Sun]  -> every week on those days
                         "daily"                                      -> every day
                         "once"   + "date": "YYYY-MM-DD"              -> that date only
                       a window whose end <= start runs past midnight.

An override with `until` stops applying at that time and the layer below decides.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DAY_NAMES = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
HORIZON_DAYS = 9
RULE_TYPES = ("weekly", "daily", "once")


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def validate_rules(rules) -> list[dict]:
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("rules harus berupa daftar (maks 100)")
    clean = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("aturan jadwal tidak valid")
        kind = rule.get("type", "weekly")
        if kind not in RULE_TYPES:
            raise ValueError(f"jenis jadwal tidak dikenal: {kind}")
        extra_field = {"weekly": "days", "once": "date"}.get(kind)
        missing = [k for k in ("start", "end", extra_field) if k and k not in rule]
        if missing:
            raise ValueError(f"isian jadwal kurang: {missing[0]}")
        start, end = str(rule["start"]), str(rule["end"])
        for value in (start, end):
            minutes = _minutes(value)
            if not 0 <= minutes < 24 * 60:
                raise ValueError(f"jam tidak valid: {value}")
        if _minutes(start) == _minutes(end):
            raise ValueError("jam mulai dan selesai tidak boleh sama")
        item = {"type": kind, "start": start, "end": end}
        if kind == "weekly":
            days = sorted({int(d) for d in rule["days"]})
            if not days or any(d < 0 or d > 6 for d in days):
                raise ValueError("hari harus 0 (Senin) sampai 6 (Minggu)")
            item["days"] = days
        elif kind == "once":
            item["date"] = date.fromisoformat(str(rule["date"])).isoformat()
        clean.append(item)
    return clean


def _span(day: date, rule: dict, tz: ZoneInfo) -> tuple[float, float]:
    start_min, end_min = _minutes(rule["start"]), _minutes(rule["end"])
    length = end_min - start_min if end_min > start_min else end_min + 24 * 60 - start_min
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    # wall-clock arithmetic keeps the window at the same local time across DST changes
    return ((midnight + timedelta(minutes=start_min)).timestamp(),
            (midnight + timedelta(minutes=start_min + length)).timestamp())


def open_intervals(rules: list[dict], now_ts: float, tz: ZoneInfo) -> list[tuple[float, float]]:
    """Merged [start, end) epoch intervals when the lab is open, around `now_ts`."""
    today = datetime.fromtimestamp(now_ts, tz).date()
    spans = []
    for offset in range(-1, HORIZON_DAYS):
        day = today + timedelta(days=offset)
        for rule in rules:
            kind = rule.get("type", "weekly")
            if kind == "daily" or (kind == "weekly" and day.weekday() in rule["days"]):
                spans.append(_span(day, rule, tz))
    for rule in rules:
        if rule.get("type") == "once":  # one-off dates may lie beyond the horizon; keep them all
            day = date.fromisoformat(rule["date"])
            if day >= today - timedelta(days=1):
                spans.append(_span(day, rule, tz))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for begin, end in spans:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


def _override_active(override, at: float) -> bool:
    if not override:
        return False
    until = override.get("until")
    return until is None or at < until


def _locked_at(at, pc_override, global_override, schedule_enabled, intervals):
    if _override_active(pc_override, at):
        return pc_override["mode"] == "lock", "manual"
    if _override_active(global_override, at):
        return global_override["mode"] == "lock", "global"
    if not schedule_enabled:
        return False, "schedule"
    return not any(begin <= at < end for begin, end in intervals), "schedule"


def evaluate(now_ts, pc_override, global_override, schedule_enabled, rules, tz: ZoneInfo) -> dict:
    """Returns {"locked", "source", "changes_at", "changes_in"} (changes_* None if it never changes)."""
    intervals = open_intervals(rules, now_ts, tz) if schedule_enabled else []
    locked, source = _locked_at(now_ts, pc_override, global_override, schedule_enabled, intervals)

    breakpoints = {edge for interval in intervals for edge in interval}
    for override in (pc_override, global_override):
        if override and override.get("until") is not None:
            breakpoints.add(override["until"])

    changes_at = None
    for point in sorted(p for p in breakpoints if p > now_ts):
        state, _ = _locked_at(point, pc_override, global_override, schedule_enabled, intervals)
        if state != locked:
            changes_at = point
            break
    return {
        "locked": locked,
        "source": source,
        "changes_at": changes_at,
        "changes_in": None if changes_at is None else max(0, int(changes_at - now_ts)),
    }


def describe_time(ts: float, tz: ZoneInfo, now: float | None = None) -> str:
    local = datetime.fromtimestamp(ts, tz)
    if now is not None and ts - now > 6 * 24 * 3600:
        return f"{local:%d/%m/%Y %H:%M}"
    return f"{DAY_NAMES[local.weekday()]} {local:%H:%M}"


def lock_message(state: dict, tz: ZoneInfo, now: float | None = None) -> str:
    if state["source"] == "manual":
        reason = "Komputer ini dikunci oleh admin."
    elif state["source"] == "global":
        reason = "Semua komputer sedang dikunci oleh admin."
    else:
        reason = "Saat ini di luar jadwal sesi lab."
    if state["changes_at"] is None:
        return reason + " Tunggu admin membuka kunci."
    return f"{reason} Kunci dibuka {describe_time(state['changes_at'], tz, now)}."
