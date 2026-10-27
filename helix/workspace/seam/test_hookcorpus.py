"""ORDER 63 (P-20261004-10) — hookcorpus.py: the hook's corpus of logged catches, run on a schedule
and after every build that changes the hook.

Nothing here writes the live catches log, the live hook or the live HOOKCORPUS.jsonl: every run
appends to a temp ledger, the fake pytests are shell scripts in a temp brain, and the positive
control runs on a temp copy of the hook and its test file. An autouse fixture checks the sha256 of
the live hook and the live catches log before and after every test.

Run:  python3 -m pytest -q test_hookcorpus.py
"""

import sys

sys.dont_write_bytecode = True

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hookcorpus  # noqa: E402

LIVE_BRAIN = Path.home() / "helix"
LIVE_HOOK = LIVE_BRAIN / "factgate_hook.py"
LIVE_HOOK_TESTS = LIVE_BRAIN / "test_factgate_hook.py"
LIVE_CATCHES = LIVE_BRAIN / "workspace/catches/CATCHES.jsonl"
LIVE_LEDGER = HERE / "HOOKCORPUS.jsonl"
LIVE_VENV_PY = LIVE_BRAIN / ".venv/bin/python"
LIVE_HOOKCORPUS = HERE / "hookcorpus.py"


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


@pytest.fixture(autouse=True)
def live_untouched():
    before = [sha(p) for p in (LIVE_HOOK, LIVE_HOOK_TESTS, LIVE_CATCHES, LIVE_LEDGER)]
    yield
    assert [sha(p) for p in (LIVE_HOOK, LIVE_HOOK_TESTS, LIVE_CATCHES, LIVE_LEDGER)] == before


def fake_brain(tmp_path, out, rc=0, sleep=0):
    """A temp brain whose .venv/bin/python is a script printing `out` and exiting `rc`."""
    b = tmp_path / "brain"
    (b / ".venv/bin").mkdir(parents=True)
    (b / "factgate_hook.py").write_text("# a hook\n")
    py = b / ".venv/bin/python"
    py.write_text(f"#!/usr/bin/env bash\n{'sleep %s' % sleep if sleep else ''}\n"
                  f"cat <<'EOF'\n{out}\nEOF\nexit {rc}\n")
    py.chmod(0o755)
    return b


HELD_OUT = ".........................x..........                                     [100%]\n" \
           "35 passed, 816 deselected, 1 xfailed in 0.56s"
CHANGED_OUT = (
    "....F....F.......\n"
    "=========================== short test summary info ============================\n"
    "FAILED test_factgate_hook.py::test_55_every_logged_catch_keeps_its_verdict[catch32] - "
    "AssertionError: catch #32 was ALLOWED\n"
    "FAILED test_factgate_hook.py::test_55_every_logged_catch_keeps_its_verdict[catch37] - "
    "AssertionError: catch #37 was ALLOWED\n"
    "2 failed, 33 passed, 816 deselected in 0.61s")
ID32 = "test_factgate_hook.py::test_55_every_logged_catch_keeps_its_verdict[catch32]"


# ---------------------------------------------------------------- the summary line

def test_parse_a_held_summary():
    r = hookcorpus.parse(HELD_OUT)
    assert (r["passed"], r["failed"], r["xfailed"], r["errors"]) == (35, 0, 1, 0)
    assert r["first_failure"] is None


def test_parse_a_changed_summary_names_the_first_failure():
    r = hookcorpus.parse(CHANGED_OUT)
    assert (r["passed"], r["failed"], r["xfailed"], r["errors"]) == (33, 2, 0, 0)
    assert r["first_failure"] == ID32


def test_parse_a_collection_error_counts_as_an_error():
    r = hookcorpus.parse("ERROR test_factgate_hook.py - SyntaxError: invalid syntax\n"
                         "1 error in 0.12s")
    assert r["errors"] == 1 and r["first_failure"] == "test_factgate_hook.py"


def test_parse_no_summary_is_none():
    assert hookcorpus.parse("ERROR: file or directory not found: test_factgate_hook.py\n") is None


# ---------------------------------------------------------------- run() and line() on a fake pytest

def test_held_line_and_record(tmp_path):
    b = fake_brain(tmp_path, HELD_OUT)
    ledger = tmp_path / "L.jsonl"
    res = hookcorpus.run(brain=b, ledger=ledger)
    h = sha(b / "factgate_hook.py")
    assert hookcorpus.line(res) == f"- hookcorpus: 36/36 verdicts held · hook {h[:12]}"
    [rec] = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert rec["hook_sha256"] == h and rec["status"] == "held"
    assert (rec["passed"], rec["failed"], rec["xfailed"], rec["errors"]) == (35, 0, 1, 0)
    assert rec["first_failure"] is None and rec["ts"].endswith("Z")


