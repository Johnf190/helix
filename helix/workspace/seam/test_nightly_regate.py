"""Tests for the nightly, rewired to write the registry only through regate.

Nothing here runs the ANCHOR audit, the cohort step, or the OpTC retry: `score()` and
the output assembly are tested in isolation, with `REG` and `TODAY` monkeypatched onto
the module. The registry is always a COPY.

`sys.dont_write_bytecode` is set before importing, so importing this worktree's nightly
cannot drop a .pyc into `workspace/anchor/`, which is read-only for this order.
"""

import sys

sys.dont_write_bytecode = True

import datetime
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REAL_REG = Path.home() / "helix/workspace/seam/registry/registry.json"
REGATE = Path.home() / "helix/workspace/regate/regate.py"
SEALGATE = Path.home() / "helix/workspace/sealgate/sealgate.py"
LEDGER = "REGATE_LEDGER.jsonl"

KINDS = {"0000": "time", "0001": "time",
         "0002": "experiment", "0003": "experiment", "0004": "experiment"}

# The nightly as it was BEFORE the rewire, pinned to a commit, never to a branch.
# `master` was the before-state only until order 07 merged the rewire into it; after
# that these tests were comparing the new code with itself — three crashed and two
# passed vacuously. A reference point that moves is not a reference point.
BEFORE = "a80ef9f"

# Order 51b: the registry fixture, pinned to a commit. 71496aa is the last seam commit to write
# registry/registry.json before order 51's Part D (632adbe) added the live `depends_on` edges.
REGISTRY_PIN = "71496aa"

sys.path.insert(0, str(HERE))
import nightly  # noqa: E402  — the rewired one, from this worktree


# Order 41: the suite's refusals go to temp logs, never to the live refusals.log beside
# regate.py or sealgate.py. regate runs (the tests' own and the nightly's, through
# nightly.REGATE) go through a shim (a file in a temp dir named regate.py) that loads the real
# regate, points its REFUSALS_LOG and SEALGATE at temp paths, and calls its main(): the code
# and the exit codes are the real thing.
SHIM = """import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location({name!r}, {gate!r})
mod = importlib.util.module_from_spec(spec)
sys.modules[{name!r}] = mod
spec.loader.exec_module(mod)
for key, value in {paths!r}.items():
    setattr(mod, key, Path(value))
sys.exit(mod.main())
"""


def write_shim(directory, gate, **paths):
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / gate.name
    shim.write_text(SHIM.format(name=gate.stem, gate=str(gate),
                                paths={k: str(v) for k, v in paths.items()}))
    return shim


LIVE_REGATE = REGATE
REFUSALS_LOG = None            # regate's temp refusals log, set below


@pytest.fixture(autouse=True, scope="session")
def refusals_to_temp_logs(tmp_path_factory):
    global REGATE, REFUSALS_LOG
    d = tmp_path_factory.mktemp("gates")
    REFUSALS_LOG = d / "regate_refusals.log"
    sealgate_run = write_shim(d / "sealgate", SEALGATE,
                              REFUSALS_LOG=d / "sealgate_refusals.log")
    REGATE = write_shim(d / "regate", LIVE_REGATE, REFUSALS_LOG=REFUSALS_LOG,
                        SEALGATE=sealgate_run)
    was, nightly.REGATE = nightly.REGATE, REGATE
    yield
    nightly.REGATE = was


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def before_nightly(tmp_path):
    """The pre-rewire nightly.py at BEFORE, extracted with git show and imported."""
    d = tmp_path / "before_nightly"
    d.mkdir()
    for f in ("nightly.py", "rung01.py"):
        blob = subprocess.run(["git", "-C", str(HERE), "show", f"{BEFORE}:{f}"],
                              capture_output=True, check=True).stdout
        (d / f).write_bytes(blob)
    return load_from(d / "nightly.py", "nightly_before")


def registry_copy(tmp_path, kinds=False, baseline=False, name="registry.json"):
    reg = tmp_path / name
    reg.write_bytes(subprocess.run(["git", "-C", str(HERE), "show",
                                    f"{REGISTRY_PIN}:registry/registry.json"],
                                   capture_output=True, check=True).stdout)
    if kinds:
        data = json.loads(reg.read_text())
        for eid, kind in KINDS.items():
            data[eid]["predictions"][0]["kind"] = kind
        reg.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    if baseline:
        assert subprocess.run([sys.executable, str(REGATE), "baseline", str(reg)],
                              capture_output=True).returncode == 0
    return reg


def due_now(reg, ids=("0000-p1", "0001-p1"), when="2026-09-01", baseline_after=False):
    """Make chosen predictions due by moving their DUE DATE into the past.

    Not by moving the clock: regate reads the system date, and must — a date it
    accepted from its caller would be a way past S2. So a test that wants a score
    written makes the prediction genuinely due instead.
    """
    data = json.loads(Path(reg).read_text())
    for entry in data.values():
        for pred in entry.get("predictions", []):
            if pred["id"] in ids:
                pred["due"] = when
    Path(reg).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    if baseline_after:
        assert subprocess.run([sys.executable, str(REGATE), "baseline", str(reg)],
                              capture_output=True).returncode == 0
    return reg


def corpus(n_abs=40, abs_broken=20, n_rel=40, rel_broken=4,
           n_gated=40, gated_broken=4, n_ungated=40, ungated_broken=20):
    """rows/seen shaped as audit() and cohort() produce them, all born after the cutoff."""
    rows, seen = [], {}
    def add(prefix, count, broken, cls, gated):
        for i in range(count):
            key = f"workspace|{prefix}{i}.md|path|t{i}"
            rows.append(dict(corpus="workspace", key=key, cls=cls,
                             broken=int(i < broken), gated=gated))
            seen[key] = dict(first="2026-10-01", cls=cls, gated=gated)
    add("abs", n_abs, abs_broken, "d2_abs", 0)
    add("rel", n_rel, rel_broken, "d2_rel", 0)
    add("gat", n_gated, gated_broken, "d2_rel", 1)
    add("ung", n_ungated, ungated_broken, "d2_rel", 0)
    return rows, seen


@pytest.fixture
def at(monkeypatch):
    """Point the nightly at a copy of the registry, on a chosen day."""
    def _at(reg, day):
        monkeypatch.setattr(nightly, "REG", Path(reg))
        monkeypatch.setattr(nightly, "TODAY", datetime.date.fromisoformat(day))
        return Path(reg)
    return _at


# ============================================================ the latent crash

def test_the_current_nightly_crashes_when_a_manual_metric_comes_due(tmp_path):
    """0004-p1's metric is manual_prereg5, due 2027-01-31.

    The pre-rewire score() appends every past-due unscored prediction to its output whether
    or not it knows the metric, and main() then reads p['brier'].
    """
    m = before_nightly(tmp_path)
    m.REG = registry_copy(tmp_path)
    m.TODAY = datetime.date(2027, 2, 1)
    out = m.score([], {})
    # On that night 0000-p1 and 0001-p1 are due too, and both take the UNDERPOWERED
    # branch, so they do get a brier. 0004-p1's metric is one score() does not know,
    # so it comes back with nothing set — and main() reads p['brier'] regardless.
    assert sorted(p["id"] for _, p in out) == ["0000-p1", "0001-p1", "0004-p1"]
    manual = [p for _, p in out if p["id"] == "0004-p1"][0]
    assert "brier" not in manual, "nothing scored it, so there is no brier"
    assert "scored" not in manual
    with pytest.raises(KeyError) as exc:          # main(), line 145, verbatim
        [f"  - **#{eid}** {p['id']}: brier={p['brier']} — {p['note']}" for eid, p in out]
    assert "brier" in str(exc.value)


def test_the_rewired_nightly_reports_it_as_overdue_instead(at, tmp_path):
    reg = at(registry_copy(tmp_path, kinds=True, baseline=True), "2027-02-01")
    scored, refused, overdue = nightly.score([], {})
    assert [(eid, p["id"]) for eid, p in overdue] == [("0004", "0004-p1")]
    lines = nightly.summary_lines(scored, refused, overdue)
    assert "  - 🟡 OVERDUE (manual): #0004 0004-p1, due 2027-01-31" in lines, lines
    assert json.loads(reg.read_text())["0004"]["predictions"][0].get("scored") is None, \
        "a manual metric is never auto-scored"
    # 0000-p1 and 0001-p1 are due on that night too, and regate refuses them, because
    # regate reads the real clock and will not take 2027-02-01 from its caller. That
    # refusal is reported, not worked around — which is the other half of the point.
    assert sorted(p["id"] for _, p, _ in refused) == ["0000-p1", "0001-p1"]
    assert scored == []


def test_the_same_night_side_by_side(at, tmp_path):
    """Identical input, pre-rewire vs rewired: the only difference is the crash."""
    reg_before = due_now(registry_copy(tmp_path, name="m.json"), ids=("0004-p1",))
    m = before_nightly(tmp_path)
    m.REG = Path(reg_before)
    m.TODAY = datetime.date.today()
    out = m.score([], {})
    assert [p["id"] for _, p in out] == ["0004-p1"]
    with pytest.raises(KeyError):
        [f"brier={p['brier']}" for _, p in out]

    reg = at(due_now(registry_copy(tmp_path, kinds=True), ids=("0004-p1",)),
             datetime.date.today().isoformat())
    subprocess.run([sys.executable, str(REGATE), "baseline", str(reg)], capture_output=True)
    scored, refused, overdue = nightly.score([], {})
    assert (scored, refused) == ([], [])
    assert nightly.summary_lines(scored, refused, overdue) == \
        ["  - 🟡 OVERDUE (manual): #0004 0004-p1, due 2026-09-01"]


# ====================================================== scoring, through regate

def test_the_nightly_scores_an_automatic_metric_through_regate(at, tmp_path):
    reg = at(due_now(registry_copy(tmp_path, kinds=True), baseline_after=True),
             datetime.date.today().isoformat())
    before_ledger = len((reg.parent / LEDGER).read_text().splitlines())
    rows, seen = corpus()
    scored, refused, overdue = nightly.score(rows, seen)
    assert refused == [] and overdue == []
    assert sorted(p["id"] for _, p in scored) == ["0000-p1", "0001-p1"]
    written = json.loads(reg.read_text())
    for eid in ("0000", "0001"):
        pred = written[eid]["predictions"][0]
        assert pred["scored"] == datetime.date.today().isoformat(), pred
        assert pred["outcome"] in (0, 1)
        assert pred["brier"] == round((pred["p"] - pred["outcome"]) ** 2, 4)
    after = (reg.parent / LEDGER).read_text().splitlines()
    assert len(after) == before_ledger + 2
    assert all(json.loads(l)["kind"] == "score" for l in after[-2:])
    assert subprocess.run([sys.executable, str(REGATE), "check", str(reg)],
                          capture_output=True).returncode == 0


def test_underpowered_goes_through_regate_too(at, tmp_path):
    """No new references at all: both automatic metrics are UNDERPOWERED."""
    reg = at(due_now(registry_copy(tmp_path, kinds=True), baseline_after=True),
             datetime.date.today().isoformat())
    scored, refused, overdue = nightly.score([], {})
    assert refused == [] and overdue == []
    written = json.loads(reg.read_text())
    for eid in ("0000", "0001"):
        pred = written[eid]["predictions"][0]
        assert pred["outcome"] is None and pred["brier"] is None
        assert pred["note"].startswith("UNDERPOWERED:"), pred["note"]
    assert subprocess.run([sys.executable, str(REGATE), "check", str(reg)],
                          capture_output=True).returncode == 0


