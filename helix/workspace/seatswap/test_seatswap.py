"""Order 49, Part A: seatswap.py. Alice is always a fake injected with monkeypatch; her swap.py
runs in the REAL reproduction sandbox (bwrap, no spend). Packs are made with the real replay.py
on a temp PACKS.jsonl and a temp bulk dir; the ledger is a temp SWAPS.jsonl."""

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

import seatswap

HERE = Path(__file__).resolve().parent
REPLAY_PY = HERE.parent / "replay" / "replay.py"

# The test pack: a tiny analysis named like the real one, so `import score_33b` is the copy.
SCRIPT = '''"""Total of column x, and the w-weighted sum of x, over the rows of data.csv (header x,w)."""
import csv
import sys

rows = list(csv.DictReader(open(sys.argv[1])))
print(f"total: {sum(float(r['x']) for r in rows)}")
print(f"weighted: {sum(float(r['x']) * float(r['w']) for r in rows)}")
'''
DATA = "x,w\n1234.5,2\n2345.25,3\n3456.125,5\n"
TOTAL, WEIGHTED = 7035.875, 26785.375          # strings that appear nowhere in the inputs
NUMBERS = {"total": {"value": TOTAL, "pattern": r"^total: ([\d.]+)$"},
           "weighted": {"value": WEIGHTED, "pattern": r"^weighted: ([\d.]+)$"}}
TOLERANCE = {"total": 0, "weighted": 0.01}
NOTE = "Sum of x, and sum of x*w, over data.csv. Deterministic."

RIGHT = '''import csv, json
rows = list(csv.DictReader(open("../pack/data.csv")))
total = sum(float(r["x"]) for r in rows)
weighted = sum(float(r["x"]) * float(r["w"]) for r in rows)
print("my own reading of the method")
print(json.dumps({"total": total, "weighted": weighted}))
'''
NOTES = "Nothing looks wrong: two sums over three rows.\n"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def temp_everything(tmp_path, monkeypatch):
    """Each test: its own SWAPS.jsonl, PACKS.jsonl, pack bulk and run bulk; no real Alice."""
    monkeypatch.setattr(seatswap, "LEDGER", tmp_path / "SWAPS.jsonl")
    monkeypatch.setattr(seatswap, "PACKS", tmp_path / "PACKS.jsonl")
    monkeypatch.setattr(seatswap, "PACK_BULK", tmp_path / "packs")
    monkeypatch.setattr(seatswap, "BULK", tmp_path / "runs")
    (tmp_path / "packs").mkdir()

    def no_real_alice(workdir, prompt):
        raise AssertionError("a test reached the real Alice")
    monkeypatch.setattr(seatswap, "ALICE", no_real_alice)
    yield


@pytest.fixture
def pack(tmp_path):
    """A real replay pack, made by replay.py itself on the temp ledger and bulk dir."""
    replay = load("seatswap_test_replay", REPLAY_PY)
    replay.PACKS, replay.BULK, replay.MOUNT = seatswap.PACKS, seatswap.PACK_BULK, None
    src = tmp_path / "src"
    src.mkdir()
    (src / "score_33b.py").write_text(SCRIPT)
    (src / "data.csv").write_text(DATA)
    replay.make("toy_pack", f"python3 {src / 'score_33b.py'} {src / 'data.csv'}",
                [src / "data.csv"], NUMBERS, TOLERANCE, note=NOTE)
    return "toy_pack"


def alice_writes(files, seen=None):
    """A fake Alice that writes `files` ({name: text}) into her cwd (alice/). If `seen` is a
    list, it gets (workdir, prompt, {every file in her scratch: bytes}) as she found them."""
    def fake(workdir, prompt):
        workdir = Path(workdir)
        if seen is not None:
            scratch = workdir.parent
            found = {str(p.relative_to(scratch)): p.read_bytes()
                     for p in scratch.rglob("*") if p.is_file()}
            seen.append((workdir, prompt, found))
        for name, text in files.items():
            (workdir / name).write_text(text)
        return {"model": "fake"}
    return fake


