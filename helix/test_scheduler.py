#!/usr/bin/env python3
"""Scheduling: Odin starting work on his own, at a time.

Roadmap #2 asks for overnight projects. Until now Odin could only react — every turn began
with the user. The work lane (2026-09-06) gave him somewhere to run without muting chat; this
gives him a reason to start.

The due-logic lives in scheduler.py as pure functions so it can be tested without threads,
clocks, or model calls. app.py owns only the ticker thread and the routes.

Run:  .venv/bin/python test_scheduler.py
"""
import os, sys, tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scheduler  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def dt(s):
    return datetime.fromisoformat(s)


# ── interval schedules ────────────────────────────────────────────────────────

def test_interval_is_due_once_the_gap_has_passed():
    s = {"every_minutes": 60, "last_run": "2026-09-06T03:00:00"}
    check("not due after 59 minutes", not scheduler.is_due(s, dt("2026-09-06T03:59:00")))
    check("due after 60 minutes", scheduler.is_due(s, dt("2026-09-06T04:00:00")))


def test_an_interval_that_never_ran_is_due_immediately():
    s = {"every_minutes": 60, "last_run": None}
    check("a fresh interval schedule is due", scheduler.is_due(s, dt("2026-09-06T03:00:00")))


# ── daily-at schedules ────────────────────────────────────────────────────────

def test_daily_at_fires_after_its_time_and_only_once():
    s = {"at": "03:00", "last_run": None}
    check("not due at 02:59", not scheduler.is_due(s, dt("2026-09-06T02:59:00")))
    check("due at 03:00", scheduler.is_due(s, dt("2026-09-06T03:00:00")))
    s2 = dict(s, last_run="2026-09-06T03:00:30")
    check("not due again the same day", not scheduler.is_due(s2, dt("2026-09-06T09:00:00")))
    check("due again the next day", scheduler.is_due(s2, dt("2026-09-07T03:00:00")))


def test_a_missed_daily_run_does_not_backfill():
    """Helix was off overnight. On boot it must not fire a pile of catch-up jobs."""
    s = {"at": "03:00", "last_run": "2026-09-01T03:00:00"}
    # five days later, well past 03:00 — should fire ONCE, not five times
    check("a long-missed daily is due once", scheduler.is_due(s, dt("2026-09-06T09:00:00")))
    after = dict(s, last_run="2026-09-06T09:00:01")
    check("and not again that day", not scheduler.is_due(after, dt("2026-09-06T23:00:00")))


# ── enabling / validation ─────────────────────────────────────────────────────

def test_a_disabled_schedule_is_never_due():
    s = {"every_minutes": 1, "last_run": None, "enabled": False}
    check("disabled never fires", not scheduler.is_due(s, dt("2026-09-06T03:00:00")))


def test_a_schedule_with_no_trigger_is_never_due():
    check("no trigger never fires", not scheduler.is_due({"last_run": None}, dt("2026-09-06T03:00:00")))


def test_validate_rejects_nonsense():
    ok, _ = scheduler.validate({"name": "n", "prompt": "p", "every_minutes": 60})
    check("a sane interval schedule validates", ok)
    ok2, err2 = scheduler.validate({"name": "n", "prompt": "", "every_minutes": 60})
    check("an empty prompt is rejected", not ok2, err2)
    ok3, err3 = scheduler.validate({"name": "n", "prompt": "p"})
    check("no trigger is rejected", not ok3, err3)
    ok4, err4 = scheduler.validate({"name": "n", "prompt": "p", "at": "25:00"})
    check("a bad clock time is rejected", not ok4, err4)
    ok5, err5 = scheduler.validate({"name": "n", "prompt": "p", "every_minutes": 0})
    check("a zero interval is rejected", not ok5, err5)


def test_minimum_interval_is_enforced():
    """A one-minute agent job would stampede the work lane and burn the subscription."""
    ok, err = scheduler.validate({"name": "n", "prompt": "p", "every_minutes": 2})
    check("an interval below the floor is rejected", not ok, err)


# ── the store ─────────────────────────────────────────────────────────────────

def test_store_round_trips_and_survives_a_missing_file():
    d = tempfile.mkdtemp(); p = os.path.join(d, "schedules.json")
    check("a missing store reads as empty", scheduler.load(p) == [])
    items = [{"id": "a", "name": "sweep", "prompt": "do it", "every_minutes": 60,
              "enabled": True, "last_run": None}]
    scheduler.save(p, items)
    check("what was saved comes back", scheduler.load(p) == items, f"got {scheduler.load(p)}")


def test_a_corrupt_store_does_not_take_helix_down():
    d = tempfile.mkdtemp(); p = os.path.join(d, "schedules.json")
    open(p, "w").write("{ this is not json")
    check("a corrupt store reads as empty rather than raising", scheduler.load(p) == [])


def test_due_list_picks_only_the_ready_ones():
    now = dt("2026-09-06T04:00:00")
    items = [
        {"id": "ready",   "every_minutes": 60, "last_run": "2026-09-06T03:00:00", "enabled": True},
        {"id": "waiting", "every_minutes": 60, "last_run": "2026-09-06T03:59:00", "enabled": True},
        {"id": "off",     "every_minutes": 60, "last_run": None, "enabled": False},
    ]
    ids = [s["id"] for s in scheduler.due(items, now)]
    check("only the ready schedule is returned", ids == ["ready"], f"got {ids}")


