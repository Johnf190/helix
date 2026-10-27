"""Tests for ORDER 31 — the nightly restart (item 20).

Three parts:
  A. the trigger in app.py: fake clock, the real lane locks, fake probes, a recording launcher.
  B. nightly_restart.sh against a FAKE app: a small HTTP server on a temp port, a temp app.py,
     temp ledger, temp .lkg and a temp Desktop, all under one temp root (HELIX_NR_TEST_ROOT).
  C. the announcement on boot: a fake ledger, a temp session dir.

🔴 Nothing here starts or kills the live app, or touches memory/sessions/log.md, the live
ledgers or ~/Desktop: every test points app.SESS and the ledger at tmp_path, the launcher is a
recorder (A) or a fake (B), and the script refuses a test root that is the real helix dir.

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_nightly_restart.py
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402

SCRIPT = HELIX / "nightly_restart.sh"
NIGHTLY = "3810a43ef3c7"


# ======================================================================= shared

class Clock:
    """A fake local clock. sleep() moves it; nothing really waits."""

    def __init__(self, s):
        self.t = datetime.fromisoformat(s)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += timedelta(seconds=s)


class Buf:
    """Stands in for the nightly's TurnBuf."""

    def __init__(self, status="done", announced=True):
        self.id = "nightlybuf"
        self.status = status
        self.announced = announced