def test_changed_line_names_the_first_failure(tmp_path):
    b = fake_brain(tmp_path, CHANGED_OUT, rc=1)
    ledger = tmp_path / "L.jsonl"
    res = hookcorpus.run(brain=b, ledger=ledger)
    assert hookcorpus.line(res) == f"- 🔴 hookcorpus: 2 verdict(s) changed: {ID32}"
    [rec] = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert rec["status"] == "changed" and rec["failed"] == 2 and rec["first_failure"] == ID32


def test_no_venv_is_not_run(tmp_path):
    b = tmp_path / "brain"
    b.mkdir()
    res = hookcorpus.run(brain=b, ledger=tmp_path / "L.jsonl")
    assert res["status"] == "not run"
    assert hookcorpus.line(res) == f"- hookcorpus: not run (no {b}/.venv/bin/python)"


def test_a_timeout_is_not_run(tmp_path):
    b = fake_brain(tmp_path, HELD_OUT, sleep=5)
    res = hookcorpus.run(brain=b, ledger=tmp_path / "L.jsonl", timeout=0.5)
    assert hookcorpus.line(res) == "- hookcorpus: not run (no result after 0.5 s)"


def test_no_summary_is_not_run(tmp_path):
    b = fake_brain(tmp_path, "ERROR: file or directory not found: test_factgate_hook.py", rc=4)
    res = hookcorpus.run(brain=b, ledger=tmp_path / "L.jsonl")
    assert hookcorpus.line(res) == "- hookcorpus: not run (no pytest summary, exit 4)"


def test_nothing_selected_is_not_run(tmp_path):
    b = fake_brain(tmp_path, "816 deselected in 0.40s", rc=5)
    res = hookcorpus.run(brain=b, ledger=tmp_path / "L.jsonl")
    assert hookcorpus.line(res) == "- hookcorpus: not run (no corpus test ran, exit 5)"