# ── app integration: the ticker actually starts jobs ──────────────────────────

def _app():
    os.environ["HELIX_NO_WARMUP"] = "1"; os.environ["HELIX_NO_PRECONNECT"] = "1"
    # Order 32: without this, importing app starts the real ticker (_scheduler_loop) on the live
    # memory/schedules.json, and a due schedule would start a real job. Tests drive scheduler_tick.
    os.environ["HELIX_NO_SCHEDULER"] = "1"
    import app
    return app


def test_importing_app_starts_no_real_scheduler_thread():
    import threading
    app = _app()
    live = [t.name for t in threading.enumerate()
            if getattr(t, "_target", None) is app._scheduler_loop and t.is_alive()]
    check("no _scheduler_loop thread is running in the test process", live == [], f"threads={live}")


def test_a_due_schedule_starts_a_job_and_records_the_run():
    app = _app()
    d = tempfile.mkdtemp(); store = os.path.join(d, "schedules.json")
    real_store, app.SCHEDULES = app.SCHEDULES, store
    started = []
    real_start, app.start_job = app.start_job, lambda text, **k: started.append(text) or type(
        "B", (), {"id": "job1"})()
    try:
        scheduler.save(store, [{"id": "s1", "name": "sweep", "prompt": "run the sweep",
                                "every_minutes": 60, "enabled": True, "last_run": None}])
        app.scheduler_tick()
        check("the due schedule started a job", started == ["run the sweep"], f"started={started}")
        after = scheduler.load(store)
        check("last_run was recorded", after[0]["last_run"] is not None)
        check("the turn id was recorded", after[0].get("last_turn_id") == "job1", f"got {after[0]}")

        started.clear()
        app.scheduler_tick()
        check("it does not fire again immediately", started == [], f"started={started}")
    finally:
        app.start_job, app.SCHEDULES = real_start, real_store


def test_the_ticker_skips_while_the_work_lane_is_busy():
    """The work lane is serial. A due schedule must skip, not queue up a backlog."""
    app = _app()
    d = tempfile.mkdtemp(); store = os.path.join(d, "schedules.json")
    real_store, app.SCHEDULES = app.SCHEDULES, store
    started = []
    real_start, app.start_job = app.start_job, lambda text, **k: started.append(text) or type(
        "B", (), {"id": "j"})()
    app.WORK.lock.acquire()
    try:
        scheduler.save(store, [{"id": "s1", "name": "n", "prompt": "p", "every_minutes": 60,
                                "enabled": True, "last_run": None}])
        app.scheduler_tick()
        check("nothing started while WORK was busy", started == [], f"started={started}")
        check("and last_run was NOT stamped", scheduler.load(store)[0]["last_run"] is None)
    finally:
        app.WORK.lock.release()
        app.start_job, app.SCHEDULES = real_start, real_store


def test_schedule_routes_create_list_and_delete():
    app = _app()
    d = tempfile.mkdtemp(); store = os.path.join(d, "schedules.json")
    real_store, app.SCHEDULES = app.SCHEDULES, store
    try:
        with app.app.test_client() as tc:
            r = tc.post("/schedules", json={"name": "nightly", "prompt": "sweep",
                                            "at": "03:30"}).get_json()
            check("a schedule can be created", r.get("ok") is True, f"{r}")
            sid = r.get("id")
            lst = tc.get("/schedules").get_json()
            check("it appears in the list", any(x["id"] == sid for x in lst["schedules"]), f"{lst}")

            bad = tc.post("/schedules", json={"name": "x", "prompt": "y", "every_minutes": 1})
            check("the interval floor is enforced by the route", bad.status_code == 400)

            tc.post(f"/schedules/{sid}/toggle")
            off = tc.get("/schedules").get_json()["schedules"][0]
            check("toggle disables it", off["enabled"] is False, f"{off}")

            tc.delete(f"/schedules/{sid}")
            check("delete removes it", tc.get("/schedules").get_json()["schedules"] == [])
    finally:
        app.SCHEDULES = real_store


def test_metrics_reports_schedules():
    app = _app()
    with app.app.test_client() as tc:
        j = tc.get("/metrics").get_json()
    check("/metrics reports how many schedules exist", "schedules" in j, f"keys={sorted(j.keys())}")


print("\nHELIX — scheduler (Odin starts work on his own)\n" + "=" * 52)
test_interval_is_due_once_the_gap_has_passed()
test_an_interval_that_never_ran_is_due_immediately()
test_daily_at_fires_after_its_time_and_only_once()
test_a_missed_daily_run_does_not_backfill()
test_a_disabled_schedule_is_never_due()
test_a_schedule_with_no_trigger_is_never_due()
test_validate_rejects_nonsense()
test_minimum_interval_is_enforced()
test_store_round_trips_and_survives_a_missing_file()
test_a_corrupt_store_does_not_take_helix_down()
test_due_list_picks_only_the_ready_ones()
test_a_due_schedule_starts_a_job_and_records_the_run()
test_the_ticker_skips_while_the_work_lane_is_busy()
test_schedule_routes_create_list_and_delete()
test_metrics_reports_schedules()
test_importing_app_starts_no_real_scheduler_thread()
print("=" * 52)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