def read_ledger(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def chain_ok(p):
    lines = Path(p).read_text().splitlines()
    for i, l in enumerate(lines):
        rec = json.loads(l)
        want = "genesis" if i == 0 else hashlib.sha256(lines[i - 1].encode()).hexdigest()
        if rec.get("seq") != i + 1 or rec.get("prev") != want:
            return False
    return True


@pytest.fixture
def nr(tmp_path, monkeypatch):
    """Temp ledger + temp session dir + a recording launcher + an idle machine."""
    led = tmp_path / "RESTARTS.jsonl"
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(app, "RESTARTS_LEDGER", str(led))
    monkeypatch.setattr(app, "SESS", str(sess))
    monkeypatch.setattr(app, "_NR_LAUNCHED", set())
    calls = []
    monkeypatch.setattr(app, "_nightly_restart_launch", lambda: calls.append("launch"))
    monkeypatch.setattr(app, "_builder_active", lambda: (None, None))
    monkeypatch.setattr(app, "_queue_read", lambda: None)
    monkeypatch.setattr(app, "_nr_live_attempts", lambda path=None: [])
    monkeypatch.setattr(app, "_nr_gpu_others", lambda: [])
    return {"ledger": led, "sess": sess, "calls": calls}


# ======================================================================= A. the trigger

def test_constants_are_code_constants():
    assert app.NIGHTLY_RESTART_SCHEDULE_ID == NIGHTLY
    assert app.NIGHTLY_RESTART_GRACE_S == 30
    assert app.NIGHTLY_RESTART_RECHECK_S == 300
    assert (app._NR_FROM.isoformat(), app._NR_GIVE_UP.isoformat()) == ("04:00:00", "06:59:00")


def test_the_ledger_is_off_when_app_is_imported():
    assert app.RESTARTS_LEDGER is None


def test_nightly_done_at_0431_restarts_once(nr):
    c = Clock("2026-10-03T04:31:00")
    out = app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    assert out == "started" and nr["calls"] == ["launch"]
    assert c.slept == [30]                                   # the grace, and nothing else
    [trig] = read_ledger(nr["ledger"])
    assert trig["event"] == "trigger" and trig["reason"] == "nightly"
    assert trig["local"] == "2026-10-03T04:31:30" and trig["pid"] == os.getpid()
    assert trig["running_sha"] == app._BOOT_APP_SHA and trig["prev"] == "genesis"


def test_the_same_at_noon_does_not_restart(nr):
    c = Clock("2026-10-03T12:00:00")
    out = app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    assert nr["calls"] == []
    assert out == "skipped: outside 04:00–06:59 (12:00)"
    recs = read_ledger(nr["ledger"])
    assert [r["event"] for r in recs] == ["skipped", "announced"]


@pytest.mark.parametrize("hhmm", ["03:59", "07:00", "23:30"])
def test_outside_the_window_never_restarts(nr, hhmm):
    c = Clock(f"2026-10-03T{hhmm}:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep).startswith("skipped: outside")
    assert nr["calls"] == []


def test_a_second_time_the_same_day_is_skipped(nr):
    c = Clock("2026-10-03T04:31:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "started"
    c.t = datetime.fromisoformat("2026-10-03T05:10:00")
    out = app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    assert out == "skipped: already ran today" and nr["calls"] == ["launch"]
    assert read_ledger(nr["ledger"])[-2]["reason"] == "already ran today"
    assert chain_ok(nr["ledger"])


def test_already_ran_today_is_read_from_the_ledger_not_memory(nr, monkeypatch):
    """A fresh process (empty _NR_LAUNCHED) still sees today's start line."""
    app._nr_append(str(nr["ledger"]), {"event": "start", "reason": "nightly"},
                   datetime.fromisoformat("2026-10-03T04:31:40"))
    c = Clock("2026-10-03T04:45:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "skipped: already ran today"
    assert nr["calls"] == []


def test_the_next_day_runs_again(nr):
    c = Clock("2026-10-03T04:31:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "started"
    c.t = datetime.fromisoformat("2026-10-04T04:31:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "started"
    assert nr["calls"] == ["launch", "launch"]


def test_a_busy_lane_waits_then_gives_up_at_0659(nr):
    c = Clock("2026-10-03T04:31:00")
    app.WORK.lock.acquire()
    try:
        out = app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    finally:
        app.WORK.lock.release()
    assert out == "skipped: busy (WORK)" and nr["calls"] == []
    assert c.t.strftime("%H:%M:%S") == "06:59:00"
    assert c.slept[0] == 30 and set(c.slept[1:-1]) == {300}  # re-checked every 5 minutes
    skip = [r for r in read_ledger(nr["ledger"]) if r["event"] == "skipped"]
    assert skip == [dict(skip[0], reason="busy (WORK)", local="2026-10-03T06:59:00")]


def test_a_busy_lane_that_frees_up_is_restarted_after_it(nr, monkeypatch):
    c = Clock("2026-10-03T04:31:00")
    seen = []

    def busy():
        seen.append(c.t)
        return ["WORK"] if len(seen) < 3 else []
    monkeypatch.setattr(app, "_nightly_restart_busy", busy)
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "started"
    assert nr["calls"] == ["launch"] and c.t.strftime("%H:%M:%S") == "04:41:30"


@pytest.mark.parametrize("lane", ["CHAT", "WORK", "SCHED"])
def test_every_named_lane_counts_as_busy(nr, lane):
    l = getattr(app, lane)
    l.lock.acquire()
    try:
        assert app._nightly_restart_busy() == [lane]
    finally:
        l.lock.release()
    assert app._nightly_restart_busy() == []


def test_the_other_busy_probes_name_what_was_busy(nr, monkeypatch):
    monkeypatch.setattr(app, "_builder_active", lambda: (4242, "helix-builder-31"))
    monkeypatch.setattr(app, "_queue_read", lambda: {"status": "running", "pid": os.getpid()})
    monkeypatch.setattr(app, "_nr_live_attempts", lambda path=None: ["attempt pid 77 (demo/x)"])
    monkeypatch.setattr(app, "_nr_gpu_others", lambda: ["GPU pid 88"])
    assert app._nightly_restart_busy() == ["builder pid 4242", "build queue",
                                           "attempt pid 77 (demo/x)", "GPU pid 88"]


def test_waits_for_the_nightly_to_finish_and_be_pushed(nr):
    c = Clock("2026-10-03T04:30:00")
    b = Buf(status="running", announced=False)
    ticks = []

    def sleep(s):
        c.sleep(s)
        ticks.append(s)
        if len(ticks) == 3:
            b.status = "done"                    # finished, but not yet logged and pushed
        if len(ticks) == 5:
            b.announced = True
    assert app._nightly_restart_after(b, now=c.now, sleep=sleep) == "started"
    assert ticks[:5] == [10] * 5 and ticks[5] == 30 and nr["calls"] == ["launch"]


def test_an_errored_nightly_still_counts_as_finished(nr):
    c = Clock("2026-10-03T04:31:00")
    assert app._nightly_restart_after(Buf(status="error"), now=c.now, sleep=c.sleep) == "started"


def test_a_nightly_still_running_at_0659_is_a_skip(nr):
    c = Clock("2026-10-03T06:50:00")
    out = app._nightly_restart_after(Buf(status="running", announced=False), now=c.now, sleep=c.sleep)
    assert out == "skipped: the nightly had not finished by 06:59" and nr["calls"] == []


def test_the_scheduler_arms_only_for_the_nightly_id(nr, tmp_path, monkeypatch):
    import scheduler
    store = tmp_path / "schedules.json"
    monkeypatch.setattr(app, "SCHEDULES", str(store))
    monkeypatch.setattr(app, "start_job", lambda text, **k: Buf(status="running"))
    armed = []
    monkeypatch.setattr(app, "_nightly_restart_arm", lambda buf: armed.append(buf.id))
    scheduler.save(str(store), [{"id": "aaaaaaaaaaaa", "name": "other", "prompt": "p",
                                 "every_minutes": 60, "enabled": True, "last_run": None}])
    app.scheduler_tick()
    assert armed == []                                       # another schedule id: never armed
    scheduler.save(str(store), [{"id": NIGHTLY, "name": "nightly", "prompt": "p",
                                 "every_minutes": 60, "enabled": True, "last_run": None}])
    app.scheduler_tick()
    assert armed == ["nightlybuf"]


def test_the_run_now_route_never_arms(nr, tmp_path, monkeypatch):
    import scheduler
    store = tmp_path / "schedules.json"
    monkeypatch.setattr(app, "SCHEDULES", str(store))
    monkeypatch.setattr(app, "start_job", lambda text, **k: Buf(status="running"))
    armed = []
    monkeypatch.setattr(app, "_nightly_restart_arm", lambda buf: armed.append(buf.id))
    scheduler.save(str(store), [{"id": NIGHTLY, "name": "nightly", "prompt": "p",
                                 "at": "04:30", "enabled": True, "last_run": None}])
    with app.app.test_client() as tc:
        assert tc.post(f"/schedules/{NIGHTLY}/run").get_json()["ok"] is True
    assert armed == []


def test_no_route_reaches_the_restart():
    """No view function names any nightly-restart function (directly)."""
    names = {n for n in dir(app) if n.startswith(("_nightly_restart", "_nr_"))}
    for rule in app.app.url_map.iter_rules():
        fn = app.app.view_functions[rule.endpoint]
        assert not names & set(fn.__code__.co_names), rule.rule
    assert "_nightly_restart_arm" in app.scheduler_tick.__code__.co_names


def test_arming_does_nothing_with_the_ledger_off(monkeypatch):
    monkeypatch.setattr(app, "RESTARTS_LEDGER", None)
    monkeypatch.setattr(app, "_nightly_restart_launch",
                        lambda: (_ for _ in ()).throw(AssertionError("launched")))
    assert app._nightly_restart_arm(Buf()) is None
    assert app._nightly_restart_after(Buf()) == "off"


def test_no_env_var_or_setting_is_read_by_the_trigger():
    for fn in (app._nightly_restart_after, app._nightly_restart_try, app._nr_in_window,
               app._nr_ran_today, app._nightly_restart_busy):
        assert "environ" not in fn.__code__.co_names and "getenv" not in fn.__code__.co_names


def test_the_launcher_starts_the_script_in_its_own_session(monkeypatch):
    seen = []

    class P:
        def __init__(self, argv, **kw):
            seen.append((argv, kw))
    monkeypatch.setattr(subprocess, "Popen", P)
    monkeypatch.setenv("HELIX_NR_TEST_ROOT", "/tmp/should-not-pass")
    app._nightly_restart_launch()
    [(argv, kw)] = seen
    assert argv == ["bash", str(HELIX / "nightly_restart.sh")]
    assert kw["start_new_session"] is True and kw["env"]["HELIX_NO_BROWSER"] == "1"
    assert not [k for k in kw["env"] if k.startswith("HELIX_NR_TEST")]


def test_a_failed_launch_is_a_skip_and_does_not_retry(nr, monkeypatch):
    def boom():
        raise OSError("no bash")
    monkeypatch.setattr(app, "_nightly_restart_launch", boom)
    c = Clock("2026-10-03T04:31:00")
    out = app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    assert out == "skipped: could not start nightly_restart.sh (OSError: no bash)"
    c.t = datetime.fromisoformat("2026-10-03T05:00:00")
    assert app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep) == "skipped: already ran today"


# ---- the idle probes themselves

def test_gpu_probe_tells_resident_processes_apart(monkeypatch):
    rd = str(HELIX / "render_daemon.py")
    cmds = {os.getpid(): ["python", "app.py"], 11: ["/home/x/render_env/bin/python", rd],
            12: ["python3", "train.py"], 13: ["python3", "/tmp/helix/render_daemon.py"]}
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: list(cmds))
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (cmds[pid], "/"))
    assert app._nr_gpu_others() == ["GPU pid 12", "GPU pid 13"]


def test_gpu_probe_counts_a_relative_render_daemon_by_its_cwd(monkeypatch):
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: [21])
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (["python", "render_daemon.py"], str(HELIX)))
    assert app._nr_gpu_others() == []


def test_gpu_probe_fails_closed(monkeypatch):
    def boom():
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(app, "_nr_gpu_pids", boom)
    assert app._nr_gpu_others() == ["GPU unknown (nvidia-smi failed: FileNotFoundError)"]


def test_gpu_probe_ignores_browser_gpu_processes_but_not_real_work(monkeypatch):
    cmds = {31: ["/opt/google/chrome/chrome", "--type=gpu-process", "--crashpad-handler-pid=1"],
            32: ["/opt/brave.com/brave/brave", "--type=gpu-process", "--field-trial-handle=3"],
            33: ["python3", "train.py"]}
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: list(cmds))
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (cmds[pid], "/"))
    assert app._nr_gpu_others() == ["GPU pid 33"]


