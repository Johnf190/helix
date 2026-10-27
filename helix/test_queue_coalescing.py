#!/usr/bin/env python3
"""Regression tests for the stale-queue lag.

Bug (the user, 2026-09-06): messages sent while a long agent turn is running each spawn a
producer thread that blocks on _turn_lock, then runs a FULL agent turn for its (by now
stale) message, in order. Sending 3-5 messages during a long turn means 3-5 answers to
dead questions before the newest one is addressed.

Fix under test: messages that arrive mid-turn are held in a pending batch; when the
running turn finishes, the whole batch is drained as ONE combined agent turn whose
prompt presents the LATEST message as the current question. The latest message's
bubble streams the real answer; earlier bubbles resolve with a short fold note.

Run:  .venv/bin/python test_queue_coalescing.py   (or python3)
"""
import os, sys, threading, time, tempfile, shutil, uuid

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"      # order 32: no real ticker on memory/schedules.json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_mid_turn_messages_coalesce_into_one_turn():
    calls = []                       # texts _run_turn was invoked with
    first_started = threading.Event()
    release_first = threading.Event()

    async def fake_run_turn(text, q, lane=None):   # lane arg added 2026-09-06 (Lane split)
        calls.append(text)
        if len(calls) == 1:
            first_started.set()
            # long agent turn: hold until the test releases it
            while not release_first.is_set():
                import asyncio
                await asyncio.sleep(0.01)
        q.put(("final", {"text": f"answer-{len(calls)}"}))
        q.put(app._STREAM_DONE)

    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    real_run, app._run_turn = app._run_turn, fake_run_turn
    real_greet, app.greet = app.greet, lambda t: None      # force the escalate path, fast
    try:
        bufs, threads = [], []
        for msg in ("first question", "second question", "third question"):
            b = app.TurnBuf(uuid.uuid4().hex[:12], msg, "odin")
            app._register_turn(b)
            th = threading.Thread(target=app._produce, args=(b,), daemon=True)
            bufs.append(b); threads.append(th)

        threads[0].start()
        check("long turn starts", first_started.wait(5))
        threads[1].start(); threads[2].start()
        time.sleep(0.3)              # give the queued messages time to (wrongly) start turns

        check("queued messages do not start their own turns while one runs",
              len(calls) == 1, f"turns started so far: {len(calls)} (texts={calls!r})")

        release_first.set()
        check("all turns complete", _wait(lambda: all(b.status != "running" for b in bufs), 10),
              f"statuses={[b.status for b in bufs]}")
        _wait(lambda: len(calls) >= 2, 5)
        time.sleep(0.3)              # would-be extra turns get a moment to appear

        check("the two queued messages are answered by ONE combined turn, not two",
              len(calls) == 2, f"total agent turns: {len(calls)} (texts={calls!r})")
        combined = calls[1] if len(calls) > 1 else ""
        check("the combined turn carries both queued messages",
              "second question" in combined and "third question" in combined,
              f"combined prompt: {combined!r}")
        check("the LATEST message is presented as the current question",
              combined.rfind("third question") > combined.rfind("second question"),
              f"combined prompt: {combined!r}")
        check("the latest bubble gets the real answer",
              "answer-2" in (bufs[2].final or bufs[2].text),
              f"buf3 final={bufs[2].final!r} text={bufs[2].text!r}")
        check("the earlier bubble resolves with a fold note, not a full stale answer",
              bufs[1].status == "done" and "answer-" not in (bufs[1].final or "")
              and (bufs[1].final or bufs[1].text),
              f"buf2 status={bufs[1].status} final={bufs[1].final!r} text={bufs[1].text!r}")
        check("the lock is free afterwards", not app.CHAT.lock.locked())
        check("no pending leftovers", not getattr(app, "_pending", []))
    finally:
        app._run_turn = real_run
        app.greet = real_greet
        app.SESS = real_sess
        try: app.CHAT.lock.release()
        except RuntimeError: pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_message_prompt_is_unchanged():
    """No batching in play -> the agent must see the user's text verbatim, no wrapper."""
    calls = []

    async def fake_run_turn(text, q, lane=None):   # lane arg added 2026-09-06 (Lane split)
        calls.append(text)
        q.put(("final", {"text": "ok"}))
        q.put(app._STREAM_DONE)

    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    real_run, app._run_turn = app._run_turn, fake_run_turn
    real_greet, app.greet = app.greet, lambda t: None
    try:
        b = app.TurnBuf(uuid.uuid4().hex[:12], "solo question", "odin")
        app._produce(b)
        check("a lone message reaches the agent verbatim",
              calls == ["solo question"], f"calls={calls!r}")
        check("lone turn completes", b.status == "done" and b.final == "ok",
              f"status={b.status} final={b.final!r}")
    finally:
        app._run_turn = real_run
        app.greet = real_greet
        app.SESS = real_sess
        shutil.rmtree(tmp, ignore_errors=True)


print("\nHELIX — stale-queue coalescing regression\n" + "=" * 46)
test_mid_turn_messages_coalesce_into_one_turn()
test_single_message_prompt_is_unchanged()
print("=" * 46)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
