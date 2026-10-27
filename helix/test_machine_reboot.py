"""Tests for ORDER 67 Part C — the nightly restart reboots instead, only when due.

app.py is imported as test_nightly_restart.py imports it. The RESTARTS ledger, MACHINE.jsonl and
the gdm config are temp files; the sudo call is a recording `run=` passed into
_nightly_restart_try; the launcher is a recorder. Nothing here reboots, calls sudo, or starts
nightly_restart.sh.

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_machine_reboot.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402

DAY = "2026-10-19"
REBOOT_ARGV = ["sudo", "-n", "/usr/local/sbin/helix-machine", "reboot"]
AUTOLOGIN = """# GDM configuration storage
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=__USER__

[security]
"""
NO_AUTOLOGIN = """# GDM configuration storage
[daemon]
# Enabling automatic login
#  AutomaticLoginEnable = true
#  AutomaticLogin = user1

[security]
"""


class Clock:
    def __init__(self, s):
        self.t = datetime.fromisoformat(s)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += timedelta(seconds=s)


class Run:
    """Records the argv and kwargs of each call; answers with `code`."""

    def __init__(self, code=0, exc=None):
        self.code, self.exc, self.calls = code, exc, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, "", "helix-machine: no reason to reboot, nothing done\n"
                                           if self.code == 4 else "")


def read_ledger(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def nr(tmp_path, monkeypatch):
    led = tmp_path / "RESTARTS.jsonl"
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(app, "RESTARTS_LEDGER", str(led))
    monkeypatch.setattr(app, "SESS", str(sess))
    monkeypatch.setattr(app, "_NR_LAUNCHED", set())
    launches = []
    monkeypatch.setattr(app, "_nightly_restart_launch", lambda: launches.append("launch"))
    monkeypatch.setattr(app, "_builder_active", lambda: (None, None))
    monkeypatch.setattr(app, "_queue_read", lambda: None)
    monkeypatch.setattr(app, "_nr_live_attempts", lambda path=None: [])
    monkeypatch.setattr(app, "_nr_gpu_others", lambda: [])
    machine = tmp_path / "MACHINE.jsonl"
    gdm = tmp_path / "custom.conf"
    gdm.write_text(AUTOLOGIN)

    def day(due, reason=None, date=DAY):
        with machine.open("a") as fh:
            fh.write(json.dumps({"date": date, "reboot_due": due, "reboot_reason": reason}) + "\n")

    def go(run, at=f"{DAY}T04:41:00"):
        c = Clock(at)
        out = app._nightly_restart_try(c.now, c.sleep, run=run, machine=str(machine), gdm=str(gdm))
        return out, c
    return dict(ledger=led, launches=launches, machine=machine, gdm=gdm, day=day, go=go)


# ------------------------------------------------------------------ the decision

def test_due_and_autologin_reboots(nr):
    nr["day"](True, "reboot-required (linux-image-6.8)")
    run = Run(0)
    out, _ = nr["go"](run)
    assert out == "reboot" and nr["launches"] == []
    [(argv, kw)] = run.calls
    assert argv == REBOOT_ARGV and kw["timeout"] == 20
    [rec] = read_ledger(nr["ledger"])
    assert rec["event"] == "reboot" and rec["reason"] == "reboot-required (linux-image-6.8)"
    assert datetime.fromisoformat(DAY).date() in app._NR_LAUNCHED


def test_due_without_autologin_restarts_normally_and_says_why(nr):
    nr["gdm"].write_text(NO_AUTOLOGIN)
    nr["day"](True, "reboot-required")
    run = Run(0)
    out, _ = nr["go"](run)
    assert out == "started" and nr["launches"] == ["launch"] and run.calls == []
    [trig] = read_ledger(nr["ledger"])
    assert trig["event"] == "trigger" and trig["machine"] == "reboot due, not done: no automatic login"


def test_helix_machine_refusing_falls_through_to_the_normal_restart(nr):
    nr["day"](True, "reboot-required")
    run = Run(4)
    out, _ = nr["go"](run)
    assert out == "started" and nr["launches"] == ["launch"] and len(run.calls) == 1
    recs = read_ledger(nr["ledger"])
    assert [r["event"] for r in recs] == ["reboot", "skipped", "trigger"]
    assert recs[1]["exit"] == 4 and recs[1]["reason"].startswith("reboot not done: helix-machine exit 4")


def test_a_sudo_timeout_also_falls_through(nr):
    nr["day"](True, "reboot-required")
    out, _ = nr["go"](Run(exc=subprocess.TimeoutExpired(REBOOT_ARGV, 20)))
    recs = read_ledger(nr["ledger"])
    assert out == "started" and [r["event"] for r in recs] == ["reboot", "skipped", "trigger"]
    assert recs[1]["exit"] is None and "timed out" in recs[1]["reason"]


def test_a_broken_gpu_reboots_before_the_busy_loop(nr, monkeypatch):
    monkeypatch.setattr(app, "_nr_gpu_others", lambda: ["GPU unknown (nvidia-smi failed: RuntimeError)"])
    busy_checked = []
    real_busy = app._nightly_restart_busy
    monkeypatch.setattr(app, "_nightly_restart_busy", lambda: busy_checked.append(1) or real_busy())
    nr["day"](True, "GPU driver mismatch")
    run = Run(0)
    out, c = nr["go"](run)
    assert out == "reboot" and busy_checked == [] and c.slept == []
    assert [x[0] for x in run.calls] == [REBOOT_ARGV]
    assert [r["event"] for r in read_ledger(nr["ledger"])] == ["reboot"]


def test_a_broken_gpu_without_autologin_keeps_todays_busy_skip(nr, monkeypatch):
    nr["gdm"].write_text(NO_AUTOLOGIN)
    monkeypatch.setattr(app, "_nr_gpu_others", lambda: ["GPU unknown (nvidia-smi failed: RuntimeError)"])
    nr["day"](True, "GPU driver mismatch")
    run = Run(0)
    out, _ = nr["go"](run, at=f"{DAY}T06:58:00")
    assert out.startswith("skipped: busy (GPU unknown") and run.calls == []


def test_not_due_is_todays_behaviour(nr):
    nr["day"](False)
    run = Run(0)
    out, c = nr["go"](run)
    assert out == "started" and nr["launches"] == ["launch"] and run.calls == []
    [trig] = read_ledger(nr["ledger"])
    assert set(trig) == {"event", "reason", "running_sha", "pid", "local", "ts", "seq", "prev"}


@pytest.mark.parametrize("setup", ["yesterday", "missing", "garbage_last_line"])
def test_only_todays_last_line_counts(nr, setup):
    if setup == "yesterday":
        nr["day"](True, "reboot-required", date="2026-10-18")
    elif setup == "garbage_last_line":
        nr["day"](True, "reboot-required")
        with nr["machine"].open("a") as fh:
            fh.write("{not json\n")
    run = Run(0)
    out, _ = nr["go"](run)
    assert out == "started" and run.calls == []


def test_the_default_run_and_paths_are_the_real_ones():
    assert app.NIGHTLY_MACHINE == f"{app.BRAIN}/workspace/seam/MACHINE.jsonl"
    assert app.NIGHTLY_GDM_CONF == "/etc/gdm3/custom.conf"
    assert app.NIGHTLY_REBOOT == REBOOT_ARGV and app.NIGHTLY_REBOOT_TIMEOUT_S == 20


# ------------------------------------------------------------------ the gdm parser

@pytest.mark.parametrize("text,want", [
    (AUTOLOGIN, True),
    (NO_AUTOLOGIN, False),
    ("[daemon]\nAutomaticLoginEnable = True\nAutomaticLogin = __USER__\n", True),
    ("[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=someone-else\n", False),
    ("[daemon]\nAutomaticLoginEnable=false\nAutomaticLogin=__USER__\n", False),
    ("[daemon]\nAutomaticLogin=__USER__\n", False),
    ("[security]\nAutomaticLoginEnable=true\nAutomaticLogin=__USER__\n", False),
])
def test_autologin_parser(tmp_path, text, want):
    p = tmp_path / "custom.conf"
    p.write_text(text)
    assert app._nr_autologin(str(p)) is want


def test_autologin_parser_on_a_missing_file(tmp_path):
    assert app._nr_autologin(str(tmp_path / "nope.conf")) is False

