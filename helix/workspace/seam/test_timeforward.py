"""ORDER 56 (P-20261004-5) — the nightly's registry work, dry-run on the day after each due date.

timeforward.py copies a registry dir to a temp dir, points nightly's REG / REGATE / TODAY at
the copy, a regate shim and the simulated date, and calls the nightly's registry functions.
These tests build their registries from the pinned seam commit (as test_nightly_regate.py
does), never from the live one, and check that the live registry and its ledger do not move.

Run:  python3 -m pytest -q test_timeforward.py
"""
import sys

sys.dont_write_bytecode = True

import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIVE_REGISTRY = HERE / "registry"
LIVE_REG = LIVE_REGISTRY / "registry.json"
LIVE_LEDGER = LIVE_REGISTRY / "REGATE_LEDGER.jsonl"
REAL_REGATE = Path.home() / "helix/workspace/regate/regate.py"
NAMECHECK = Path.home() / "helix/build_orders/tools/namecheck.py"
REGISTRY_PIN = "71496aa"            # as test_nightly_regate.py: a commit, never a live path

sys.path.insert(0, str(HERE))
import nightly  # noqa: E402
import timeforward  # noqa: E402

BASELINE_SHIM = """import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("regate", {gate!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.REFUSALS_LOG = Path({log!r})
sys.exit(mod.main())
"""


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def live_registry_never_moves():
    before = (sha(LIVE_REG), sha(LIVE_LEDGER))
    yield
    assert (sha(LIVE_REG), sha(LIVE_LEDGER)) == before


