"""ORDER 34 — the per-turn memory patch, wired into _turn_prompt.

Run with the venv (app imports flask):  .venv/bin/python -m pytest -q test_memory_patch.py
The retriever runs on a fact base and vector cache in tmp_path with a stub embedder; nothing live is
read for retrieval and nothing live is written (PATCHES_LEDGER is pointed at tmp_path).
"""
import asyncio
import json
import os
import queue as _queue
import sys
import time

import pytest

os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
os.environ.setdefault("HELIX_NO_SCHEDULER", "1")
os.environ["HELIX_NO_WARMUP"] = "1"     # its SDK pre-connect builds options, which rewrites the live wall_exec.sh
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, f"{HERE}/workspace/memretrieve")
import app  # noqa: E402
import factgate_hook  # noqa: E402
from test_memretrieve import Stub  # noqa: E402

OPEN = {"bm25": 0.5, "embed": 0.5}


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    """Patch on, a tmp fact base behind app's memretrieve, a tmp PATCHES ledger, empty HISTORY."""
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "111_image_gen.md").write_text("# Helix image generation\nlocal picture generator, start it with render.\n")
    (facts / "96_repo.md").write_text("# Helix published\npublic github repository, MIT licence.\n")
    (facts / "76_boat.md").write_text("# The boat\nboat weight and draft, foam core planing hull.\n")
    mr = app._memretrieve()
    ix = mr.Index(facts_dir=str(facts), cache_dir=str(tmp_path / "cache"), embedder=Stub())
    monkeypatch.setattr(mr, "_DEFAULT", ix)
    monkeypatch.setattr(mr, "FLOORS", dict(OPEN))
    monkeypatch.setenv("HELIX_MEMORY_PATCH", "1")
    led = tmp_path / "PATCHES.jsonl"
    monkeypatch.setattr(app, "PATCHES_LEDGER", str(led))
    monkeypatch.setattr(app, "HISTORY", [])
    monkeypatch.setattr(app, "_crosstalk_odin", lambda: "")
    monkeypatch.setattr(app, "_preamble", lambda: "PREAMBLE")
    monkeypatch.setattr(app, "_inline_names", lambda: set())
    lane = app.Lane("chat-test34")

    class W:
        pass
    w = W()
    w.facts, w.ix, w.mr, w.led, w.lane, w.capsys = facts, ix, mr, led, lane, capsys
    w.rows = lambda: [json.loads(l) for l in led.read_text().splitlines()] if led.exists() else []
    return w


def patch_of(prompt):
    return prompt.split(app._memretrieve().PATCH_HEADER)[1] if app._memretrieve().PATCH_HEADER in prompt else ""


# ------------------------------------------------------------------ the shape of a turn

def test_a_tool_lane_turn_ends_with_the_patch_after_the_user_text(world):
    world.lane.primed = True
    p = app._turn_prompt(world.lane, "how do I start the local picture generator")
    head = world.mr.PATCH_HEADER
    assert p.startswith("how do I start the local picture generator\n\n" + head)
    assert "# memory/facts/111_image_gen.md\n# Helix image generation" in p
    [row] = world.rows()
    assert row["lane"] == "chat-test34" and row["reason"] == "ok" and row["arm"] == app.PATCH_ARM
    assert row["files"][0]["file"] == "111_image_gen.md" and row["files"][0]["mode"] == "full"
    assert isinstance(row["files"][0]["score"], float) and row["ts"].endswith("Z")


def test_the_priming_turn_gets_the_preamble_then_the_text_then_the_patch(world):
    p = app._turn_prompt(world.lane, "public github repository")
    assert p.startswith("PREAMBLE\n\n=== USER ===\npublic github repository\n\n" + world.mr.PATCH_HEADER)