def ledger():
    return seatswap.read_ledger(seatswap.LEDGER)


# ---------------------------------------------------------------------- outcomes

def test_right_numbers_agree(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT, "NOTES.md": NOTES}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "AGREE", rec
    assert [r["ok"] for r in rec["comparison"]] == [True, True]
    assert rec["comparison"][0] == {"key": "total", "alice": TOTAL, "packed": TOTAL, "tol": 0.0,
                                    "ok": True}
    assert seatswap.label(rec) == "AGREE"
    assert len(ledger()) == 1


def test_one_number_off_beyond_tolerance_is_disagree_naming_the_key(pack, monkeypatch):
    """Positive control: `weighted` is 1 off, its tolerance is 0.01."""
    off = RIGHT.replace('"weighted": weighted', '"weighted": weighted + 1')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": off, "NOTES.md": NOTES}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "DISAGREE", rec
    assert seatswap.label(rec) == \
        "DISAGREE (weighted: alice 26786.375, packed 26785.375, tol 0.01)"
    assert [r["key"] for r in rec["comparison"] if not r["ok"]] == ["weighted"]


def test_within_tolerance_still_agrees(pack, monkeypatch):
    near = RIGHT.replace('"weighted": weighted', '"weighted": weighted + 0.005')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": near}))
    assert seatswap.run(pack)["outcome"] == "AGREE"


def test_a_missing_key_is_a_disagreement(pack, monkeypatch):
    missing = RIGHT.replace(', "weighted": weighted', "")
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": missing}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "DISAGREE"
    assert seatswap.label(rec) == "DISAGREE (weighted: alice missing, packed 26785.375, tol 0.01)"


def test_a_value_that_is_not_a_number_is_a_disagreement(pack, monkeypatch):
    text = RIGHT.replace('"total": total', '"total": str(total)')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": text}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "DISAGREE"
    assert "total: alice missing" in seatswap.label(rec)


def test_importing_the_packs_script_is_copied(pack, monkeypatch):
    text = "import json\nimport score_33b\nprint(json.dumps({'total': 0, 'weighted': 0}))\n"
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": text}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "COPIED", rec
    assert "score_33b" in rec["why"]
    assert rec["repro"] == [] and rec["comparison"] == []      # a copy is never run or compared


def test_running_the_packs_script_by_subprocess_is_copied(pack, monkeypatch):
    text = ("import subprocess\n"
            "subprocess.run(['python3', '../pack/' + 'score' + '_33b.py', '../pack/data.csv'])\n")
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": text}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "COPIED"
    assert "subprocess" in rec["why"]


def test_a_byte_copy_of_the_packs_script_is_copied(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT, "mine.py": SCRIPT}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "COPIED"
    assert "mine.py" in rec["why"] and "sha256" in rec["why"]


def test_nondeterministic_output_is_not_reproduced(pack, monkeypatch):
    text = "import json, os\nprint(json.dumps({'total': int.from_bytes(os.urandom(4), 'big'), " \
           "'weighted': 0}))\n"
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": text}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "NOT_REPRODUCED", rec
    assert rec["comparison"] == []
    assert len(rec["repro"]) == 2