def test_the_maths_is_unchanged_from_the_before_commit(at, tmp_path):
    """Same rows in, same outcome and note out. The two metrics are not touched."""
    rows, seen = corpus()
    m = before_nightly(tmp_path)
    m.REG = Path(due_now(registry_copy(tmp_path, name="before_registry.json")))
    m.TODAY = datetime.date.today()
    before_out = {p["id"]: (p["outcome"], p["note"]) for _, p in m.score(rows, seen)}

    at(due_now(registry_copy(tmp_path, kinds=True), baseline_after=True),
       datetime.date.today().isoformat())
    scored, refused, overdue = nightly.score(rows, seen)
    ours = {p["id"]: (p["outcome"], p["note"]) for _, p in scored}
    assert ours == {k: v for k, v in before_out.items() if k in ours}
    assert set(ours) == {"0000-p1", "0001-p1"}


def test_a_refusal_writes_nothing_and_is_reported(at, tmp_path):
    """The goalpost is moved before the nightly runs. regate refuses; nothing is written."""
    reg = Path(due_now(registry_copy(tmp_path, kinds=True), baseline_after=True))
    data = json.loads(reg.read_text())
    data["0000"]["predictions"][0]["min_n"] = 10
    reg.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    at(reg, datetime.date.today().isoformat())
    before_reg, before_led = sha(reg), sha(reg.parent / LEDGER)
    scored, refused, overdue = nightly.score(corpus()[0], corpus()[1])
    assert scored == []
    assert sorted(p["id"] for _, p, _ in refused) == ["0000-p1", "0001-p1"]
    assert "S1_EXISTS_AND_UNDRIFTED" in refused[0][2], refused[0][2]
    assert (sha(reg), sha(reg.parent / LEDGER)) == (before_reg, before_led), \
        "a refused score must leave the registry and the ledger byte-identical"
    lines = nightly.summary_lines(scored, refused, overdue)
    assert lines[0].startswith("  - 🔴 SCORE REFUSED #0000 0000-p1: "), lines[0]


# ============================================================ the registry line

def test_the_registry_line_says_ok_when_the_ledger_agrees(at, tmp_path):
    """CHANGED IN ORDER 10: the line now carries the narrative count as well, taken
    from regate's own summary rather than counted here."""
    at(registry_copy(tmp_path, kinds=True, baseline=True), "2026-09-23")
    assert nightly.registry_line() == "- registry: OK (5 predictions, 0 narratives)"


def test_the_registry_line_names_the_first_problem(at, tmp_path):
    reg = registry_copy(tmp_path, kinds=True, baseline=True)
    data = json.loads(reg.read_text())
    data["0000"]["predictions"][0]["min_n"] = 10
    reg.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    at(reg, "2026-09-23")
    line = nightly.registry_line()
    assert line == "- 🔴 registry: GOALPOST MOVED: 0000-p1 field min_n 30 -> 10", line


def test_the_registry_line_says_no_ledger_and_creates_none(at, tmp_path):
    reg = at(registry_copy(tmp_path), "2026-09-23")
    assert not (reg.parent / LEDGER).exists()
    assert nightly.registry_line() == "- registry: NO LEDGER — not yet baselined"
    assert not (reg.parent / LEDGER).exists(), "the nightly must never baseline anything"


def test_the_registry_line_copes_with_no_registry_at_all(at, tmp_path):
    at(tmp_path / "absent.json", "2026-09-23")
    assert "registry" in nightly.registry_line()


# ==================================================== what must not have changed

def source(name):
    """That file as it was at BEFORE."""
    return subprocess.run(["git", "-C", str(HERE), "show", f"{BEFORE}:{name}"],
                          capture_output=True, text=True, check=True).stdout


def region(text, start, end):
    return text[text.index(start):text.index(end)]


def test_audit_cohort_and_rate_are_byte_identical_to_the_before_commit():
    mine = (HERE / "nightly.py").read_text()
    theirs = source("nightly.py")
    assert region(mine, "def audit():", "def score(") == \
        region(theirs, "def audit():", "def score(")


def test_the_optc_retry_is_byte_identical_to_the_before_commit():
    mine = (HERE / "nightly.py").read_text()
    theirs = source("nightly.py")
    marker = "    # one blocked acquisition is retried here rather than by a human remembering to"
    assert region(mine, marker, "    per_corpus, rows = audit()") == \
        region(theirs, marker, "    per_corpus, rows = audit()")


def test_the_nightly_no_longer_writes_the_registry():
    src = (HERE / "nightly.py").read_text()
    offenders = [l for l in src.splitlines() if "REG.write_text" in l]
    assert offenders == [], offenders


def test_no_flags_or_env_vars_were_added_to_the_nightly():
    src = (HERE / "nightly.py").read_text()
    for forbidden in ("argparse", "os.environ", "getenv", "sys.argv"):
        assert forbidden not in src, forbidden


def test_the_nightly_has_no_escape_hatch():
    src = (HERE / "nightly.py").read_text()
    import re
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], hits


def test_the_live_registry_and_its_ledger_are_never_touched(tmp_path):
    """Since order 07 the live registry HAS a ledger. Neither may move.

    The suite works on copies, so running it must leave both files byte-identical.
    """
    live_ledger = REAL_REG.parent / LEDGER
    assert live_ledger.exists(), "the live registry has been baselined since order 07"
    before_reg, before_led = sha(REAL_REG), sha(live_ledger)
    reg = registry_copy(tmp_path, kinds=True, baseline=True)
    assert reg != REAL_REG and reg.parent != REAL_REG.parent
    nightly.REG = Path(reg)                      # never the live one
    assert (sha(REAL_REG), sha(live_ledger)) == (before_reg, before_led), \
        "the suite must not disturb the live registry or its ledger"


# ==========================================================================
# ORDER 10 — two small fixes from order 09's §4.
#   1. the registry line reports narratives too, parsed from regate's own output
#   2. a reminder before an unsealed experiment comes due
# ==========================================================================

class FakeProc:
    """Stands in for regate's CompletedProcess, so a summary line with no narrative
    count can be exercised without an old regate to hand."""

    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_the_registry_line_reports_the_narrative_count(at, tmp_path):
    reg = at(registry_copy(tmp_path, kinds=True, baseline=True), datetime.date.today().isoformat())
    narrative = reg.parent / "0000_protocol.md"
    narrative.write_text("# 0000 — the protocol\n\nprose.\n")
    assert subprocess.run([sys.executable, str(REGATE), "attach-narrative", str(reg),
                           "0000", "0000_protocol.md"], capture_output=True).returncode == 0
    assert nightly.registry_line() == "- registry: OK (5 predictions, 1 narratives)"


def test_the_registry_line_reports_no_narratives_as_zero_when_regate_says_zero(at, tmp_path):
    at(registry_copy(tmp_path, kinds=True, baseline=True), datetime.date.today().isoformat())
    assert nightly.registry_line() == "- registry: OK (5 predictions, 0 narratives)"


def test_the_registry_line_repeats_regate_when_it_reports_no_narrative_count(at, tmp_path,
                                                                            monkeypatch):
    """If regate's summary has no narrative count, say what regate said — never a
    guessed 0. An older regate, or a changed message, must not become a false claim."""
    at(registry_copy(tmp_path, kinds=True, baseline=True), datetime.date.today().isoformat())
    monkeypatch.setattr(nightly, "regate", lambda *a: FakeProc(
        "OK: /somewhere/registry.json still says what the ledger says it said\n"
        "  5 prediction(s), 7 ledger line(s), chain intact\n"))
    line = nightly.registry_line()
    assert line == "- registry: OK (5 prediction(s), 7 ledger line(s), chain intact)", line
    assert "narrative" not in line
    assert "0 narratives" not in line


def test_a_failing_registry_line_is_unchanged(at, tmp_path):
    reg = registry_copy(tmp_path, kinds=True, baseline=True)
    data = json.loads(reg.read_text())
    data["0000"]["predictions"][0]["min_n"] = 10
    reg.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    at(reg, datetime.date.today().isoformat())
    assert nightly.registry_line() == \
        "- 🔴 registry: GOALPOST MOVED: 0000-p1 field min_n 30 -> 10"


# ------------------------------------------------------------ fix 2: NEEDS SEAL

def due_in(reg, days, pred_id="0004-p1", today=None):
    """Move one prediction's due date to `days` from `today`."""
    today = today or datetime.date.today()
    data = json.loads(Path(reg).read_text())
    for entry in data.values():
        for pred in entry.get("predictions", []):
            if pred["id"] == pred_id:
                pred["due"] = (today + datetime.timedelta(days=days)).isoformat()
    Path(reg).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return reg


def test_needs_seal_warns_at_29_days(at, tmp_path):
    reg = at(due_in(registry_copy(tmp_path, kinds=True), 29),
             datetime.date.today().isoformat())
    due = (datetime.date.today() + datetime.timedelta(days=29)).isoformat()
    assert nightly.needs_seal_lines() == \
        [f"  - 🟡 NEEDS SEAL: #0004 0004-p1 due {due} (29 days), no prereg attached"]


def test_needs_seal_is_quiet_at_31_days(at, tmp_path):
    at(due_in(registry_copy(tmp_path, kinds=True), 31), datetime.date.today().isoformat())
    assert nightly.needs_seal_lines() == []


def test_needs_seal_is_quiet_once_a_prereg_is_attached(at, tmp_path):
    reg = due_in(registry_copy(tmp_path, kinds=True), 29)
    data = json.loads(Path(reg).read_text())
    data["0004"]["predictions"][0]["prereg"] = "somewhere/PREREG.md"
    Path(reg).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    at(reg, datetime.date.today().isoformat())
    assert nightly.needs_seal_lines() == []


def test_needs_seal_is_quiet_for_a_scored_prediction(at, tmp_path):
    reg = due_in(registry_copy(tmp_path, kinds=True), 29)
    data = json.loads(Path(reg).read_text())
    data["0004"]["predictions"][0].update(scored="2026-09-23", outcome=1, brier=0.25, note="x")
    Path(reg).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    at(reg, datetime.date.today().isoformat())
    assert nightly.needs_seal_lines() == []


def test_needs_seal_is_quiet_for_a_time_prediction(at, tmp_path):
    reg = due_in(registry_copy(tmp_path, kinds=True), 29, pred_id="0000-p1")
    at(reg, datetime.date.today().isoformat())
    assert nightly.needs_seal_lines() == [], "0000-p1 is kind time; it needs no seal"


def test_needs_seal_is_quiet_for_a_prediction_with_no_kind(at, tmp_path):
    """An absent kind reads as `time`, the strict reading, so no reminder.

    The kind is removed here rather than assumed absent: registry_copy() copies the
    LIVE registry, and the live registry has declared kinds since order 07, so
    `kinds=False` means "add none", not "has none".
    """
    reg = due_in(registry_copy(tmp_path), 29)
    data = json.loads(Path(reg).read_text())
    for entry in data.values():
        for pred in entry.get("predictions", []):
            pred.pop("kind", None)
    Path(reg).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    assert "kind" not in Path(reg).read_text()
    at(reg, datetime.date.today().isoformat())
    assert nightly.needs_seal_lines() == []


