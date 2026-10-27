"""ORDER 61b — tests for build_orders/tools/ordertests.py (P-7's (b): the suites are green BEFORE a build).

Like orderlint, ordertests finds ~/helix from its own path, so every test puts COPIES of both tools
inside a fake brain and runs those copies.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
RULE_TEXT = "The report's very LAST line must be exactly `DONE` or exactly `STOP`.\n"


@pytest.fixture
def brain(tmp_path):
    H = tmp_path / "helix"
    (H / "build_orders" / "tools").mkdir(parents=True)
    for name in ("orderlint.py", "ordertests.py"):
        shutil.copy2(TOOLS / name, H / "build_orders" / "tools" / name)
    (H / "app.py").write_text("import json\n")
    for d, ok in (("green", True), ("red", False)):
        (H / d).mkdir()
        (H / d / f"{d}mod.py").write_text("X = 1\n")
        (H / d / f"test_{d}mod.py").write_text(
            f"# names {d}mod.py\nimport {d}mod as mod\n\ndef test_one():\n    assert mod.X == 1\n\n"
            f"def test_two():\n    assert {ok}\n")
    (H / "green" / "test_unrelated.py").write_text("def test_never_collected():\n    assert False\n")
    return H


def order(edits):
    return ("# order\n\n## ORDER HEADER\n"
            f"EDITS: {edits}\nDOES NOT TOUCH: memory\nRESTART: no\n\n## Body\n" + RULE_TEXT)


def run(H, text=None, argv=None):
    if argv is None:
        p = H / "ORDER_X.md"
        p.write_text(text)
        argv = [str(p)]
    return subprocess.run([sys.executable, str(H / "build_orders" / "tools" / "ordertests.py"), *argv],
                          capture_output=True, text=True, timeout=300)


def test_one_red_dir_exits_4_naming_it(brain):
    r = run(brain, order("green/greenmod.py, red/redmod.py"))
    assert r.returncode == 4, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "green: 2 passed, 0 failed" in lines, lines
    assert "red: 1 passed, 1 failed" in lines, lines
    assert lines[-1] == "ordertests: RED in red", lines


def test_all_green_exits_0(brain):
    r = run(brain, order("green/greenmod.py"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == ["green: 2 passed, 0 failed", "ordertests: all green"], r.stdout


def test_errors_are_counted_and_red(brain):
    (brain / "green" / "test_broken.py").write_text("# names greenmod.py\nimport no_such_module\n")
    r = run(brain, order("green/greenmod.py"))
    assert r.returncode == 4, r.stdout
    assert "green: 0 passed, 0 failed, 1 errors" in r.stdout.splitlines(), r.stdout


def test_no_tests_name_the_edits(brain):
    (brain / "lonely.py").write_text("x = 1\n")
    r = run(brain, order("lonely.py"))
    assert r.returncode == 0 and r.stdout.splitlines()[-1] == "ordertests: all green (no test files name an EDITS file)"


def test_the_brain_root_needs_its_venv(brain):
    (brain / "rootmod.py").write_text("X = 1\n")
    (brain / "test_rootmod.py").write_text("# rootmod.py\ndef test_x():\n    assert True\n")
    r = run(brain, order("rootmod.py"))
    assert r.returncode == 4, r.stdout
    assert ".: cannot run, no .venv/bin/python" in r.stdout.splitlines(), r.stdout
    venv = brain / ".venv" / "bin"
    venv.mkdir(parents=True)
    os.symlink(sys.executable, venv / "python")
    r = run(brain, order("rootmod.py"))
    assert r.returncode == 0 and ".: 1 passed, 0 failed" in r.stdout.splitlines(), r.stdout


def test_usage_exit_2(brain):
    for argv in ([], ["a.md", "b.md"], ["--full", "a.md"], [str(brain / "missing.md")]):
        assert run(brain, argv=argv).returncode == 2, argv


def test_it_imports_the_lints_parser_and_does_not_copy_it():
    src = (TOOLS / "ordertests.py").read_text()
    assert re.search(r"^import orderlint$", src, re.M), "must import orderlint"
    assert "orderlint.check(" in src and "orderlint._test_files(" in src
    assert "def _header" not in src and "ORDER HEADER" not in src and "os.walk" not in src


def test_it_edits_no_file_outside_the_test_run(brain):
    text = order("green/greenmod.py, red/redmod.py")
    (brain / "ORDER_X.md").write_text(text)

    def snap():
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in brain.rglob("*")
                if "__pycache__" not in p.parts and ".pytest_cache" not in p.parts}
    before = snap()
    run(brain, argv=[str(brain / "ORDER_X.md")])
    assert snap() == before
    assert not list(brain.rglob(".pytest_cache"))