def test_gpu_probe_ignores_the_real_one_string_browser_cmdline(monkeypatch):
    # Chrome rewrites its child's process title: /proc/<pid>/cmdline is one space-joined string.
    cmds = {34: ["/opt/google/chrome/chrome --type=gpu-process --gpu-recent-crash-count=0 "
                 "--user-data-dir=/home/x/.config/helix-chrome-profile"],
            35: ["/snap/brave/512/opt/brave.com/brave/brave --type=gpu-process --field-trial-handle=3"],
            36: ["python3", "train.py"]}
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: list(cmds))
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (cmds[pid], "/"))
    assert app._nr_gpu_others() == ["GPU pid 36"]


def test_gpu_probe_matches_the_browser_marker_as_a_whole_argument(monkeypatch):
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: [41])
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (["python3", "--name=my-gpu-process-test"], "/"))
    assert app._nr_gpu_others() == ["GPU pid 41"]


def test_gpu_probe_counts_the_marker_on_a_binary_that_is_not_a_browser(monkeypatch):
    cmds = {42: ["python3", "x.py", "--type=gpu-process"], 43: ["python3 x.py --type=gpu-process"]}
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: list(cmds))
    monkeypatch.setattr(app, "_nr_argv", lambda pid: (cmds[pid], "/"))
    assert app._nr_gpu_others() == ["GPU pid 42", "GPU pid 43"]


