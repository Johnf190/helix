"""Tests for replay.py (order 45/45b). Every test runs on temp paths set by the `world` fixture:
never the live PACKS.jsonl / REPLAYS.jsonl, never __BULK__/replay."""
import json
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import replay  # noqa: E402

LIVE_PACKS, LIVE_REPLAYS, LIVE_BULK = replay.PACKS, replay.REPLAYS, replay.BULK

SCRIPT = '''import sys
ext = {ext!r}
x = int(open(sys.argv[1]).read().split()[0])
bump = int(open(ext).read()) if ext else 0
print(f"total: {{x * 10 + bump:,}}")
print(f"count: {{x}}")
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    monkeypatch.setattr(replay, "BULK", bulk)
    monkeypatch.setattr(replay, "MOUNT", None)
    monkeypatch.setattr(replay, "PACKS", tmp_path / "PACKS.jsonl")
    monkeypatch.setattr(replay, "REPLAYS", tmp_path / "REPLAYS.jsonl")
    return tmp_path


def synth(tmp, x=7, ext=None):
    """A tiny analysis: a script, its data dir, and the numbers it reports."""
    src = tmp / "src"
    (src / "data").mkdir(parents=True, exist_ok=True)
    (src / "data" / "in.txt").write_text(f"{x}\n")
    (src / "calc.py").write_text(SCRIPT.format(ext=str(ext) if ext else ""))
    cmd = f"{sys.executable} {src / 'calc.py'} {src / 'data' / 'in.txt'}"
    numbers = {"total": {"value": x * 10, "pattern": r"^total: ([\d,]+)$"},
               "count": {"value": x, "pattern": r"^count: (\d+)$"}}
    return cmd, [src / "data"], numbers, {"total": 0, "count": 0}


def ledger(p):
    return [json.loads(l) for l in Path(p).read_text().splitlines()] if Path(p).exists() else []


# ------------------------------------------------------------------------ the happy path

def test_make_then_run_matches(world):
    cmd, inputs, numbers, tol = synth(world)
    # the input is the data dir; the command names a file inside it, so calc.py and in.txt are code
    rec = replay.make("syn1", cmd, inputs, numbers, tol)
    assert Path(rec["path"]) == replay.BULK / "syn1"
    assert ledger(replay.PACKS) == [rec]
    m = json.loads((replay.BULK / "syn1" / "manifest.json").read_text())
    assert m["cmd"] == [sys.executable, "calc.py", "in.txt"]
    assert set(m["files"]) == {"calc.py", "in.txt", "data/in.txt"}
    assert m["tolerance"] == tol and m["numbers"] == numbers
    assert m["platform"]["python"] and isinstance(m["platform"]["packages"], list)
    assert m["code"][0]["sha256"] == m["files"]["calc.py"]["sha256"]
    code, res = replay.run("syn1")
    assert (code, res["result"]) == (0, "MATCH")
    assert replay.line_for(res) == "- replay: syn1 MATCH (2/2 within tolerance)"
    assert [r["result"] for r in ledger(replay.REPLAYS)] == ["MATCH"]


def test_pack_preserves_mtimes(world):
    cmd, inputs, numbers, tol = synth(world)
    src = inputs[0] / "in.txt"
    os.utime(src, ns=(1_000_000_000_123_456_789, 1_000_000_000_123_456_789))
    replay.make("syn1", cmd, inputs, numbers, tol)
    assert (replay.BULK / "syn1" / "data" / "in.txt").stat().st_mtime_ns == 1_000_000_000_123_456_789


def test_random_picks_a_pack(world):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("a", cmd, inputs, numbers, tol)
    replay.make("b", cmd, inputs, numbers, tol)
    seen = {replay.run(None)[1]["name"] for _ in range(30)}
    assert seen <= {"a", "b"} and seen


# ------------------------------------------------------------------- positive controls (101_*)

def test_altered_input_after_make_is_tampered(world):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("syn1", cmd, inputs, numbers, tol)
    f = replay.BULK / "syn1" / "data" / "in.txt"
    st = f.stat()
    f.write_text("8\n")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))   # same mtime, same size: content alone
    code, res = replay.run("syn1")
    assert (code, res["result"]) == (4, "TAMPERED")
    assert res["changes"] == ["data/in.txt: sha256 changed"]
    assert replay.line_for(res) == "- replay: 🔴 syn1 TAMPERED"
    assert ledger(replay.REPLAYS)[-1]["result"] == "TAMPERED"
    assert "numbers" not in res, "a tampered pack is not replayed"


@pytest.mark.parametrize("change", ["mtime", "added", "missing", "manifest"])
def test_other_changes_are_tampered(world, change):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("syn1", cmd, inputs, numbers, tol)
    pack = replay.BULK / "syn1"
    if change == "mtime":
        os.utime(pack / "calc.py", ns=(1, 1))
    elif change == "added":
        (pack / "extra.txt").write_text("x")
    elif change == "missing":
        os.rename(pack / "in.txt", world / "moved_out.txt")
    else:
        m = json.loads((pack / "manifest.json").read_text())
        m["tolerance"]["total"] = 1000
        (pack / "manifest.json").write_text(json.dumps(m))
    assert replay.run("syn1")[1]["result"] == "TAMPERED"


def test_number_outside_tolerance_is_mismatch(world):
    ext = world / "outside.txt"               # read by the command, not part of the pack
    ext.write_text("0")
    cmd, inputs, numbers, tol = synth(world, ext=ext)
    replay.make("syn1", cmd, inputs, numbers, tol)
    ext.write_text("5")
    code, res = replay.run("syn1")
    assert (code, res["result"]) == (4, "MISMATCH")
    assert replay.line_for(res) == "- replay: 🔴 syn1 MISMATCH (total: got 75, packed 70, tol 0)"


def test_number_inside_tolerance_matches(world):
    ext = world / "outside.txt"
    ext.write_text("0")
    cmd, inputs, numbers, tol = synth(world, ext=ext)
    replay.make("syn1", cmd, inputs, numbers, {"total": 5, "count": 0})
    ext.write_text("5")
    assert replay.run("syn1")[1]["result"] == "MATCH"


def test_a_command_that_fails_is_failed_not_match(world):
    ext = world / "outside.txt"
    ext.write_text("0")
    cmd, inputs, numbers, tol = synth(world, ext=ext)
    replay.make("syn1", cmd, inputs, numbers, tol)
    ext.write_text("not a number")             # the command now raises
    code, res = replay.run("syn1")
    assert (code, res["result"]) == (4, "FAILED")
    assert replay.line_for(res).startswith("- replay: 🔴 syn1 FAILED (exit 1)")


# ------------------------------------------------------------------------------- make refuses

def nothing_written(world, name):
    assert not (replay.BULK / name).exists()
    assert ledger(replay.PACKS) == []


def test_a_number_with_no_tolerance_refuses_make(world):
    cmd, inputs, numbers, _tol = synth(world)
    with pytest.raises(replay.Refused, match="NO_TOLERANCE"):
        replay.make("syn1", cmd, inputs, numbers, {"total": 0})
    nothing_written(world, "syn1")


def test_declared_numbers_that_do_not_reproduce_refuse_make(world):
    cmd, inputs, numbers, tol = synth(world)
    numbers["count"]["value"] = 8
    with pytest.raises(replay.Refused, match="do not reproduce"):
        replay.make("syn1", cmd, inputs, numbers, tol)
    nothing_written(world, "syn1")


def test_a_name_is_never_reused(world):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("syn1", cmd, inputs, numbers, tol)
    with pytest.raises(replay.Refused, match="new version is a new name"):
        replay.make("syn1", cmd, inputs, numbers, tol)
    assert len(ledger(replay.PACKS)) == 1


# --------------------------------------------- FENCED_INPUT (BUILD_PROTOCOL §1, ruling of 45b)

def test_credentials_in_an_input_dir_refuse_make_and_without_it_make_succeeds(world):
    """The ruling's positive control: same dir, with and without x_credentials.md."""
    cmd, inputs, numbers, tol = synth(world)
    bad = inputs[0] / "x_credentials.md"
    bad.write_text("never read")
    bad.chmod(0)                               # had make opened it, this would be PermissionError
    with pytest.raises(replay.Refused) as e:
        replay.make("syn1", cmd, inputs, numbers, tol)
    assert str(e.value) == f"FENCED_INPUT {bad}"
    nothing_written(world, "syn1")
    bad.chmod(0o600)
    os.rename(bad, world / "x_credentials.md")  # the caller excludes it
    replay.make("syn1", cmd, inputs, numbers, tol)
    assert replay.run("syn1")[1]["result"] == "MATCH"


@pytest.mark.parametrize("fname", ["SECURITY_credentials.md", "narrow_Credential_kill.md",
                                   "ACCOUNTS.json", "prod.env", ".env"])
def test_fenced_file_names(world, fname):
    cmd, inputs, numbers, tol = synth(world)
    (inputs[0] / fname).write_text("x")
    with pytest.raises(replay.Refused, match="^FENCED_INPUT "):
        replay.make("syn1", cmd, inputs, numbers, tol)
    nothing_written(world, "syn1")


def test_a_fenced_dir_or_file_input_refuses(world):
    cmd, inputs, numbers, tol = synth(world)
    sub = inputs[0] / "credentials"
    sub.mkdir()
    with pytest.raises(replay.Refused, match=re.escape(f"FENCED_INPUT {sub}")):
        replay.make("syn1", cmd, inputs, numbers, tol)
    f = world / "my_accounts.txt"
    f.write_text("x")
    with pytest.raises(replay.Refused, match="FENCED_INPUT"):
        replay.make("syn2", cmd, [f], numbers, tol)
    assert ledger(replay.PACKS) == []


def test_cli_refusal_exits_non_zero_and_names_the_path(world, capsys):
    cmd, inputs, numbers, tol = synth(world)
    (inputs[0] / "x_credentials.md").write_text("x")
    (world / "n.json").write_text(json.dumps(numbers))
    (world / "t.json").write_text(json.dumps(tol))
    code = replay.main(["make", "syn1", "--cmd", cmd, "--inputs", str(inputs[0]),
                        "--numbers", str(world / "n.json"), "--tolerance", str(world / "t.json")])
    assert code == 3
    assert f"FENCED_INPUT {inputs[0] / 'x_credentials.md'}" in capsys.readouterr().err
    nothing_written(world, "syn1")


# ------------------------------------------------------------------------ no bulk, no packs

def test_no_packs_yet(world):
    code, res = replay.run(None)
    assert (code, replay.line_for(res)) == (0, "- replay: no packs yet")
    assert not replay.REPLAYS.exists()


def test_bulk_not_mounted_is_reported_and_writes_no_result(world, monkeypatch):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("syn1", cmd, inputs, numbers, tol)
    monkeypatch.setattr(replay, "MOUNT", world)          # a dir that is not a mount point
    code, res = replay.run(None)
    assert code == 2
    assert replay.line_for(res) == f"- replay: 🔴 replay did not run (bulk store not mounted: {world})"
    assert not replay.REPLAYS.exists()
    assert replay.main(["run", "--random"]) == 2


def test_replay_line_is_one_line(world):
    cmd, inputs, numbers, tol = synth(world)
    replay.make("syn1", cmd, inputs, numbers, tol)
    assert replay.replay_line() == "- replay: syn1 MATCH (2/2 within tolerance)"


# --------------------------------------------------------------------------------- the fences

def test_live_paths_are_the_declared_ones_and_tests_never_use_them(world):
    assert LIVE_PACKS == HERE / "PACKS.jsonl" and LIVE_REPLAYS == HERE / "REPLAYS.jsonl"
    assert LIVE_BULK == Path("__BULK__/replay")
    assert str(world) in str(replay.PACKS) and str(world) in str(replay.BULK)


def test_replay_has_no_escape_hatch():
    src = (HERE / "replay.py").read_text()
    hits = [l for l in src.splitlines()
            if re.search(r"force|override|bypass|skip_check|FACTGATE_OFF|maintenance|environ",
                         l, flags=re.I)]
    assert hits == []