def test_a_past_due_experiment_gets_only_the_overdue_line(at, tmp_path):
    """Past due is OVERDUE (manual); it must not also say NEEDS SEAL."""
    reg = at(due_in(registry_copy(tmp_path, kinds=True), -3, today=datetime.date.today()),
             datetime.date.today().isoformat())
    subprocess.run([sys.executable, str(REGATE), "baseline", str(reg)], capture_output=True)
    scored, refused, overdue = nightly.score([], {})
    lines = nightly.summary_lines(scored, refused, overdue) + nightly.needs_seal_lines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("  - 🟡 OVERDUE (manual): #0004 0004-p1")
    assert not any("NEEDS SEAL" in l for l in lines)


def test_needs_seal_writes_nothing(at, tmp_path):
    reg = at(due_in(registry_copy(tmp_path, kinds=True), 29),
             datetime.date.today().isoformat())
    subprocess.run([sys.executable, str(REGATE), "baseline", str(reg)], capture_output=True)
    before = (sha(reg), sha(reg.parent / LEDGER))
    assert nightly.needs_seal_lines()
    assert (sha(reg), sha(reg.parent / LEDGER)) == before


def test_the_nightly_has_no_escape_hatch_still():
    import re as _re
    src = (HERE / "nightly.py").read_text()
    assert [l for l in src.splitlines()
            if _re.search(r"force|override|bypass|skip_check", l, flags=_re.I)] == []


# ==========================================================================
# ORDER 11 — one line on the fact base, from factgate. Report only: the nightly
# never records, and never writes anything under memory/.
# ==========================================================================

FACTGATE = Path.home() / "helix/workspace/factgate/factgate.py"


def facts_dir(tmp_path, n=3):
    d = tmp_path / "facts"
    d.mkdir()
    for i in range(n):
        (d / f"{i:03d}_fact.md").write_text(f"# fact {i}\n\nsomething true.\n")
    return d


def baseline_facts(d, state):
    assert subprocess.run([sys.executable, str(FACTGATE), "--state-dir", str(state),
                           "baseline", str(d)], capture_output=True).returncode == 0


@pytest.fixture
def facts_at(monkeypatch, tmp_path):
    """Point the nightly at a fact directory and a state directory of our own."""
    def _at(d, state):
        monkeypatch.setattr(nightly, "FACTS", Path(d))
        monkeypatch.setattr(nightly, "FACTGATE_STATE", Path(state))
        return Path(d)
    return _at


def test_the_facts_line_is_ok_when_nothing_moved(facts_at, tmp_path):
    d, state = facts_dir(tmp_path), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    assert nightly.facts_line() == "- facts: OK (3 recorded)"


def test_the_facts_line_counts_new_changed_and_deleted(facts_at, tmp_path):
    d, state = facts_dir(tmp_path, 4), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    (d / "000_fact.md").write_text("# fact 0\n\nreworded.\n")
    (d / "001_fact.md").unlink()
    (d / "900_new.md").write_text("# new\n\nappeared.\n")
    line = nightly.facts_line()
    assert line.startswith("- 🔴 facts: 1 new, 1 changed, 1 deleted off-ledger — "), line
    assert "900_new.md" in line and "000_fact.md" in line and "001_fact.md" in line


def test_the_facts_line_names_at_most_five(facts_at, tmp_path):
    d, state = facts_dir(tmp_path, 8), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    for i in range(8):
        (d / f"{i:03d}_fact.md").write_text(f"# fact {i}\n\nall reworded.\n")
    line = nightly.facts_line()
    assert "0 new, 8 changed, 0 deleted" in line, line
    assert line.count("_fact.md") == 5, line
    assert "…" in line or "..." in line, line


def test_the_facts_line_says_no_ledger_and_creates_none(facts_at, tmp_path):
    d, state = facts_dir(tmp_path), tmp_path / "state"
    state.mkdir()
    facts_at(d, state)
    assert nightly.facts_line() == "- facts: NO LEDGER — not yet baselined"
    assert not (state / "FACTS_LEDGER.jsonl").exists()


def test_the_facts_line_reports_a_tampered_ledger(facts_at, tmp_path):
    d, state = facts_dir(tmp_path), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    p = state / "FACTS_LEDGER.jsonl"
    lines = p.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["size"] = 4242
    lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n")
    assert "LEDGER TAMPERED" in nightly.facts_line()


def test_the_nightly_never_records_a_fact(facts_at, tmp_path):
    """Report only. The directory and the ledger are byte-identical afterwards."""
    d, state = facts_dir(tmp_path), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    (d / "000_fact.md").write_text("# fact 0\n\nreworded.\n")
    before_dir = sorted((p.name, sha(p)) for p in d.iterdir())
    before_led = sha(state / "FACTS_LEDGER.jsonl")
    nightly.facts_line()
    assert sorted((p.name, sha(p)) for p in d.iterdir()) == before_dir
    assert sha(state / "FACTS_LEDGER.jsonl") == before_led


def test_the_nightly_points_at_the_real_fact_base_by_default():
    assert nightly.FACTS == Path.home() / "helix/memory/facts"
    assert nightly.FACTGATE == Path.home() / "helix/workspace/factgate/factgate.py"


# ==========================================================================
# ORDER 22b — one line on INTERIM facts, from `factgate.py interim`, right after the facts
# line. Report only, and it never stops the nightly. Tested against a stub, as order 20's
# vault_line() was, plus one run of the real factgate on a temp directory.
# ==========================================================================

def stub_factgate(tmp_path, body):
    p = tmp_path / "factgate_stub.py"
    p.write_text("import sys\n" + body + "\n")
    return p


def test_the_interim_line_repeats_factgates_summary(monkeypatch, tmp_path):
    stub = stub_factgate(tmp_path, "print('INTERIM a.md')\nprint('  verdict words: L1 proven')\n"
                                   "print('interim: 3 facts, 1 with verdict words: a.md')")
    monkeypatch.setattr(nightly, "FACTGATE", stub)
    assert nightly.interim_line() == "- interim: 3 facts, 1 with verdict words: a.md"


def test_the_interim_line_passes_the_state_dir_and_the_facts_dir(monkeypatch, tmp_path):
    stub = stub_factgate(tmp_path, "print('interim: ' + ' '.join(sys.argv[1:]))")
    monkeypatch.setattr(nightly, "FACTGATE", stub)
    monkeypatch.setattr(nightly, "FACTGATE_STATE", tmp_path / "st")
    monkeypatch.setattr(nightly, "FACTS", tmp_path)
    assert nightly.interim_line() == f"- interim: --state-dir {tmp_path / 'st'} interim {tmp_path}"


@pytest.mark.parametrize("body", ["sys.exit(1)", "print('nothing useful')",
                                  "raise SystemExit('boom')", "import time; time.sleep(5)"])
def test_the_interim_line_never_stops_the_nightly(monkeypatch, tmp_path, body):
    monkeypatch.setattr(nightly, "FACTGATE", stub_factgate(tmp_path, body))
    monkeypatch.setattr(nightly, "INTERIM_TIMEOUT", 1)
    line = nightly.interim_line()
    assert line.startswith("- interim: unavailable"), line


def test_the_interim_line_with_no_factgate(monkeypatch, tmp_path):
    monkeypatch.setattr(nightly, "FACTGATE", tmp_path / "nope.py")
    assert nightly.interim_line() == "- interim: no factgate"


def test_the_interim_line_with_the_real_factgate_on_a_temp_dir(facts_at, tmp_path):
    d, state = facts_dir(tmp_path), tmp_path / "state"
    state.mkdir()
    baseline_facts(d, state)
    facts_at(d, state)
    (d / "009_new.md").write_text("# decisive\n")
    before = sorted((p.name, sha(p)) for p in list(d.iterdir()) + list(state.iterdir()))
    line = nightly.interim_line()
    assert line.startswith("- interim: 4 facts, 1 with verdict words: 009_new.md"), line
    assert sorted((p.name, sha(p)) for p in list(d.iterdir()) + list(state.iterdir())) == before


def test_the_interim_line_comes_right_after_the_facts_line():
    src = (Path(nightly.__file__)).read_text()
    assert "             facts_line(),\n             interim_line(),\n" in src


# ==========================================================================
# ORDER 30 — one line on the infrastructure, from heartbeat.py, right after the vault line.
# The heartbeat itself is tested in test_heartbeat.py; here only the wiring.
# ==========================================================================

def test_the_heartbeat_line_comes_right_after_the_vault_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             vault_line(),\n"
            "             heartbeat_line(HERE / \"HEARTBEAT.jsonl\"),\n") in src


def test_the_heartbeat_line_is_the_one_from_heartbeat_py():
    import heartbeat
    assert nightly.heartbeat_line is heartbeat.heartbeat_line
    assert Path(heartbeat.__file__).resolve().parent == Path(nightly.__file__).resolve().parent


def test_the_other_lines_keep_their_place_and_shape():
    src = (Path(nightly.__file__)).read_text()
    assert ("             registry_line(),\n             facts_line(),\n"
            "             interim_line(),\n             retired_line(),\n"
            "             categories_line(),\n"
            "             vault_line(),\n") in src
    assert src.count("heartbeat_line") == 2, "one import, one call — nothing else in the nightly"
    # ORDER 36: the gaps line sits between the heartbeat call and the OpTC line, called once
    assert ("             heartbeat_line(HERE / \"HEARTBEAT.jsonl\"),\n"
            "             machine_line(),\n"
            "             gaps_line(),\n"
            "             correction_line(),\n"
            "             house_line(),\n"
            "             canary_line(),\n"
            "             replay_line(),\n"
            "             coldread_line(),\n"
            "             corrections_line(),\n"
            "             reviews_line(),\n"
            "             veto_line(),\n"
            "             seatswap_line(),\n"
            "             provenance_line(),\n"
            "             proposals_line(),\n"
            "             timeforward_line(),\n"
            "             hookcorpus_line(),\n"
            "             f\"- {optc[0]}\"]\n") in src
    assert src.count("gaps_line(),") == 1, "called once, in the lines list"
    # ORDER 37: the correction line sits right after the gaps line, called once; the monthly
    # report is extended onto the end, once
    assert src.count("correction_line(),") == 1, "called once, in the lines list"
    assert src.count("lines.extend(correction_report_lines())") == 1
    # ORDER 38: the house line sits right after the correction line, called once
    assert src.count("house_line(),") == 1, "called once, in the lines list"
    # ORDER 44: the canary line sits right after the house line, called once
    assert src.count("canary_line(),") == 1, "called once, in the lines list"
    # ORDER 45: the replay line sits right after the canary line, called once
    assert src.count("replay_line(),") == 1, "called once, in the lines list"
    # ORDER 46: the coldread line sits right after the replay line, called once
    assert src.count("coldread_line(),") == 1, "called once, in the lines list"
    # ORDER 47: the reviews line sits right after the coldread line, called once
    assert src.count("reviews_line(),") == 1, "called once, in the lines list"
    # ORDER 48: the veto line sits right after the reviews line, called once
    assert src.count("veto_line(),") == 1, "called once, in the lines list"
    # ORDER 49: the seatswap line sits right after the veto line, called once
    assert src.count("seatswap_line(),") == 1, "called once, in the lines list"
    # ORDER 51: the provenance line sits right after the seatswap line, called once
    assert src.count("provenance_line(),") == 1, "called once, in the lines list"
    # ORDER 52: the proposals line sits right after the provenance line, called once
    assert src.count("proposals_line(),") == 1, "called once, in the lines list"
    # ORDER 56: the timeforward line sits right after the proposals line, called once
    assert src.count("timeforward_line(),") == 1, "called once, in the lines list"
    # ORDER 63: the hookcorpus line sits right after the timeforward line, called once
    assert src.count("hookcorpus_line(),") == 1, "called once, in the lines list"
    # ORDER 67: the machine line sits right after the heartbeat line (before gaps), called once
    assert src.count("machine_line(),") == 1, "called once, in the lines list"
    # ORDER 70: the retired line sits right after the interim line (before vault), called once
    assert src.count("retired_line(),") == 1, "called once, in the lines list"
    # ORDER 72: the corrections line sits right after the coldread line (before reviews), called once
    assert src.count("corrections_line(),") == 1, "called once, in the lines list"
    # ORDER 73b: the categories line sits right after the retired line (before vault), called once
    assert src.count("categories_line(),") == 1, "called once, in the lines list"


