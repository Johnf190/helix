"""ORDER 54c — tests for build_orders/tools/orderlint.py and run_order.sh's refusal 6.

The lint finds ~/helix from its own path (tools/../..), so every fake-brain test puts a COPY of
the script inside the fake brain and runs that copy. Nothing in argv or the environment points
it anywhere else.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
LINT = TOOLS / "orderlint.py"
ORDERS = TOOLS.parent
HELIX = ORDERS.parent
LIVE_RUN_ORDER = ORDERS / "run_order.sh"
LIVE_BUILDER_RUN = ORDERS / "builder_run.sh"
ORDER_54D = ORDERS / "ORDER_54d_orderlint_from_quarantine.md"   # ORDER 54d: was 54c

RULE_TEXT = "The report's very LAST line must be exactly `STOP` (the protocol also accepts `DONE`).\n"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------- a fake ~/helix

@pytest.fixture
def brain(tmp_path):
    """A fake ~/helix: app.py imports fakemod (a local module) and json (not local)."""
    H = tmp_path / "helix"
    (H / "build_orders" / "tools").mkdir(parents=True)
    shutil.copy2(LINT, H / "build_orders" / "tools" / "orderlint.py")
    (H / "app.py").write_text("import json\nimport fakemod\nfrom fakepkg import thing\n"
                              "def f():\n    import latemod\n")
    (H / "fakemod.py").write_text("X = 1\n")
    (H / "fakepkg.py").write_text("thing = 1\n")
    (H / "latemod.py").write_text("Y = 1\n")
    (H / "other.py").write_text("Z = 1\n")
    (H / "memory" / "facts").mkdir(parents=True)
    (H / "memory" / "facts" / "01_a.md").write_text("fact\n")
    (H / "workspace" / "thing").mkdir(parents=True)
    (H / "workspace" / "thing" / "a.py").write_text("a = 1\n")
    return H


def header(edits="other.py", dnt="memory", restart="no", fences=(), extra=""):
    lines = ["# order", "", "## ORDER HEADER", f"EDITS: {edits}", f"DOES NOT TOUCH: {dnt}"]
    lines += [f"FENCE: {p} sha256 {h}" for p, h in fences]
    lines += [f"RESTART: {restart}"] + ([extra] if extra else []) + ["", "## Body", RULE_TEXT]
    return "\n".join(lines) + "\n"


def lint(H, text=None, *args, path=None):
    if path is None:
        path = H / "ORDER_X.md"
        path.write_text(text)
    r = subprocess.run([sys.executable, str(H / "build_orders" / "tools" / "orderlint.py"),
                        str(path), *args], capture_output=True, text=True, timeout=60)
    return r


def hard(r):
    return [l for l in r.stdout.splitlines() if l.startswith("HARD: ")]


def test_good_minimal_order_is_clean(brain):
    r = lint(brain, header())
    assert r.returncode == 0 and hard(r) == [], r.stdout + r.stderr


# ---------------------------------------------------------------- rule 1: the header itself

@pytest.mark.parametrize("text,needle", [
    ("# order\n\n" + RULE_TEXT, "no '## ORDER HEADER'"),
    (header().replace("EDITS: other.py\n", ""), "missing EDITS:"),
    (header().replace("DOES NOT TOUCH: memory\n", ""), "missing DOES NOT TOUCH:"),
    (header().replace("RESTART: no\n", ""), "missing RESTART:"),
    (header(extra="OWNER: odin"), "unknown key 'OWNER'"),
    (header(extra="just words"), "malformed header line"),
    (header(extra="EDITS: other.py"), "duplicate key 'EDITS'"),            # correction 10
    (header(extra="RESTART: no"), "duplicate key 'RESTART'"),
    (header() + "\n## ORDER HEADER\nEDITS: other.py\n", "2 '## ORDER HEADER' sections"),  # correction 10
    (header(restart="maybe"), "RESTART must be yes or no"),
    (header(extra="FENCE: app.py sha256 abc"), "malformed FENCE line"),
    (header(edits="other.py, , fakemod.py"), "empty EDITS item"),
])
def test_rule_1_fires(brain, text, needle):
    r = lint(brain, text)
    assert r.returncode == 3, r.stdout
    assert any(needle in l and "rule 1" in l for l in hard(r)), r.stdout


# ---------------------------------------------------------------- rule 2: EDITS inside DOES NOT TOUCH

@pytest.mark.parametrize("edits,dnt", [("workspace/thing/a.py", "workspace/thing"),
                                       ("workspace/thing", "workspace/thing"),
                                       ("other.py", "memory, other.py")])
def test_rule_2_fires(brain, edits, dnt):
    r = lint(brain, header(edits=edits, dnt=dnt))
    assert r.returncode == 3 and any("rule 2" in l for l in hard(r)), r.stdout


def test_rule_2_is_component_wise_not_a_string_prefix(brain):
    (brain / "workspace" / "thing2").mkdir()
    (brain / "workspace" / "thing2" / "b.py").write_text("b = 1\n")
    r = lint(brain, header(edits="workspace/thing2/b.py", dnt="workspace/thing"))
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------- rule 3: existence

def test_rule_3_missing_path_not_marked_new(brain):
    r = lint(brain, header(edits="nope.py"))
    assert r.returncode == 3 and any("rule 3" in l and "nope.py" in l for l in hard(r)), r.stdout


def test_rule_3_new_path_that_already_exists(brain):
    r = lint(brain, header(edits="other.py (new)"))
    assert r.returncode == 3, r.stdout
    assert any("rule 3" in l and "(new)" in l and "already exists" in l for l in hard(r)), r.stdout


def test_rule_3_new_path_absent_is_clean(brain):
    r = lint(brain, header(edits="other.py, workspace/thing/new.py (new)"))
    assert r.returncode == 0, r.stdout


def test_paths_are_relative_to_the_lints_own_brain(brain, tmp_path):
    """A path that exists only in the REAL ~/helix does not exist in the fake brain."""
    assert (HELIX / "factgate_hook.py").exists()
    r = lint(brain, header(edits="factgate_hook.py"))
    assert r.returncode == 3 and any("rule 3" in l for l in hard(r)), r.stdout


# ---------------------------------------------------------------- rule 4: FENCE, now

def test_rule_4_fence_matching_now_is_clean_one_byte_later_hard(brain):
    f = brain / "workspace" / "thing" / "a.py"
    text = header(fences=[("workspace/thing/a.py", sha(f))])
    assert lint(brain, text).returncode == 0
    f.write_text("a = 2\n")                                        # one byte
    r = lint(brain, text)
    assert r.returncode == 3 and any("rule 4" in l and "differs" in l for l in hard(r)), r.stdout


def test_rule_4_fence_on_a_missing_file(brain):
    r = lint(brain, header(fences=[("gone.py", "0" * 64)]))
    assert r.returncode == 3 and any("rule 4" in l and "missing" in l for l in hard(r)), r.stdout


def test_rule_4_two_fences_both_checked(brain):
    a = brain / "workspace" / "thing" / "a.py"
    r = lint(brain, header(fences=[("workspace/thing/a.py", sha(a)), ("other.py", "1" * 64)]))
    assert r.returncode == 3 and len([l for l in hard(r) if "rule 4" in l]) == 1, r.stdout


# ---------------------------------------------------------------- rule 5: RESTART

@pytest.mark.parametrize("edits", ["app.py", "fakemod.py", "fakepkg.py", "latemod.py"])
def test_rule_5_restart_no_but_edits_a_start_up_module(brain, edits):
    r = lint(brain, header(edits=edits, restart="no"))
    assert r.returncode == 3 and any("rule 5" in l for l in hard(r)), r.stdout


def test_rule_5_restart_no_with_a_module_app_does_not_import_is_clean(brain):
    assert lint(brain, header(edits="other.py", restart="no")).returncode == 0


def test_rule_5_imports_are_read_from_app_py_at_lint_time(brain):
    (brain / "app.py").write_text("import other\n")
    r = lint(brain, header(edits="other.py", restart="no"))
    assert r.returncode == 3 and any("rule 5" in l for l in hard(r)), r.stdout
    assert lint(brain, header(edits="fakemod.py", restart="no")).returncode == 0


def test_rule_5_restart_yes_without_ready_for_restart(brain):
    r = lint(brain, header(edits="fakemod.py", restart="yes"))
    assert r.returncode == 3 and any("rule 5" in l and "READY FOR /restart" in l
                                     for l in hard(r)), r.stdout
    assert lint(brain, header(edits="fakemod.py", restart="yes") + "READY FOR /restart\n").returncode == 0


# ---------------------------------------------------------------- rule 6: the last-line rule

def test_rule_6_fires_without_the_rule(brain):
    r = lint(brain, header().replace(RULE_TEXT, "Report to reports/.\n"))
    assert r.returncode == 3 and any("rule 6" in l for l in hard(r)), r.stdout


def test_rule_6_last_line_is_case_insensitive_done_is_not(brain):     # correction 8
    for spelling in ("very LAST line", "the last line", "Last Line"):
        assert lint(brain, header().replace(RULE_TEXT, f"{spelling} is DONE\n")).returncode == 0
    r = lint(brain, header().replace(RULE_TEXT, "the last line is done\n"))
    assert r.returncode == 3 and any("rule 6" in l for l in hard(r)), r.stdout


# ---------------------------------------------------------------- memory/facts

@pytest.mark.parametrize("edits", ["memory/facts/01_a.md", "memory/facts/02_b.md (new)", "memory/facts"])
def test_memory_facts_in_edits_is_hard_in_both_modes(brain, edits):
    for args in ((), ("--full",)):
        r = lint(brain, header(edits=edits, dnt="workspace/thing"), *args)
        assert r.returncode == 3 and any("memory/facts" in l for l in hard(r)), r.stdout


# ---------------------------------------------------------------- modes, output, exit codes

def test_json_output(brain):
    r = lint(brain, header(edits="nope.py"), "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 3 and set(out) == {"hard", "notes"} and out["hard"], out


def test_usage_exit_2(brain):
    t = brain / "build_orders" / "tools" / "orderlint.py"
    for argv in ([], ["--nope", "x.md"], [str(brain / "missing.md")], ["a.md", "b.md"]):
        r = subprocess.run([sys.executable, str(t), *argv], capture_output=True, text=True)
        assert r.returncode == 2, (argv, r.stdout, r.stderr)


def test_flags_never_change_the_hard_findings(brain):
    text = header(edits="nope.py, other.py (new)", restart="maybe")
    base = hard(lint(brain, text))
    assert base and hard(lint(brain, text, "--full")) == base
    assert json.loads(lint(brain, text, "--json").stdout)["hard"] == [l[6:] for l in base]


def test_the_environment_changes_nothing(brain):
    text = header(edits="nope.py")
    p = brain / "ORDER_X.md"
    p.write_text(text)
    t = brain / "build_orders" / "tools" / "orderlint.py"
    env = {**os.environ, "HOME": "/nonexistent", "HELIX": str(HELIX), "ORDERLINT_ROOT": str(HELIX)}
    r = subprocess.run([sys.executable, str(t), str(p)], capture_output=True, text=True, env=env)
    assert r.returncode == 3 and hard(r) == hard(lint(brain, path=p)), r.stdout


def test_full_mode_lists_escape_hatch_and_count_pins_as_notes_only(brain):
    (brain / "test_fakemod.py").write_text(
        "import fakemod\n\ndef test_fakemod_has_no_escape_hatch():\n    pass\n\n"
        "def test_n():\n    assert open('fakemod.py').read().count('X') == 1\n")
    (brain / "test_other.py").write_text("def test_escape_hatch():\n    pass\n")       # names no EDITS file
    pin = "open('fakemod.py').read().count('X')\n"                  # names the file AND pins
    for d in ("quarantine", ".venv", "__pycache__", "node_modules", "workspace/thing/.git"):
        (brain / d).mkdir(parents=True, exist_ok=True)
        (brain / d / "test_hidden.py").write_text(pin)
    (brain / "test_b.bak.py").write_text(pin)
    text = header(edits="fakemod.py", restart="yes") + "READY FOR /restart\n"
    plain = lint(brain, text)
    assert plain.returncode == 0 and "NOTE:" not in plain.stdout, plain.stdout
    r = lint(brain, text, "--full")
    assert r.returncode == 0, r.stdout
    notes = [l for l in r.stdout.splitlines() if l.startswith("NOTE: ")]
    assert any("test_fakemod.py:3" in l and "escape_hatch" in l for l in notes), notes
    assert any("test_fakemod.py:7" in l and ".count(" in l for l in notes), notes
    assert not [l for l in notes if re.search(r"test_other|test_hidden|test_b", l)], notes


def test_the_lint_writes_nothing(brain):
    def snap():
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in brain.rglob("*")}
    (brain / "ORDER_X.md").write_text(header(edits="nope.py"))
    before = snap()
    for args in ((), ("--full",), ("--json",), ("--full", "--json")):
        lint(brain, path=brain / "ORDER_X.md", *args)
    assert snap() == before


def test_source_scan_no_escape_hatch_words_and_no_env_or_root_switch():
    src = LINT.read_text()
    assert not re.search(r"force|override|bypass|skip_check", src, re.I)
    assert "os.environ" not in src and "getenv" not in src and "expanduser" not in src
    assert "--root" not in src
    opts = set(re.findall(r"""["'](--[a-z-]+)["']""", src))
    assert opts == {"--full", "--json"}, opts


# ---------------------------------------------------------------- the real ~/helix (correction 1)

def test_positive_control_a_temp_order_naming_real_files_is_clean(tmp_path):
    """1(i): real ~/helix, a fence computed NOW, a (new) path that does not exist."""
    new = tmp_path / "does_not_exist_yet.py"
    text = header(edits=f"app.py, {new} (new)", dnt="memory", restart="yes",
                  fences=[("app.py", sha(HELIX / "app.py"))]) + "READY FOR /restart\n"
    p = tmp_path / "ORDER_T.md"
    p.write_text(text)
    r = subprocess.run([sys.executable, str(LINT), str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_landed_54d_lints_with_exactly_its_two_new_files(tmp_path):
    """1(ii) as rewritten by 54c correction 11 and 54d decision 13: a landed order no longer lints
    clean. The copy drops its FENCE: lines, so no live hash is pinned through the order file."""
    p = tmp_path / ORDER_54D.name
    lines = ORDER_54D.read_text().splitlines(keepends=True)
    p.write_text("".join(l for l in lines if not l.startswith("FENCE:")))
    r = subprocess.run([sys.executable, str(LINT), str(p)], capture_output=True, text=True)
    assert r.returncode == 3, r.stdout
    h = hard(r)
    assert len(h) == 2, h
    assert any("build_orders/tools/orderlint.py" in l and "(new)" in l and "already exists" in l for l in h), h
    assert any("build_orders/tools/test_orderlint.py" in l and "(new)" in l and "already exists" in l
               for l in h), h


# ---------------------------------------------------------------- run_order.sh refusal 6

@pytest.fixture
def runner(brain, tmp_path):
    B = brain / "build_orders"
    shutil.copy2(LIVE_RUN_ORDER, B / "run_order.sh")
    shutil.copy2(LIVE_BUILDER_RUN, B / "builder_run.sh")
    (B / "BUILD_PROTOCOL.md").write_text("protocol line\n" * 300)
    cli = tmp_path / ".local" / "bin" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)

    def run(nn):
        return subprocess.run(["bash", str(B / "run_order.sh"), "--dry-run", nn], capture_output=True,
                              text=True, timeout=60,
                              env={**os.environ, "HOME": str(tmp_path), "BUILD_FAKE_HHMM": "1200"})
    return B, run


def test_runner_refuses_a_bad_order(runner):
    B, run = runner
    (B / "ORDER_77_bad.md").write_text(header(edits="nope.py"))
    r = run("77")
    assert r.returncode != 0 and "DRY RUN" not in r.stdout, r.stdout
    assert r.stderr.startswith("refused: order lint — HARD: rule 3"), r.stderr
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr


def test_runner_dry_runs_a_good_order(runner):
    B, run = runner
    (B / "ORDER_78_good.md").write_text(header(edits="x.py (new)"))
    r = run("78")
    assert r.returncode == 0 and "DRY RUN" in r.stdout, r.stdout + r.stderr


def test_runner_refuses_when_the_lint_is_missing(runner):
    B, run = runner
    (B / "ORDER_78_good.md").write_text(header(edits="x.py (new)"))
    (B / "tools" / "orderlint.py").unlink()
    r = run("78")
    assert r.returncode != 0 and r.stderr.strip() == "refused: no orderlint.py", r.stderr


def test_runner_refusal_6_has_no_switch():
    src = LIVE_RUN_ORDER.read_text()
    block = src.split("# --- refusal 6")[1].split("# --- refusal 2")[0]
    assert "orderlint.py" in block and "$ORDER_FILE" in block
    assert not re.search(r"force|override|bypass|skip|\$\{?[A-Z_]*LINT[A-Z_]*:-", block, re.I), block
    assert src.index("# --- refusal 1") < src.index("# --- refusal 6") < src.index("# --- refusal 2")


# ---------------------------------------------------------------- ORDER 61b: the --full pre-flight notes

sys.path.insert(0, str(TOOLS))
import orderlint  # noqa: E402


def notes(r):
    return [l for l in r.stdout.splitlines() if l.startswith("NOTE: ")]


def lettered(r, letter):
    return [l for l in notes(r) if l.startswith(f"NOTE: ({letter}) ")]


@pytest.mark.parametrize("line,fires", [("before: master", True), ("Compare before and after against HEAD.", True),
                                        ("before: `other.py` as it is now", True),
                                        ("before: 62975d7", False), ("before: master at 62975d7", False),
                                        ("compare against master", False), ("nothing to see", False)])
def test_a_unpinned_before_reference(brain, line, fires):
    r = lint(brain, header() + line + "\n", "--full")
    a = lettered(r, "a")
    assert r.returncode == 0, r.stdout
    if fires:
        n = (header() + line).count("\n") + 1
        assert a == [f"NOTE: (a) line {n}: a 'before' reference is not pinned to a commit or sha256"], r.stdout
    else:
        assert a == [], r.stdout


def test_a_c_d_e_are_full_only_and_never_change_the_exit_code(brain):
    text = ("# WORK ORDER 99 — PROPOSAL P-20261004-7\n" + header().split("\n", 1)[1]
            + "before: master\n## Acceptance\n- `app.py` still loads\n## Tests\n- reads memory/sessions/log.md\n")
    plain = lint(brain, text)
    assert plain.returncode == 0 and "NOTE:" not in plain.stdout, plain.stdout
    full = lint(brain, text, "--full")
    assert full.returncode == 0, full.stdout
    for letter in "acde":
        assert lettered(full, letter), (letter, full.stdout)
    assert hard(full) == hard(plain) == []


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")


def test_c_proposal_status_from_the_ledger(tmp_path, monkeypatch):
    led = tmp_path / "PROPOSALS.jsonl"
    _ledger(led, [
        {"kind": "proposal", "id": "P-20261004-7", "status": "pending", "acceptance": "five more checks"},
        {"kind": "proposal", "id": "P-20261004-8", "status": "pending", "acceptance": "x"},
        {"kind": "proposal", "id": "P-20261004-4", "status": "pending", "acceptance": "y"},
        {"kind": "decision", "proposal": "P-20261004-7", "decision": "approve"},
        {"kind": "decision", "proposal": "P-20261004-4", "decision": "park"},
    ])
    monkeypatch.setattr(orderlint, "PROPOSALS", str(led))

    def c(first):
        return [n for n in orderlint.preflight_notes(first + "\nbody\n", []) if n.startswith("(c)")]
    assert c("# WORK ORDER 61 — PROPOSAL P-20261004-7: x") == [
        "(c) P-20261004-7 approved · acceptance: five more checks"]
    assert c("# WORK ORDER 63 — PROPOSAL P-20261004-8: x") == ["(c) P-20261004-8 is pending, not approved"]
    assert c("# WORK ORDER 64 — PROPOSAL P-20261004-4: x") == ["(c) P-20261004-4 is parked, not approved"]
    assert c("# WORK ORDER 65 — PROPOSAL P-20261004-99: x") == ["(c) P-20261004-99 is not in PROPOSALS.jsonl"]
    assert c("# WORK ORDER 66 — no proposal") == []
    assert c("# WORK ORDER 67\nP-20261004-7 on line two") == []                     # first line only
    monkeypatch.setattr(orderlint, "PROPOSALS", str(tmp_path / "gone.jsonl"))
    got = c("# WORK ORDER 61 — PROPOSAL P-20261004-7: x")
    assert len(got) == 1 and got[0].startswith("(c) P-20261004-7: cannot read "), got


def test_c_reads_the_ledger_beside_the_brain(brain):
    (brain / "workspace" / "proposals").mkdir(parents=True)
    _ledger(brain / "workspace" / "proposals" / "PROPOSALS.jsonl",
            [{"kind": "proposal", "id": "P-20261004-9", "status": "pending", "acceptance": "hook judges effects"},
             {"kind": "decision", "proposal": "P-20261004-9", "decision": "approve"}])
    r = lint(brain, "# WORK ORDER 62 — PROPOSAL P-20261004-9: x\n" + header().split("\n", 1)[1], "--full")
    assert lettered(r, "c") == ["NOTE: (c) P-20261004-9 approved · acceptance: hook judges effects"], r.stdout


def test_d_checked_path_outside_the_edits(brain):
    body = ("## Acceptance\n- `app.py` and other.py, and workspace/thing/a.py; nope.py; e.g. this.\n"
            "### detail\n- memory/facts/01_a.md\n"
            "## Notes\n- fakemod.py is not in a checked section\n"
            "## Odin's LIVE CHECKS after /restart\n- fakemod.py\n")
    r = lint(brain, header(edits="other.py, workspace/thing") + body, "--full")
    d = lettered(r, "d")
    assert r.returncode == 0, r.stdout
    assert {l.split(": ", 2)[2] for l in d} == {"app.py is checked but this order does not change it",
                                                "memory/facts/01_a.md is checked but this order does not change it",
                                                "fakemod.py is checked but this order does not change it"}, d
    assert len(d) == 3 and not [l for l in d if "other.py" in l or "workspace/thing" in l], d


def test_d_positive_control_fixture_on_the_real_brain(tmp_path):
    """61b correction 1: a fixture order, never a real order file."""
    assert (HELIX / "app.py").is_file() and (TOOLS / "orderlint.py").is_file()

    def run(checked):
        p = tmp_path / "ORDER_T.md"
        p.write_text(header(edits="build_orders/tools/orderlint.py", dnt="memory")
                     + f"## Odin's live checks after the build\na. `{checked}` still answers.\n")
        return subprocess.run([sys.executable, str(LINT), str(p), "--full"], capture_output=True, text=True)
    r = run("app.py")
    d = lettered(r, "d")
    print("\n".join(d))
    assert r.returncode == 0 and len(d) == 1 and "app.py is checked but this order does not change it" in d[0], r.stdout
    r = run("build_orders/tools/orderlint.py")
    assert r.returncode == 0 and lettered(r, "d") == [], r.stdout


def test_a_positive_control_fixture_on_the_real_brain(tmp_path):
    def run(line):
        p = tmp_path / "ORDER_T.md"
        p.write_text(header(edits="build_orders/tools/orderlint.py", dnt="memory") + line + "\n")
        return subprocess.run([sys.executable, str(LINT), str(p), "--full"], capture_output=True, text=True)
    a = lettered(run("before: master"), "a")
    print("\n".join(a))
    assert len(a) == 1, a
    assert lettered(run("before: 62975d7"), "a") == []


def test_e_a_test_in_edits_that_may_write_live_memory(brain):
    (brain / "test_w.py").write_text("def test_w():\n    x = 1\n"
                                     "    with open('memory/sessions/log.md', 'a') as f:\n        f.write('x')\n")
    (brain / "test_r.py").write_text("from pathlib import Path\n"
                                     "def test_r():\n    Path('memory/sessions/log.md').read_text()\n")
    (brain / "test_absent_not_in_edits.py").write_text("open('notepad.json', 'w')\n")
    r = lint(brain, header(edits="test_w.py, test_r.py, test_new.py (new)"), "--full")
    e = lettered(r, "e")
    assert r.returncode == 0, r.stdout
    assert e == ["NOTE: (e) test_w.py:3: a test may write live memory"], r.stdout


@pytest.mark.parametrize("word", ["notepad.json", "memory/facts", "memory/sessions", "sessions/log.md"])
def test_e_the_orders_tests_section_names_live_memory(brain, word):
    body = f"## Part A\n- {word} is mentioned here\n## Tests\n- a fixture that touches {word}\n## After\n- {word}\n"
    text = header() + body
    r = lint(brain, text, "--full")
    n = text.splitlines().index(f"- a fixture that touches {word}") + 1
    e = lettered(r, "e")
    assert len(e) == 1 and e[0].startswith(f"NOTE: (e) line {n}: "), r.stdout


def test_new_notes_keep_the_lint_read_only(brain):
    (brain / "workspace" / "proposals").mkdir(parents=True)
    _ledger(brain / "workspace" / "proposals" / "PROPOSALS.jsonl", [])
    (brain / "ORDER_X.md").write_text("# P-20261004-1\n" + header().split("\n", 1)[1] + "before: HEAD\n")

    def snap():
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in brain.rglob("*")}
    before = snap()
    for args in (("--full",), ("--full", "--json")):
        lint(brain, None, *args, path=brain / "ORDER_X.md")
    assert snap() == before
