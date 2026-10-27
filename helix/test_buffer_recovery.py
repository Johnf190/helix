#!/usr/bin/env python3
"""Regression tests for the 1MB JSON buffer failure.

Bug: the Claude Agent SDK caps one stdout JSON line at 1MB. A large tool result trips it,
the SDK's message-reader task dies fatally, and because app.py caches the client in the
_client global and only rebuilds it `if _client is None`, every later turn reuses a client
whose reader is dead. The session stays broken until Helix is restarted.

Run:  .venv/bin/python test_buffer_recovery.py
"""
import os, sys, asyncio, queue

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

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── 1. the transport must be given a buffer ceiling above the 1MB default ──────
def test_buffer_size_configured():
    import claude_agent_sdk
    captured = {}

    class FakeClient:
        def __init__(self, opts):
            captured["opts"] = opts

        async def connect(self):
            pass

    real = claude_agent_sdk.ClaudeSDKClient
    claude_agent_sdk.ClaudeSDKClient = FakeClient
    app.CHAT.client = None
    try:
        asyncio.run(app._ensure_client())
    finally:
        claude_agent_sdk.ClaudeSDKClient = real
        app.CHAT.client = None

    opts = captured.get("opts")
    size = getattr(opts, "max_buffer_size", None)
    check("options carry an explicit max_buffer_size", size is not None,
          "max_buffer_size is None -> SDK falls back to the 1MB default that caused this bug")
    check("ceiling is meaningfully above the 1MB default", bool(size) and size > 8 * 1024 * 1024,
          f"got {size!r}")


# ── 2. a turn that dies must not poison the cached client ─────────────────────
def test_client_reset_after_fatal_error():
    class DeadClient:
        """Stands in for a client whose message reader has died."""
        async def query(self, *a, **k):
            raise Exception("Failed to decode JSON: JSON message exceeded maximum "
                            "buffer size of 1048576 bytes")

        def receive_response(self):
            raise AssertionError("must not be reached")

    app.CHAT.client = DeadClient(); app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True
    q = queue.Queue()
    asyncio.run(app._run_turn("what is the state of D30?", q))

    events = []
    while not q.empty():
        item = q.get()
        if item is app._STREAM_DONE:
            events.append(("done", {}))
        else:
            events.append(item)

    kinds = [e[0] for e in events]
    check("the failed turn reports an error to the UI", "error" in kinds, f"events={kinds}")
    check("the stream is always closed", "done" in kinds, f"events={kinds}")
    check("the dead client is discarded so the next turn reconnects", app.CHAT.client is None,
          "_client still holds the dead object -> every later turn reuses a broken reader")
    app.CHAT.client = None


# ── 3. the user-facing message must be intelligible ──────────────────────────
def test_error_message_is_legible():
    class DeadClient:
        async def query(self, *a, **k):
            raise Exception("Failed to decode JSON: JSON message exceeded maximum "
                            "buffer size of 1048576 bytes")

        def receive_response(self):
            raise AssertionError("must not be reached")

    app.CHAT.client = DeadClient(); app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True
    q = queue.Queue()
    asyncio.run(app._run_turn("hello", q))
    msg = ""
    while not q.empty():
        item = q.get()
        if item is not app._STREAM_DONE and item[0] == "error":
            msg = item[1].get("message", "")
    check("oversized-output failures are explained, not dumped raw",
          "buffer" not in msg.lower() or "too large" in msg.lower() or "reconnect" in msg.lower(),
          f"raw SDK text shown to the user: {msg!r}")
    app.CHAT.client = None