def test_gpu_probe_counts_an_unreadable_cmdline(monkeypatch):
    def gone(pid):
        raise PermissionError(pid)
    monkeypatch.setattr(app, "_nr_gpu_pids", lambda: [51])
    monkeypatch.setattr(app, "_nr_argv", gone)
    assert app._nr_gpu_others() == ["GPU pid 51"]


def test_live_attempts_counts_a_running_pid_and_ignores_a_reused_one(tmp_path):
    led = tmp_path / "ATTEMPTS.jsonl"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old = "2020-01-01T00:00:00Z"
    dead = 2 ** 22 + 7
    led.write_text("\n".join(json.dumps(r) for r in [
        {"scope": "demo/live", "pid": os.getpid(), "ts": now},       # this process: alive, older
        {"scope": "demo/reused", "pid": os.getpid(), "ts": old},     # pid alive but started later
        {"scope": "demo/dead", "pid": dead, "ts": now},
        {"scope": "demo/nopid", "ts": now},
    ]) + "\nnot json\n")
    assert app._nr_live_attempts(str(led)) == [f"attempt pid {os.getpid()} (demo/live)"]
    assert app._nr_live_attempts(str(tmp_path / "missing.jsonl")) == []


# ======================================================================= C. the announcement

def note_count():
    return app._note_seq