def test_line_never_raises(monkeypatch, tmp_path):
    def boom(**kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(hookcorpus, "run", boom)
    assert hookcorpus.line() == "- hookcorpus: not run (RuntimeError: kaboom)"


def test_an_unwritable_ledger_does_not_stop_the_line(tmp_path):
    b = fake_brain(tmp_path, HELD_OUT)
    res = hookcorpus.run(brain=b, ledger=tmp_path / "no" / "such" / "dir" / "L.jsonl")
    assert hookcorpus.line(res).startswith("- hookcorpus: 36/36 verdicts held")


def test_the_command_is_the_orders(tmp_path):
    b = fake_brain(tmp_path, HELD_OUT)
    res = hookcorpus.run(brain=b, ledger=tmp_path / "L.jsonl")
    assert res["argv"] == [str(b / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "test_factgate_hook.py", "-k", "test_55_every or test_55_the_corpus"]
    assert hookcorpus.TIMEOUT == 300
    assert hookcorpus.LEDGER == LIVE_LEDGER and hookcorpus.LEDGER.name != "CATCHES.jsonl"


# ---------------------------------------------------------------- the CLI

def cli(*args):
    return subprocess.run([sys.executable, str(LIVE_HOOKCORPUS), *args],
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("out,rc,want", [(HELD_OUT, 0, 0), (CHANGED_OUT, 1, 4)])
def test_cli_exit_codes(tmp_path, out, rc, want):
    b = fake_brain(tmp_path, out, rc=rc)
    p = cli("--brain", str(b), "--ledger", str(tmp_path / "L.jsonl"))
    assert p.returncode == want, p
    assert p.stdout.strip() == hookcorpus.line(hookcorpus.run(brain=b, ledger=tmp_path / "L2.jsonl"))


def test_cli_not_run_is_exit_2(tmp_path):
    (tmp_path / "brain").mkdir()
    p = cli("--brain", str(tmp_path / "brain"), "--ledger", str(tmp_path / "L.jsonl"))
    assert p.returncode == 2 and p.stdout.startswith("- hookcorpus: not run (no ")


# ---------------------------------------------------------------- the real corpus

def copy_brain(tmp_path, drop_app_py):
    """A temp copy of the live hook and its test file. BRAIN is pinned to the live brain (the corpus
    names ~/helix paths); with drop_app_py, app.py is removed from the hook's guarded roots."""
    b = tmp_path / "copy"
    b.mkdir()
    src = LIVE_HOOK.read_text()
    pin = 'BRAIN = os.path.dirname(os.path.abspath(__file__))'
    assert src.count(pin) == 1
    src = src.replace(pin, f"BRAIN = {str(LIVE_BRAIN)!r}")
    if drop_app_py:
        root = '_P7_FILES = (f"{BRAIN}/app.py", '
        assert src.count(root) == 1
        src = src.replace(root, "_P7_FILES = (")
    (b / "factgate_hook.py").write_text(src)
    shutil.copy2(LIVE_HOOK_TESTS, b / "test_factgate_hook.py")
    return b


def test_the_live_corpus_holds_on_a_temp_ledger(tmp_path):
    res = hookcorpus.run(ledger=tmp_path / "L.jsonl")
    assert res["status"] == "held", res
    assert res["failed"] == res["errors"] == 0 and res["passed"] >= 35
    assert hookcorpus.line(res).endswith(f"verdicts held · hook {sha(LIVE_HOOK)[:12]}")


def test_negative_control_the_pinned_copy_holds(tmp_path):
    b = copy_brain(tmp_path, drop_app_py=False)
    res = hookcorpus.run(brain=b, python=LIVE_VENV_PY, ledger=tmp_path / "L.jsonl")
    assert res["status"] == "held", res


def test_positive_control_a_dropped_guarded_root_changes_deny_rows(tmp_path):
    b = copy_brain(tmp_path, drop_app_py=True)
    ledger = tmp_path / "L.jsonl"
    p = subprocess.run([sys.executable, str(LIVE_HOOKCORPUS), "--brain", str(b),
                        "--python", str(LIVE_VENV_PY), "--ledger", str(ledger)],
                       capture_output=True, text=True, timeout=400)
    assert p.returncode == 4, p
    assert p.stdout.startswith("- 🔴 hookcorpus: "), p.stdout
    [rec] = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert rec["status"] == "changed" and rec["failed"] >= 1
    # the rows that changed are deny rows about app.py (#32, #37, #68 in the corpus)
    assert rec["first_failure"].startswith("test_factgate_hook.py::test_55_every_logged_catch_keeps_its_verdict[catch")
    n = int(rec["first_failure"].rsplit("catch", 1)[1].rstrip("]"))
    assert n in (32, 37, 68), rec


# ---------------------------------------------------------------- builder_run.sh (Part C)
# test_build_queue.py's scratch-HOME harness (its BR class), imported, not edited: the live
# builder_run.sh runs in a scratch HOME whose fake claude can change a scratch factgate_hook.py.

def _bq():
    spec = importlib.util.spec_from_file_location("bq_harness_63", LIVE_BRAIN / "test_build_queue.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def br(tmp_path):
    bq = _bq()
    br = bq.BR(tmp_path)
    (br.brain / "factgate_hook.py").write_text("# scratch hook\n")
    seam = br.brain / "workspace" / "seam"
    seam.mkdir(parents=True)
    shutil.copy2(LIVE_HOOKCORPUS, seam / "hookcorpus.py")
    cli_ = br.home / ".local" / "bin" / "claude"
    cli_.write_text(bq.FAKE_CLI.replace(
        "set -e\n",
        "set -e\n[ -n \"${FAKE_HOOK_CHANGE:-}\" ] && echo '# changed' >> \"$HOME/helix/factgate_hook.py\"\n", 1))
    return br


def test_builder_run_unchanged_hook(br):
    br.run()
    [rec] = br.runs()
    assert rec["hookcorpus"] == "hook unchanged"
    assert rec["report_check"] == "VERIFIED", rec


def test_builder_run_changed_hook_without_a_venv_is_not_run_and_flags_nothing(br):
    br.run(FAKE_HOOK_CHANGE="1")
    [rec] = br.runs()
    assert rec["hookcorpus"] == f"- hookcorpus: not run (no {br.brain}/.venv/bin/python)", rec
    assert rec["report_check"] == "VERIFIED", rec


def test_builder_run_changed_hook_with_changed_verdicts_flags_the_report(br, tmp_path):
    py = br.brain / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{CHANGED_OUT}\nEOF\nexit 1\n")
    py.chmod(0o755)
    br.run(FAKE_HOOK_CHANGE="1")
    [rec] = br.runs()
    assert rec["hookcorpus"] == f"- 🔴 hookcorpus: 2 verdict(s) changed: {ID32}"
    assert rec["report_check"] == f"FLAGGED: hookcorpus: {ID32}"
    assert f"⚠ REPORT FLAGGED: hookcorpus: {ID32}" in br.notes()[0]
    # its record went to the scratch brain's ledger
    assert (br.brain / "workspace/seam/HOOKCORPUS.jsonl").exists()


def test_builder_run_changed_hook_held_flags_nothing(br):
    py = br.brain / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{HELD_OUT}\nEOF\n")
    py.chmod(0o755)
    br.run(FAKE_HOOK_CHANGE="1")
    [rec] = br.runs()
    assert rec["hookcorpus"].startswith("- hookcorpus: 36/36 verdicts held · hook ")
    assert rec["report_check"] == "VERIFIED", rec


def test_builder_run_with_no_hookcorpus_script_still_writes_the_line(br):
    (br.brain / "workspace/seam/hookcorpus.py").unlink()
    br.run(FAKE_HOOK_CHANGE="1")
    [rec] = br.runs()
    assert rec["hookcorpus"].startswith("- hookcorpus: not run ("), rec
    assert rec["report_check"] == "VERIFIED", rec
