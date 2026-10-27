#!/usr/bin/env python3
"""Regression tests for silent background-job reports.

Bug (the user, 2026-09-06): "when you say you'll hit me back when the job finishes you never
do — output doesn't come until I ask." The CLI DOES push a report turn when a background
job finishes (verified empirically: probe saw 'JOB REPORT' arrive while idle), but Helix
only listened inside receive_response() during a user-triggered turn. Between turns the
report sat unread in the SDK's buffer until the next question dragged it in.

Fix under test: one long-lived reader owns the message stream and routes through
_MsgRouter. Messages for a turn the user asked for go to that turn's queue; messages
with NO user turn waiting are an unsolicited turn (a background report) and become a
proactive TurnBuf — registered, logged, and announced via /metrics for the UI to render.

Run:  .venv/bin/python test_proactive_reports.py
"""
import os, sys, asyncio, tempfile, shutil

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"      # order 32: no real ticker on memory/schedules.json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

# Order 32: building CHAT/WORK options calls _write_wall(), which rewrote the live ~/helix/wall_exec.sh.
# Point it at a temp path, as test_write_wall.py's autouse fixture does; restored in the finally below.
import tempfile as _tf, shutil as _sh  # noqa: E402
_REAL_WALL_EXEC = app.WALL_EXEC
_WALL_TMP = _tf.mkdtemp(prefix="helix_test_wall_")
app.WALL_EXEC = os.path.join(_WALL_TMP, "wall_exec.sh")

from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage,  # noqa: E402
                              StreamEvent, TextBlock)

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def result_msg(text=None):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="s", result=text)


