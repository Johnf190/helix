"""Tests for factgate_hook.py — the gate on Odin's ordinary write path.

Every test points the hook at a tmp_path copy of a facts directory and a tmp factgate
state dir. **Nothing here touches ~/helix/memory/ or workspace/factgate/state/.**

The hooks are async (the SDK's HookCallback returns an Awaitable), so each test drives
them with asyncio.run rather than pulling in a pytest plugin.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factgate_hook  # noqa: E402

FACTGATE_REAL = Path.home() / "helix/workspace/factgate/factgate.py"


# ------------------------------------------------------------------ scaffolding

def call_pre(tool, tool_input, cwd, tool_use_id="tu_1", session_id="s_1"):
    return asyncio.run(factgate_hook.pre_tool_use(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
         "cwd": str(cwd), "session_id": session_id, "tool_use_id": tool_use_id,
         "transcript_path": "/dev/null"}, tool_use_id, None))


def call_post(tool, tool_input, cwd, tool_use_id="tu_1", session_id="s_1"):
    return asyncio.run(factgate_hook.post_tool_use(
        {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input,
         "cwd": str(cwd), "session_id": session_id, "tool_use_id": tool_use_id,
         "tool_response": {"ok": True}, "transcript_path": "/dev/null"}, tool_use_id, None))


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def reason(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A facts dir, a factgate state dir, and a catches file — all disposable."""
    facts = tmp_path / "facts"
    facts.mkdir()
    for i in range(3):
        (facts / f"{i:03d}_fact.md").write_text(f"# fact {i}\n\nsomething true.\n")
    state = tmp_path / "state"
    state.mkdir()
    catches = tmp_path / "CATCHES.jsonl"
    monkeypatch.setattr(factgate_hook, "FACTS", str(facts))
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(FACTGATE_REAL))
    monkeypatch.setattr(factgate_hook, "FACTGATE_STATE", str(state))
    monkeypatch.setattr(factgate_hook, "FACTGATE_DIR", str(tmp_path / "factgate_dir"))
    (tmp_path / "factgate_dir").mkdir()
    monkeypatch.setattr(factgate_hook, "CATCHES", str(catches))
    assert subprocess.run([sys.executable, str(FACTGATE_REAL), "--state-dir", str(state),
                           "baseline", str(facts)], capture_output=True).returncode == 0
    return {"facts": facts, "state": state, "catches": catches, "cwd": tmp_path,
            "factgate_dir": tmp_path / "factgate_dir"}


def dirty(rig):
    (rig["facts"] / "000_fact.md").write_text("# fact 0\n\nquietly reworded.\n")


def catches_lines(rig):
    p = rig["catches"]
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------------- P1

