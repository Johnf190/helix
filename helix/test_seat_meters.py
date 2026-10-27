#!/usr/bin/env python3
"""ORDER 66 — the ODIN/ALICE meters read real per-seat CPU; /stat and /metrics sample once per
0.8 s however many pollers there are.

Run:  .venv/bin/python -m pytest -q test_seat_meters.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"

import pytest  # noqa: E402

import app  # noqa: E402

V7 = Path(__file__).resolve().parent / "static" / "index_v7.html"
CLK = app._CLK


def _reset_seats():
    app._seat_prev.update(t=0.0, ticks={}, val=(None, None))


@pytest.fixture(autouse=True)
def _fresh():
    _reset_seats()
    app._cpu_cache.update(t=0.0)
    app._proc_cpu_prev.update(t=0.0)
    yield
    _reset_seats()


# ── 1. classification over a fake /proc tree ────────────────────────────────
ROOT = 1000


def _tree(ticks):
    """{(pid, starttime): (ppid, ticks, argv0)} — ticks given per pid."""
    shape = {
        1001: (ROOT, "agy"),      # Alice's agy …
        1002: (1001, "node"),     # … and its grandchild: alice
        1003: (ROOT, "claude"),   # the claude CLI …
        1004: (1003, "bash"),     # … and its tool shell: odin
        1005: (ROOT, "bash"),     # disappears before the second sample
        9999: (1, "chrome"),      # not a descendant: ignored
    }
    return {(pid, 7): (pp, ticks[pid], a) for pid, (pp, a) in shape.items() if pid in ticks}


def test_classification_and_vanishing_pid(monkeypatch):
    first = _tree({1001: 0, 1002: 0, 1003: 0, 1004: 0, 1005: 0, 9999: 0})
    # one second later: agy +0.5 s, its child +0.25 s, claude +1 s, bash +0.5 s,
    # chrome +5 s (ignored), 1005 gone (its ticks are lost), 1006 new (no window yet)
    second = _tree({1001: CLK // 2, 1002: CLK // 4, 1003: CLK, 1004: CLK // 2, 9999: 5 * CLK})
    second[(1006, 9)] = (ROOT, 50 * CLK, "bash")
    snaps = iter([first, second])
    monkeypatch.setattr(app, "_proc_snapshot", lambda: next(snaps))
    assert app._seat_cpu(root=ROOT, now=100.0) == (None, None)       # no window yet: no reading
    odin, alice = app._seat_cpu(root=ROOT, now=101.0)
    assert odin == pytest.approx(100.0 * (CLK + CLK // 2) / CLK, abs=0.1)
    assert alice == pytest.approx(100.0 * (CLK // 2 + CLK // 4) / CLK, abs=0.1)


def test_split_assigns_seats():
    snap = _tree({1001: 0, 1002: 0, 1003: 0, 1004: 0, 1005: 0, 9999: 0})
    seat = app._seat_split(snap, ROOT)
    assert seat[(1001, 7)] == seat[(1002, 7)] == "alice"
    assert seat[(1003, 7)] == seat[(1004, 7)] == seat[(1005, 7)] == "odin"
    assert (9999, 7) not in seat


def test_pid_reuse_is_a_different_process(monkeypatch):
    # same pid, new starttime: the old process's ticks must not be diffed against the new one's
    first = {(1003, 7): (ROOT, 0, "claude")}
    second = {(1003, 8): (ROOT, 30 * CLK, "claude")}
    snaps = iter([first, second])
    monkeypatch.setattr(app, "_proc_snapshot", lambda: next(snaps))
    app._seat_cpu(root=ROOT, now=10.0)
    assert app._seat_cpu(root=ROOT, now=11.0) == (0.0, 0.0)


# ── 2. one sampler per reading, not per caller ──────────────────────────────
def test_stat_cpu_cached_within_window():
    c = app.app.test_client()
    a = c.get("/stat").get_json()["cpu"]; t1 = app._cpu_cache["t"]
    b = c.get("/stat").get_json()["cpu"]; t2 = app._cpu_cache["t"]
    assert a == b and t1 == t2 and t1 > 0
    assert isinstance(b, int)


def test_metrics_cpu_and_seats_cached_within_window():
    c = app.app.test_client()
    c.get("/metrics"); time.sleep(0.9)                    # prime: both windows now exist
    j1 = c.get("/metrics").get_json(); t1 = (app._proc_cpu_prev["t"], app._seat_prev["t"])
    j2 = c.get("/metrics").get_json(); t2 = (app._proc_cpu_prev["t"], app._seat_prev["t"])
    assert t1 == t2
    for k in ("cpu", "odin_cpu", "alice_cpu"):
        assert j1[k] == j2[k], k
        assert isinstance(j1[k], float), (k, j1[k])


def test_resample_after_window(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_proc_snapshot", lambda: calls.append(1) or {})
    app._seat_cpu(root=ROOT, now=50.0)
    app._seat_cpu(root=ROOT, now=50.5)                     # cached
    app._seat_cpu(root=ROOT, now=50.9)                     # 0.9 s on: a new sample
    assert len(calls) == 2


# ── 3. a broken reader prints no reading, never a zero ──────────────────────
def test_reader_failure_is_null(monkeypatch):
    def boom():
        raise OSError("no /proc")
    monkeypatch.setattr(app, "_proc_snapshot", boom)
    r = app.app.test_client().get("/metrics")
    assert r.status_code == 200
    j = r.get_json()
    assert j["odin_cpu"] is None and j["alice_cpu"] is None
    assert "cpu" in j


# ── 4. the page reads the data ──────────────────────────────────────────────
def _loop_body(src):
    i = src.index("function loop(t){")
    return src[i:src.index("requestAnimationFrame(loop)}", i)]


def test_page_meters_read_metrics():
    src = V7.read_text()
    body = _loop_body(src)
    assert "Math.sin(" not in body
    # the two removed per-mood seat tables (the bezel arc has its own mood table: not this order's)
    assert "THINKING:.75,SPEAKING:.9" not in src and "THINKING:.6,SPEAKING:.05" not in src
    for s in ("odin_cpu", "alice_cpu", "no reading", "mic open"):
        assert s in src, s
    assert re.search(r"odin_cpu", body) and re.search(r"alice_cpu", body)


# ── 5. positive control: a real busy child reads in the odin bucket ─────────
def test_real_busy_child_reads_odin():
    p = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        time.sleep(0.3)
        root = os.getpid()
        app._seat_cpu(root=root)
        time.sleep(1.0)
        odin, alice = app._seat_cpu(root=root)
        assert odin is not None and odin >= 50.0, odin
        assert alice == 0.0, alice
    finally:
        p.kill(); p.wait()