# ==========================================================================
# ORDER 36 — one line on what the record OMITS, from workspace/gapaudit, after the heartbeat.
# The audit itself is tested in workspace/gapaudit/test_gapaudit.py; here only the wiring.
# ==========================================================================

def test_the_gaps_line_comes_right_after_the_heartbeat_line():
    # ORDER 67: the machine line now sits between them
    src = (Path(nightly.__file__)).read_text()
    assert ("             heartbeat_line(HERE / \"HEARTBEAT.jsonl\"),\n"
            "             machine_line(),\n"
            "             gaps_line(),\n") in src


def test_the_gaps_line_is_the_one_from_gapaudit(monkeypatch):
    import types
    fake = types.SimpleNamespace(gaps_line=lambda: "- gaps: 4 orphans in the last 7 days (new since yesterday: 1)")
    monkeypatch.setitem(sys.modules, "gapaudit", fake)
    assert nightly.gaps_line() == "- gaps: 4 orphans in the last 7 days (new since yesterday: 1)"
    assert nightly.GAPAUDIT == Path.home() / "helix/workspace/gapaudit"


def test_the_gaps_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "gapaudit", types.SimpleNamespace(gaps_line=boom))
    assert nightly.gaps_line() == "- gaps: 🔴 gap audit did not run (RuntimeError: kaboom)"


def test_the_gaps_line_with_no_gapaudit(monkeypatch):
    monkeypatch.setitem(sys.modules, "gapaudit", None)          # import gapaudit → ImportError
    line = nightly.gaps_line()
    assert line.startswith("- gaps: 🔴 gap audit did not run (ModuleNotFoundError: "), line
    assert "gapaudit" in line


def test_the_real_gaps_line_reads_and_writes_nothing_it_should_not(monkeypatch, tmp_path):
    """The real gapaudit, pointed at an empty temp Helix: a line, every source unavailable."""
    monkeypatch.syspath_prepend(str(nightly.GAPAUDIT))
    import gapaudit
    monkeypatch.setattr(gapaudit, "HELIX", tmp_path)
    monkeypatch.setattr(gapaudit.gaps_line, "__defaults__", (tmp_path, None))
    line = nightly.gaps_line()
    assert line.startswith("- gaps: 0 orphans in the last 7 days (new since yesterday: 0)"), line
    assert "runs: source unavailable" in line
    assert list(tmp_path.iterdir()) == []


# ==========================================================================
# ORDER 37 — one line on the correction rate, from workspace/metric, after the gaps line;
# the full report on the first night of the month. The metric itself is tested in
# workspace/metric/test_correction_rate.py; here only the wiring.
# ==========================================================================

def test_the_correction_line_comes_right_after_the_gaps_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             gaps_line(),\n"
            "             correction_line(),\n") in src


def test_the_correction_line_is_the_one_from_the_metric(monkeypatch):
    import types
    seen = []

    def line(today=None):
        seen.append(today)
        return "- correction: 49/225 marked (21.8%)"
    monkeypatch.setitem(sys.modules, "correction_rate", types.SimpleNamespace(correction_line=line))
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 2))
    assert nightly.correction_line() == "- correction: 49/225 marked (21.8%)"
    assert seen == [datetime.date(2026, 10, 2)]
    assert nightly.METRIC == Path.home() / "helix/workspace/metric"


def test_the_correction_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom(today=None):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "correction_rate", types.SimpleNamespace(correction_line=boom))
    assert nightly.correction_line() == "- correction: 🔴 metric did not run (RuntimeError: kaboom)"


def test_the_correction_line_with_no_metric(monkeypatch):
    monkeypatch.setitem(sys.modules, "correction_rate", None)     # import → ImportError
    line = nightly.correction_line()
    assert line.startswith("- correction: 🔴 metric did not run (ModuleNotFoundError: "), line


def test_the_full_report_on_the_first_night_of_the_month_only(monkeypatch):
    import types
    fake = types.SimpleNamespace(compute=lambda today=None: {"today": today},
                                 render=lambda res: f"# Correction-rate report — {res['today']}\n- a\n")
    monkeypatch.setitem(sys.modules, "correction_rate", fake)
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 11, 1))
    assert nightly.correction_report_lines() == ["", "# Correction-rate report — 2026-11-01", "- a"]
    for day in (2, 15, 30):
        monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 11, day))
        assert nightly.correction_report_lines() == []


def test_the_full_report_never_stops_the_nightly(monkeypatch):
    import types

    def boom(today=None):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "correction_rate", types.SimpleNamespace(compute=boom, render=str))
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 11, 1))
    assert nightly.correction_report_lines() == [
        "", "- correction report: 🔴 did not run (RuntimeError: kaboom)"]


def test_the_real_correction_line_reads_and_writes_nothing_it_should_not(monkeypatch, tmp_path):
    """The real metric, pointed at a near-empty temp Helix: a line, the sources unavailable."""
    monkeypatch.syspath_prepend(str(nightly.METRIC))
    import correction_rate
    (tmp_path / "memory/facts").mkdir(parents=True)
    (tmp_path / "memory/facts/1_a.md").write_text("this was wrong")
    (tmp_path / "app.py").write_text('_CORRECTION_MARKERS = ("was wrong",)\n')
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    monkeypatch.setattr(correction_rate.correction_line, "__defaults__", (tmp_path, None))
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 2))
    line = nightly.correction_line()
    assert line.startswith("- correction: 1/1 marked (100.0%) · before:after: no aged month yet"), line
    assert "ledger: source unavailable" in line
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


# ==========================================================================
# ORDER 38 — one `- house:` line: disks, RAM, temps, drive, and the dry-run plan (item 22).
# The housekeeping itself is tested in workspace/housekeeping/test_housekeeping.py; here only
# the wiring, and that the nightly never applies anything.
# ==========================================================================

def test_the_house_line_comes_right_after_the_correction_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             correction_line(),\n"
            "             house_line(),\n") in src


def test_the_house_line_is_the_one_from_housekeeping(monkeypatch):
    import types
    seen = []

    def line(conf=None, today=None, now=None):
        seen.append((conf, today))
        return "- house: / 44% · plan: would move 3 items, 0.10 GB (dry run, day 1/7)"
    monkeypatch.setitem(sys.modules, "housekeeping", types.SimpleNamespace(house_line=line))
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 3))
    assert nightly.house_line() == "- house: / 44% · plan: would move 3 items, 0.10 GB (dry run, day 1/7)"
    assert seen == [(None, datetime.date(2026, 10, 3))]
    assert nightly.HOUSEKEEPING == Path.home() / "helix/workspace/housekeeping"


def test_the_house_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom(**kw):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "housekeeping", types.SimpleNamespace(house_line=boom))
    assert nightly.house_line() == "- house: 🔴 housekeeping did not run (RuntimeError: kaboom)"


def test_the_house_line_with_no_housekeeping(monkeypatch):
    monkeypatch.setitem(sys.modules, "housekeeping", None)        # import → ImportError
    line = nightly.house_line()
    assert line.startswith("- house: 🔴 housekeeping did not run (ModuleNotFoundError: "), line


def test_the_nightly_only_ever_plans_never_applies():
    src = (Path(nightly.__file__)).read_text()
    assert "apply" not in src and "MOVES" not in src and "enabled.flag" not in src


def test_the_real_house_line_writes_only_its_plan_and_moves_nothing(monkeypatch, tmp_path):
    """The real housekeeping on a temp machine: the line, one plan file, nothing else changed."""
    import os
    monkeypatch.syspath_prepend(str(nightly.HOUSEKEEPING))
    import housekeeping as hk
    helix, t = tmp_path / "helix", tmp_path / "tmp"
    (helix / "workspace" / "x" / "__pycache__").mkdir(parents=True)
    (helix / "workspace" / "x" / "__pycache__" / "a.pyc").write_bytes(b"1234")
    t.mkdir()
    (t / "old").write_bytes(b"x" * 10)
    old = datetime.datetime(2026, 9, 1).timestamp()
    os.utime(t / "old", (old, old))
    conf = hk.Conf(helix=helix, tmp=t, odin_drive=tmp_path / "odin", hdd=tmp_path / "hdd",
                   quarantine=tmp_path / "hdd" / "q", uid=os.getuid())
    monkeypatch.setattr(hk, "Conf", lambda: conf)
    monkeypatch.setattr(hk, "measure", lambda c: dict(
        root_pct=44.0, hdd_pct=None, hdd_mounted=False, mem_total=31.0, mem_free=2.0, mem_avail=11.0,
        cpu_temp=45.0, cpu_src="x", gpu_temp=33, gpu_mem=845, gpu_mem_total=1, gpu_err=None,
        odin_mounted=False, helix_ok=True, helix_why="200", render_ok=True, render_why="200",
        restart_pending=[]))
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 2))
    line = nightly.house_line()
    assert line == ("- house: / 44% · hdd NOT mounted · RAM 11.0G avail of 31G · CPU 45°C · "
                    "GPU 33°C 845MiB · odin drive NOT mounted · plan: would move 2 items, 0.00 GB "
                    "(dry run, day 1/7) · 🔴 __BULK__ not mounted · 🔴 odin drive not mounted"), line
    plan = helix / "workspace" / "housekeeping" / "plans" / "2026-10-02.json"
    after = sorted(str(p) for p in tmp_path.rglob("*"))
    assert set(after) - set(before) == {str(plan.parent.parent), str(plan.parent), str(plan)}
    assert set(before) <= set(after) and not (tmp_path / "hdd").exists()


# ==========================================================================
# ORDER 41 — this suite's regate runs log their refusals to a temp log.
# ==========================================================================

def test_order41_the_nightlys_regate_refusals_go_to_a_temp_log(at, tmp_path):
    live = LIVE_REGATE.parent / "refusals.log"
    assert nightly.REGATE == REGATE != LIVE_REGATE
    reg = at(registry_copy(tmp_path, kinds=True, baseline=True), datetime.date.today().isoformat())
    res = nightly.regate("score", "0000-p1", "--outcome", "1", "--note", "early")
    assert res.returncode == 3, res.stdout + res.stderr
    assert str(reg) in REFUSALS_LOG.read_text()
    assert not live.exists() or str(reg) not in live.read_text()


# ==========================================================================
# ORDER 44 — one line on the gates' recall AND specificity, from workspace/canary, after the
# house line. The canaries themselves are tested in workspace/canary/test_canary.py; here the
# wiring, and that the nightly's call writes neither live refusals log.
# ==========================================================================

def test_the_canary_line_comes_right_after_the_house_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             house_line(),\n"
            "             canary_line(),\n") in src


def test_the_canary_line_is_the_one_from_canary(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "canary", types.SimpleNamespace(
        canary_line=lambda: "- canary: 7/7 caught · 3/3 acquitted"))
    assert nightly.canary_line() == "- canary: 7/7 caught · 3/3 acquitted"
    assert nightly.CANARY == Path.home() / "helix/workspace/canary"


