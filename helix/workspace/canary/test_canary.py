"""Tests for canary.py — the canary program and the acquittal control (order 44, item 12).

Every case runs the REAL gate code: byte-identical copies of factgate.py, regate.py, sealgate.py
and preflight.py in a temp world laid out like ~/helix, run as subprocesses against temp state.
Nothing here writes a live file; `test_a_run_leaves_every_live_file_byte_identical` proves it.
"""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import canary  # noqa: E402

HELIX = Path.home() / "helix"
LIVE_GATES = {"factgate": HELIX / "workspace/factgate/factgate.py",
              "regate": HELIX / "workspace/regate/regate.py",
              "sealgate": HELIX / "workspace/sealgate/sealgate.py",
              "preflight": HELIX / "workspace/preflight/preflight.py",
              "review": HELIX / "workspace/sealgate/review.py",
              "veto": HELIX / "workspace/veto/veto.py",
              "categories": HELIX / "workspace/categories/categories.py"}
CATEGORY_LIST = HELIX / "workspace/categories/CATEGORIES.txt"
CANARIES = ["off-ledger", "silent-edit", "interim-verdict", "no-category", "paraphrase-r5",
            "supersede-open-r5",
            "vacuous-prereg-p5", "no-preflight-p8", "wrong-reviewer-p9", "vetoed-seal-p10",
            "disputed-seal-p10"]
