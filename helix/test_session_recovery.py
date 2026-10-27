#!/usr/bin/env python3
"""Regression tests for "Failed to authenticate: OAuth session expired".

Two independent causes, both real:

1. Helix pins a Claude SDK client for the life of the process. The one that hit this had been
   up 8 days; its OAuth session had long expired. Recovery has to retire the client so the next
   turn spawns a CLI that re-reads credentials from disk.

2. The CLI resolves credentials via CLAUDE_CONFIG_DIR. Helix's live credentials are on the HDD
   at ~/claude/config, but a stale ~/.claude/.credentials.json also exists. If the variable is
   ever missing, the CLI silently falls back to those dead credentials and reports exactly this
   error. app.py must not depend on the launcher having exported it.

Run:  .venv/bin/python test_session_recovery.py
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

AUTH_ERR = "Failed to authenticate: OAuth session expired and could not be refreshed"

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── the config dir must be pinned by app.py, not inherited from whoever launched it ──
def test_config_dir_pinned():
    """Launching via anything but start_helix.sh (a stale shell, cron, a hand-run python) leaves
    CLAUDE_CONFIG_DIR unset, and the CLI then reads the dead ~/.claude credentials."""
    import subprocess
    hdd = os.path.expanduser("~/claude/config")
    if not os.path.isdir(hdd):
        print("  SKIP  no ~/claude/config here: Claude uses its default config folder"); return
    probe = (
        "import os,sys;"
        "os.environ.pop('CLAUDE_CONFIG_DIR',None);"      # simulate a launcher that never exported it
        "sys.path.insert(0,%r);"
        "import app;"
        "print(os.environ.get('CLAUDE_CONFIG_DIR','<unset>'))"
    ) % os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env["HELIX_NO_WARMUP"] = "1"
    env["HELIX_NO_PRECONNECT"] = "1"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env=env, cwd=os.path.dirname(os.path.abspath(__file__)))
    got = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else "<no output>"
    check("importing app pins CLAUDE_CONFIG_DIR even when the launcher didn't",
          got == hdd,
          f"got {got!r}; expected {hdd!r} -> CLI would fall back to the stale ~/.claude credentials")


# ── an expired session must not wedge the process until a restart ──
def test_auth_failure_drops_client():
    import app

    class ExpiredClient:
        """The SDK raises the CLI's error text out of receive_messages()."""
        async def query(self, *a, **k):
            pass

        def receive_messages(self):
            async def gen():
                raise Exception(AUTH_ERR)
                yield  # pragma: no cover
            return gen()

        async def disconnect(self):
            pass

    app.CHAT.client = ExpiredClient(); app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True
    q = queue.Queue()

    async def drive():
        # a directly-injected client has no reader; give it one, as _ensure_client would
        app.CHAT.reader_task = asyncio.get_running_loop().create_task(app._read_stream(app.CHAT.client))
        await app._run_turn("what's next on ATTEST?", q)
    asyncio.run(drive())

    msg = ""
    kinds = []
    while not q.empty():
        item = q.get()
        if item is app._STREAM_DONE:
            continue
        kinds.append(item[0])
        if item[0] == "error":
            msg = item[1].get("message", "")

    check("the expired session is reported, not swallowed", "error" in kinds, f"events={kinds}")
    check("the dead client is retired so the next turn re-authenticates", app.CHAT.client is None,
          "client stayed cached -> every later turn fails until Helix is restarted")
    check("the message names the actual remedy (sign in again)",
          "sign in" in msg.lower() or "/login" in msg.lower() or "log in" in msg.lower(),
          f"raw SDK text shown to the user: {msg!r}")
    check("the message does not just dump SDK internals",
          "could not be refreshed" not in msg or "sign in" in msg.lower(),
          f"got: {msg!r}")
    app.CHAT.client = None


# ── after re-login, the very next turn must work with no restart ──
def test_next_turn_after_relogin():
    import app, claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    class ExpiredClient:
        async def query(self, *a, **k):
            pass

        def receive_messages(self):
            async def gen():
                raise Exception(AUTH_ERR)
                yield  # pragma: no cover
            return gen()

        async def disconnect(self):
            pass

    built = {"n": 0}

    class FreshClient:
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
                                    is_error=False, num_turns=1, session_id="s",
                                    total_cost_usd=0.0, usage={}, result="back online")
            return gen()

    app.CHAT.client = ExpiredClient(); app.CHAT.reader_task = None   # injected client: no stale reader
    app.CHAT.primed = True

    async def drive():
        app.CHAT.reader_task = asyncio.get_running_loop().create_task(app._read_stream(app.CHAT.client))
        await app._run_turn("are you there?", queue.Queue())
    asyncio.run(drive())

    real = claude_agent_sdk.ClaudeSDKClient
    claude_agent_sdk.ClaudeSDKClient = FreshClient
    q = queue.Queue()
    try:
        asyncio.run(app._run_turn("try again", q))
    finally:
        claude_agent_sdk.ClaudeSDKClient = real

    kinds = []
    while not q.empty():
        item = q.get()
        if item is not app._STREAM_DONE:
            kinds.append(item[0])
    check("a fresh authenticated client is spawned", built["n"] == 1, f"built={built['n']}")
    check("the next turn succeeds without restarting Helix",
          "final" in kinds and "error" not in kinds, f"events={kinds}")
    app.CHAT.client = None


print("\nHELIX — OAuth session recovery\n" + "=" * 46)
try:
    test_config_dir_pinned()
    test_auth_failure_drops_client()
    test_next_turn_after_relogin()
    check("the lane options wrote the wrapper to temp, never the live wall_exec.sh",
          os.path.isfile(app.WALL_EXEC) and app.WALL_EXEC != _REAL_WALL_EXEC,
          f"temp wrapper {app.WALL_EXEC} missing")
finally:
    app.WALL_EXEC = _REAL_WALL_EXEC
    _sh.rmtree(_WALL_TMP, ignore_errors=True)
print("=" * 46)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