# ── 4. the guarantee that matters: the turn AFTER a blow-up works ────────────
def test_next_turn_recovers():
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    class DeadClient:
        async def query(self, *a, **k):
            raise Exception("Failed to decode JSON: JSON message exceeded maximum "
                            "buffer size of 1048576 bytes")

        def receive_response(self):
            raise AssertionError("must not be reached")

        async def disconnect(self):
            pass

    built = {"n": 0}

    class HealthyClient:
        def __init__(self, opts):
            built["n"] += 1

        async def connect(self):
            pass

        async def query(self, *a, **k):
            pass

        def receive_messages(self):
            # the interface the long-lived reader (_read_stream) consumes
            async def gen():
                yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id="t",
                                    total_cost_usd=0.0, usage={}, result="recovered")
            return gen()

    # turn 1 dies on the oversized message
    app.CHAT.client = DeadClient(); app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True
    q1 = queue.Queue()
    asyncio.run(app._run_turn("read the huge log", q1))

    # turn 2 must transparently build a new client and succeed
    real = claude_agent_sdk.ClaudeSDKClient
    claude_agent_sdk.ClaudeSDKClient = HealthyClient
    q2 = queue.Queue()
    try:
        asyncio.run(app._run_turn("are you there?", q2))
    finally:
        claude_agent_sdk.ClaudeSDKClient = real

    events = []
    while not q2.empty():
        item = q2.get()
        if item is not app._STREAM_DONE:
            events.append(item)
    kinds = [e[0] for e in events]
    check("a fresh client is built for the next turn", built["n"] == 1, f"built={built['n']}")
    check("the next turn succeeds instead of erroring", "final" in kinds and "error" not in kinds,
          f"events={kinds}")
    app.CHAT.client = None


# ── 5. pressing STOP must not cost you the session ───────────────────────────
def test_interrupt_keeps_session():
    class Client:
        async def query(self, *a, **k):
            # the interrupt lands mid-turn, exactly as pressing STOP does
            app.CHAT.interrupting = True
            raise Exception("cancelled")

        def receive_response(self):
            raise AssertionError("must not be reached")

        async def disconnect(self):
            raise AssertionError("STOP must not tear down the client")

    c = Client()
    app.CHAT.client = c; app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True
    q = queue.Queue()
    asyncio.run(app._run_turn("stop that", q))
    check("STOP keeps the live session and its context", app.CHAT.client is c,
          "the client was torn down on a deliberate interrupt")
    app.CHAT.client = None
    app.CHAT.interrupting = False


# ── 6. a turn that dies must still leave a trace of what was being worked on ──
def test_failed_turn_is_logged():
    """The crashed turn takes its CLI session with it. If the question is never written to
    log.md, recent_sessions() can't replay it and he comes back not knowing what he was doing."""
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    real_sess, app.SESS = app.SESS, tmp
    # order 29: /ask_stream reaches _emit_audio -> prune_tts(), which deleted live tts/*.wav
    tmp_tts = tempfile.mkdtemp()
    real_tts, app.TTSDIR = app.TTSDIR, tmp_tts
    real_hist = list(app.HISTORY)
    try:
        app.CHAT.client = None
        app.HISTORY.clear()

        class DeadClient:
            async def query(self, *a, **k):
                raise Exception("Failed to decode JSON: JSON message exceeded maximum "
                                "buffer size of 1048576 bytes")

            def receive_response(self):
                raise AssertionError("must not be reached")

            async def disconnect(self):
                pass

        app.CHAT.client = DeadClient(); app.CHAT.reader_task = None   # injected client: no stale reader
        app.CHAT.primed = True
        client = app.app.test_client()
        r = client.post("/ask_stream", json={"text": "keep auditing the D30 arc", "mode": "odin"})
        body = r.get_data(as_text=True)

        check("the UI is told the turn failed", "error" in body, f"body={body[:120]!r}")
        try:
            log = open(f"{tmp}/log.md").read()
        except Exception:
            log = ""
        check("the unanswered question is written to the session log",
              "keep auditing the D30 arc" in log,
              "log.md has no record -> he returns with no idea what he was working on")
        check("it is replayed by recent_sessions()",
              "keep auditing the D30 arc" in app.recent_sessions(),
              "written but not reachable by the preamble")
    finally:
        app.SESS = real_sess
        app.TTSDIR = real_tts
        app.HISTORY.clear(); app.HISTORY.extend(real_hist)
        app.CHAT.client = None
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp_tts, ignore_errors=True)


print("\nHELIX — 1MB JSON buffer regression\n" + "=" * 46)
try:
    test_buffer_size_configured()
    test_client_reset_after_fatal_error()
    test_error_message_is_legible()
    test_next_turn_recovers()
    test_interrupt_keeps_session()
    test_failed_turn_is_logged()
    check("the lane options wrote the wrapper to temp, never the live wall_exec.sh",
          os.path.isfile(app.WALL_EXEC) and app.WALL_EXEC != _REAL_WALL_EXEC,
          f"temp wrapper {app.WALL_EXEC} missing")
finally:
    app.WALL_EXEC = _REAL_WALL_EXEC
    _sh.rmtree(_WALL_TMP, ignore_errors=True)
print("=" * 46)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