def test_an_unannounced_ok_is_announced_once(nr):
    led = str(nr["ledger"])
    t = datetime.fromisoformat("2026-10-03T04:31:30")
    app._nr_append(led, {"event": "trigger", "reason": "nightly", "running_sha": "a" * 64}, t)
    app._nr_append(led, {"event": "start", "reason": "nightly", "app_sha": "b" * 64,
                         "local": "2026-10-03T04:31:31"}, t)
    app._nr_append(led, {"event": "ok", "seconds": 7, "pid": 99}, t)
    n0 = note_count()
    text = app._nightly_restart_announce(led, sleep=lambda s: None)
    assert text == "🔁 Nightly restart 04:31: healthy in 7 s · app.py changed since last start"
    assert note_count() == n0 + 1
    log = (nr["sess"] / "log.md").read_text()
    assert log.count(text) == 1
    assert read_ledger(led)[-1]["event"] == "announced" and read_ledger(led)[-1]["of"] == 3
    # next boot: nothing
    assert app._nightly_restart_announce(led, sleep=lambda s: None) is None
    assert note_count() == n0 + 1 and (nr["sess"] / "log.md").read_text().count(text) == 1
    assert chain_ok(led)


def test_unchanged_and_rolled_back_and_down_texts(nr):
    led = str(nr["ledger"])
    t = datetime.fromisoformat("2026-10-03T04:32:00")
    app._nr_append(led, {"event": "trigger", "reason": "nightly", "running_sha": "a" * 64}, t)
    app._nr_append(led, {"event": "start", "app_sha": "a" * 64, "local": "2026-10-03T04:32:01"}, t)
    app._nr_append(led, {"event": "ok", "seconds": 12, "pid": 5}, t)
    assert app._nightly_restart_announce(led, sleep=lambda s: None).endswith(
        "healthy in 12 s · app.py unchanged")
    app._nr_append(led, {"event": "trigger", "running_sha": "a" * 64}, t)
    app._nr_append(led, {"event": "start", "app_sha": "c" * 64, "local": "2026-10-04T04:33:02"}, t)
    app._nr_append(led, {"event": "rolled_back", "failed_copy": "app.py.failed.20261004T033400Z",
                         "seconds": 20, "pid": 6}, t)
    assert app._nightly_restart_announce(led, sleep=lambda s: None) == (
        "🔴 Nightly restart 04:33: the new app.py did not come up healthy — rolled back to last "
        "known good (bad copy kept as app.py.failed.20261004T033400Z)")
    app._nr_append(led, {"event": "start", "app_sha": "c" * 64, "local": "2026-10-05T04:34:00"}, t)
    app._nr_append(led, {"event": "down", "why": "no last known good"}, t)
    assert app._nightly_restart_announce(led, sleep=lambda s: None) == (
        "🔴 Nightly restart 04:34: Helix did not come back (no last known good). "
        "See ~/Desktop/HELIX_DID_NOT_COME_BACK.txt")


