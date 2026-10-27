#!/usr/bin/env python3
"""ORDER 76 Part B — /remember writes a `Category:` line, so factgate's F5 records the note.

FACTS and factgate's state are pointed at temp dirs (a fresh `baseline` there). Nothing here
reads or writes the live memory/facts or workspace/factgate/state. The gate itself is the live
workspace/factgate/factgate.py with the live CATEGORIES.txt.

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_remember_category.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402
import factgate_hook  # noqa: E402

FACTGATE_REAL = HELIX / "workspace/factgate/factgate.py"
CATEGORIES_PY = HELIX / "workspace/categories/categories.py"


def _categories():
    import importlib.util
    spec = importlib.util.spec_from_file_location("t76_categories", CATEGORIES_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _categories()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    facts, state = tmp_path / "facts", tmp_path / "state"
    facts.mkdir()
    state.mkdir()
    (facts / "001_old.md").write_text("# old\nCategory: helix\n\nan old fact.\n")
    monkeypatch.setattr(app, "FACTS", str(facts))
    monkeypatch.setattr(factgate_hook, "FACTS", str(facts))
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(FACTGATE_REAL))
    monkeypatch.setattr(factgate_hook, "FACTGATE_STATE", str(state))
    assert subprocess.run([sys.executable, str(FACTGATE_REAL), "--state-dir", str(state),
                           "baseline", str(facts)], capture_output=True).returncode == 0
    return {"facts": facts, "state": state, "client": app.app.test_client()}


def remember(rig, text):
    r = rig["client"].post("/remember", json={"text": text})
    body = r.get_json()
    assert body["ok"] is True, body
    return rig["facts"] / body["file"]


def ledger(rig):
    return [json.loads(l) for l in (rig["state"] / "FACTS_LEDGER.jsonl").read_text().splitlines()]


def test_the_written_line_parses_with_declared_all(rig):
    p = remember(rig, "the boat frames are printed; glue the boat tomorrow")
    text = p.read_text()
    lines = text.splitlines()
    assert lines[:2] == ["---", f"saved: {lines[1][7:]}"] and lines[2:4] == ["status: unverified", "---"]
    assert lines[4].startswith("Category: ")
    names, problem = C.declared_all(text, C.load())
    assert problem is None and names == [lines[4][len("Category: "):]]
    assert lines[5] == "the boat frames are printed; glue the boat tomorrow"


def test_the_guess_is_propose_backfills(rig):
    p = remember(rig, "boat frames printed")
    assert p.read_text().splitlines()[4] == "Category: project: boats"


def test_a_guess_that_raises_writes_personal(rig, monkeypatch, tmp_path):
    bad = tmp_path / "bad_guess.py"
    bad.write_text("def guess(*a, **k):\n    raise RuntimeError('boom')\n")
    monkeypatch.setattr(app, "FACT_GUESS_PY", str(bad))
    p = remember(rig, "boat frames printed")
    assert p.read_text().splitlines()[4] == "Category: personal"


def test_a_guess_of_nothing_writes_personal(rig, monkeypatch, tmp_path):
    empty = tmp_path / "empty_guess.py"
    empty.write_text("class categories:\n    load = staticmethod(lambda: [])\n"
                     "def guess(*a, **k):\n    return ('', '', 'low')\n")
    monkeypatch.setattr(app, "FACT_GUESS_PY", str(empty))
    p = remember(rig, "boat frames printed")
    assert p.read_text().splitlines()[4] == "Category: personal"


def test_a_missing_guesser_writes_personal(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "FACT_GUESS_PY", str(tmp_path / "gone.py"))
    p = remember(rig, "a note about nothing in particular")
    assert p.read_text().splitlines()[4] == "Category: personal"


def test_remember_leaves_sys_path_as_it_was(rig):
    before = list(sys.path)
    remember(rig, "boat frames printed")
    assert sys.path == before


def test_factgate_records_the_note(rig, capsys):
    before = len(ledger(rig))
    p = remember(rig, "the kids start school on monday")
    assert "NOT" not in capsys.readouterr().out
    after = ledger(rig)
    assert len(after) == before + 1
    assert after[-1]["file"] == p.name and after[-1]["kind"] == "record"


def test_the_gate_used_here_does_refuse_a_note_with_no_line(rig):
    # control: the same gate refuses a new fact without the line, so the test above means F5 passed
    (rig["facts"] / "20991231-no-line-000000.md").write_text(
        "---\nsaved: 2099-12-31\nstatus: unverified\n---\nno category here\n")
    ok, detail = factgate_hook.record_remember("20991231-no-line-000000.md")
    assert ok is False and "REFUSED" in detail