def test_the_canary_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "canary", types.SimpleNamespace(canary_line=boom))
    assert nightly.canary_line() == "- canary: 🔴 canary did not run (RuntimeError: kaboom)"


def test_the_canary_line_with_no_canary(monkeypatch):
    monkeypatch.setitem(sys.modules, "canary", None)              # import → ImportError
    line = nightly.canary_line()
    assert line.startswith("- canary: 🔴 canary did not run (ModuleNotFoundError: "), line


def test_the_real_canary_line_writes_neither_live_refusals_log(monkeypatch, tmp_path):
    """Pre-flight 3: the nightly's canary call runs the gates as temp copies, so their refusals
    log beside the copies. Both live logs, the live registry and its ledger stay byte-identical;
    the run's record goes to a temp CANARY.jsonl; it takes under 60 s."""
    import time
    monkeypatch.syspath_prepend(str(nightly.CANARY))
    import canary
    monkeypatch.setattr(canary, "CANARY_LOG", tmp_path / "CANARY.jsonl")
    live = [LIVE_REGATE.parent / "refusals.log",
            Path.home() / "helix/workspace/sealgate/refusals.log",
            REAL_REG, REAL_REG.parent / LEDGER]
    before = [sha(p) if p.exists() else None for p in live]
    t0 = time.monotonic()
    line = nightly.canary_line()
    took = time.monotonic() - t0
    c = sum(1 for k in canary.CASES if k.kind == "canary")
    a = sum(1 for k in canary.CASES if k.kind == "acquittal")
    assert c >= 11 and a >= 3, (c, a)                             # a shrinking case list still fails
    assert line == f"- canary: {c}/{c} caught · {a}/{a} acquitted", line
    assert [sha(p) if p.exists() else None for p in live] == before
    assert len((tmp_path / "CANARY.jsonl").read_text().splitlines()) == 1
    assert took < 60, took


# ==========================================================================
# ORDER 45 — one line replaying one stored pack at random, from workspace/replay, after the
# canary line. The packs themselves are tested in workspace/replay/test_replay.py; here the
# wiring, and that the nightly's call fits in a minute without touching the live ledgers here.
# ==========================================================================

def test_the_replay_line_comes_right_after_the_canary_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             canary_line(),\n"
            "             replay_line(),\n") in src


def test_the_replay_line_is_the_one_from_replay(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "replay", types.SimpleNamespace(
        replay_line=lambda: "- replay: p MATCH (3/3 within tolerance)"))
    assert nightly.replay_line() == "- replay: p MATCH (3/3 within tolerance)"
    assert nightly.REPLAY == Path.home() / "helix/workspace/replay"


def test_the_replay_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "replay", types.SimpleNamespace(replay_line=boom))
    assert nightly.replay_line() == "- replay: 🔴 replay did not run (RuntimeError: kaboom)"


def test_the_replay_line_with_no_replay(monkeypatch):
    monkeypatch.setitem(sys.modules, "replay", None)              # import → ImportError
    line = nightly.replay_line()
    assert line.startswith("- replay: 🔴 replay did not run (ModuleNotFoundError: "), line


def test_the_real_replay_line_on_temp_ledgers(monkeypatch, tmp_path):
    """The real module through the nightly's call, its ledgers and bulk store pointed at temp
    paths: no packs → the no-packs line, nothing written, under 60 s."""
    import time
    monkeypatch.syspath_prepend(str(nightly.REPLAY))
    import replay
    monkeypatch.setattr(replay, "PACKS", tmp_path / "PACKS.jsonl")
    monkeypatch.setattr(replay, "REPLAYS", tmp_path / "REPLAYS.jsonl")
    monkeypatch.setattr(replay, "BULK", tmp_path)
    monkeypatch.setattr(replay, "MOUNT", None)
    t0 = time.monotonic()
    line = nightly.replay_line()
    assert time.monotonic() - t0 < 60
    assert line == "- replay: no packs yet", line
    assert not (tmp_path / "REPLAYS.jsonl").exists()


# ==========================================================================
# ORDER 46 — one line on the last monthly cold read, from workspace/coldread, after the replay
# line. The read itself is tested in workspace/coldread/test_coldread.py; here only the wiring,
# and that the nightly's call reads the ledger only (never starts a read), on a temp ledger.
# ==========================================================================

def test_the_coldread_line_comes_right_after_the_replay_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             replay_line(),\n"
            "             coldread_line(),\n") in src


def test_the_coldread_line_is_the_one_from_coldread(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "coldread", types.SimpleNamespace(
        coldread_line=lambda: "- coldread: none yet · next 2026-11-01"))
    assert nightly.coldread_line() == "- coldread: none yet · next 2026-11-01"
    assert nightly.COLDREAD == Path.home() / "helix/workspace/coldread"


def test_the_coldread_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "coldread", types.SimpleNamespace(coldread_line=boom))
    assert nightly.coldread_line() == "- coldread: 🔴 coldread did not run (RuntimeError: kaboom)"


def test_the_coldread_line_with_no_coldread(monkeypatch):
    monkeypatch.setitem(sys.modules, "coldread", None)            # import → ImportError
    line = nightly.coldread_line()
    assert line.startswith("- coldread: 🔴 coldread did not run (ModuleNotFoundError: "), line


def test_the_real_coldread_line_on_a_temp_ledger_never_starts_a_read(monkeypatch, tmp_path):
    """The real module through the nightly's call, its ledger at a temp path and its model made
    to fail: no reads → the none-yet line, nothing written, no session, under 5 s."""
    import time
    monkeypatch.syspath_prepend(str(nightly.COLDREAD))
    import coldread
    monkeypatch.setattr(coldread, "READS", tmp_path / "READS.jsonl")
    monkeypatch.setattr(coldread, "READS_DIR", tmp_path / "READS")

    def no(*a, **k):
        raise AssertionError("the nightly must never start a read")
    monkeypatch.setattr(coldread, "run", no)
    monkeypatch.setattr(coldread, "claude_session", no)
    t0 = time.monotonic()
    line = nightly.coldread_line()
    assert time.monotonic() - t0 < 5
    assert line.startswith("- coldread: none yet · next "), line
    assert not list(tmp_path.iterdir())


# ==========================================================================
# ORDER 47 — one line on reviewer rotation, from workspace/sealgate/review.py, after the coldread
# line. The rotation itself is tested in workspace/sealgate/test_review.py; here only the wiring,
# and that the nightly's call reads the ledger only, on a temp ledger.
# ==========================================================================

def test_the_reviews_line_comes_right_after_the_coldread_line():
    # ORDER 72: the corrections line now sits between them
    src = (Path(nightly.__file__)).read_text()
    assert ("             coldread_line(),\n"
            "             corrections_line(),\n"
            "             reviews_line(),\n") in src


def test_the_reviews_line_is_the_one_from_review(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "review", types.SimpleNamespace(
        reviews_line=lambda: "- reviews: 2 seals · Alice 1 / Claude-cold 1 · chain OK"))
    assert nightly.reviews_line() == "- reviews: 2 seals · Alice 1 / Claude-cold 1 · chain OK"
    assert nightly.SEALGATE == Path.home() / "helix/workspace/sealgate"


def test_the_reviews_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "review", types.SimpleNamespace(reviews_line=boom))
    assert nightly.reviews_line() == "- reviews: 🔴 reviews did not run (RuntimeError: kaboom)"


def test_the_reviews_line_with_no_review_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "review", None)              # import → ImportError
    line = nightly.reviews_line()
    assert line.startswith("- reviews: 🔴 reviews did not run (ModuleNotFoundError: "), line


def test_the_real_reviews_line_on_a_temp_ledger_reads_only(monkeypatch, tmp_path):
    """The real module through the nightly's call, its ledger at a temp path, its reviewers made
    to fail: the none-yet line, then the counts line; nothing written by the nightly's call."""
    monkeypatch.syspath_prepend(str(nightly.SEALGATE))
    import review
    ledger = tmp_path / "REVIEWS.jsonl"
    monkeypatch.setattr(review, "LEDGER", ledger)

    def no(*a, **k):
        raise AssertionError("the nightly must never call a reviewer")
    monkeypatch.setitem(review.REVIEWERS, "Alice", no)
    monkeypatch.setitem(review.REVIEWERS, "Claude-cold", no)
    assert nightly.reviews_line() == "- reviews: none yet"
    assert not ledger.exists()
    review.append([{"kind": "assign", "seal": "/tmp/x/PREREG.md", "reviewer": "Alice",
                    "repeat": False, "throwaway": True}])
    before = ledger.read_bytes()
    assert nightly.reviews_line() == "- reviews: 1 seals · Alice 1 / Claude-cold 0 · chain OK"
    assert ledger.read_bytes() == before


# ==========================================================================
# ORDER 48 — Alice's veto (amendment 9a): one `- veto:` line right after the reviews line. The
# veto itself is tested in workspace/veto/test_veto.py; here only the wiring, and that the
# nightly's call reads the ledger only, on a temp ledger.
# ==========================================================================

def test_the_veto_line_comes_right_after_the_reviews_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             reviews_line(),\n"
            "             veto_line(),\n") in src


def test_the_veto_line_is_the_one_from_veto(monkeypatch):
    import types
    want = "- veto: 3 runs · 1 confirmed · 1 disputed · 0 invalid · chain OK"
    monkeypatch.setitem(sys.modules, "veto", types.SimpleNamespace(veto_line=lambda: want))
    assert nightly.veto_line() == want
    assert nightly.VETO == Path.home() / "helix/workspace/veto"


def test_the_veto_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "veto", types.SimpleNamespace(veto_line=boom))
    assert nightly.veto_line() == "- veto: 🔴 veto did not run (RuntimeError: kaboom)"


def test_the_veto_line_with_no_veto_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "veto", None)                # import → ImportError
    line = nightly.veto_line()
    assert line.startswith("- veto: 🔴 veto did not run (ModuleNotFoundError: "), line


def test_the_real_veto_line_on_a_temp_ledger_reads_only(monkeypatch, tmp_path):
    """The real module through the nightly's call, its ledger at a temp path, Alice made to fail:
    none yet, then the counts line, then a broken chain; nothing written by the nightly's call."""
    monkeypatch.syspath_prepend(str(nightly.VETO))
    import veto
    ledger = tmp_path / "VETOES.jsonl"
    monkeypatch.setattr(veto, "LEDGER", ledger)

    def no(*a, **k):
        raise AssertionError("the nightly must never call Alice or the judge")
    monkeypatch.setattr(veto, "ALICE", no)
    monkeypatch.setattr(veto, "JUDGE", no)
    assert nightly.veto_line() == "- veto: none yet"
    assert not ledger.exists()
    veto.append([{"kind": "veto", "seal": "/tmp/x/PREREG.md", "outcome": "NO_BREAK"},
                 {"kind": "veto", "seal": "/tmp/x/PREREG.md", "outcome": "BREAK_REPRODUCED",
                  "disagreement": True},
                 {"kind": "veto", "seal": "/tmp/x/PREREG.md", "outcome": "BREAK_CONFIRMED"},
                 {"kind": "veto", "seal": "/tmp/x/PREREG.md", "outcome": "BREAK_DISPUTED",
                  "disagreement": True, "disagreement_kind": "alice_vs_judge"},
                 {"kind": "veto", "seal": "/tmp/x/PREREG.md", "outcome": "BREAK_INVALID"}])
    before = ledger.read_bytes()
    # ORDER 48b: confirmed / disputed / invalid, and order 48's BREAK_REPRODUCED as legacy
    assert nightly.veto_line() == \
        "- veto: 5 runs · 1 confirmed · 1 disputed · 1 invalid · 1 legacy · chain OK"
    assert ledger.read_bytes() == before
    ledger.write_bytes(before.split(b"\n", 1)[1])                 # first line removed
    assert nightly.veto_line().endswith("🔴 chain BROKEN at line 1")