def test_a_skip_is_announced_when_it_happens(nr):
    c = Clock("2026-10-03T12:00:00")
    n0 = note_count()
    app._nightly_restart_after(Buf(), now=c.now, sleep=c.sleep)
    assert note_count() == n0 + 1
    assert "Nightly restart skipped: outside 04:00–06:59 (12:00)" in (nr["sess"] / "log.md").read_text()
    assert app._nightly_restart_announce(str(nr["ledger"]), sleep=lambda s: None) is None


def test_boot_waits_for_the_outcome_of_a_sequence_in_progress(nr):
    led = str(nr["ledger"])
    t = datetime.fromisoformat("2026-10-03T04:31:30")
    app._nr_append(led, {"event": "trigger", "running_sha": "a" * 64}, t)
    app._nr_append(led, {"event": "start", "app_sha": "a" * 64, "local": "2026-10-03T04:31:31"}, t)
    waits = []

    def sleep(s):
        waits.append(s)
        if len(waits) == 4:                       # the script's health check passes meanwhile
            app._nr_append(led, {"event": "ok", "seconds": 9, "pid": 7}, t)
    assert app._nightly_restart_announce(led, sleep=sleep).endswith("healthy in 9 s · app.py unchanged")
    assert len(waits) == 4


def test_boot_gives_up_waiting_loudly(nr):
    led = str(nr["ledger"])
    t = datetime.fromisoformat("2026-10-03T04:31:30")
    app._nr_append(led, {"event": "start", "app_sha": "a" * 64, "local": "2026-10-03T04:31:31"}, t)
    text = app._nightly_restart_announce(led, sleep=lambda s: None)
    assert text.startswith("🔴 Nightly restart 04:31: started but recorded no outcome")


def test_an_empty_or_missing_ledger_announces_nothing(nr):
    assert app._nightly_restart_announce(str(nr["ledger"]), sleep=lambda s: None) is None
    assert not (nr["sess"] / "log.md").exists()


def test_boot_runs_the_announcement_only_as_main():
    src = (HELIX / "app.py").read_text()
    main = src.split('if __name__=="__main__":', 1)[1]
    assert "_nightly_restart_announce" in main


# ======================================================================= B. the script

FAKE_APP = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, mode = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if mode == "GOOD" and self.path == "/stat":
            body, code = b"{}", 200
        elif mode == "GOOD" and self.path == "/model":
            body, code = json.dumps({"odin": "opus"}).encode(), 200
        else:
            body, code = b"broken", 500
        self.send_response(code); self.end_headers(); self.wfile.write(body)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

