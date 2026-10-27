"""Tests for the correction-rate metric (order 37, item 18).

Every test but one works on a temp Helix built from fixtures. The one exception is
`test_the_markers_and_the_classification_match_app_on_every_live_fact`, which imports the live
app.py in a SUBPROCESS with order 32's env guards (no warm-up, no pre-connect, no scheduler), so
nothing app starts can outlive the test.
"""

import sys

sys.dont_write_bytecode = True

import hashlib
import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import correction_rate as cr  # noqa: E402

LIVE = Path.home() / "helix"

APP = '''
_CORRECTION_MARKERS = (
    "⚠️ update", "correction (",
    "superseded",
    "was wrong",
)


def _fact_has_correction(text):
    low = (text or "").lower()
    return any(m in low for m in _CORRECTION_MARKERS)
'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def lines(p, rows):
    write(p, "".join(json.dumps(r) + "\n" for r in rows))


def make_helix(tmp, facts=None, ledger=None, catches=None, registry=None, regate_log="",
               sealgate_log="", runs=None, log="", archive=None, app=APP):
    """A temp Helix. `ledger` rows may give `text` instead of `sha256`: it is hashed here."""
    h = tmp / "helix"
    if app is not None:
        write(h / "app.py", app)
    (h / "memory/facts").mkdir(parents=True)
    for name, text in (facts or {}).items():
        write(h / "memory/facts" / name, text)
    for name, text in (archive or {}).items():
        write(h / "memory/archive" / name, text)
    if ledger is not None:
        rows = []
        for i, r in enumerate(ledger, 1):
            r = dict(r)
            if "text" in r:
                r["sha256"] = sha(r.pop("text"))
            rows.append({"factgate": "1.0", "line": i, "reason": "t", **r})
        lines(h / "workspace/factgate/state/FACTS_LEDGER.jsonl", rows)
    if catches is not None:
        lines(h / "workspace/catches/CATCHES.jsonl", catches)
    if registry is not None:
        write(h / "workspace/seam/registry/registry.json", json.dumps(registry))
        write(h / "workspace/seam/registry/REGATE_LEDGER.jsonl", "")
    write(h / "workspace/regate/refusals.log", regate_log)
    write(h / "workspace/sealgate/refusals.log", sealgate_log)
    if runs is not None:
        lines(h / "build_orders/runs/RUNS.jsonl", runs)
    write(h / "memory/sessions/log.md", log)
    return h


def snapshot(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------------------------
# the markers: read out of app.py with ast, never by importing it
# --------------------------------------------------------------------------------------------

def test_markers_are_read_from_app_py_with_ast(tmp_path):
    h = make_helix(tmp_path)
    assert cr.markers(h / "app.py") == ("⚠️ update", "correction (", "superseded", "was wrong")


def test_markers_never_import_app(tmp_path):
    """An app.py whose import would explode still gives up its tuple."""
    h = make_helix(tmp_path, app='raise SystemExit("imported!")\n_CORRECTION_MARKERS = ("x",)\n')
    assert cr.markers(h / "app.py") == ("x",)


def test_the_module_does_not_import_app():
    src = (HERE / "correction_rate.py").read_text()
    assert "import app" not in src and "__import__" not in src and "importlib" not in src


def test_a_missing_marker_tuple_is_an_error_not_a_guess(tmp_path):
    h = make_helix(tmp_path, app="X = 1\n")
    with pytest.raises(ValueError, match="_CORRECTION_MARKERS"):
        cr.markers(h / "app.py")


def test_the_rule_is_lower_case_substring():
    m = ("superseded", "⚠️ update")
    assert cr.has_correction("This was SUPERSEDED by 12_x", m)
    assert cr.has_correction("⚠️ UPDATE (2026-07-15)", m)
    assert not cr.has_correction("it supersedes 11_y", m)
    assert not cr.has_correction("", m) and not cr.has_correction(None, m)


def test_the_markers_and_the_classification_match_app_on_every_live_fact():
    """The one test that imports app.py — in a subprocess, with order 32's guards."""
    env = dict(os.environ, HELIX_NO_WARMUP="1", HELIX_NO_PRECONNECT="1", HELIX_NO_SCHEDULER="1",
               PYTHONDONTWRITEBYTECODE="1")
    prog = ("import json, os, sys; sys.path.insert(0, os.getcwd()); import app\n"
            "names = sorted(f for f in os.listdir(app.FACTS) if f.endswith('.md'))\n"
            "print(json.dumps({'markers': list(app._CORRECTION_MARKERS),"
            " 'marked': [n for n in names if app._fact_has_correction(open(app.FACTS + '/' + n).read())],"
            " 'names': names}))")
    py = LIVE / ".venv/bin/python"          # app.py's own interpreter (flask lives there)
    r = subprocess.run([str(py), "-c", prog], cwd=LIVE, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    live = json.loads(r.stdout.strip().splitlines()[-1])
    m = cr.markers(LIVE / "app.py")
    assert list(m) == live["markers"]
    facts = [f for f in cr.load_facts(LIVE, m) if f["where"] == "facts"]
    assert [f["name"] for f in facts] == live["names"]
    assert [f["name"] for f in facts if f["marked"]] == live["marked"]


# --------------------------------------------------------------------------------------------
# (a) correction share
# --------------------------------------------------------------------------------------------

def test_share_counts_facts_and_archive(tmp_path):
    h = make_helix(tmp_path, facts={"1_a.md": "fine", "2_b.md": "this was wrong", "3_c.md": "x"},
                   archive={"old/9_z.md": "superseded"}, ledger=[])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["share"] == {"marked": 2, "total": 4, "facts": [1, 3], "archive": [1, 1]}
    assert "2/4 marked (50.0%)" in cr.render(res)


def test_no_archive_dir_is_fine(tmp_path):
    h = make_helix(tmp_path, facts={"1_a.md": "superseded"}, ledger=[])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["share"]["archive"] == [0, 0]


# --------------------------------------------------------------------------------------------
# (c) dates from the ledger; unknown where it cannot say
# --------------------------------------------------------------------------------------------

def classify_one(tmp_path, text, ledger):
    h = make_helix(tmp_path, facts={"5_f.md": text}, ledger=ledger)
    res = cr.compute(h, today=date(2026, 10, 2))
    return res["facts"][0]


def test_a_baseline_first_fact_is_pre_ledger_unknown(tmp_path):
    t = "this was wrong"
    f = classify_one(tmp_path, t, [dict(file="5_f.md", kind="baseline", ts="2026-09-24T20:42:37", text=t)])
    assert f["status"] == "pre-ledger" and f["first_written"] is None and f["ttc_days"] is None


def test_a_fact_written_with_its_marker_is_at_birth_not_a_later_correction(tmp_path):
    t = "superseded"
    f = classify_one(tmp_path, t, [dict(file="5_f.md", kind="record", ts="2026-09-25T10:00:00", text=t)])
    assert f["status"] == "at-birth" and f["first_written"] == "2026-09-25T10:00:00"
    assert f["ttc_days"] is None


def test_a_marker_present_only_at_a_later_write_is_unknown_because_the_ledger_has_no_text(tmp_path):
    t = "superseded"
    f = classify_one(tmp_path, t, [dict(file="5_f.md", kind="record", ts="2026-09-25T10:00:00", text="v1"),
                                   dict(file="5_f.md", kind="record", ts="2026-09-27T10:00:00", text=t)])
    assert f["status"] == "unknown" and f["ttc_days"] is None
    assert f["marked_by"] == "2026-09-27T10:00:00" and "no text" in f["why"]


def test_a_fact_not_in_the_ledger_is_unknown(tmp_path):
    f = classify_one(tmp_path, "superseded", [])
    assert f["status"] == "unknown" and "not in the ledger" in f["why"]


def test_a_fact_changed_outside_the_ledger_is_unknown(tmp_path):
    f = classify_one(tmp_path, "superseded",
                     [dict(file="5_f.md", kind="record", ts="2026-09-25T10:00:00", text="other")])
    assert f["status"] == "unknown" and "matches no ledger write" in f["why"]


def test_an_unmarked_fact_still_gets_its_birth_date(tmp_path):
    f = classify_one(tmp_path, "plain", [dict(file="5_f.md", kind="record", ts="2026-09-25T10:00:00", text="plain")])
    assert f["status"] == "unmarked" and f["first_written"] == "2026-09-25T10:00:00"


def test_mtime_is_never_used(tmp_path):
    t = "superseded"
    h = make_helix(tmp_path, facts={"5_f.md": t}, ledger=[])
    os.utime(h / "memory/facts/5_f.md", (1_700_000_000, 1_700_000_000))
    f = cr.compute(h, today=date(2026, 10, 2))["facts"][0]
    assert f["first_written"] is None and f["status"] == "unknown"
    src = (HERE / "correction_rate.py").read_text()
    assert "st_mtime" not in src and "getmtime" not in src


def test_time_to_correction_reports_unknowns_not_a_median(tmp_path):
    t = "superseded"
    h = make_helix(tmp_path, facts={"5_f.md": t, "6_g.md": t}, ledger=[
        dict(file="5_f.md", kind="baseline", ts="2026-09-24T20:42:37", text=t)])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["ttc"] == {"known": 0, "unknown": 2, "at_birth": 0, "median_days": None,
                          "p25_days": None, "p75_days": None, "min_days": None, "max_days": None}
    assert "median n/a (0 known, 2 unknown)" in cr.render(res)


def test_time_to_correction_spread_from_known_values():
    s = cr.spread([1.0, 2.0, 3.0, 10.0])
    assert s["median_days"] == 2.5 and s["min_days"] == 1.0 and s["max_days"] == 10.0
    assert s["p25_days"] == 1.75 and s["p75_days"] == 4.75


# --------------------------------------------------------------------------------------------
# (b) caught before vs after
# --------------------------------------------------------------------------------------------

CATCHES = [
    {"n": 1, "date": "2026-09-23", "stage": "build", "caught_by": "x"},
    {"n": 2, "date": "2026-09-25", "stage": "pre-commit", "caught_by": "factgate-hook"},
    {"n": 3, "date": "2026-09-25", "stage": "pre-commit", "caught_by": "factgate-hook"},
    {"n": 4, "date": "2026-09-26", "stage": "post-hoc", "caught_by": "cold-review"},
    {"n": 5, "date": "2026-09-29", "kind": "retraction", "retracts": [2, "3"], "why": "false block"},
    {"n": 6, "date": "2026-10-01", "stage": "pre-commit", "caught_by": "factgate-hook"},
    {"n": 7, "date": "2026-10-01", "kind": "retraction", "retracts": [99], "why": "dangling"},
]


def test_retractions_are_subtracted_and_never_counted_themselves(tmp_path):
    h = make_helix(tmp_path, catches=CATCHES, ledger=[])
    res = cr.compute(h, today=date(2026, 10, 2))
    c = res["before"]["catches"]
    assert c == {"entries": 5, "retracted": 2, "counted": 3, "retraction_lines": 2,
                 "dangling_retractions": [99], "post_hoc_stage": 1}
    assert res["months"]["2026-09"]["before"] == 2 and res["months"]["2026-10"]["before"] == 1


def test_refusals_from_test_runs_are_not_catches(tmp_path):
    reg = ("2026-09-23T22:20:36 exit=4 verb=check target=/tmp/tmp.Q/ac3/registry.json failed=GOALPOST MOVED\n"
           "2026-09-24T09:00:00 exit=4 verb=check target=/home/u/helix/workspace/seam/registry/registry.json failed=DELETED\n"
           "garbage line\n")
    seal = ("2026-09-23T21:29:20 exit=3 verb=token prereg=/tmp/tmp.b/p1/PREREG.md failed=P1\n"
            "2026-09-23T21:29:20 exit=3 verb=token prereg=/home/u/helix/workspace/sealgate/fixtures/real_copy/PREREG.md failed=P1\n"
            "2026-10-01T10:00:00 exit=3 verb=seal prereg=/home/u/helix/workspace/crosscheck/attest/PREREG_5.md failed=P2\n")
    h = make_helix(tmp_path, regate_log=reg, sealgate_log=seal, ledger=[])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["before"]["regate"] == {"live": 1, "test_runs": 1, "unparsed": 1}
    assert res["before"]["sealgate"] == {"live": 1, "test_runs": 2, "unparsed": 0}
    assert res["months"]["2026-09"]["before"] == 1 and res["months"]["2026-10"]["before"] == 1


def test_reportcheck_flagged_counts_run_lines_only(tmp_path):
    runs = [{"order": "28", "unit": "u28", "started": "2026-10-02T06:50:18Z", "report_check": "FLAGGED"},
            {"order": "29", "unit": "u29", "started": "2026-10-02T08:00:00Z", "report_check": "VERIFIED"},
            {"queue": "q", "event": "halt", "at": "2026-10-02T11:01:35Z", "reason": "report check 'FLAGGED'"},
            {"queue": "q", "event": "order_end", "order": "33b", "unit": "u33b", "report_check": "FLAGGED"}]
    h = make_helix(tmp_path, runs=runs, ledger=[])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["before"]["reportcheck_flagged"] == 1
    assert res["months"]["2026-10"]["before"] == 1


def test_after_is_known_after_corrections_and_unknowns_ride_beside_it(tmp_path):
    t = "superseded"
    h = make_helix(tmp_path, facts={"5_f.md": t}, catches=CATCHES[:1], ledger=[
        dict(file="5_f.md", kind="record", ts="2026-09-25T10:00:00", text="v1"),
        dict(file="5_f.md", kind="record", ts="2026-09-27T10:00:00", text=t)])
    res = cr.compute(h, today=date(2026, 12, 1))
    m = res["months"]["2026-09"]
    assert (m["before"], m["after"], m["after_unknown"]) == (1, 0, 1)
    assert "1:0 (+1 unknown)" in cr.render(res)


# --------------------------------------------------------------------------------------------
# (d) vintage: fully-aged cohorts only
# --------------------------------------------------------------------------------------------

def vintage_helix(tmp_path):
    return make_helix(tmp_path, facts={"5_f.md": "plain", "6_g.md": "superseded", "7_h.md": "plain"},
                      ledger=[dict(file="5_f.md", kind="record", ts="2026-08-10T10:00:00", text="plain"),
                              dict(file="6_g.md", kind="record", ts="2026-08-11T10:00:00", text="superseded"),
                              dict(file="7_h.md", kind="record", ts="2026-09-12T10:00:00", text="plain")],
                      log="## 2026-07-03 12:40\n**You:** hi\n", catches=[], runs=[], registry={})


def test_a_young_cohort_is_censored_never_a_rate(tmp_path):
    h = vintage_helix(tmp_path)
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["vintage"]["2026-09"]["state"] == "censored" and res["vintage"]["2026-09"]["rate"] is None
    assert res["vintage"]["2026-08"]["state"] == "aged"
    out = cr.render(res)
    assert "2026-09: censored" in out and "ages on 2026-10-30" in out


def test_the_age_boundary_is_exactly_n_days_after_the_month_ends(tmp_path):
    h = vintage_helix(tmp_path)
    assert cr.compute(h, today=date(2026, 10, 29))["vintage"]["2026-09"]["state"] == "censored"
    assert cr.compute(h, today=date(2026, 10, 30))["vintage"]["2026-09"]["state"] == "aged"
    assert cr.compute(h, today=date(2026, 10, 9), n_days=7)["vintage"]["2026-09"]["state"] == "aged"


def test_an_aged_cohort_gets_a_rate_from_known_corrections_only(tmp_path):
    h = vintage_helix(tmp_path)
    v = cr.compute(h, today=date(2026, 10, 2))["vintage"]["2026-08"]
    assert v == {"state": "aged", "written": 2, "corrected": 0, "unknown": 0, "at_birth": 1, "rate": 0.0}


def test_months_with_zero_facts_are_printed_as_zero_not_skipped(tmp_path):
    h = vintage_helix(tmp_path)
    res = cr.compute(h, today=date(2026, 10, 2))
    assert list(res["months"]) == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert res["vintage"]["2026-07"] == {"state": "aged", "written": 0, "corrected": 0, "unknown": 0,
                                         "at_birth": 0, "rate": None}
    assert "2026-07: aged, 0 facts written — no cohort" in cr.render(res)


def test_pre_ledger_facts_are_their_own_cohort_with_no_month(tmp_path):
    h = make_helix(tmp_path, facts={"1_a.md": "superseded", "2_b.md": "x"},
                   ledger=[dict(file="1_a.md", kind="baseline", ts="2026-09-24T20:42:37", text="superseded"),
                           dict(file="2_b.md", kind="baseline", ts="2026-09-24T20:42:37", text="x")])
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["pre_ledger"] == {"facts": 2, "marked": 1}
    assert all(v["written"] == 0 for v in res["vintage"].values())


# --------------------------------------------------------------------------------------------
# (e) paralysis watch, (f) the specificity slot, the header
# --------------------------------------------------------------------------------------------

def test_leads_per_registry_entry_opened(tmp_path):
    h = make_helix(tmp_path, facts={
        "1_lead.md": "# 🔵 LEAD (not started) — a thing. 2026-09-23.\nbody",
        "2_lead.md": "# 🔵 LEAD — another\n",
        "3_not.md": "# leading edge of the wing\nLEAD in the body does not count\n",
        "4_body.md": "intro\n# A heading\n## LEAD second heading does not count\n"},
        ledger=[dict(file="2_lead.md", kind="record", ts="2026-10-01T09:00:00", text="# 🔵 LEAD — another\n")],
        registry={"0000": {"registered": "2026-09-22"}, "0001": {"registered": "2026-09-22"},
                  "0005": {"registered": "2026-10-01"}})
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["months"]["2026-09"]["leads"] == 1 and res["months"]["2026-09"]["opened"] == 2
    assert res["months"]["2026-10"]["leads"] == 1 and res["months"]["2026-10"]["opened"] == 1
    assert res["leads_undated"] == 0
    out = cr.render(res)
    assert "2026-09: 1 leads / 2 opened = 0.50" in out


def test_a_lead_with_no_date_anywhere_is_undated(tmp_path):
    h = make_helix(tmp_path, facts={"1_lead.md": "# LEAD — undated\n"}, ledger=[], registry={})
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["leads_undated"] == 1
    assert "1 lead with no date" in cr.render(res)


def test_zero_opened_is_not_a_division(tmp_path):
    h = make_helix(tmp_path, facts={"1_lead.md": "# LEAD 2026-09-02\n"}, ledger=[], registry={})
    out = cr.render(cr.compute(h, today=date(2026, 10, 2)))
    assert "2026-09: 1 leads / 0 opened = n/a" in out


def test_the_specificity_slot_and_the_header_warning(tmp_path):
    h = make_helix(tmp_path, ledger=[])
    out = cr.render(cr.compute(h, today=date(2026, 10, 2)))
    assert "canary recall: n/a (item 12 not built)" in out
    assert "acquittal: n/a (not built)" in out
    head = "\n".join(out.splitlines()[:4])
    assert "these numbers mean nothing alone until item 12 exists" in head


def test_the_session_log_gives_dates_only(tmp_path):
    """Marker words inside log.md change nothing: the log only extends the month span."""
    h = make_helix(tmp_path, ledger=[], log="## 2026-08-01 10:00\nthis was wrong, superseded\n")
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["share"]["marked"] == 0 and list(res["months"])[0] == "2026-08"
    assert res["months"]["2026-08"]["sessions"] == 1


# --------------------------------------------------------------------------------------------
# missing sources, read-only, CLI, the nightly line
# --------------------------------------------------------------------------------------------

def test_missing_sources_are_reported_never_a_crash(tmp_path):
    h = tmp_path / "helix"
    write(h / "app.py", APP)
    (h / "memory/facts").mkdir(parents=True)
    res = cr.compute(h, today=date(2026, 10, 2))
    assert set(res["unavailable"]) == {"ledger", "catches", "registry", "regate_ledger", "regate",
                                    "sealgate", "runs", "log"}
    out = cr.render(res)
    assert "ledger: source unavailable" in out


def test_corrupt_json_lines_are_skipped_and_counted(tmp_path):
    h = make_helix(tmp_path, catches=CATCHES[:2], ledger=[])
    with (h / "workspace/catches/CATCHES.jsonl").open("a") as f:
        f.write("{not json\n")
    res = cr.compute(h, today=date(2026, 10, 2))
    assert res["before"]["catches"]["counted"] == 2 and res["bad_lines"]["catches"] == 1


def test_a_run_changes_no_byte_and_works_on_a_read_only_tree(tmp_path):
    h = vintage_helix(tmp_path)
    before = snapshot(h)
    for p in sorted(h.rglob("*"), reverse=True):
        p.chmod(0o555 if p.is_dir() else 0o444)
    try:
        res = cr.compute(h, today=date(2026, 10, 2))
        cr.render(res)
        cr.correction_line(h, today=date(2026, 10, 2))
        r = subprocess.run([sys.executable, "-B", str(HERE / "correction_rate.py"), "report", "--json",
                            "--helix", str(h)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    finally:
        for p in h.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
    assert snapshot(h) == before


def test_cli_text_json_and_n_days(tmp_path):
    h = vintage_helix(tmp_path)
    r = subprocess.run([sys.executable, "-B", str(HERE / "correction_rate.py"), "report", "--helix", str(h)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("# Correction-rate report"), r.stderr
    r = subprocess.run([sys.executable, "-B", str(HERE / "correction_rate.py"), "report", "--json",
                        "--n-days", "7", "--helix", str(h)], capture_output=True, text=True)
    d = json.loads(r.stdout)
    assert d["n_days"] == 7 and d["share"]["total"] == 3
    r = subprocess.run([sys.executable, "-B", str(HERE / "correction_rate.py"), "report", "--n-days", "0",
                        "--helix", str(h)], capture_output=True, text=True)
    assert r.returncode == 2


def test_the_correction_line(tmp_path):
    h = vintage_helix(tmp_path)
    line = cr.correction_line(h, today=date(2026, 10, 2))
    assert line == ("- correction: 1/3 marked (33.3%) · before:after 2026-08: 0:0"
                    " · time-to-correction: median n/a (0 known, 0 unknown)")
    h2 = make_helix(tmp_path / "2", facts={"1_a.md": "x"}, ledger=[], log="## 2026-09-03 12:40\n")
    assert "before:after: no aged month yet (2026-09 ages on 2026-10-30)" in cr.correction_line(
        h2, today=date(2026, 10, 2))


def test_the_correction_line_never_raises(tmp_path):
    assert cr.correction_line(tmp_path / "nothing", today=date(2026, 10, 2)).startswith(
        "- correction: 🔴 metric failed (")


def test_live_paths_are_the_ones_the_order_names():
    assert cr.HELIX == Path.home() / "helix"
    assert cr.PATHS["ledger"] == "workspace/factgate/state/FACTS_LEDGER.jsonl"
    assert cr.PATHS["catches"] == "workspace/catches/CATCHES.jsonl"
    assert cr.PATHS["registry"] == "workspace/seam/registry/registry.json"
    assert cr.PATHS["regate_ledger"] == "workspace/seam/registry/REGATE_LEDGER.jsonl"
    assert cr.PATHS["log"] == "memory/sessions/log.md"
    assert cr.N_DAYS == 30
