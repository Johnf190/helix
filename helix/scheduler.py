"""When Odin should start work on his own.

Pure functions plus a JSON store — no threads, no clock of its own, no model calls. app.py
owns the ticker and the routes; everything decidable is decided here so it can be tested
against a fixed `now`.

Two trigger shapes, deliberately not full cron:
  {"every_minutes": N}  — run every N minutes
  {"at": "HH:MM"}       — run once a day at that local time

Cron's expressiveness is not the missing piece; a reason to start is. If a real need for
"every other Tuesday" turns up, add it then.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

# A schedule that fires every couple of minutes would stampede the serial work lane and burn
# the subscription while the user sleeps. Agent turns cost 10-60s each; anything under a quarter
# hour is almost certainly a mistake rather than an intention.
MIN_INTERVAL_MINUTES = 15


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _parse_at(s):
    """'HH:MM' -> (h, m), or None if it isn't a real clock time."""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
    except Exception:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def is_due(sched, now):
    """Should this schedule run at `now`?"""
    if not sched.get("enabled", True):
        return False

    last = _parse(sched.get("last_run"))

    every = sched.get("every_minutes")
    if every:
        if last is None:
            return True                       # never run: start the cycle now
        return now - last >= timedelta(minutes=every)

    at = _parse_at(sched.get("at"))
    if at:
        h, m = at
        today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < today:
            return False                      # its time has not come round yet today
        # Due since `today`. Fire only if the last run was before it — which also means a run
        # missed while Helix was off fires ONCE on return, never a backlog of catch-ups.
        return last is None or last < today

    return False                              # no trigger: nothing to fire


def due(items, now):
    return [s for s in items if is_due(s, now)]


def validate(sched):
    """(ok, error). Checked before anything reaches the store."""
    if not str(sched.get("prompt", "")).strip():
        return False, "prompt is empty"
    if not str(sched.get("name", "")).strip():
        return False, "name is empty"

    every, at = sched.get("every_minutes"), sched.get("at")
    if every in (None, "") and at in (None, ""):
        return False, "needs every_minutes or at"
    if every not in (None, ""):
        try:
            every = int(every)
        except Exception:
            return False, "every_minutes must be a number"
        if every < MIN_INTERVAL_MINUTES:
            return False, f"every_minutes must be at least {MIN_INTERVAL_MINUTES}"
    if at not in (None, "") and _parse_at(at) is None:
        return False, "at must be HH:MM"
    return True, ""


def load(path):
    """Read the store. A missing or corrupt file reads as empty rather than raising —
    a bad schedules file must never be the reason Helix will not boot."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save(path, items):
    """Write atomically, so a crash mid-write cannot leave a truncated store behind."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
