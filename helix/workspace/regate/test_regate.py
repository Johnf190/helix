"""Tests for regate.py — the registry write gate.

Everything runs the real CLI in a subprocess, so the exit codes are the real thing.

The registry under test is always a COPY. `fixtures/real_copy/registry.json` is a
byte-identical mode-444 copy of `~/helix/workspace/seam/registry/registry.json`;
tests copy it into tmp_path. The real registry is never read-write and never gets a
ledger beside it — one test asserts its hash is unchanged by the whole suite.
"""

import datetime
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

HERE = Path(__file__).resolve().parent
REGATE = HERE / "regate.py"
FIXTURES = HERE / "fixtures"
REAL_REGISTRY = Path.home() / "helix/workspace/seam/registry/registry.json"
SEALGATE = Path.home() / "helix/workspace/sealgate/sealgate.py"
SEALGATE_FIXTURES = Path.home() / "helix/workspace/sealgate/fixtures/complete"
LEDGER = "REGATE_LEDGER.jsonl"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_CHECK = 4

ALL_REGISTER_PRECONDITIONS = ["R1_ID_IS_NEXT", "R2_PREDICTION_COMPLETE",
                              "R3_REGISTRY_UNDRIFTED", "R4_PREREG_VERIFIES"]
ALL_SCORE_PRECONDITIONS = ["S1_EXISTS_AND_UNDRIFTED", "S2_DUE_HAS_ARRIVED",
                           "S3_NOT_ALREADY_SCORED", "S4_NONE_NEEDS_UNDERPOWERED",
                           "S7_SEAT_SWAP", "S8_NOT_QUARANTINED", "S9_RUN_AFTER_SEAL"]
# Order 51: attach-prereg and attach-pack refuse on a quarantined entry; attach-deps' own four.
ALL_ATTACH_PRECONDITIONS = ["A1_PREDICTION_EXISTS_AND_UNDRIFTED", "A2_PREREG_SET_ONCE",
                            "A3_PREREG_VERIFIES", "A_NOT_QUARANTINED"]
ALL_PACK_PRECONDITIONS = ["A1_PREDICTION_EXISTS_AND_UNDRIFTED", "A4_PACK_SET_ONCE",
                          "A5_PACK_IN_PACKS_AND_VERIFIES", "A_NOT_QUARANTINED"]
ALL_DEPS_PRECONDITIONS = ["D1_ENTRY_EXISTS_AND_UNDRIFTED", "D2_DEPS_SET_ONCE",
                          "D3_EDGES_RESOLVE", "D4_NO_CYCLE"]


# Order 41: the suite's refusals go to temp logs, never to the live refusals.log beside
# regate.py or sealgate.py. Every run goes through a shim (a file in a temp dir named like the
# gate) that loads the real gate, points its REFUSALS_LOG (and regate's SEALGATE) at temp
# paths, and calls its main(): the code and the exit codes are the real thing.
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


REGATE_RUN = REGATE            # replaced by the shim for the whole session, below
SEALGATE_RUN = SEALGATE
REFUSALS_LOG = None            # regate's temp refusals log
SEALGATE_REFUSALS_LOG = None
REVIEWS_LEDGER = None          # order 47b: sealgate's temp REVIEWS.jsonl
review = None                  # order 47b: sealgate's review.py, writing to that temp ledger
VETOES_LEDGER = None           # order 48: sealgate's temp VETOES.jsonl
veto = None                    # order 48: veto.py, writing to that temp ledger

# Order 47b: sealgate's P9 refuses a token unless the seal has an `assign` + `review` record.
# sealed_prereg() makes them with the real review.py, on the temp ledger, with a fixture reply
# in place of the reviewer (no reviewer is ever called). The shim points sealgate's
# REVIEWS_LEDGER at the same temp file; nothing in sealgate or review.py can be redirected.
REVIEW_PY = SEALGATE.parent / "review.py"
FIXTURE_REVIEW = ("[F1] The bar of 0.72 is asserted, not derived.\n"
                  "[F2] B2 could be met by an artefact of the split.\n"
                  "[F3] No positive control.\n")


# Order 48: sealgate's P10 refuses a token unless Alice was run against the seal's current body.
# sealed_prereg() gives it a NO_BREAK run with the real veto.py, on a temp VETOES.jsonl and a temp
# bulk dir, with a fake Alice that writes NO_BREAK.md (Alice is never called). The shim points
# sealgate's VETOES_LEDGER at the same temp file; nothing in sealgate or veto.py is redirected.
VETO_PY = SEALGATE.parent.parent / "veto" / "veto.py"

# Order 49: S7 refuses to score an experiment without a seat-swap AGREE for its attached pack's
# current manifest. Packs are made with the real replay.py on a temp PACKS.jsonl and bulk dir, and
# swapped with the real seatswap.py on a temp SWAPS.jsonl, with a fake Alice (Alice is never
# called). regate asks seatswap through a shim pointed at the same temp files; nothing in regate,
# replay.py or seatswap.py is redirected otherwise.
SEATSWAP_PY = SEALGATE.parent.parent / "seatswap" / "seatswap.py"
REPLAY_PY = SEALGATE.parent.parent / "replay" / "replay.py"
SEATSWAP_RUN = None
SWAPS_LEDGER = PACKS_LEDGER = PACK_BULK = None
REPLAYS_LEDGER = None          # order 51: regate reads REPLAYS.jsonl (and SWAPS.jsonl) as files
ATTEMPTS_LOG = None            # order 60: S9 reads a temp ATTEMPTS.jsonl, emptied before each test
seatswap = replay = None
FIXTURE_PACK = "regate_fixture_pack"      # made once per session, with one AGREE
PACK_SCRIPT = ('"""Total of column x over the rows of data.csv (header x)."""\n'
               "import csv, sys\n"
               "print(f\"total: {sum(float(r['x']) for r in csv.DictReader(open(sys.argv[1])))}\")\n")
PACK_DATA = "x\n1234.5\n2345.25\n"
SWAP_RIGHT = ("import csv, json\n"
              "t = sum(float(r['x']) for r in csv.DictReader(open('../pack/data.csv')))\n"
              "print(json.dumps({'total': t}))\n")
SWAP_WRONG = SWAP_RIGHT.replace("{'total': t}", "{'total': t + 1}")


def make_pack(tmp, name, swap_text=None):
    """A real replay pack in the temp PACKS.jsonl; if swap_text is given, one seat-swap run of a
    fake Alice that writes it as swap.py. → the run's record, or None."""
    src = Path(tmp) / f"src_{name}"
    src.mkdir(parents=True)
    (src / "score_33b.py").write_text(PACK_SCRIPT)
    (src / "data.csv").write_text(PACK_DATA)
    replay.make(name, f"python3 {src / 'score_33b.py'} {src / 'data.csv'}", [src / "data.csv"],
                {"total": {"value": 3579.75, "pattern": r"^total: ([\d.]+)$"}}, {"total": 0},
                note="fixture")
    if swap_text is None:
        return None

    def fake_alice(workdir, prompt):
        (Path(workdir) / "swap.py").write_text(swap_text)
        return {"model": "fixture"}
    seatswap.ALICE = fake_alice
    return seatswap.run(name)