@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_p1_allows_a_fact_write_while_the_base_is_clean(rig, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    out = call_pre(tool, {key: str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) is None, out
    assert catches_lines(rig) == [], "an allow is not a catch"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_p1_denies_a_fact_write_while_the_base_is_dirty(rig, tool):
    dirty(rig)
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    out = call_pre(tool, {key: str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out
    assert "CHANGED OFF-LEDGER" in reason(out), reason(out)
    assert "000_fact.md" in reason(out)


def test_p1_deny_says_who_can_clear_it(rig):
    """Odin cannot clear this himself (P5), so the reason must say so plainly."""
    dirty(rig)
    out = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny"
    low = reason(out).lower()
    assert "user" in low or "human" in low, reason(out)


def test_p1_fails_closed_when_factgate_is_missing(rig, monkeypatch):
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(rig["cwd"] / "not_here.py"))
    out = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out


def test_p1_fails_closed_when_factgate_hangs(rig, monkeypatch, tmp_path):
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(sleeper))
    monkeypatch.setattr(factgate_hook, "TIMEOUT", 1)
    out = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out
    assert "timed out" in reason(out).lower() or "timeout" in reason(out).lower()


def test_two_denied_writes_in_a_row_are_two_denies(rig):
    dirty(rig)
    a = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    b = call_pre("Write", {"file_path": str(rig["facts"] / "005_new.md")}, rig["cwd"])
    assert decision(a) == decision(b) == "deny"
    assert len(catches_lines(rig)) == 2


# ------------------------------------------------------------------------- P2

def test_p2_denies_a_write_into_a_subdirectory_of_facts(rig):
    sub = rig["facts"] / "archive"
    sub.mkdir()
    out = call_pre("Write", {"file_path": str(sub / "old.md")}, rig["cwd"])
    assert decision(out) == "deny", out
    assert "subdirector" in reason(out).lower(), reason(out)


# ------------------------------------------------------------------------- P3

def test_p3_denies_odin_editing_factgate_itself(rig):
    out = call_pre("Edit", {"file_path": str(rig["factgate_dir"] / "factgate.py")}, rig["cwd"])
    assert decision(out) == "deny", out
    assert "own" in reason(out).lower() or "checks" in reason(out).lower()


# ------------------------------------------------------------------------- P4

WRITE_PATTERNS = [
    "echo x > {f}/00_example_72f9.md",
    "echo x >> {f}/00_example_72f9.md",
    "echo x | tee {f}/00_example_72f9.md",
    "sed -i s/a/b/ {f}/00_example_72f9.md",
    "perl -i -pe s/a/b/ {f}/00_example_72f9.md",
    "mv /tmp/x {f}/00_example_72f9.md",
    "cp /tmp/x {f}/00_example_72f9.md",
    "rm {f}/00_example_72f9.md",
    "truncate -s 0 {f}/00_example_72f9.md",
    "touch {f}/00_example_72f9.md",
    "ln -s /tmp/x {f}/00_example_72f9.md",
    "chmod 777 {f}/00_example_72f9.md",
    "install /tmp/x {f}/00_example_72f9.md",
    "dd if=/tmp/x of={f}/00_example_72f9.md",
]


@pytest.mark.parametrize("template", WRITE_PATTERNS)
def test_p4_denies_every_bash_write_into_facts(rig, template):
    out = call_pre("Bash", {"command": template.format(f=rig["facts"])}, rig["cwd"])
    assert decision(out) == "deny", f"{template} -> {out}"
    assert "Write/Edit" in reason(out), reason(out)


@pytest.mark.parametrize("cmd", ["cat {f}/00_example_72f9.md", "grep -r thing {f}",
                                 "ls -l {f}", "wc -l {f}/00_example_72f9.md"])
def test_p4_allows_harmless_reads_of_facts(rig, cmd):
    out = call_pre("Bash", {"command": cmd.format(f=rig["facts"])}, rig["cwd"])
    assert decision(out) is None, f"{cmd} -> {out}"


def test_p4_catches_a_relative_path_from_cwd(rig, monkeypatch):
    """Odin's cwd is ~/helix/workspace, so `../memory/facts/x` resolves into the base."""
    cwd = rig["facts"].parent / "workspace"
    cwd.mkdir()
    out = call_pre("Bash", {"command": "echo x > ../facts/00_example_72f9.md"}, cwd)
    assert decision(out) == "deny", out


def test_p4_catches_a_symlinked_path(rig, tmp_path):
    link = tmp_path / "factlink"
    link.symlink_to(rig["facts"])
    out = call_pre("Bash", {"command": f"echo x > {link}/00_example_72f9.md"}, rig["cwd"])
    assert decision(out) == "deny", out


def test_p1_catches_a_symlinked_write_target(rig, tmp_path):
    link = tmp_path / "factlink2"
    link.symlink_to(rig["facts"])
    dirty(rig)
    out = call_pre("Write", {"file_path": str(link / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out


def test_a_bash_write_somewhere_else_is_allowed(rig):
    out = call_pre("Bash", {"command": "echo x > /tmp/harmless.txt"}, rig["cwd"])
    assert decision(out) is None, out


# ------------------------------------------------------------------------- P5

@pytest.mark.parametrize("cmd", [
    # the recording verbs are refused wherever factgate is invoked from
    "python3 workspace/factgate/factgate.py record x y --reason z",
    "python3 workspace/factgate/factgate.py record-delete x y --reason z",
    "python3 workspace/factgate/factgate.py baseline x",
    # a write to what factgate owns. CHANGED IN ORDER 13: these two used to be written
    # with a relative `workspace/factgate/...` path, which never matched the rig's
    # monkeypatched FACTGATE_DIR — the old substring rule denied them on the word alone,
    # so the mismatch was invisible. Now the rule asks where the write lands, so the
    # path has to be the one under test.
    "echo x > {fg}/state/FACTS_LEDGER.jsonl",
    "rm {fg}/factgate.py",
])
def test_p5_denies_odin_touching_factgate(rig, cmd):
    cmd = cmd.format(fg=rig["factgate_dir"])
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    assert decision(out) == "deny", f"{cmd} -> {out}"


def test_p5_leaves_factgate_check_allowed(rig):
    out = call_pre("Bash", {"command": "python3 workspace/factgate/factgate.py check "
                                       "~/helix/memory/facts"}, rig["cwd"])
    assert decision(out) is None, out


# --------------------------------------------------------------- fail open/closed

def test_a_malformed_input_for_a_non_fact_tool_fails_open(rig):
    out = call_pre("Bash", {}, rig["cwd"])            # no command at all
    assert decision(out) is None, out
    out = call_pre("Grep", None, rig["cwd"])          # tool_input not a dict
    assert decision(out) is None, out


def test_an_exception_on_a_fact_path_fails_closed(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected at the check stage")
    monkeypatch.setattr(factgate_hook, "_factgate_check", boom)
    out = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out
    assert "injected" in reason(out), reason(out)


def test_an_exception_off_the_fact_path_fails_open(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected at the resolve stage")
    monkeypatch.setattr(factgate_hook, "_targets", boom)
    out = call_pre("Bash", {"command": "ls /tmp"}, rig["cwd"])
    assert decision(out) is None, out


def test_the_hooks_never_raise(rig, monkeypatch):
    monkeypatch.setattr(factgate_hook, "_targets",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    call_pre("Write", {"file_path": str(rig["facts"] / "z.md")}, rig["cwd"])
    call_post("Write", {"file_path": str(rig["facts"] / "z.md")}, rig["cwd"])


# ------------------------------------------------------------------- post hook

def test_post_hook_records_exactly_one_ledger_line(rig):
    before = len((rig["state"] / "FACTS_LEDGER.jsonl").read_text().splitlines())
    (rig["facts"] / "004_new.md").write_text("# new\nCategory: helix\n\nwritten by odin.\n")
    out = call_post("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    after = (rig["state"] / "FACTS_LEDGER.jsonl").read_text().splitlines()
    assert len(after) == before + 1, out
    rec = json.loads(after[-1])
    assert rec["file"] == "004_new.md" and rec["kind"] == "record"
    assert "tu_1" in rec["reason"] and "s_1" in rec["reason"]
    assert subprocess.run([sys.executable, str(FACTGATE_REAL), "--state-dir", str(rig["state"]),
                           "check", str(rig["facts"])], capture_output=True).returncode == 0


def test_post_hook_treats_unchanged_as_fine(rig):
    out = call_post("Write", {"file_path": str(rig["facts"] / "000_fact.md")}, rig["cwd"])
    assert "not" not in (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_post_hook_warns_plainly_when_it_cannot_record(rig, monkeypatch):
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(rig["cwd"] / "gone.py"))
    (rig["facts"] / "004_new.md").write_text("# new\n\nwritten by odin.\n")
    out = call_post("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "not" in ctx.lower() and "record" in ctx.lower(), out
    assert "nightly" in ctx.lower(), out


def test_post_hook_asks_for_a_category_line_when_that_is_the_only_refusal(rig):
    # ORDER 76: F5 alone is Odin's own fix (a line in his own new fact), not "Tell the user"
    before = (rig["state"] / "FACTS_LEDGER.jsonl").read_text()
    (rig["facts"] / "004_new.md").write_text("# new\n\nwritten by odin, no category.\n")
    out = call_post("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    assert ctx == ("factgate did NOT record 004_new.md: it has no valid Category: line. Add one "
                   "near the top (names: workspace/categories/CATEGORIES.txt), then write the "
                   "file again."), out
    assert (rig["state"] / "FACTS_LEDGER.jsonl").read_text() == before


def test_post_hook_keeps_tell_user_when_f5_is_not_the_only_refusal(rig):
    led = rig["state"] / "FACTS_LEDGER.jsonl"
    led.write_text(led.read_text().replace("000_fact.md", "009_fact.md", 1))   # F1 fails too
    (rig["facts"] / "004_new.md").write_text("# new\n\nwritten by odin, no category.\n")
    out = call_post("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Tell the user" in ctx and "has no valid Category: line" not in ctx, out


def test_post_hook_ignores_writes_outside_the_fact_base(rig):
    out = call_post("Write", {"file_path": "/tmp/whatever.md"}, rig["cwd"])
    assert out == {} or not (out or {}).get("hookSpecificOutput"), out


# --------------------------------------------------------------------- catches

def test_every_deny_appends_one_catches_line(rig):
    dirty(rig)
    call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    lines = catches_lines(rig)
    assert len(lines) == 1, lines
    c = lines[0]
    assert c["stage"] == "pre-commit" and c["caught_by"] == "factgate-hook"
    assert c["fixed_in"] == "refused"
    assert "P1" in c["where"] and "004_new.md" in c["where"]
    assert c["n"] >= 1 and c["date"]


def test_catches_n_continues_from_the_existing_file(rig):
    rig["catches"].write_text(json.dumps({"n": 41, "date": "2026-01-01", "stage": "pre-run",
                                          "caught_by": "x", "where": "y", "what": "z",
                                          "fixed_in": "w"}) + "\n")
    call_pre("Bash", {"command": f"rm {rig['facts']}/00_example_72f9.md"}, rig["cwd"])
    lines = catches_lines(rig)
    assert len(lines) == 2 and lines[0]["n"] == 41 and lines[1]["n"] == 42


def test_a_broken_catches_log_does_not_turn_a_deny_into_an_allow(rig, monkeypatch):
    monkeypatch.setattr(factgate_hook, "CATCHES", "/proc/nonexistent/CATCHES.jsonl")
    dirty(rig)
    out = call_pre("Write", {"file_path": str(rig["facts"] / "004_new.md")}, rig["cwd"])
    assert decision(out) == "deny", out


def test_the_hook_has_no_escape_hatch():
    import re
    src = (Path(__file__).resolve().parent / "factgate_hook.py").read_text()
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check|FACTGATE_OFF|maintenance",
                         l, flags=re.I)]
    assert hits == [], hits


# ==========================================================================
# ORDER 13 — stop refusing harmless reads.
#
# In its first 30 minutes live, the hook refused five of Odin's Bash commands.
# ONE was real (#17, a shell append to a fact). FOUR were read-only false
# positives, all for the same two reasons: `_WRITE_RE` matched a bare `>` (so
# `2>&1` and `2>/dev/null` read as writes), and P5 fired on the SUBSTRING
# "factgate" (so `factgate_hook.py` or `139_example_d22c.md` in a filename
# counted as touching the tool).
#
# These tests use the REAL paths, because the commands in the live catches log
# name them. Nothing here writes: every case is a Bash rule, which is pure path
# and string logic with no subprocess. Only the catches file is redirected.
# ==========================================================================

REAL_FACTS = str(Path.home() / "helix/memory/facts")
REAL_FG_DIR = str(Path.home() / "helix/workspace/factgate")


@pytest.fixture
def real_paths(tmp_path, monkeypatch):
    """The real guarded paths, with the catches log sent to a scratch file."""
    monkeypatch.setattr(factgate_hook, "FACTS", REAL_FACTS)
    monkeypatch.setattr(factgate_hook, "FACTGATE_DIR", REAL_FG_DIR)
    monkeypatch.setattr(factgate_hook, "CATCHES", str(tmp_path / "CATCHES.jsonl"))
    return {"cwd": Path.home() / "helix", "catches": tmp_path / "CATCHES.jsonl"}


# The four live false positives. #14, #15 and #18 are truncated at 120 chars in
# CATCHES.jsonl; the tails are reconstructed from order 13's table and marked below.
LIVE_FALSE_POSITIVES = {
    # verbatim from the log, plus a reconstructed tail "…_211455/app.py 2>/dev/null"
    "#14": "cd ~/helix; sha256sum app.py factgate_hook.py test_factgate_hook.py "
           "build_orders/quarantine/order12_backup_20260924_211455/app.py 2>/dev/null",
    # verbatim to "find workspace -name factgate.", tail reconstructed from the table
    "#15": 'cd ~/helix; curl -s -o /dev/null -w "stat %{http_code}\\n" '
           'http://127.0.0.1:7777/stat; F=$(find workspace -name factgate.py); '
           'python3 $F check memory/facts; .venv/bin/python -m pytest -q '
           'test_factgate_hook.py 2>&1 | tail -2',
    # verbatim and complete (81 chars, not truncated)
    "#16": "cd ~/helix; .venv/bin/python -m pytest -q test_factgate_hook.py 2>&1 | tail -2",
    # verbatim to "find . -path ./tts -prune -o", tail reconstructed from the table
    "#18": "cd ~/helix; date +%H:%M; ls -l --time-style=+%H:%M "
           "build_orders/reports/ORDER_12_report.md; find . -path ./tts -prune -o "
           "-newer memory/facts/139_example_d22c.md -print 2>/dev/null",
}


@pytest.mark.parametrize("catch", sorted(LIVE_FALSE_POSITIVES))
def test_the_live_false_positives_are_allowed(real_paths, catch):
    out = call_pre("Bash", {"command": LIVE_FALSE_POSITIVES[catch]}, real_paths["cwd"])
    assert decision(out) is None, f"{catch} still refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


ALSO_ALLOWED = [
    # Odin must be able to read his own ledger entries without asking the builder
    "cat ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl | tail -1",
    "wc -l ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "tail -3 ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "grep 139_ ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "sha256sum ~/helix/workspace/factgate/factgate.py",
    # a redirect that lands somewhere harmless, in a command that names the fact base
    "grep x ~/helix/memory/facts/00_example_72f9.md > /tmp/out",
    "ls ~/helix/memory/facts > /dev/null 2>&1",
    # the sanctioned check, with stderr folded in
    "python3 ~/helix/workspace/factgate/factgate.py check ~/helix/memory/facts 2>&1",
]


@pytest.mark.parametrize("cmd", ALSO_ALLOWED)
def test_reads_and_harmless_redirects_are_allowed(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, real_paths["cwd"])
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"


STILL_DENIED = [
    # the real one (#17), and the same with harmless redirects appended
    "echo x >> ~/helix/memory/facts/00_example_72f9.md",
    "echo x >> ~/helix/memory/facts/00_example_72f9.md 2>/dev/null",
    "echo x >> ~/helix/memory/facts/00_example_72f9.md 2>&1",
    # every redirect operator
    "echo x > ~/helix/memory/facts/a.md",
    "echo x >| ~/helix/memory/facts/a.md",
    "echo x &> ~/helix/memory/facts/a.md",
    "echo x 1> ~/helix/memory/facts/a.md",
    "echo x >> ~/helix/memory/facts/a.md",
    # every spelling of the path
    "echo x > $HOME/helix/memory/facts/a.md",
    "echo x > __HOME__/helix/memory/facts/a.md",
    # a quoted target with a space in it
    'echo x > "~/helix/memory/facts/a file.md"',
    # unresolvable targets, with the path mentioned -> fail closed
    "F=~/helix/memory/facts/a.md; echo x > $F",
    "echo x > ~/helix/memory/facts/*.md",
    # the write-word rule, unchanged
    "cat a > /tmp/x; cp /tmp/x ~/helix/memory/facts/",
    "rm ~/helix/memory/facts/00_example_72f9.md",
    "sed -i s/a/b/ ~/helix/memory/facts/00_example_72f9.md",
    # factgate's own state and its recording verbs
    "echo {} > ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "tee -a ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "python3 ~/helix/workspace/factgate/factgate.py record ~/helix/memory/facts a.md --reason z",
    "python3 ~/helix/workspace/factgate/factgate.py record-delete ~/helix/memory/facts a.md --reason z",
    "python3 ~/helix/workspace/factgate/factgate.py baseline ~/helix/memory/facts",
    "sed -i s/a/b/ ~/helix/workspace/factgate/factgate.py",
    # the hook's own files: narrowing the mention test must not un-guard these
    "rm ~/helix/factgate_hook.py",
    "echo x > ~/helix/test_factgate_hook.py",
]


@pytest.mark.parametrize("cmd", STILL_DENIED)
def test_nothing_that_was_refused_is_now_allowed(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, real_paths["cwd"])
    assert decision(out) == "deny", f"{cmd!r} is now ALLOWED — that is a new hole"
    assert real_paths["catches"].exists(), "a deny must still be logged"


def test_a_relative_write_from_odins_cwd_is_denied(real_paths):
    """Odin's cwd is ~/helix/workspace, so `../memory/facts/a.md` lands in the base."""
    out = call_pre("Bash", {"command": "echo x > ../memory/facts/a.md"},
                   Path.home() / "helix/workspace")
    assert decision(out) == "deny", reason(out)


def test_a_symlinked_redirect_target_is_denied(rig, tmp_path):
    link = tmp_path / "L"
    link.symlink_to(rig["facts"])
    out = call_pre("Bash", {"command": f"echo x > {link}/a.md"}, rig["cwd"])
    assert decision(out) == "deny", reason(out)


def test_fd_duplications_and_dev_null_are_not_writes(real_paths):
    """The two forms that caused three of the four false positives."""
    for cmd in ("ls ~/helix/memory/facts 2>&1", "ls ~/helix/memory/facts 1>&2",
                "ls ~/helix/memory/facts >/dev/null", "ls ~/helix/memory/facts 2>/dev/null",
                "ls ~/helix/memory/facts &>/dev/null", "ls ~/helix/memory/facts >>/dev/null"):
        assert decision(call_pre("Bash", {"command": cmd}, real_paths["cwd"])) is None, cmd


def test_factgate_the_word_only_counts_as_the_tool(real_paths):
    """A filename with `factgate` inside a longer identifier is not the tool."""
    for cmd in ("wc -l factgate_hook.py", "ls test_factgate_hook.py",
                "cat memory/facts/139_example_d22c.md"):
        assert decision(call_pre("Bash", {"command": cmd}, real_paths["cwd"])) is None, cmd


# ==========================================================================
# ORDER 14 — the hook must guard its OWN files from the editor tools.
#
# Order 13 made Bash treat factgate_hook.py and test_factgate_hook.py as
# factgate's (`_factgate_paths()`), so `rm ~/helix/factgate_hook.py` is refused.
# But P3 — the editor-tool rule — only ever tested FACTGATE_DIR, and the hook's
# own files live in ~/helix, outside it. So a plain Edit of the hook was allowed:
# the most direct way to switch the gate off, open since order 12.
#
# These use the REAL hook paths. Nothing is written: pre_tool_use only decides.
# ==========================================================================

HOOK_FILE = str(Path.home() / "helix/factgate_hook.py")
HOOK_TEST_FILE = str(Path.home() / "helix/test_factgate_hook.py")
HELIX = Path.home() / "helix"


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_p3_guards_the_hook_file_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: HOOK_FILE}, HELIX)
    assert decision(out) == "deny", f"{tool} of the hook itself was ALLOWED: {out}"
    assert "P3" in real_paths["catches"].read_text()


def test_p3_guards_the_hooks_own_test_file(real_paths):
    out = call_pre("Edit", {"file_path": HOOK_TEST_FILE}, HELIX)
    assert decision(out) == "deny", out


@pytest.mark.parametrize("spelling", [
    "../factgate_hook.py",                       # relative, from Odin's cwd
    "$HOME/helix/factgate_hook.py",
    "~/helix/factgate_hook.py",
])
def test_p3_catches_every_spelling_of_the_hook_path(real_paths, spelling):
    cwd = HELIX / "workspace" if spelling.startswith("..") else HELIX
    out = call_pre("Write", {"file_path": spelling}, cwd)
    assert decision(out) == "deny", f"{spelling!r} was ALLOWED: {out}"


def test_p3_catches_a_symlink_to_the_hook_file(real_paths, tmp_path):
    link = tmp_path / "innocent.py"
    link.symlink_to(HOOK_FILE)
    out = call_pre("Write", {"file_path": str(link)}, HELIX)
    assert decision(out) == "deny", out


@pytest.mark.parametrize("path", [
    str(Path.home() / "helix/factgate_hook_notes.md"),   # shares a prefix, is not the file
    str(Path.home() / "helix/workspace/scratch.py"),
])
def test_p3_does_not_guard_files_that_merely_look_similar(real_paths, path):
    out = call_pre("Write", {"file_path": path}, HELIX)
    assert decision(out) is None, f"{path} was refused: {reason(out)}"


def test_a_normal_fact_write_on_a_clean_base_is_still_allowed(real_paths):
    """P1 unchanged: the real fact base is clean, so a top-level fact write passes."""
    out = call_pre("Write", {"file_path": str(Path.home() / "helix/memory/facts/zz_probe.md")},
                   HELIX)
    assert decision(out) is None, f"a legitimate fact write was refused: {reason(out)}"


# ==========================================================================
# ORDER 15 — P6: builds and restarts start on the user's word in chat, never from Odin's shell.
#
# `/build NN` and `/restart` are handled by app.py on the user's typed word. Odin reaching the same
# machinery from Bash — the runner, systemd-run, the launcher, or a POST to his own /ask_stream —
# would be a new door to the fan-out failure. Pure string rules: nothing here runs anything.
# ==========================================================================

RUNNER = str(Path.home() / "helix/build_orders/run_order.sh")
P6_SENTENCE = "Builds and restarts start on the user's word in chat, not from Odin's shell."

P6_DENIED = [
    "bash build_orders/run_order.sh 16",
    "~/helix/build_orders/run_order.sh 16",
    "cd ~/helix && ./build_orders/run_order.sh --dry-run 16",
    "systemd-run --user --collect sleep 60",
    "systemd-run --user --unit=x bash -c 'echo hi'",
    "~/helix/start_helix.sh",
    "bash ~/helix/start_helix.sh",
    "HELIX_NO_BROWSER=1 setsid bash ~/helix/start_helix.sh < /dev/null &",
    "systemctl --user stop helix-builder-16",
    "curl -d '{\"q\":\"/build 16\"}' 127.0.0.1:7777/ask_stream",
    "curl -s -X POST -H 'Content-Type: application/json' -d '{\"text\":\"/restart\"}' "
    "http://localhost:7777/ask_stream",
    "cat build_orders/run_order.sh | bash -s 16",
    "grep x $(bash build_orders/run_order.sh 16)",
    # ORDER 31: the nightly restart is app.py's, never Odin's
    "bash ~/helix/nightly_restart.sh",
    "~/helix/nightly_restart.sh",
    "cd ~/helix && ./nightly_restart.sh",
    "setsid bash ~/helix/nightly_restart.sh < /dev/null &",
    "HELIX_NR_TEST_ROOT=/tmp/x bash ~/helix/nightly_restart.sh",
]


@pytest.mark.parametrize("cmd", P6_DENIED)
def test_p6_denies_odin_starting_a_build_or_restart(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED: {out}"
    assert reason(out) == P6_SENTENCE, reason(out)
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P6 "), lines


P6_ALLOWED = [
    "cat build_orders/run_order.sh",
    "cat ~/helix/build_orders/run_order.sh",
    "cat build_orders/runs/RUNS.jsonl",
    "tail -1 build_orders/runs/RUNS.jsonl",
    "grep -n restart app.py",
    "grep -n systemd-run build_orders/run_order.sh",
    "wc -l build_orders/run_order.sh build_orders/builder_run.sh",
    "sha256sum start_helix.sh",
    "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7777/stat",
    "ls build_orders/",
    "cat ~/helix/nightly_restart.sh",                                      # ORDER 31: reads stay
    "sha256sum nightly_restart.sh; tail -3 workspace/cycle/RESTARTS.jsonl",
]


@pytest.mark.parametrize("cmd", P6_ALLOWED)
def test_p6_leaves_reading_the_runner_allowed(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_p3_guards_the_runner_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: RUNNER}, HELIX)
    assert decision(out) == "deny", f"{tool} of run_order.sh was ALLOWED: {out}"
    assert reason(out) == P6_SENTENCE, reason(out)
    assert "P3" in real_paths["catches"].read_text()


def test_p3_catches_a_relative_spelling_of_the_runner(real_paths):
    out = call_pre("Edit", {"file_path": "run_order.sh"}, HELIX / "build_orders")
    assert decision(out) == "deny", out


# ==========================================================================
# ORDER 16 — narrow P6, guard the build path (P7), full command in catches.
#
# §A. P6 refused three of Odin's read-only commands (catches #24, #25, #27) and a man-page read
# (#28) because `echo`, `ps`, `cut`, `zcat` were not on the reader list. The list grows by verbs
# that only read; nothing that can run code is added.
# §B. The build path (app.py, the launcher, the protocol, the runner, reports/, runs/) could be
# changed by Odin's Write/Edit. P7 guards it from the editor tools AND from Bash writes, with one
# list, so the two cannot drift.
# §D. A Bash refusal now logs the full command (capped at 4000) beside the 120-char `where`.
# Pure path and string logic: nothing here writes outside tmp_path.
# ==========================================================================

P7_SENTENCE = ("The build path (app, launcher, protocol, runner, reports) changes only through a "
               "build order that the user starts.")
BO = HELIX / "build_orders"

# Catches #24, #25, #27, #28, full text recovered from Odin's session transcripts (the log cut
# each at 120 characters). ORDER 16 kept their FULL texts refused, because #25 runs
# `.venv/bin/python` and #27 runs `sed`. ORDER 23b (Odin's ruling 1) moved both to allowed. They
# are the #45/#46 class: a read of a P6 name next to an unrelated non-read, and neither one starts
# a build.
CATCH_24 = ("cd ~/helix; ls -la --time-style=+%H:%M build_orders/reports/ | tail -5; echo ---; "
            "ps -o pid,lstart,cmd -C python3 | grep app.py; echo ---; tail -30 "
            "build_orders/reports/ORDER_15_report.md 2>/dev/null; echo ---; grep -n "
            "\"/build\\|/restart\" app.py | head; ls build_orders/run_order.sh "
            "build_orders/RUNS.jsonl 2>&1")
CATCH_25 = ("cd ~/helix; sha256sum app.py factgate_hook.py test_factgate_hook.py start_helix.sh "
            "| cut -c1-16; wc -l < workspace/catches/CATCHES.jsonl; .venv/bin/python -m pytest "
            "-q test_factgate_hook.py 2>&1 | tail -2")
CATCH_27 = ("cd ~/helix; wc -l workspace/catches/CATCHES.jsonl; tail -8 "
            "workspace/catches/CATCHES.jsonl; echo ---; sha256sum app.py factgate_hook.py "
            "test_factgate_hook.py start_helix.sh build_orders/BUILD_PROTOCOL.md "
            "build_orders/run_order.sh build_orders/builder_run.sh; echo ---; cat start_helix.sh; "
            "echo ---; grep -n \"P6\" factgate_hook.py | head -30; echo; sed -n 150,400p "
            "build_orders/reports/ORDER_15_report.md | grep -nE \"live check|Live|P6|false|"
            "restart\" | head -30")
CATCH_28 = ("zcat /usr/share/man/man1/systemd-run.1.gz 2>/dev/null | grep -c -i unsetenv; zcat "
            "/usr/share/man/man1/systemd-run.1.gz 2>/dev/null | grep -i -E '^\\\\fB-E|setenv' | "
            "head -5; grep -n -A8 \"AC4\" __HOME__/helix/build_orders/reports/"
            "ORDER_15_report.md | head -20")

P16_ALLOWED = [
    "cd ~/helix; ls build_orders/ | tail -5; echo ---; ps -o pid,cmd -C python3 | grep app",
    "sha256sum start_helix.sh build_orders/run_order.sh | cut -c1-16",
    CATCH_24,
    CATCH_25[:120],
    CATCH_27[:120],
    CATCH_25,                                    # ORDER 23b: was refused, a read next to pytest
    CATCH_27,                                    # ORDER 23b: was refused, a read next to `sed -n`
    CATCH_28,
    "cd ~/helix; sha256sum start_helix.sh | cut -c1-16; echo ---; ps -o pid -C python3",
    "grep -h run_order.sh build_orders/*.md | sort | uniq -c | nl",
    "printf '%s\\n' run_order.sh | tr a-z A-Z",
    "pgrep -af helix-builder; true",
    "basename build_orders/run_order.sh; dirname build_orders/run_order.sh",
    "readlink -f start_helix.sh; realpath build_orders/run_order.sh; date",
]


@pytest.mark.parametrize("cmd", P16_ALLOWED)
def test_p6_allows_the_new_read_only_verbs(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


# Verbs that can run code. None may become a "reader".
NEVER_READERS = ("awk", "sed", "xargs", "env", "bash", "sh", "python", "python3", "find",
                 "watch", "nohup", "timeout", "setsid", "sudo")

P16_STILL_DENIED = [
    "bash ~/helix/build_orders/run_order.sh 16",
    "cat run_order.sh | bash",
    "echo x > ~/helix/start_helix.sh",
    "awk 'BEGIN{system(\"~/helix/build_orders/run_order.sh 1\")}'",
    "xargs -a list sh run_order.sh",
    "curl -X POST 127.0.0.1:7777/restart",
    "systemd-run --user true",
    "echo x > build_orders/run_order.sh",
    "echo `bash build_orders/run_order.sh 1`",
    "echo $(bash build_orders/run_order.sh 1)",
    "cat <(bash build_orders/run_order.sh 1)",
    "printf x | bash build_orders/run_order.sh 1",
    "date; ~/helix/start_helix.sh",
    # quoting: a quoted string is one argument, never a way to hide a verb or a substitution
    "\"bash\" build_orders/run_order.sh 16",
    "'sh' build_orders/run_order.sh 16",
    "echo \"$(bash build_orders/run_order.sh 1)\"",
    "grep \"a;b\" build_orders/run_order.sh; bash build_orders/run_order.sh 1",
    "grep 'a|b' build_orders/run_order.sh | bash",
    "echo \"unterminated | bash build_orders/run_order.sh 1",
    "echo x >> ~/helix/nightly_restart.sh",                                # ORDER 31
    "cat /tmp/x | bash ~/helix/nightly_restart.sh",
    "date; ~/helix/nightly_restart.sh",
] + [f"{v} build_orders/run_order.sh 16" for v in NEVER_READERS]


@pytest.mark.parametrize("cmd", P16_STILL_DENIED)
def test_p6_still_denies_after_narrowing(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} is now ALLOWED — that is a new hole"
    assert real_paths["catches"].exists(), "a deny must still be logged"


def test_no_verb_that_runs_code_is_a_reader():
    assert not set(NEVER_READERS) & set(factgate_hook._P6_READERS)


P7_GUARDED = [
    HELIX / "app.py",
    HELIX / "start_helix.sh",
    BO / "BUILD_PROTOCOL.md",
    BO / "builder_run.sh",
    BO / "reports/ORDER_15_report.md",
    BO / "runs/x.jsonl",
    BO / "quarantine/x.md",
    BO / ".git/config",
    HELIX / "nightly_restart.sh",                                          # ORDER 31
    HELIX / ".lkg/app.py",
    HELIX / ".lkg/app.py.sha256",
]


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("path", P7_GUARDED, ids=lambda p: str(p).split("helix/")[-1])
def test_p7_guards_the_build_path_from_every_editor_tool(real_paths, tool, key, path):
    out = call_pre(tool, {key: str(path)}, HELIX)
    assert decision(out) == "deny", f"{tool} of {path} was ALLOWED: {out}"
    assert reason(out) == P7_SENTENCE, reason(out)
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P7 "), lines


@pytest.mark.parametrize("spelling,cwd", [
    ("app.py", HELIX),                                        # relative
    ("../app.py", HELIX / "workspace"),                       # relative, from Odin's cwd
    ("~/helix/start_helix.sh", HELIX),
    ("$HOME/helix/build_orders/BUILD_PROTOCOL.md", HELIX),
    ("reports/ORDER_15_report.md", BO),
])
def test_p7_catches_every_spelling(real_paths, spelling, cwd):
    out = call_pre("Write", {"file_path": spelling}, cwd)
    assert decision(out) == "deny", f"{spelling!r} was ALLOWED: {out}"
    assert reason(out) == P7_SENTENCE


def test_p7_catches_a_tmp_symlink_to_the_app(real_paths, tmp_path):
    link = tmp_path / "innocent.py"
    link.symlink_to(HELIX / "app.py")
    out = call_pre("Edit", {"file_path": str(link)}, HELIX)
    assert decision(out) == "deny", out


def test_p7_catches_a_tmp_symlink_to_the_reports_dir(real_paths, tmp_path):
    link = tmp_path / "R"
    link.symlink_to(BO / "reports")
    out = call_pre("Write", {"file_path": str(link / "ORDER_15_report.md")}, HELIX)
    assert decision(out) == "deny", out


def test_p7_review_name_on_a_symlink_does_not_unguard_its_target(real_paths, tmp_path):
    """The *_review_odin.md exception is judged on the RESOLVED file, not the link's name."""
    link = tmp_path / "ORDER_99_review_odin.md"
    link.symlink_to(BO / "reports/ORDER_15_report.md")
    out = call_pre("Write", {"file_path": str(link)}, HELIX)
    assert decision(out) == "deny", out


P7_BASH_DENIED = [
    "sed -i s/a/b/ ~/helix/app.py",
    "cp x ~/helix/app.py",
    "echo x >> ~/helix/build_orders/BUILD_PROTOCOL.md",
    "echo x > $HOME/helix/app.py",
    "mv /tmp/r.md ~/helix/build_orders/reports/ORDER_15_report.md",
    "tee -a build_orders/runs/RUNS.jsonl",
    "rm ~/helix/build_orders/builder_run.sh",
    "touch ~/helix/build_orders/quarantine/x",
    "echo x > ../app.py",
]


@pytest.mark.parametrize("cmd", P7_BASH_DENIED)
def test_p7_denies_bash_writes_to_the_build_path(real_paths, cmd):
    cwd = HELIX / "workspace" if "../" in cmd else HELIX
    out = call_pre("Bash", {"command": cmd}, cwd)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED: {out}"
    assert reason(out) == P7_BASH_SENTENCE, reason(out)      # ORDER 17: Bash sentence names Write/Edit
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P7 "), lines


@pytest.mark.parametrize("path", [
    BO / "ORDER_17_x.md",
    BO / "NEXT.md",
    BO / "reports/ORDER_16_review_odin.md",
    HELIX / "app_notes.md",                          # shares a prefix, is not the file
    HELIX / "workspace/scratch.py",
], ids=lambda p: str(p).split("helix/")[-1])
def test_p7_leaves_odins_own_files_writable(real_paths, path):
    out = call_pre("Write", {"file_path": str(path)}, HELIX)
    assert decision(out) is None, f"{path} was refused: {reason(out)}"
    assert not real_paths["catches"].exists()


@pytest.mark.parametrize("cmd", [
    "cat ~/helix/app.py | head",
    "grep -n build ~/helix/app.py",
    "sha256sum ~/helix/build_orders/BUILD_PROTOCOL.md ~/helix/build_orders/builder_run.sh",
    "tail -1 ~/helix/build_orders/runs/RUNS.jsonl",
    "echo x > ~/helix/build_orders/reports/ORDER_16_review_odin.md",
    "echo x >> ~/helix/build_orders/NEXT.md",
    "echo x > ~/helix/app_notes.md",
])
def test_p7_leaves_reads_and_odins_files_allowed_from_bash(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"


def test_p7_leaves_the_read_tool_alone(real_paths):
    assert decision(call_pre("Read", {"file_path": str(HELIX / "app.py")}, HELIX)) is None


LONG_TAIL = " # " + "x" * 300


@pytest.mark.parametrize("rule,cmd", [
    ("P6", "bash ~/helix/build_orders/run_order.sh 16" + LONG_TAIL),
    ("P7", "echo x >> ~/helix/app.py" + LONG_TAIL),
    ("P4", "echo x >> ~/helix/memory/facts/00_example_72f9.md" + LONG_TAIL),
    ("P5", "python3 ~/helix/workspace/factgate/factgate.py record a" + LONG_TAIL),
])
def test_a_bash_refusal_logs_the_full_command(real_paths, rule, cmd):
    assert len(cmd) > 200
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny"
    rec = json.loads(real_paths["catches"].read_text().splitlines()[0])
    assert rec["where"] == f"{rule} {cmd[:120]}"         # unchanged: 120 chars of command
    assert len(rec["where"]) - len(rule) - 1 == 120
    assert rec["command"] == cmd


def test_the_logged_command_is_capped_at_4000(real_paths):
    cmd = "bash ~/helix/build_orders/run_order.sh 16 # " + "y" * 5000
    call_pre("Bash", {"command": cmd}, HELIX)
    rec = json.loads(real_paths["catches"].read_text().splitlines()[0])
    assert rec["command"] == cmd[:4000]


def test_an_editor_refusal_has_no_command_field(real_paths):
    call_pre("Write", {"file_path": str(HELIX / "app.py")}, HELIX)
    rec = json.loads(real_paths["catches"].read_text().splitlines()[0])
    assert "command" not in rec


# ==========================================================================
# ORDER 17 — P7 judges what a Bash command WRITES, not what it mentions.
# Order 16's P7 refused a write word anywhere + a guarded path mentioned anywhere (heredoc bodies
# included), so Odin writing an order or a doc whose TEXT says "app.py" was refused. Now: data
# heredoc bodies are data, write words are judged by their operands, interpreter code is still
# inspected, and an unresolvable target is judged by what it could point at.
# Every case runs at Odin's real cwd (~/helix/workspace) AND at ~/helix unless it says otherwise.
# ==========================================================================

P7_BASH_SENTENCE = (P7_SENTENCE + " Odin's own files — ORDER_*.md, NEXT.md, "
                    "reports/*_review_odin.md — are writable; use Write/Edit for them.")
BOTH_CWDS = [HELIX / "workspace", HELIX]
cwd_ids = lambda c: str(c).split("helix")[-1] or "helix"

P17_ALLOWED = [
    # Odin writing an order whose text names app.py, reports/, cp and rm (historical #28-#30)
    "cat > ~/helix/build_orders/ORDER_99_x.md <<'EOF'\n# ORDER 99\n1. `cp app.py app.py.bak`\n"
    "2. report to `~/helix/build_orders/reports/ORDER_99_report.md`\n3. rm nothing; edit "
    "~/helix/app.py only with a backup\nEOF",
    # a doc by heredoc through a variable (historical #25, #26)
    "D=~/helix/tools/public_src/docs\ncat > $D/06-voice.md <<'EOF'\n# Voice\nTuning knobs at the "
    "top of `app.py`; see ~/helix/app.py and build_orders/reports/.\nEOF",
    "cp ~/helix/app.py ~/helix/workspace/app_copy.py",               # reading app.py is not a write
    "rm -rf /tmp/rprobe; ls ~/helix/build_orders/reports/ | grep 15",  # historical #15
    "touch -d \"2026-09-25 13:46\" /tmp/ref46; ls build_orders/reports/",  # historical #20
    # a RESULT file whose body mentions app.py (historical #23)
    "cat > RESULT.md <<'EOF'\nBudget 45,000 chars (app.py FACTS_BUDGET).\nEOF\necho ok",
    # historical #19: cp elsewhere, then a read of app.py
    "TS=$(date +%H%M); cp /tmp/m.pdf ~/helix/static/boat/m_v$TS.pdf && echo ok; "
    "grep -n token ~/helix/app.py | head -5",
    "grep -n cp ~/helix/app.py; echo x | tee /tmp/x",               # `cp` as a grep pattern
    "cat > /tmp/notes.md <<EOF\ncp x ~/helix/app.py\necho x > ~/helix/app.py\nEOF",
    "tee /tmp/x.md <<'EOF'\nrm ~/helix/build_orders/BUILD_PROTOCOL.md\nEOF",
]
P17_ALLOWED_AT_HELIX = [
    "cp app.py app.py.bak-x",
    "python3 -m py_compile app.py && gzip -c odin.log > odin.log.$(date +%F).gz",   # historical #16
    "cp app.py app.py.bak-mobile-$(date +%Y%m%d-%H%M%S) && wc -l app.py",          # historical #14
]


@pytest.mark.parametrize("cwd", BOTH_CWDS, ids=cwd_ids)
@pytest.mark.parametrize("cmd", P17_ALLOWED)
def test_p7_allows_commands_that_only_mention_the_build_path(real_paths, cmd, cwd):
    out = call_pre("Bash", {"command": cmd}, cwd)
    assert decision(out) is None, f"{cmd!r} at {cwd} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


@pytest.mark.parametrize("cmd", P17_ALLOWED_AT_HELIX)
def test_p7_allows_backups_and_logs_at_helix(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"
    assert not real_paths["catches"].exists()


P17_DENIED = [
    "cp x ~/helix/build_orders/quarantine/y",                          # historical #18
    "echo x >> ~/helix/app.py",
    "tee ~/helix/start_helix.sh < /tmp/x",
    "sed -i s/a/b/ ~/helix/build_orders/BUILD_PROTOCOL.md",
    "mv /tmp/x ~/helix/build_orders/reports/ORDER_16_report.md",
    f"python3 - <<'PY'\np=\"{HELIX}/app.py\"; open(p,\"w\").write(s)\nPY",
    "cat <<'EOF' | bash\necho x > ~/helix/app.py\nEOF",
    "cat > $X/app.py <<'EOF'\nhello\nEOF",
    # a data heredoc to the build path is judged by its target
    "cat > ~/helix/app.py <<'EOF'\nprint('hi')\nEOF",
    "cat <<'EOF' > ~/helix/build_orders/reports/ORDER_16_report.md\nfake\nEOF",
    "tee -a ~/helix/build_orders/runs/RUNS.jsonl <<'EOF'\n{}\nEOF",
    # a data heredoc that is RUN later in the same command is code (historical #31's shape)
    f"cat > /tmp/p.py <<'PY'\nopen(\"{HELIX}/app.py\",\"w\").write('')\nPY\npython3 /tmp/p.py",
    "cat > /tmp/p.sh <<'EOF'\necho x > ~/helix/app.py\nEOF\nbash /tmp/p.sh",
    # write words judged by their operands, in every command position
    "cp -t ~/helix/build_orders/reports /tmp/x",
    "cp --target-directory=$HOME/helix/build_orders/quarantine /tmp/x",
    "install -m 644 /tmp/x ~/helix/start_helix.sh",
    "ln -sf /tmp/x ~/helix/build_orders/builder_run.sh",
    "dd if=/tmp/x of=__HOME__/helix/app.py",
    "truncate -s 0 ~/helix/app.py",
    "chmod 777 ~/helix/build_orders/run_order.sh",
    "touch ~/helix/build_orders/.git/index.lock",
    "perl -pi -e s/a/b/ ~/helix/app.py",
    "sudo cp /tmp/x ~/helix/app.py",
    "true && rm -f ~/helix/build_orders/reports/ORDER_15_report.md",
    "if true; then cp /tmp/x ~/helix/app.py; fi",
    # interpreter code is still inspected
    f"python3 -c \"open('{HELIX}/app.py','a').write('x')\"",
    "python3 - <<'PY'\nimport shutil; shutil.copy('/tmp/x', '__HOME__/helix/app.py')\nPY",
    "python3 - <<'PY'\nfrom pathlib import Path\n"
    "Path('__HOME__/helix/build_orders/BUILD_PROTOCOL.md').write_text('')\nPY",
    "bash -c 'echo x > ~/helix/app.py'",
    "bash <<'EOF'\ncp /tmp/x ~/helix/app.py\nEOF",
    "cat <<'EOF' | python3\nopen('__HOME__/helix/app.py','w')\nEOF",
    "eval 'cp /tmp/x ~/helix/app.py'",
    # historical #22 / #24 shape: back up, then patch app.py from a Python heredoc
    "cd __HOME__/helix; cp app.py app.py.bak-upload-$(date +%s); python3 - <<'PYEOF'\n"
    "p = \"app.py\"\nsrc = open(p).read()\nopen(p, \"w\").write(src)\nPYEOF",
    # unresolvable targets
    "cd ~/helix/build_orders/reports && echo x > ORDER_16_report.md",
    "D=~/helix/build_orders/reports; cat > $D/ORDER_16_report.md <<'EOF'\nx\nEOF",
    "D=~/helix/build_orders; echo x > $D/reports/new.md",
    "echo x > ~/helix/build_orders/reports/$N.md",
    "echo x > ~/helix/build_orders/runs/*.jsonl",
    "cp /tmp/x \"$(echo ~/helix/app.py)\"",
    "echo x > ~/helix/ap$X",
    "for f in ~/helix/build_orders/reports/*; do echo x > $f; done",
    "ls ~/helix/build_orders/reports | while read f; do rm \"$f\"; done",
    "find ~/helix/build_orders/reports -name '*.md' -exec rm {} \\;",
    "ls ~/helix/build_orders/runs/* | xargs rm",
]


@pytest.mark.parametrize("cwd", BOTH_CWDS, ids=cwd_ids)
@pytest.mark.parametrize("cmd", P17_DENIED)
def test_p7_still_denies_real_writes_to_the_build_path(real_paths, cmd, cwd):
    out = call_pre("Bash", {"command": cmd}, cwd)
    assert decision(out) == "deny", f"{cmd!r} at {cwd} was ALLOWED — that is a hole"
    assert reason(out) == P7_BASH_SENTENCE, reason(out)
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P7 "), lines


def test_p7_bash_deny_tells_odin_to_use_write_edit(real_paths):
    out = call_pre("Bash", {"command": "echo x >> ~/helix/app.py"}, HELIX / "workspace")
    assert "use Write/Edit for them" in reason(out), reason(out)


def test_p7_editor_tools_unchanged_by_order_17(real_paths):
    ok = call_pre("Write", {"file_path": str(BO / "reports/ORDER_16_review_odin.md")}, HELIX)
    assert decision(ok) is None, reason(ok)
    out = call_pre("Write", {"file_path": str(BO / "reports/ORDER_16_report.md")}, HELIX)
    assert decision(out) == "deny" and reason(out) == P7_SENTENCE, reason(out)
    e = call_pre("Edit", {"file_path": str(BO / "reports/ORDER_16_review_odin.md")}, HELIX)
    assert decision(e) is None, reason(e)


# ORDER 18 — §A. P6 refused catch #31, a `systemctl --user list-units` STATUS READ that launched
# nothing. A systemctl part is a read iff every option is on a short list and the verb only reads.
CATCH_31 = ("cd ~/helix/build_orders; date; ls -la --time-style=+%H:%M runs/ | tail -8; ls -la "
            "--time-style=+%H:%M reports/ | tail -3; systemctl --user list-units 'helix-builder-*' "
            "--all --no-pager 2>&1 | head; tail -3 runs/RUNS.jsonl 2>/dev/null")

P18_ALLOWED = [
    CATCH_31,
    "systemctl --user status helix-builder-18.service --no-pager",
    "systemctl --user show -p ActiveState helix-builder-18",
    "systemctl --user list-units 'helix-builder-*' --all --no-pager 2>&1 | head",
]
P18_DENIED = [
    "systemctl --user start helix-builder-18",
    "systemctl --user restart helix-builder-18",
    "systemctl --user stop helix-builder-18",
    "systemctl --user kill helix-builder-18",
    "systemctl --user edit helix-builder-18",
    "systemctl --user daemon-reload; ls helix-builder",
    "systemctl --user status helix-builder-18 && bash ~/helix/build_orders/run_order.sh 99",
    "systemctl --user --some-new-flag status helix-builder-18",      # unknown option
    "systemctl helix-builder-18",                                     # no verb
    "systemd-run --user --unit helix-builder-x true",                 # unchanged
    "systemctl --user status $(bash ~/helix/build_orders/run_order.sh 99)",  # substitution
]


@pytest.mark.parametrize("cwd", BOTH_CWDS, ids=cwd_ids)
@pytest.mark.parametrize("cmd", P18_ALLOWED)
def test_p6_allows_a_systemctl_status_read(real_paths, cmd, cwd):
    out = call_pre("Bash", {"command": cmd}, cwd)
    assert decision(out) is None, f"{cmd!r} at {cwd} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


@pytest.mark.parametrize("cwd", BOTH_CWDS, ids=cwd_ids)
@pytest.mark.parametrize("cmd", P18_DENIED)
def test_p6_still_refuses_systemctl_that_is_not_a_read(real_paths, cmd, cwd):
    out = call_pre("Bash", {"command": cmd}, cwd)
    assert decision(out) == "deny", f"{cmd!r} at {cwd} was ALLOWED — that is a hole"
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P6 "), lines


def test_systemctl_is_not_a_p6_reader():
    assert "systemctl" not in factgate_hook._P6_READERS


# ==========================================================================
# ORDER 22b — P5 (a): `baseline-legacy` and `release` are recording verbs too (the `-legacy`
# suffix used to slip past the lookahead). P5 (b): the rule fires only when the command
# actually RUNS factgate.py — as the program or as the script argument of an interpreter —
# not when the word sits in a grep pattern or a path that is only read. Catch #42 was a
# false block of exactly that kind.
# ==========================================================================

CATCH_42 = ('cd ~/helix/workspace/factgate/state; grep -n -E "14[89]_|15[01]_" FACTS_LEDGER.jsonl'
            ' | cut -c1-120; sed -n 1,40p ~/helix/build_orders/reports/ORDER_22_report.md | grep'
            ' -n -i -E "legacy|baseline|stop|question" | head')


@pytest.mark.parametrize("cmd", [
    CATCH_42,
    "python3 ~/helix/workspace/factgate/factgate.py check ~/helix/memory/facts",
    "python3 ~/helix/workspace/factgate/factgate.py interim ~/helix/memory/facts",
    "python3 ~/helix/workspace/factgate/factgate.py interim ~/helix/memory/facts --json | head",
    "grep -n release ~/helix/workspace/factgate/factgate.py",
    "grep -n -E 'baseline-legacy|record' ~/helix/workspace/factgate/README.md",
    "wc -l ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl; grep -c release "
    "~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "cd ~/helix/workspace/factgate && python3 factgate.py check ../../memory/facts",
])
def test_p5_allows_reads_and_report_verbs(rig, cmd):
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    assert decision(out) is None, f"{cmd} -> {out}"
    assert catches_lines(rig) == []


@pytest.mark.parametrize("cmd", [
    "python3 ~/helix/workspace/factgate/factgate.py record x",
    "python3 ~/helix/workspace/factgate/factgate.py release x",
    "python3 ~/helix/workspace/factgate/factgate.py release ~/helix/memory/facts 151_a.md "
    "--witness report:build_orders/reports/ORDER_21_report.md --reason r",
    "python3 ~/helix/workspace/factgate/factgate.py baseline-legacy ~/helix/memory/facts "
    "--cutoff-line 209 --cutoff-sha256 abc --vault-seq 1",
    "python3 -u ~/helix/workspace/factgate/factgate.py --state-dir /tmp/s release x",
    "cd ~/helix/workspace/factgate && ./factgate.py release a b",
    "cd ~/helix/workspace/factgate; python3 factgate.py baseline-legacy x",
    "env python3 ~/helix/workspace/factgate/factgate.py release x",
    "timeout 5 python3 ~/helix/workspace/factgate/factgate.py record-delete x y",
    "bash -c 'python3 ~/helix/workspace/factgate/factgate.py release x'",
    "echo $(python3 ~/helix/workspace/factgate/factgate.py release x)",
    "ls | python3 ~/helix/workspace/factgate/factgate.py baseline x",
    "python3 -c \"import subprocess; subprocess.run(['python3', 'factgate.py', 'release'])\"",
    "python3 - <<'E'\nimport subprocess\nsubprocess.run(['factgate.py', 'release', 'x'])\nE",
    "bash <<'E'\npython3 ~/helix/workspace/factgate/factgate.py release x\nE",
    "F=~/helix/workspace/factgate/factgate.py; python3 $F release x",
])
def test_p5_refuses_running_a_recording_verb(rig, cmd):
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    assert decision(out) == "deny", f"{cmd} -> {out}"
    assert "P5" in catches_lines(rig)[-1]["where"]


# ==========================================================================
# ORDER 23b — P6 judges what RUNS, not what is mentioned. Catches #45 and #46 were pure reads of
# a P6 name, refused because one unrelated part (`crontab -l`, `sed -n`) was not on the reader
# list and `_only_reads` judged the whole command. A command that names a P6 name and is not all
# reads is now refused only if a parsed command runs one (as the program, or as the script of a
# shell/`source`/`.`/`exec`), is a systemctl part that is not a status read, or feeds a shell or
# xargs. Code that cannot be parsed keeps the old word rule.
# ==========================================================================

CATCH_45 = ('cd ~/helix; grep -n "CLAUDE_CONFIG_DIR\\|claude/config\\|Ny diskenhed" start_helix.sh'
            ' app.py render_daemon.py 2>/dev/null | head; grep -rln "Ny diskenhed" ~/.config/systemd'
            ' ~/.local/share/applications ~/Desktop 2>/dev/null; crontab -l 2>/dev/null | grep -i'
            ' diskenhed')
CATCH_46 = ('cd ~/helix/build_orders; ls; grep -n "" BUILD_PROTOCOL.md | sed -n 1,15p; grep -n'
            ' "def.*build\\|/build\\|run_order" ~/helix/app.py | head -20; wc -l run_order.sh')

P23B_ALLOWED = [
    CATCH_45,
    CATCH_46,
    "grep -n start_helix ~/helix/*.sh | head -3; crontab -l",          # Odin's live check a
    "sed -n 1,20p ~/helix/start_helix.sh",
    "awk 'NR<5' ~/helix/build_orders/run_order.sh",
    "diff ~/helix/start_helix.sh ~/helix/start_helix.sh.bak.20260925_212224",
    "stat ~/helix/build_orders/run_order.sh; file ~/helix/start_helix.sh",
    "sha256sum ~/helix/build_orders/run_queue.sh",
    "grep -c x ~/helix/start_helix.sh; python3 -m pytest -q test_factgate_hook.py | tail -2",
    "cat ~/helix/build_orders/run_order.sh | grep -n systemd-run; uptime",
]

P23B_DENIED = [
    "bash ~/helix/start_helix.sh",
    "~/helix/start_helix.sh",
    "source ~/helix/start_helix.sh",
    ". ~/helix/start_helix.sh",
    "exec ~/helix/build_orders/run_order.sh 99",
    "bash ~/helix/build_orders/run_order.sh 99",
    "bash ~/helix/build_orders/run_queue.sh 98",
    "~/helix/build_orders/run_queue.sh 98",
    "systemd-run --user --collect true",
    "systemctl --user start helix-builder-99",
    "cat ~/helix/build_orders/run_order.sh | bash",
    "cat ~/helix/build_orders/run_order.sh | sh -s 99",
    "echo /build 5 | curl -d @- 127.0.0.1:7777/ask_stream",
    "curl -d '{\"q\":\"/build 5\"}' localhost:7777/ask_stream",
    # the fix must not open a new way round
    "uptime; bash ~/helix/start_helix.sh",
    "crontab -l; ~/helix/build_orders/run_order.sh 99",
    "bash < ~/helix/build_orders/run_order.sh",
    "bash <(cat ~/helix/build_orders/run_order.sh) 99",
    "bash -o pipefail ~/helix/build_orders/run_order.sh 99",
    "F=~/helix/build_orders/run_order.sh; $F 99",
    "F=~/helix/build_orders/run_order.sh; bash \"$F\" 99",
    "bash -c 'cd ~/helix && ./start_helix.sh'",
    "python3 -c \"import os; os.system('~/helix/start_helix.sh')\"",
    "bash <<'E'\n~/helix/start_helix.sh\nE",
    "nohup setsid ~/helix/start_helix.sh &",
    "tmux new-session -d 'bash ~/helix/build_orders/run_order.sh 99'",
    "grep -l x ~/helix/build_orders/run_order.sh | xargs bash",
    "awk '{print | \"bash\"}' ~/helix/build_orders/run_order.sh",
    "awk 'BEGIN{system(\"~/helix/start_helix.sh\")}'",
    "sed -n -i 1p /tmp/start_helix.sh; uptime",                 # -i is not a read
    "crontab ~/helix/start_helix.sh",
    "echo '* * * * * ~/helix/start_helix.sh' | crontab -",
    "echo ~/helix/start_helix.sh | at now",
    "echo `~/helix/start_helix.sh`",
]


@pytest.mark.parametrize("cmd", P23B_ALLOWED)
def test_p6_allows_reads_of_p6_names(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


@pytest.mark.parametrize("cmd", P23B_DENIED)
def test_p6_still_refuses_running_a_p6_name(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} is ALLOWED — that is a hole"
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P6 "), lines


def test_run_queue_is_a_p6_name():
    assert factgate_hook._P6_NAMES_RE.search("run_queue.sh")


# ORDER 23b, Odin's rulings 3–5. (3) A wrapper the parser does not know (ssh, strace, flock, tmux)
# with a P6 program as an argument is refused, and so is P5's factgate.py with a recording verb.
# (4) An unbalanced quote is unparseable, so the word rule applies. An awk program that names a P6
# name is not a read. A P6 name among a prefix's words (timeout's duration slot) is refused.
# (5) `>` inside quotes is not a redirect, so `awk '$1>5'` on a P6 name is a read.
P23B_WRAPPERS_DENIED = [
    "ssh localhost ~/helix/start_helix.sh",
    "strace ~/helix/start_helix.sh",
    "strace -f bash ~/helix/build_orders/run_order.sh 99",
    "flock /tmp/l ~/helix/start_helix.sh",
    "tmux new -d bash ~/helix/build_orders/run_order.sh 99",
    "tmux new-session -d ~/helix/build_orders/run_queue.sh 98",
    "nohup ~/helix/start_helix.sh",
    "setsid ~/helix/start_helix.sh",
    "env ~/helix/start_helix.sh",
    "env FOO=1 ~/helix/build_orders/run_order.sh 99",
    "uptime; ssh localhost ~/helix/start_helix.sh",
    "strace systemd-run --user true",
    # ruling 4
    "echo \"unterminated | bash build_orders/run_order.sh 1",
    "uptime; echo 'unterminated ~/helix/start_helix.sh",
    "awk build_orders/run_order.sh 16",
    "awk '/start_helix.sh/' /etc/hostname; uptime",
    "timeout build_orders/run_order.sh 16",
    "timeout -s KILL ~/helix/start_helix.sh 5",
    "timeout 5 ~/helix/start_helix.sh",
]

P23B_QUOTED_GT_ALLOWED = [
    "awk '$1>5' ~/helix/build_orders/run_order.sh",
    "awk '$1>5' ~/helix/build_orders/run_order.sh; uptime",
    "grep -n \"a>b\" ~/helix/start_helix.sh; crontab -l",
    "grep -c '<(' ~/helix/build_orders/run_order.sh",
]

P23B_QUOTED_STILL_DENIED = [
    "awk '$1>5 {print > \"/tmp/start_helix.sh\"}' x; uptime",        # names a P6 name
    "echo \"$(bash ~/helix/build_orders/run_order.sh 1)\"",          # live inside double quotes
    "echo \"`~/helix/start_helix.sh`\"",
]


@pytest.mark.parametrize("cmd", P23B_WRAPPERS_DENIED + P23B_QUOTED_STILL_DENIED)
def test_p6_refuses_wrappers_and_unparseable(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} is ALLOWED — that is a hole"
    lines = [json.loads(l) for l in real_paths["catches"].read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["where"].startswith("P6 "), lines


@pytest.mark.parametrize("cmd", P23B_QUOTED_GT_ALLOWED)
def test_p6_quoted_gt_is_not_a_redirect(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"
    assert not real_paths["catches"].exists(), "an allow must not be logged as a catch"


@pytest.mark.parametrize("cmd", [
    "strace python3 ~/helix/workspace/factgate/factgate.py release x",
    "ssh localhost ~/helix/workspace/factgate/factgate.py record x",
    "flock /tmp/l python3 ~/helix/workspace/factgate/factgate.py baseline x",
    "tmux new -d python3 ~/helix/workspace/factgate/factgate.py record-delete x y",
])
def test_p5_refuses_wrappers_running_a_recording_verb(rig, cmd):
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    assert decision(out) == "deny", f"{cmd} -> {out}"
    assert "P5" in catches_lines(rig)[-1]["where"]


def test_p5_wrapper_rule_leaves_reads_and_report_verbs(rig):
    for cmd in ("strace python3 ~/helix/workspace/factgate/factgate.py check x",
                "sed -n /release/p ~/helix/workspace/factgate/factgate.py"):
        out = call_pre("Bash", {"command": cmd}, rig["cwd"])
        assert decision(out) is None, f"{cmd} -> {out}"
    assert catches_lines(rig) == []


# ---------------------------------------------------------------- ORDER 24: the build queue
# The queue starts only from the user's `/build 25-30` in chat. Odin's shell running run_queue.sh is
# P6's business, by the same "runs it, not mentions it" rule as run_order.sh (order 23b).
P24_DENIED = [
    "bash ~/helix/build_orders/run_queue.sh 98",                 # Odin's live check c
    "bash ~/helix/build_orders/run_queue.sh --start 25 26",
    "bash ~/helix/build_orders/run_queue.sh --run q-20261001_120000",
    "cd ~/helix/build_orders && ./run_queue.sh --start 25",
    "python3 - < ~/helix/build_orders/run_queue.sh",
    "nohup bash ~/helix/build_orders/run_queue.sh --start 25 &",
]

P24_ALLOWED = [
    "grep -n QUEUE ~/helix/build_orders/run_queue.sh",
    "grep -c run_queue.sh ~/helix/app.py; wc -l ~/helix/build_orders/run_queue.sh",
    "cat ~/helix/build_orders/runs/QUEUE.json",
]


@pytest.mark.parametrize("cmd", P24_DENIED)
def test_p24_odin_cannot_run_the_queue(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} is ALLOWED — Odin could start a queue"


@pytest.mark.parametrize("cmd", P24_ALLOWED)
def test_p24_odin_may_read_the_queue(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"


# ---------------------------------------------------------------- ORDER 23c: the write wall's wrapper
# app.py writes ~/helix/wall_exec.sh and points Odin's shell through it. The wall ro-binds it, so
# the shell cannot edit it; P7 is what keeps the editor tools off it.
WALL_EXEC = str(HELIX / "wall_exec.sh")


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_p7_guards_the_wall_wrapper_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: WALL_EXEC}, HELIX)
    assert decision(out) == "deny", f"{tool} of wall_exec.sh was ALLOWED: {out}"
    assert reason(out) == P7_SENTENCE, reason(out)


@pytest.mark.parametrize("cmd", ["echo x > ~/helix/wall_exec.sh",
                                 "cp /tmp/x ~/helix/wall_exec.sh",
                                 "sed -i s/bwrap/true/ wall_exec.sh"])
def test_p7_denies_bash_writes_to_the_wall_wrapper(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED: {out}"


def test_p7_leaves_reading_the_wall_wrapper_allowed(real_paths):
    out = call_pre("Bash", {"command": "cat ~/helix/wall_exec.sh"}, HELIX)
    assert decision(out) is None, reason(out)


# ---------------------------------------------------------------- ORDER 33b: tee/redirect judged by where it lands
# Catch #55 (2026-10-02, P4): `cd ~/helix/memory/facts; … | tee /tmp/pins.txt` was refused because the
# command named the fact base (the `cd`) and contained the word `tee`. A tee or a redirect is now
# resolved against the cwd it runs in (tracking `cd`) and judged by where it lands. Every other write
# word keeps the mention rule.
CATCH_55 = ("cd ~/helix/memory/facts; for p in 15_ 91_ 99_ 101_ 103_ 112_ 120_ 142_ 145_ 153_ 158_ 164_; "
            "do f=$(ls ${p}*.md); printf \"%6d %s\\n\" $(wc -c < $f) \"$f\"; done | tee /tmp/pins.txt; "
            "awk '{s+=$1} END {print s}' /tmp/pins.txt; sed -n 1190,1215p ~/helix/app.py | head -30")


def test_33b_catch_55_reproduction_is_allowed(real_paths):
    out = call_pre("Bash", {"command": CATCH_55}, HELIX)
    assert decision(out) is None, reason(out)
    assert not real_paths["catches"].exists()


@pytest.mark.parametrize("cmd", [
    "cd ~/helix/memory/facts; ls | tee /tmp/x",               # Odin's live check d
    "cd ~/helix/memory/facts && wc -c *.md > /tmp/sizes.txt",
    "cd ~/helix/memory/facts; ls | tee -a /tmp/x /tmp/y",
    "cat ~/helix/memory/facts/00_example_72f9.md | tee /tmp/copy.md",
])
def test_33b_tee_or_redirect_out_of_the_fact_base_is_allowed(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, f"{cmd!r} refused: {reason(out)}"


@pytest.mark.parametrize("cmd", [
    "cd ~/helix; ls | tee memory/facts/x",                    # the order's AC
    "cd ~/helix/memory/facts; ls | tee x.md",                 # relative name after cd …/facts
    "cd ~/helix/memory/facts; ls > x.md",                     # a redirect after the cd: was ALLOWED
    "cd ~/helix/memory/facts; ls >> 00_example_72f9.md",
    "cd ~/helix/memory; ls | tee facts/x.md",
    "cd ~/helix/memory/facts; ls | tee /tmp/x ./y.md",        # one of two tee targets lands inside
    "cd ~/helix/memory/facts; cd -; ls | tee x.md",           # cwd unknown -> refused
    "cd ~/helix/memory/facts; ls | tee $OUT",                 # target unknown -> refused
    "cd ~/helix/memory/facts; ls | sudo tee x.md",
    "cd ~/helix/memory/facts; echo $(ls | tee x.md)",         # a tee the parser cannot place
    "cd ~/helix/memory/facts; bash -c 'ls | tee x.md'",
    "cd ~/helix/memory/facts; ls | tee /tmp/x; cp /tmp/x y.md",   # other write words: mention rule
])
def test_33b_tee_or_redirect_into_the_fact_base_is_still_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"
    assert "Write/Edit" in reason(out), reason(out)


# ---------------------------------------------------------------- ORDER 33b: memory/PINNED.txt
PINNED = str(HELIX / "memory" / "PINNED.txt")


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_33b_pinned_txt_is_guarded_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: PINNED}, HELIX)
    assert decision(out) == "deny", f"{tool} of PINNED.txt was ALLOWED: {out}"


@pytest.mark.parametrize("cmd", ["echo 99_x.md >> ~/helix/memory/PINNED.txt",
                                 "cd ~/helix/memory; echo x | tee -a PINNED.txt",
                                 "sed -i d ~/helix/memory/PINNED.txt",
                                 "cp /tmp/x ~/helix/memory/PINNED.txt"])
def test_33b_bash_writes_to_pinned_txt_are_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_33b_reading_pinned_txt_is_allowed(real_paths):
    out = call_pre("Bash", {"command": "cat ~/helix/memory/PINNED.txt; wc -l ~/helix/memory/PINNED.txt"},
                   HELIX)
    assert decision(out) is None, reason(out)


def test_33b_docstring_p7_names_the_wall_wrapper_and_the_pin_list():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "wall_exec.sh" in p7 and "PINNED.txt" in p7


# ---------------------------------------------------------------- ORDER 34: workspace/cycle/PATCHES.jsonl
PATCHES = str(HELIX / "workspace" / "cycle" / "PATCHES.jsonl")


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_34_patch_ledger_is_guarded_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: PATCHES}, HELIX)
    assert decision(out) == "deny", f"{tool} of PATCHES.jsonl was ALLOWED: {out}"


@pytest.mark.parametrize("cmd", ["echo '{}' >> ~/helix/workspace/cycle/PATCHES.jsonl",
                                 "cd ~/helix/workspace/cycle; echo x | tee -a PATCHES.jsonl",
                                 "sed -i d ~/helix/workspace/cycle/PATCHES.jsonl",
                                 "cp /tmp/x ~/helix/workspace/cycle/PATCHES.jsonl"])
def test_34_bash_writes_to_the_patch_ledger_are_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_34_reading_the_patch_ledger_is_allowed(real_paths):
    out = call_pre("Bash", {"command": "tail -5 ~/helix/workspace/cycle/PATCHES.jsonl; "
                                       "wc -l ~/helix/workspace/cycle/PATCHES.jsonl"}, HELIX)
    assert decision(out) is None, reason(out)


def test_34_docstring_p7_names_the_patch_ledger():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "PATCHES.jsonl" in p7


# ---------------------------------------------------------------- ORDER 35b: archive, restore, memory/archive/
ARCHIVE = str(HELIX / "memory" / "archive")


@pytest.mark.parametrize("cmd", [
    "python3 ~/helix/workspace/factgate/factgate.py archive x",
    "python3 ~/helix/workspace/factgate/factgate.py archive 10_a.md 11_b.md --why closed",
    "python3 ~/helix/workspace/factgate/factgate.py restore 10_a.md --why asked",
    "cd ~/helix/workspace/factgate && ./factgate.py archive a",
    "env python3 ~/helix/workspace/factgate/factgate.py restore x --why y",
    "bash -c 'python3 ~/helix/workspace/factgate/factgate.py archive x'",
    "python3 - <<'E'\nimport subprocess\nsubprocess.run(['factgate.py', 'archive', 'x'])\nE",
    "F=~/helix/workspace/factgate/factgate.py; python3 $F restore x",
])
def test_35b_p5_refuses_archive_and_restore(rig, cmd):
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    assert decision(out) == "deny", f"{cmd} -> {out}"
    assert "P5" in catches_lines(rig)[-1]["where"]


@pytest.mark.parametrize("cmd", [
    "grep -n archive ~/helix/workspace/factgate/factgate.py",
    "grep -c '\"restore\": true' ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "python3 ~/helix/workspace/factgate/factgate.py archive --help | head -3",
])
def test_35b_p5_reads_about_archive_are_allowed_but_help_is_a_run(rig, cmd):
    out = call_pre("Bash", {"command": cmd}, rig["cwd"])
    if "--help" in cmd:                       # it runs factgate.py with a recording verb: refused
        assert decision(out) == "deny", out
    else:
        assert decision(out) is None, f"{cmd} -> {out}"


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_35b_memory_archive_is_guarded_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: ARCHIVE + "/10_a.md"}, HELIX)
    assert decision(out) == "deny", f"{tool} into memory/archive was ALLOWED: {out}"


@pytest.mark.parametrize("cmd", ["echo x > ~/helix/memory/archive/10_a.md",
                                 "cd ~/helix/memory/archive; echo x | tee 10_a.md",
                                 "mv ~/helix/memory/facts/10_a.md ~/helix/memory/archive/",
                                 "cp /tmp/x ~/helix/memory/archive/10_a.md",
                                 "rm ~/helix/memory/archive/10_a.md"])
def test_35b_bash_writes_into_memory_archive_are_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_35b_reading_memory_archive_is_allowed(real_paths):
    out = call_pre("Bash", {"command": "ls ~/helix/memory/archive; grep -rn attest "
                                       "~/helix/memory/archive | head"}, HELIX)
    assert decision(out) is None, reason(out)


def test_35b_archive_and_restore_are_recording_verbs_and_the_docstring_says_so():
    assert {"archive", "restore"} <= set(factgate_hook._RECORDING_VERBS)
    assert factgate_hook._build_path_owns(ARCHIVE + "/x.md")
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "memory/archive/" in p7


# ---------------------------------------------------------------- ORDER 36: workspace/cycle/NO_RECORD_NEEDED.jsonl
NO_RECORD = str(HELIX / "workspace" / "cycle" / "NO_RECORD_NEEDED.jsonl")


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_36_gap_opt_out_is_guarded_from_every_editor_tool(real_paths, tool, key):
    out = call_pre(tool, {key: NO_RECORD}, HELIX)
    assert decision(out) == "deny", f"{tool} of NO_RECORD_NEEDED.jsonl was ALLOWED: {out}"


@pytest.mark.parametrize("cmd", [
    "echo '{\"id\": \"x\", \"why\": \"y\"}' >> ~/helix/workspace/cycle/NO_RECORD_NEEDED.jsonl",
    "cd ~/helix/workspace/cycle; echo x | tee -a NO_RECORD_NEEDED.jsonl",
    "sed -i d ~/helix/workspace/cycle/NO_RECORD_NEEDED.jsonl",
    "cp /tmp/x ~/helix/workspace/cycle/NO_RECORD_NEEDED.jsonl"])
def test_36_bash_writes_to_the_gap_opt_out_are_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_36_reading_the_gap_opt_out_is_allowed(real_paths):
    out = call_pre("Bash", {"command": "cat ~/helix/workspace/cycle/NO_RECORD_NEEDED.jsonl; "
                                       "python3 ~/helix/workspace/gapaudit/gapaudit.py run "
                                       "--since 2026-09-15"}, HELIX)
    assert decision(out) is None, reason(out)


def test_36_odins_gap_audit_files_stay_his():
    """Only the opt-out is guarded: the audit and its rules live in workspace/, which is Odin's."""
    assert factgate_hook._build_path_owns(NO_RECORD)
    for f in ("gapaudit.py", "gapaudit_rules.json", "test_gapaudit.py"):
        assert not factgate_hook._build_path_owns(str(HELIX / "workspace" / "gapaudit" / f)), f


def test_36_docstring_p7_names_the_gap_opt_out():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "NO_RECORD_NEEDED.jsonl" in p7


# ---------------------------------------------------------------- ORDER 38: housekeeping's flag and MOVES ledger
HK_FLAG = str(HELIX / "workspace" / "cycle" / "housekeeping_enabled.flag")
HK_MOVES = str(HELIX / "workspace" / "cycle" / "MOVES.jsonl")


@pytest.mark.parametrize("target", [HK_FLAG, HK_MOVES])
@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_38_housekeeping_flag_and_moves_are_guarded_from_every_editor_tool(real_paths, tool, key,
                                                                           target):
    out = call_pre(tool, {key: target}, HELIX)
    assert decision(out) == "deny", f"{tool} of {target} was ALLOWED: {out}"


@pytest.mark.parametrize("cmd", [
    "touch ~/helix/workspace/cycle/housekeeping_enabled.flag",
    "echo yes > ~/helix/workspace/cycle/housekeeping_enabled.flag",
    "cd ~/helix/workspace/cycle; : > housekeeping_enabled.flag",
    "cp /tmp/x ~/helix/workspace/cycle/housekeeping_enabled.flag",
    "echo '{\"path\": \"/tmp/x\"}' >> ~/helix/workspace/cycle/MOVES.jsonl",
    "cd ~/helix/workspace/cycle; echo x | tee -a MOVES.jsonl",
    "sed -i d ~/helix/workspace/cycle/MOVES.jsonl",
    "cp /tmp/x ~/helix/workspace/cycle/MOVES.jsonl"])
def test_38_bash_writes_to_the_flag_and_moves_are_refused(real_paths, cmd):
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_38_reading_them_and_running_a_plan_is_allowed(real_paths):
    out = call_pre("Bash", {"command": "cat ~/helix/workspace/cycle/MOVES.jsonl; "
                                       "ls -l ~/helix/workspace/cycle/housekeeping_enabled.flag; "
                                       "python3 ~/helix/workspace/housekeeping/housekeeping.py plan"},
                   HELIX)
    assert decision(out) is None, reason(out)


def test_38_odins_housekeeping_files_stay_his():
    """Only the flag and the ledger are guarded; the code and its plans live in workspace/."""
    assert factgate_hook._build_path_owns(HK_FLAG) and factgate_hook._build_path_owns(HK_MOVES)
    for f in ("housekeeping.py", "test_housekeeping.py", "plans/2026-10-02.json"):
        assert not factgate_hook._build_path_owns(str(HELIX / "workspace" / "housekeeping" / f)), f


def test_38_docstring_p7_names_the_flag_and_moves():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "housekeeping_enabled.flag" in p7 and "MOVES.jsonl" in p7


# ---------------------------------------------------------------- ORDER 39: three false-block shapes, P7 judges mv's sources
F39 = "~/helix/memory/facts"
WS39 = HELIX / "workspace"
CATCH_64 = ('cd ~/helix; python3 -B -c "\nimport factgate_hook as h, os\nF=os.path.expanduser(\'~/helix/memory/facts\')\n'
            'for c in [\'cd ~/helix/memory/facts; ls | tee /tmp/x\',\n          \'cd ~/helix/memory/facts; ls | tee /tmp/x >/dev/null\',\n'
            '          \'cd ~/helix/memory/facts; ls | tee /tmp/x; echo \\"tee exit\\"\',\n'
            '          \'cd ~/helix/memory/facts; ls | tee /tmp/x; wc -l </tmp/x\']:\n'
            '    print(h._writes_facts(c, os.path.expanduser(\'~/helix/workspace\')), c)\n"')
CATCH_65 = ("cd ~/helix; find workspace -name 'refusals.log' -exec wc -l {} \\; 2>/dev/null; grep -n 'ended' "
            "build_orders/builder_run.sh | head -5; grep -n 'studio.db' app.py | head -5; grep -n -i 'render' "
            "start_helix.sh | head; grep -n '\"mv\"' factgate_hook.py | head -5; grep -n -i "
            "'output\\|argparse\\|add_argument' workspace/reportcheck/reportcheck.py | head -10; "
            "cat workspace/seam/heartbeat_targets.json | head -40")


@pytest.mark.parametrize("cmd", [
    f"cd {F39}; ls | tee /tmp/x",
    f"cd {F39}; ls | tee /tmp/x >/dev/null",
    f'cd {F39}; ls | tee /tmp/x; echo "tee exit"',                  # catch #60
    f"cd {F39}; ls | tee /tmp/x; wc -l </tmp/x",
    f"cd {F39}; ls | tee /tmp/x; echo 'tee done'",
    f"cd {F39}; ls | tee /tmp/x; printf 'tee: %s\\n' ok",
    f'cd {F39}; echo "no tee here"; ls',
])
def test_39_a_quoted_tee_in_echo_or_printf_is_not_a_tee(real_paths, cmd):
    assert not factgate_hook._writes_facts(cmd, str(WS39)), cmd
    out = call_pre("Bash", {"command": cmd}, WS39)
    assert decision(out) is None, reason(out)


@pytest.mark.parametrize("cmd", [
    f'bash -c "ls | tee {F39}/x"',
    f"cd {F39}; bash -c \"ls | tee x\"",
    f"cd {F39}; sh -c 'ls | tee x'",
    f'cd {F39}; eval "ls | tee x"',
    f"cd {F39}; python3 -c \"import os; os.system('ls | tee x')\"",
    f"echo x | tee {F39}/x",
    f"cd {F39}; echo x | tee x",
    f"cd {F39}; echo $(ls | tee x)",
    f'cd {F39}; echo "$(ls | tee x)"',
    f"cd {F39}; echo `ls | tee x`",
    f"cd {F39}; ls | xargs tee",
    f"cd {F39}; ls | xargs -I{{}} tee {{}}",
    f'cd {F39}; echo "ls | tee x" | bash',                         # echo's text fed to a shell
    f"cd {F39}; printf 'tee x' | sh",
    f"cd {F39}; echo 'tee x' | xargs",
    f'cd {F39}; echo "tee x" > y',
])
def test_39_a_tee_that_can_run_is_still_refused(real_paths, cmd):
    assert factgate_hook._writes_facts(cmd, str(WS39)), cmd
    out = call_pre("Bash", {"command": cmd}, WS39)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


def test_39_catch_64_is_allowed_since_order_62b(real_paths):
    # ORDER 39 pinned #64 as a P4 refusal; order 62b (P-20261004-9) allows it: a read-only harness.
    out = call_pre("Bash", {"command": CATCH_64}, HELIX)
    assert out == {}, reason(out)
    assert not real_paths["catches"].exists()


@pytest.mark.parametrize("cmd", [
    "find workspace -name 'refusals.log' -exec wc -l {} \\;",
    "find workspace -name refusals.log -exec wc -l {} +",
    "find ~/helix/workspace -name refusals.log -execdir head -3 {} \\;",
    "find build_orders -name 'run_*.sh' -exec grep -n systemd-run {} +",
    "find build_orders -name 'run_*.sh' -ok wc -l {} \\;",
])
def test_39_find_exec_of_a_reader_is_not_a_run(cmd):
    assert not factgate_hook._p6_runs(cmd), cmd


def test_39_catch_65_is_no_longer_a_p6_run(real_paths):
    """P6, P7 and P4 all pass catch #65 now. P5 still refuses the whole command, for a different
    reason this order does not touch: `grep -n '"mv"' factgate_hook.py` holds a write word and
    names factgate. Without that part the command is allowed."""
    assert not factgate_hook._starts_build_or_restart(CATCH_65)
    assert not factgate_hook._writes_build_path(CATCH_65, str(HELIX))
    assert not factgate_hook._writes_facts(CATCH_65, str(HELIX))
    rest = CATCH_65.replace(" grep -n '\"mv\"' factgate_hook.py | head -5;", "")
    assert rest != CATCH_65
    out = call_pre("Bash", {"command": rest}, HELIX)
    assert decision(out) is None, reason(out)


@pytest.mark.parametrize("cmd", [
    "find build_orders -name run_order.sh",                        # ruling 3 on find's own words
    "find build_orders -name run_order.sh -exec cat {} \\;",
    "find build_orders/run_order.sh 16",
    "find build_orders -name run_order.sh -exec bash {} \\;",
    "find build_orders -name run_order.sh -execdir sh {} +",
    "find build_orders -name run_order.sh -exec {} \\;",
    "find . -exec run_order.sh \\;",
    "find . -name x -exec ~/helix/build_orders/run_order.sh 39 \\;",
    "find . -ok systemd-run --user true \\;",
    "find . -exec start_helix.sh {} +",
    "find . -exec ~/helix/nightly_restart.sh \\;",
    "find . -exec env run_queue.sh \\;",
    "find . -exec sh -c 'run_order.sh 39' \\;",
    "find . -exec ssh host run_order.sh \\;",
    "find . -name x -exec cat {} \\; | bash run_order.sh",
])
def test_39_find_exec_that_runs_a_p6_name_is_still_refused(real_paths, cmd):
    assert factgate_hook._starts_build_or_restart(cmd), cmd
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


@pytest.mark.parametrize("cmd", [
    "mv ~/helix/app.py /tmp/",
    "mv ~/helix/app.py /tmp/app.py",
    "cd ~/helix; mv app.py /tmp/",
    "mv -t /tmp ~/helix/app.py",
    "mv --target-directory=/tmp ~/helix/start_helix.sh",
    "mv /tmp/a ~/helix/build_orders/BUILD_PROTOCOL.md /tmp/d/",
    "mv ~/helix/build_orders/reports/ORDER_38_report.md /tmp/",
    "mv ~/helix/workspace/cycle/MOVES.jsonl ~/helix/workspace/old.jsonl",
    "mv ~/helix/.lkg /tmp/",
])
def test_39_p7_judges_every_mv_operand(real_paths, cmd):
    assert factgate_hook._writes_build_path(cmd, str(HELIX)), cmd
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


@pytest.mark.parametrize("cmd", [
    "mv /tmp/a /tmp/b",
    "mv workspace/x workspace/y",
    "cd ~/helix; mv workspace/x workspace/y",
    "mv -t /tmp/d /tmp/a /tmp/b",
    "mv ~/helix/build_orders/ORDER_99_x.md ~/helix/build_orders/ORDER_99_y.md",
    "cp ~/helix/app.py /tmp/app.py",                               # a cp source is a read
    "ln -s ~/helix/app.py /tmp/applink",                           # ln sources are not judged
])
def test_39_mv_outside_the_build_path_and_cp_reads_are_allowed(real_paths, cmd):
    assert not factgate_hook._writes_build_path(cmd, str(HELIX)), cmd
    out = call_pre("Bash", {"command": cmd}, HELIX)
    assert decision(out) is None, reason(out)


def test_39_docstring_p7_names_wall_exec():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "wall_exec.sh" in p7


def test_39_no_new_helper_carries_an_escape_hatch_word():
    import re
    names = [n for n in dir(factgate_hook) if n.startswith(("_p4", "_p6", "_p7", "_tee", "_TEE", "_P"))]
    assert not [n for n in names if re.search(r"force|override|bypass|skip_check", n, re.I)]


# ---------------------------------------------------------------- ORDER 43: catch #67, a quoted tee piped into a reader
# Catch #67 (2026-10-03, P4): `cd …/facts; ls | tee /tmp/x; echo "tee exit $?" | tail -1` was refused.
# The cause was the pipe, not the `$?`: order 39 counted echo's quoted `tee` as text only when that
# output was not piped on. Text piped into readers that never run their input is text.
P43 = "cd ~/helix/memory/facts; "
ROWS_43 = [
    'ls | tee /tmp/x; echo "tee exit"',
    'ls | tee /tmp/x; echo "tee exit $?" | tail -1',               # catch #67
    'ls | tee /tmp/x; echo hi | tail -1',
    'ls | tee /tmp/x; echo "a $?"',
    'echo "a $?" | tail -1',
]


@pytest.mark.parametrize("rest", ROWS_43)
def test_43_the_five_rows_are_not_fact_writes(real_paths, rest):
    cmd = P43 + rest
    assert not factgate_hook._writes_facts(cmd, str(WS39)), cmd
    out = call_pre("Bash", {"command": cmd}, WS39)
    assert decision(out) is None, reason(out)
    assert not real_paths["catches"].exists()


@pytest.mark.parametrize("cmd", [
    P43 + "ls | tee /tmp/x; echo 'tee exit' | tail -1 | grep -c exit",
    P43 + 'ls | tee /tmp/x; printf "tee %s\\n" "$?" | head -1',
])
def test_43_quoted_tee_piped_into_readers_is_text(real_paths, cmd):
    assert not factgate_hook._writes_facts(cmd, str(WS39)), cmd


@pytest.mark.parametrize("cmd", [
    P43 + "ls | tee new.md",
    P43 + 'echo "x $?" | tee -a 01_example_aeec.md',
    'ls | tee ~/helix/memory/facts/z.md; echo "$?" | tail -1',
    P43 + 'ls | tee /tmp/x; echo "tee x" | tail -1 | sh',            # a shell anywhere: no text tees
    P43 + "ls | tee /tmp/x; echo 'tee x' | awk '{system($0)}'",      # not a reader
    P43 + "ls | tee /tmp/x; echo 'tee x' | tail -1 | xargs",
    P43 + "echo 'tee x' | tail -1 > y.md",                          # the reader's redirect lands inside
])
def test_43_real_fact_writes_are_still_refused(real_paths, cmd):
    assert factgate_hook._writes_facts(cmd, str(WS39)), cmd
    out = call_pre("Bash", {"command": cmd}, WS39)
    assert decision(out) == "deny", f"{cmd!r} was ALLOWED"


# ---------------------------------------------------------------- ORDER 54d: build_orders/tools (the order lint)
TOOLS54 = str(HELIX / "build_orders" / "tools")


def test_54d_the_order_lint_is_build_path_and_a_write_there_is_p7(real_paths):
    assert factgate_hook._build_path_owns(TOOLS54 + "/orderlint.py")
    out = call_pre("Write", {"file_path": TOOLS54 + "/orderlint.py", "content": "x = 1\n"}, HELIX)
    assert decision(out) == "deny", f"Write to build_orders/tools/orderlint.py was ALLOWED: {out}"
    assert "build path" in reason(out), reason(out)


def test_54d_docstring_p7_names_the_tools_dir():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    assert "build_orders/tools" in p7


# ---------------------------------------------------------------- ORDER 55: the catches corpus (P-20261004-4, rewritten)
# Every CATCHES.jsonl line with n <= 72 that carries a `command`, pasted here as a literal list on
# 2026-10-04 (the live log is a moving file and is never read by this test; order 32's rule).
# (n, command, expect, rule): `rule` is the rule the hook gave on 2026-10-04 before order 55's edit.
# The thirteen `allow` rows are Odin's: 31, 34, 42, 45, 46, 55, 67 were already allowed; 38, 50, 51, 60,
# 65 and 72 were retracted false blocks still refused that day. 57 and 58 were retracted as test lines
# written to the live log, but the commands themselves must stay refused.
# ORDER 62b (P-20261004-9): 41, 64 and 69 are now allowed (the hook judges what python code WRITES, resolved).
# 20 and 29 are added as `allow`; they carry no `command`, so each is its `where` minus the rule, i.e. the
# command cut at 120 characters (#20 is cut inside its heredoc). Both were already allowed before order 62b.
# 62 and 63 pin the user's condition (the cycle ledgers stay refused). They were Write-tool refusals with no
# command; here they are the Bash append of #58's shape, and test_62b_cycle_ledgers_stay_closed pins the Write.
HOOK_CORPUS = [
    (20, 'cd ~/helix/build_orders; f=ORDER_14_hook_self_protection.md; python3 - "$f" <<\'EOF\'\nimport sys\np=sys.argv[1]; s=open(p).',
     'allow', None),
    (29, 'cd __HOME__/helix/build_orders && cp ORDER_16_guards_window_retractions.md __HOME__/helix/workspace/',
     'allow', None),
    (31, "cd ~/helix/build_orders; date; ls -la --time-style=+%H:%M runs/ | tail -8; ls -la --time-style=+%H:%M reports/ | tail -3; systemctl --user list-units 'helix-builder-*' --all --no-pager 2>&1 | head; tail -3 runs/RUNS.jsonl 2>/dev/null",
     'allow', None),
    (32, 'echo x >> ~/helix/app.py',
     'deny', 'P7'),
    (33, 'bash ~/helix/build_orders/run_order.sh 99',
     'deny', 'P6'),
    (34, 'cd ~/helix/build_orders; date; ls -la --time-style=+%m-%d_%H:%M runs/ | tail -4; tail -1 runs/RUNS.jsonl | cut -c1-250; ls -la --time-style=+%m-%d_%H:%M reports/ | tail -2; pgrep -af "[c]laude -p" | cut -c1-120; systemctl --user list-units \'helix-builder-*\' --all --no-pager 2>&1 | head -5',
     'allow', None),
    (36, 'systemctl --user start helix-builder-99',
     'deny', 'P6'),
    (37, 'sha256sum ~/helix/app.py; echo x >> ~/helix/app.py',
     'deny', 'P7'),
    (38, 'for p in __HOME__/helix/workspace/factgate/state/FACTS_LEDGER.jsonl __HOME__/helix/workspace/seam/registry/REGATE_LEDGER.jsonl __HOME__/helix/workspace/catches/CATCHES.jsonl; do [ "$(realpath $p)" = "$p" ] && echo "ok $p" || echo "SYMLINK $p -> $(realpath $p)"; done; ps -o pid,cmd -p $(pgrep -f "python app.py") ; grep Groups /proc/$(pgrep -f "python app.py")/status',
     'allow', None),
    (40, 'cd ~/helix/build_orders; date +%H:%M:%S; systemctl --user is-active helix-builder-20-20260930_223404; ls -l reports/ORDER_20* 2>&1; ls ~/helix/vault/; F=runs/ORDER_20_20260930_223404.jsonl; wc -l $F; grep -o \'"description":"[^"]*"\' $F | tail -12; grep -c \'"name":"Bash"\' $F; python3 - <<\'E\'\nimport json\nf=\'runs/ORDER_20_20260930_223404.jsonl\'\nfor l in open(f):\n    try:d=json.loads(l)\n    except:continue\n    if d.get(\'type\')==\'assistant\':\n        for c in d[\'message\'].get(\'content\',[]):\n            if c.get(\'type\')==\'text\': last=c[\'text\']\nprint(last[-600:])\nE',
     'deny', 'P6'),
    (41, 'cd ~/helix/build_orders; python3 - <<\'E\'\np=\'ORDER_21_preflight_gate_and_leftovers.md\'\ns=open(p).read()\na=s.index(\'C1. **Retraction of catch #40**\'); b=s.index(\'C3. **Schedule prompt:**\')\ns=s[:a]+\'C1/C2. WITHDRAWN (see reissue note 1).\\n\'+s[b:]\ns=s.replace("""   Only files changed: `workspace/preflight/*` (new), `workspace/sealgate/{sealgate.py,test_sealgate.py,README.md}`,\n   `factgate_hook.py`, `test_factgate_hook.py`, `memory/schedules.json` (prompt field only), CATCHES (one line),\n   and the seam git merge.""","""   `factgate_hook.py`, `test_factgate_hook.py` unchanged. CATCHES: you append nothing.\n   Only files changed: `workspace/preflight/*` (new), `workspace/sealgate/{sealgate.py,test_sealgate.py,README.md}`,\n   `memory/schedules.json` (prompt field only), the attempt-1 report rename, and the seam git merge.""")\ns=s.replace("""c. `systemctl --user is-active helix-builder-99` → runs, no catch; `systemctl --user start helix-builder-99` → refused.\n""","")\ns=s.replace("1. `pytest` green for `test_preflight.py`, `sealgate/test_sealgate.py`, `test_factgate_hook.py`,","1. `pytest` green for `test_preflight.py`, `sealgate/test_sealgate.py`, `test_factgate_hook.py` (unchanged count),")\ns=s.replace("No restart needed\\nunless C2\'s hook change requires one — say which (the hook is read per call, so it should not).","No restart needed.")\nopen(p,\'w\').write(s)\nE\ngrep -n -E "C1|C2|CATCHES|restart" ORDER_21_preflight_gate_and_leftovers.md',
     'allow', None),
    (42, 'cd ~/helix/workspace/factgate/state; grep -n -E "14[89]_|15[01]_" FACTS_LEDGER.jsonl | cut -c1-120; sed -n 1,40p ~/helix/build_orders/reports/ORDER_22_report.md | grep -n -i -E "legacy|baseline|stop|question" | head',
     'allow', None),
    (44, "cd ~/helix; curl -s -m5 localhost:7777/stat | head -c 150; echo; F=$(ls workspace/factgate/factgate.py 2>/dev/null || find . -name factgate.py -not -path './.venv/*' | head -1); echo $F; python3 $F interim memory/facts 2>&1 | tail -8; python3 $F release memory/facts/151_example_0d9d.md --witness x 2>&1 | tail -2",
     'deny', 'P5'),
    (45, 'cd ~/helix; grep -n "CLAUDE_CONFIG_DIR\\|claude/config\\|Ny diskenhed" start_helix.sh app.py render_daemon.py 2>/dev/null | head; grep -rln "Ny diskenhed" ~/.config/systemd ~/.local/share/applications ~/Desktop 2>/dev/null; crontab -l 2>/dev/null | grep -i diskenhed',
     'allow', None),
    (46, 'cd ~/helix/build_orders; ls; grep -n "" BUILD_PROTOCOL.md | sed -n 1,15p; grep -n "def.*build\\|/build\\|run_order" ~/helix/app.py | head -20; wc -l run_order.sh',
     'allow', None),
    (48, 'bash ~/helix/build_orders/run_order.sh 99',
     'deny', 'P6'),
    (49, 'bash ~/helix/build_orders/run_queue.sh 98',
     'deny', 'P6'),
    (50, 'T=$(mktemp -d); mkdir -p $T/h/memory/facts $T/h/workspace/catches $T/h/workspace/scratch; touch $T/h/app.py\ntime bwrap --bind / / --bind $T/h $T/h --bind $T/h/memory $T/h/memory --bind $T/h/workspace $T/h/workspace --ro-bind $T/h/memory/facts $T/h/memory/facts --ro-bind $T/h/workspace/catches $T/h/workspace/catches --ro-bind $T/h/app.py $T/h/app.py -- bash -c "\npython3 -c \\"open(\'$T/h/memory/facts/x\',\'w\')\\" 2>&1 | tail -1\necho y >> $T/h/app.py 2>&1; mv $T/h/workspace $T/h/box2 2>&1\nmkdir $T/h/workspace/newproj && echo NEWDIR_OK; echo z > $T/h/new.txt && echo NEWFILE_OK\ncurl -s -o /dev/null -w \'stat %{http_code}\\n\' localhost:7777/stat; nvidia-smi -L 2>/dev/null | head -1; ls __MEDIA__/ | head -3"\nrm -rf $T',
     'allow', None),
    (51, 'cat > ~/helix/build_orders/ORDER_23c_os_write_wall.md <<\'EOF\'\n# WORK ORDER 23c: ITEM 25, THE OS-LEVEL WRITE WALL FOR ODIN\'S BASH (bwrap). Replaces the wall half of ORDER_23 (void).\n# Written by Odin 2026-10-01 under standing rule `145_*`. Spec: `memory/facts/140_*` item 25.\n# Execute with `BUILD_PROTOCOL.md` open. ONE ORDER, then stop and report.\n# Timing gate: not between 03:30 and 05:30; no nightly running. Record session start first.\n# 🔴 No sudo. **Never restart Helix.** Foreground only. Full app.py backup/rollback as in order 12.\n# READ FIRST: `reports/ORDER_23_attempt1_stopped_report.md`. Its Step-0 findings, nightly path and design notes\n# are adopted below as RULINGS. Do not re-litigate them. Part 1 of order 23 already shipped in 23b, so it is out of scope.\n\n## What changed since attempt 1 (verified by Odin 2026-10-01, ~17:30)\n- the user installed an AppArmor profile for bwrap (`~/hdd_migration/bwrap_apparmor.sh`, printed "BWRAP OK").\n  `bwrap --ro-bind / / --dev /dev --proc /proc true` now succeeds as Odin\'s user, while\n  `kernel.apparmor_restrict_unprivileged_userns` is still 1 (the narrow fix, as planned).\n- Odin pre-tested the wall shape on temp dirs. Results:\n  - ro-bound file and dir → `EROFS` on python open and `>>`.\n  - `mv` / `rm -rf` of a self-bound ancestor → "Device or resource busy" (the parent-rename hole is closed).\n  - `mkdir` new dir in a self-bound ancestor → OK. New file in it → OK.\n  - `curl localhost:7777/stat` → 200. `/media/...` visible. Wall time ≈ 0.15 s for the whole probe incl. python.\n  - 🔴 **TRAP FOUND:** with `--bind / /` alone, EVERY `>/dev/null` and `2>/dev/null` redirect fails with\n    "Permission denied" (device nodes are nodev inside the userns), and `nvidia-smi` fails with it.\n    `--dev-bind /dev /dev` fixes both (REDIR_OK, GPU visible). **It is mandatory.**\n\n## RULINGS (pre-decided. These are not ambiguities. Do not stop on them.)\nR1. **Mechanism = bwrap via `CLAUDE_CODE_SHELL_PREFIX`**, passed through `ClaudeAgentOptions(env=…)` in the tools\n    branch of `_lane_options` (`app.py` ~line 813). The prefix is built **inline in app.py**: no wrapper script file\n    (a wrapper is editable by Odin\'s tools; app.py is not).\nR2. **bwrap arguments, in this order:** `bwrap --bind / / --dev-bind /dev /dev`, then `--bind A A` for each ancestor\n    (`~/helix`, `~/helix/memory`, `~/helix/workspace`, `~/helix/workspace/seam`), then `--ro-bind-try P P` for each\n    guarded path, then `--`. NOT `--dev /dev`, NOT `--proc /proc`. Network is NOT unshared.\n    The CLI passes the prefix ONE argument, the whole command string, so the prefix ends `-- /bin/bash -c` (verify\n    against the attempt-1 probe format and adjust only the tail if the measured format requires it).\nR3. **Guarded set** = `~/helix/memory/facts`, `~/helix/workspace/factgate`, `~/helix/workspace/seam/registry`,\n    `~/helix/workspace/catches`, `~/helix/factgate_hook.py`, `~/helix/test_factgate_hook.py`, `~/helix/app.py`,\n    `~/helix/build_orders`. One constant, realpath-resolved. Add a test that fails if it and the hook\'s guarded\n    set drift (import from the hook if practical, otherwise an explicit equality test).\nR4. **Lanes:** walled = CHAT and WORK (WORK carries front-desk jobs). FRONT has no tools and is untouched.\n    **Add `SCHED = Lane("sched")`, unwalled**, used ONLY by `scheduler_tick` via `start_job(..., lane=SCHED)`.\n    `start_job`\'s default stays WORK. `scheduler_tick` keeps "skip while WORK is busy" (test_scheduler.py relies\n    on it) and also skips while SCHED is busy. Any other way the 04:30 nightly would be walled is a bug to fix,\n    not a reason to stop.\nR5. **Hooks are unaffected:** factgate\'s PreToolUse/PostToolUse are Python callbacks in app.py\'s process (lines\n    834–842), not shell commands, so they still write CATCHES / FACTS_LEDGER. Confirm with one test. Write/Edit\n    tools run in the CLI process and are not walled. Confirm with one test.\nR6. **Fail closed, loud:** if `shutil.which("bwrap")` is None at options',
     'allow', None),
    (52, 'cd ~/helix; ps -o pid,lstart -p 641440; grep -n -iE \'record|restart_log|RESTARTS|jsonl\' build_orders/reports/ORDER_31_report.md | head -20; python3 -c "p=\'__HOME__/helix/\'+\'nightly_restart.sh\'; open(p,\'a\')" 2>&1 | tail -1',
     'deny', 'P7'),
    (53, 'bash ~/helix/nightly_restart.sh --help',
     'deny', 'P6'),
    (55, 'cd ~/helix/memory/facts; for p in 15_ 91_ 99_ 101_ 103_ 112_ 120_ 142_ 145_ 153_ 158_ 164_; do f=$(ls ${p}*.md); printf "%6d %s\\n" $(wc -c < $f) "$f"; done | tee /tmp/pins.txt; awk \'{s+=$1} END {print s}\' /tmp/pins.txt; sed -n 1190,1215p ~/helix/app.py | head -30',
     'allow', None),
    (57, 'touch ~/helix/workspace/cycle/housekeeping_enabled.flag',
     'deny', 'P7'),
    (58, "echo '{}' >> ~/helix/workspace/cycle/MOVES.jsonl",
     'deny', 'P7'),
    (59, 'cd ~/helix/memory/facts; ls | tee /tmp/x >/dev/null; echo "tee exit $? lines $(wc -l </tmp/x)"; echo x >> ~/helix/memory/PINNED.txt; echo "append exit $?"',
     'deny', 'P4'),
    (60, 'cd ~/helix/memory/facts; ls | tee /tmp/x >/dev/null; echo "tee exit $? lines $(wc -l </tmp/x)"',
     'allow', None),
    (62, "echo '{}' >> ~/helix/workspace/cycle/MOVES.jsonl",
     'deny', 'P7'),
    (63, "echo '{}' >> ~/helix/workspace/cycle/NO_RECORD_NEEDED.jsonl",
     'deny', 'P7'),
    (64, 'cd ~/helix; python3 -B -c "\nimport factgate_hook as h, os\nF=os.path.expanduser(\'~/helix/memory/facts\')\nfor c in [\'cd ~/helix/memory/facts; ls | tee /tmp/x\',\n          \'cd ~/helix/memory/facts; ls | tee /tmp/x >/dev/null\',\n          \'cd ~/helix/memory/facts; ls | tee /tmp/x; echo \\"tee exit\\"\',\n          \'cd ~/helix/memory/facts; ls | tee /tmp/x; wc -l </tmp/x\']:\n    print(h._writes_facts(c, os.path.expanduser(\'~/helix/workspace\')), c)\n"',
     'allow', None),
    (65, 'cd ~/helix; find workspace -name \'refusals.log\' -exec wc -l {} \\; 2>/dev/null; grep -n \'ended\' build_orders/builder_run.sh | head -5; grep -n \'studio.db\' app.py | head -5; grep -n -i \'render\' start_helix.sh | head; grep -n \'"mv"\' factgate_hook.py | head -5; grep -n -i \'output\\|argparse\\|add_argument\' workspace/reportcheck/reportcheck.py | head -10; cat workspace/seam/heartbeat_targets.json | head -40',
     'allow', None),
    (67, 'cd ~/helix/memory/facts; ls | tee /tmp/x; echo "tee exit $?" | tail -1',
     'allow', None),
    (68, 'sha256sum ~/helix/app.py; mv ~/helix/app.py /tmp/',
     'deny', 'P7'),
    (69, 'cd ~/helix; .venv/bin/python -B -c "\nimport factgate_hook as h, os\nW=os.path.expanduser(\'~/helix/workspace\')\nfor c in [\'cd ~/helix/memory/facts; ls | tee /tmp/x; echo \\"tee exit\\"\',\n \'cd ~/helix/memory/facts; ls | tee /tmp/x; echo \\"tee exit \\$?\\" | tail -1\',\n \'cd ~/helix/memory/facts; ls | tee /tmp/x; echo hi | tail -1\',\n \'cd ~/helix/memory/facts; echo \\"a \\$?\\"\',\n \'cd ~/helix/memory/facts; ls | tail -1\']:\n    print(h._writes_facts(c, W), \'|\', c)\n"; grep -n "def _writes_facts" -A60 factgate_hook.py | head -90',
     'allow', None),
    (71, 'cd ~/helix; grep -rn -i "credential" --include=*.py workspace/seam workspace/factgate* app.py 2>/dev/null | grep -v test_ | head; find . -name "factgate_hook.py" -not -path "*/node_modules/*" | head -2; echo; cd workspace/seam && git branch -v | head; git log --oneline -3; grep -n "canary_line()," nightly.py test_nightly_regate.py; cd ../replay && git branch -v; git status --short | head; touch __BULK__/.w && rm __BULK__/.w && echo HDD-writable; ls ~/helix/memory/facts | grep -i -E "credential|accounts|\\.env"',
     'deny', 'P5'),
    (72, 'cd /tmp && ~/.local/bin/virtualenv -q /tmp/pfvenv >/dev/null 2>&1 && /tmp/pfvenv/bin/pip -q install pyflakes 2>&1 | tail -1; /tmp/pfvenv/bin/python -m pyflakes --version; cd ~/helix; /tmp/pfvenv/bin/python -m pyflakes app.py factgate_hook.py 2>&1 | grep -E "undefined name|undefined local|syntax|invalid" | head; echo "---"; /tmp/pfvenv/bin/python -m pyflakes app.py 2>&1 | wc -l; python3 - <<\'EOF\'\nimport ast\nt=ast.parse(open(\'__HOME__/helix/app.py\').read())\nimport os\nmods=set()\nfor n in ast.walk(t):\n    if isinstance(n,ast.Import): mods|={a.name.split(\'.\')[0] for a in n.names}\n    if isinstance(n,ast.ImportFrom) and n.module: mods.add(n.module.split(\'.\')[0])\nprint(sorted(m for m in mods if os.path.exists(f\'__HOME__/helix/{m}.py\')))\nEOF\ngrep -n "^REG\\|REGISTRY\\|LEDGER\\s*=" workspace/seam/nightly.py | head; grep -n "^def today\\|^TODAY" workspace/regate/regate.py',
     'allow', None),
]


# Catch #60 cannot be allowed without moving a deny row: #59 is #60's exact command plus
# `echo x >> ~/helix/memory/PINNED.txt`, which writes outside the fact base. Judged by effect, #59 would
# pass P4 and be refused by P7 instead, and its row says P4. Order 55 leaves #60 failing, on the record.
XFAIL_55 = {60: "#59 = #60 + a PINNED.txt append; allowing #60 by effect moves #59 from P4 to P7"}
CORPUS_PARAMS = [pytest.param(*r, id=f"catch{r[0]}",
                              marks=[pytest.mark.xfail(strict=True, reason=XFAIL_55[r[0]])]
                              if r[0] in XFAIL_55 else [])
                 for r in HOOK_CORPUS]


@pytest.mark.parametrize("n,cmd,expect,rule", CORPUS_PARAMS)
def test_55_every_logged_catch_keeps_its_verdict(real_paths, n, cmd, expect, rule):
    out = asyncio.run(factgate_hook.pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": factgate_hook.BRAIN}, None, None))
    if expect == "allow":
        assert out == {}, f"catch #{n} refused: {reason(out)}"
        assert not real_paths["catches"].exists()
    else:
        assert decision(out) == "deny", f"catch #{n} was ALLOWED"
        got = json.loads(real_paths["catches"].read_text().splitlines()[0])["where"].split()[0]
        assert got == rule, f"catch #{n}: refused by {got}, expected {rule}"


def test_55_the_corpus_is_complete():
    ns = [r[0] for r in HOOK_CORPUS]
    assert len(ns) == len(set(ns)) == 35
    assert {r[0] for r in HOOK_CORPUS if r[2] == "allow"} == {20, 29, 31, 34, 38, 41, 42, 45, 46, 50, 51,
                                                              55, 60, 64, 65, 67, 69, 72}
    assert {57, 58, 62, 63} <= {r[0] for r in HOOK_CORPUS if r[2] == "deny"}



# ---------------------------------------------------------------- ORDER 55 Part C: each fixed shape, a variant and its write twin
H55 = str(HELIX)


def rule_of(real_paths, cmd, cwd=HELIX):
    """None if allowed, else the rule the hook logged."""
    out = call_pre("Bash", {"command": cmd}, cwd)
    if decision(out) is None:
        assert not real_paths["catches"].exists()
        return None
    return json.loads(real_paths["catches"].read_text().splitlines()[-1])["where"].split()[0]


VARIANTS_55 = [
    # 38: a loop of reads over factgate's files, a quoted `->` in echo's text
    ("38", 'for p in ~/helix/workspace/factgate/factgate.py ~/helix/factgate_hook.py; do [ -L "$p" ] '
           '&& echo "LINK $p -> $(readlink -f $p)" || echo "file $p"; done'),
    # 50: a scratch brain under a fresh mktemp directory, and one under /tmp
    ("50", "S=$(mktemp -d); mkdir -p $S/brain/memory/facts $S/brain/workspace/scratch2; "
           "touch $S/brain/memory/facts/001_x.md $S/brain/app.py; cp /etc/hostname $S/brain/memory/facts/; "
           "echo y >> $S/brain/workspace/scratch2/x.txt; mv $S/brain/memory $S/brain/m2; rm -rf $S"),
    ("50", "mkdir -p /tmp/o55brain/memory/facts && touch /tmp/o55brain/memory/facts/x.md; "
           "rm -rf /tmp/o55brain"),
    # 51: Odin writing an order whose BODY names factgate, facts and app.py
    ("51", "cat > ~/helix/build_orders/ORDER_99_o55_variant.md <<'EOF'\n# order\nRun `factgate record "
           "memory/facts x`; rm -rf workspace/factgate; echo x > memory/facts/1.md; mv app.py /tmp\nEOF"),
    # 65: find -exec of a reader over factgate, and a grep for write words in the hook
    ("65", "cd ~/helix; find workspace/factgate -name '*.py' -exec wc -l {} +; "
           "grep -n '\"rm\"\\|\"cp\"' factgate_hook.py | head -3"),
    # 72: a scratch venv, a pip install, a linter over the build path, python open() for reading
    ("72", "cd /tmp && ~/.local/bin/virtualenv -q /tmp/o55v >/dev/null 2>&1 && /tmp/o55v/bin/pip -q "
           "install ruff 2>&1 | tail -1; cd ~/helix; /tmp/o55v/bin/python -m ruff check factgate_hook.py "
           "scheduler.py 2>&1 | head; python3 - <<'EOF'\n"
           f"print(len(open('{H55}/frontdesk.py').read()), open('{H55}/app.py', 'rb').read(9))\nEOF"),
]


@pytest.mark.parametrize("n,cmd", VARIANTS_55, ids=[f"variant{n}_{i}" for i, (n, _c) in enumerate(VARIANTS_55)])
def test_55_a_variant_of_each_read_shape_is_allowed(real_paths, n, cmd):
    assert rule_of(real_paths, cmd) is None, f"variant of #{n} refused"


# #60's variant shares #60's xfail: the same P4 rule refuses it, and must, while #59 stays P4.
@pytest.mark.xfail(strict=True, reason=XFAIL_55[60])
def test_55_variant_60_is_allowed(real_paths):
    cmd = 'cd ~/helix/memory/facts; ls | tee /tmp/o55y >/dev/null; echo "tee done, $(wc -l </tmp/o55y) lines"'
    assert rule_of(real_paths, cmd) is None


TWINS_55 = [
    # 38's twin: the same echo, landing in factgate's ledger
    ("38", 'for p in ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl; do echo "ok -> x" >> $p; done', "P5"),
    ("38", 'echo "ok -> x" > ~/helix/workspace/factgate/state/FACTS_LEDGER.jsonl', "P5"),
    # 50's twins: the scratch dir is real, the write is not in it
    ("50", "T=$(mktemp -d); mkdir -p $T/h/memory/facts; touch ~/helix/memory/facts/x.md", "P4"),
    ("50", "T=$(mktemp -d); T=~/helix; touch $T/memory/facts/x.md", "P4"),
    ("50", "T=$(mktemp -d); touch $T/../..__HOME__/helix/memory/facts/x.md", "P4"),
    ("50", "T=$(mktemp -d -p ~/helix); touch $T/../app.py", "P7"),
    ("50", "T=$(mktemp -d); touch $T/h/app.py; touch ~/helix/app.py", "P7"),
    # 51's twins: the heredoc's TARGET is guarded, or the "data" is run afterwards
    ("51", "cat > ~/helix/workspace/factgate/notes.md <<'EOF'\nhello\nEOF", "P5"),
    ("51", "cat > /tmp/o55s.sh <<'EOF'\npython3 workspace/factgate/factgate.py record memory/facts x\nEOF\n"
           "bash /tmp/o55s.sh", "P5"),
    ("51", "cat > ~/helix/app.py <<'EOF'\nprint('x')\nEOF", "P7"),
    ("51", "cat > ~/helix/memory/facts/200_x.md <<'EOF'\n# x\nEOF", "P4"),
    # 60's twins: the tee lands in the fact base
    ("60", 'cd ~/helix/memory/facts; ls | tee x.md >/dev/null; echo "tee exit $? lines $(wc -l <x.md)"', "P4"),
    ("60", "cd ~/helix; ls memory | tee memory/facts/x.md >/dev/null", "P4"),
    # 65's twins: find -exec of a writer; a real mv beside the grep
    ("65", "cd ~/helix; find workspace/factgate -name '*.pyc' -exec rm {} \\;", "P5"),
    ("65", "cd ~/helix; grep -n '\"mv\"' factgate_hook.py | head -5; mv factgate_hook.py /tmp/", "P5"),
    ("65", "cd ~/helix; find build_orders/reports -name '*.md' -exec rm {} +", "P7"),
    # 72's twins: python open() for writing; pip installing INTO a guarded root; a write word fed by a pipe
    ("72", "cd ~/helix; python3 - <<'EOF'\n"
           f"open('{H55}/app.py', 'w').write('x')\nEOF", "P7"),
    ("72", "/tmp/o55v/bin/pip -q install --target ~/helix/build_orders/tools ruff; wc -l ~/helix/app.py", "P7"),
    ("72", "/tmp/o55v/bin/pip -q install --target ~/helix/workspace/factgate ruff", "P5"),
    ("72", "cd ~/helix/workspace/factgate; git rm -q factgate.py", "P5"),
    ("72", "wc -l ~/helix/app.py; echo ~/helix/app.py | parallel rm", "P7"),
]


@pytest.mark.parametrize("n,cmd,rule", TWINS_55, ids=[f"twin{n}_{i}" for i, (n, _c, _r) in enumerate(TWINS_55)])
def test_55_the_write_twin_of_each_shape_is_refused(real_paths, n, cmd, rule):
    assert rule_of(real_paths, cmd) == rule, f"write twin of #{n}"


def test_55_no_new_helper_or_line_carries_an_escape_hatch_word():
    import re
    import inspect
    for name in ("_p5_effect_text", "_p5_inert", "_p7_temp", "_p7_could_reach", "_p7_parse",
                 "_p4_without_data", "_p4_without_temp"):
        src = inspect.getsource(getattr(factgate_hook, name))
        assert not re.search(r"force|override|bypass|skip_check", name + src, re.I), name


# ---------------------------------------------------------------- ORDER 62b (P-20261004-9): judge what python code WRITES, resolved
# Python code (a heredoc python reads its program from, or `python -c`) is parsed; when every effect resolves,
# its targets are judged and its text no longer counts as a mention. Code that cannot be resolved is judged
# as before. the user's condition: the seven workspace/cycle ledgers stay refused (four added to _P7_FILES here).
CYCLE_62 = ["RESTARTS", "TURNS", "PATCHES", "MOVES", "NO_RECORD_NEEDED", "ATTEMPTS", "GPU"]
FORMS_62 = ["echo x >> ~/helix/workspace/cycle/{}.jsonl",
            "cp /etc/hostname ~/helix/workspace/cycle/{}.jsonl",
            "python3 -c \"open('" + H55 + "/workspace/cycle/{}.jsonl','a')\"",
            "cd ~/helix/workspace/cycle; python3 - <<'E'\nopen('{}.jsonl', 'a').write('x')\nE"]


@pytest.mark.parametrize("name", CYCLE_62)
@pytest.mark.parametrize("form", FORMS_62, ids=["append", "cp", "py_c", "py_heredoc"])
def test_62b_cycle_ledgers_stay_closed(real_paths, name, form):
    assert rule_of(real_paths, form.format(name)) == "P7", f"{name}: {form}"


@pytest.mark.parametrize("name", CYCLE_62)
def test_62b_cycle_ledgers_refuse_the_editor_tools(real_paths, name):
    out = call_pre("Write", {"file_path": f"{H55}/workspace/cycle/{name}.jsonl", "content": "x"}, HELIX)
    assert decision(out) == "deny"
    assert json.loads(real_paths["catches"].read_text().splitlines()[-1])["where"].split()[0] == "P7"


def test_62b_the_cycle_code_and_its_tests_stay_unguarded():
    for f in ("cycle.py", "attempt.py", "gpu_sample.py", "test_cycle.py", "README.md"):
        assert not factgate_hook._build_path_owns(f"{H55}/workspace/cycle/{f}"), f
    assert f"{H55}/workspace/cycle" not in factgate_hook._P7_DIRS


def test_62b_rows_20_and_29_are_the_logged_where_text():
    rows = {r[0]: r[1] for r in HOOK_CORPUS}
    assert len(rows[20]) == len(rows[29]) == 120
    assert rows[20].startswith("cd ~/helix/build_orders; f=ORDER_14_") and rows[20].endswith("s=open(p).")
    assert rows[29].endswith("__HOME__/helix/workspace/")


ALLOWED_62 = [
    # 41's shape on a scratch order file: read it, edit it, write it back
    "cd ~/helix/build_orders; python3 - <<'E'\np='ORDER_99_o62_scratch.md'\ns=open(p).read()\n"
    "s=s.replace('a', 'b')\nopen(p,'w').write(s)\nE\ngrep -n b ORDER_99_o62_scratch.md",
    # the same through sys.argv, the file named by a variable the command sets (catch #20's shape)
    "cd ~/helix/build_orders; f=ORDER_99_o62_scratch.md; python3 - \"$f\" <<'EOF'\nimport sys\n"
    "p=sys.argv[1]; s=open(p).read()\nopen(p,'w').write(s)\nEOF",
    # 64's harness with another pure predicate, and an f-string / os.path.join write to /tmp
    "cd ~/helix; python3 -B -c \"\nimport factgate_hook as h, os\nF=os.path.expanduser('~/helix/memory/facts')\n"
    "for c in ['cd ~/helix/memory/facts; ls | tee /tmp/x', 'rm ~/helix/app.py']:\n"
    "    print(h._only_reads(c), h._p7_parse(c, F)[0][0]['cwd'], c)\n\"",
    "cd ~/helix; python3 - <<'E'\nimport os\nd='/tmp'\nout=os.path.join(d, f'o62_{1}.txt')\n"
    "open(out, 'w').write(open('app.py').read()[:10] + ' memory/facts tee factgate record')\nE",
    # a pure harness that mentions factgate's recording verbs and a fact write, and does neither
    "cd ~/helix; python3 -c \"import factgate_hook as h; print(h._mentions('factgate record x; "
    "tee memory/facts/x.md', '/', h.FACTS))\"",
]


@pytest.mark.parametrize("cmd", ALLOWED_62, ids=[f"allowed{i}" for i in range(len(ALLOWED_62))])
def test_62b_resolved_python_writes_outside_the_guards_are_allowed(real_paths, cmd):
    assert rule_of(real_paths, cmd) is None, cmd


TWINS_62 = [
    # the same python heredoc, writing the fact base / the build path / factgate
    ("cd ~/helix/build_orders; python3 - <<'E'\np='" + H55 + "/memory/facts/x.md'\nopen(p,'w').write('x')\nE", "P4"),
    ("cd ~/helix/memory/facts; python3 - <<'E'\nopen('x.md','w').write('x')\nE", "P4"),
    ("cd ~/helix; python3 - <<'E'\np='app.py'\ns=open(p).read()\nopen(p,'w').write(s)\nE", "P7"),
    ("cd ~/helix; python3 - <<'E'\nimport os\nos.replace('/tmp/x', os.path.join('" + H55 + "', 'app.py'))\nE", "P7"),
    ("cd ~/helix; python3 - <<'E'\nfrom pathlib import Path\nPath('build_orders/reports/x.md').write_text('x')\nE",
     "P7"),
    ("cd ~/helix; python3 - <<'E'\nimport shutil\nshutil.copy('/etc/hostname', '" + H55 + "/build_orders/tools/')\nE",
     "P7"),
    ("cd ~/helix; python3 - <<'E'\nimport shutil\nshutil.rmtree('" + H55 + "/memory')\nE", "P4"),
    ("cd /tmp; python3 -c \"a='fact'; open(f'" + H55 + "/workspace/{a}gate/x', 'w')\"", "P5"),
    ("cd ~/helix; f=app.py; python3 - \"$f\" <<'E'\nimport sys\nopen(sys.argv[1],'a')\nE", "P7"),
    ("cd ~/helix; python3 - build_orders/BUILD_PROTOCOL.md <<'E'\nimport sys\nopen(sys.argv[1],'w')\nE", "P7"),
    # the harness shape calling a hook function that is not on the pure list
    ("cd ~/helix; python3 -B -c \"\nimport factgate_hook as h, os\nF=os.path.expanduser('~/helix/memory/facts')\n"
     "for c in ['cd ~/helix/memory/facts; ls | tee /tmp/x']:\n"
     "    print(h.pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': c}}, None, None), c)\n\"", "P4"),
    ("cd ~/helix; python3 -B -c \"\nimport factgate_hook as h, os\nF=os.path.expanduser('~/helix/memory/facts')\n"
     "for c in ['cd ~/helix/memory/facts; ls | tee /tmp/x']:\n    print(h.record_remember('x.md'), c)\n\"", "P4"),
    # a target that cannot be resolved: judged as before
    ("cd ~/helix/build_orders; python3 - <<'E'\nimport glob\nfor p in glob.glob('reports/*.md'):\n"
     "    open(p,'w')\nE", "P7"),
    ("cd ~/helix; python3 -c \"import os; p=os.environ.get('X', 'app.py'); open(p, 'w')\"", "P7"),
    ("cd ~/helix; python3 -c \"open('$A' + 'app.py', 'w')\"", "P7"),
    # what bash hands python is not what the hook reads: \x61 survives bash's double quotes
    ("cd ~/helix; python3 -c \"open('__HOME__/helix/\\x61pp.py', 'w'); print('app.py')\"", "P7"),
    # code that can run anything is not cleared
    ("cd ~/helix; python3 - <<'E'\nimport subprocess\nprint('app.py'); open('/tmp/x','w')\nE", "P7"),
    # the copy of an order file onto the build path, and a real run
    ("cd ~/helix/build_orders; cp ORDER_62_hook_resolves_write_targets.md ~/helix/app.py", "P7"),
    ("bash ~/helix/build_orders/run_order.sh 99", "P6"),
]


@pytest.mark.parametrize("cmd,rule", TWINS_62, ids=[f"twin{i}" for i in range(len(TWINS_62))])
def test_62b_the_write_twin_of_each_allowed_shape_is_refused(real_paths, cmd, rule):
    assert rule_of(real_paths, cmd) == rule, cmd


def test_62b_p6_judges_a_name_outside_cleared_code_as_before(real_paths):
    # A P6 name only inside cleared python code is text (catch #41's order text names helix-builder). Named
    # in the shell part, P6's run test is unchanged: catch #40 (`python3 -` beside a status read) stays P6.
    assert rule_of(real_paths, "cd ~/helix; python3 -c \"print('bash run_order.sh 99')\"") is None
    assert rule_of(real_paths, "cd ~/helix; wc -l build_orders/run_order.sh; python3 - <<'E'\nprint(1)\nE") \
        == "P6"
    assert rule_of(real_paths, "cd ~/helix; python3 -c \"import os; os.system('bash run_order.sh 99')\"") \
        == "P6"


def test_62b_the_pure_list_is_the_order_list():
    assert factgate_hook._PY_HOOK_PURE == ("_writes_facts", "_p7_parse", "_mentions", "_redirect_targets",
                                           "_only_reads")
    for name in ("pre_tool_use", "post_tool_use", "_log_catch", "_deny", "record_remember",
                 "_factgate_check"):
        assert name not in factgate_hook._PY_HOOK_PURE


def test_62b_every_pure_predicate_does_no_io(real_paths, monkeypatch):
    import builtins

    def boom(*a, **k):
        raise AssertionError("I/O from a predicate on the pure list")

    cmd = ("cd ~/helix/memory/facts; ls | tee x.md >/dev/null; echo x >> ~/helix/app.py; "
           "python3 - <<'E'\nopen('/tmp/o62','w')\nE")
    calls = {"_writes_facts": (cmd, H55), "_p7_parse": (cmd, H55), "_mentions": (cmd, H55, REAL_FACTS),
             "_redirect_targets": (cmd,), "_only_reads": (cmd,)}
    assert set(calls) == set(factgate_hook._PY_HOOK_PURE)
    for mod, attr in ((builtins, "open"), (os, "open"), (os, "system"), (os, "popen"),
                      (subprocess, "run"), (subprocess, "Popen"), (subprocess, "call"),
                      (subprocess, "check_call"), (subprocess, "check_output")):
        monkeypatch.setattr(mod, attr, boom)
    got = {name: getattr(factgate_hook, name)(*args) for name, args in calls.items()}
    monkeypatch.undo()
    assert got["_writes_facts"] is True and got["_only_reads"] is False, got


def test_62b_no_new_helper_carries_an_escape_hatch_word():
    import re
    import inspect
    names = [n for n in dir(factgate_hook) if n.startswith(("_py", "_PY"))]
    assert len(names) >= 5, names
    for name in names:
        obj = getattr(factgate_hook, name)
        src = inspect.getsource(obj) if callable(obj) else repr(obj)
        assert not re.search(r"force|override|bypass|skip_check", name + src, re.I), name


def test_62b_docstring_p7_names_the_cycle_ledgers():
    p7 = factgate_hook.__doc__.split("ORDER 16 — P7.")[1].split("Honest scope of P7")[0]
    for name in ("RESTARTS", "TURNS", "ATTEMPTS", "GPU"):
        assert name in p7, name


NOT_CLEARED_62 = [
    "cd /tmp; (cd ~/helix); python3 -c \"open('app.py','w')\"",                 # a subshell's cd
    "cd ~/helix; pushd /tmp; popd; python3 -c \"open('x.md','w')\"",
    "cd ~/helix; for i in 1; do python3 -c \"open('x.md','w')\"; done",
    "cd /tmp; python3 - <<'E'\n# -*- coding: utf-7 -*-\nprint(1)\nE",              # python decodes by the cookie
    "cd /tmp; python3 - <<E\nopen('$X','w')\nE",                                  # bash expands this body
    "cd /tmp; python3 - <<-'E'\n\tprint(1)\n\tE",
    "cd /tmp; PYTHONPATH=/tmp/x python3 -c \"print(1)\"",
    "cd /tmp; python3(){ :; }; python3 -c \"print(1)\"",
    "cd /tmp; python3 -c \"print(1)\" # comment",
    "cd /tmp; python3 -c \"open = print; open('x','w')\"",
    "cd /tmp; python3 -c \"import os as o; f = o; f.remove('x')\"",
    "cd /tmp; python3 -c \"[open(p, 'w') for p in ['x']]\"",
    "cd /tmp; python3 -c \"import sys; sys.path.insert(0, '/x'); import json\"",
    "cd /tmp; python3 -c \"import factgate_hook as h; print(h._only_reads('ls'))\"",   # not ~/helix's hook
    "cd ~/helix; python3 -c \"import factgate_hook as h; h.CATCHES = '/tmp/x'\"",
    "cd ~/helix; python3 -c \"import factgate_hook as h; print(h._writes_facts.__globals__)\"",
    "cd /tmp; python3 /tmp/script.py",
    "cd /tmp; python3 -m json.tool x",
    "cd /tmp; cat x.py | python3 -",
]


@pytest.mark.parametrize("cmd", NOT_CLEARED_62, ids=[f"nc{i}" for i in range(len(NOT_CLEARED_62))])
def test_62b_shapes_that_are_never_cleared(real_paths, cmd):
    assert factgate_hook._py_parts(cmd, H55)["segs"] == {}, cmd


def test_62b_a_redirect_of_cleared_python_is_still_judged(real_paths):
    cmd = f"cd ~/helix; python3 -c \"print('x')\" > {H55}/app.py"
    assert factgate_hook._py_parts(cmd, H55)["segs"]
    assert rule_of(real_paths, cmd) == "P7"
