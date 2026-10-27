"""ORDER 56 — tests for build_orders/tools/namecheck.py, /restart refusing on its findings
(app.py `_do_restart`) and builder_run.sh recording it on the RUNS line.

Run with the venv python (app.py needs flask):
    .venv/bin/python -m pytest -q build_orders/tools/test_namecheck.py

namecheck finds the brain from its own path (tools/../..), so every fake-brain test runs a COPY
of the script inside the fake brain. The linter venv is the real one on the HDD; the one test
that needs it missing passes a missing path to main() in-process. Nothing in argv or the
environment points namecheck anywhere else.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
NAMECHECK = TOOLS / "namecheck.py"
ORDERS = TOOLS.parent
HELIX = ORDERS.parent
VENV = Path("__BULK__/venvs/lint")

sys.path.insert(0, str(HELIX))


def load_namecheck():
    spec = importlib.util.spec_from_file_location("namecheck", NAMECHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*files, script=NAMECHECK):
    return subprocess.run([sys.executable, str(script), *map(str, files)],
                          capture_output=True, text=True, timeout=60)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------- Part A: namecheck.py

def test_the_linter_venv_exists_and_is_pinned():
    assert (VENV / "bin" / "python").exists()
    req = (VENV / "requirements.txt").read_text().split()
    assert len(req) == 1 and re.fullmatch(r"pyflakes==\d+\.\d+\.\d+", req[0]), req
    assert load_namecheck().VENV == VENV


def test_an_undefined_name_exits_4_naming_it(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def remember():\n    log('x')\n")
    r = run(f)
    assert r.returncode == 4, (r.stdout, r.stderr)
    [line] = r.stdout.strip().splitlines()
    assert "undefined name 'log'" in line and str(f) in line and ":2:" in line


def test_an_undefined_local_and_a_syntax_error_are_findings(tmp_path):
    a = tmp_path / "local.py"
    a.write_text("x = 1\ndef f():\n    print(x)\n    x = 2\n    return x\n")
    b = tmp_path / "broken.py"
    b.write_text("def f(:\n")
    r = run(a, b)
    assert r.returncode == 4, (r.stdout, r.stderr)
    out = r.stdout.strip().splitlines()
    assert len(out) == 2, out
    assert any("referenced before assignment" in l and str(a) in l for l in out), out
    assert any(str(b) in l and "syntax" in l.lower() for l in out), out


def test_a_clean_file_exits_0(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("import os\n\ndef f():\n    return os.getcwd()\n")
    r = run(f)
    assert (r.returncode, r.stdout) == (0, ""), (r.stdout, r.stderr)


def test_an_unused_import_only_exits_0(tmp_path):
    f = tmp_path / "unused.py"
    f.write_text("import os\nimport sys\nx = 1\ndef g():\n    y = 2\n")
    r = run(f)
    assert (r.returncode, r.stdout) == (0, ""), (r.stdout, r.stderr)


def test_a_missing_venv_exits_2_and_says_unavailable(tmp_path, capsys):
    f = tmp_path / "bad.py"
    f.write_text("log()\n")
    nc = load_namecheck()
    assert nc.main([str(f)], venv=tmp_path / "no_such_venv") == 2
    out = capsys.readouterr().out
    assert out.startswith("namecheck unavailable: "), out


def test_a_venv_without_pyflakes_exits_2(tmp_path, capsys):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    # a python with no site-packages at all, so it cannot have pyflakes
    py = venv / "bin" / "python"
    py.write_text('#!/bin/sh\nexec /usr/bin/python3 -I -S "$@"\n')
    py.chmod(0o755)
    f = tmp_path / "bad.py"
    f.write_text("log()\n")
    nc = load_namecheck()
    assert nc.main([str(f)], venv=venv) == 2
    assert capsys.readouterr().out.startswith("namecheck unavailable: ")


def test_a_missing_file_exits_2(tmp_path):
    r = run(tmp_path / "nope.py")
    assert r.returncode == 2 and r.stdout.startswith("namecheck unavailable: "), r.stdout


def fake_brain(tmp_path, app_text, modules=None):
    brain = tmp_path / "helix"
    tools = brain / "build_orders" / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(NAMECHECK, tools / "namecheck.py")
    (brain / "app.py").write_text(app_text)
    for name, text in (modules or {}).items():
        (brain / name).write_text(text)
    return brain, tools / "namecheck.py"


def test_no_args_checks_app_py_and_the_brain_modules_it_imports(tmp_path):
    brain, script = fake_brain(
        tmp_path,
        "import os\nimport scheduler\nfrom frontdesk import add_note\n"
        "def f():\n    import factgate_hook\n    return factgate_hook, scheduler, add_note, os\n",
        {"scheduler.py": "x = 1\n", "frontdesk.py": "def add_note():\n    return undefined_a\n",
         "factgate_hook.py": "def h():\n    return undefined_b\n",
         "not_imported.py": "undefined_c\n"})
    nc_files = load_namecheck().startup_files(brain)
    assert [p.name for p in nc_files] == ["app.py", "factgate_hook.py", "frontdesk.py",
                                          "scheduler.py"]
    r = run(script=script)
    assert r.returncode == 4, (r.stdout, r.stderr)
    out = r.stdout
    assert "undefined_a" in out and "undefined_b" in out and "undefined_c" not in out


def test_no_args_on_a_clean_brain_exits_0(tmp_path):
    brain, script = fake_brain(tmp_path, "import scheduler\nprint(scheduler.x)\n",
                               {"scheduler.py": "x = 1\n"})
    r = run(script=script)
    assert (r.returncode, r.stdout) == (0, ""), (r.stdout, r.stderr)


def test_no_args_without_an_app_py_exits_2(tmp_path):
    brain, script = fake_brain(tmp_path, "")
    (brain / "app.py").unlink()
    r = run(script=script)
    assert r.returncode == 2 and r.stdout.startswith("namecheck unavailable: "), r.stdout


def test_the_live_app_py_is_clean_today():
    r = run()
    assert (r.returncode, r.stdout) == (0, ""), (r.stdout, r.stderr)


def test_namecheck_is_read_only(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("log()\n")
    before = sorted(p.name for p in tmp_path.iterdir()), sha(f)
    run(f)
    assert (sorted(p.name for p in tmp_path.iterdir()), sha(f)) == before


def test_no_escape_hatch_in_namecheck():
    hits = [l for l in NAMECHECK.read_text().splitlines()
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], hits
    assert "os.environ" not in NAMECHECK.read_text() and "getenv" not in NAMECHECK.read_text()


# ---------------------------------------------------------------- Part B: /restart

@pytest.fixture
def app_mod(monkeypatch):
    os.environ.setdefault("HELIX_NO_WARMUP", "1")
    os.environ.setdefault("HELIX_NO_PRECONNECT", "1")
    os.environ.setdefault("HELIX_NO_SCHEDULER", "1")
    import app
    monkeypatch.setattr(app, "_builder_active", lambda: (None, None))
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a))
    app._test_launched = launched
    return app


def test_restart_refuses_on_findings_and_never_launches(app_mod, monkeypatch):
    found = [f"/x/app.py:{n}:5: undefined name 'log{n}'" for n in range(1, 8)]
    monkeypatch.setattr(app_mod, "_namecheck", lambda: (4, found))
    msg = app_mod._do_restart()
    assert app_mod._test_launched == []
    assert msg.startswith("not restarting: undefined names would crash the new app.py:"), msg
    assert all(l in msg for l in found[:5]) and found[5] not in msg, msg


def test_restart_with_namecheck_unavailable_restarts_with_the_note(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_namecheck",
                        lambda: (2, ["namecheck unavailable: no linter at /nowhere/bin/python"]))
    msg = app_mod._do_restart()
    assert len(app_mod._test_launched) == 1
    assert msg.startswith("restarting…"), msg
    assert "(namecheck unavailable: no linter at /nowhere/bin/python)" in msg, msg


def test_restart_when_namecheck_times_out_restarts_with_the_note(app_mod, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setattr(subprocess, "run", slow)
    rc, out = app_mod._namecheck()
    assert rc == 2 and out[0].startswith("namecheck unavailable: timed out"), out
    msg = app_mod._do_restart()
    assert len(app_mod._test_launched) == 1
    assert "(namecheck unavailable: timed out" in msg, msg


def test_restart_when_clean_is_unchanged(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_namecheck", lambda: (0, []))
    msg = app_mod._do_restart()
    assert len(app_mod._test_launched) == 1
    assert msg == "restarting… the page reconnects on its own in a few seconds."


def test_namecheck_runs_after_the_builder_and_busy_checks(app_mod, monkeypatch):
    def never(*a, **k):
        raise AssertionError("namecheck ran before the busy check")
    monkeypatch.setattr(app_mod, "_namecheck", never)
    app_mod.SCHED.lock.acquire()
    try:
        msg = app_mod._do_restart()
    finally:
        app_mod.SCHED.lock.release()
    assert msg.startswith("not restarting: a turn is in flight"), msg
    monkeypatch.setattr(app_mod, "_builder_active", lambda: (123, "u"))
    assert app_mod._do_restart().startswith("not restarting: a builder is active")
    assert app_mod._test_launched == []


def test_the_real_namecheck_call_is_namecheck_py_with_no_args(app_mod, monkeypatch):
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **k):
        seen.append((cmd, k.get("timeout")))
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert app_mod._namecheck() == (0, [])
    [(cmd, timeout)] = seen
    assert cmd[1:] == [str(NAMECHECK)] and timeout == 30, (cmd, timeout)


# ---------------------------------------------------------------- Part C: builder_run.sh

from test_build_queue import BR, FAKE_CLI  # noqa: E402  — the order-26 scratch-HOME harness

UNDEFINED_CLI = FAKE_CLI.replace(
    'set -e\n', 'set -e\nprintf "def remember():\\n    log(1)\\n" >> "$HOME/helix/app.py"\n', 1)


@pytest.fixture
def br(tmp_path):
    return BR(tmp_path)


def with_namecheck(br, app_text="import os\nprint(os.sep)\n"):
    tools = br.B / "tools"
    tools.mkdir()
    shutil.copy2(NAMECHECK, tools / "namecheck.py")
    (br.brain / "app.py").write_text(app_text)


def test_builder_run_with_an_undefined_name_is_flagged(br):
    with_namecheck(br)
    cli = br.home / ".local" / "bin" / "claude"
    cli.write_text(UNDEFINED_CLI)
    br.run()
    [rec] = br.runs()
    assert "undefined name 'log'" in rec["namecheck"], rec
    assert rec["report_check"].startswith("FLAGGED: namecheck: "), rec
    assert "undefined name 'log'" in rec["report_check"], rec
    [note] = br.notes()
    assert "⚠ REPORT FLAGGED: namecheck: " in note, note


def test_builder_run_clean_records_namecheck_clean(br):
    with_namecheck(br)
    br.run()
    [rec] = br.runs()
    assert rec["namecheck"] == "clean", rec
    assert rec["report_check"] == "VERIFIED", rec


def test_builder_run_without_namecheck_is_unavailable_and_not_flagged(br):
    br.run()                       # the harness's scratch brain has no namecheck.py, no app.py
    [rec] = br.runs()
    assert rec["namecheck"].startswith("unavailable: "), rec
    assert rec["report_check"] == "VERIFIED", rec


def test_builder_run_a_flagged_report_stays_flagged_with_namecheck_findings(br):
    with_namecheck(br)
    (br.home / ".local" / "bin" / "claude").write_text(UNDEFINED_CLI)
    br.run(FAKE_LIE="1")
    [rec] = br.runs()
    assert rec["report_check"].startswith("FLAGGED: namecheck: "), rec
    assert "(report check: FLAGGED)" in rec["report_check"], rec
