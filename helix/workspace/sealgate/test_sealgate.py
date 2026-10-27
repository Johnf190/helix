"""Tests for sealgate.py — the seal gate.

Everything runs the real CLI in a subprocess, so exit codes are the real thing and
not a return value from an imported function.

Fixtures: `fixtures/complete/` is a synthetic, complete pre-registration. Each test
copies it into tmp_path and breaks EXACTLY ONE precondition, so the refusal can be
checked to name that precondition and no other.

`fixtures/real_copy/PREREG.md` is a byte-identical, read-only copy of a REAL
pre-registration. It is never written to. It is supposed to be refused.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SEALGATE = HERE / "sealgate.py"
FIXTURES = HERE / "fixtures"
REFUSALS_LOG = None            # order 41: a temp log for the session, set below
SEALGATE_RUN = SEALGATE        # order 41: the shim that writes to it, set below

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_DRIFT = 4

ALL_PRECONDITIONS = [
    "P1_REVIEW_STAMP",
    "P2_REVIEWER_NOT_AUTHOR",
    "P3_FINDINGS_DISPOSITIONED",
    "P4_CONTROLS_CHECKLIST",
    "P5_BANDS_AND_STOP_CONDITIONS",
    "P6_CONTENT_HASH",
    "P7_STAMP_BLOCK_CANONICAL",
    "P8_PREFLIGHT",
    "P9_ASSIGNED_REVIEWER",
    "P10_ALICE_VETO",
]

# Order 21: preflight.py lives beside sealgate, and P8 imports it.
PREFLIGHT_DIR = HERE.parent / "preflight"
sys.path.insert(0, str(PREFLIGHT_DIR))
import preflight  # noqa: E402
sys.path.insert(0, str(HERE))
import review  # noqa: E402
# Order 48: veto.py lives in ../veto, and P10 imports it.
VETO_DIR = HERE.parent / "veto"
sys.path.insert(0, str(VETO_DIR))
import veto  # noqa: E402

BRIEF_NAME = "PREFLIGHT_fixture.md"
RESHAPE_LINE = "narrow the claim to the synthetic probe only"


def brief_sections(verdict="GO"):
    return (
        "## 1. Academia\n"
        "- Doe 2004, \"A synthetic probe\" — the closest prior work; does not read the planted signal\n\n"
        "## 2. GitHub\n"
        "- NOTHING FOUND\n"
        "- query: synthetic probe planted signal github\n\n"
        "## 3. Known math\n"
        "- https://example.org/probe-theory — standard bound on probe accuracy\n\n"
        "## 4. Known limits\n"
        "- NOTHING FOUND\n"
        "- query: synthetic probe impossibility\n\n"
        "## 5. Cheapest kill\n"
        "Run the answer-free floor first.\n\n"
        "## 6. Verdict\n"
        f"{verdict}\n")


def write_brief(directory, verdict="GO", searcher="Alice", name=BRIEF_NAME):
    """A brief exactly as `preflight.py run` would write it, without calling Alice."""
    text = preflight.build_brief(b"Idea: the synthetic probe fixture.\n", brief_sections(verdict),
                                 searcher=searcher, model="Gemini 3.1 Pro (High)",
                                 date_utc="2026-09-30T22:00:00Z", n_queries=4)
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    return path


def add_preflight(prereg, verdict="GO", searcher="Alice", brief_rel=BRIEF_NAME, extra=""):
    """Write a program-built brief beside the prereg and append its pre-flight section.

    fixtures/complete/PREREG.md is left untouched; this happens on the tmp_path copy.
    """
    brief = write_brief(prereg.parent, verdict=verdict, searcher=searcher)
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    with open(prereg, "a", encoding="utf-8") as fh:
        fh.write(f"\n## PRE-FLIGHT\nBrief: {brief_rel}\nBrief-sha256: {digest}\n{extra}")
    return brief


# Order 41: the suite's refusals go to a temp log, never to the live refusals.log beside
# sealgate.py. Every run goes through a shim (a file in a temp dir named sealgate.py) that
# loads the real sealgate, points its REFUSALS_LOG at the temp log, and calls its main():
# the code and the exit codes are the real thing.
# Order 47: the shim also points the gate at the test's own temp REVIEWS.jsonl (the variable is
# read here, by the test shim, never by sealgate.py), so no test reads or writes the live ledger.
SHIM = """import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("sealgate", {gate!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["sealgate"] = mod
spec.loader.exec_module(mod)
mod.REFUSALS_LOG = Path({log!r})
mod.REVIEWS_LEDGER = Path(os.environ["SEALGATE_TEST_LEDGER"])
mod.VETOES_LEDGER = Path(os.environ["SEALGATE_TEST_VETOES"])
sys.exit(mod.main())
"""
LEDGER = None                  # order 47: this test's temp REVIEWS.jsonl, set below
VETOES = None                  # order 48: this test's temp VETOES.jsonl, set below
AUTO_VETO = True               # order 48: see `run` below


@pytest.fixture(autouse=True, scope="session")
def refusals_to_a_temp_log(tmp_path_factory):
    global REFUSALS_LOG, SEALGATE_RUN
    d = tmp_path_factory.mktemp("sealgate")
    REFUSALS_LOG = d / "refusals.log"
    SEALGATE_RUN = d / "sealgate.py"
    SEALGATE_RUN.write_text(SHIM.format(gate=str(SEALGATE), log=str(REFUSALS_LOG)))
    yield


@pytest.fixture(autouse=True)
def reviews_to_a_temp_ledger(tmp_path_factory, monkeypatch):
    """Order 47: each test has its own empty REVIEWS.jsonl, and both reviewers are fakes that
    answer [F1]-[F3] (the fixture's findings); no test ever calls a real reviewer."""
    global LEDGER
    LEDGER = tmp_path_factory.mktemp("reviews") / "REVIEWS.jsonl"
    monkeypatch.setattr(review, "LEDGER", LEDGER)
    for name in review.REVIEWER_NAMES:
        monkeypatch.setitem(review.REVIEWERS, name, fake_reviewer(FAKE_REPLY))
    yield


@pytest.fixture(autouse=True)
def vetoes_to_a_temp_ledger(reviews_to_a_temp_ledger, tmp_path_factory, monkeypatch):
    """Order 48: each test has its own empty VETOES.jsonl and bulk dir, and Alice is a fake that
    writes NO_BREAK.md; no test ever calls the real Alice. AUTO_VETO is back on for every test."""
    global VETOES, AUTO_VETO
    d = tmp_path_factory.mktemp("vetoes")
    VETOES = d / "VETOES.jsonl"
    AUTO_VETO = True
    monkeypatch.setattr(veto, "LEDGER", VETOES)
    monkeypatch.setattr(veto, "BULK", d / "bulk")
    monkeypatch.setattr(veto, "REVIEWS_LEDGER", LEDGER)
    monkeypatch.setattr(veto, "ALICE", fake_alice({"NO_BREAK.md": "I tried the bands.\n"}))

    def no_real_judge(prompt, workdir):                      # order 48b: never the real judge
        raise AssertionError("a test reached the real judge")
    monkeypatch.setattr(veto, "JUDGE", no_real_judge)
    yield


def fake_alice(files):
    def alice(scratch, prompt):
        for name, text in files.items():
            (Path(scratch) / name).write_text(text)
        return {"model": "fake"}
    return alice


def fake_judge(valid):
    """Order 48b: a fake cold judge answering the JSON the real one is asked for."""
    return lambda prompt, workdir: {"text": json.dumps({"valid": valid, "why": "fixture"}),
                                    "meta": {"model": "fake"}}


def give_veto(prereg, files=None, judge=None):
    """The real veto.run, on this test's temp ledger, with a fake Alice (and, for a break that
    reaches it, a fake judge saying `judge`). → the record."""
    if files is not None or judge is not None:
        with pytest.MonkeyPatch.context() as mp:
            if files is not None:
                mp.setattr(veto, "ALICE", fake_alice(files))
            if judge is not None:
                mp.setattr(veto, "JUDGE", fake_judge(judge))
            return veto.run(prereg)
    return veto.run(prereg)


def vetoed(prereg):
    """True if the temp ledger already holds a passing run for this seal's current body."""
    key, body = veto.seal_key(prereg), veto.body_sha256(Path(prereg).read_bytes())
    return any(rec and rec.get("seal") == key and rec.get("body_sha256") == body
               and rec.get("outcome") in ("NO_BREAK", "BREAK_NOT_REPRODUCED", "BREAK_INVALID")
               for _, _, rec in veto.read_ledger(VETOES))


@pytest.fixture
def no_auto_veto():
    """P10's own tests: nothing is vetoed unless the test does it."""
    global AUTO_VETO
    AUTO_VETO = False
    yield


FAKE_REPLY = ("[F1] The bar of 0.72 is asserted, not derived.\n"
              "[F2] B2 can be met by an artefact of the split.\n"
              "[F3] No positive control.\n")


def fake_reviewer(text):
    return lambda prompt: {"text": text, "meta": {"model": "fake"}}


def give_review(prereg):
    """The real assign and run, on this test's temp ledger, with the fake reviewer. On a fresh
    ledger the first assignment is Alice, which is who the fixtures stamp."""
    reviewer, _ = review.assign(prereg, LEDGER)
    review.run(prereg, LEDGER)
    return reviewer


def run(*args):
    """Order 48: P10 needs a veto run for the seal's CURRENT body. The tests that break exactly
    one OTHER precondition edit the body, so (unless a test asks for `no_auto_veto`) `check` and
    `token` first give the current body a fake-Alice NO_BREAK run if it has none."""
    if AUTO_VETO and len(args) > 1 and args[0] in ("check", "token") \
            and Path(args[1]).is_file() and not vetoed(args[1]):
        give_veto(args[1])
    env = dict(__import__("os").environ, SEALGATE_TEST_LEDGER=str(LEDGER),
               SEALGATE_TEST_VETOES=str(VETOES))
    return subprocess.run([sys.executable, str(SEALGATE_RUN), *map(str, args)],
                          capture_output=True, text=True, env=env)


def failed_preconditions(stderr):
    """The precondition ids the gate reported as FAILED, in order."""
    return re.findall(r"^FAILED\s+(P\d+_[A-Z_]+)", stderr, flags=re.M)


def log_lines():
    return REFUSALS_LOG.read_text().splitlines() if REFUSALS_LOG.exists() else []


@pytest.fixture
def sealed(tmp_path):
    """A complete prereg, copied out and stamped by a reviewer who is not the author."""
    shutil.copy(FIXTURES / "complete" / "PREREG.md", tmp_path / "PREREG.md")
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", tmp_path / "FINDINGS.md")
    prereg = tmp_path / "PREREG.md"
    add_preflight(prereg)
    give_review(prereg)
    res = run("stamp", prereg, "--reviewer", "Alice", "--findings", "FINDINGS.md")
    assert res.returncode == EXIT_OK, res.stderr
    return prereg


@pytest.fixture
def unsealed(tmp_path):
    """The same complete prereg, with its pre-flight but no stamp on it yet."""
    shutil.copy(FIXTURES / "complete" / "PREREG.md", tmp_path / "PREREG.md")
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", tmp_path / "FINDINGS.md")
    add_preflight(tmp_path / "PREREG.md")
    give_review(tmp_path / "PREREG.md")
    return tmp_path / "PREREG.md"


@pytest.fixture
def bare(tmp_path):
    """The complete prereg as it was before order 21: no pre-flight section, no stamp."""
    shutil.copy(FIXTURES / "complete" / "PREREG.md", tmp_path / "PREREG.md")
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", tmp_path / "FINDINGS.md")
    give_review(tmp_path / "PREREG.md")
    return tmp_path / "PREREG.md"


# ---------------------------------------------- the eight preconditions, blocking

def test_p1_blocks_when_there_is_no_review_stamp(unsealed):
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P1_REVIEW_STAMP"], res.stderr
    assert not (unsealed.parent / "RUN_TOKEN.json").exists()


def test_p2_blocks_when_the_reviewer_is_the_author(unsealed):
    """A seal reviewed by the person who wrote it is an unreviewed seal."""
    res = run("stamp", unsealed, "--reviewer", "odin", "--findings", "FINDINGS.md")
    assert res.returncode == EXIT_OK, res.stderr
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    # order 47: odin is not the assigned reviewer (Alice) either, so P9 refuses too
    assert failed_preconditions(res.stderr) == ["P2_REVIEWER_NOT_AUTHOR",
                                                "P9_ASSIGNED_REVIEWER"], res.stderr
    assert "odin" in res.stderr.lower()


def test_p3_blocks_when_a_finding_has_no_disposition(unsealed):
    findings = unsealed.parent / "FINDINGS.md"
    text = findings.read_text()
    text = text.replace(
        "  DISPOSITION: ACCEPTED — planted_positive is in the controls block and ran.", "")
    findings.write_text(text)
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P3_FINDINGS_DISPOSITIONED"], res.stderr
    assert "F3" in res.stderr


def test_p3_blocks_when_declined_has_no_written_reason(unsealed):
    findings = unsealed.parent / "FINDINGS.md"
    text = findings.read_text()
    text = re.sub(r"DISPOSITION: DECLINED:.*?(?=\n\n)", "DISPOSITION: DECLINED:",
                  text, flags=re.S)
    findings.write_text(text)
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P3_FINDINGS_DISPOSITIONED"], res.stderr
    assert "F2" in res.stderr


def test_p4_blocks_when_a_skipped_control_has_no_written_reason(sealed):
    text = sealed.read_text()
    text = text.replace("| nothing is ranked in this run  |", "|                                |")
    sealed.write_text(text)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P4_CONTROLS_CHECKLIST"], res.stderr
    assert "random_ranking_arm" in res.stderr


def test_p4_blocks_when_a_control_row_is_missing_entirely(sealed):
    text = sealed.read_text()
    text = "\n".join(l for l in text.splitlines() if "influence_test" not in l)
    sealed.write_text(text)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P4_CONTROLS_CHECKLIST"], res.stderr
    assert "influence_test" in res.stderr


# Order 75 (item 14): planted_positive is the one control that may not be skipped, and it must
# have PASSed. A rig that has never caught a plant cannot report a null.
PLANTED_ROW = "| planted_positive    | ran           | 2.0080 vs 0.0080 PASS                |"


def with_planted_row(prereg, status, result, reason=""):
    text = prereg.read_text()
    assert PLANTED_ROW in text
    prereg.write_text(text.replace(
        PLANTED_ROW + "                                |",
        f"| planted_positive    | {status} | {result} | {reason} |"))
    return run("check", prereg)


def p4_line(out):
    return next(l for l in out.splitlines() if "P4_CONTROLS_CHECKLIST:" in l)


def test_p4_blocks_when_planted_positive_is_skipped_even_with_a_reason(sealed):
    res = with_planted_row(sealed, "skipped", "", "the rig is new, no plant was made")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P4_CONTROLS_CHECKLIST"], res.stderr
    assert ("planted_positive: may not be skipped — a rig that has never caught a plant "
            "cannot report a null (item 14)") in res.stderr, res.stderr


def test_p4_passes_when_planted_positive_ran_and_passed(sealed):
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert p4_line(res.stdout).startswith("ok"), res.stdout
    assert p4_line(res.stdout).endswith(", positive control PASS"), res.stdout


@pytest.mark.parametrize("cell", [
    "2.0080 vs 0.0080 FAIL",
    "0.0000 vs 0.0080 INDETERMINATE",
    "",
    "2.0080 vs 0.0080 PASSED",
    "2.0080 vs 0.0080 pass",
    "PASS then FAIL on rerun",
])
def test_p4_blocks_when_planted_positive_ran_without_a_clean_pass(sealed, cell):
    res = with_planted_row(sealed, "ran", cell)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P4_CONTROLS_CHECKLIST"], res.stderr
    line = p4_line(res.stderr)
    assert "planted_positive" in line and repr(cell) in line, line


def test_p4_still_lets_another_control_be_skipped_with_a_reason(sealed):
    """The unchanged rule: random_ranking_arm is skipped with a reason in the fixture."""
    assert "| random_ranking_arm  | skipped       |" in sealed.read_text()
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "6 ran, 1 skipped with reasons, positive control PASS" in p4_line(res.stdout)


def test_p5_blocks_when_the_stop_conditions_section_is_missing(sealed):
    text = sealed.read_text().replace("## STOP CONDITIONS", "## Some other heading")
    sealed.write_text(text)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P5_BANDS_AND_STOP_CONDITIONS"], res.stderr
    assert "stop condition" in res.stderr.lower()


def test_p5_blocks_when_the_bands_section_is_missing(sealed):
    text = sealed.read_text().replace("## BANDS — each names", "## Expectations — each names")
    sealed.write_text(text)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P5_BANDS_AND_STOP_CONDITIONS"], res.stderr
    assert "band" in res.stderr.lower()


def test_p6_blocks_when_the_stamp_block_is_not_unique(sealed):
    """Two stamp blocks make "the content outside the stamp" ambiguous.

    It is also the obvious way to hide an edit: bury the changed text inside a
    second block that the hash excludes.
    """
    text = sealed.read_text()
    block = re.search(r"<!-- SEALGATE-STAMP BEGIN -->.*?<!-- SEALGATE-STAMP END -->",
                      text, flags=re.S).group(0)
    sealed.write_text(text + "\n\n" + block + "\n")
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P6_CONTENT_HASH"], res.stderr
    assert "2" in res.stderr


# ------------------------------------------------------------- the happy path

def test_happy_path_issues_a_run_token(sealed):
    res = run("token", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token_path = sealed.parent / "RUN_TOKEN.json"
    assert token_path.exists()
    token = json.loads(token_path.read_text())
    assert token["prereg"].endswith("PREREG.md")
    assert token["reviewer"] == "Alice"
    assert token["author_line"]
    assert len(token["body_sha256"]) == 64
    assert len(token["findings_sha256"]) == 64
    assert token["n_findings"] == 3
    assert token["section_sha256"], "sections must be hashed so drift can be named"
    for p in ALL_PRECONDITIONS:
        assert token["preconditions"][p] == "ok", token["preconditions"]


def test_check_reports_ok_without_writing_a_token(sealed):
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert not (sealed.parent / "RUN_TOKEN.json").exists(), "check must not issue anything"


# ------------------------------------------------------------------ the drift

def test_verify_passes_immediately_after_the_token_is_issued(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    res = run("verify", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_verify_catches_a_one_character_edit_to_the_bands_section(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    text = sealed.read_text()
    assert "exceeds 0.72." in text
    sealed.write_text(text.replace("exceeds 0.72.", "exceeds 0.62.", 1))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "BANDS" in res.stderr, res.stderr
    assert "STOP CONDITIONS" not in res.stderr.replace("BANDS", ""), \
        "drift must name the section that changed, not every section"


def test_verify_catches_the_findings_file_edited_after_the_token(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    findings = sealed.parent / "FINDINGS.md"
    findings.write_text(findings.read_text().replace("DISPOSITION: DECLINED:",
                                                     "DISPOSITION: ADDRESSED —", 1))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "findings" in res.stderr.lower()


def test_verify_catches_the_reviewer_being_swapped_in_the_stamp(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    sealed.write_text(sealed.read_text().replace("reviewer: Alice", "reviewer: Bob"))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "reviewer" in res.stderr.lower()


def test_verify_refuses_when_there_is_no_token(sealed):
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "RUN_TOKEN.json" in res.stderr


def test_token_does_not_overwrite_an_existing_token(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    first = (sealed.parent / "RUN_TOKEN.json").read_text()
    sealed.write_text(sealed.read_text().replace("exceeds 0.72.", "exceeds 0.62.", 1))
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert (sealed.parent / "RUN_TOKEN.json").read_text() == first, \
        "re-issuing a token must not erase the commitment the first one made"


def test_stamp_does_not_silently_replace_an_existing_stamp(sealed):
    res = run("stamp", sealed, "--reviewer", "Bob", "--findings", "FINDINGS.md")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "Alice" in sealed.read_text(), "the original stamp must survive"


def test_stamp_refuses_a_findings_file_that_does_not_exist(unsealed):
    res = run("stamp", unsealed, "--reviewer", "Alice", "--findings", "NOPE.md")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "NOPE.md" in res.stderr


# ------------------------------------------------------------ the refusals log

def test_every_refusal_appends_exactly_one_line_to_the_log(unsealed):
    before = len(log_lines())
    assert run("token", unsealed).returncode == EXIT_REFUSED
    after = log_lines()
    assert len(after) == before + 1, "one refusal, one line"
    line = after[-1]
    assert "P1_REVIEW_STAMP" in line
    assert str(unsealed) in line
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", line), "line must start with a timestamp"


def test_a_blessed_token_writes_nothing_to_the_refusals_log(sealed):
    before = len(log_lines())
    assert run("token", sealed).returncode == EXIT_OK
    assert len(log_lines()) == before, "the gate logs refusals, not blessings"


def test_order41_the_suite_logs_refusals_to_a_temp_log_not_the_live_one(tmp_path):
    """The refusal is real and logged, in the session's temp log; the live log never sees it."""
    live = HERE / "refusals.log"
    assert REFUSALS_LOG.parent != HERE
    unsealed = tmp_path / "PREREG.md"
    shutil.copy(FIXTURES / "real_copy" / "PREREG.md", unsealed)
    assert run("token", unsealed).returncode == EXIT_REFUSED
    assert str(unsealed) in REFUSALS_LOG.read_text()
    assert not live.exists() or str(unsealed) not in live.read_text()


# ------------------------------------------- agreement with order 01's artifact

def test_required_controls_match_order_01_seal_checklist():
    """The seven control names are order 01's, read from its checklist, never edited."""
    checklist = HERE.parent / "controls" / "SEAL_CHECKLIST.md"
    if not checklist.exists():
        pytest.skip(f"{checklist} not present")
    names = re.findall(r"^\|\s*([a-z_]+)\s*\|", checklist.read_text(), flags=re.M)
    names = [n for n in names if n != "control"]
    sys.path.insert(0, str(HERE))
    import sealgate
    assert sorted(names) == sorted(sealgate.REQUIRED_CONTROLS), (names, sealgate.REQUIRED_CONTROLS)


# --------------------------------------------------------- the real-world canary

def test_the_real_prereg_copy_is_refused():
    """No existing pre-registration in this programme has a review stamp.

    If this ever passes, the gate is not checking anything.
    """
    real = FIXTURES / "real_copy" / "PREREG.md"
    before = real.read_bytes()
    res = run("check", real)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "P1_REVIEW_STAMP" in failed_preconditions(res.stderr), res.stderr
    assert real.read_bytes() == before, "the gate must never write to a prereg it is checking"


# ---------------------------------------------------------- no escape hatch

def test_the_gate_has_no_escape_hatch():
    """Acceptance criterion 7, asserted in the suite as well as at the command line.
    Order 47: review.py is scanned too. Order 48: veto.py and sandbox.py too."""
    for path in (SEALGATE, HERE / "review.py", VETO_DIR / "veto.py", VETO_DIR / "sandbox.py"):
        src = path.read_text()
        hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
                if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
        assert hits == [], f"escape hatch introduced in {path.name}: {hits}"


def test_missing_file_is_a_usage_error_not_a_blessing(tmp_path):
    res = run("check", tmp_path / "does_not_exist.md")
    assert res.returncode == EXIT_USAGE, res.stdout + res.stderr
    assert "does_not_exist.md" in res.stderr, res.stderr


def test_the_token_records_the_author_line_without_markdown(sealed):
    """`**Author:** Odin` is the convention in this programme's real preregs.

    The token is a record a reviewer reads, so it must say Odin, not '** Odin'.
    """
    assert run("token", sealed).returncode == EXIT_OK
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["author_line"] == "Odin", token["author_line"]
    assert token["authors"] == ["odin"], token["authors"]


# ==========================================================================
# ORDER 03 — the stamp-block seam.
#
# Order 02 hashed the prereg "excluding the stamp block itself", so the stamp
# block was covered by no hash at all: content placed inside it was invisible to
# verify and fully visible to a reader. Odin put a post-hoc band there and the
# gate reported "byte-for-byte ... INTACT", exit 0.
#
# Two mechanisms, tested separately: detection (a whole-file hash) and
# prevention (the stamp block must be canonical).
# ==========================================================================

ODIN_INJECTION = ("- **B4 — added AFTER the token.** Any accuracy above 0.30 "
                  "also counts as a pass.\n")


def inject_into_stamp_block(prereg, line=ODIN_INJECTION):
    """Put a line inside the stamp block, where order 02's hash could not see it."""
    text = prereg.read_text()
    assert "- sealgate: " in text
    prereg.write_text(re.sub(r"^(- sealgate: .*\n)", r"\1" + line, text, count=1, flags=re.M))


def test_odins_attack_a_post_hoc_band_in_the_stamp_block_is_caught(sealed):
    """Acceptance criterion 2, the headline. Exit 4, and the stamp region named."""
    assert run("token", sealed).returncode == EXIT_OK
    inject_into_stamp_block(sealed)
    assert "B4" in sealed.read_text()
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "stamp region" in out.lower(), out
    assert "INTACT" not in out, out
    assert "byte-for-byte" not in out, \
        "the byte-for-byte claim is false the moment the file hash does not match"


def test_the_stamp_region_message_says_the_body_is_intact_but_not_covered(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    inject_into_stamp_block(sealed)
    err = run("verify", sealed).stderr.lower()
    assert "body" in err, err
    assert "after the token" in err, err
    assert "not covered" in err, err


def test_the_injection_is_refused_at_token_time_by_p7(unsealed):
    """Acceptance criterion 3: prevention, not only detection."""
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    inject_into_stamp_block(unsealed)
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P7_STAMP_BLOCK_CANONICAL"], res.stderr
    assert "B4" in res.stderr, "the refusal must name the line it would not accept"
    assert not (unsealed.parent / "RUN_TOKEN.json").exists()


def test_p7_refuses_an_unrecognised_field_in_the_stamp_block(unsealed):
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    inject_into_stamp_block(unsealed, "- note: this looks like a field but is not one\n")
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P7_STAMP_BLOCK_CANONICAL"], res.stderr
    assert "note" in res.stderr


def test_p7_refuses_a_duplicated_field_in_the_stamp_block(unsealed):
    """Two `- reviewer:` lines: the parser would take one, a reader the other."""
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    inject_into_stamp_block(unsealed, "- reviewer: Odin\n")
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P7_STAMP_BLOCK_CANONICAL"], res.stderr
    assert "reviewer" in res.stderr


def test_p7_refuses_a_findings_hash_that_is_not_a_hash(unsealed):
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    text = unsealed.read_text()
    unsealed.write_text(re.sub(r"^- findings-sha256: .*$", "- findings-sha256: not-a-hash",
                               text, count=1, flags=re.M))
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "P7_STAMP_BLOCK_CANONICAL" in failed_preconditions(res.stderr), res.stderr


def test_p7_is_not_evaluated_when_there_is_no_stamp_block(unsealed):
    """So the "exactly one FAILED" assertions on P1 stay true by design."""
    res = run("token", unsealed)
    assert res.returncode == EXIT_REFUSED
    assert failed_preconditions(res.stderr) == ["P1_REVIEW_STAMP"], res.stderr
    assert re.search(r"^not-evaluated\s+P7_STAMP_BLOCK_CANONICAL", res.stderr, re.M), res.stderr


def test_a_stamp_written_by_sealgate_is_canonical(sealed):
    """P7 must not refuse the gate's own output."""
    res = run("token", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["preconditions"]["P7_STAMP_BLOCK_CANONICAL"] == "ok"


def test_the_token_records_a_whole_file_hash_at_schema_2(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["schema"] == "sealgate/run-token/2", token["schema"]
    assert len(token["file_sha256"]) == 64
    assert token["file_sha256"] != token["body_sha256"], \
        "the file hash covers the stamp block, the body hash does not"
    assert token["file_sha256"] == hashlib.sha256(sealed.read_bytes()).hexdigest()


def test_the_intact_path_still_claims_byte_for_byte(sealed):
    """Acceptance criterion 4."""
    assert run("token", sealed).returncode == EXIT_OK
    res = run("verify", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "byte-for-byte" in res.stdout
    assert "INTACT" in res.stdout


def test_a_whitespace_only_edit_in_the_body_is_drift(sealed):
    """Acceptance criterion 8: the body hash is over RAW BYTES, not normalised text."""
    assert run("token", sealed).returncode == EXIT_OK
    text = sealed.read_text()
    assert "exceeds 0.72.\n" in text
    sealed.write_text(text.replace("exceeds 0.72.\n", "exceeds 0.72.   \n", 1))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "DRIFT" in res.stderr, res.stderr
    assert "BANDS" in res.stderr, res.stderr
    assert "byte-for-byte" not in res.stdout + res.stderr


def test_a_schema_1_token_is_refused_loudly(sealed):
    """Acceptance criterion 6. No fallback to body-only verification."""
    assert run("token", sealed).returncode == EXIT_OK
    token_path = sealed.parent / "RUN_TOKEN.json"
    token = json.loads(token_path.read_text())
    del token["file_sha256"]
    token["schema"] = "sealgate/run-token/1"
    token_path.write_text(json.dumps(token, indent=2, sort_keys=True))
    res = run("verify", sealed)
    assert res.returncode != EXIT_OK, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "INTACT" not in out, "a schema-1 token must never produce an INTACT claim"
    assert "byte-for-byte" not in out
    assert "re-issue" in out.lower() or "reissue" in out.lower(), out
    assert "schema" in out.lower()


def test_a_token_with_an_unknown_schema_is_refused(sealed):
    assert run("token", sealed).returncode == EXIT_OK
    token_path = sealed.parent / "RUN_TOKEN.json"
    token = json.loads(token_path.read_text())
    token["schema"] = "sealgate/run-token/99"
    token_path.write_text(json.dumps(token, indent=2, sort_keys=True))
    res = run("verify", sealed)
    assert res.returncode != EXIT_OK, res.stdout + res.stderr
    assert "INTACT" not in res.stdout + res.stderr


def test_body_drift_still_names_the_section(sealed):
    """Acceptance criterion 5: order 02's regression, unchanged."""
    assert run("token", sealed).returncode == EXIT_OK
    sealed.write_text(sealed.read_text().replace("exceeds 0.72.", "exceeds 0.62.", 1))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "DRIFT" in res.stderr
    assert "BANDS" in res.stderr
    assert "byte-for-byte" not in res.stdout + res.stderr


# ==========================================================================
# ORDER 04 — the findings file must be hashed over its RAW BYTES.
#
# Order 03 moved the pre-registration to raw-byte hashing but left the findings
# file hashed as a decoded view:
#   sha256(findings_path.read_text(encoding="utf-8", errors="replace"))
# Two byte changes survive that view untouched:
#   1. errors="replace" — different invalid-UTF-8 bytes decode to the same U+FFFD
#   2. read_text() translates newlines — LF -> CRLF decodes to the same text
# Same class as order 03: the witness hashes a view of the evidence, not the
# evidence.
# ==========================================================================

def sealed_with_findings_bytes(tmp_path, transform=lambda b: b):
    """A stamped prereg whose FINDINGS.md bytes are `transform`ed before stamping."""
    shutil.copy(FIXTURES / "complete" / "PREREG.md", tmp_path / "PREREG.md")
    findings = tmp_path / "FINDINGS.md"
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", findings)
    findings.write_bytes(transform(findings.read_bytes()))
    prereg = tmp_path / "PREREG.md"
    add_preflight(prereg)
    give_review(prereg)
    res = run("stamp", prereg, "--reviewer", "Alice", "--findings", "FINDINGS.md")
    assert res.returncode == EXIT_OK, res.stderr
    return prereg


def with_invalid_utf8(raw, byte=b"\xff"):
    """Put one invalid byte in a finding's prose, where it decodes to U+FFFD."""
    assert b"No positive control" in raw
    return raw.replace(b"No positive control", b"No positive" + byte + b"control")


def test_the_two_invalid_bytes_really_do_decode_the_same():
    """The premise of the next two tests, asserted rather than assumed."""
    assert b"\xff".decode("utf-8", errors="replace") == "�"
    assert b"\xfe".decode("utf-8", errors="replace") == "�"
    assert b"\xff" != b"\xfe"


def test_an_invalid_utf8_byte_swap_in_the_findings_file_is_caught(tmp_path):
    """Acceptance criterion 2. \\xff -> \\xfe: same decoded text, different bytes."""
    prereg = sealed_with_findings_bytes(tmp_path, with_invalid_utf8)
    assert run("token", prereg).returncode == EXIT_OK
    findings = tmp_path / "FINDINGS.md"
    before = findings.read_bytes()
    findings.write_bytes(before.replace(b"\xff", b"\xfe"))
    assert findings.read_bytes() != before
    assert (findings.read_bytes().decode("utf-8", errors="replace")
            == before.decode("utf-8", errors="replace")), \
        "the point of this test is that the decoded view is identical"
    res = run("verify", prereg)
    assert res.returncode != EXIT_OK, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "FINDINGS.md" in out, out
    assert "findings" in out.lower(), out
    assert "INTACT" not in out, out


def test_newline_translation_in_the_findings_file_is_caught(tmp_path):
    """Acceptance criterion 3. LF -> CRLF: read_text() hides it, the bytes do not."""
    prereg = sealed_with_findings_bytes(tmp_path)
    assert run("token", prereg).returncode == EXIT_OK
    findings = tmp_path / "FINDINGS.md"
    before = findings.read_bytes()
    assert b"\r\n" not in before, "fixture must start LF-only"
    findings.write_bytes(before.replace(b"\n", b"\r\n"))
    assert findings.read_text() == before.decode("utf-8"), \
        "read_text translates newlines, which is exactly why hashing it is wrong"
    res = run("verify", prereg)
    assert res.returncode != EXIT_OK, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "FINDINGS.md" in out, out
    assert "INTACT" not in out, out


def test_an_invalid_utf8_byte_swap_between_stamp_and_token_is_refused(tmp_path):
    """Acceptance criterion 4, first edit: caught before a token is ever issued."""
    prereg = sealed_with_findings_bytes(tmp_path, with_invalid_utf8)
    findings = tmp_path / "FINDINGS.md"
    findings.write_bytes(findings.read_bytes().replace(b"\xff", b"\xfe"))
    res = run("token", prereg)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P1_REVIEW_STAMP"], res.stderr
    assert "changed after it was stamped" in res.stderr, res.stderr
    assert not (tmp_path / "RUN_TOKEN.json").exists()


def test_newline_translation_between_stamp_and_token_is_refused(tmp_path):
    """Acceptance criterion 4, second edit."""
    prereg = sealed_with_findings_bytes(tmp_path)
    findings = tmp_path / "FINDINGS.md"
    findings.write_bytes(findings.read_bytes().replace(b"\n", b"\r\n"))
    res = run("token", prereg)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P1_REVIEW_STAMP"], res.stderr
    assert "changed after it was stamped" in res.stderr, res.stderr


def test_the_stamped_findings_hash_is_the_sha256_of_the_raw_bytes(tmp_path):
    """Acceptance criterion 5's independent check, on a file where the two differ."""
    prereg = sealed_with_findings_bytes(tmp_path, with_invalid_utf8)
    findings = tmp_path / "FINDINGS.md"
    raw = findings.read_bytes()
    byte_hash = hashlib.sha256(raw).hexdigest()
    text_hash = hashlib.sha256(raw.decode("utf-8", errors="replace").encode("utf-8")).hexdigest()
    assert byte_hash != text_hash, "fixture must be one where the two views disagree"
    stamp = re.search(r"^- findings-sha256: ([0-9a-f]{64})$", prereg.read_text(), re.M)
    assert stamp, prereg.read_text()
    assert stamp.group(1) == byte_hash, "the stamp must record the bytes, not the view"
    assert run("token", prereg).returncode == EXIT_OK
    token = json.loads((tmp_path / "RUN_TOKEN.json").read_text())
    assert token["findings_sha256"] == byte_hash


def test_the_intact_path_still_works_with_byte_hashed_findings(sealed):
    """Acceptance criterion 5: regression, not a new feature."""
    assert run("token", sealed).returncode == EXIT_OK
    res = run("verify", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "INTACT" in res.stdout
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["findings_sha256"] == hashlib.sha256(
        (sealed.parent / "FINDINGS.md").read_bytes()).hexdigest()


def test_no_findings_hash_is_computed_from_decoded_text():
    """Every read_text() result in the source must stay away from a hash call."""
    src = SEALGATE.read_text()
    offenders = [(i, l) for i, l in enumerate(src.splitlines(), 1)
                 if "read_text" in l and re.search(r"sha256|hash", l)]
    assert offenders == [], f"a hash is being computed from decoded text: {offenders}"


# ==========================================================================
# ORDER 21 — P8_PREFLIGHT: no run token without Alice's pre-flight brief.
#
# The seal body names the brief (`Brief:`) and pins its bytes (`Brief-sha256:`) under
# a heading naming the pre-flight. The body hash already covers both lines, so the
# stamp block is unchanged, and `verify` is unchanged: tokens issued before P8 stay valid.
# ==========================================================================

def stamp_and_token(prereg):
    assert run("stamp", prereg, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    return run("token", prereg)


def test_p8_blocks_a_seal_with_no_preflight_section(bare):
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "pre-flight" in res.stderr.lower()
    assert not (bare.parent / "RUN_TOKEN.json").exists()


def test_p8_blocks_when_the_brief_file_does_not_exist(bare):
    add_preflight(bare, brief_rel="PREFLIGHT_nowhere.md")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "PREFLIGHT_nowhere.md" in res.stderr


def test_p8_blocks_when_the_brief_bytes_changed_after_sealing(unsealed):
    """Even a legitimate `## Odin's additions` changes the bytes the seal pinned."""
    brief = unsealed.parent / BRIEF_NAME
    with open(brief, "a", encoding="utf-8") as fh:
        fh.write("\n## Odin's additions\n- Smith 1999 — found later\n")
    assert preflight.check_brief(brief.read_bytes())[0] == [], "the brief itself is still valid"
    res = stamp_and_token(unsealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "sha256" in res.stderr.lower()


def test_p8_blocks_a_brief_that_fails_preflight_check(bare):
    """Hash pinned correctly, but sections 1-6 were edited after the program wrote them."""
    brief = write_brief(bare.parent)
    brief.write_bytes(brief.read_bytes().replace(b"\nGO\n", b"\nGO \n"))
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    with open(bare, "a", encoding="utf-8") as fh:
        fh.write(f"\n## PRE-FLIGHT\nBrief: {BRIEF_NAME}\nBrief-sha256: {digest}\n")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "Brief-body-sha256" in res.stderr


def test_p8_blocks_a_no_go_verdict(bare):
    add_preflight(bare, verdict="NO-GO")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "NO-GO" in res.stderr


def test_p8_blocks_reshape_without_a_reshape_section(bare):
    add_preflight(bare, verdict=f"RESHAPE: {RESHAPE_LINE}")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "RESHAPE" in res.stderr


def test_p8_blocks_reshape_whose_section_does_not_quote_the_line(bare):
    add_preflight(bare, verdict=f"RESHAPE: {RESHAPE_LINE}",
                  extra="\n## RESHAPE\nWe changed some things.\n")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr


def test_p8_allows_reshape_when_the_reshape_section_quotes_the_line(bare):
    add_preflight(bare, verdict=f"RESHAPE: {RESHAPE_LINE}",
                  extra=f"\n## RESHAPE after the pre-flight\n> {RESHAPE_LINE}\n"
                        f"The claim is now restricted accordingly.\n")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((bare.parent / "RUN_TOKEN.json").read_text())
    assert token["preconditions"]["P8_PREFLIGHT"] == "ok"


def test_p8_blocks_a_brief_whose_searcher_is_an_author_of_the_seal(bare):
    add_preflight(bare, searcher="Odin")
    res = stamp_and_token(bare)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P8_PREFLIGHT"], res.stderr
    assert "odin" in res.stderr.lower()


def test_p8_ok_is_recorded_in_the_token(sealed):
    res = run("token", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["preconditions"]["P8_PREFLIGHT"] == "ok"
    assert token["preflight"]["brief"] == BRIEF_NAME
    assert token["preflight"]["brief_sha256"] == hashlib.sha256(
        (sealed.parent / BRIEF_NAME).read_bytes()).hexdigest()
    assert token["preflight"]["verdict"] == "GO"
    assert set(token["preconditions"]) == set(ALL_PRECONDITIONS)


def test_check_says_all_ten(sealed):
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "all ten preconditions" in res.stdout, res.stdout
    assert "P8_PREFLIGHT" in res.stdout
    assert "P9_ASSIGNED_REVIEWER" in res.stdout
    assert "P10_ALICE_VETO" in res.stdout


def test_the_brief_path_and_hash_are_covered_by_the_body_hash(sealed):
    """Swapping the pinned brief after the token is body drift; no stamp change needed."""
    assert run("token", sealed).returncode == EXIT_OK
    text = sealed.read_text()
    sealed.write_text(re.sub(r"^Brief-sha256: [0-9a-f]{64}$", "Brief-sha256: " + "0" * 64,
                             text, count=1, flags=re.M))
    res = run("verify", sealed)
    assert res.returncode == EXIT_DRIFT, res.stdout + res.stderr
    assert "PRE-FLIGHT" in res.stderr


def test_a_token_issued_before_p8_still_verifies(bare, tmp_path):
    """Existing tokens stay valid: verify is unchanged.

    The token is issued by sealgate.py AS OF COMMIT 8cb65ad (order 04, before P8), on a
    seal with no pre-flight at all, then verified by the current sealgate.py.
    """
    old = tmp_path / "old_sealgate" / "sealgate.py"
    old.parent.mkdir()
    src = subprocess.run(["git", "-C", str(HERE), "show", "8cb65ad:sealgate.py"],
                         capture_output=True, check=True).stdout
    old.write_bytes(src)
    run_old = lambda *a: subprocess.run([sys.executable, str(old), *map(str, a)],
                                        capture_output=True, text=True)
    assert run_old("stamp", bare, "--reviewer", "Alice",
                   "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run_old("token", bare)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "P8_PREFLIGHT" not in json.loads((bare.parent / "RUN_TOKEN.json").read_text())[
        "preconditions"]
    res = run("verify", bare)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "INTACT" in res.stdout


def test_p8_has_no_date_exemption_or_waiver():
    src = SEALGATE.read_text()
    p8 = src[src.index("def check_p8"):src.index("def run_checks")]
    assert not re.search(r"datetime|date\.|waive|exempt|environ|argv", p8), \
        "P8 must have no waiver, no date-based exemption and no flag"


# ------------------------------------------------------------- order 27: UTC

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_order27_a_new_token_is_issued_at_utc_z(sealed):
    import datetime
    assert run("token", sealed).returncode == EXIT_OK
    issued = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())["issued_at"]
    assert UTC_Z.match(issued), issued
    age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(issued)
    assert abs(age.total_seconds()) < 120, issued


def test_order27_a_token_with_an_old_naive_issued_at_still_verifies(sealed):
    """issued_at is not hashed anywhere: an old token (naive local time) verifies as before."""
    assert run("token", sealed).returncode == EXIT_OK
    token_path = sealed.parent / "RUN_TOKEN.json"
    token = json.loads(token_path.read_text())
    token["issued_at"] = "2026-09-23T23:03:25"
    token_path.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n")
    res = run("verify", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "token issued at 2026-09-23T23:03:25" in res.stdout


# ==========================================================================
# ORDER 47 — P9_ASSIGNED_REVIEWER: the stamp's reviewer is the one review.py ASSIGNED, that
# reviewer's raw review is on file and still hashes to the ledger, every finding id it raised is
# in the stamped findings file, and the ledger chain is intact up to those lines.
# ==========================================================================

def seal_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    shutil.copy(FIXTURES / "complete" / "PREREG.md", d / "PREREG.md")
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", d / "FINDINGS.md")
    add_preflight(d / "PREREG.md")
    return d / "PREREG.md"


def stamp_as(prereg, reviewer):
    assert run("stamp", prereg, "--reviewer", reviewer,
               "--findings", "FINDINGS.md").returncode == EXIT_OK


def test_p9_all_good_issues_a_token(tmp_path):
    """Positive control for the positive controls: assigned, reviewed, stamped by that reviewer."""
    p = seal_dir(tmp_path, "s")
    assert give_review(p) == "Alice"
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((p.parent / "RUN_TOKEN.json").read_text())
    assert token["preconditions"]["P9_ASSIGNED_REVIEWER"] == "ok"
    assert token["review"]["assigned"] == "Alice"
    assert token["review"]["findings"] == ["F1", "F2", "F3"]


def test_p9_refuses_a_stamp_by_someone_other_than_the_assigned_reviewer(tmp_path):
    review.assign(seal_dir(tmp_path, "first"), LEDGER)            # Alice
    p = seal_dir(tmp_path, "second")
    assert give_review(p) == "Claude-cold"
    stamp_as(p, "Alice")                                          # the friendlier one
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert "Claude-cold" in res.stderr
    assert not (p.parent / "RUN_TOKEN.json").exists()


def test_p9_refuses_when_the_raw_review_was_edited_after(tmp_path):
    p = seal_dir(tmp_path, "s")
    give_review(p)
    raw = next(p.parent.glob("REVIEW_Alice_*.md"))
    raw.write_text(raw.read_text().replace("[F3] No positive control.\n", ""))
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert raw.name in res.stderr


def test_p9_refuses_when_the_raw_review_is_gone(tmp_path):
    """Order 47b: the nightly line forgives a missing raw review; P9 never does."""
    p = seal_dir(tmp_path, "s")
    give_review(p)
    raw = next(p.parent.glob("REVIEW_Alice_*.md"))
    raw.unlink()
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert "does not exist" in res.stderr
    assert not (p.parent / "RUN_TOKEN.json").exists()


def test_p9_refuses_when_a_finding_id_is_missing_from_the_findings_file(tmp_path, monkeypatch):
    monkeypatch.setitem(review.REVIEWERS, "Alice",
                        fake_reviewer(FAKE_REPLY + "[F4] The bar was chosen after a pilot.\n"))
    p = seal_dir(tmp_path, "s")
    give_review(p)
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert "F4" in res.stderr


def test_p9_refuses_a_seal_with_no_assignment(tmp_path):
    p = seal_dir(tmp_path, "s")
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert "assign" in res.stderr


def test_p9_refuses_an_assignment_with_no_review(tmp_path):
    p = seal_dir(tmp_path, "s")
    review.assign(p, LEDGER)
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr


def test_p9_refuses_when_the_chain_is_broken_before_its_lines(tmp_path):
    review.assign(seal_dir(tmp_path, "a"), LEDGER)
    review.assign(seal_dir(tmp_path, "b"), LEDGER)
    p = seal_dir(tmp_path, "s")
    review.assign(p, LEDGER)                                      # Alice again
    review.run(p, LEDGER)
    raw = LEDGER.read_text().splitlines()
    LEDGER.write_text("\n".join([raw[0]] + raw[2:]) + "\n")       # line 2 deleted
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr
    assert "chain" in res.stderr


def test_p9_refuses_a_forged_reassignment(tmp_path):
    """A second assign line naming the other reviewer, appended by hand with a valid chain."""
    p = seal_dir(tmp_path, "s")
    give_review(p)                                                # Alice
    review.append([{"kind": "assign", "seal": review.seal_key(p), "reviewer": "Claude-cold",
                    "repeat": True, "throwaway": True}], LEDGER)
    stamp_as(p, "Alice")
    res = run("token", p)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr


def test_p9_is_not_evaluated_without_a_stamp(tmp_path):
    p = seal_dir(tmp_path, "s")
    res = run("token", p)
    assert failed_preconditions(res.stderr) == ["P1_REVIEW_STAMP"], res.stderr
    assert re.search(r"^not-evaluated\s+P9_ASSIGNED_REVIEWER", res.stderr, re.M), res.stderr


def test_p9_fails_closed_when_review_py_cannot_be_loaded(tmp_path):
    """A copy of sealgate with no review.py beside it: P9 fails, it never passes."""
    gate_dir = tmp_path / "w" / "x" / "gate"     # order 48: deep enough that the veto copy keys
    gate_dir.mkdir(parents=True)                 # the seal by its absolute path, like today's
    shutil.copy(SEALGATE, gate_dir / "sealgate.py")
    shutil.copytree(PREFLIGHT_DIR, gate_dir.parent / "preflight",     # P8's sibling, so only P9
                    ignore=shutil.ignore_patterns("__pycache__", "briefs", "calibration"))
    (gate_dir.parent / "veto").mkdir()                                # P10's sibling (order 48)
    shutil.copy(VETO_DIR / "veto.py", gate_dir.parent / "veto" / "veto.py")
    shim = gate_dir / "shim.py"
    shim.write_text(SHIM.format(gate=str(gate_dir / "sealgate.py"), log=str(tmp_path / "r.log")))
    p = seal_dir(tmp_path, "s")
    give_review(p)
    stamp_as(p, "Alice")
    give_veto(p)
    env = dict(__import__("os").environ, SEALGATE_TEST_LEDGER=str(LEDGER),
               SEALGATE_TEST_VETOES=str(VETOES))
    res = subprocess.run([sys.executable, str(shim), "token", str(p)], capture_output=True,
                         text=True, env=env)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P9_ASSIGNED_REVIEWER"], res.stderr


def test_a_token_issued_before_p9_still_verifies(unsealed, tmp_path):
    """verify is unchanged in meaning. The token is issued by sealgate.py AS OF COMMIT dc17419
    (order 41, the last commit before P9) on a fixture copy, then verified by today's gate."""
    old = tmp_path / "old_sealgate" / "sealgate.py"
    old.parent.mkdir()
    old.write_bytes(subprocess.run(["git", "-C", str(HERE), "show", "dc17419:sealgate.py"],
                                   capture_output=True, check=True).stdout)
    shutil.copytree(PREFLIGHT_DIR, tmp_path / "preflight",
                    ignore=shutil.ignore_patterns("__pycache__", "briefs", "calibration"))
    run_old = lambda *a: subprocess.run([sys.executable, str(old), *map(str, a)],
                                        capture_output=True, text=True)
    assert run_old("stamp", unsealed, "--reviewer", "Alice",
                   "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run_old("token", unsealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((unsealed.parent / "RUN_TOKEN.json").read_text())
    assert "P9_ASSIGNED_REVIEWER" not in token["preconditions"]
    assert "P8_PREFLIGHT" in token["preconditions"]
    res = run("verify", unsealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "INTACT" in res.stdout


def test_p9_has_no_waiver():
    src = SEALGATE.read_text()
    p9 = src[src.index("def check_p9"):src.index("def run_checks")]
    assert not re.search(r"datetime|date\.|waive|exempt|environ|argv|throwaway", p9), \
        "P9 must have no waiver, no date-based exemption, no flag and no throwaway pass"


# ------------------------------------------------- P10, Alice's veto (order 48, 9a)

DETERMINISTIC_BREAK = "echo checking\necho 'BREAK: the band cannot fail'\n"


def test_p10_refuses_a_seal_alice_never_saw(sealed, no_auto_veto):
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"], res.stderr
    assert "Alice has not been run against this seal's current body" in res.stderr
    assert not (sealed.parent / "RUN_TOKEN.json").exists()


def test_p10_no_break_issues_a_token_that_records_the_veto(sealed, no_auto_veto):
    rec = give_veto(sealed)
    assert rec["outcome"] == "NO_BREAK"
    res = run("token", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["preconditions"]["P10_ALICE_VETO"] == "ok"
    assert token["veto"]["outcome"] == "NO_BREAK"
    assert token["veto"]["line"] == rec["seq"]
    assert run("verify", sealed).returncode == EXIT_OK


def test_p10_a_break_that_did_not_reproduce_passes(sealed, no_auto_veto):
    rec = give_veto(sealed, {"break.sh": "echo \"BREAK: $(date +%s%N)\"\n", "BREAK.md": "x\n"})
    assert rec["outcome"] == "BREAK_NOT_REPRODUCED"
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_p10_a_confirmed_break_refuses(sealed, no_auto_veto):
    rec = give_veto(sealed, {"break.sh": DETERMINISTIC_BREAK, "BREAK.md": "x\n"}, judge=True)
    assert rec["outcome"] == "BREAK_CONFIRMED"
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert "BREAK: the band cannot fail" in res.stderr
    assert "one confirmed break vetoes the seal" in res.stderr


def test_p10_a_disputed_break_refuses_and_says_user_decides(sealed, no_auto_veto):
    rec = give_veto(sealed, {"break.sh": DETERMINISTIC_BREAK, "BREAK.md": "x\n"}, judge=False)
    assert rec["outcome"] == "BREAK_DISPUTED"
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert "disputed: Alice and the judge disagree, the user decides" in res.stderr
    assert not (sealed.parent / "RUN_TOKEN.json").exists()


def test_p10_an_invalid_break_passes(sealed, no_auto_veto):
    """Tampering is thrown out: a break that plants hashlib.py is BREAK_INVALID, which passes."""
    rec = give_veto(sealed, {"break.sh": "echo 'import _hashlib' > hashlib.py\n"
                                         "echo 'BREAK: shadowed'\n", "BREAK.md": "x\n"})
    assert rec["outcome"] == "BREAK_INVALID" and rec["why"] == "tamper: hashlib.py", rec
    res = run("token", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((sealed.parent / "RUN_TOKEN.json").read_text())
    assert token["veto"]["outcome"] == "BREAK_INVALID"


def test_p10_a_legacy_break_reproduced_line_still_refuses_its_body(sealed, no_auto_veto):
    """Order 48's two calibration runs are BREAK_REPRODUCED lines. They stay readable and
    still refuse their (old) bodies, even beside a passing run."""
    raw = sealed.read_bytes()
    veto.append([{"kind": "veto", "seal": veto.seal_key(sealed),
                  "body_sha256": veto.body_sha256(raw), "outcome": "BREAK_REPRODUCED",
                  "why": None, "files": {}, "scratch": "/nonexistent",
                  "repro": [{"rc": 0, "last_line": "BREAK: legacy"}] * 2}])
    give_veto(sealed)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert "BREAK: legacy" in res.stderr
    assert "one reproduced break vetoes the seal" in res.stderr


@pytest.mark.parametrize("judge", [True, False])
def test_p10_union_rule_a_later_no_break_does_not_wash_out_a_break(sealed, no_auto_veto, judge):
    give_veto(sealed, {"break.sh": DETERMINISTIC_BREAK, "BREAK.md": "x\n"}, judge=judge)
    give_veto(sealed)
    give_veto(sealed)
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]


def test_p10_failed_is_never_ok(sealed, no_auto_veto):
    def broken(scratch, prompt):
        raise veto.AliceFailed("timed out after 900 s")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(veto, "ALICE", broken)
        assert veto.run(sealed)["outcome"] == "FAILED"
    give_veto(sealed, {})                                   # no output: FAILED too
    res = run("check", sealed)
    assert res.returncode == EXIT_REFUSED
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert "a run that did not finish is not a pass" in res.stderr
    assert "timed out after 900 s" in res.stderr


def test_p10_an_edited_seal_needs_a_new_run(sealed, no_auto_veto):
    give_veto(sealed)
    assert run("check", sealed).returncode == EXIT_OK
    sealed.write_text(sealed.read_text().replace("0.72", "0.70", 1))
    res = run("check", sealed)
    assert failed_preconditions(res.stderr)[-1] == "P10_ALICE_VETO", res.stderr
    assert "current body" in res.stderr


def test_p10_a_break_of_an_older_body_does_not_veto_the_current_one(sealed, no_auto_veto):
    text = sealed.read_text()
    sealed.write_text(text.replace("0.72", "0.70", 1))
    give_veto(sealed, {"break.sh": DETERMINISTIC_BREAK, "BREAK.md": "x\n"}, judge=True)
    sealed.write_text(text)
    give_veto(sealed)
    res = run("check", sealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_p10_a_veto_run_before_the_stamp_does_not_count(unsealed, no_auto_veto):
    """`stamp` appends a blank line outside the stamp block, so stamping changes body_sha256:
    Alice must be run on the seal as stamped."""
    give_veto(unsealed)
    assert run("stamp", unsealed, "--reviewer", "Alice",
               "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run("token", unsealed)
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"], res.stdout + res.stderr
    give_veto(unsealed)
    res = run("token", unsealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_p10_another_seals_veto_does_not_count(sealed, tmp_path, no_auto_veto):
    twin = tmp_path / "twin"
    twin.mkdir()
    for f in sealed.parent.iterdir():
        if f.is_file():
            shutil.copy(f, twin / f.name)
    give_veto(twin / "PREREG.md")                              # same body, other seal
    res = run("check", sealed)
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"], res.stderr


@pytest.mark.parametrize("damage", ["edit", "delete"])
def test_p10_refuses_when_her_recorded_files_moved(sealed, no_auto_veto, damage):
    rec = give_veto(sealed)
    f = Path(rec["scratch"]) / "alice" / "NO_BREAK.md"
    if damage == "edit":
        f.write_text("I broke it, actually.\n")
    else:
        f.unlink()
    res = run("token", sealed)
    assert res.returncode == EXIT_REFUSED
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert ("changed after it was recorded" if damage == "edit" else "does not exist") in res.stderr


def test_p10_refuses_when_the_chain_is_broken(sealed, no_auto_veto):
    give_veto(sealed)
    give_veto(sealed)
    lines = VETOES.read_text().splitlines()
    VETOES.write_text(lines[1] + "\n")                        # the first line removed
    res = run("check", sealed)
    assert failed_preconditions(res.stderr) == ["P10_ALICE_VETO"]
    assert "chain is broken" in res.stderr


def test_p10_fails_closed_when_veto_py_cannot_be_loaded(sealed, tmp_path, no_auto_veto):
    give_veto(sealed)
    spec = __import__("importlib.util").util.spec_from_file_location("sg_p10", SEALGATE)
    sg = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(sg)
    sg.VETOES_LEDGER = VETOES
    assert sg.check_p10(sealed, sealed.read_bytes())[0].status == "ok"
    sg._veto_module = None
    sg.VETO_PY = tmp_path / "no_such_veto.py"
    check, _ = sg.check_p10(sealed, sealed.read_bytes())
    assert check.status == "failed" and "cannot read the veto ledger" in check.detail


def test_p10_has_no_waiver():
    src = SEALGATE.read_text()
    p10 = src[src.index("def check_p10"):src.index("def run_checks")]
    assert not re.search(r"datetime|date\.|waive|exempt|environ|argv|throwaway|overrul|"
                         r"force|override|bypass", p10, flags=re.I), \
        "P10 must have no waiver, no date-based exemption, no flag and no overrule"


def test_a_token_issued_before_p10_still_verifies(unsealed, tmp_path, no_auto_veto):
    """verify is unchanged in meaning. The token is issued by sealgate.py AS OF COMMIT 2963f58
    (order 47/47b, the last commit before P10), with its review.py, on a fixture copy and against
    this test's temp REVIEWS.jsonl, then verified by today's gate."""
    old = tmp_path / "w" / "x" / "old_sealgate"      # its review.py keys the seal like today's
    old.mkdir(parents=True)
    for name in ("sealgate.py", "review.py"):
        (old / name).write_bytes(subprocess.run(
            ["git", "-C", str(HERE), "show", f"2963f58:{name}"],
            capture_output=True, check=True).stdout)
    shutil.copytree(PREFLIGHT_DIR, old.parent / "preflight",
                    ignore=shutil.ignore_patterns("__pycache__", "briefs", "calibration"))
    shim = old / "shim.py"
    shim.write_text(SHIM.replace("mod.VETOES_LEDGER = Path(os.environ[\"SEALGATE_TEST_VETOES\"])\n",
                                 "").format(gate=str(old / "sealgate.py"),
                                            log=str(tmp_path / "old_refusals.log")))
    env = dict(__import__("os").environ, SEALGATE_TEST_LEDGER=str(LEDGER))
    run_old = lambda *a: subprocess.run([sys.executable, str(shim), *map(str, a)],
                                        capture_output=True, text=True, env=env)
    assert run_old("stamp", unsealed, "--reviewer", "Alice",
                   "--findings", "FINDINGS.md").returncode == EXIT_OK
    res = run_old("token", unsealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    token = json.loads((unsealed.parent / "RUN_TOKEN.json").read_text())
    assert "P10_ALICE_VETO" not in token["preconditions"]
    assert "P9_ASSIGNED_REVIEWER" in token["preconditions"]
    assert not vetoed(unsealed)
    res = run("verify", unsealed)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "INTACT" in res.stdout