ACQUITTALS = ["recorded-fact", "new-scope-entry", "complete-prereg"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    return canary.build_world(tmp_path)


@pytest.fixture
def temp_log(monkeypatch, tmp_path):
    log = tmp_path / "CANARY.jsonl"
    monkeypatch.setattr(canary, "CANARY_LOG", log)
    return log


def case(name):
    return next(c for c in canary.CASES if c.name == name)


# ------------------------------------------------------------------ the world

def test_the_world_is_laid_out_like_helix_and_the_copies_are_the_live_gates(world, tmp_path):
    assert world == tmp_path / "helix"
    for name, live in LIVE_GATES.items():
        copy = world / live.relative_to(HELIX)
        assert copy.is_file() and copy != live
        assert sha(copy) == sha(live), name
    assert canary.gate_shas(world) == {n: sha(p) for n, p in LIVE_GATES.items()}


def test_order78_the_world_has_the_live_category_list(world):
    """Order 76's F5 loads categories.py by path and fails closed without it: the world carries
    it and CATEGORIES.txt byte-identical, and every file it reads beside itself."""
    for live in (LIVE_GATES["categories"], CATEGORY_LIST):
        copy = world / live.relative_to(HELIX)
        assert copy.is_file() and sha(copy) == sha(live), live
    for name in ("ASSIGNED.csv", "BACKFILL_PROPOSAL.csv", "OVERRIDES.csv"):     # what it reads
        live = LIVE_GATES["categories"].parent / name
        copy = world / live.relative_to(HELIX)
        assert (sha(copy) if copy.is_file() else None) == (sha(live) if live.is_file() else None)
    assert not (world / "workspace/categories/OVERRIDES_DRAFT.csv").exists()   # it never reads


def test_no_path_a_copy_resolves_points_into_the_real_helix(world, tmp_path):
    """Pre-flight 4: every Path a copy derives (HELIX, its state dir, its refusals log, regate's
    SEALGATE and CYCLE, sealgate's PREFLIGHT_PY …) lands in the temp world."""
    paths = canary.resolved_paths(world)
    for key in ("factgate.HELIX", "factgate.DEFAULT_STATE", "factgate.FACTS_DIR",
                "factgate.REGISTRY_JSON", "regate.REFUSALS_LOG", "regate.SEALGATE", "regate.CYCLE",
                "sealgate.REFUSALS_LOG", "sealgate.PREFLIGHT_PY", "preflight.DEFAULT_OUT",
                "sealgate.REVIEW_PY", "sealgate.REVIEWS_LEDGER", "review.LEDGER", "review.HELIX",
                "sealgate.VETO_PY", "sealgate.VETOES_LEDGER", "veto.LEDGER", "veto.HELIX",
                "veto.REVIEWS_LEDGER", "veto.SEALGATE_PY", "factgate.CATEGORIES_PY",
                "categories.DEFAULT", "categories.FACTS"):
        assert key in paths, key
    real = str(HELIX.resolve())
    inside = [k for k, v in paths.items() if v == real or v.startswith(real + "/")]
    assert inside == [], inside
    # Order 48: veto.BULK is the HDD (outside helix); write_veto points it at the case dir.
    outside = [k for k, v in paths.items() if not v.startswith(str(tmp_path.resolve()) + "/")]
    assert outside == ["veto.BULK"], outside


def test_every_case_the_order_names_is_there_and_every_gate_has_an_acquittal():
    names = [c.name for c in canary.CASES]
    assert len(names) == len(set(names))
    assert [c.name for c in canary.CASES if c.kind == "canary"] == CANARIES
    assert [c.name for c in canary.CASES if c.kind == "acquittal"] == ACQUITTALS
    assert {c.gate for c in canary.CASES if c.kind == "acquittal"} == \
        {"factgate", "regate", "sealgate"}


# ------------------------------------------------------- every case, real gates

@pytest.mark.parametrize("name", CANARIES)
def test_each_canary_is_caught_by_the_real_gate(world, name):
    r = canary.run_case(case(name), world)
    assert r["outcome"] == "caught", r


@pytest.mark.parametrize("name", ACQUITTALS)
def test_each_acquittal_passes_the_real_gate(world, name):
    r = canary.run_case(case(name), world)
    assert r["outcome"] == "acquitted", r


def test_the_canaries_say_why_they_were_caught(world):
    got = {c.name: canary.run_case(c, world)["detail"] for c in canary.CASES}
    assert "NEW OFF-LEDGER" in got["off-ledger"]
    assert "CHANGED OFF-LEDGER" in got["silent-edit"]
    assert "🟢" in got["interim-verdict"] and "PASSED" in got["interim-verdict"]
    assert "F5_CATEGORY_DECLARED" in got["no-category"]
    assert "R5_NOT_A_RESURRECTION" in got["paraphrase-r5"]
    assert "R5_NOT_A_RESURRECTION" in got["supersede-open-r5"] \
        and "not INVALID" in got["supersede-open-r5"]
    assert "P5_BANDS_AND_STOP_CONDITIONS" in got["vacuous-prereg-p5"]
    assert "P8_PREFLIGHT" in got["no-preflight-p8"]
    assert "P9_ASSIGNED_REVIEWER" in got["wrong-reviewer-p9"]
    assert "P10_ALICE_VETO" in got["vetoed-seal-p10"]
    assert "P10_ALICE_VETO" in got["disputed-seal-p10"]


def test_the_copies_log_their_refusals_beside_themselves(world):
    for name in ("paraphrase-r5", "supersede-open-r5", "vacuous-prereg-p5", "no-preflight-p8"):
        canary.run_case(case(name), world)
    regate_log = (world / "workspace/regate/refusals.log").read_text()
    sealgate_log = (world / "workspace/sealgate/refusals.log").read_text()
    assert regate_log.count("verb=register") == 2, regate_log
    assert "P5_BANDS_AND_STOP_CONDITIONS" in sealgate_log and "P8_PREFLIGHT" in sealgate_log


def test_the_complete_prereg_really_gets_a_token(world):
    canary.run_case(case("complete-prereg"), world)
    tokens = list(world.rglob("RUN_TOKEN.json"))
    assert len(tokens) == 1 and json.loads(tokens[0].read_text())["preconditions"]


# ---------------------------------------- positive control for the canary itself

def test_a_broken_gate_turns_its_canary_red(world):
    """101_* rule 1: R5's threshold patched to 1.01 in the regate COPY. The paraphrase canary
    must then be MISSED and say so; every other case keeps its outcome."""
    copy = world / "workspace/regate/regate.py"
    src = copy.read_text()
    assert src.count("R5_THRESHOLD = 0.59\n") == 1
    copy.write_text(src.replace("R5_THRESHOLD = 0.59\n", "R5_THRESHOLD = 1.01\n"))
    results = canary.run_cases(world)
    by = {r["name"]: r["outcome"] for r in results}
    assert by.pop("paraphrase-r5") == "missed"
    assert set(by.values()) == {"caught", "acquitted"}, by
    assert canary.summary(results) == \
        "🔴 10/11 caught (missed: paraphrase-r5) · 3/3 acquitted"


def test_a_gate_that_refuses_everything_turns_an_acquittal_red(world):
    """The twin: a factgate copy whose check always finds something must fail the acquittal."""
    copy = world / "workspace/factgate/factgate.py"
    src = copy.read_text()
    needle = "    problems, recorded = run_check(args.facts_dir, args.state_dir)\n"
    assert src.count(needle) == 1
    copy.write_text(src.replace(needle, needle + "    problems = problems or ['planted']\n"))
    r = canary.run_case(case("recorded-fact"), world)
    assert r["outcome"] == "refused", r
    results = [r] + [canary.run_case(case(n), world) for n in ("new-scope-entry",)]
    assert canary.summary(results) == "0/0 caught · 🔴 1/2 acquitted (refused: recorded-fact)"


# ------------------------------------------------------------- counting rules

def test_a_case_that_errors_is_named_and_folded_into_no_rate(world):
    def boom(helix, d):
        raise RuntimeError("no fixture")
    cases = [canary.Case("planted-error", "canary", "regate", boom), case("off-ledger"),
             case("recorded-fact")]
    results = canary.run_cases(world, cases)
    assert [r["outcome"] for r in results] == ["error", "caught", "acquitted"]
    assert "RuntimeError: no fixture" in results[0]["detail"]
    t = canary.tally(results)
    assert (t["caught"], t["canaries"], t["acquitted"], t["acquittals"]) == (1, 1, 1, 1)
    assert t["errors"] == ["planted-error"]
    assert canary.summary(results) == "1/1 caught · 1/1 acquitted · 🔴 errored: planted-error"


def test_a_canary_the_gate_refuses_for_another_reason_is_an_error_not_a_catch(world):
    """Caught means the named refusal. Exit 2 (usage) or a different precondition is a broken
    case, and it must not inflate recall."""
    out = canary.verdict_from_refusal("canary", 2, "", "usage: …", "R5_NOT_A_RESURRECTION")
    assert out[0] == "error"
    out = canary.verdict_from_refusal("canary", 3, "", "FAILED   R1_ID_IS_NEXT: x\n",
                                      "R5_NOT_A_RESURRECTION")
    assert out[0] == "error"
    assert canary.verdict_from_refusal("canary", 0, "REGISTERED", "", "R5")[0] == "missed"
    assert canary.verdict_from_refusal("acquittal", 3, "", "FAILED   P1_X: y\n", None)[0] == \
        "refused"
    assert canary.verdict_from_refusal("acquittal", 0, "ok", "", None)[0] == "acquitted"


def test_the_line_shapes():
    def res(name, kind, outcome):
        return {"name": name, "kind": kind, "outcome": outcome}
    ok = [res(n, "canary", "caught") for n in CANARIES] + \
         [res(n, "acquittal", "acquitted") for n in ACQUITTALS]
    assert canary.summary(ok) == "11/11 caught · 3/3 acquitted"
    miss = [dict(r, outcome="missed") if r["name"] == "paraphrase-r5" else r for r in ok]
    assert canary.summary(miss) == "🔴 10/11 caught (missed: paraphrase-r5) · 3/3 acquitted"


# ------------------------------------------------- the run, the record, the live base

def live_state():
    return canary.snapshot(canary.live_files())


def test_the_live_files_named_in_the_hard_rule_are_all_watched():
    names = {str(p) for p in canary.live_files()}
    for p in (HELIX / "workspace/factgate/state/FACTS_LEDGER.jsonl",
              HELIX / "workspace/seam/registry/registry.json",
              HELIX / "workspace/seam/registry/REGATE_LEDGER.jsonl",
              HELIX / "workspace/regate/refusals.log",
              HELIX / "workspace/sealgate/refusals.log",
              HELIX / "workspace/sealgate/REVIEWS.jsonl",
              HELIX / "workspace/veto/VETOES.jsonl"):
        assert str(p) in names, p
    facts = sorted(p for p in (HELIX / "memory/facts").iterdir() if p.is_file())
    assert facts and all(str(p) in names for p in facts)


def test_a_run_leaves_every_live_file_byte_identical(temp_log, capsys):
    before = live_state()
    gates_before = {n: sha(p) for n, p in LIVE_GATES.items()}
    live_log = HERE / "CANARY.jsonl"
    log_before = sha(live_log) if live_log.exists() else None
    assert canary.main(["run"]) == 0, capsys.readouterr()
    assert live_state() == before
    assert {n: sha(p) for n, p in LIVE_GATES.items()} == gates_before
    assert (sha(live_log) if live_log.exists() else None) == log_before
    out = capsys.readouterr().out
    assert "11/11 caught · 3/3 acquitted" in out
    assert len([l for l in out.splitlines() if re.match(r"^(CAUGHT|ACQUITTED)\s", l)]) == 14
    assert re.search(r"^CAUGHT\s+canary\s+factgate\s+no-category\s.*F5_CATEGORY_DECLARED", out,
                     re.M), out


def test_a_run_appends_exactly_one_line_to_the_record(temp_log, capsys):
    temp_log.write_text('{"earlier": 1}\n')
    t0 = time.monotonic()
    assert canary.main(["run", "--json"]) == 0
    took = time.monotonic() - t0
    lines = temp_log.read_text().splitlines()
    assert len(lines) == 2 and lines[0] == '{"earlier": 1}'
    rec = json.loads(lines[1])
    assert rec["date"] == time.strftime("%Y-%m-%d")
    assert rec["recall"] == "11/11" and rec["specificity"] == "3/3"
    assert rec["cases"] == {**{n: "caught" for n in CANARIES},
                            **{n: "acquitted" for n in ACQUITTALS}}
    assert rec["gates"] == {n: sha(p) for n, p in LIVE_GATES.items()}
    assert rec["errors"] == [] and rec["live_untouched"] is True
    assert json.loads(capsys.readouterr().out) == rec
    assert took < 60, took


def test_canary_line_is_the_nightly_shape_and_writes_its_record(temp_log):
    line = canary.canary_line()
    assert line == "- canary: 11/11 caught · 3/3 acquitted"
    assert len(temp_log.read_text().splitlines()) == 1


def test_the_cli_is_one_verb():
    res = subprocess.run([sys.executable, str(HERE / "canary.py"), "--help"],
                         capture_output=True, text=True)
    assert res.returncode == 0 and "run" in res.stdout


def test_no_escape_hatch_words():
    src = (HERE / "canary.py").read_text()
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check|FACTGATE_OFF|maintenance|environ",
                         l, flags=re.I)]
    assert hits == [], hits
    assert not re.search(r"skip", src, re.I)


