#!/usr/bin/env python3
"""The work lane — a long background job must not mute Odin.

Before lanes there was ONE turn lock and ONE SDK session, so a job that ran for an hour left
the user unable to ask anything for that hour: every message parked behind it and printed
"waiting — folding into the current turn". That is roadmap item #2's blocker.

These tests use a faked _run_turn (no model calls) and assert the property that matters:
CHAT completes a turn while WORK is still holding its own lane.

Run:  .venv/bin/python test_work_lane.py
"""
import os, sys, tempfile, threading, time

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"      # order 32: no real ticker on memory/schedules.json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

# Redirect the front desk's pad into a scratch file BEFORE any route runs. Without this the
# /job route test below reaches frontdesk.set_last_job and writes the real memory/notepad.json
# (found by order 12's step 0). Same pattern test_model_selector.py uses for UI_STATE.
_ORIG_NOTEPAD = app.NOTEPAD
app.NOTEPAD = os.path.join(tempfile.mkdtemp(), "notepad.json")

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_chat_and_work_are_separate_lanes():
    check("there is a work lane", isinstance(getattr(app, "WORK", None), app.Lane))
    check("it is not the chat lane", getattr(app, "WORK", None) is not app.CHAT)
    check("the two do not share a turn lock", app.WORK.lock is not app.CHAT.lock)
    check("the two do not share a router", app.WORK.router is not app.CHAT.router)
    check("the two do not share a client", app.WORK.client is None or app.WORK.client is not app.CHAT.client)
    check("the two do not share a coalescing queue", app.WORK.pending is not app.CHAT.pending)


def test_a_long_work_job_does_not_block_chat():
    """THE POINT OF THE WHOLE PIECE. A job holds WORK; chat must still answer."""
    release_job = threading.Event()
    job_running = threading.Event()

    async def fake_run_turn(text, q, lane=None):
        lane = app.CHAT if lane is None else lane
        if lane.name == "work":
            job_running.set()
            while not release_job.is_set():      # a long job: hold the work lane
                import asyncio
                await asyncio.sleep(0.01)
            q.put(("final", {"text": "job done"}))
        else:
            q.put(("final", {"text": "chat answered"}))
        q.put(app._STREAM_DONE)

    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    real_run, app._run_turn = app._run_turn, fake_run_turn
    real_greet, app.greet = app.greet, lambda t: None       # force the escalate path
    try:
        job = app.start_job("run the long sweep")
        check("the job is running on the work lane", job_running.wait(timeout=5))
        check("the work lane is held", app.WORK.lock.locked())

        # ── the assertion the old architecture could not satisfy ──────────────
        chat = app.TurnBuf("c" + os.urandom(4).hex(), "are you there?", "odin")
        app._register_turn(chat)
        t = threading.Thread(target=app._produce, args=(chat,), daemon=True)
        t.start(); t.join(timeout=8)

        check("chat answered WHILE the job was still running", chat.status == "done",
              f"chat status={chat.status} (job still held: {app.WORK.lock.locked()})")
        check("chat got its real answer", "chat answered" in (chat.final or chat.text),
              f"final={chat.final!r}")
        check("the job really was still holding its lane", app.WORK.lock.locked())

        release_job.set()
        for _ in range(500):
            if job.status != "running": break
            time.sleep(0.01)
        check("the job finishes once released", job.status == "done", f"job status={job.status}")
        check("the work lane is free afterwards", not app.WORK.lock.locked())
    finally:
        release_job.set()
        app._run_turn, app.greet, app.SESS = real_run, real_greet, real_sess
        for lane in (app.CHAT, app.WORK):
            if lane.lock.locked():
                try: lane.lock.release()
                except Exception: pass


def test_a_finished_job_is_announced_to_the_ui():
    """A job nobody is watching must still surface — as a bubble Odin raises himself."""
    async def fake_run_turn(text, q, lane=None):
        q.put(("final", {"text": "the sweep finished, 8/8 green"}))
        q.put(app._STREAM_DONE)

    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    real_run, app._run_turn = app._run_turn, fake_run_turn
    real_greet, app.greet = app.greet, lambda t: None
    seq0 = app._note_seq
    try:
        job = app.start_job("run the sweep")
        for _ in range(500):
            if job.status != "running": break
            time.sleep(0.01)
        check("the job completed", job.status == "done", f"status={job.status}")
        check("the note counter advanced (UI announce)", app._note_seq > seq0,
              f"seq {seq0} -> {app._note_seq}")
        check("the job id is in the announce feed", job.id in list(app._note_ids),
              f"ids={list(app._note_ids)}")
        check("the answer is retrievable by id", app._turns.get(job.id) is job)
    finally:
        app._run_turn, app.greet, app.SESS = real_run, real_greet, real_sess


def test_job_route_submits_and_returns_an_id():
    async def fake_run_turn(text, q, lane=None):
        q.put(("final", {"text": "ok"}))
        q.put(app._STREAM_DONE)

    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    real_run, app._run_turn = app._run_turn, fake_run_turn
    real_greet, app.greet = app.greet, lambda t: None
    try:
        with app.app.test_client() as tc:
            r = tc.post("/job", json={"text": "sweep the logs"}).get_json()
            check("/job accepts the task", r.get("ok") is True, f"resp={r}")
            check("/job returns a turn id", bool(r.get("id")), f"resp={r}")
            tid = r.get("id")
            for _ in range(500):
                if app._turns.get(tid) and app._turns[tid].status != "running": break
                time.sleep(0.01)
            st = tc.get(f"/turn_state?id={tid}").get_json()
            check("the job is recoverable via /turn_state", st.get("found") is True, f"state={st}")
        with app.app.test_client() as tc:
            check("/job rejects an empty task", tc.post("/job", json={"text": "  "}).status_code == 400)
    finally:
        app._run_turn, app.greet, app.SESS = real_run, real_greet, real_sess


def test_metrics_reports_both_lanes():
    with app.app.test_client() as tc:
        j = tc.get("/metrics").get_json()
    check("/metrics still reports chat busy-ness", "turn_busy" in j)
    check("/metrics reports the work lane too", "work_busy" in j, f"keys={sorted(j.keys())}")


print("\nHELIX — work lane (a long job must not mute chat)\n" + "=" * 52)
test_chat_and_work_are_separate_lanes()
test_a_long_work_job_does_not_block_chat()
test_a_finished_job_is_announced_to_the_ui()
test_job_route_submits_and_returns_an_id()
test_metrics_reports_both_lanes()
print("=" * 52)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