# ==========================================================================
# ORDER 49 — seat-swap replication (item 10): one `- seatswap:` line right after the veto line.
# seatswap itself is tested in workspace/seatswap/test_seatswap.py; here only the wiring, and that
# the nightly's call reads the ledger only, on a temp ledger.
# ==========================================================================

def test_the_seatswap_line_comes_right_after_the_veto_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             veto_line(),\n"
            "             seatswap_line(),\n") in src


def test_the_seatswap_line_is_the_one_from_seatswap(monkeypatch):
    import types
    want = "- seatswap: 2 runs · 1 agree · 1 disagree · chain OK"
    monkeypatch.setitem(sys.modules, "seatswap",
                        types.SimpleNamespace(seatswap_line=lambda: want))
    assert nightly.seatswap_line() == want
    assert nightly.SEATSWAP == Path.home() / "helix/workspace/seatswap"


def test_the_seatswap_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "seatswap", types.SimpleNamespace(seatswap_line=boom))
    assert nightly.seatswap_line() == "- seatswap: 🔴 seatswap did not run (RuntimeError: kaboom)"


def test_the_seatswap_line_with_no_seatswap_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "seatswap", None)            # import → ImportError
    line = nightly.seatswap_line()
    assert line.startswith("- seatswap: 🔴 seatswap did not run (ModuleNotFoundError: "), line


def test_the_real_seatswap_line_on_a_temp_ledger_reads_only(monkeypatch, tmp_path):
    """The real module through the nightly's call, its ledger at a temp path, Alice made to fail:
    none yet, then the counts line, then a broken chain; nothing written by the nightly's call."""
    monkeypatch.syspath_prepend(str(nightly.SEATSWAP))
    import seatswap
    ledger = tmp_path / "SWAPS.jsonl"
    monkeypatch.setattr(seatswap, "LEDGER", ledger)

    def no(*a, **k):
        raise AssertionError("the nightly must never call Alice")
    monkeypatch.setattr(seatswap, "ALICE", no)
    assert nightly.seatswap_line() == "- seatswap: none yet"
    assert not ledger.exists()
    seatswap.append([{"kind": "swap", "pack": "p", "outcome": "AGREE"},
                     {"kind": "swap", "pack": "p", "outcome": "DISAGREE"},
                     {"kind": "swap", "pack": "p", "outcome": "COPIED"},
                     {"kind": "swap", "pack": "p", "outcome": "AGREE"}])
    before = ledger.read_bytes()
    assert nightly.seatswap_line() == "- seatswap: 4 runs · 2 agree · 1 disagree · chain OK"
    assert ledger.read_bytes() == before
    ledger.write_bytes(before.split(b"\n", 1)[1])                 # first line removed
    assert nightly.seatswap_line() == "- seatswap: 🔴 chain BROKEN at line 1"


# ==========================================================================
# ORDER 51 — the provenance DAG (item 23): one `- provenance:` line right after the seatswap line.
# The DAG itself is tested in workspace/regate/test_regate.py; here only the wiring, and that the
# nightly's call reads only, on a temp copy of the live registry.
# ==========================================================================

def test_the_provenance_line_comes_right_after_the_seatswap_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             seatswap_line(),\n"
            "             provenance_line(),\n") in src


def test_the_provenance_line_is_the_one_from_regate(monkeypatch):
    import types
    want = "- provenance: 5 entries · 1 edge · 0 undeclared · 0 quarantined · no cycles"
    seen = []
    monkeypatch.setitem(sys.modules, "regate",
                        types.SimpleNamespace(provenance_line=lambda reg: seen.append(reg) or want))
    assert nightly.provenance_line() == want
    assert seen == [nightly.REG], "regate is asked about the nightly's own registry"
    assert nightly.PROVENANCE == Path.home() / "helix/workspace/regate"


def test_the_provenance_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom(reg):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "regate", types.SimpleNamespace(provenance_line=boom))
    assert nightly.provenance_line() == \
        "- provenance: 🔴 provenance did not run (RuntimeError: kaboom)"


def test_the_provenance_line_with_no_regate_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "regate", None)              # import → ImportError
    line = nightly.provenance_line()
    assert line.startswith("- provenance: 🔴 provenance did not run (ModuleNotFoundError: "), line


ORDER51_BASE = "b5354a6"      # seam, immediately before order 51 declared the live edges


def test_the_real_provenance_line_on_a_temp_registry_reads_only(monkeypatch, tmp_path):
    """The real regate through the nightly's call, on a temp copy of the registry as it was at
    ORDER51_BASE (a commit, never the live file), its pack ledgers at temp paths: undeclared, then
    Part D's declarations, then 0003 dead."""
    spec = importlib.util.spec_from_file_location(        # the real file: REGATE is a shim here
        "regate", Path.home() / "helix/workspace/regate/regate.py")
    rg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rg)
    for name in ("PACKS", "REPLAYS", "SWAPS"):
        monkeypatch.setattr(rg, name, tmp_path / f"{name}.jsonl")
    monkeypatch.setattr(rg, "REFUSALS_LOG", tmp_path / "refusals.log")
    monkeypatch.setitem(sys.modules, "regate", rg)
    reg = tmp_path / "registry.json"
    reg.write_bytes(subprocess.run(["git", "-C", str(HERE), "show",
                                    f"{ORDER51_BASE}:registry/registry.json"],
                                   capture_output=True, check=True).stdout)
    assert sorted(json.loads(reg.read_text())) == ["0000", "0001", "0002", "0003", "0004"]
    monkeypatch.setattr(nightly, "REG", reg)
    assert rg.main(["baseline", str(reg)]) == 0
    assert nightly.provenance_line() == \
        "- provenance: 5 entries · 0 edges · 5 undeclared · 0 quarantined · no cycles"
    for eid in ("0000", "0001", "0002", "0003"):
        assert rg.main(["attach-deps", str(reg), eid, "--none"]) == 0
    assert rg.main(["attach-deps", str(reg), "0004", "--on", "0003"]) == 0
    before = (reg.read_bytes(), (tmp_path / LEDGER).read_bytes())
    assert nightly.provenance_line() == \
        "- provenance: 5 entries · 1 edge · 0 undeclared · 0 quarantined · no cycles"
    assert (reg.read_bytes(), (tmp_path / LEDGER).read_bytes()) == before, "reads only"
    assert rg.main(["status", str(reg), "0003", "INVALID — fixture"]) == 0
    assert nightly.provenance_line() == (
        "- provenance: 5 entries · 1 edge · 0 undeclared · 1 quarantined · no cycles\n"
        "  - 🔴 QUARANTINED: 0004 ← 0003 INVALID")
    assert not (tmp_path / "refusals.log").exists()


def test_registry_copy_reads_the_pinned_commit_never_the_live_file(monkeypatch, tmp_path):
    """Order 51b: the fixture is the registry at REGISTRY_PIN, a commit. REAL_REG is pointed at
    a decoy here, so a fixture that read the live path again would copy the decoy and fail."""
    decoy = tmp_path / "decoy_registry.json"
    decoy.write_text('{"decoy": true}')
    monkeypatch.setattr(sys.modules[__name__], "REAL_REG", decoy)
    pinned = subprocess.run(["git", "-C", str(HERE), "show", f"{REGISTRY_PIN}:registry/registry.json"],
                            capture_output=True, check=True).stdout
    assert registry_copy(tmp_path).read_bytes() == pinned


# ==========================================================================
# ORDER 52 — the proposal engine (item 21): one `- proposals:` line right after the provenance
# line. The engine itself is tested in workspace/proposals/test_propose.py; here only the wiring,
# and that the nightly's call reads only, on temp ledgers.
# ==========================================================================

def test_the_proposals_line_comes_right_after_the_provenance_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             provenance_line(),\n"
            "             proposals_line(),\n") in src


def test_the_proposals_line_is_the_one_from_propose(monkeypatch):
    import types
    want = ("- proposals: 0 pending · 0 approved · 0 rejected · friction +0/−0 · leads 0 · "
            "last batch never · chain OK")
    monkeypatch.setitem(sys.modules, "propose", types.SimpleNamespace(proposals_line=lambda: want))
    assert nightly.proposals_line() == want
    assert nightly.PROPOSALS == Path.home() / "helix/workspace/proposals"


def test_the_proposals_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "propose", types.SimpleNamespace(proposals_line=boom))
    assert nightly.proposals_line() == "- proposals: 🔴 proposals did not run (RuntimeError: kaboom)"


def test_the_proposals_line_with_no_propose_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "propose", None)              # import → ImportError
    line = nightly.proposals_line()
    assert line.startswith("- proposals: 🔴 proposals did not run (ModuleNotFoundError: "), line


def test_the_real_proposals_line_on_temp_ledgers_reads_only_and_goes_red_on_a_broken_chain(
        monkeypatch, tmp_path):
    """The real propose.py through the nightly's call, its ledgers in a temp dir: a chained
    proposal reads OK and is left byte-identical; an edited middle line turns the line 🔴."""
    spec = importlib.util.spec_from_file_location(
        "propose", Path.home() / "helix/workspace/proposals/propose.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)
    monkeypatch.setattr(pr, "HERE", tmp_path)
    monkeypatch.setitem(sys.modules, "propose", pr)
    assert nightly.proposals_line() == (
        "- proposals: 0 pending · 0 approved · 0 rejected · friction +0/−0 · leads 0 · "
        "last batch never · chain OK")
    pr.append(tmp_path / "PROPOSALS.jsonl", [
        {"kind": "proposal", "id": f"P-20261004-{k}", "status": "pending",
         "friction": "ADDS-FRICTION", "lane": "backend", "title": f"t{k}"} for k in (1, 2, 3)])
    before = (tmp_path / "PROPOSALS.jsonl").read_bytes()
    assert nightly.proposals_line() == (
        "- proposals: 3 pending · 0 approved · 0 rejected · friction +3/−0 · leads 0 · "
        "last batch never · chain OK")
    assert (tmp_path / "PROPOSALS.jsonl").read_bytes() == before, "reads only"
    lines = before.decode().splitlines()
    lines[1] = lines[1].replace("t2", "t9")
    (tmp_path / "PROPOSALS.jsonl").write_text("\n".join(lines) + "\n")
    line = nightly.proposals_line()
    assert "chain BROKEN" in line and "\n  - 🔴 chain BROKEN: PROPOSALS.jsonl line 3 " in line, line   # line 3 no longer carries line 2's hash


# ==========================================================================
# ORDER 63 (P-20261004-10) — one line on the hook's corpus of logged catches, from hookcorpus.py,
# right after the timeforward line. hookcorpus.py itself is tested in test_hookcorpus.py.
# ==========================================================================

def test_the_hookcorpus_line_comes_right_after_the_timeforward_line():
    src = (Path(nightly.__file__)).read_text()
    assert ("             timeforward_line(),\n"
            "             hookcorpus_line(),\n") in src


def test_the_hookcorpus_line_is_the_one_from_hookcorpus(monkeypatch):
    import types
    want = "- hookcorpus: 36/36 verdicts held · hook a370fc16df9c"
    monkeypatch.setitem(sys.modules, "hookcorpus", types.SimpleNamespace(line=lambda: want))
    assert nightly.hookcorpus_line() == want


def test_the_hookcorpus_line_never_stops_the_nightly(monkeypatch):
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "hookcorpus", types.SimpleNamespace(line=boom))
    assert nightly.hookcorpus_line() == "- hookcorpus: 🔴 hookcorpus did not run (RuntimeError: kaboom)"