def test_a_script_that_fails_is_not_reproduced(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": "raise SystemExit(3)\n"}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "NOT_REPRODUCED"
    assert [r["rc"] for r in rec["repro"]] == [3, 3]


def test_a_last_line_that_is_not_a_json_object_is_not_reproduced(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": "print('total 7035.875')\n"}))
    assert seatswap.run(pack)["outcome"] == "NOT_REPRODUCED"


def test_reproduction_has_no_network_and_a_read_only_pack(pack, monkeypatch):
    text = ('import json, socket\n'
            'try:\n    open("../pack/data.csv", "a").write("1,1\\n"); w = "writable"\n'
            'except OSError:\n    w = "read-only"\n'
            'try:\n    socket.create_connection(("1.1.1.1", 53), timeout=2); n = "net"\n'
            'except OSError:\n    n = "no-net"\n'
            'print(w, n)\n'
            'print(json.dumps({"total": 7035.875, "weighted": 26785.375}))\n')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": text}))
    rec = seatswap.run(pack)
    out = (Path(rec["run_dir"]) / "repro1.stdout.txt").read_text()
    assert out.splitlines()[0] == "read-only no-net", out
    assert rec["outcome"] == "AGREE"


def test_no_swap_py_is_failed(pack, monkeypatch):
    """A refusal (order 48b's first run: 'Sorry, I cannot fulfill your request') leaves nothing."""
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({}))
    rec = seatswap.run(pack)
    assert rec["outcome"] == "FAILED"
    assert "no swap.py" in rec["why"]


def test_an_alice_error_is_failed(pack, monkeypatch):
    def boom(workdir, prompt):
        raise seatswap.AliceFailed("agy status 'ERROR'")
    monkeypatch.setattr(seatswap, "ALICE", boom)
    rec = seatswap.run(pack)
    assert seatswap.label(rec) == "FAILED (agy status 'ERROR')"


def test_a_tampered_pack_is_failed_and_alice_is_never_asked(pack):
    data = seatswap.PACK_BULK / pack / "data.csv"
    st = data.stat()
    data.write_text(DATA.replace("1234.5", "1234.6"))
    os.utime(data, ns=(st.st_atime_ns, st.st_mtime_ns))
    rec = seatswap.run(pack)                     # ALICE is the autouse no_real_alice
    assert seatswap.label(rec) == "FAILED (pack tampered)"
    assert "data.csv: sha256 changed" in rec["changes"]


def test_an_unknown_pack_is_refused_without_a_ledger_line(pack):
    with pytest.raises(seatswap.Refused):
        seatswap.run("no_such_pack")
    assert not seatswap.LEDGER.exists()


# ------------------------------------------------------- what Alice is given

def test_the_reported_values_are_withheld_from_her(pack, monkeypatch):
    seen = []
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}, seen))
    seatswap.run(pack)
    workdir, prompt, found = seen[0]
    values = [str(TOTAL), str(WEIGHTED), "7035", "26785"]
    for v in values:
        assert v not in prompt
        for name, raw in found.items():
            assert v.encode() not in raw, f"{v} in {name}"
    shown = json.loads(found["pack/manifest.json"])
    assert shown["numbers"] == {"total": {"pattern": NUMBERS["total"]["pattern"]},
                                "weighted": {"pattern": NUMBERS["weighted"]["pattern"]}}
    assert shown["note"] == NOTE
    assert sorted(found) == ["pack/data.csv", "pack/manifest.json", "pack/score_33b.py"]


def test_the_pack_copy_keeps_its_mtimes(pack, monkeypatch):
    seen = []
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}, seen))
    seatswap.run(pack)
    workdir = seen[0][0]
    for name in ("data.csv", "score_33b.py"):
        assert (workdir.parent / "pack" / name).stat().st_mtime_ns == \
            (seatswap.PACK_BULK / pack / name).stat().st_mtime_ns


def test_the_brief_points_to_the_pack_and_is_under_the_cap(pack, monkeypatch):
    seen = []
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}, seen))
    rec = seatswap.run(pack)
    prompt = seen[0][1]
    assert "Write YOUR OWN analysis (`swap.py`)" in prompt
    assert "do not import, call, exec or copy it" in prompt
    assert "../pack/" in prompt and "total, weighted" in prompt
    assert DATA not in prompt and SCRIPT not in prompt
    assert rec["prompt_bytes"] == len(prompt.encode()) < 120_000


def test_credential_named_files_are_never_copied(tmp_path):
    src = tmp_path / "p"
    (src / "sub").mkdir(parents=True)
    for n in ("a.csv", "my_credentials.txt", "sub/ACCOUNTS.json", ".env", "manifest.json"):
        (src / n).write_text("x")
    copied, left_out = seatswap.copy_pack(src, tmp_path / "dest")
    assert copied == ["a.csv"]
    assert sorted(n for n, _ in left_out) == [".env", "my_credentials.txt", "sub/ACCOUNTS.json"]
    assert sorted(p.name for p in (tmp_path / "dest").rglob("*") if p.is_file()) == ["a.csv"]