def assistant(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def sysmsg():
    return SystemMessage(subtype="status", data={})


def test_unsolicited_turn_becomes_a_proactive_bubble():
    """No user turn waiting + a report arrives -> a done TurnBuf exists, logged, announced."""
    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    try:
        router = app._MsgRouter()
        seq0 = app._note_seq
        turns0 = set(app._turns.keys())
        for m in (sysmsg(), sysmsg(), assistant("JOB REPORT: the sweep finished, 8/8 green"),
                  result_msg()):
            router.handle(m)
        new = [t for tid, t in app._turns.items() if tid not in turns0]
        check("a proactive TurnBuf is registered", len(new) == 1, f"new turns: {len(new)}")
        buf = new[0] if new else None
        check("it is finished, not left running", buf and buf.status == "done",
              f"status={getattr(buf, 'status', None)}")
        check("it carries the report text", buf and "JOB REPORT" in (buf.final or buf.text),
              f"final={getattr(buf, 'final', None)!r}")
        check("the note counter advanced (UI announce)", app._note_seq == seq0 + 1,
              f"seq {seq0} -> {app._note_seq}")
        check("the note id is announced", buf and buf.id in list(app._note_ids),
              f"ids={list(app._note_ids)}")
        check("it was logged to the transcript",
              any("JOB REPORT" in ln for ln in open(f"{tmp}/log.md").read().splitlines())
              if os.path.exists(f"{tmp}/log.md") else False)
        check("router is idle afterwards", router.current is None and router.waiting is None)
    finally:
        app.SESS = real_sess
        shutil.rmtree(tmp, ignore_errors=True)


def test_chatter_only_notification_makes_no_bubble():
    """Task bookkeeping with nothing visible to say must NOT create an empty bubble."""
    router = app._MsgRouter()
    seq0, turns0 = app._note_seq, set(app._turns.keys())
    for m in (sysmsg(), sysmsg(), result_msg()):
        router.handle(m)
    check("no TurnBuf for silent chatter",
          set(app._turns.keys()) == turns0, "a bubble was created for nothing")
    check("no announce for silent chatter", app._note_seq == seq0)


def test_user_turn_messages_route_to_the_user_queue():
    """A registered user turn gets its own messages; no proactive buf is created."""
    router = app._MsgRouter()
    turns0 = set(app._turns.keys())
    loop = asyncio.new_event_loop()
    try:
        aq = asyncio.Queue()
        router.expect(aq)
        msgs = [assistant("the answer"), result_msg()]
        for m in msgs:
            router.handle(m)
        got = []
        while not aq.empty():
            got.append(loop.run_until_complete(aq.get()))
        check("user turn receives its messages in order", got == msgs,
              f"got {[type(g).__name__ for g in got]}")
        check("no proactive buf for a user turn", set(app._turns.keys()) == turns0)
        check("router idle after the user turn", router.current is None and router.waiting is None)
    finally:
        loop.close()


def test_note_midflight_then_user_turn_waits_its_turn():
    """A background report already streaming keeps its own bubble; the user turn that
    queries meanwhile gets the NEXT turn's messages, not the report's tail."""
    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    try:
        router = app._MsgRouter()
        turns0 = set(app._turns.keys())
        router.handle(assistant("JOB REPORT: half way"))     # unsolicited turn opens
        aq = asyncio.Queue()
        router.expect(aq)                                    # user asks mid-report
        router.handle(assistant("JOB REPORT: done"))         # still the report's turn
        router.handle(result_msg())                          # report turn closes
        user_msgs = [assistant("your answer"), result_msg()]
        for m in user_msgs:
            router.handle(m)
        new = [t for tid, t in app._turns.items() if tid not in turns0]
        check("the report still becomes its own bubble", len(new) == 1 and
              "half way" in (new[0].final or new[0].text) and "done" in (new[0].final or new[0].text),
              f"new={[(getattr(b,'final',None)) for b in new]!r}")
        got = []
        while not aq.empty():
            got.append(aq.get_nowait())
        check("the user queue got ONLY the user turn's messages", got == user_msgs,
              f"got {[type(g).__name__ for g in got]}")
    finally:
        app.SESS = real_sess
        shutil.rmtree(tmp, ignore_errors=True)


def test_stray_idle_message_does_not_swallow_the_next_user_turn():
    """THE DEADLOCK (the user, 2026-09-06: "no voice", turn_busy stuck true).

    The CLI emits a SystemMessage on session init while nobody is waiting. That opened a
    _NoteSink with no ResultMessage to ever close it, so the sink stayed `current` forever.
    The next user turn's messages were then fed to that immortal sink; its ResultMessage
    closed the NOTE (publishing the user's answer as a silent proactive bubble) while the
    user's own queue received NOTHING -> _run_turn blocks on aq.get() -> _turn_lock is
    never released -> every later message prints "waiting - folding into the current turn".
    """
    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    try:
        router = app._MsgRouter()
        router.handle(sysmsg())               # stray init while idle; NO ResultMessage follows
        aq = asyncio.Queue()
        router.expect(aq)                     # user asks a question
        user_msgs = [assistant("your answer"), result_msg("your answer")]
        for m in user_msgs:
            router.handle(m)
        got = []
        while not aq.empty():
            got.append(aq.get_nowait())
        check("the user turn reaches the user queue (else Helix deadlocks)",
              got == user_msgs, f"got {[type(g).__name__ for g in got]} - queue starved")
        check("the user's answer is NOT published as a proactive bubble",
              not any("your answer" in (getattr(t, "final", "") or getattr(t, "text", ""))
                      for t in app._turns.values()),
              "the answer was rerouted into a silent ODIN-UPDATE bubble")
        check("router is idle after the user turn", router.current is None)
    finally:
        app.SESS = real_sess
        shutil.rmtree(tmp, ignore_errors=True)


def test_user_turn_takes_over_a_contentless_open_sink():
    """Same disease, arrival order swapped: the sink is open and has said nothing visible
    when the user queries. The user turn must win it, not stream into the note."""
    router = app._MsgRouter()
    router.handle(sysmsg())
    aq = asyncio.Queue()
    router.expect(aq)
    router.handle(assistant("hello"))
    check("a contentless open sink yields to the waiting user turn",
          not aq.empty(), "user queue starved by an idle sink")


def test_escalated_turn_releases_the_lock_when_the_stream_is_lost():
    """Defense in depth: even if routing loses a turn, the lock must come back so Helix
    stays usable instead of wedging until restart."""
    import threading
    buf = app.TurnBuf("t" + uuid_hex(), "q", "odin")
    real = app._TURN_STREAM_TIMEOUT
    app._TURN_STREAM_TIMEOUT = 1.0
    # order 29: the lost turn is logged -- into a temp SESS, never the user's live session log
    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    try:
        done = threading.Event()

        def run():
            try:
                app._run_escalated(buf, "a question that never streams")
            finally:
                done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        finished = done.wait(timeout=8)
        check("a turn whose stream never arrives ends instead of hanging", finished,
              "_run_escalated blocked forever - this is the wedge")
        check("the turn is marked finished", buf.status in ("error", "done"),
              f"status={buf.status}")
        logged = open(f"{tmp}/log.md").read() if os.path.exists(f"{tmp}/log.md") else ""
        check("the lost turn is logged to the temp SESS, not the live log",
              "a question that never streams" in logged, f"temp log: {logged!r}")
    finally:
        app._TURN_STREAM_TIMEOUT = real
        app.SESS = real_sess
        shutil.rmtree(tmp, ignore_errors=True)


def uuid_hex():
    import uuid as _u
    return _u.uuid4().hex[:8]


def test_transport_failure_wakes_the_waiting_turn():
    """If the stream dies while a user turn waits, the turn must error, not hang."""
    router = app._MsgRouter()
    aq = asyncio.Queue()
    router.expect(aq)
    boom = RuntimeError("transport died")
    router.fail(boom)
    got = aq.get_nowait() if not aq.empty() else None
    check("waiting turn receives the failure", got is boom, f"got={got!r}")
    check("router resets after failure", router.current is None and router.waiting is None)


def test_metrics_exposes_note_feed():
    with app.app.test_client() as tc:
        j = tc.get("/metrics").get_json()
    check("/metrics carries note_seq", "note_seq" in j, f"keys={sorted(j.keys())}")
    check("/metrics carries note_ids", "note_ids" in j)


print("\nHELIX — proactive background-report regression\n" + "=" * 48)
try:
    test_unsolicited_turn_becomes_a_proactive_bubble()
    test_chatter_only_notification_makes_no_bubble()
    test_user_turn_messages_route_to_the_user_queue()
    test_note_midflight_then_user_turn_waits_its_turn()
    test_stray_idle_message_does_not_swallow_the_next_user_turn()
    test_user_turn_takes_over_a_contentless_open_sink()
    test_escalated_turn_releases_the_lock_when_the_stream_is_lost()
    test_transport_failure_wakes_the_waiting_turn()
    test_metrics_exposes_note_feed()
    check("the lane options wrote the wrapper to temp, never the live wall_exec.sh",
          os.path.isfile(app.WALL_EXEC) and app.WALL_EXEC != _REAL_WALL_EXEC,
          f"temp wrapper {app.WALL_EXEC} missing")
finally:
    app.WALL_EXEC = _REAL_WALL_EXEC
    _sh.rmtree(_WALL_TMP, ignore_errors=True)
print("=" * 48)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