def test_order47_the_review_records_live_in_the_temp_world_only(world):
    """The complete prereg's assign + review lines, and its raw review, are in the WORLD's
    REVIEWS.jsonl and case dir; the acquittal stays acquitted; the live ledger is not touched."""
    live = HELIX / "workspace/sealgate/REVIEWS.jsonl"
    before = sha(live) if live.exists() else None
    r = canary.run_case(case("complete-prereg"), world)
    assert r["outcome"] == "acquitted", r
    recs = [json.loads(l) for l in
            (world / "workspace/sealgate/REVIEWS.jsonl").read_text().splitlines()]
    assert [x["kind"] for x in recs] == ["assign", "review"]
    assert recs[0]["reviewer"] == recs[1]["reviewer"] == "Alice"
    assert recs[1]["findings"] == ["F1", "F2", "F3"]
    assert list((world / "cases/complete-prereg").glob("REVIEW_Alice_*.md"))
    assert (sha(live) if live.exists() else None) == before


def test_order47_the_wrong_reviewer_canary_is_caught_by_p9_alone(world):
    r = canary.run_case(case("wrong-reviewer-p9"), world)
    assert r["outcome"] == "caught", r
    assert r["detail"] == "exit 3: P9_ASSIGNED_REVIEWER", r


def test_order48_the_vetoed_seal_canary_is_caught_by_p10_alone(world):
    r = canary.run_case(case("vetoed-seal-p10"), world)
    assert r["outcome"] == "caught", r
    assert r["detail"] == "exit 3: P10_ALICE_VETO", r