def load_seatswap(tmp):
    global seatswap, replay
    replay = load_module("regate_test_replay", REPLAY_PY)
    replay.PACKS, replay.BULK, replay.MOUNT = PACKS_LEDGER, PACK_BULK, None
    seatswap = load_module("regate_test_seatswap", SEATSWAP_PY)
    seatswap.LEDGER, seatswap.PACKS, seatswap.PACK_BULK = SWAPS_LEDGER, PACKS_LEDGER, PACK_BULK
    seatswap.BULK = Path(tmp) / "swap_runs"
    assert make_pack(tmp, FIXTURE_PACK, SWAP_RIGHT)["outcome"] == "AGREE"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def swapped(registry, pid, pack=FIXTURE_PACK):
    """Attach a pack with an AGREE (order 49's S7) through the real attach-pack verb."""
    res = run("attach-pack", registry, pid, pack)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def load_veto(ledger, bulk, reviews):
    spec = importlib.util.spec_from_file_location("regate_test_veto", VETO_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LEDGER, mod.BULK, mod.REVIEWS_LEDGER = Path(ledger), Path(bulk), Path(reviews)

    def fake_alice(scratch, prompt):
        (Path(scratch) / "NO_BREAK.md").write_text("fixture: no break tried\n")
        return {"model": "fixture"}
    mod.ALICE = fake_alice
    return mod


def load_review(ledger):
    spec = importlib.util.spec_from_file_location("regate_test_review", REVIEW_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LEDGER = Path(ledger)
    for name in mod.REVIEWER_NAMES:
        mod.REVIEWERS[name] = lambda prompt: {"text": FIXTURE_REVIEW,
                                              "meta": {"model": "fixture"}}
    return mod


@pytest.fixture(autouse=True, scope="session")
def refusals_to_temp_logs(tmp_path_factory):
    global REGATE_RUN, SEALGATE_RUN, REFUSALS_LOG, SEALGATE_REFUSALS_LOG, REVIEWS_LEDGER, review
    global VETOES_LEDGER, veto, SEATSWAP_RUN, SWAPS_LEDGER, PACKS_LEDGER, PACK_BULK
    global REPLAYS_LEDGER, ATTEMPTS_LOG
    d = tmp_path_factory.mktemp("gates")
    REFUSALS_LOG = d / "regate_refusals.log"
    SEALGATE_REFUSALS_LOG = d / "sealgate_refusals.log"
    REVIEWS_LEDGER = d / "REVIEWS.jsonl"
    VETOES_LEDGER = d / "VETOES.jsonl"
    SEALGATE_RUN = write_shim(d / "sealgate", SEALGATE, REFUSALS_LOG=SEALGATE_REFUSALS_LOG,
                              REVIEWS_LEDGER=REVIEWS_LEDGER, VETOES_LEDGER=VETOES_LEDGER)
    SWAPS_LEDGER, PACKS_LEDGER, PACK_BULK = d / "SWAPS.jsonl", d / "PACKS.jsonl", d / "packs"
    PACK_BULK.mkdir()
    SEATSWAP_RUN = write_shim(d / "seatswap", SEATSWAP_PY, LEDGER=SWAPS_LEDGER,
                              PACKS=PACKS_LEDGER, PACK_BULK=PACK_BULK, BULK=d / "swap_runs")
    REPLAYS_LEDGER = d / "REPLAYS.jsonl"
    ATTEMPTS_LOG = d / "ATTEMPTS.jsonl"
    REGATE_RUN = write_shim(d / "regate", REGATE, REFUSALS_LOG=REFUSALS_LOG,
                            SEALGATE=SEALGATE_RUN, SEATSWAP=SEATSWAP_RUN,
                            PACKS=PACKS_LEDGER, REPLAYS=REPLAYS_LEDGER, SWAPS=SWAPS_LEDGER,
                            ATTEMPTS=ATTEMPTS_LOG)
    load_seatswap(d)
    if REVIEW_PY.exists():
        review = load_review(REVIEWS_LEDGER)
    if VETO_PY.exists():
        veto = load_veto(VETOES_LEDGER, d / "veto_bulk", REVIEWS_LEDGER)
    yield


@pytest.fixture(autouse=True)
def no_attempts_logged():
    """Order 60: every test starts with an empty temp ATTEMPTS.jsonl; the live one is never read."""
    ATTEMPTS_LOG.write_text("")
    yield


def ran_after_seal(prereg, scope="legacy", offset=1):
    """Order 60 (S9): log one run of `scope` in the temp ATTEMPTS.jsonl, `offset` seconds after
    the prereg token's issued_at. A positive offset waits until that time has passed, so the
    run is never in the future when regate reads it. -> the ts written."""
    import time
    issued = json.loads((Path(prereg).parent / "RUN_TOKEN.json").read_text())["issued_at"]
    at = datetime.datetime.fromisoformat(issued.replace("Z", "+00:00")) \
        + datetime.timedelta(seconds=offset)
    while datetime.datetime.now(datetime.timezone.utc) < at:
        time.sleep(0.1)
    ts = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(ATTEMPTS_LOG, "a") as fh:
        fh.write(json.dumps({"scope": scope, "kind": "run", "n": 1, "cmd": "true", "ts": ts})
                 + "\n")
    return ts


def run(*args):
    return subprocess.run([sys.executable, str(REGATE_RUN), *map(str, args)],
                          capture_output=True, text=True)


def failed(stderr):
    return re.findall(r"^FAILED\s+([RSATNVD]\d_[A-Z_]+|A_NOT_QUARANTINED)", stderr, flags=re.M)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def registry(tmp_path):
    """A writable copy of the real registry, with no ledger yet."""
    dst = tmp_path / "registry.json"
    shutil.copy(FIXTURES / "real_copy" / "registry.json", dst)
    dst.chmod(0o644)
    return dst


@pytest.fixture
def based(registry):
    """A copy with a baseline ledger recorded."""
    res = run("baseline", registry)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    return registry


def edit(registry, fn):
    """Hand-edit the registry JSON, the way a person with an editor would.

    Order 29: an edit that changes nothing proves nothing (a tamper test whose "edit" wrote the
    value already stored would then fail on a correct `check`), so it is refused here."""
    reg = json.loads(registry.read_text())
    before = json.loads(registry.read_text())
    fn(reg)
    assert reg != before, "edit() changed nothing: a no-op edit tests nothing"
    registry.write_text(json.dumps(reg, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ baseline

def test_baseline_records_every_prediction(registry):
    res = run("baseline", registry)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    ledger = registry.parent / LEDGER
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert [l["id"] for l in lines] == ["0000-p1", "0001-p1", "0002-p1", "0003-p1", "0004-p1"]
    assert all(l["kind"] == "baseline" for l in lines)
    assert all(len(l["frozen_sha256"]) == 64 for l in lines)


def test_baseline_records_the_scores_that_already_exist(registry):
    """0002-p1 and 0003-p1 are already scored in the real registry."""
    run("baseline", registry)
    lines = {json.loads(l)["id"]: json.loads(l)
             for l in (registry.parent / LEDGER).read_text().splitlines()}
    assert lines["0002-p1"]["score"]["outcome"] == 1
    assert lines["0003-p1"]["score"]["outcome"] == 0
    assert lines["0000-p1"]["score"] is None


def test_baseline_records_absent_fields_as_absent(registry):
    """0002-p1 has no min_n. Absent must not be encoded as null."""
    run("baseline", registry)
    lines = {json.loads(l)["id"]: json.loads(l)
             for l in (registry.parent / LEDGER).read_text().splitlines()}
    assert "min_n" in lines["0000-p1"]["frozen"]
    assert "min_n" not in lines["0002-p1"]["frozen"]


def test_baseline_is_append_only(based):
    """A second baseline appends; it does not rewrite what the first one said."""
    ledger = based.parent / LEDGER
    first = ledger.read_text()
    res = run("baseline", based)
    assert res.returncode == EXIT_OK, res.stderr
    assert ledger.read_text().startswith(first), "earlier lines must survive verbatim"


# --------------------------------------------------------------------- check

def test_check_passes_straight_after_baseline(based):
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_check_refuses_when_there_is_no_ledger(registry):
    res = run("check", registry)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "UNRECORDED" in res.stderr


def test_the_goalpost_attack_on_min_n(based):
    """min_n 30 -> 10: the exact move the protocol forbids."""
    edit(based, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0000-p1 field min_n 30 -> 10" in res.stderr, res.stderr


def test_the_goalpost_attack_on_p(based):
    edit(based, lambda r: r["0000"]["predictions"][0].update(p=0.61))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0000-p1 field p 0.6 -> 0.61" in res.stderr, res.stderr


def test_the_goalpost_attack_on_one_word_of_kill(based):
    def one_word(r):
        pred = r["0000"]["predictions"][0]
        pred["kill"] = pred["kill"].replace("LOWER", "HIGHER") if "LOWER" in pred["kill"] \
            else pred["kill"].replace("retires", "revises", 1)
    edit(based, one_word)
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0000-p1 field kill" in res.stderr, res.stderr


@pytest.mark.parametrize("field,value", [
    ("due", "2027-12-21"),
    ("metric", "something_else"),
    ("statement", "A quietly rewritten statement."),
])
def test_the_goalpost_attack_on_other_frozen_fields(based, field, value):
    edit(based, lambda r: r["0000"]["predictions"][0].update({field: value}))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert f"GOALPOST MOVED: 0000-p1 field {field}" in res.stderr, res.stderr


def test_removing_a_frozen_field_is_caught(based):
    edit(based, lambda r: r["0000"]["predictions"][0].pop("min_n"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0000-p1 field min_n 30 -> <absent>" in res.stderr, res.stderr


def test_a_scope_cut_recorded_in_status_is_legitimate(based):
    """status, entry-level notes and sealed_code are NOT frozen.

    CHANGED IN ORDER 06. Until v1.1 this test edited `status` by hand and expected
    `check` to pass. Order 06 §A.4 makes a status change go through `regate status`
    so the record keeps it; a hand edit is now reported STATUS CHANGED OFF-LEDGER.
    The assertion the test was making — a scope cut is legitimate and must stay
    possible — is unchanged, and is now made through the sanctioned path.
    """
    assert run("status", based, "0000",
               "scope cut hard by cold review — see #0009").returncode == EXIT_OK

    def cut(r):
        r["0003"]["sealed_code"] = "deadbee"
        r["0001"]["note"] = "an entry-level note added later"
    edit(based, cut)
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(based.read_text())["0000"]["status"] == \
        "scope cut hard by cold review — see #0009"


def test_a_hand_registered_entry_is_unrecorded(based):
    def add(r):
        r["0005"] = {"claim": "Added by hand, bypassing the gate.", "status": "open",
                     "predictions": [{"id": "0005-p1", "due": "2027-01-01", "p": 0.5,
                                      "metric": "m", "statement": "s", "kill": "k"}]}
    edit(based, add)
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "UNRECORDED: 0005-p1" in res.stderr, res.stderr


def test_a_deleted_entry_is_caught(based):
    edit(based, lambda r: r.pop("0001"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "DELETED: 0001-p1" in res.stderr, res.stderr


def test_a_rewritten_score_is_caught(based):
    """0002-p1 was scored outcome 1. Flipping it after the fact is the whole point."""
    edit(based, lambda r: r["0002"]["predictions"][0].update(outcome=0, brier=0.36))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "SCORE REWRITTEN: 0002-p1" in res.stderr, res.stderr


def test_rewriting_only_the_score_note_is_caught(based):
    edit(based, lambda r: r["0002"]["predictions"][0].update(note="PASS: a kinder summary."))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "SCORE REWRITTEN: 0002-p1" in res.stderr, res.stderr


def test_a_tampered_ledger_hash_is_caught(based):
    ledger = based.parent / LEDGER
    lines = ledger.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["frozen"]["min_n"] = 10
    rec["frozen_sha256"] = hashlib.sha256(b"whatever").hexdigest()
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ledger.write_text("\n".join(lines) + "\n")
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "LEDGER TAMPERED" in res.stderr, res.stderr


def test_a_deleted_ledger_line_is_caught(based):
    ledger = based.parent / LEDGER
    lines = ledger.read_text().splitlines()
    del lines[2]
    ledger.write_text("\n".join(lines) + "\n")
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "LEDGER TAMPERED" in res.stderr, res.stderr


# ------------------------------------------------------------------ register

def test_register_adds_one_entry_and_check_still_passes(based):
    res = run("register", based, FIXTURES / "entries" / "0005_ok.json")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    reg = json.loads(based.read_text())
    assert "0005" in reg and reg["0005"]["predictions"][0]["id"] == "0005-p1"
    assert list(reg) == ["0000", "0001", "0002", "0003", "0004", "0005"]
    assert run("check", based).returncode == EXIT_OK
    lines = [json.loads(l) for l in (based.parent / LEDGER).read_text().splitlines()]
    assert lines[-1]["id"] == "0005-p1" and lines[-1]["kind"] == "register"


def test_register_keeps_the_nightlys_json_format(based):
    run("register", based, FIXTURES / "entries" / "0005_ok.json")
    text = based.read_text()
    assert text.startswith('{\n "0000": {\n  "claim"'), text[:60]


def test_register_leaves_no_temp_file_behind(based):
    run("register", based, FIXTURES / "entries" / "0005_ok.json")
    leftovers = [p.name for p in based.parent.iterdir()
                 if p.name not in ("registry.json", LEDGER)]
    assert leftovers == [], leftovers


def entry_under_id(tmp_path, new_id, name):
    """The known-good entry, re-keyed — prediction ids renamed to match, so that R1 is
    the only thing wrong with it."""
    entry = json.loads((FIXTURES / "entries" / "0005_ok.json").read_text())["0005"]
    for pred in entry["predictions"]:
        pred["id"] = pred["id"].replace("0005", new_id)
    path = tmp_path / name
    path.write_text(json.dumps({new_id: entry}, indent=1))
    return path


def test_register_refuses_an_id_that_already_exists(based, tmp_path):
    res = run("register", based, entry_under_id(tmp_path, "0004", "dupe.json"))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R1_ID_IS_NEXT"], res.stderr
    assert "already exists" in res.stderr


def test_register_refuses_an_id_that_skips_ahead(based, tmp_path):
    res = run("register", based, entry_under_id(tmp_path, "0007", "skip.json"))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R1_ID_IS_NEXT"], res.stderr
    assert "expected '0005'" in res.stderr


@pytest.mark.parametrize("fixture,needle", [
    ("0005_no_kill.json", "kill"),
    ("0005_empty_kill.json", "kill"),
    ("0005_past_due.json", "due"),
    ("0005_p_out_of_range.json", "p"),
    ("0005_no_predictions.json", "predictions"),
    ("0005_id_mismatch.json", "id"),
])
def test_register_refuses_an_incomplete_prediction(based, fixture, needle):
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("register", based, FIXTURES / "entries" / fixture)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert needle in res.stderr, res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led), \
        "a refusal must leave the registry and the ledger byte-identical"


def test_register_refuses_when_the_registry_has_already_drifted(based):
    edit(based, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("register", based, FIXTURES / "entries" / "0005_ok.json")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R3_REGISTRY_UNDRIFTED"], res.stderr
    assert "GOALPOST MOVED" in res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


# ------------------------------------------- register R4, through real sealgate

# Order 21 (sealgate P8_PREFLIGHT): a seal needs a pre-flight section naming a valid
# brief, or `token` refuses it. Built the way sealgate/test_sealgate.py builds it.
PREFLIGHT_DIR = Path.home() / "helix/workspace/preflight"
BRIEF_NAME = "PREFLIGHT_fixture.md"


def add_preflight(prereg):
    """Write a program-built brief beside the prereg and append its pre-flight section."""
    sys.path.insert(0, str(PREFLIGHT_DIR))
    import preflight
    sections = (
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
        "GO\n")
    text = preflight.build_brief(b"Idea: the synthetic probe fixture.\n", sections,
                                 searcher="Alice", model="Gemini 3.1 Pro (High)",
                                 date_utc="2026-09-30T22:00:00Z", n_queries=4)
    brief = prereg.parent / BRIEF_NAME
    brief.write_bytes(text.encode("utf-8"))
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    with open(prereg, "a", encoding="utf-8") as fh:
        fh.write(f"\n## PRE-FLIGHT\nBrief: {BRIEF_NAME}\nBrief-sha256: {digest}\n")


def sealed_prereg(tmp_path, tamper=False):
    """A sealgate-tokened pre-registration, optionally with its stamp region edited."""
    d = tmp_path / "seal"
    d.mkdir()
    shutil.copy(SEALGATE_FIXTURES / "PREREG.md", d / "PREREG.md")
    shutil.copy(SEALGATE_FIXTURES / "FINDINGS.md", d / "FINDINGS.md")
    add_preflight(d / "PREREG.md")
    reviewer, _ = review.assign(d / "PREREG.md", REVIEWS_LEDGER)     # order 47b: P9
    review.run(d / "PREREG.md", REVIEWS_LEDGER)
    sg = [sys.executable, str(SEALGATE_RUN)]
    assert subprocess.run(sg + ["stamp", str(d / "PREREG.md"), "--reviewer", reviewer,
                                "--findings", "FINDINGS.md"],
                          capture_output=True).returncode == 0
    assert veto.run(d / "PREREG.md")["outcome"] == "NO_BREAK"         # order 48: P10, as stamped
    assert subprocess.run(sg + ["token", str(d / "PREREG.md")],
                          capture_output=True).returncode == 0
    if tamper:
        p = d / "PREREG.md"
        p.write_text(re.sub(r"^(- sealgate: .*\n)", r"\1- **B4 — added AFTER the token.**\n",
                            p.read_text(), count=1, flags=re.M))
    return d / "PREREG.md"


def entry_with_prereg(tmp_path, prereg):
    entry = json.loads((FIXTURES / "entries" / "0005_ok.json").read_text())
    entry["0005"]["prereg"] = str(prereg)
    path = tmp_path / "entry_prereg.json"
    path.write_text(json.dumps(entry, indent=1))
    return path


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_register_accepts_an_entry_whose_prereg_verifies(based, tmp_path):
    entry = entry_with_prereg(tmp_path, sealed_prereg(tmp_path))
    res = run("register", based, entry)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "sealgate" in res.stdout.lower()


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_register_refuses_an_entry_whose_prereg_stamp_region_was_edited(based, tmp_path):
    entry = entry_with_prereg(tmp_path, sealed_prereg(tmp_path, tamper=True))
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("register", based, entry)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R4_PREREG_VERIFIES"], res.stderr
    assert "STAMP REGION CHANGED" in res.stderr or "stamp region" in res.stderr.lower()
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_register_refuses_a_prereg_that_does_not_exist(based, tmp_path):
    entry = entry_with_prereg(tmp_path, tmp_path / "nowhere" / "PREREG.md")
    res = run("register", based, entry)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R4_PREREG_VERIFIES"], res.stderr


# --------------------------------------------------------------------- score

@pytest.fixture
def scorable(registry):
    """A copy where 0004-p1's due date is in the past BEFORE the baseline is taken."""
    edit(registry, lambda r: r["0004"]["predictions"][0].update(due="2026-01-31"))
    assert run("baseline", registry).returncode == EXIT_OK
    return registry


def test_score_writes_the_outcome_and_computes_brier_itself(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held on real data")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    pred = json.loads(scorable.read_text())["0004"]["predictions"][0]
    assert pred["outcome"] == 1
    assert pred["brier"] == 0.25, pred          # p = 0.5 -> (0.5 - 1)^2
    assert pred["note"] == "held on real data"
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", pred["scored"])
    assert run("check", scorable).returncode == EXIT_OK


def test_score_records_the_score_in_the_ledger(scorable):
    run("score", scorable, "0004-p1", "--outcome", "0", "--note", "did not hold")
    last = json.loads((scorable.parent / LEDGER).read_text().splitlines()[-1])
    assert last["kind"] == "score" and last["id"] == "0004-p1"
    assert last["score"]["outcome"] == 0 and last["score"]["brier"] == 0.25


def test_score_has_no_way_to_supply_a_brier(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "x", "--brier", "0.0")
    assert res.returncode == EXIT_USAGE, res.stdout + res.stderr
    assert "unrecognized arguments" in res.stderr


def test_score_refuses_before_the_due_date(based):
    """0000-p1 is due 2026-12-21."""
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("score", based, "0000-p1", "--outcome", "1", "--note", "early")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S2_DUE_HAS_ARRIVED"], res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


def test_score_refuses_to_overwrite_a_written_score(based):
    """0002-p1 is already scored, outcome 1.

    S2 fires as well as S3, and that is not a test artefact: in the real registry
    0002-p1 is due 2026-12-21 and was scored 2026-09-22 — three months early, by
    hand. Both refusals are true of that entry, so both are asserted.
    """
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("score", based, "0002-p1", "--outcome", "0", "--note", "on reflection")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "S3_NOT_ALREADY_SCORED" in failed(res.stderr), res.stderr
    assert failed(res.stderr) == ["S2_DUE_HAS_ARRIVED", "S3_NOT_ALREADY_SCORED"], res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


def test_score_refuses_a_second_score_with_nothing_else_wrong(scorable):
    """S3 alone: past due, undrifted, valid outcome — and already scored by this tool."""
    assert run("score", scorable, "0004-p1", "--outcome", "1",
               "--note", "first and only").returncode == EXIT_OK
    before_reg, before_led = sha(scorable), sha(scorable.parent / LEDGER)
    res = run("score", scorable, "0004-p1", "--outcome", "0", "--note", "on reflection")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S3_NOT_ALREADY_SCORED"], res.stderr
    assert json.loads(scorable.read_text())["0004"]["predictions"][0]["outcome"] == 1
    assert (sha(scorable), sha(scorable.parent / LEDGER)) == (before_reg, before_led)


def test_score_refuses_outcome_none_without_an_underpowered_note(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "none", "--note", "not enough data")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S4_NONE_NEEDS_UNDERPOWERED"], res.stderr


def test_score_accepts_outcome_none_with_an_underpowered_note(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "none",
              "--note", "UNDERPOWERED: only 3 dual-vantage hosts (needed 30)")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    pred = json.loads(scorable.read_text())["0004"]["predictions"][0]
    assert pred["outcome"] is None and pred["brier"] is None
    assert run("check", scorable).returncode == EXIT_OK


def test_score_refuses_an_unknown_id(based):
    res = run("score", based, "9999-p1", "--outcome", "1", "--note", "x")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S1_EXISTS_AND_UNDRIFTED"], res.stderr


def test_score_refuses_on_a_drifted_registry(scorable):
    edit(scorable, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "x")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S1_EXISTS_AND_UNDRIFTED"], res.stderr


# ------------------------------------------------------------- the whole tool

def test_every_refusal_appends_one_line_to_the_refusals_log(based):
    log = REFUSALS_LOG
    before = len(log.read_text().splitlines()) if log.exists() else 0
    assert run("score", based, "0000-p1", "--outcome", "1", "--note", "early").returncode \
        == EXIT_REFUSED
    after = log.read_text().splitlines()
    assert len(after) == before + 1
    assert "S2_DUE_HAS_ARRIVED" in after[-1] and str(based) in after[-1]


def test_order41_the_suite_logs_refusals_to_a_temp_log_not_the_live_one(based):
    """The refusal is real and logged, in the session's temp log; the live log never sees it."""
    live = HERE / "refusals.log"
    assert REFUSALS_LOG.parent != HERE
    res = run("score", based, "0000-p1", "--outcome", "1", "--note", "early")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert str(based) in REFUSALS_LOG.read_text()
    assert not live.exists() or str(based) not in live.read_text()


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order41_sealgate_refusals_made_through_regate_go_to_a_temp_log_too(based, tmp_path):
    """regate asks sealgate to verify; sealgate's refusal lands in the temp sealgate log."""
    live = SEALGATE.parent / "refusals.log"
    prereg = sealed_prereg(tmp_path, tamper=True)
    res = run("register", based, entry_with_prereg(tmp_path, prereg))
    assert failed(res.stderr) == ["R4_PREREG_VERIFIES"], res.stderr
    assert str(prereg) in SEALGATE_REFUSALS_LOG.read_text()
    assert not live.exists() or str(prereg) not in live.read_text()


def test_the_gate_has_no_escape_hatch():
    src = REGATE.read_text()
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], f"escape hatch introduced: {hits}"


def test_missing_registry_is_a_usage_error(tmp_path):
    res = run("check", tmp_path / "nope.json")
    assert res.returncode == EXIT_USAGE, res.stdout + res.stderr
    assert "nope.json" in res.stderr


def test_the_real_registry_and_its_ledger_are_never_touched(registry):
    """Nothing this suite does may reach the live registry or its ledger.

    CHANGED IN ORDER 09. Until order 07 this asserted the live registry had NO ledger
    beside it. The cutover baselined it, deliberately, so the assertion became false
    for the right reason. What must stay true is that this suite works on copies: both
    live files are byte-identical before and after.
    """
    live_ledger = REAL_REGISTRY.parent / LEDGER
    assert live_ledger.exists(), "the live registry has been baselined since order 07"
    before_reg, before_led = sha(REAL_REGISTRY), sha(live_ledger)
    assert run("baseline", registry).returncode == EXIT_OK
    assert run("check", registry).returncode == EXIT_OK
    assert run("register", registry,
               FIXTURES / "entries" / "0005_ok.json").returncode == EXIT_OK
    assert (sha(REAL_REGISTRY), sha(live_ledger)) == (before_reg, before_led)


SEAM_BEFORE_CUTOVER = "a80ef9f"     # seam, immediately before order 07 labelled the kinds


def test_the_fixture_copy_is_the_pre_cutover_registry():
    """The fixture is pinned to a commit, not to the live file.

    CHANGED IN ORDER 09. It used to assert the fixture matched the LIVE registry, which
    order 07 changed by adding five `kind` lines. The fixture must stay kind-free —
    `test_a_prediction_with_no_kind_is_treated_as_time` needs a registry that declares
    none — so what is checked now is that the fixture is still byte-identical to the
    blob it was copied from. That can fail if someone edits the fixture; it cannot fail
    merely because the live registry moved on.
    """
    blob = subprocess.run(["git", "-C", str(Path.home() / "helix/workspace/seam"), "show",
                           f"{SEAM_BEFORE_CUTOVER}:registry/registry.json"],
                          capture_output=True, check=True).stdout
    assert hashlib.sha256(blob).hexdigest() == sha(FIXTURES / "real_copy" / "registry.json"), \
        "the fixture is no longer the pre-cutover registry it claims to be"


# ==========================================================================
# ORDER 06 — regate v1.1: kinds, set-once prereg, frozen claim, logged status.
#
# Order 05 found that 0002-p1/0003-p1 were scored three months before their due
# date. That was legitimate — they are EXPERIMENTS, where `due` means "run by" —
# but the registry could not tell that apart from cheating on a TIME prediction.
# `kind` makes the difference explicit, and an experiment may only be scored early
# if its pre-registration still verifies under sealgate.
# ==========================================================================

KINDS = {"0000": "time", "0001": "time",
         "0002": "experiment", "0003": "experiment", "0004": "experiment"}


def add_kinds(registry, kinds=KINDS):
    edit(registry, lambda r: [r[e]["predictions"][0].update(kind=k)
                              for e, k in kinds.items() if e in r])


@pytest.fixture
def kinded(registry):
    """A copy with kinds declared and then baselined, as order 07 will do for real."""
    add_kinds(registry)
    assert run("baseline", registry).returncode == EXIT_OK
    return registry


# ------------------------------------------------------------------ kind frozen

def test_kind_is_frozen(kinded):
    edit(kinded, lambda r: r["0000"]["predictions"][0].update(kind="experiment"))
    res = run("check", kinded)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert 'GOALPOST MOVED: 0000-p1 field kind "time" -> "experiment"' in res.stderr, res.stderr


def test_kind_appears_in_the_ledgers_frozen_fields(kinded):
    lines = {json.loads(l)["id"]: json.loads(l)
             for l in (kinded.parent / LEDGER).read_text().splitlines()}
    assert lines["0000-p1"]["frozen"]["kind"] == "time"
    assert lines["0002-p1"]["frozen"]["kind"] == "experiment"


def test_register_requires_a_kind(based):
    res = run("register", based, FIXTURES / "entries" / "0005_no_kind.json")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "kind" in res.stderr


def test_register_refuses_a_kind_it_does_not_know(based):
    res = run("register", based, FIXTURES / "entries" / "0005_bad_kind.json")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "guess" in res.stderr


def test_register_requires_a_prereg_for_an_experiment(based):
    res = run("register", based, FIXTURES / "entries" / "0005_experiment_no_prereg.json")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "prereg" in res.stderr


# ------------------------------------------------- scoring: time vs experiment

def test_a_time_prediction_cannot_be_scored_early(kinded):
    """0000-p1 is kind time, due 2026-12-21."""
    res = run("score", kinded, "0000-p1", "--outcome", "1", "--note", "early")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S2_DUE_HAS_ARRIVED"], res.stderr
    assert re.search(r"^not-evaluated\s+S5_EXPERIMENT_SEALED", res.stderr, re.M), res.stderr


def test_an_experiment_cannot_be_scored_early_without_a_prereg(kinded):
    """0004-p1 is kind experiment, due 2027-01-31, and has no prereg yet."""
    swapped(kinded, "0004-p1")                                   # order 49: S7 is not the point
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S5_EXPERIMENT_SEALED"], res.stderr
    assert "prereg" in res.stderr


def test_an_experiment_with_a_verifying_prereg_can_be_scored_early(kinded, tmp_path):
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    swapped(kinded, "0004-p1")                                   # order 49: S7
    ran_after_seal(prereg)                                       # order 60: S9
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    pred = json.loads(kinded.read_text())["0004"]["predictions"][0]
    assert pred["outcome"] == 1 and pred["brier"] == 0.25
    assert run("check", kinded).returncode == EXIT_OK


def test_an_experiment_whose_prereg_stops_verifying_cannot_be_scored_early(kinded, tmp_path):
    """Attach a good prereg, then edit its stamp region. The seal no longer holds."""
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    swapped(kinded, "0004-p1")                                   # order 49: S7 is not the point
    prereg.write_text(re.sub(r"^(- sealgate: .*\n)", r"\1- **B4 — added AFTER the token.**\n",
                             prereg.read_text(), count=1, flags=re.M))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S5_EXPERIMENT_SEALED"], res.stderr
    assert "STAMP REGION CHANGED" in res.stderr, res.stderr


def test_a_prediction_with_no_kind_is_treated_as_time(based):
    """The real registry has no kinds yet. Absent must take the strict reading."""
    res = run("score", based, "0000-p1", "--outcome", "1", "--note", "early")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "S2_DUE_HAS_ARRIVED" in failed(res.stderr), res.stderr
    assert "no kind declared" in res.stderr or "treated as time" in res.stderr, res.stderr


def test_an_experiment_past_its_due_date_needs_no_seal(registry):
    edit(registry, lambda r: r["0004"]["predictions"][0].update(kind="experiment",
                                                                due="2026-01-31"))
    assert run("baseline", registry).returncode == EXIT_OK
    swapped(registry, "0004-p1")                                 # order 49: S7
    res = run("score", registry, "0004-p1", "--outcome", "0", "--note", "ran it, it failed")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


# ----------------------------------------------------------- prereg is set-once

def test_attach_prereg_records_the_path_in_the_ledger(kinded, tmp_path):
    prereg = sealed_prereg(tmp_path)
    res = run("attach-prereg", kinded, "0004-p1", prereg)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(kinded.read_text())["0004"]["predictions"][0]["prereg"] == str(prereg)
    last = json.loads((kinded.parent / LEDGER).read_text().splitlines()[-1])
    assert last["kind"] == "attach-prereg" and last["frozen"]["prereg"] == str(prereg)
    assert run("check", kinded).returncode == EXIT_OK


def test_attach_prereg_refuses_a_prereg_that_does_not_verify(kinded, tmp_path):
    prereg = sealed_prereg(tmp_path, tamper=True)
    before_reg, before_led = sha(kinded), sha(kinded.parent / LEDGER)
    res = run("attach-prereg", kinded, "0004-p1", prereg)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["A3_PREREG_VERIFIES"], res.stderr
    assert (sha(kinded), sha(kinded.parent / LEDGER)) == (before_reg, before_led)


def test_attach_prereg_is_set_once(kinded, tmp_path):
    first = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", first).returncode == EXIT_OK
    second = tmp_path / "second"
    second.mkdir()
    shutil.copytree(first.parent, second / "seal")
    res = run("attach-prereg", kinded, "0004-p1", second / "seal" / "PREREG.md")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["A2_PREREG_SET_ONCE"], res.stderr
    assert json.loads(kinded.read_text())["0004"]["predictions"][0]["prereg"] == str(first)


def test_swapping_a_prereg_path_by_hand_is_a_moved_goalpost(kinded, tmp_path):
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    edit(kinded, lambda r: r["0004"]["predictions"][0].update(prereg="/somewhere/else.md"))
    res = run("check", kinded)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004-p1 field prereg" in res.stderr, res.stderr


def test_removing_a_prereg_by_hand_is_a_moved_goalpost(kinded, tmp_path):
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    edit(kinded, lambda r: r["0004"]["predictions"][0].pop("prereg"))
    res = run("check", kinded)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004-p1 field prereg" in res.stderr, res.stderr
    assert "<absent>" in res.stderr


def test_adding_a_prereg_by_hand_is_a_moved_goalpost(kinded):
    edit(kinded, lambda r: r["0004"]["predictions"][0].update(prereg="/added/by/hand.md"))
    res = run("check", kinded)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004-p1 field prereg <absent> ->" in res.stderr, res.stderr


# ------------------------------------------------------------- claim is frozen

def test_rewording_a_claim_is_a_moved_goalpost(based):
    edit(based, lambda r: r["0000"].update(
        claim="A claim that passes the gate breaks less often. Probably."))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0000 field claim" in res.stderr, res.stderr


def test_the_claim_is_recorded_in_the_ledger(based):
    first = json.loads((based.parent / LEDGER).read_text().splitlines()[0])
    assert first["entry_frozen"]["claim"].startswith("A claim that passes the five-attachment")
    assert len(first["entry_frozen_sha256"]) == 64


# ------------------------------------------------------------- status is logged

def test_a_hand_edited_status_is_reported_off_ledger(based):
    edit(based, lambda r: r["0001"].update(status="quietly retired"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "STATUS CHANGED OFF-LEDGER: 0001" in res.stderr, res.stderr


def test_the_same_change_through_the_status_verb_passes(based):
    res = run("status", based, "0001", "scope cut after cold review — see #0009")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(based.read_text())["0001"]["status"] == \
        "scope cut after cold review — see #0009"
    assert run("check", based).returncode == EXIT_OK
    last = json.loads((based.parent / LEDGER).read_text().splitlines()[-1])
    assert last["kind"] == "status" and last["entry"] == "0001"
    assert last["status"] == "scope cut after cold review — see #0009"
    assert last["prev_status"] == "open"


def test_status_refuses_an_unknown_entry(based):
    res = run("status", based, "9999", "whatever")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["T1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr


def test_status_refuses_empty_text(based):
    res = run("status", based, "0001", "   ")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["T2_STATUS_NOT_EMPTY"], res.stderr


def test_status_refuses_on_a_drifted_registry(based):
    edit(based, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    res = run("status", based, "0001", "a new status")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["T1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr


def test_sealed_code_and_entry_notes_are_still_unfrozen(based):
    def cut(r):
        r["0003"]["sealed_code"] = "deadbee"
        r["0001"]["note"] = "an entry-level note added later"
    edit(based, cut)
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


# ==========================================================================
# ORDER 09 L3 — the prose entry files are not covered by the lock.
#
# seam/registry/0000_protocol.md and its siblings carry the claim in prose. They
# could be reworded while `check` passed, because the freeze covers the JSON only.
# A narrative is attached by path + sha256 of its raw bytes, recorded in the LEDGER
# and not in the registry JSON, and an edit that did not go through the gate is
# reported.
# ==========================================================================

NARRATIVE = """# 0000 — the protocol

A claim that passes the five-attachment gate breaks less often than one that does not.
This is the prose the JSON entry points at.
"""


@pytest.fixture
def narrative(tmp_path):
    p = tmp_path / "0000_protocol.md"
    p.write_text(NARRATIVE)
    return p


def test_attach_narrative_records_the_path_and_hash_in_the_ledger(based, narrative):
    before_json = sha(based)
    res = run("attach-narrative", based, "0000", narrative)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    last = json.loads((based.parent / LEDGER).read_text().splitlines()[-1])
    assert last["kind"] == "narrative" and last["entry"] == "0000"
    assert last["narrative_path"] == str(narrative)
    assert last["narrative_sha256"] == hashlib.sha256(narrative.read_bytes()).hexdigest()
    assert sha(based) == before_json, \
        "the narrative lives in the ledger; the registry JSON must not be touched"
    assert run("check", based).returncode == EXIT_OK


def test_the_check_message_counts_the_narratives(based, narrative):
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "5 prediction(s)" in res.stdout and "1 narrative(s)" in res.stdout, res.stdout


def test_attach_narrative_is_set_once(based, narrative, tmp_path):
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    other = tmp_path / "other.md"
    other.write_text("# a different prose file\n")
    res = run("attach-narrative", based, "0000", other)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["N2_NARRATIVE_SET_ONCE"], res.stderr


def test_attach_narrative_refuses_a_file_that_is_not_there(based, tmp_path):
    res = run("attach-narrative", based, "0000", tmp_path / "nope.md")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["N3_NARRATIVE_EXISTS"], res.stderr
    assert "nope.md" in res.stderr


def test_attach_narrative_refuses_an_unknown_entry(based, narrative):
    res = run("attach-narrative", based, "9999", narrative)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["N1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr


def test_attach_narrative_refuses_on_a_drifted_registry(based, narrative):
    edit(based, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    res = run("attach-narrative", based, "0000", narrative)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["N1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr


def test_a_narrative_reworded_off_ledger_is_caught(based, narrative):
    """The attack this verb exists for: change the prose, leave the JSON alone."""
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    narrative.write_text(NARRATIVE.replace("breaks less often", "breaks less often, probably"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert f"NARRATIVE CHANGED OFF-LEDGER: 0000 {narrative}" in res.stderr, res.stderr


def test_a_narrative_that_has_gone_missing_is_caught(based, narrative):
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    narrative.rename(narrative.with_suffix(".moved"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert f"NARRATIVE MISSING: 0000 {narrative}" in res.stderr, res.stderr


def test_revise_narrative_is_the_legitimate_way_to_edit_one(based, narrative):
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    was = hashlib.sha256(narrative.read_bytes()).hexdigest()
    narrative.write_text(NARRATIVE + "\nAdded after the cold review, deliberately.\n")
    now = hashlib.sha256(narrative.read_bytes()).hexdigest()
    assert run("check", based).returncode == EXIT_CHECK, "the edit must be visible first"
    res = run("revise-narrative", based, "0000", "cold review added a scope note")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert run("check", based).returncode == EXIT_OK
    last = json.loads((based.parent / LEDGER).read_text().splitlines()[-1])
    assert last["kind"] == "revise-narrative"
    assert last["prev_narrative_sha256"] == was
    assert last["narrative_sha256"] == now
    assert last["reason"] == "cold review added a scope note"


def test_revise_narrative_needs_a_reason_in_writing(based, narrative):
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    narrative.write_text(NARRATIVE + "\nquietly\n")
    res = run("revise-narrative", based, "0000", "  ")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["V3_REASON_IN_WRITING"], res.stderr


def test_revise_narrative_refuses_an_entry_with_no_narrative(based):
    res = run("revise-narrative", based, "0001", "there is nothing to revise")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["V1_NARRATIVE_ATTACHED"], res.stderr


def test_revise_narrative_refuses_when_the_registry_has_other_drift(based, narrative):
    """It tolerates the narrative mismatch it is about to record, and nothing else."""
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    narrative.write_text(NARRATIVE + "\nrevised\n")
    edit(based, lambda r: r["0000"]["predictions"][0].update(min_n=10))
    res = run("revise-narrative", based, "0000", "a real reason")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["V2_REGISTRY_OTHERWISE_UNDRIFTED"], res.stderr
    assert "GOALPOST MOVED" in res.stderr


def test_two_entries_can_each_have_their_own_narrative(based, narrative, tmp_path):
    second = tmp_path / "0001_absolute_paths.md"
    second.write_text("# 0001 — absolute paths\n\nThe prose for entry 0001.\n")
    assert run("attach-narrative", based, "0000", narrative).returncode == EXIT_OK
    assert run("attach-narrative", based, "0001", second).returncode == EXIT_OK
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "2 narrative(s)" in res.stdout
    second.write_text("# 0001 — absolute paths\n\nReworded.\n")
    res = run("check", based)
    assert res.returncode == EXIT_CHECK
    assert "NARRATIVE CHANGED OFF-LEDGER: 0001" in res.stderr
    assert "0000" not in res.stderr.split("NARRATIVE CHANGED OFF-LEDGER: 0001")[1], \
        "only the one that moved is named"


# ==========================================================================
# ORDER 25b — amendment 1a: THE FALSIFIER HAS A CONSEQUENCE.
#
# A kill condition was prose: meeting it changed nothing unless someone said so.
# Now a prediction carries a frozen `kill_rule` beside its prose `kill` — either
# {"on": "outcome"|"value", "op": ..., "value": <number>} or the literal "prose".
# A scored outcome that meets a structured rule sets the entry INVALID in the same
# call; INVALID is terminal; and a new entry that restates an INVALID claim in the
# same scope must cite it and declare a structural change (R5). INVALID is local to
# its `scope` (the user, 2026-10-01): it never propagates, and R5 only refuses inside it.
# ==========================================================================

VALUE_RULE = {"on": "value", "op": "<", "value": 0.05}
OUTCOME_RULE = {"on": "outcome", "op": "==", "value": 0}
SIMILARITY = json.loads((FIXTURES / "similarity" / "groups.json").read_text())
CLAIM_A = SIMILARITY["original"]["claim"]
STMT_A = SIMILARITY["original"]["statements"][0]
PARAPHRASE = SIMILARITY["paraphrases"][0]
DIFFERENT = SIMILARITY["different"][0]
REAL_WHAT = "a per-request trace instrument replaces the median-of-replay timer"


def make_entry(eid, claim=CLAIM_A, statement=STMT_A, scope="proj/a", kill_rule=VALUE_RULE,
               kind="time", due="2027-06-30", p=0.6, **extra):
    pred = {"id": f"{eid}-p1", "kind": kind, "due": due, "p": p,
            "metric": "latency_reduction_fraction", "statement": statement,
            "kill": "A reduction below 0.05 retires the claim in this scope."}
    if kill_rule is not None:
        pred["kill_rule"] = kill_rule
    entry = {"claim": claim, "registered": "2026-10-01", "status": "open",
             "predictions": [pred]}
    if scope is not None:
        entry["scope"] = scope
    entry.update(extra)
    return {eid: entry}


def write_entry(tmp_path, payload, name=None):
    path = tmp_path / (name or f"entry_{next(iter(payload))}_{len(list(tmp_path.iterdir()))}.json")
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    return path


def preload(registry, *payloads):
    """Entries added before the baseline, past due, so they can be scored today.

    The same trick as `scorable`: register refuses a past due date, and scoring an
    experiment early needs a real seal, so most scoring tests start from here."""
    def add(r):
        for payload in payloads:
            for eid, entry in payload.items():
                for pred in entry["predictions"]:
                    pred["due"] = "2026-01-31"
                r[eid] = entry
    edit(registry, add)
    assert run("baseline", registry).returncode == EXIT_OK
    return registry


def ledger_lines(registry):
    return [json.loads(l) for l in (registry.parent / LEDGER).read_text().splitlines()]


def load_regate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("regate_under_test", REGATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REFUSALS_LOG, mod.SEALGATE = REFUSALS_LOG, SEALGATE_RUN    # order 41
    mod.PACKS, mod.REPLAYS, mod.SWAPS = PACKS_LEDGER, REPLAYS_LEDGER, SWAPS_LEDGER   # order 51
    return mod


# ------------------------------------------------------------ R2: kill_rule

def test_register_requires_a_kill_rule(based, tmp_path):
    res = run("register", based, write_entry(tmp_path, make_entry("0005", kill_rule=None)))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "kill_rule" in res.stderr


@pytest.mark.parametrize("rule", [
    {"on": "value", "op": "!=", "value": 0.05},
    {"on": "brier", "op": "<", "value": 0.05},
    {"on": "value", "op": "<", "value": "0.05"},
    {"on": "value", "op": "<", "value": True},
    {"on": "value", "op": "<"},
    {"on": "value", "op": "<", "value": 0.05, "unless": "it looks good"},
    {"on": "outcome", "op": "==", "value": 0.5},
    "PROSE",
    "",
    7,
])
def test_register_refuses_a_malformed_kill_rule(based, tmp_path, rule):
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    res = run("register", based, write_entry(tmp_path, make_entry("0005", kill_rule=rule)))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "kill_rule" in res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


def test_a_structured_kill_rule_is_registered_and_not_flagged(based, tmp_path):
    res = run("register", based, write_entry(tmp_path, make_entry("0005")))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "PROSE KILL" not in res.stdout
    last = ledger_lines(based)[-1]
    assert last["kind"] == "register" and last["kill_prose"] is False
    assert last["frozen"]["kill_rule"] == VALUE_RULE


def test_a_prose_kill_rule_is_allowed_but_flagged_at_registration(based, tmp_path):
    res = run("register", based, write_entry(tmp_path, make_entry("0005", kill_rule="prose")))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "PROSE KILL: 0005-p1 (not auto-evaluable)" in res.stdout, res.stdout
    assert ledger_lines(based)[-1]["kill_prose"] is True


def test_kill_rule_is_frozen(based, tmp_path):
    assert run("register", based, write_entry(tmp_path, make_entry("0005"))).returncode == 0
    edit(based, lambda r: r["0005"]["predictions"][0]["kill_rule"].update(value=0.01))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0005-p1 field kill_rule" in res.stderr, res.stderr


def test_kill_rule_cannot_be_downgraded_to_prose_by_hand(based, tmp_path):
    assert run("register", based, write_entry(tmp_path, make_entry("0005"))).returncode == 0
    edit(based, lambda r: r["0005"]["predictions"][0].update(kill_rule="prose"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0005-p1 field kill_rule" in res.stderr, res.stderr


# --------------------------------------------------------------- R2: scope

def test_register_requires_a_scope(based, tmp_path):
    res = run("register", based, write_entry(tmp_path, make_entry("0005", scope=None)))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "scope" in res.stderr


@pytest.mark.parametrize("scope", ["", "   ", "Proj/A", "proj//a", "/proj", "proj/", "proj a", 3])
def test_register_refuses_a_malformed_scope(based, tmp_path, scope):
    res = run("register", based, write_entry(tmp_path, make_entry("0005", scope=scope)))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R2_PREDICTION_COMPLETE"], res.stderr
    assert "scope" in res.stderr


def test_scope_is_frozen(based, tmp_path):
    assert run("register", based, write_entry(tmp_path, make_entry("0005"))).returncode == 0
    edit(based, lambda r: r["0005"].update(scope="proj/elsewhere"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert 'GOALPOST MOVED: 0005 field scope "proj/a" -> "proj/elsewhere"' in res.stderr, \
        res.stderr


def test_supersedes_and_structural_change_are_frozen(based, tmp_path):
    # order 41: only an INVALID entry can be superseded, so 0004 is retired first
    assert run("status", based, "0004", "INVALID — decided by hand").returncode == EXIT_OK
    payload = make_entry("0005", supersedes="0004",
                         structural_change={"type": "instrument", "what": REAL_WHAT})
    assert run("register", based, write_entry(tmp_path, payload)).returncode == EXIT_OK
    edit(based, lambda r: r["0005"].update(supersedes="0003"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0005 field supersedes" in res.stderr, res.stderr
    edit(based, lambda r: r["0005"].update(supersedes="0004"))
    edit(based, lambda r: r["0005"]["structural_change"].update(type="mechanism"))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0005 field structural_change" in res.stderr, res.stderr


# ------------------------------------------------------------- score --value

def test_score_requires_a_value_for_a_value_rule(registry):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S6_VALUE_MATCHES_RULE"], res.stderr
    assert "--value" in res.stderr


def test_score_refuses_a_value_for_an_outcome_rule(registry):
    preload(registry, make_entry("0005", kill_rule=OUTCOME_RULE))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "x", "--value", "0.3")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S6_VALUE_MATCHES_RULE"], res.stderr


def test_score_refuses_a_value_for_a_legacy_prediction(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "x", "--value", "0.8")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S6_VALUE_MATCHES_RULE"], res.stderr


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_score_refuses_a_value_that_is_not_a_finite_number(registry, bad):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "x", f"--value={bad}")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S6_VALUE_MATCHES_RULE"], res.stderr


def test_score_refuses_a_value_that_is_not_a_number(registry):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "x", "--value", "high")
    assert res.returncode == EXIT_USAGE, res.stdout + res.stderr


def test_the_value_is_written_with_the_score_and_frozen_with_it(registry):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "held", "--value", "0.31")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    pred = json.loads(registry.read_text())["0005"]["predictions"][0]
    assert pred["value"] == 0.31
    assert ledger_lines(registry)[-1]["score"]["value"] == 0.31
    edit(registry, lambda r: r["0005"]["predictions"][0].update(value=0.04))
    res = run("check", registry)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "SCORE REWRITTEN: 0005-p1 field value 0.31 -> 0.04" in res.stderr, res.stderr


# --------------------------------------------------------------- auto-INVALID

def test_a_met_value_kill_makes_the_entry_invalid_in_the_same_call(registry):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "0", "--note", "no gain",
              "--value", "0.03")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    entry = json.loads(registry.read_text())["0005"]
    assert entry["predictions"][0]["outcome"] == 0
    assert entry["status"] == (f"INVALID — killed by 0005-p1: value 0.03 < 0.05 "
                               f"(regate, {datetime.date.today().isoformat()})"), entry["status"]
    last = ledger_lines(registry)[-1]
    assert last["kind"] == "auto_invalid" and last["id"] == "0005-p1"
    assert last["old_status"] == "open" and last["new_status"] == entry["status"]
    assert last["rule"] == VALUE_RULE and last["actual"] == 0.03
    assert ledger_lines(registry)[-2]["kind"] == "score"
    assert "AUTO-INVALID" in res.stdout, res.stdout
    assert run("check", registry).returncode == EXIT_OK


def test_an_unmet_value_kill_leaves_the_status_alone(registry):
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "held",
              "--value", "0.05")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())["0005"]["status"] == "open"
    assert ledger_lines(registry)[-1]["kind"] == "score"


def test_the_value_rule_decides_even_when_the_outcome_says_held(registry):
    """The rule is on the value. An outcome of 1 does not talk a met kill out of it."""
    preload(registry, make_entry("0005"))
    res = run("score", registry, "0005-p1", "--outcome", "1", "--note", "x", "--value", "0.01")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())["0005"]["status"].startswith("INVALID")


def test_a_met_outcome_kill_makes_the_entry_invalid(registry):
    preload(registry, make_entry("0005", kill_rule=OUTCOME_RULE))
    res = run("score", registry, "0005-p1", "--outcome", "0", "--note", "did not hold")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())["0005"]["status"] == (
        f"INVALID — killed by 0005-p1: outcome 0 == 0 (regate, "
        f"{datetime.date.today().isoformat()})")


def test_an_outcome_rule_that_is_not_met_leaves_the_status_alone(registry):
    preload(registry, make_entry("0005", kill_rule=OUTCOME_RULE))
    assert run("score", registry, "0005-p1", "--outcome", "1",
               "--note", "held").returncode == EXIT_OK
    assert json.loads(registry.read_text())["0005"]["status"] == "open"


def test_underpowered_never_auto_invalidates(registry):
    preload(registry, make_entry("0005", kill_rule=OUTCOME_RULE))
    res = run("score", registry, "0005-p1", "--outcome", "none",
              "--note", "UNDERPOWERED: 3 of 30")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())["0005"]["status"] == "open"


def test_a_prose_kill_is_not_evaluated(registry):
    preload(registry, make_entry("0005", kill_rule="prose"))
    res = run("score", registry, "0005-p1", "--outcome", "0", "--note", "did not hold")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "PROSE KILL — not evaluated by regate; decide by hand via status" in res.stdout
    assert json.loads(registry.read_text())["0005"]["status"] == "open"
    assert ledger_lines(registry)[-1]["kind"] == "score"


def test_a_legacy_prediction_is_scored_as_before(scorable):
    """No kill_rule = legacy prose. The nightly's `score --outcome` keeps working."""
    res = run("score", scorable, "0004-p1", "--outcome", "0", "--note", "did not hold")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "PROSE KILL — not evaluated" in res.stdout
    reg = json.loads(scorable.read_text())
    assert not reg["0004"]["status"].startswith("INVALID")
    assert "value" not in reg["0004"]["predictions"][0]


def test_a_second_met_kill_on_an_invalid_entry_scores_but_keeps_the_first_verdict(registry):
    payload = make_entry("0005", kill_rule=OUTCOME_RULE)
    second = dict(payload["0005"]["predictions"][0], id="0005-p2")
    payload["0005"]["predictions"].append(second)
    preload(registry, payload)
    assert run("score", registry, "0005-p1", "--outcome", "0", "--note", "x").returncode == 0
    first = json.loads(registry.read_text())["0005"]["status"]
    res = run("score", registry, "0005-p2", "--outcome", "0", "--note", "x")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())["0005"]["status"] == first
    assert ledger_lines(registry)[-1]["kind"] == "score"
    assert run("check", registry).returncode == EXIT_OK


# ---------------------------------------------------------- INVALID is terminal

@pytest.fixture
def killed(registry):
    preload(registry, make_entry("0005"))
    assert run("score", registry, "0005-p1", "--outcome", "0", "--note", "no gain",
               "--value", "0.03").returncode == EXIT_OK
    return registry


def test_status_refuses_to_reopen_an_invalid_entry(killed):
    before_reg, before_led = sha(killed), sha(killed.parent / LEDGER)
    res = run("status", killed, "0005", "reopened")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert re.search(r"^FAILED\s+R_INVALID_IS_FINAL", res.stderr, re.M), res.stderr
    assert (sha(killed), sha(killed.parent / LEDGER)) == (before_reg, before_led)


def test_status_set_to_invalid_by_hand_is_terminal_too(based):
    """A prose kill is decided by hand through `status`, and then it is final."""
    assert run("status", based, "0001", "INVALID — prose kill met, decided by the user").returncode \
        == EXIT_OK
    res = run("status", based, "0001", "open again")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "R_INVALID_IS_FINAL" in res.stderr


def test_a_hand_edit_away_from_invalid_is_a_moved_goalpost(killed):
    edit(killed, lambda r: r["0005"].update(status="open"))
    res = run("check", killed)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0005 status left INVALID" in res.stderr, res.stderr
    assert "STATUS CHANGED OFF-LEDGER: 0005" not in res.stderr


# ------------------------------------------------------ scope: never propagates

def test_auto_invalid_does_not_propagate_to_another_scope(registry):
    preload(registry, make_entry("0005", scope="proj/a"),
            make_entry("0006", scope="proj/b"))
    before = json.loads(registry.read_text())
    assert run("score", registry, "0005-p1", "--outcome", "0", "--note", "x",
               "--value", "0.01").returncode == EXIT_OK
    after = json.loads(registry.read_text())
    assert after["0005"]["status"].startswith("INVALID")
    for eid in before:
        if eid != "0005":
            assert after[eid]["status"] == before[eid]["status"], eid


# ------------------------------------------------- R5: not a resurrection

def paraphrase_entry(eid, scope="proj/a", **extra):
    return make_entry(eid, claim=PARAPHRASE["claim"], statement=PARAPHRASE["statements"][0],
                      scope=scope, **extra)


def test_a_paraphrase_of_an_invalid_claim_in_its_scope_is_refused(killed, tmp_path):
    before_reg, before_led = sha(killed), sha(killed.parent / LEDGER)
    res = run("register", killed, write_entry(tmp_path, paraphrase_entry("0006")))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr
    assert "0005" in res.stderr and "supersedes" in res.stderr, res.stderr
    assert (sha(killed), sha(killed.parent / LEDGER)) == (before_reg, before_led)


def test_a_paraphrase_in_another_scope_is_allowed_and_witnessed(killed, tmp_path):
    res = run("register", killed, write_entry(tmp_path, paraphrase_entry("0006", scope="proj/c")))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "RESEMBLES INVALID 0005 (scope proj/a): allowed, different scope" in res.stdout, \
        res.stdout
    last = ledger_lines(killed)[-1]
    assert [r["id"] for r in last["resembles_invalid"]] == ["0005"]
    assert last["resembles_invalid"][0]["scope"] == "proj/a"
    assert 0 < last["resembles_invalid"][0]["similarity"] <= 1


def test_a_different_claim_in_the_same_scope_is_allowed(killed, tmp_path):
    payload = make_entry("0006", claim=DIFFERENT["claim"], statement=DIFFERENT["statements"][0])
    res = run("register", killed, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert ledger_lines(killed)[-1]["resembles_invalid"] == []


def test_a_legacy_invalid_entry_is_scope_legacy(based, tmp_path):
    """0000-0004 have no scope; they read as `legacy`, so a resemblance is witnessed only."""
    assert run("status", based, "0004", "INVALID — decided by hand").returncode == EXIT_OK
    reg = json.loads(based.read_text())
    payload = make_entry("0005", claim=reg["0004"]["claim"],
                         statement=reg["0004"]["predictions"][0]["statement"], scope="proj/a")
    res = run("register", based, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "RESEMBLES INVALID 0004 (scope legacy)" in res.stdout, res.stdout


def test_superseding_an_invalid_entry_needs_a_structural_change(killed, tmp_path):
    res = run("register", killed, write_entry(tmp_path, paraphrase_entry("0006",
                                                                          supersedes="0005")))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr
    assert "structural_change" in res.stderr


@pytest.mark.parametrize("kind", ["parameter", "seed", "prompt", "wording", "learning rate", ""])
def test_a_non_structural_change_is_refused(killed, tmp_path, kind):
    payload = paraphrase_entry("0006", supersedes="0005",
                               structural_change={"type": kind, "what": REAL_WHAT})
    res = run("register", killed, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr


def test_a_structural_change_needs_a_real_what(killed, tmp_path):
    payload = paraphrase_entry("0006", supersedes="0005",
                               structural_change={"type": "instrument", "what": "new timer"})
    res = run("register", killed, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr
    assert "20" in res.stderr


@pytest.mark.parametrize("kind", ["instrument", "mechanism"])
def test_a_declared_structural_change_is_allowed(killed, tmp_path, kind):
    payload = paraphrase_entry("0006", supersedes="0005",
                               structural_change={"type": kind, "what": REAL_WHAT})
    res = run("register", killed, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(killed.read_text())["0006"]["supersedes"] == "0005"


def test_supersedes_must_name_an_entry_that_exists(based, tmp_path):
    res = run("register", based, write_entry(tmp_path, make_entry("0005", supersedes="0042")))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr


def test_order41_superseding_an_entry_that_is_not_invalid_is_refused(based, tmp_path):
    """Only an INVALID claim can be superseded: an open one is still alive."""
    before_reg, before_led = sha(based), sha(based.parent / LEDGER)
    payload = make_entry("0005", supersedes="0004",
                         structural_change={"type": "instrument", "what": REAL_WHAT})
    res = run("register", based, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr
    assert "supersedes 0004, which is not INVALID" in res.stderr, res.stderr
    assert (sha(based), sha(based.parent / LEDGER)) == (before_reg, before_led)


def test_order41_a_paraphrase_cannot_dodge_r5_by_superseding_an_open_entry(killed, tmp_path):
    """The loophole: a dead claim reworded, pointed at a live entry, skipped the resemblance
    check. 0006 is a paraphrase of INVALID 0005 in its scope, and supersedes open 0004."""
    before_reg, before_led = sha(killed), sha(killed.parent / LEDGER)
    payload = paraphrase_entry("0006", supersedes="0004",
                               structural_change={"type": "instrument", "what": REAL_WHAT})
    res = run("register", killed, write_entry(tmp_path, payload))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["R5_NOT_A_RESURRECTION"], res.stderr
    assert (sha(killed), sha(killed.parent / LEDGER)) == (before_reg, before_led)


# ------------------------------------------- the threshold is measured, not guessed

def test_the_threshold_sits_between_the_measured_groups():
    rg = load_regate()
    orig = SIMILARITY["original"]
    same = [rg.resemblance(e["claim"], e["statements"], orig["claim"], orig["statements"])
            for e in SIMILARITY["paraphrases"]]
    different = [rg.resemblance(e["claim"], e["statements"], orig["claim"], orig["statements"])
                 for e in SIMILARITY["different"]]
    live = json.loads((FIXTURES / "real_copy" / "registry.json").read_text())
    ids = sorted(live)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            different.append(rg.resemblance(
                live[a]["claim"], [p["statement"] for p in live[a]["predictions"]],
                live[b]["claim"], [p["statement"] for p in live[b]["predictions"]]))
    assert len(same) >= 3 and len(different) >= 3 + 10
    assert max(different) < rg.R5_THRESHOLD <= min(same), (same, different)


# ------------------------------------------------- check: legacy prose kills

def test_check_counts_legacy_prose_kills_at_the_end_of_its_summary(based, tmp_path):
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    summary = [l for l in res.stdout.splitlines() if "prediction(s)" in l][0]
    assert summary.endswith(", 5 legacy prose kills"), summary
    assert re.search(r"(\d+) prediction\(s\), (\d+) narrative\(s\)", summary), \
        "nightly.registry_line()'s regex must still match"
    assert run("register", based, write_entry(tmp_path, make_entry("0005"))).returncode == 0
    res = run("check", based)
    assert "6 prediction(s)" in res.stdout and res.stdout.rstrip().endswith(
        ", 5 legacy prose kills"), res.stdout


# ------------------------------------- the amendment end to end (AC 2a -> 2c)

@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_the_amendment_end_to_end_through_register(based, tmp_path):
    """Register an experiment with a value rule and a real seal, score it early with
    a value that meets the kill: INVALID in the same call, then final."""
    payload = make_entry("0005", kind="experiment")
    payload["0005"]["predictions"][0]["prereg"] = str(sealed_prereg(tmp_path))
    assert run("register", based, write_entry(tmp_path, payload)).returncode == EXIT_OK
    swapped(based, "0005-p1")                                    # order 49: S7
    ran_after_seal(payload["0005"]["predictions"][0]["prereg"], scope="proj/a")   # order 60: S9
    res = run("score", based, "0005-p1", "--outcome", "0", "--note", "no gain",
              "--value", "0.03")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(based.read_text())["0005"]["status"].startswith(
        "INVALID — killed by 0005-p1: value 0.03 < 0.05")
    assert ledger_lines(based)[-1]["kind"] == "auto_invalid"
    assert run("status", based, "0005", "reopened").returncode == EXIT_REFUSED


# ------------------------------------------------------------------ order 27: UTC

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_order27_new_ledger_lines_are_utc_z_and_old_naive_lines_still_check(based):
    """The baseline fixture's lines are written by this regate (Z). Rewrite one chain whose
    ts are naive local (as every line before order 27 is), then append: both must check."""
    ledger = based.parent / LEDGER
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert lines and all(UTC_Z.match(l["ts"]) for l in lines), [l["ts"] for l in lines]
    # rebuild the same chain with old-style naive timestamps
    prev, out = hashlib.sha256(b"").hexdigest(), []
    for rec in lines:
        rec = dict(rec, prev=prev, ts="2026-09-23T23:03:25")
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out.append(raw)
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    ledger.write_text("".join(l + "\n" for l in out))
    assert run("check", based).returncode == EXIT_OK
    assert run("baseline", based).returncode == EXIT_OK          # appends new lines
    ts = [json.loads(l)["ts"] for l in ledger.read_text().splitlines()]
    assert ts[:len(out)] == ["2026-09-23T23:03:25"] * len(out)
    assert all(UTC_Z.match(t) for t in ts[len(out):]), ts
    assert run("check", based).returncode == EXIT_OK


# ------------------------------------------------------------------ order 27 part E: the cycle line

def _zt(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cycle_dir(tmp_path, monkeypatch):
    """Scratch GPU/TURNS ledgers; cycle.py reads HELIX_CYCLE_DIR, which the subprocess inherits."""
    d = tmp_path / "cycle"
    d.mkdir()
    monkeypatch.setenv("HELIX_CYCLE_DIR", str(d))
    return d


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order27_scoring_a_sealed_experiment_prints_and_stores_seal_to_score(kinded, tmp_path,
                                                                              cycle_dir):
    import time
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    swapped(kinded, "0004-p1")                                   # order 49: S7
    ran_after_seal(prereg)                                       # order 60: S9
    now = time.time()
    (cycle_dir / "GPU.jsonl").write_text(
        json.dumps({"ts": _zt(now - 3600), "idle": True}) + "\n"
        + json.dumps({"ts": _zt(now), "pid": 7, "cmd": "python3 run.py", "mem_mib": 9}) + "\n")
    (cycle_dir / "TURNS.jsonl").write_text(
        json.dumps({"ts": _zt(now - 3600), "lane": "chat", "cost_usd": 99.0}) + "\n"
        + json.dumps({"ts": _zt(now), "lane": "chat", "cost_usd": 1.5, "output_tokens": 5}) + "\n"
        + json.dumps({"ts": _zt(now), "lane": "work", "cost_usd": 1.6}) + "\n")
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    m = re.search(r"^cycle: seal→score \d+s · GPU 1 min · 2 turns · \$3\.10 \(token spend in "
                  r"window\)$", res.stdout, re.M)
    assert m, res.stdout
    pred = json.loads(kinded.read_text())["0004"]["predictions"][0]
    c = pred["cycle"]
    assert c["from"] == "seal" and c["gpu_min"] == 1 and c["turns"] == 2 and c["cost_usd"] == 3.1
    assert c["upper_bound"] is True and c["start"].endswith("Z") and c["end"].endswith("Z")
    issued = json.loads((prereg.parent / "RUN_TOKEN.json").read_text())["issued_at"]
    assert c["start"] == issued
    last = json.loads((kinded.parent / LEDGER).read_text().splitlines()[-1])
    assert last["score"]["cycle"] == c                       # frozen with the score
    chk = run("check", kinded)
    assert chk.returncode == EXIT_OK, chk.stderr
    assert f"  0004-p1 cycle: {c['line']}" in chk.stdout, chk.stdout


def test_order27_a_time_prediction_is_measured_from_its_ledger_line(scorable, cycle_dir):
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.search(r"^cycle: baseline→score \S+ · GPU not sampled · turns not logged$",
                     res.stdout, re.M), res.stdout
    c = json.loads(scorable.read_text())["0004"]["predictions"][0]["cycle"]
    assert c["from"] == "baseline" and "gpu_min" not in c and "turns" not in c


def test_order27_check_marks_legacy_scores_not_recorded(based):
    before = json.loads(based.read_text())
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stderr
    assert "  0002-p1 cycle: not recorded (pre-gate)" in res.stdout, res.stdout
    assert "  0003-p1 cycle: not recorded (pre-gate)" in res.stdout, res.stdout
    assert "0000-p1 cycle" not in res.stdout                     # not scored, no line
    assert json.loads(based.read_text()) == before               # legacy score fields untouched
    summary = [l for l in res.stdout.splitlines() if "prediction(s)" in l]
    assert len(summary) == 1 and re.search(r"(\d+) prediction\(s\), (\d+) narrative\(s\)",
                                           summary[0])


def test_order27_a_rewritten_cycle_is_caught(scorable, cycle_dir):
    assert run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held").returncode == 0
    stored = json.loads(scorable.read_text())["0004"]["predictions"][0]["cycle"]["wall_s"]
    # order 29: never a literal -- a score in the same second stores wall_s 1 already
    edit(scorable, lambda r: r["0004"]["predictions"][0]["cycle"].update(wall_s=stored + 1000))
    assert json.loads(scorable.read_text())["0004"]["predictions"][0]["cycle"]["wall_s"] != stored
    res = run("check", scorable)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "SCORE REWRITTEN: 0004-p1 field cycle" in res.stderr, res.stderr


def _regate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("regate_under_test", REGATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REFUSALS_LOG, mod.SEALGATE = REFUSALS_LOG, SEALGATE_RUN    # order 41
    mod.PACKS, mod.REPLAYS, mod.SWAPS = PACKS_LEDGER, REPLAYS_LEDGER, SWAPS_LEDGER   # order 51
    return mod


def test_order27_cycle_start_prefers_register_over_baseline_and_seal_over_both(tmp_path):
    rg = _regate_module()
    reg = tmp_path / "registry.json"
    reg.write_text("{}")
    (tmp_path / LEDGER).write_text("".join(json.dumps(r) + "\n" for r in [
        {"kind": "baseline", "id": "x-p1", "ts": "2026-09-23T23:03:25"},
        {"kind": "register", "id": "y-p1", "ts": "2026-10-01T10:00:00Z"},
        {"kind": "score", "id": "y-p1", "ts": "2026-10-01T11:00:00Z"}]))
    assert rg.cycle_start({"id": "y-p1", "kind": "time"}, reg) == ("register", "2026-10-01T10:00:00Z")
    assert rg.cycle_start({"id": "x-p1"}, reg) == ("baseline", "2026-09-23T23:03:25")
    seal = tmp_path / "s"
    seal.mkdir()
    (seal / "RUN_TOKEN.json").write_text(json.dumps({"issued_at": "2026-10-01T09:00:00Z"}))
    assert rg.cycle_start({"id": "y-p1", "prereg": str(seal / "PREREG.md")}, reg) == \
        ("seal", "2026-10-01T09:00:00Z")
    # a prereg whose token cannot be read falls back to the ledger line, and says so
    assert rg.cycle_start({"id": "y-p1", "prereg": str(tmp_path / "nope" / "P.md")}, reg) == \
        ("register", "2026-10-01T10:00:00Z")


def test_order27_a_broken_cycle_tool_never_blocks_the_score(tmp_path, monkeypatch):
    rg = _regate_module()
    monkeypatch.setattr(rg, "CYCLE", tmp_path / "no_cycle.py")
    c = rg.measure_cycle("register", "2026-10-01T10:00:00Z")
    assert c["from"] == "register" and c["line"].startswith("not measured (")
    assert "gpu_min" not in c and "cost_usd" not in c


# ------------------------------------------------------------------ order 28 (item 24): K next to every score

def _chain(recs):
    """ATTEMPTS.jsonl as app.py's POST /attempt writes it."""
    out, prev = [], "genesis"
    for i, r in enumerate(recs, 1):
        raw = json.dumps(dict(r, seq=i, prev=prev), ensure_ascii=False)
        out.append(raw)
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return "".join(l + "\n" for l in out)


def _att(t, n=1, pid=None, scope="proj/a", cwd="/work/exp"):
    r = {"ts": _zt(t), "scope": scope, "kind": "run", "n": n, "cmd": "python3 train.py", "cwd": cwd}
    if pid:
        r["pid"] = pid
    return r


@pytest.fixture
def scoped(registry):
    """0005 in scope proj/a, past due, baselined: scorable today."""
    return preload(registry, make_entry("0005", kill_rule=OUTCOME_RULE))


def _three_attempts(cycle_dir, gpu_extra=()):
    import time
    now = time.time()
    (cycle_dir / "ATTEMPTS.jsonl").write_text(_chain([
        _att(now - 300, pid=101), _att(now - 200, n=4, pid=102), _att(now - 100, pid=103),
        _att(now - 50, n=7, scope="proj/b")]))
    (cycle_dir / "GPU.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": _zt(now - 600), "idle": True},
        {"ts": _zt(now - 240), "pid": 101, "cmd": "python3 train.py", "cwd": "/work/exp", "mem_mib": 9},
        *gpu_extra]))
    return now


def test_order28_score_prints_and_stores_k_counted_from_the_attempt_log(scoped, cycle_dir):
    now = _three_attempts(cycle_dir)
    res = run("score", scoped, "0005-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    out = res.stdout.splitlines()
    i = [n for n, l in enumerate(out) if l.startswith("cycle: ")][0]
    since = datetime.datetime.fromtimestamp(now - 300, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    assert out[i + 1] == f"K=6 (since {since}) · K witnessed (GPU)", res.stdout
    k = json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]
    assert {key: k[key] for key in ("k", "since", "witness", "unlogged")} == \
        {"k": 6, "since": _zt(now - 300), "witness": "ok", "unlogged": 0}
    assert k["line"] == out[i + 1] and k["until"].endswith("Z")
    assert ledger_lines(scoped)[-1]["score"]["k"] == k                  # frozen with the score
    chk = run("check", scoped)
    assert chk.returncode == EXIT_OK, chk.stderr
    assert f"  0005-p1 K: {k['line']}" in chk.stdout, chk.stdout
    assert chk.stdout.splitlines()[-1].endswith("legacy prose kills")   # the summary stays last


def test_order28_a_scope_with_no_attempts_prints_k_0_and_still_scores(scoped, cycle_dir):
    res = run("score", scoped, "0005-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "\nK=0 · no attempts logged\n" in res.stdout, res.stdout
    k = json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]
    assert k["k"] == 0 and k["since"] is None


def test_order28_an_unlogged_gpu_job_disputes_k_and_is_named(scoped, cycle_dir):
    import time
    extra = [{"ts": _zt(time.time() - 120), "pid": 999, "cmd": "python3 train.py --seed 9",
              "cwd": "/work/exp", "mem_mib": 9}]
    _three_attempts(cycle_dir, extra)
    res = run("score", scoped, "0005-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.search(r"^K=6 \(since \S+\) · K disputed: \+1 unlogged GPU job\(s\)$", res.stdout,
                     re.M), res.stdout
    assert re.search(r"^  unlogged GPU job: pid 999 .*python3 train\.py --seed 9", res.stdout, re.M)
    k = json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]
    assert k["witness"] == "disputed" and k["unlogged"] == 1 and k["k"] == 6


def test_order28_a_broken_attempt_chain_scores_but_prints_no_k(scoped, cycle_dir):
    _three_attempts(cycle_dir)
    led = cycle_dir / "ATTEMPTS.jsonl"
    lines = led.read_text().splitlines()
    damaged = lines[1].replace('"n": 4', '"n": 1')
    assert damaged != lines[1]                                       # order 29: a real edit
    lines[1] = damaged
    led.write_text("\n".join(lines) + "\n")
    res = run("score", scoped, "0005-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "\nK unverifiable (chain broken at seq 3)\n" in res.stdout, res.stdout
    k = json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]
    assert k["k"] is None and k["witness"] == "unverifiable"


def test_order28_check_marks_legacy_scores_k_not_recorded(based):
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stderr
    assert "  0002-p1 K: not recorded (pre-gate)" in res.stdout, res.stdout
    assert "  0003-p1 K: not recorded (pre-gate)" in res.stdout, res.stdout
    assert "0000-p1 K" not in res.stdout
    summary = [l for l in res.stdout.splitlines() if "prediction(s)" in l]
    assert len(summary) == 1 and res.stdout.splitlines()[-1] == summary[0]


def test_order28_a_hand_edited_k_is_a_rewritten_score(scoped, cycle_dir):
    _three_attempts(cycle_dir)
    assert run("score", scoped, "0005-p1", "--outcome", "1", "--note", "held").returncode == 0
    stored = json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]["k"]
    edit(scoped, lambda r: r["0005"]["predictions"][0]["k"].update(k=stored + 1000))   # order 29
    assert json.loads(scoped.read_text())["0005"]["predictions"][0]["k"]["k"] != stored
    res = run("check", scoped)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "SCORE REWRITTEN: 0005-p1 field k" in res.stderr, res.stderr


def test_order28_a_legacy_entry_is_counted_in_scope_legacy(scorable, cycle_dir):
    import time
    (cycle_dir / "ATTEMPTS.jsonl").write_text(_chain([_att(time.time() - 60, n=2, scope="legacy")]))
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.search(r"^K=2 \(since ", res.stdout, re.M), res.stdout


def test_order28_a_broken_k_tool_never_blocks_the_score(tmp_path, monkeypatch):
    rg = _regate_module()
    monkeypatch.setattr(rg, "CYCLE", tmp_path / "no_cycle.py")
    k = rg.measure_k("proj/a", "2026-10-02T10:00:00Z")
    assert k["k"] is None and k["line"].startswith("K not measured (")


def test_order28_k_is_a_score_field_and_old_scores_hash_as_before():
    rg = _regate_module()
    assert rg.SCORE_FIELDS[-1] == "k" and "cycle" in rg.SCORE_FIELDS
    old = {"scored": "2026-09-01", "outcome": 1, "brier": 0.25, "note": "x"}
    assert rg.score_of(dict(old)) == old                       # absent k stays absent


# ==========================================================================
# ORDER 49 — item 10, seat-swap replication. An experiment is scored only with an attached replay
# pack (attach-pack, set once) and an intact seat-swap AGREE for that pack's CURRENT manifest
# (S7). A time prediction has no analysis to swap. The packs and runs are real (replay.py,
# seatswap.py with a fake Alice) on the session's temp ledgers; regate asks seatswap as a
# subprocess through the shim.
# ==========================================================================

@pytest.fixture
def due_experiment(registry):
    """0004-p1 as an experiment past its due date (S5 ok without a seal), baselined."""
    edit(registry, lambda r: r["0004"]["predictions"][0].update(kind="experiment",
                                                                due="2026-01-31"))
    assert run("baseline", registry).returncode == EXIT_OK
    return registry


def pack_name(tmp_path, tag):
    return f"{tag}_{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]}"


def score_0004(registry):
    return run("score", registry, "0004-p1", "--outcome", "1", "--note", "ran it, it held")


def test_order49_the_pinned_score_preconditions_are_all_in_regate():
    mod = load_module("regate_pin_check", REGATE)
    assert set(ALL_SCORE_PRECONDITIONS) <= set(mod.SCORE_PRECONDITIONS)
    assert mod.SCORE_PRECONDITIONS[-3:-1] == ["S7_SEAT_SWAP", "S8_NOT_QUARANTINED"]   # order 51
    assert mod.SCORE_PRECONDITIONS[-1] == "S9_RUN_AFTER_SEAL"                           # order 60
    assert "pack" in mod.FROZEN_FIELDS


def test_order49_s7_an_experiment_with_no_pack_is_refused(due_experiment):
    before = sha(due_experiment)
    res = score_0004(due_experiment)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "no replay pack attached" in res.stderr
    assert sha(due_experiment) == before


def test_order49_s7_a_disagree_is_refused(due_experiment, tmp_path):
    name = pack_name(tmp_path, "disagree")
    assert make_pack(tmp_path, name, SWAP_WRONG)["outcome"] == "DISAGREE"
    swapped(due_experiment, "0004-p1", name)                     # attach checks the pack only
    res = score_0004(due_experiment)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "outcome DISAGREE (total: alice 3580.75, packed 3579.75, tol 0)" in res.stderr


def test_order49_s7_a_disagree_beside_an_agree_is_refused(due_experiment, tmp_path):
    name = pack_name(tmp_path, "both")
    assert make_pack(tmp_path, name, SWAP_RIGHT)["outcome"] == "AGREE"
    seatswap.ALICE = lambda w, p: (Path(w) / "swap.py").write_text(SWAP_WRONG) and {}
    assert seatswap.run(name)["outcome"] == "DISAGREE"
    swapped(due_experiment, "0004-p1", name)
    res = score_0004(due_experiment)
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "DISAGREE" in res.stderr


@pytest.mark.parametrize("outcome", ["COPIED", "NOT_REPRODUCED", "FAILED"])
def test_order49_s7_an_outcome_that_is_not_agree_is_refused_and_named(due_experiment, tmp_path,
                                                                       outcome):
    name = pack_name(tmp_path, outcome.lower())
    make_pack(tmp_path, name)
    seatswap.append([{"kind": "swap", "pack": name, "outcome": outcome, "why": "fixture",
                      "manifest_sha256": sha(PACK_BULK / name / "manifest.json")}])
    swapped(due_experiment, "0004-p1", name)
    res = score_0004(due_experiment)
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert f"outcome {outcome} (fixture)" in res.stderr, res.stderr


def test_order49_s7_a_pack_with_no_run_is_refused(due_experiment, tmp_path):
    name = pack_name(tmp_path, "norun")
    make_pack(tmp_path, name)
    swapped(due_experiment, "0004-p1", name)
    res = score_0004(due_experiment)
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "outcome none" in res.stderr and "no seat-swap run" in res.stderr


def test_order49_s7_an_agree_on_an_old_manifest_is_refused(due_experiment, tmp_path):
    name = pack_name(tmp_path, "old")
    old = make_pack(tmp_path, name, SWAP_RIGHT)
    assert old["outcome"] == "AGREE"
    swapped(due_experiment, "0004-p1", name)
    m = PACK_BULK / name / "manifest.json"                       # the pack, as re-made
    man = json.loads(m.read_text())
    man["note"] = "fixture, re-made"
    m.write_text(json.dumps(man, indent=1) + "\n")
    rec = json.loads(PACKS_LEDGER.read_text().splitlines()[-1])
    assert rec["name"] == name
    with open(PACKS_LEDGER, "a") as fh:
        fh.write(json.dumps(dict(rec, manifest_sha256=sha(m))) + "\n")
    res = score_0004(due_experiment)
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "no run for the current manifest" in res.stderr
    assert old["manifest_sha256"][:12] in res.stderr


def test_order49_s7_a_pack_tampered_after_its_agree_is_refused(due_experiment, tmp_path):
    name = pack_name(tmp_path, "tamper")
    assert make_pack(tmp_path, name, SWAP_RIGHT)["outcome"] == "AGREE"
    swapped(due_experiment, "0004-p1", name)
    (PACK_BULK / name / "data.csv").write_text(PACK_DATA + "1.0\n")
    res = score_0004(due_experiment)
    assert failed(res.stderr) == ["S7_SEAT_SWAP"], res.stderr
    assert "pack tampered" in res.stderr


def test_order49_s7_an_agree_on_the_current_manifest_scores(due_experiment):
    swapped(due_experiment, "0004-p1")
    res = score_0004(due_experiment)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.search(r"^ok\s+S7_SEAT_SWAP: seat swap AGREE for pack regate_fixture_pack "
                     r"\(manifest [0-9a-f]{12}\)$", res.stdout, re.M), res.stdout
    assert json.loads(due_experiment.read_text())["0004"]["predictions"][0]["outcome"] == 1
    assert run("check", due_experiment).returncode == EXIT_OK


def test_order49_s7_a_time_prediction_is_unaffected(kinded):
    edit(kinded, lambda r: r["0000"]["predictions"][0].update(due="2026-01-31"))
    assert run("baseline", kinded).returncode == EXIT_OK
    res = run("score", kinded, "0000-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "ok       S7_SEAT_SWAP: time prediction, no analysis to swap" in res.stdout


def test_order49_s7_a_prediction_with_no_kind_is_read_as_time(scorable):
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "S7_SEAT_SWAP: time prediction (no kind declared, treated as time), no analysis " \
           "to swap" in res.stdout


def test_order49_s7_is_not_evaluated_for_an_unknown_id(based):
    res = run("score", based, "0099-p1", "--outcome", "1", "--note", "x")
    assert re.search(r"^not-evaluated\s+S7_SEAT_SWAP", res.stderr, re.M), res.stderr


# ------------------------------------------------------------------ attach-pack

def test_order49_attach_pack_records_the_pack_in_the_registry_and_ledger(due_experiment):
    res = run("attach-pack", due_experiment, "0004-p1", FIXTURE_PACK)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "PACK ATTACHED: 0004-p1 -> regate_fixture_pack" in res.stdout
    assert json.loads(due_experiment.read_text())["0004"]["predictions"][0]["pack"] == \
        FIXTURE_PACK
    last = ledger_lines(due_experiment)[-1]
    assert last["kind"] == "attach-pack" and last["frozen"]["pack"] == FIXTURE_PACK
    assert last["manifest_sha256"] == sha(PACK_BULK / FIXTURE_PACK / "manifest.json")
    assert run("check", due_experiment).returncode == EXIT_OK


def test_order49_attach_pack_is_set_once(due_experiment, tmp_path):
    other = pack_name(tmp_path, "other")
    make_pack(tmp_path, other)
    swapped(due_experiment, "0004-p1")
    before = (sha(due_experiment), sha(due_experiment.parent / LEDGER))
    for name in (other, FIXTURE_PACK):
        res = run("attach-pack", due_experiment, "0004-p1", name)
        assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
        assert failed(res.stderr) == ["A4_PACK_SET_ONCE"], res.stderr
    assert (sha(due_experiment), sha(due_experiment.parent / LEDGER)) == before


def test_order49_attach_pack_refuses_a_pack_not_in_packs(due_experiment):
    res = run("attach-pack", due_experiment, "0004-p1", "no_such_pack")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["A5_PACK_IN_PACKS_AND_VERIFIES"], res.stderr
    assert "not in PACKS.jsonl" in res.stderr


def test_order49_attach_pack_refuses_a_pack_that_does_not_verify(due_experiment, tmp_path):
    name = pack_name(tmp_path, "bad")
    make_pack(tmp_path, name)
    (PACK_BULK / name / "added.txt").write_text("x")
    res = run("attach-pack", due_experiment, "0004-p1", name)
    assert failed(res.stderr) == ["A5_PACK_IN_PACKS_AND_VERIFIES"], res.stderr
    assert "added added.txt" in res.stderr


def test_order49_attach_pack_refuses_an_unknown_id_and_a_drifted_registry(due_experiment):
    res = run("attach-pack", due_experiment, "0099-p1", FIXTURE_PACK)
    assert failed(res.stderr) == ["A1_PREDICTION_EXISTS_AND_UNDRIFTED"], res.stderr
    edit(due_experiment, lambda r: r["0004"]["predictions"][0].update(p=0.9))
    res = run("attach-pack", due_experiment, "0004-p1", FIXTURE_PACK)
    assert failed(res.stderr) == ["A1_PREDICTION_EXISTS_AND_UNDRIFTED"], res.stderr


def test_order49_a_pack_swapped_or_added_by_hand_is_a_moved_goalpost(due_experiment, tmp_path):
    edit(due_experiment, lambda r: r["0004"]["predictions"][0].update(pack=FIXTURE_PACK))
    res = run("check", due_experiment)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004-p1 field pack <absent> -> \"regate_fixture_pack\"" in res.stderr


def test_order49_regate_never_imports_seatswap():
    src = REGATE.read_text()
    assert not re.search(r"^\s*(import|from)\s+seatswap", src, re.M)
    assert "spec_from_file_location" not in src
    assert '[sys.executable, str(SEATSWAP), "check", str(pack)]' in src


# ==========================================================================
# ORDER 51 — item 23: THE EPISTEMIC PROVENANCE DAG, registry entries only.
#
# Every entry declares, once, what it rests on (`attach-deps`): other entries, or `pack:<name>`
# for the code/data a claim stands on. Quarantine is COMPUTED on every read, never stored: an
# entry is quarantined while any upstream node, transitively, is INVALID (or quarantined), or is
# a pack whose latest replay is MISMATCH/TAMPERED or whose latest seat swap is DISAGREE. A
# quarantined entry cannot be scored (S8) or have a prereg or pack attached (A_NOT_QUARANTINED),
# and `check` lists its dropped seals. regate reads REPLAYS.jsonl and SWAPS.jsonl as files.
# ==========================================================================

def deps(registry, eid, *on, runner=None):
    """attach-deps through the real verb: no edges → --none."""
    flags = ["--none"] if not on else [a for e in on for a in ("--on", e)]
    return (runner or run)("attach-deps", registry, eid, *flags)


def declared(registry, eid, *on, runner=None):
    res = deps(registry, eid, *on, runner=runner)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    return res


def kill(registry, eid, runner=None):
    """Score <eid>-p1 so its value kill_rule (< 0.05) is met: order 25b's auto-INVALID."""
    res = (runner or run)("score", registry, f"{eid}-p1", "--outcome", "0", "--note", "no gain",
                          "--value", "0.03")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert json.loads(registry.read_text())[eid]["status"].startswith("INVALID")


def score_held(registry, eid, runner=None):
    return (runner or run)("score", registry, f"{eid}-p1", "--outcome", "1", "--note", "held",
                           "--value", "0.2")


def state(registry):
    return sha(registry), sha(registry.parent / LEDGER)


@pytest.fixture
def chain3(registry):
    """A=0005 (--none) → B=0006 (on 0005) → C=0007 (on 0006): all open, past due, baselined."""
    preload(registry, make_entry("0005"), make_entry("0006", scope="proj/b"),
            make_entry("0007", scope="proj/c"))
    declared(registry, "0005")
    declared(registry, "0006", "0005")
    declared(registry, "0007", "0006")
    return registry


@pytest.fixture
def own_ledgers(tmp_path):
    """A regate shim with its own REPLAYS.jsonl and SWAPS.jsonl, so a test can plant, break or
    re-chain them without touching the session's. → (runner, replays, swaps)."""
    replays, swaps = tmp_path / "own" / "REPLAYS.jsonl", tmp_path / "own" / "SWAPS.jsonl"
    shim = write_shim(tmp_path / "own" / "regate", REGATE, REFUSALS_LOG=REFUSALS_LOG,
                      SEALGATE=SEALGATE_RUN, SEATSWAP=SEATSWAP_RUN, PACKS=PACKS_LEDGER,
                      REPLAYS=replays, SWAPS=swaps)

    def runner(*args):
        return subprocess.run([sys.executable, str(shim), *map(str, args)],
                              capture_output=True, text=True)
    return runner, replays, swaps


def plant_replay(replays, name, result):
    with open(replays, "a") as fh:
        fh.write(json.dumps({"at": "2026-10-03T23:00:00", "name": name, "result": result}) + "\n")


def plant_swap(swaps, name, outcome):
    seatswap.append([{"kind": "swap", "pack": name, "outcome": outcome, "why": "fixture",
                      "manifest_sha256": "0" * 64}], ledger=swaps)


def quarantined(res):
    return re.findall(r"^\s*QUARANTINED: (.+)$", res.stdout, flags=re.M)


# ------------------------------------------------------------------ the pins

def test_order51_the_pinned_precondition_lists_are_all_in_regate():
    mod = load_module("regate_pin_check_51", REGATE)
    assert set(ALL_SCORE_PRECONDITIONS) <= set(mod.SCORE_PRECONDITIONS)
    assert mod.ATTACH_PRECONDITIONS == ALL_ATTACH_PRECONDITIONS
    assert mod.PACK_PRECONDITIONS == ALL_PACK_PRECONDITIONS
    assert mod.DEPS_PRECONDITIONS == ALL_DEPS_PRECONDITIONS
    assert mod.PRECONDITION_ORDER["attach-deps"] == ALL_DEPS_PRECONDITIONS
    # Part A: not frozen in the registry's own field lists (every legacy entry would drift);
    # the attach-deps ledger line is the frozen record.
    assert "depends_on" not in mod.ENTRY_FROZEN_FIELDS
    assert "depends_on" not in mod.FROZEN_FIELDS


def test_order51_regate_reads_the_pack_ledgers_as_files_and_imports_neither():
    src = REGATE.read_text()
    assert not re.search(r"^\s*(import|from)\s+(replay|seatswap)\b", src, re.M)
    assert "spec_from_file_location" not in src
    assert 'REPLAYS = Path.home() / "helix/workspace/replay/REPLAYS.jsonl"' in src
    assert 'SWAPS = Path.home() / "helix/workspace/seatswap/SWAPS.jsonl"' in src
    assert 'PACKS = Path.home() / "helix/workspace/replay/PACKS.jsonl"' in src


# ------------------------------------------------------------ attach-deps (Part A)

def test_order51_attach_deps_records_the_edges_in_the_registry_and_the_ledger(based):
    res = declared(based, "0004", "0003")
    assert "DEPENDENCIES DECLARED: 0004 -> 0003" in res.stdout
    for name in ALL_DEPS_PRECONDITIONS:
        assert re.search(rf"^ok\s+{name}: ", res.stdout, re.M), res.stdout
    assert json.loads(based.read_text())["0004"]["depends_on"] == ["0003"]
    last = ledger_lines(based)[-1]
    assert last["kind"] == "attach-deps" and last["entry"] == "0004" and last["id"] is None
    assert last["frozen"] == {"depends_on": ["0003"]}
    assert last["frozen_sha256"] == hashlib.sha256(
        json.dumps({"depends_on": ["0003"]}, separators=(",", ":")).encode()).hexdigest()
    assert run("check", based).returncode == EXIT_OK


def test_order51_none_is_an_explicit_empty_declaration(based):
    res = declared(based, "0001")
    assert "DEPENDENCIES DECLARED: 0001 -> none" in res.stdout
    assert json.loads(based.read_text())["0001"]["depends_on"] == []
    assert ledger_lines(based)[-1]["frozen"] == {"depends_on": []}
    rg = load_regate()
    p = rg.provenance(based)
    assert p["undeclared"] == ["0000", "0002", "0003", "0004"], "--none is a declaration"
    assert p["edges"] == 0


def test_order51_on_and_none_together_or_neither_is_a_usage_error(based):
    before = state(based)
    assert run("attach-deps", based, "0004").returncode == EXIT_USAGE
    assert run("attach-deps", based, "0004", "--none", "--on", "0003").returncode == EXIT_USAGE
    assert state(based) == before


@pytest.mark.parametrize("second", [(), ("0002",), ("0003",)])
def test_order51_attach_deps_is_set_once(based, second):
    declared(based, "0004", "0003")
    before = state(based)
    res = deps(based, "0004", *second)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D2_DEPS_SET_ONCE"], res.stderr
    assert "already declared" in res.stderr
    assert state(based) == before


def test_order51_none_is_set_once_too(based):
    declared(based, "0001")
    res = deps(based, "0001", "0000")
    assert failed(res.stderr) == ["D2_DEPS_SET_ONCE"], res.stderr


def test_order51_attach_deps_refuses_an_unknown_entry_id_as_an_edge(based):
    before = state(based)
    res = deps(based, "0004", "0003", "0099")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D3_EDGES_RESOLVE"], res.stderr
    assert "0099 is not an entry in this registry" in res.stderr
    assert state(based) == before


def test_order51_attach_deps_refuses_an_unknown_target_and_a_drifted_registry(based):
    res = deps(based, "0099", "0003")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr
    assert re.search(r"^not-evaluated\s+D4_NO_CYCLE", res.stderr, re.M), res.stderr
    edit(based, lambda r: r["0004"]["predictions"][0].update(p=0.9))
    res = deps(based, "0004", "0003")
    assert failed(res.stderr) == ["D1_ENTRY_EXISTS_AND_UNDRIFTED"], res.stderr


def test_order51_attach_deps_refuses_a_pack_not_in_packs(based):
    res = deps(based, "0004", "pack:no_such_pack")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D3_EDGES_RESOLVE"], res.stderr
    assert "pack:no_such_pack is not in PACKS.jsonl" in res.stderr


def test_order51_attach_deps_accepts_a_pack_in_packs(based):
    res = declared(based, "0004", "0003", f"pack:{FIXTURE_PACK}")
    assert f"DEPENDENCIES DECLARED: 0004 -> 0003, pack:{FIXTURE_PACK}" in res.stdout
    assert ledger_lines(based)[-1]["frozen"] == {"depends_on": ["0003", f"pack:{FIXTURE_PACK}"]}


def test_order51_attach_deps_refuses_an_edge_named_twice(based):
    res = deps(based, "0004", "0003", "0003")
    assert failed(res.stderr) == ["D3_EDGES_RESOLVE"], res.stderr
    assert "0003 is named twice" in res.stderr


def test_order51_a_self_dependency_is_refused(based):
    before = state(based)
    res = deps(based, "0004", "0004")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D4_NO_CYCLE"], res.stderr
    assert "self-dependency: 0004 depends on itself" in res.stderr
    assert state(based) == before


def test_order51_a_cycle_is_refused_and_named(based):
    declared(based, "0003", "0004")
    before = state(based)
    res = deps(based, "0004", "0003")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["D4_NO_CYCLE"], res.stderr
    assert "cycle: 0004 → 0003 → 0004" in res.stderr
    assert state(based) == before


def test_order51_a_longer_cycle_is_refused_and_named(based):
    declared(based, "0002", "0003")
    declared(based, "0003", "0004")
    res = deps(based, "0004", "0001", "0002")
    assert failed(res.stderr) == ["D4_NO_CYCLE"], res.stderr
    assert "cycle: 0004 → 0002 → 0003 → 0004" in res.stderr


def test_order51_a_diamond_is_not_a_cycle(based):
    declared(based, "0001", "0000")
    declared(based, "0002", "0000")
    declared(based, "0003", "0001", "0002")
    assert run("check", based).returncode == EXIT_OK


@pytest.mark.parametrize("change", [
    lambda e: e.update(depends_on=["0002"]),
    lambda e: e.update(depends_on=[]),
    lambda e: e.update(depends_on=["0003", "0002"]),
    lambda e: e.pop("depends_on"),
])
def test_order51_hand_editing_a_declared_edge_is_a_moved_goalpost(based, change):
    declared(based, "0004", "0003")
    edit(based, lambda r: change(r["0004"]))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004 field depends_on [\"0003\"] -> " in res.stderr, res.stderr


def test_order51_adding_an_edge_by_hand_is_a_moved_goalpost(based):
    edit(based, lambda r: r["0004"].update(depends_on=["0003"]))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "GOALPOST MOVED: 0004 field depends_on <absent> -> [\"0003\"]" in res.stderr


def test_order51_a_tampered_attach_deps_line_breaks_the_chain(based):
    declared(based, "0004", "0003")
    led = based.parent / LEDGER
    led.write_text(led.read_text().replace('"depends_on":["0003"]', '"depends_on":["0002"]'))
    res = run("check", based)
    assert res.returncode == EXIT_CHECK
    assert "LEDGER TAMPERED" in res.stderr


# ------------------------------------------------------ quarantine, computed (Part B)

def test_order51_positive_control_b_killed_quarantines_c_on_the_very_next_check(chain3):
    res = run("check", chain3)
    assert res.returncode == EXIT_OK and quarantined(res) == [], res.stdout + res.stderr
    kill(chain3, "0006")
    res = run("check", chain3)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert quarantined(res) == ["0007 ← 0006 INVALID"], res.stdout
    assert re.match(r"^  8 prediction\(s\), 0 narrative\(s\), ", res.stdout.splitlines()[-1]), \
        "the summary stays the last line, which nightly.registry_line() matches"


def test_order51_positive_control_scoring_c_is_refused_s8_naming_the_path(chain3):
    kill(chain3, "0006")
    before = state(chain3)
    res = score_held(chain3, "0007")
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S8_NOT_QUARANTINED"], res.stderr
    assert "0007 is quarantined: 0007 ← 0006 INVALID" in res.stderr
    assert state(chain3) == before


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order51_positive_control_attach_prereg_on_c_is_refused(chain3, tmp_path):
    kill(chain3, "0006")
    prereg = sealed_prereg(tmp_path)
    before = state(chain3)
    res = run("attach-prereg", chain3, "0007-p1", prereg)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["A_NOT_QUARANTINED"], res.stderr
    assert "0007 is quarantined: 0007 ← 0006 INVALID" in res.stderr
    assert state(chain3) == before


def test_order51_attach_pack_on_a_quarantined_entry_is_refused(chain3):
    kill(chain3, "0006")
    before = state(chain3)
    res = run("attach-pack", chain3, "0007-p1", FIXTURE_PACK)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["A_NOT_QUARANTINED"], res.stderr
    assert "0007 is quarantined: 0007 ← 0006 INVALID" in res.stderr
    assert state(chain3) == before


def test_order51_quarantine_is_transitive_and_names_the_whole_path(chain3):
    kill(chain3, "0005")
    res = run("check", chain3)
    assert quarantined(res) == ["0006 ← 0005 INVALID", "0007 ← 0006 ← 0005 INVALID"], res.stdout
    res = score_held(chain3, "0007")
    assert failed(res.stderr) == ["S8_NOT_QUARANTINED"], res.stderr
    assert "0007 ← 0006 ← 0005 INVALID" in res.stderr


def test_order51_quarantine_is_computed_never_stored(chain3):
    kill(chain3, "0006")
    reg = json.loads(chain3.read_text())
    assert reg["0007"]["status"] == "open", "no status is written downstream"
    assert all(l["entry"] != "0007" or l["kind"] in ("baseline", "attach-deps")
               for l in ledger_lines(chain3)), "no ledger line is written downstream"
    assert "QUARANTINED" not in chain3.read_text()


def test_order51_a_hand_set_invalid_status_quarantines_downstream_too(based):
    """Odin's live check b, on the fixture: Part D's declarations, then 0003 dies by a prose
    kill decided by hand through `status` → 0004 is quarantined, path `0004 ← 0003 INVALID`."""
    for eid in ("0000", "0001", "0002", "0003"):
        declared(based, eid)
    declared(based, "0004", "0003")
    assert quarantined(run("check", based)) == []
    assert run("status", based, "0003", "INVALID — prose kill met (fixture)").returncode == EXIT_OK
    res = run("check", based)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert quarantined(res) == ["0004 ← 0003 INVALID"], res.stdout


def test_order51_check_lists_the_dropped_seal_of_a_quarantined_entry(registry):
    c = make_entry("0007", scope="proj/c")
    c["0007"]["predictions"][0]["prereg"] = "seal/PREREG.md"
    preload(registry, make_entry("0005"), make_entry("0006", scope="proj/b"), c)
    declared(registry, "0006", "0005")
    declared(registry, "0007", "0006")
    assert "seal dropped" not in run("check", registry).stdout
    kill(registry, "0006")
    res = run("check", registry)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.findall(r"^\s*seal dropped: (.+)$", res.stdout, re.M) == \
        ["seal/PREREG.md (entry quarantined: 0007 ← 0006 INVALID)"], res.stdout


def test_order51_invalid_is_final_so_quarantine_does_not_lift(chain3):
    kill(chain3, "0006")
    assert run("status", chain3, "0006", "reopened").returncode == EXIT_REFUSED
    assert deps(chain3, "0007", "0005").returncode == EXIT_REFUSED      # the edge is set once
    assert quarantined(run("check", chain3)) == ["0007 ← 0006 INVALID"]


# ----------------------------------------------------------------- pack edges

@pytest.mark.parametrize("result", ["MISMATCH", "TAMPERED"])
def test_order51_a_pack_whose_latest_replay_failed_quarantines_and_a_match_lifts_it(
        registry, tmp_path, own_ledgers, result):
    runner, replays, _ = own_ledgers
    name = pack_name(tmp_path, "rep")
    make_pack(tmp_path, name)
    preload(registry, make_entry("0005"))
    declared(registry, "0005", f"pack:{name}", runner=runner)
    plant_replay(replays, name, "MATCH")
    assert quarantined(runner("check", registry)) == []
    plant_replay(replays, name, result)
    res = runner("check", registry)
    assert quarantined(res) == [f"0005 ← pack:{name} {result}"], res.stdout + res.stderr
    res = score_held(registry, "0005", runner=runner)
    assert failed(res.stderr) == ["S8_NOT_QUARANTINED"], res.stderr
    assert f"0005 ← pack:{name} {result}" in res.stderr
    plant_replay(replays, name, "MATCH")                    # packs can recover; INVALID can't
    assert quarantined(runner("check", registry)) == []
    res = score_held(registry, "0005", runner=runner)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_order51_a_pack_whose_latest_seat_swap_disagrees_quarantines_and_an_agree_lifts_it(
        registry, tmp_path, own_ledgers):
    runner, _, swaps = own_ledgers
    name = pack_name(tmp_path, "swp")
    make_pack(tmp_path, name)
    preload(registry, make_entry("0005"))
    declared(registry, "0005", f"pack:{name}", runner=runner)
    plant_swap(swaps, name, "AGREE")
    assert quarantined(runner("check", registry)) == []
    plant_swap(swaps, name, "DISAGREE")
    res = runner("check", registry)
    assert quarantined(res) == [f"0005 ← pack:{name} DISAGREE"], res.stdout + res.stderr
    assert failed(score_held(registry, "0005", runner=runner).stderr) == ["S8_NOT_QUARANTINED"]
    plant_swap(swaps, name, "AGREE")
    assert quarantined(runner("check", registry)) == []


def test_order51_another_packs_failure_does_not_quarantine(registry, tmp_path, own_ledgers):
    runner, replays, swaps = own_ledgers
    mine, other = pack_name(tmp_path, "mine"), pack_name(tmp_path, "other")
    make_pack(tmp_path, mine)
    preload(registry, make_entry("0005"))
    declared(registry, "0005", f"pack:{mine}", runner=runner)
    plant_replay(replays, other, "MISMATCH")
    plant_swap(swaps, other, "DISAGREE")
    assert quarantined(runner("check", registry)) == []


def test_order51_a_pack_failure_quarantines_downstream_through_an_entry(registry, tmp_path,
                                                                         own_ledgers):
    runner, replays, _ = own_ledgers
    name = pack_name(tmp_path, "x")
    make_pack(tmp_path, name)
    preload(registry, make_entry("0005"), make_entry("0006", scope="proj/b"))
    declared(registry, "0005", f"pack:{name}", runner=runner)
    declared(registry, "0006", "0005", runner=runner)
    plant_replay(replays, name, "MISMATCH")
    assert quarantined(runner("check", registry)) == [
        f"0005 ← pack:{name} MISMATCH", f"0006 ← 0005 ← pack:{name} MISMATCH"]


def test_order51_an_unreadable_replays_line_fails_closed(registry, tmp_path, own_ledgers):
    runner, replays, _ = own_ledgers
    name = pack_name(tmp_path, "bad")
    make_pack(tmp_path, name)
    preload(registry, make_entry("0005"), make_entry("0006", scope="proj/b"))
    declared(registry, "0005", f"pack:{name}", runner=runner)
    declared(registry, "0006", runner=runner)
    plant_replay(replays, name, "MATCH")
    with open(replays, "a") as fh:
        fh.write("{not json\n")
    res = runner("check", registry)
    assert quarantined(res) == [f"0005 ← pack:{name} REPLAYS.jsonl unreadable at line 2"], \
        res.stdout + res.stderr


def test_order51_a_broken_swaps_chain_fails_closed(registry, tmp_path, own_ledgers):
    runner, _, swaps = own_ledgers
    name = pack_name(tmp_path, "chain")
    make_pack(tmp_path, name)
    preload(registry, make_entry("0005"))
    declared(registry, "0005", f"pack:{name}", runner=runner)
    plant_swap(swaps, name, "AGREE")
    plant_swap(swaps, name, "AGREE")
    assert quarantined(runner("check", registry)) == []
    swaps.write_bytes(swaps.read_bytes().split(b"\n", 1)[1])        # first line removed
    res = runner("check", registry)
    assert quarantined(res) == [f"0005 ← pack:{name} SWAPS.jsonl chain broken at line 1"], \
        res.stdout + res.stderr


# ------------------------------------------------------------------- acquittal

def test_order51_acquittal_a_healthy_upstream_quarantines_nothing(chain3):
    res = score_held(chain3, "0006")                 # scored, the kill not met: still open
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "kill not met" in res.stdout
    res = run("check", chain3)
    assert quarantined(res) == [] and "seal dropped" not in res.stdout
    res = score_held(chain3, "0007")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert re.search(r"^ok\s+S8_NOT_QUARANTINED: ", res.stdout, re.M), res.stdout


def test_order51_acquittal_none_entries_and_upstreams_are_never_quarantined(chain3):
    kill(chain3, "0006")
    rg = load_regate()
    q = rg.provenance(chain3)["quarantined"]
    assert "0005" not in q, "an upstream is not quarantined by what rests on it"
    assert "0006" not in q, "INVALID is its own state, not a quarantine"
    assert list(q) == ["0007"]


def test_order51_acquittal_an_undeclared_entry_is_reported_not_refused(based):
    res = run("score", based, "0000-p1", "--outcome", "1", "--note", "x")
    assert "S8_NOT_QUARANTINED" in res.stdout + res.stderr
    assert "S8_NOT_QUARANTINED" not in failed(res.stderr)


def test_order51_s8_is_not_evaluated_for_an_unknown_id(based):
    res = run("score", based, "0099-p1", "--outcome", "1", "--note", "x")
    assert re.search(r"^not-evaluated\s+S8_NOT_QUARANTINED", res.stderr, re.M), res.stderr


# ----------------------------------------------------------- the nightly line (Part C)

def test_order51_provenance_line_counts_and_lists_quarantined_paths(based):
    rg = load_regate()
    assert rg.provenance_line(based) == ("- provenance: 5 entries · 0 edges · 5 undeclared · "
                                         "0 quarantined · no cycles")
    for eid in ("0000", "0001", "0002", "0003"):
        declared(based, eid)
    declared(based, "0004", "0003")
    before = state(based)
    assert rg.provenance_line(based) == ("- provenance: 5 entries · 1 edge · 0 undeclared · "
                                         "0 quarantined · no cycles")
    assert state(based) == before, "reads only"
    assert run("status", based, "0003", "INVALID — prose kill met (fixture)").returncode == EXIT_OK
    assert rg.provenance_line(based) == ("- provenance: 5 entries · 1 edge · 0 undeclared · "
                                         "1 quarantined · no cycles\n"
                                         "  - 🔴 QUARANTINED: 0004 ← 0003 INVALID")


def test_order51_provenance_line_reports_a_broken_ledger_and_a_missing_registry(based, tmp_path):
    rg = load_regate()
    assert rg.provenance_line(tmp_path / "nope.json") == "- provenance: no registry.json"
    led = based.parent / LEDGER
    led.write_bytes(led.read_bytes().split(b"\n", 1)[1])
    assert rg.provenance_line(based).startswith("- provenance: 🔴 LEDGER TAMPERED: line ")


def test_order51_provenance_line_names_a_cycle_that_got_into_the_ledger(based):
    """attach-deps refuses a cycle; the nightly still says so if one ever got past it."""
    rg = load_regate()
    declared(based, "0003", "0004")
    reg = json.loads(based.read_text())
    reg["0004"]["depends_on"] = ["0003"]
    rg.append_ledger(based, [rg.ledger_record("attach-deps", "0004", reg["0004"],
                                              frozen={"depends_on": ["0003"]},
                                              frozen_sha256=rg.sha256_bytes(
                                                  rg.canonical({"depends_on": ["0003"]})))])
    based.write_text(json.dumps(reg, indent=1, ensure_ascii=False))
    line = rg.provenance_line(based)
    assert line.splitlines()[0].endswith("· 🔴 1 cycle"), line
    assert "  - 🔴 cycle: 0003 → 0004 → 0003" in line.splitlines(), line


# ------------------------------------------- order 60 (P-6): run after the seal, S9

def early_sealed(kinded, tmp_path):
    """0004-p1 (experiment, due 2027-01-31, scope legacy) with a verifying prereg and an AGREE
    pack: S1-S8 all pass for an early score. -> the prereg path."""
    prereg = sealed_prereg(tmp_path)
    assert run("attach-prereg", kinded, "0004-p1", prereg).returncode == EXIT_OK
    swapped(kinded, "0004-p1")
    return prereg


def refused_on_s9_only(registry, res, before):
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert failed(res.stderr) == ["S9_RUN_AFTER_SEAL"], res.stderr
    assert (sha(registry), sha(registry.parent / LEDGER)) == before
    last = REFUSALS_LOG.read_text().splitlines()[-1]
    assert "verb=score" in last and last.endswith("failed=S9_RUN_AFTER_SEAL"), last


def test_order60_s9_is_last_and_s1_to_s8_keep_their_order():
    mod = load_module("regate_pin_s9", REGATE)
    assert mod.SCORE_PRECONDITIONS == [
        "S1_EXISTS_AND_UNDRIFTED", "S2_DUE_HAS_ARRIVED", "S3_NOT_ALREADY_SCORED",
        "S4_NONE_NEEDS_UNDERPOWERED", "S5_EXPERIMENT_SEALED", "S6_VALUE_MATCHES_RULE",
        "S7_SEAT_SWAP", "S8_NOT_QUARANTINED", "S9_RUN_AFTER_SEAL"]
    assert mod.ATTEMPTS == Path.home() / "helix/workspace/cycle/ATTEMPTS.jsonl"


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_run_logged_1s_after_the_seal_lets_the_early_score_through(kinded,
                                                                                tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ts = ran_after_seal(prereg, offset=1)
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert f"ok       S9_RUN_AFTER_SEAL: 1 run(s) in legacy after the seal; first {ts}\n" \
        in res.stdout, res.stdout
    assert json.loads(kinded.read_text())["0004"]["predictions"][0]["outcome"] == 1
    assert run("check", kinded).returncode == EXIT_OK


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_run_logged_1s_before_the_seal_only_is_refused(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, offset=-1)
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    refused_on_s9_only(kinded, res, before)
    issued = json.loads((prereg.parent / "RUN_TOKEN.json").read_text())["issued_at"]
    assert (f"FAILED   S9_RUN_AFTER_SEAL: 0004-p1 is scored early, but no run in scope legacy "
            f"is logged after its seal ({issued}); run it with attempt.py first") \
        in res.stderr, res.stderr


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_run_at_the_seal_second_is_not_after_it(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, offset=0)
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    refused_on_s9_only(kinded, run("score", kinded, "0004-p1", "--outcome", "1", "--note",
                                   "x"), before)


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_run_logged_in_the_future_does_not_count(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    issued = json.loads((prereg.parent / "RUN_TOKEN.json").read_text())["issued_at"]
    later = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    ATTEMPTS_LOG.write_text(json.dumps({"scope": "legacy", "kind": "run", "n": 1,
                                        "ts": later.strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "x")
    refused_on_s9_only(kinded, res, before)
    assert f"after its seal ({issued})" in res.stderr


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_run_after_the_seal_in_another_scope_is_refused(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, scope="helix/selftest", offset=1)
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    refused_on_s9_only(kinded, res, before)
    assert "no run in scope legacy is logged after its seal" in res.stderr, res.stderr


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_a_missing_attempts_file_fails_closed(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, offset=1)
    ATTEMPTS_LOG.unlink()
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    refused_on_s9_only(kinded, res, before)
    assert f"the attempts log {ATTEMPTS_LOG} cannot be read (FileNotFoundError" in res.stderr, \
        res.stderr


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_s9_an_unreadable_attempts_line_fails_closed(kinded, tmp_path):
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, offset=1)
    with open(ATTEMPTS_LOG, "a") as fh:
        fh.write("{not json\n")
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it, it held")
    refused_on_s9_only(kinded, res, before)
    assert "ATTEMPTS.jsonl line 2 cannot be read" in res.stderr, res.stderr


def test_order60_s9_an_unreadable_seal_time_fails_closed(tmp_path, monkeypatch):
    """S5 runs sealgate verify first, so through the CLI a token with no readable issued_at
    cannot get this far; the function still refuses, and says which."""
    rg = _regate_module()
    attempts = tmp_path / "ATTEMPTS.jsonl"
    attempts.write_text(json.dumps({"scope": "legacy", "ts": "2026-10-01T10:00:00Z"}) + "\n")
    monkeypatch.setattr(rg, "ATTEMPTS", attempts)
    seal = tmp_path / "s"
    seal.mkdir()
    (seal / "RUN_TOKEN.json").write_text(json.dumps({"issued_at": "not a time"}))
    reg = tmp_path / "registry.json"
    reg.write_text("{}")
    status, detail = rg.run_after_seal({"id": "x-p1", "prereg": str(seal / "PREREG.md")}, {}, reg)
    assert status == "failed" and "its seal time cannot be read (ValueError" in detail, detail
    status, detail = rg.run_after_seal({"id": "x-p1", "prereg": str(tmp_path / "no" / "P.md")},
                                       {}, reg)
    assert status == "failed" and "its seal time cannot be read (FileNotFoundError" in detail
    (seal / "RUN_TOKEN.json").write_text(json.dumps({"issued_at": "2026-10-01T09:00:00Z"}))
    assert rg.run_after_seal({"id": "x-p1", "prereg": str(seal / "PREREG.md")}, {}, reg) == \
        ("ok", "1 run(s) in legacy after the seal; first 2026-10-01T10:00:00Z")


def test_order60_s9_a_time_prediction_due_today_or_past_is_unchanged(scorable):
    """No attempts logged at all (the file is empty): S9 does not apply."""
    res = run("score", scorable, "0004-p1", "--outcome", "1", "--note", "held")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "ok       S9_RUN_AFTER_SEAL: not an early experiment score\n" in res.stdout


def test_order60_s9_an_experiment_scored_on_or_after_its_due_date_is_unchanged(registry):
    edit(registry, lambda r: r["0004"]["predictions"][0].update(kind="experiment",
                                                                due="2026-01-31"))
    assert run("baseline", registry).returncode == EXIT_OK
    swapped(registry, "0004-p1")
    ATTEMPTS_LOG.unlink()                       # not even read: S9 does not apply
    res = run("score", registry, "0004-p1", "--outcome", "0", "--note", "ran it, it failed")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "ok       S9_RUN_AFTER_SEAL: not an early experiment score\n" in res.stdout


def test_order60_s9_an_early_time_prediction_is_refused_by_s2_alone(kinded):
    res = run("score", kinded, "0000-p1", "--outcome", "1", "--note", "early")
    assert failed(res.stderr) == ["S2_DUE_HAS_ARRIVED"], res.stderr
    assert re.search(r"^ok\s+S9_RUN_AFTER_SEAL: not an early experiment score$", res.stderr,
                     re.M), res.stderr


def test_order60_s9_waits_on_s5_when_the_experiment_names_no_prereg(kinded):
    swapped(kinded, "0004-p1")
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "ran it")
    assert failed(res.stderr) == ["S5_EXPERIMENT_SEALED"], res.stderr
    assert re.search(r"^not-evaluated\s+S9_RUN_AFTER_SEAL: depends on S5_EXPERIMENT_SEALED$",
                     res.stderr, re.M), res.stderr


@pytest.mark.skipif(not SEALGATE.exists(), reason="sealgate not present")
def test_order60_positive_control_catch_7_seal_then_score_with_no_logged_run(kinded, tmp_path):
    """Catch #7's shape: the run happened (logged 7m38s BEFORE the seal, as 0002-p1's result
    was known before it was sealed), then seal, then score minutes later. Refused."""
    prereg = early_sealed(kinded, tmp_path)
    ran_after_seal(prereg, offset=-458)
    before = (sha(kinded), sha(kinded.parent / LEDGER))
    res = run("score", kinded, "0004-p1", "--outcome", "1", "--note", "held, scored after seal")
    print(res.stderr)
    refused_on_s9_only(kinded, res, before)
    # and with nothing logged at all
    ATTEMPTS_LOG.write_text("")
    refused_on_s9_only(kinded, run("score", kinded, "0004-p1", "--outcome", "1", "--note", "x"),
                       before)


def test_order60_s9_has_no_way_around_it():
    src = REGATE.read_text()
    body = src[src.index("def run_after_seal"):src.index("\ndef cycle_start")]
    assert "environ" not in body and "getenv" not in body and "args." not in body
    s9 = src[src.index('        # Order 60 (P-6): an early score'):]
    s9 = s9[:s9.index("    if any(v[0] ==")]
    assert "environ" not in s9 and "args." not in s9.replace("args.registry", "")