def test_the_hookcorpus_line_with_no_hookcorpus_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "hookcorpus", None)            # import → ImportError
    line = nightly.hookcorpus_line()
    assert line.startswith("- hookcorpus: 🔴 hookcorpus did not run (ModuleNotFoundError: "), line


def test_a_raising_hookcorpus_line_does_not_stop_the_nightly_and_appears_once(monkeypatch, tmp_path):
    """main() with every other step stubbed: a hookcorpus that raises still yields one line, right
    after the timeforward line, and NIGHTLY.md is written (to a temp HERE)."""
    import types

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(sys.modules, "hookcorpus", types.SimpleNamespace(line=boom))
    monkeypatch.setattr(nightly, "HERE", tmp_path)
    monkeypatch.setattr(nightly, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(nightly, "REG", tmp_path / "registry" / "registry.json")
    monkeypatch.setattr(nightly.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="optc: stub\n", stderr=""))
    monkeypatch.setattr(nightly, "audit", lambda: ({"facts": {"broken": 0, "epsilon": 0.0}}, []))
    monkeypatch.setattr(nightly, "cohort", lambda rows: (set(), 0))
    monkeypatch.setattr(nightly, "score", lambda rows, seen: ([], [], []))
    for name in ("registry_line", "facts_line", "interim_line", "categories_line", "vault_line", "gaps_line",
                 "correction_line", "house_line", "canary_line", "replay_line", "coldread_line",
                 "corrections_line", "reviews_line", "veto_line", "seatswap_line", "provenance_line", "proposals_line",
                 "timeforward_line"):
        monkeypatch.setattr(nightly, name, lambda name=name: f"- {name}: stub")
    monkeypatch.setattr(nightly, "heartbeat_line", lambda p: "- heartbeat: stub")
    monkeypatch.setattr(nightly, "needs_seal_lines", lambda: [])
    monkeypatch.setattr(nightly, "correction_report_lines", lambda: [])
    nightly.main()
    out = (tmp_path / "NIGHTLY.md").read_text().splitlines()
    i = out.index("- timeforward_line: stub")
    assert out[i + 1] == "- hookcorpus: 🔴 hookcorpus did not run (RuntimeError: kaboom)"
    assert sum(l.startswith("- hookcorpus:") for l in out) == 1
    assert out[i + 2] == "- optc: stub"


# ==========================================================================
# ORDER 70 — one line on retired passages (memretrieve.retirements()), right after `- interim:`.
# The rule itself is tested in workspace/memretrieve/test_memretrieve.py; here the line and wiring.
# ==========================================================================

R_SHARE = ("# finance folder\nThe folder lives on the internal disk. `backup_memory.sh` mirrors it to\n"
           "`//192.168.77.17/odin/box1`, but **only when the user says to back up.** Nothing else copies it.\n")
R_PASSAGE = "mirrors it to `//192.168.77.17/odin/box1`, but **only when the user says to back up.**"


@pytest.fixture
def retired_at(monkeypatch, tmp_path):
    """The nightly pointed at a temp fact base, PINNED.txt and history, on 2026-10-23."""
    facts = tmp_path / "memory" / "facts"
    facts.mkdir(parents=True)
    monkeypatch.setattr(nightly, "FACTS", facts)
    monkeypatch.setattr(nightly, "PINNED", tmp_path / "memory" / "PINNED.txt")
    monkeypatch.setattr(nightly, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 23))

    def fact(name, text):
        (facts / name).write_text(text)

    def history(*rows):
        (tmp_path / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return facts, fact, history, tmp_path


def test_70_the_retired_line_with_nothing_retired(retired_at):
    facts, fact, history, _t = retired_at
    fact("103_fin.md", R_SHARE)
    assert nightly.retired_line() == "- retired: 0 passages in 0 facts · 0 unresolved"
    history({"date": "2026-10-22", "retired": []})
    assert nightly.retired_line() == "- retired: 0 passages in 0 facts · 0 unresolved"


def test_70_the_retired_line_counts_and_names_what_is_new_since_yesterday(retired_at):
    facts, fact, history, _t = retired_at
    fact("103_fin.md", R_SHARE)
    fact("50_x.md", "# x\nthe old way of doing the thing was this\n")
    fact("164_a.md", f"# a\nRETIRES: `103_*` \"{R_PASSAGE}\"\n")
    fact("170_b.md", "# b\nRETIRES: `103_fin` \"The folder lives on the internal disk.\"\n"
                     "RETIRES: `50_x.md` \"the old way of doing the thing\"\n")
    history({"date": "2026-10-21", "retired": []}, {"date": "2026-10-22", "retired": ["164_a.md→103_fin.md"]},
            {"date": "2026-10-23", "retired": ["170_b.md→103_fin.md"]})       # today's own row is not "yesterday"
    assert nightly.retired_line() == ("- retired: 3 passages in 2 facts · 0 unresolved · new since yesterday: 2 "
                                      "(170_b.md→103_fin.md, 170_b.md→50_x.md)")
    history({"date": "2026-10-22", "retired": nightly.retired_pairs()})
    assert nightly.retired_line() == "- retired: 3 passages in 2 facts · 0 unresolved"


def test_70_the_retired_line_flags_unresolved_and_shows_the_first_three(retired_at):
    facts, fact, history, _t = retired_at
    fact("103_fin.md", R_SHARE)
    fact("9_bad.md", "# bad\n" + "".join(f"RETIRES: `103_*` \"not in the old fact, number {i}\"\n" for i in range(4)))
    history({"date": "2026-10-22", "retired": []})
    assert nightly.retired_line() == ("- retired: 0 passages in 0 facts · 🟡 4 unresolved (9_bad.md line 2: 0 matches "
                                      "in 103_fin.md; 9_bad.md line 3: 0 matches in 103_fin.md; 9_bad.md line 4: "
                                      "0 matches in 103_fin.md …)")


def test_70_a_new_retirement_in_a_pinned_fact_is_named_every_night_it_is_new(retired_at):
    facts, fact, history, tmp = retired_at
    (tmp / "memory" / "PINNED.txt").write_text("# pins\n103_fin.md\n")
    fact("103_fin.md", R_SHARE)
    fact("164_a.md", f"# a\nRETIRES: `103_*` \"{R_PASSAGE}\"\n")
    fact("50_x.md", "# x\nthe old way of doing the thing was this\n")
    fact("60_y.md", "# y\nRETIRES: `50_*` \"the old way of doing the thing\"\n")
    history({"date": "2026-10-22", "retired": []})
    assert nightly.retired_line() == ("- retired: 2 passages in 2 facts · 0 unresolved · new since yesterday: 2 "
                                      "(164_a.md→103_fin.md, 60_y.md→50_x.md) · 📌 pinned fact changed: "
                                      "164_a.md→103_fin.md")
    history({"date": "2026-10-22", "retired": ["164_a.md→103_fin.md", "60_y.md→50_x.md"]})
    assert "📌" not in nightly.retired_line()


def test_70_the_retired_line_never_stops_the_nightly(retired_at, monkeypatch):
    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(nightly, "_retired_now", boom)
    assert nightly.retired_line() == "- retired: 🔴 did not run (RuntimeError: kaboom)"
    assert nightly.retired_pairs() is None


def test_70_the_retired_line_reads_and_writes_nothing_it_should_not(retired_at):
    facts, fact, history, tmp = retired_at
    fact("103_fin.md", R_SHARE)
    fact("164_a.md", f"# a\nRETIRES: `103_*` \"{R_PASSAGE}\"\n")
    history({"date": "2026-10-22", "retired": []})
    before = sorted((str(p), p.read_bytes() if p.is_file() else b"") for p in tmp.rglob("*"))
    assert nightly.retired_line().startswith("- retired: 1 passages in 1 facts")
    assert sorted((str(p), p.read_bytes() if p.is_file() else b"") for p in tmp.rglob("*")) == before


def test_70_the_retired_line_uses_memretrieves_one_parser():
    src = (Path(nightly.__file__)).read_text()
    assert "RETIRES" not in src, "no second parser: memretrieve.retirements() is the one"
    assert nightly.MEMRETRIEVE == Path.home() / "helix/workspace/memretrieve"
    assert nightly.PINNED == Path.home() / "helix/memory/PINNED.txt"


def test_70_main_puts_the_retired_line_after_interim_and_its_pairs_in_the_history_row(retired_at, monkeypatch):
    import types
    facts, fact, history, tmp = retired_at
    fact("103_fin.md", R_SHARE)
    fact("164_a.md", f"# a\nRETIRES: `103_*` \"{R_PASSAGE}\"\n")
    history({"date": "2026-10-22", "corpora": {"facts": {"broken": 0, "epsilon": 0.0}}, "retired": []})
    monkeypatch.setattr(nightly, "HERE", tmp)
    monkeypatch.setattr(nightly, "REG", tmp / "registry" / "registry.json")
    monkeypatch.setattr(nightly.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="optc: stub\n", stderr=""))
    monkeypatch.setattr(nightly, "audit", lambda: ({"facts": {"broken": 0, "epsilon": 0.0}}, []))
    monkeypatch.setattr(nightly, "cohort", lambda rows: (set(), 0))
    monkeypatch.setattr(nightly, "score", lambda rows, seen: ([], [], []))
    for name in ("registry_line", "facts_line", "interim_line", "categories_line", "vault_line", "machine_line", "gaps_line",
                 "correction_line", "house_line", "canary_line", "replay_line", "coldread_line",
                 "corrections_line", "reviews_line", "veto_line", "seatswap_line", "provenance_line", "proposals_line",
                 "timeforward_line", "hookcorpus_line"):
        monkeypatch.setattr(nightly, name, lambda name=name: f"- {name}: stub")
    monkeypatch.setattr(nightly, "heartbeat_line", lambda p: "- heartbeat: stub")
    monkeypatch.setattr(nightly, "needs_seal_lines", lambda: [])
    monkeypatch.setattr(nightly, "correction_report_lines", lambda: [])
    nightly.main()
    out = (tmp / "NIGHTLY.md").read_text().splitlines()
    i = out.index("- interim_line: stub")
    assert out[i + 1] == ("- retired: 1 passages in 1 facts · 0 unresolved · new since yesterday: 1 "
                          "(164_a.md→103_fin.md)")
    assert out[i + 2] == "- categories_line: stub"          # ORDER 73b: between retired and vault
    assert out[i + 3] == "- vault_line: stub"
    assert sum(l.startswith("- retired:") for l in out) == 1
    rows = [json.loads(l) for l in (tmp / "history.jsonl").read_text().splitlines()]
    assert rows[-1]["date"] == "2026-10-23" and rows[-1]["retired"] == ["164_a.md→103_fin.md"]
    assert (facts / "103_fin.md").read_text() == R_SHARE


# ==========================================================================
# ORDER 72 — one line on the correction queue (workspace/corrections), right after `- coldread:`.
# The queue itself is tested in workspace/corrections/test_corrections.py; here the line and wiring.
# ==========================================================================