def test_the_no_tools_lane_gets_nothing(world, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the front desk must never reach the retriever")
    monkeypatch.setattr(app, "_memory_patch", boom)
    for lane in (app.FRONT, app.Lane("nt", no_tools=True)):
        assert app._turn_prompt(lane, "how do I start the local picture generator") == \
            "how do I start the local picture generator"
    assert world.rows() == []


def test_header_text_is_the_orders():
    assert app._memretrieve().PATCH_HEADER == (
        "=== MEMORY PATCH (facts likely relevant to this turn — open in full before citing) ===")
    assert (app.PATCH_K, app.PATCH_BUDGET, app.PATCH_TIMEOUT) == (5, 6000, 1.5)


# ------------------------------------------------------------------ floor, skip, query

def test_below_the_floor_the_patch_is_empty_and_the_row_says_why(world):
    world.lane.primed = True
    assert app._turn_prompt(world.lane, "ok") == "ok"
    [row] = world.rows()
    assert row["reason"] == "below floor" and row["files"] == [] and row["chars"] == 0


def test_ok_after_a_topic_still_gets_no_patch(world):
    """Odin's live check b: the newest turn is the gate; older turns only shape the ranking."""
    world.lane.primed = True
    app.HISTORY.append(("you", "how do I start the local picture generator"))
    assert app._turn_prompt(world.lane, "ok") == "ok"


def test_facts_inlined_in_the_preamble_are_skipped(world, monkeypatch):
    monkeypatch.setattr(app, "_inline_names", lambda: {"111_image_gen.md"})
    p = app._turn_prompt(world.lane, "local picture generator github")          # priming turn
    assert "111_image_gen.md" not in patch_of(p) and "96_repo.md" in patch_of(p)
    assert world.lane.mem_sent >= {"111_image_gen.md", "96_repo.md"}


def test_a_fact_already_patched_this_session_is_not_sent_again(world):
    world.lane.primed = True
    p1 = app._turn_prompt(world.lane, "local picture generator")
    assert "111_image_gen.md" in patch_of(p1)
    p2 = app._turn_prompt(world.lane, "local picture generator again")
    assert "111_image_gen.md" not in patch_of(p2)
    # a new session (re-prime) starts from the preamble's inline set again
    world.lane.primed = False
    p3 = app._turn_prompt(world.lane, "local picture generator")
    assert "111_image_gen.md" in patch_of(p3)


def test_the_query_is_the_last_three_user_turns_newest_first(world):
    app.HISTORY.extend([("you", "one"), ("odin", "x"), ("alice", "y"), ("you", "two"), ("odin", "z"),
                        ("you", "three")])
    assert app._user_turns("four") == ["four", "three", "two"]
    assert app._user_turns("only", n=1) == ["only"]


def test_a_fact_remembered_mid_session_reaches_the_next_turn(world):
    world.lane.primed = True
    app._turn_prompt(world.lane, "boat draft")
    (world.facts / "200_new_rule.md").write_text("# New rule\nthe zeppelin hangar is off limits\n")
    p = app._turn_prompt(world.lane, "zeppelin hangar")
    assert "# memory/facts/200_new_rule.md" in p


# ------------------------------------------------------------------ never fails a turn

def test_timeout_means_no_patch_one_log_line_and_the_prompt_still_returns(world, monkeypatch):
    import threading
    monkeypatch.setattr(app, "PATCH_TIMEOUT", 0.2)
    real = world.ix.retrieve
    release = threading.Event()

    def slow(*a, **k):                                 # stuck until the test has its answer
        release.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(world.ix, "retrieve", slow)
    world.lane.primed = True
    t0 = time.monotonic()
    p = app._turn_prompt(world.lane, "local picture generator")
    dt = time.monotonic() - t0
    release.set()
    assert p == "local picture generator"
    assert 0.2 <= dt < 1.5, dt                         # gave up at the timeout, did not wait it out
    out = world.capsys.readouterr().out
    assert out.count("[patch] no memory patch this turn — timeout") == 1, out
    [row] = world.rows()
    assert row["reason"] == "timeout" and row["files"] == []
    app._patch_pool.submit(lambda: None).result(5)     # the stray worker is done before teardown


def test_an_error_means_no_patch_and_one_log_line(world, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("index on fire")
    monkeypatch.setattr(world.ix, "retrieve", broken)
    world.lane.primed = True
    assert app._turn_prompt(world.lane, "local picture generator") == "local picture generator"
    out = world.capsys.readouterr().out
    assert out.count("[patch] no memory patch this turn — error RuntimeError: index on fire") == 1, out
    assert world.rows()[0]["reason"] == "error RuntimeError"


def test_a_turn_with_a_timed_out_patch_still_runs_to_its_result(world, monkeypatch):
    """The whole turn, through _run_turn with a fake client: retrieval times out, the turn answers."""
    from claude_agent_sdk import ResultMessage
    monkeypatch.setattr(app, "PATCH_TIMEOUT", 0.1)
    monkeypatch.setattr(world.ix, "retrieve", lambda *a, **k: time.sleep(0.6) or {"hits": [], "reason": "x", "gate": {}})
    sent = []

    class Client:
        async def query(self, prompt, *a, **k):
            sent.append(prompt)

        def receive_messages(self):
            async def gen():
                yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=5, is_error=False,
                                    num_turns=1, session_id="s", total_cost_usd=0.0, usage={}, result="answered")
            return gen()

        async def disconnect(self):
            pass
    lane = world.lane
    lane.client, lane.primed = Client(), True
    q = _queue.Queue()

    async def drive():
        lane.reader_task = asyncio.get_running_loop().create_task(app._read_stream(lane.client, lane))
        await app._run_turn("local picture generator", q, lane)
    monkeypatch.setattr(app, "_mark_model_state", lambda *a, **k: None)
    monkeypatch.setattr(app, "_log_turn_usage", lambda *a, **k: None)
    asyncio.run(drive())
    events = []
    while not q.empty():
        events.append(q.get())
    assert sent == ["local picture generator"]
    assert ("final", {"text": "answered"}) in events
    assert not [e for e in events if isinstance(e, tuple) and e[0] == "error"]
    time.sleep(0.7)


# ------------------------------------------------------------------ switches and the ledger

def test_helix_memory_patch_0_turns_it_off(world, monkeypatch):
    monkeypatch.setenv("HELIX_MEMORY_PATCH", "0")
    world.lane.primed = True
    assert app._turn_prompt(world.lane, "local picture generator") == "local picture generator"
    assert world.rows() == []


def test_off_and_unlogged_when_app_is_imported_not_run(monkeypatch):
    monkeypatch.delenv("HELIX_MEMORY_PATCH", raising=False)
    assert app._PATCH_DEFAULT is False and app._patch_on() is False
    assert app.PATCHES_LEDGER is None


def test_a_failing_ledger_write_logs_one_line_and_never_raises(world, monkeypatch):
    monkeypatch.setattr(app, "PATCHES_LEDGER", str(world.facts.parent / "no" / "such" / "P.jsonl"))
    world.lane.primed = True
    p = app._turn_prompt(world.lane, "local picture generator")
    assert "111_image_gen.md" in p
    assert world.capsys.readouterr().out.count("[patch] could not append") == 1


def test_the_ledger_is_appended_never_rewritten(world):
    world.led.write_text('{"ts": "2026-10-01T00:00:00Z", "lane": "old"}\n')
    world.lane.primed = True
    app._turn_prompt(world.lane, "local picture generator")
    app._turn_prompt(world.lane, "ok")
    rows = world.rows()
    assert [r.get("reason") for r in rows] == [None, "ok", "below floor"] and rows[0]["lane"] == "old"


def test_patches_ledger_is_walled_and_guarded():
    p = os.path.realpath(f"{app.BRAIN}/workspace/cycle/PATCHES.jsonl")
    assert p in [os.path.realpath(w) for w in app.WALL_PATHS]
    import shutil
    assert f"--ro-bind-try {p} {p}" in app._wall_script(shutil.which("bwrap") or "/usr/bin/bwrap")
    assert factgate_hook._build_path_owns(p)
    assert app.PATCHES_LEDGER is None or app.PATCHES_LEDGER == p


# ------------------------------------------------------------------ ORDER 42c: superseded facts never inline

def test_42c_a_superseded_fact_is_a_pointer_and_its_row_names_the_replacement(world):
    (world.facts / "10_old.md").write_text("# 🔴 SUPERSEDED by `20_new.md`\n# old\nquasar lantern oldbody\n")
    (world.facts / "20_new.md").write_text("# lantern, current\nquasar lantern current\n")
    world.lane.primed = True
    patch = patch_of(app._turn_prompt(world.lane, "quasar lantern oldbody"))
    assert "- memory/facts/10_old.md — SUPERSEDED by memory/facts/20_new.md (newer, used instead)" in patch
    assert "oldbody" not in patch and "# memory/facts/20_new.md\n# lantern, current" in patch
    [row] = world.rows()
    assert row["reason"] == "ok" and row["chars"] > 0
    files = {e["file"]: e for e in row["files"]}
    old = files["10_old.md"]
    assert old == {"file": "10_old.md", "score": old["score"], "mode": "pointer", "replaced_by": "20_new.md"}
    assert files["20_new.md"]["mode"] == "full"
    assert all(set(e) == {"file", "score", "mode"} for f, e in files.items() if f != "10_old.md")
    assert world.lane.mem_sent >= {"10_old.md", "20_new.md"}


def test_42c_a_replacement_already_sent_is_named_not_resent(world):
    (world.facts / "10_old.md").write_text("# 🔴 SUPERSEDED by `20_new.md`\n# old\nquasar lantern oldbody\n")
    (world.facts / "20_new.md").write_text("# lantern, current\nquasar lantern current\n")
    world.lane.primed = True
    world.lane.mem_sent = {"20_new.md"}
    patch = patch_of(app._turn_prompt(world.lane, "quasar lantern oldbody"))
    assert "SUPERSEDED by memory/facts/20_new.md (newer, used instead)" in patch
    assert "# memory/facts/20_new.md" not in patch and "oldbody" not in patch
    [row] = world.rows()
    assert [e for e in row["files"] if e["file"] == "10_old.md"][0]["replaced_by"] == "20_new.md"
    assert "20_new.md" not in [e["file"] for e in row["files"]]