# Kills the previous fake app, then runs whatever the temp app.py says: GOOD serves the health
# routes, BROKEN answers 500, DEAD never starts. Like start_helix.sh it returns once started.
FAKE_LAUNCHER = r'''#!/bin/bash
R="$HELIX_NR_TEST_ROOT"
echo "launch HELIX_NO_BROWSER=${HELIX_NO_BROWSER:-}" >> "$R/launcher_calls"
[ -f "$R/fake.pid" ] && kill "$(cat "$R/fake.pid")" 2>/dev/null && sleep 0.3
rm -f "$R/fake.pid"
mode=$(awk '{print $1; exit}' "$R/app.py")
[ "$mode" = "DEAD" ] && exit 0
nohup python3 "$R/fake_app.py" "$(cat "$R/port")" "$mode" "fakeapp-$(cat "$R/port")" \
    >/dev/null 2>&1 &
echo $! > "$R/fake.pid"
sleep 0.4
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def fake(tmp_path):
    root = tmp_path / "nr"
    root.mkdir()
    (root / "fake_app.py").write_text(FAKE_APP)
    (root / "launcher.sh").write_text(FAKE_LAUNCHER)
    (root / "launcher.sh").chmod(0o755)
    (root / "port").write_text(str(free_port()))
    (root / "Desktop").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HELIX_NR_TEST")}
    env.update(HELIX_NR_TEST_ROOT=str(root), HELIX_NR_TEST_LAUNCHER=str(root / "launcher.sh"),
               HELIX_NR_TEST_URL=f"http://127.0.0.1:{(root / 'port').read_text()}",
               HELIX_NR_TEST_PATTERN=f"fakeapp-{(root / 'port').read_text()}$", HELIX_NR_TEST_HEALTH_S="4")
    state = {"root": root, "env": env}
    yield state
    pid = root / "fake.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 9)
        except Exception:
            pass


def run_script(f):
    r = subprocess.run(["bash", str(SCRIPT)], env=f["env"], capture_output=True, text=True,
                       timeout=60)
    root = f["root"]
    calls = (root / "launcher_calls").read_text().splitlines() if (root / "launcher_calls").exists() else []
    return r, calls, read_ledger(root / "RESTARTS.jsonl")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def seed_ok_lkg(root, text):
    """A .lkg written the way a healthy run writes it, and its ok line in the ledger."""
    (root / ".lkg").mkdir()
    (root / ".lkg" / "app.py").write_text(text)
    h = sha(root / ".lkg" / "app.py")
    (root / ".lkg" / "app.py.sha256").write_text(f"{h}  app.py\n")
    app._nr_append(str(root / "RESTARTS.jsonl"), {"event": "ok", "lkg_sha": h, "seconds": 1},
                   datetime.now())
    return h


def test_script_healthy_writes_ok_and_updates_lkg(fake):
    root = fake["root"]
    (root / "app.py").write_text("GOOD v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == ["launch HELIX_NO_BROWSER=1"]
    assert [x["event"] for x in recs] == ["start", "ok"]
    start, ok = recs
    assert start["reason"] == "nightly" and start["app_sha"] == sha(root / "app.py")
    assert "old_pid" in start and ok["seconds"] <= 4 and ok["pid"] > 0
    assert (root / ".lkg" / "app.py").read_text() == "GOOD v2\n"
    assert ok["lkg_sha"] == sha(root / "app.py")
    assert (root / ".lkg" / "app.py.sha256").read_text().split()[0] == ok["lkg_sha"]
    assert chain_ok(root / "RESTARTS.jsonl")
    assert not list((root / "Desktop").iterdir())


def test_script_broken_with_a_good_lkg_rolls_back_once(fake):
    root = fake["root"]
    good = seed_ok_lkg(root, "GOOD v1\n")
    (root / "app.py").write_text("BROKEN v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 3, r.stdout + r.stderr
    assert len(calls) == 2
    assert [x["event"] for x in recs] == ["ok", "start", "rolled_back"]
    rb = recs[-1]
    kept = root / rb["failed_copy"]
    assert re.fullmatch(r"app\.py\.failed\.\d{8}T\d{6}Z", rb["failed_copy"])
    assert kept.read_text() == "BROKEN v2\n"
    assert (root / "app.py").read_text() == "GOOD v1\n" and rb["restored_sha"] == good
    assert chain_ok(root / "RESTARTS.jsonl")
    assert not list((root / "Desktop").iterdir())


def test_script_broken_without_lkg_is_down_after_exactly_one_launch(fake):
    root = fake["root"]
    (root / "app.py").write_text("BROKEN v2\n")
    (root / "odin.log").write_text("line1\nTraceback: boom\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 4, r.stdout + r.stderr
    assert len(calls) == 1
    assert [x["event"] for x in recs] == ["start", "down"]
    assert recs[-1]["why"] == ("unhealthy after 4 s (/stat answered 500), and no last known "
                              "good to roll back to")
    d = (root / "Desktop" / "HELIX_DID_NOT_COME_BACK.txt").read_text()
    assert "did not come back" in d and '"event": "down"' in d and "Traceback: boom" in d
    assert (root / "app.py").read_text() == "BROKEN v2\n"     # nothing restored, nothing moved
    assert not (root / ".lkg").exists()


def test_script_broken_and_rollback_broken_is_down_after_two_launches_never_three(fake):
    root = fake["root"]
    seed_ok_lkg(root, "DEAD v1\n")
    (root / "app.py").write_text("BROKEN v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 4, r.stdout + r.stderr
    assert len(calls) == 2
    assert [x["event"] for x in recs] == ["ok", "start", "down"]
    assert recs[-1]["why"].startswith("rolled back to last known good, still unhealthy")
    assert (root / "Desktop" / "HELIX_DID_NOT_COME_BACK.txt").exists()


def test_script_broken_with_an_identical_lkg_does_not_restart_again(fake):
    root = fake["root"]
    seed_ok_lkg(root, "BROKEN v2\n")
    (root / "app.py").write_text("BROKEN v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 4 and len(calls) == 1
    assert "same as last known good" in recs[-1]["why"]


def test_script_refuses_an_lkg_the_ledger_does_not_vouch_for(fake):
    """A .lkg planted with a matching .sha256 file but no ok line naming it is not used."""
    root = fake["root"]
    (root / ".lkg").mkdir()
    (root / ".lkg" / "app.py").write_text("GOOD planted\n")
    (root / ".lkg" / "app.py.sha256").write_text(f"{sha(root / '.lkg' / 'app.py')}  app.py\n")
    (root / "app.py").write_text("BROKEN v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 4 and len(calls) == 1
    assert "not vouched for by the ledger" in recs[-1]["why"]
    assert (root / "app.py").read_text() == "BROKEN v2\n"


def test_script_two_app_processes_are_not_healthy(fake):
    """Exactly one app.py: a second process matching the pattern fails the check."""
    root = fake["root"]
    (root / "app.py").write_text("GOOD v2\n")
    extra = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)",
                              f"fakeapp-{(root / 'port').read_text()}"])
    try:
        r, calls, recs = run_script(fake)
    finally:
        extra.kill()
    assert r.returncode == 4 and recs[-1]["event"] == "down"
    assert "2 app.py" in recs[-1]["why"] or "app.py count 2" in recs[-1]["why"]


def test_script_appends_to_an_app_written_ledger_with_an_unbroken_chain(fake):
    root = fake["root"]
    app._nr_append(str(root / "RESTARTS.jsonl"), {"event": "trigger", "reason": "nightly"},
                   datetime.now())
    (root / "app.py").write_text("GOOD v2\n")
    r, calls, recs = run_script(fake)
    assert r.returncode == 0 and [x["event"] for x in recs] == ["trigger", "start", "ok"]
    assert chain_ok(root / "RESTARTS.jsonl")


def test_script_refuses_test_overrides_pointing_at_the_real_helix(fake):
    env = dict(fake["env"], HELIX_NR_TEST_ROOT=str(HELIX))
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "refused" in r.stderr


def test_script_refuses_partial_test_overrides(fake):
    env = dict(fake["env"])
    del env["HELIX_NR_TEST_LAUNCHER"]
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "refused" in r.stderr
    assert not (fake["root"] / "RESTARTS.jsonl").exists()


def test_script_live_defaults_are_the_real_paths():
    """Read, never run: the live branch points at the real launcher, ledger, .lkg and Desktop."""
    s = SCRIPT.read_text()
    assert 'LAUNCHER="$BRAIN/start_helix.sh"' in s
    assert 'LEDGER="$BRAIN/workspace/cycle/RESTARTS.jsonl"' in s
    assert 'LKG="$BRAIN/.lkg"' in s and 'DESKTOP="$HOME/Desktop"' in s
    assert 'URL="http://127.0.0.1:7777"' in s and 'HEALTH_S=90' in s