def test_order48b_the_vetoed_seal_canary_is_a_confirmed_break(world):
    canary.run_case(case("vetoed-seal-p10"), world)
    recs = [json.loads(l) for l in
            (world / "workspace/veto/VETOES.jsonl").read_text().splitlines()]
    assert [x["outcome"] for x in recs] == ["BREAK_CONFIRMED"]
    assert recs[0]["judge"]["valid"] is True


def test_order48b_the_disputed_seal_canary_is_caught_by_p10_alone(world):
    r = canary.run_case(case("disputed-seal-p10"), world)
    assert r["outcome"] == "caught", r
    assert r["detail"] == "exit 3: P10_ALICE_VETO", r
    recs = [json.loads(l) for l in
            (world / "workspace/veto/VETOES.jsonl").read_text().splitlines()]
    assert [(x["outcome"], x["disagreement_kind"]) for x in recs] == \
        [("BREAK_DISPUTED", "alice_vs_judge")]
    seal = world / "cases/disputed-seal-p10/PREREG.md"
    code, out, err = canary.gate(world, "sealgate", "check", seal, cwd=seal.parent)
    assert code == 3 and "disputed: Alice and the judge disagree, the user decides" in err, err


def test_order48_the_veto_records_live_in_the_temp_world_only(world):
    """The complete prereg's NO_BREAK run: its line in the WORLD's VETOES.jsonl, its scratch in the
    case dir (not the HDD); the acquittal stays acquitted; the live ledger is not touched."""
    live = HELIX / "workspace/veto/VETOES.jsonl"
    before = sha(live) if live.exists() else None
    r = canary.run_case(case("complete-prereg"), world)
    assert r["outcome"] == "acquitted", r
    recs = [json.loads(l) for l in
            (world / "workspace/veto/VETOES.jsonl").read_text().splitlines()]
    # order 48b: and a BREAK_INVALID record beside it, which must not veto it
    assert [(x["kind"], x["outcome"]) for x in recs] == [("veto", "NO_BREAK"),
                                                         ("veto", "BREAK_INVALID")]
    assert recs[1]["why"] == "tamper: hashlib.py, med.py"
    assert recs[0]["scratch"].startswith(str(world / "cases/complete-prereg") + "/")
    assert (sha(live) if live.exists() else None) == before