def registry_dir(tmp_path, unscored_due):
    """A registry dir from the pinned commit, every prediction scored except those named in
    `unscored_due` ({id: due}), baselined by the real regate (its refusals to a temp log)."""
    d = tmp_path / "registry"
    d.mkdir()
    data = json.loads(subprocess.run(
        ["git", "-C", str(HERE), "show", f"{REGISTRY_PIN}:registry/registry.json"],
        capture_output=True, check=True).stdout)
    for entry in data.values():
        for p in entry.get("predictions", []):
            if p["id"] in unscored_due:
                p["due"] = unscored_due[p["id"]]
                for k in ("scored", "outcome", "brier", "note"):
                    p.pop(k, None)
            elif not p.get("scored"):
                p.update(scored="2026-09-22", outcome=None, brier=None, note="test fixture")
    (d / "registry.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    shim = tmp_path / "baseline_shim.py"
    shim.write_text(BASELINE_SHIM.format(gate=str(REAL_REGATE), log=str(tmp_path / "refusals.log")))
    r = subprocess.run([sys.executable, str(shim), "baseline", str(d / "registry.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return d


def fake_regate(tmp_path, body):
    f = tmp_path / "fake_regate" / "regate.py"
    f.parent.mkdir()
    f.write_text("import json, sys\n\ndef today():\n    return 'the real clock'\n\n" + body)
    return f


def test_one_prediction_due_2027_01_31_is_one_date_simulated_ok(tmp_path):
    reg = registry_dir(tmp_path, {"0004-p1": "2027-01-31"})
    before = sha(reg / "registry.json"), sha(reg / "REGATE_LEDGER.jsonl")
    results = timeforward.run(reg)
    assert [(r["due"], r["day"], r["status"]) for r in results] == \
        [("2027-01-31", "2027-02-01", "ok")], results
    assert timeforward.timeforward_line(reg) == \
        "- timeforward: 1 due date(s) simulated (next 2027-01-31) · 0 crashes"
    # the manual metric is reported overdue on the simulated day; the source dir did not move
    assert any("OVERDUE (manual): #0004 0004-p1" in l for l in results[0]["lines"]), results
    assert (sha(reg / "registry.json"), sha(reg / "REGATE_LEDGER.jsonl")) == before


def test_an_automatic_metric_is_scored_by_the_real_regate_on_the_copy_only(tmp_path):
    reg = registry_dir(tmp_path, {"0000-p1": "2027-01-31"})
    before = sha(reg / "registry.json"), sha(reg / "REGATE_LEDGER.jsonl")
    [res] = timeforward.run(reg)
    assert res["status"] == "ok", res
    # the real regate accepted the score on 2027-02-01 (S2 passed on the simulated date) …
    assert any(l.startswith("  - **#0000** 0000-p1: brier=None") for l in res["lines"]), res
    assert any(l.startswith("- registry: OK") for l in res["lines"]), res
    # … and wrote the temp copy, never the dir it was copied from
    assert (sha(reg / "registry.json"), sha(reg / "REGATE_LEDGER.jsonl")) == before


def test_positive_control_a_regate_that_raises_keyerror_brier_on_that_date(tmp_path, monkeypatch):
    reg = registry_dir(tmp_path, {"0004-p1": "2027-01-31"})
    fake = fake_regate(tmp_path, (
        "def main(argv=None):\n"
        "    if today() == '2027-02-01':\n"
        "        raise KeyError('brier')\n"
        "    print('1 prediction(s), 0 narrative(s)')\n"
        "    return 0\n"))
    monkeypatch.setattr(timeforward, "REGATE", fake)
    assert timeforward.timeforward_line(reg) == \
        "- 🔴 timeforward: 2027-01-31 crashes: KeyError 'brier'"
    [res] = timeforward.run(reg)
    assert res["frame"].startswith("regate.py:") and res["frame"].endswith("in main"), res
    # and through the nightly's own line, as the nightly will call it
    monkeypatch.setattr(nightly, "REG", reg / "registry.json")
    assert nightly.timeforward_line() == "- 🔴 timeforward: 2027-01-31 crashes: KeyError 'brier'"


def test_the_fake_regate_only_crashes_on_the_simulated_date(tmp_path, monkeypatch):
    reg = registry_dir(tmp_path, {"0004-p1": "2027-03-31"})
    fake = fake_regate(tmp_path, (
        "def main(argv=None):\n"
        "    if today() == '2027-02-01':\n"
        "        raise KeyError('brier')\n"
        "    print('1 prediction(s), 0 narrative(s)')\n"
        "    return 0\n"))
    monkeypatch.setattr(timeforward, "REGATE", fake)
    assert timeforward.timeforward_line(reg) == \
        "- timeforward: 1 due date(s) simulated (next 2027-03-31) · 0 crashes"


def test_an_in_process_crash_in_the_nightly_is_caught_with_its_last_frame(tmp_path, monkeypatch):
    """Catch #3's shape: a score written without a brier, and the nightly reads p['brier']."""
    reg = registry_dir(tmp_path, {"0000-p1": "2027-01-31"})
    fake = fake_regate(tmp_path, (
        "def main(argv=None):\n"
        "    argv = sys.argv[1:] if argv is None else argv\n"
        "    if argv[0] == 'score':\n"
        "        data = json.load(open(argv[1]))\n"
        "        for e in data.values():\n"
        "            for p in e['predictions']:\n"
        "                if p['id'] == argv[2]:\n"
        "                    p.update(scored=today(), outcome=None, note='n')\n"
        "        json.dump(data, open(argv[1], 'w'))\n"
        "    print('1 prediction(s), 0 narrative(s)')\n"
        "    return 0\n"))
    monkeypatch.setattr(timeforward, "REGATE", fake)
    [res] = timeforward.run(reg)
    assert (res["status"], res["error"]) == ("crash", "KeyError 'brier'"), res
    assert res["frame"].startswith("nightly.py:") and res["frame"].endswith("in summary_lines")
    assert timeforward.timeforward_line(reg) == \
        "- 🔴 timeforward: 2027-01-31 crashes: KeyError 'brier'"


def test_each_due_date_is_simulated_once_and_the_first_crash_is_named(tmp_path, monkeypatch):
    reg = registry_dir(tmp_path, {"0004-p1": "2027-01-31", "0000-p1": "2026-12-21",
                                  "0001-p1": "2026-12-21"})
    fake = fake_regate(tmp_path, (
        "def main(argv=None):\n"
        "    raise RuntimeError('boom ' + today())\n"))
    monkeypatch.setattr(timeforward, "REGATE", fake)
    results = timeforward.run(reg)
    assert [(r["due"], r["day"]) for r in results] == \
        [("2026-12-21", "2026-12-22"), ("2027-01-31", "2027-02-01")]
    assert timeforward.timeforward_line(reg) == \
        "- 🔴 timeforward: 2026-12-21 crashes: RuntimeError boom 2026-12-22 (+1 more)"


def test_no_unscored_predictions_is_zero_dates(tmp_path):
    reg = registry_dir(tmp_path, {})
    assert timeforward.run(reg) == []
    assert timeforward.timeforward_line(reg) == \
        "- timeforward: 0 due date(s) simulated (next —) · 0 crashes"


def test_the_nightly_constants_are_restored_even_after_a_crash(tmp_path, monkeypatch):
    reg = registry_dir(tmp_path, {"0000-p1": "2027-01-31"})
    was = (nightly.REG, nightly.REGATE, nightly.TODAY)

    def boom(rows, seen):
        assert nightly.REG != was[0] and nightly.TODAY == datetime.date(2027, 2, 1)
        raise ValueError("inside")
    monkeypatch.setattr(nightly, "score", boom)
    [res] = timeforward.run(reg)
    assert res["error"] == "ValueError inside", res
    assert (nightly.REG, nightly.REGATE, nightly.TODAY) == was


def test_the_simulated_regate_never_touches_the_live_refusals_logs(tmp_path):
    logs = [REAL_REGATE.parent / "refusals.log",
            Path.home() / "helix/workspace/sealgate/refusals.log"]
    before = [sha(p) if p.exists() else None for p in logs]
    # a registry whose ledger does not match it: the real regate REFUSES the score (S1)
    reg = registry_dir(tmp_path, {"0000-p1": "2027-01-31"})
    data = json.loads((reg / "registry.json").read_text())
    data["0000"]["predictions"][0]["p"] = 0.99
    (reg / "registry.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    [res] = timeforward.run(reg)
    assert res["status"] == "ok", res
    assert any("SCORE REFUSED #0000" in l for l in res["lines"]), res
    assert [sha(p) if p.exists() else None for p in logs] == before


def test_the_live_registry_dry_run_moves_nothing():
    due = sorted({p["due"] for e in json.loads(LIVE_REG.read_text()).values()
                  for p in e.get("predictions", []) if not p.get("scored")})
    results = timeforward.run(LIVE_REGISTRY)
    assert [r["due"] for r in results] == due
    # the autouse fixture checks registry.json and the ledger did not move


def test_the_nightly_line_never_stops_the_nightly(monkeypatch):
    monkeypatch.setattr(timeforward, "timeforward_line",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert nightly.timeforward_line() == \
        "- timeforward: 🔴 timeforward did not run (OSError: disk)"


def test_the_nightly_line_comes_right_after_the_proposals_line():
    src = Path(nightly.__file__).read_text()
    assert ("             proposals_line(),\n"
            "             timeforward_line(),\n") in src
    assert src.count("timeforward_line(),") == 1


def test_no_date_flag_env_var_or_hatch_in_timeforward_nightly_namecheck():
    for path in (HERE / "timeforward.py", HERE / "nightly.py", NAMECHECK):
        src = path.read_text()
        hits = [l for l in src.splitlines()
                if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
        assert hits == [], (path, hits)
        for forbidden in ("argparse", "os.environ", "getenv"):
            assert forbidden not in src, (path, forbidden)
    # the nightly still takes its date from the system clock, in one place
    src = (HERE / "nightly.py").read_text()
    assert re.findall(r"^TODAY = .*$", src, flags=re.M) == ["TODAY = datetime.date.today()"]
    assert "TODAY =" not in src.replace("TODAY = datetime.date.today()", "", 1)