def test_alice_runs_with_the_pack_read_only_and_cwd_alice(tmp_path, monkeypatch):
    """call_alice hands the sandbox: scratch writable, scratch/pack read-only, cwd scratch/alice."""
    calls = []

    class FakeSandbox:
        MODEL = "fake"

        @staticmethod
        def run_alice(scratch, prompt, timeout, cwd=None, read_only=()):
            calls.append((Path(scratch), Path(cwd), [Path(p) for p in read_only], timeout))
            return 0, json.dumps({"status": "SUCCESS", "response": "ok"}), "", 1.0
    monkeypatch.setattr(seatswap, "sandbox_module", lambda: FakeSandbox)
    work = tmp_path / "scratch" / "alice"
    work.mkdir(parents=True)
    meta = seatswap.call_alice(work, "p")
    assert calls == [(tmp_path / "scratch", work, [tmp_path / "scratch" / "pack"], 1200)]
    assert meta["response_tail"] == "ok"


def test_alice_saying_anything_but_success_is_failed(tmp_path, monkeypatch):
    class FakeSandbox:
        MODEL = "fake"

        @staticmethod
        def run_alice(scratch, prompt, timeout, cwd=None, read_only=()):
            return 0, json.dumps({"status": "ERROR"}), "", 1.0
    monkeypatch.setattr(seatswap, "sandbox_module", lambda: FakeSandbox)
    with pytest.raises(seatswap.AliceFailed):
        seatswap.call_alice(tmp_path / "scratch" / "alice", "p")


# --------------------------------------------------------------------- ledger

def test_the_ledger_records_what_the_order_names(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT, "NOTES.md": NOTES}))
    rec = seatswap.run(pack)
    line = ledger()[0][2]
    assert line["pack"] == pack
    assert line["manifest_sha256"] == sha(seatswap.PACK_BULK / pack / "manifest.json")
    assert line["outcome"] == "AGREE"
    assert line["files"]["alice/swap.py"] == hashlib.sha256(RIGHT.encode()).hexdigest()
    assert line["notes_sha256"] == hashlib.sha256(NOTES.encode()).hexdigest()
    assert line["swap_sha256"] == hashlib.sha256(RIGHT.encode()).hexdigest()
    assert [r["key"] for r in line["comparison"]] == ["total", "weighted"]
    assert line["seq"] == 1 and line["prev"] == seatswap.GENESIS and line["seatswap"]
    assert rec == line