def test_order48_without_its_veto_record_the_acquittal_is_refused_by_p10(world, monkeypatch):
    """The acquittal really depends on its NO_BREAK record: with none written, P10 refuses it."""
    monkeypatch.setattr(canary, "write_veto", lambda helix, p, outcome: None)
    r = canary.run_case(case("complete-prereg"), world)
    assert r["outcome"] == "refused" and r["detail"] == "exit 3: P10_ALICE_VETO", r


def test_order78_the_new_facts_declare_a_category_and_the_one_without_is_refused_by_f5(world):
    """The two planted NEW facts pass F5 and keep testing what they did; `no-category` is
    caught by F5 alone, so F5 itself is watched every night."""
    assert canary.run_case(case("interim-verdict"), world)["outcome"] == "caught"
    assert canary.run_case(case("recorded-fact"), world)["outcome"] == "acquitted"
    for name in ("interim-verdict/facts/04_result.md", "recorded-fact/facts/04_recorded.md"):
        head = (world / "cases" / name).read_text(encoding="utf-8").splitlines()[:15]
        assert any(l.startswith("Category: ") for l in head), name
    r = canary.run_case(case("no-category"), world)
    assert r["outcome"] == "caught" and r["detail"] == "exit 3: F5_CATEGORY_DECLARED", r
    assert (world / "cases/no-category/facts/04_uncategorised.md").is_file()


def test_order78_a_full_run_has_every_case_caught_or_acquitted_no_category_among_them(world):
    by = {r["name"]: r["outcome"] for r in canary.run_cases(world)}
    assert set(by) == set(CANARIES) | set(ACQUITTALS) and len(by) == 14
    assert by["no-category"] == "caught"
    assert set(by.values()) == {"caught", "acquitted"}, by