@pytest.fixture
def queue_at(monkeypatch, tmp_path):
    """corrections pointed at a temp READS dir, QUEUE.jsonl, fact base and log; the nightly on 2026-10-23."""
    if str(nightly.CORRECTIONS) not in sys.path:
        sys.path.append(str(nightly.CORRECTIONS))
    import corrections
    import coldread
    reads = tmp_path / "READS"
    reads.mkdir()
    (tmp_path / "facts").mkdir()
    (tmp_path / "log.md").write_text("# log\n")
    monkeypatch.setattr(corrections, "READS_DIR", reads)
    monkeypatch.setattr(corrections, "QUEUE", tmp_path / "QUEUE.jsonl")
    monkeypatch.setattr(corrections, "FACTS", tmp_path / "facts")
    monkeypatch.setattr(corrections, "LOG", tmp_path / "log.md")
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 10, 23))

    def read(date, files):
        rec = {"date": date, "outcome": "DONE", "reason": None, "files": 3, "excluded": 0, "chars": 1,
               "batches": 1, "batch_chars": [1], "cost_usd": 0.0, "sessions": 1,
               "plant": {"found": True, "file": "186_plant.md", "batch": 1, "claim": "Planted.",
                         "contradicted_by": ["174_real.md"]}}
        cands = [{"batch": 1, "file": f, "quote": f"quote of {f}", "why": "w", "confidence": 0.5}
                 for f in files + ["186_plant.md"]]
        (reads / f"{date}.md").write_text(coldread.render_md(rec, [], cands, None))
    return corrections, read, tmp_path


def test_72_the_corrections_line_with_nothing_queued(queue_at):
    corrections, read, tmp = queue_at
    assert nightly.corrections_line() == "- corrections: none queued (run corrections.py harvest)"
    assert not (tmp / "QUEUE.jsonl").exists()


def test_72_the_corrections_line_with_zero_open(queue_at):
    corrections, read, tmp = queue_at
    read("2026-10-03", [])                                          # only the plant: nothing to queue
    assert nightly.corrections_line() == "- corrections: 0 open · 0 closed · 0 rejected"


def test_72_the_nightly_harvests_a_new_read_then_reports_it(queue_at):
    corrections, read, tmp = queue_at
    read("2026-10-03", ["48_a.md", "71_b.md"])
    assert nightly.corrections_line() == "- corrections: 2 open (oldest 20 days) · 0 closed · 0 rejected"
    assert [r["id"] for r in corrections.records("finding")] == ["C-20261003-1", "C-20261003-2"]
    before = (tmp / "QUEUE.jsonl").read_bytes()
    assert nightly.corrections_line() == "- corrections: 2 open (oldest 20 days) · 0 closed · 0 rejected"
    assert (tmp / "QUEUE.jsonl").read_bytes() == before, "a read is harvested once"


def test_72_the_corrections_line_flags_the_oldest_open_past_30_days(queue_at, monkeypatch):
    corrections, read, tmp = queue_at
    read("2026-10-03", ["48_a.md", "71_b.md", "41_c.md", "81_d.md"])
    monkeypatch.setattr(nightly, "TODAY", datetime.date(2026, 11, 3))
    assert nightly.corrections_line() == (
        "- corrections: 🟡 4 open (oldest 31 days) · 0 closed · 0 rejected · oldest open: "
        "C-20261003-1 48_a.md, C-20261003-2 71_b.md, C-20261003-3 41_c.md …")


def test_72_the_corrections_line_never_stops_the_nightly(queue_at, monkeypatch):
    corrections, read, tmp = queue_at

    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(corrections, "harvest", boom)
    assert nightly.corrections_line() == "- corrections: unavailable — RuntimeError: kaboom"
    monkeypatch.setitem(sys.modules, "corrections", None)           # import → ImportError
    assert nightly.corrections_line().startswith("- corrections: unavailable — ModuleNotFoundError: ")


def test_72_a_broken_queue_is_reported_not_repaired(queue_at):
    corrections, read, tmp = queue_at
    read("2026-10-03", ["48_a.md"])
    nightly.corrections_line()
    q = tmp / "QUEUE.jsonl"
    q.write_text(q.read_text().replace("48_a.md", "48_z.md"))
    bad = q.read_bytes()
    line = nightly.corrections_line()
    assert line.startswith("- corrections: unavailable — RuntimeError: QUEUE.jsonl chain is broken"), line
    assert q.read_bytes() == bad


def test_72_main_puts_the_corrections_line_right_after_coldread(queue_at, monkeypatch):
    import types
    corrections, read, tmp = queue_at
    read("2026-10-03", ["48_a.md"])
    monkeypatch.setattr(nightly, "HERE", tmp)
    monkeypatch.setattr(nightly, "HISTORY", tmp / "history.jsonl")
    monkeypatch.setattr(nightly, "REG", tmp / "registry" / "registry.json")
    monkeypatch.setattr(nightly.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="optc: stub\n", stderr=""))
    monkeypatch.setattr(nightly, "audit", lambda: ({"facts": {"broken": 0, "epsilon": 0.0}}, []))
    monkeypatch.setattr(nightly, "cohort", lambda rows: (set(), 0))
    monkeypatch.setattr(nightly, "score", lambda rows, seen: ([], [], []))
    monkeypatch.setattr(nightly, "retired_pairs", lambda: [])
    for name in ("registry_line", "facts_line", "interim_line", "retired_line", "categories_line", "vault_line", "machine_line",
                 "gaps_line", "correction_line", "house_line", "canary_line", "replay_line", "coldread_line",
                 "reviews_line", "veto_line", "seatswap_line", "provenance_line", "proposals_line",
                 "timeforward_line", "hookcorpus_line"):
        monkeypatch.setattr(nightly, name, lambda name=name: f"- {name}: stub")
    monkeypatch.setattr(nightly, "heartbeat_line", lambda p: "- heartbeat: stub")
    monkeypatch.setattr(nightly, "needs_seal_lines", lambda: [])
    monkeypatch.setattr(nightly, "correction_report_lines", lambda: [])
    nightly.main()
    out = (tmp / "NIGHTLY.md").read_text().splitlines()
    i = out.index("- coldread_line: stub")
    assert out[i + 1] == "- corrections: 1 open (oldest 20 days) · 0 closed · 0 rejected"
    assert out[i + 2] == "- reviews_line: stub"
    assert sum(l.startswith("- corrections:") for l in out) == 1


# ==========================================================================
# ORDER 73b — one line on where each fact's category comes from (workspace/categories), right after
# the retired line. The census itself is tested in workspace/categories/test_categories.py; here the
# wiring, on a temp fact base and a temp ASSIGNED.csv (never the live ones).
# ==========================================================================

@pytest.fixture
def cats_at(retired_at, monkeypatch):
    facts, fact, history, tmp = retired_at
    side = tmp / "ASSIGNED.csv"
    monkeypatch.setattr(nightly, "CATEGORIES_ASSIGNED", side)

    def assigned(*rows):
        side.write_text("file,primary,secondary,source\n" + "".join(",".join(r) + "\n" for r in rows))
    return fact, assigned, tmp


def test_73b_the_categories_line_paths():
    assert nightly.CATEGORIES == Path.home() / "helix" / "workspace" / "categories"
    assert nightly.CATEGORIES_ASSIGNED == nightly.CATEGORIES / "ASSIGNED.csv"


def test_73b_the_categories_line_with_none_uncategorized(cats_at):
    fact, assigned, _t = cats_at
    fact("1_a.md", "# a\nCategory: rules\n")
    fact("2_b.md", "# b\n")
    assigned(["2_b.md", "personal", "", "proposal"])
    assert nightly.categories_line() == "- categories: 1 declared · 1 assigned · 0 none"


def test_73b_the_categories_line_flags_uncategorized_and_shows_the_first_three(cats_at):
    fact, assigned, _t = cats_at
    for n in ("1_a.md", "2_b.md", "3_c.md", "4_d.md"):
        fact(n, "# x\n")
    fact("5_e.md", "# e\nCategory: helix\n")
    assert not (_t / "ASSIGNED.csv").exists()                  # absent sidecar: nothing assigned
    assert nightly.categories_line() == ("- categories: 1 declared · 0 assigned · "
                                         "🟡 4 none (1_a.md, 2_b.md, 3_c.md …)")


def test_73b_the_categories_line_flags_bad_declared_lines(cats_at):
    fact, assigned, _t = cats_at
    fact("1_a.md", "# a\nCategory: wizardry\n")             # bad, but assigned
    fact("2_b.md", "# b\nCategory: rules, sorcery\n")       # bad, not assigned
    fact("3_c.md", "# c\nCategory: rules\n")
    assigned(["1_a.md", "personal", "", "proposal"])
    assert nightly.categories_line() == ("- categories: 1 declared · 1 assigned · 🟡 1 none (2_b.md)"
                                         " · 🟡 2 bad declared lines (1_a.md, 2_b.md)")


def test_73b_the_categories_line_never_stops_the_nightly(cats_at, monkeypatch, tmp_path):
    monkeypatch.setattr(nightly, "CATEGORIES", tmp_path / "nowhere")
    assert nightly.categories_line().startswith("- categories: 🔴 did not run (")


def test_73b_the_categories_line_writes_nothing(cats_at):
    fact, assigned, tmp = cats_at
    fact("1_a.md", "# a\n")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in tmp.rglob("*"))
    nightly.categories_line()
    assert sorted((p.name, p.stat().st_mtime_ns) for p in tmp.rglob("*")) == before


def test_73b_main_puts_the_categories_line_right_after_retired(cats_at, monkeypatch):
    import types
    fact, assigned, tmp = cats_at
    fact("1_a.md", "# a\nCategory: rules\n")
    monkeypatch.setattr(nightly, "HERE", tmp)
    monkeypatch.setattr(nightly, "REG", tmp / "registry" / "registry.json")
    monkeypatch.setattr(nightly.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="optc: stub\n", stderr=""))
    monkeypatch.setattr(nightly, "audit", lambda: ({"facts": {"broken": 0, "epsilon": 0.0}}, []))
    monkeypatch.setattr(nightly, "cohort", lambda rows: (set(), 0))
    monkeypatch.setattr(nightly, "score", lambda rows, seen: ([], [], []))
    monkeypatch.setattr(nightly, "retired_pairs", lambda: [])
    for name in ("registry_line", "facts_line", "interim_line", "retired_line", "vault_line", "machine_line",
                 "gaps_line", "correction_line", "house_line", "canary_line", "replay_line", "coldread_line",
                 "corrections_line", "reviews_line", "veto_line", "seatswap_line", "provenance_line",
                 "proposals_line", "timeforward_line", "hookcorpus_line"):
        monkeypatch.setattr(nightly, name, lambda name=name: f"- {name}: stub")
    monkeypatch.setattr(nightly, "heartbeat_line", lambda p: "- heartbeat: stub")
    monkeypatch.setattr(nightly, "needs_seal_lines", lambda: [])
    monkeypatch.setattr(nightly, "correction_report_lines", lambda: [])
    nightly.main()
    out = (tmp / "NIGHTLY.md").read_text().splitlines()
    i = out.index("- retired_line: stub")
    assert out[i + 1] == "- categories: 1 declared · 0 assigned · 0 none"
    assert out[i + 2] == "- vault_line: stub"
    assert sum(l.startswith("- categories:") for l in out) == 1