def test_the_ledger_is_hash_chained_with_vetos_chain(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    seatswap.run(pack)
    entries = ledger()
    assert seatswap.verify_chain(entries) == []
    assert entries[1][2]["prev"] == hashlib.sha256(entries[0][1].encode()).hexdigest()
    veto = load("seatswap_test_veto", HERE.parent / "veto" / "veto.py")
    assert veto.verify_chain(entries) == []                  # the same chain, reused
    raw = seatswap.LEDGER.read_bytes().split(b"\n")
    seatswap.LEDGER.write_bytes(b"\n".join(raw[1:]))          # first line removed
    with pytest.raises(RuntimeError, match="chain is broken"):
        seatswap.run(pack)


# ---------------------------------------------------------------- check (regate)

def check(name):
    return seatswap.check(name)


def test_check_passes_on_an_agree_for_the_current_manifest(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    res = check(pack)
    assert res["ok"] and res["pack_ok"] and res["outcome"] == "AGREE", res


def test_check_refuses_with_no_run(pack):
    res = check(pack)
    assert not res["ok"] and res["pack_ok"] and res["outcome"] == "none"


def test_check_refuses_a_disagree_even_beside_an_agree(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    off = RIGHT.replace('"total": total', '"total": total + 5')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": off}))
    seatswap.run(pack)
    res = check(pack)
    assert not res["ok"] and res["outcome"].startswith("DISAGREE (total:"), res


@pytest.mark.parametrize("outcome", ["COPIED", "NOT_REPRODUCED", "FAILED"])
def test_check_names_an_outcome_that_does_not_count(pack, outcome):
    m = seatswap.PACK_BULK / pack / "manifest.json"
    seatswap.append([{"kind": "swap", "pack": pack, "manifest_sha256": sha(m),
                      "outcome": outcome, "why": "fixture"}])
    res = check(pack)
    assert not res["ok"] and res["outcome"] == f"{outcome} (fixture)"


def test_check_refuses_an_agree_for_an_old_manifest(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    old = seatswap.run(pack)["manifest_sha256"]
    m = seatswap.PACK_BULK / pack / "manifest.json"
    man = json.loads(m.read_text())
    man["note"] += " (re-made)"
    m.write_text(json.dumps(man, indent=1) + "\n")
    rec = json.loads(seatswap.PACKS.read_text().splitlines()[-1])
    with open(seatswap.PACKS, "a") as fh:                    # the pack, as re-made: a new sha
        fh.write(json.dumps(dict(rec, manifest_sha256=sha(m))) + "\n")
    res = check(pack)
    assert res["pack_ok"] and not res["ok"]
    assert res["outcome"] == "none" and old[:12] in res["why"], res


def test_check_refuses_a_pack_not_in_packs(pack):
    res = check("nope")
    assert not res["ok"] and not res["pack_ok"] and "not in PACKS.jsonl" in res["why"]


def test_check_refuses_a_tampered_pack(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    (seatswap.PACK_BULK / pack / "extra.txt").write_text("x")
    res = check(pack)
    assert not res["ok"] and not res["pack_ok"] and "added extra.txt" in res["why"]


def test_check_refuses_on_a_broken_chain(pack, monkeypatch):
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    seatswap.run(pack)
    raw = seatswap.LEDGER.read_bytes().split(b"\n")
    seatswap.LEDGER.write_bytes(b"\n".join(raw[1:]))
    res = check(pack)
    assert not res["ok"] and "chain" in res["why"]


def test_the_check_verb_prints_json_and_exits_0_or_4(pack, monkeypatch, capsys):
    assert seatswap.main(["check", pack]) == 4
    assert json.loads(capsys.readouterr().out)["outcome"] == "none"
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    assert seatswap.main(["check", pack]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# --------------------------------------------------------------- nightly line

def test_the_nightly_line(pack, monkeypatch):
    assert seatswap.seatswap_line() == "- seatswap: none yet"
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": RIGHT}))
    seatswap.run(pack)
    off = RIGHT.replace('"total": total', '"total": total + 5')
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({"swap.py": off}))
    seatswap.run(pack)
    monkeypatch.setattr(seatswap, "ALICE", alice_writes({}))
    seatswap.run(pack)
    before = seatswap.LEDGER.read_bytes()
    assert seatswap.seatswap_line() == \
        "- seatswap: 3 runs · 1 agree · 1 disagree · chain OK"
    assert seatswap.LEDGER.read_bytes() == before
    raw = before.split(b"\n")
    seatswap.LEDGER.write_bytes(b"\n".join(raw[1:]))
    assert seatswap.seatswap_line() == "- seatswap: 🔴 chain BROKEN at line 1"


# ------------------------------------------------------------ no escape hatch

def test_seatswap_has_no_escape_hatch():
    src = (HERE / "seatswap.py").read_text()
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], f"escape hatch introduced: {hits}"


def test_the_cli_has_no_flag_that_stands_in_for_alice():
    out = subprocess.run([sys.executable, str(HERE / "seatswap.py"), "run", "--help"],
                         capture_output=True, text=True).stdout
    assert re.findall(r"--[\w-]+", out) == ["--help"], out
    out = subprocess.run([sys.executable, str(HERE / "seatswap.py"), "check", "--help"],
                         capture_output=True, text=True).stdout
    assert re.findall(r"--[\w-]+", out) == ["--help"], out


def test_the_sandbox_is_imported_not_copied():
    src = (HERE / "seatswap.py").read_text()
    assert "--ro-bind" not in src and "--unshare" not in src
    assert 'VETO / "sandbox.py"' in src
    assert 'REPLAY / "replay.py"' in src and 'VETO / "veto.py"' in src
