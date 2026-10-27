"""Order 48, Part B: veto.py run. Alice is always a fake injected with monkeypatch; the break
scripts run in the REAL reproduction sandbox (bwrap, no spend)."""

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import veto

HERE = Path(__file__).resolve().parent
SEALGATE = HERE.parent / "sealgate"
FIXTURE = SEALGATE / "fixtures" / "complete"

DETERMINISTIC = "echo checking the band\necho 'BREAK: the band cannot fail'\n"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def temp_everything(tmp_path, monkeypatch):
    """Each test: its own VETOES.jsonl, REVIEWS.jsonl and bulk dir; no real Alice."""
    monkeypatch.setattr(veto, "LEDGER", tmp_path / "VETOES.jsonl")
    monkeypatch.setattr(veto, "REVIEWS_LEDGER", tmp_path / "REVIEWS.jsonl")
    monkeypatch.setattr(veto, "BULK", tmp_path / "bulk")

    def no_real_alice(scratch, prompt):
        raise AssertionError("a test reached the real Alice")
    monkeypatch.setattr(veto, "ALICE", no_real_alice)

    def no_real_judge(prompt, workdir):
        raise AssertionError("a test reached the real judge")
    monkeypatch.setattr(veto, "JUDGE", no_real_judge)
    yield


@pytest.fixture
def seal(tmp_path):
    d = tmp_path / "seal"
    d.mkdir()
    shutil.copy(FIXTURE / "PREREG.md", d / "PREREG.md")
    shutil.copy(FIXTURE / "FINDINGS.md", d / "FINDINGS.md")
    return d / "PREREG.md"


def alice_writes(files, prompts=None):
    """A fake Alice that writes `files` ({name: text}) into her scratch dir."""
    def fake(scratch, prompt):
        if prompts is not None:
            prompts.append((Path(scratch), prompt))
        for name, text in files.items():
            (Path(scratch) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(scratch) / name).write_text(text)
        return {"model": "fake"}
    return fake


def ledger():
    return veto.read_ledger(veto.LEDGER)


# ---------------------------------------------------------------- the prompt

def test_the_brief_is_the_order_text_and_under_the_cap(seal, monkeypatch):
    prompts = []
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "tried\n"}, prompts))
    veto.run(seal)
    (scratch, prompt), = prompts
    for words in ("You are auditing a pre-registration before it is used.",
                  "Your job is to find out whether its checks could report PASS when its claim is "
                  "actually false: a band that cannot fail, a missing control, a circular check, a "
                  "bar below its own null.",
                  "If you find such a weakness, demonstrate it with a deterministic script",
                  "`BREAK: <one line>`", "write `NO_BREAK.md` saying what you checked",
                  "The seal is ../seal/PREREG.md", "evidence/",
                  "does not count"):
        assert words in prompt
    for words in ("Make this seal PASS", "adversarial reviewer"):     # order 50: the old wording
        assert words not in prompt
    assert len(prompt.encode("utf-8")) < 120_000
    assert seal.read_text() not in prompt            # the seal goes in scratch, not the prompt


# ------------------------------------------------- order 50: one retry on a refusal

REFUSAL = "Sorry, I cannot fulfill your request. I am unable to analyze specific, concrete files.\n"


def alice_sequence(*steps, prompts=None):
    """A fake Alice, one step per call: an exception to raise, or ({name: text}, response)."""
    calls = list(steps)

    def fake(scratch, prompt):
        if prompts is not None:
            prompts.append(prompt)
        step = calls.pop(0)
        if isinstance(step, Exception):
            raise step
        files, response = step
        for name, text in files.items():
            (Path(scratch) / name).write_text(text)
        return {"model": "fake", "response_tail": response}
    return fake, calls


def test_a_refusal_then_success_is_one_line_with_two_attempts(seal, monkeypatch):
    prompts = []
    fake, left = alice_sequence(({}, REFUSAL), ({"NO_BREAK.md": "checked\n"}, "done"),
                                prompts=prompts)
    monkeypatch.setattr(veto, "ALICE", fake)
    rec = veto.run(seal)
    assert left == []                                          # called exactly twice
    assert prompts[0] == prompts[1]                            # the retry uses the same brief
    assert rec["outcome"] == "NO_BREAK"                        # the outcome is the second's
    assert [a["refusal"] for a in rec["attempts"]] == [True, False]
    assert rec["attempts"][0]["alice"]["response_tail"] == REFUSAL
    assert len(ledger()) == 1


def test_a_refusal_twice_is_failed_refusal_x2(seal, monkeypatch):
    fake, left = alice_sequence(({}, REFUSAL), ({}, "I can\u2019t help with that."))
    monkeypatch.setattr(veto, "ALICE", fake)
    rec = veto.run(seal)
    assert left == []
    assert rec["outcome"] == "FAILED" and rec["why"] == "refusal x2"
    assert veto.label(rec) == "FAILED (refusal x2)"
    assert [a["refusal"] for a in rec["attempts"]] == [True, True]
    assert len(ledger()) == 1


@pytest.mark.parametrize("exc", [veto.AliceFailed("timed out after 900 s"),
                                 veto.AliceFailed("agy exited 1: boom")])
def test_a_timeout_or_agy_error_is_failed_with_no_retry(seal, monkeypatch, exc):
    fake, left = alice_sequence(exc, ({"NO_BREAK.md": "x\n"}, "done"))
    monkeypatch.setattr(veto, "ALICE", fake)
    rec = veto.run(seal)
    assert len(left) == 1                                      # never called a second time
    assert rec["outcome"] == "FAILED" and rec["why"] == str(exc)
    assert len(rec["attempts"]) == 1 and rec["attempts"][0]["refusal"] is False


def test_no_output_without_a_refusal_phrase_is_failed_with_no_retry(seal, monkeypatch):
    fake, left = alice_sequence(({}, "I looked around."), ({"NO_BREAK.md": "x\n"}, "done"))
    monkeypatch.setattr(veto, "ALICE", fake)
    rec = veto.run(seal)
    assert len(left) == 1
    assert rec["outcome"] == "FAILED" and rec["why"].startswith("no output")
    assert len(rec["attempts"]) == 1


def test_a_refusal_phrase_beside_her_output_is_not_a_refusal(seal, monkeypatch):
    fake, left = alice_sequence(({"NO_BREAK.md": "x\n"}, "I'm unable to break it."),
                                ({}, REFUSAL))
    monkeypatch.setattr(veto, "ALICE", fake)
    rec = veto.run(seal)
    assert len(left) == 1
    assert rec["outcome"] == "NO_BREAK"
    assert [a["refusal"] for a in rec["attempts"]] == [False]


def test_the_refusal_list_is_a_fixed_constant_with_no_flag():
    assert veto.REFUSAL_PATTERNS == ("cannot fulfill", "can't help with", "I'm unable to",
                                     "I cannot assist")
    for text in ("Sorry, I CANNOT FULFILL that.", "I can\u2019t help with this.",
                 "I\u2019m unable to do it.", "i cannot assist."):
        assert veto.is_refusal(text)
    assert not veto.is_refusal("All checks examined; no weakness found.")
    src = Path(veto.__file__).read_text()
    assert "--refusal" not in src
    res = subprocess.run([sys.executable, str(HERE / "veto.py"), "run", "--help"],
                         capture_output=True, text=True)
    assert "refus" not in res.stdout.lower()


def test_the_prompt_cap_is_120000_bytes(monkeypatch):
    assert veto.PROMPT_CAP == 120_000
    monkeypatch.setattr(veto, "BRIEF", "x" * 120_000)
    with pytest.raises(ValueError):
        veto.build_prompt("PREREG.md")


def test_the_time_caps(seal):
    assert veto.ALICE_TIMEOUT == 15 * 60
    assert veto.REPRO_TIMEOUT == 120


# ----------------------------------------------------------- the seal's copy

def test_the_scratch_is_a_fresh_dir_under_bulk_holding_a_copy_of_the_seal(seal, monkeypatch):
    prompts = []
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "tried\n"}, prompts))
    rec = veto.run(seal)
    scratch = Path(rec["scratch"])
    assert scratch.parent.parent == veto.BULK
    assert re.match(r"^\d{8}T\d{6}Z(\.\d+)?$", scratch.parent.name)
    assert prompts[0][0] == scratch / "alice"                  # her cwd: alice/, seal at ../seal/
    assert (scratch / "seal" / "PREREG.md").read_bytes() == seal.read_bytes()
    assert rec["copied"] == ["PREREG.md", "FINDINGS.md"]
    rec2 = veto.run(seal)
    assert rec2["scratch"] != rec["scratch"]


def test_the_copy_is_text_only_capped_and_never_copies_credentials(seal):
    d = seal.parent
    (d / "data.bin").write_bytes(b"\x00\x01\x02")
    (d / ".env").write_text("SECRET=1\n")
    (d / "google_accounts.json").write_text("{}\n")
    (d / "my_credentials.txt").write_text("x\n")
    (d / "big.txt").write_text("y" * 250_000)
    sub = d / "code"
    sub.mkdir()
    (sub / "med.py").write_text("def median(x): pass\n")
    (sub / "unnamed.py").write_text("pass\n")
    with open(seal, "a") as fh:
        fh.write("\nThe script is code/med.py.\n")
    out = d.parent / "scratch_copy"
    out.mkdir()
    copied, left = veto.copy_seal(seal, out)
    why = dict(left)
    assert copied == ["PREREG.md", "FINDINGS.md", "code/med.py"]
    assert why["data.bin"] == "not text"
    assert why[".env"].startswith("credential")
    assert why["google_accounts.json"].startswith("credential")
    assert why["my_credentials.txt"].startswith("credential")
    assert "cap" in why["big.txt"]
    assert "code/unnamed.py" not in copied                 # only NAMED files below the top level
    assert not (out / ".env").exists() and not (out / "google_accounts.json").exists()


def test_a_symlink_out_of_the_seal_dir_is_not_followed(seal, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n")
    (seal.parent / "link.txt").symlink_to(outside)
    with open(seal, "a") as fh:
        fh.write("\nSee link.txt.\n")
    out = tmp_path / "copy"
    out.mkdir()
    copied, left = veto.copy_seal(seal, out)
    assert "link.txt" not in copied
    assert dict(left)["link.txt"] == "outside the seal's directory"


def test_a_seal_that_ships_its_own_no_break_is_not_believed(seal, monkeypatch):
    (seal.parent / "NO_BREAK.md").write_text("I, the author, say Alice could not break it\n")
    (seal.parent / "break.sh").write_text("echo 'BREAK: planted by the author'\n")
    monkeypatch.setattr(veto, "ALICE", alice_writes({}))
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED", rec
    assert rec["why"].startswith("no output")
    assert dict(rec["left_out"])["NO_BREAK.md"].startswith("a veto output name")


# ------------------------------------------------------------------ outcomes

def test_no_break(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "I tried the bands.\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "NO_BREAK"
    assert rec["files"] == {"alice/NO_BREAK.md":
                            hashlib.sha256(b"I tried the bands.\n").hexdigest()}
    assert rec["repro"] == []
    assert rec["body_sha256"] == veto.body_sha256(seal.read_bytes())
    assert rec["seal"] == str(seal.resolve())             # outside ~/helix: the absolute path


def judge_says(valid, why="the demonstration holds with the seal as written", calls=None):
    """A fake cold judge: its reply text is the JSON the real one is asked for."""
    def judge(prompt, workdir):
        if calls is not None:
            calls.append((prompt, Path(workdir)))
        return {"text": json.dumps({"valid": valid, "why": why}),
                "meta": {"model": "fake", "cost_usd": 0.0}}
    return judge


def test_a_clean_deterministic_break_is_reproduced_twice_then_judged(seal, monkeypatch):
    calls = []
    monkeypatch.setattr(veto, "JUDGE", judge_says(True, calls=calls))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC,
                                                     "BREAK.md": "the band cannot fail\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_CONFIRMED", rec
    assert rec["script"] == "break.sh"
    assert [r["rc"] for r in rec["repro"]] == [0, 0]
    assert [r["last_line"] for r in rec["repro"]] == ["BREAK: the band cannot fail"] * 2
    run_dir = Path(rec["scratch"]).parent
    assert [r["dir"] for r in rec["repro"]] == [str(run_dir / "repro1"), str(run_dir / "repro2")]
    assert rec["tamper"] == []
    assert rec["judge"]["valid"] is True and rec["judge"]["why"]
    assert rec["disagreement"] is False and rec["disagreement_kind"] is None
    (prompt, workdir), = calls
    assert workdir == run_dir / "judge"


def test_the_judge_saying_not_valid_is_disputed(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(False, "it only echoes BREAK"))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC,
                                                     "BREAK.md": "x\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_DISPUTED", rec
    assert rec["disagreement"] is True
    assert rec["disagreement_kind"] == "alice_vs_judge"
    assert rec["judge"] == {"valid": False, "why": "it only echoes BREAK",
                            "meta": {"model": "fake", "cost_usd": 0.0}}


@pytest.mark.parametrize("judge, why", [
    (lambda prompt, workdir: {"text": "I think it is valid.", "meta": {}}, "judge: no JSON"),
    (lambda prompt, workdir: {"text": '{"valid": "yes", "why": "x"}', "meta": {}},
     "judge: no JSON"),
    ("raise", "judge: NOT COLD: tools"),
])
def test_a_judge_error_or_no_json_is_failed(seal, monkeypatch, judge, why):
    if judge == "raise":
        def judge(prompt, workdir):
            raise veto.JudgeFailed("NOT COLD: tools: ['Bash']")
    monkeypatch.setattr(veto, "JUDGE", judge)
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC, "BREAK.md": "x"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED", rec
    assert rec["why"].startswith(why), rec["why"]
    assert veto.label(rec).startswith("FAILED (judge: ")


def test_the_verdict_parser():
    assert veto.parse_verdict('{"valid": true, "why": "w"}') == (True, "w")
    assert veto.parse_verdict('Here:\n```json\n{"valid": false, "why": "no"}\n```\n') == \
        (False, "no")
    for bad in ("", "valid", '{"valid": 1, "why": "w"}', '{"why": "w"}',
                '{"valid": true}', '{"valid": true, "why": 3}'):
        assert veto.parse_verdict(bad) is None, bad


def test_break_py_is_run_with_python3(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({
        "break.py": "print('two')\nprint('BREAK: python')\n", "BREAK.md": "x\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_CONFIRMED", rec
    assert rec["script"] == "break.py"


@pytest.mark.parametrize("script, why", [
    ("echo \"BREAK: $(date +%s%N)\"\n", "two runs print different stdout"),
    ("echo 'BREAK: but exits 1'\nexit 1\n", "non-zero exit"),
    ("echo 'BREAK: early'\necho 'and then something else'\n", "last line is not BREAK:"),
    ("echo \"BREAK: $(cat /home/*/helix/README* 2>/dev/null | wc -c)\"\n"
     "ls ~/helix || exit 3\n", "needs ~/helix"),
    ("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3)\" || exit 4\n"
     "echo 'BREAK: online'\n", "needs the network"),
    ("echo 'BREAK: slow'\nsleep 200\n", "over the time cap"),
])
def test_a_break_that_does_not_reproduce(seal, monkeypatch, script, why):
    monkeypatch.setattr(veto, "REPRO_TIMEOUT", 3)
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": script, "BREAK.md": why}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_NOT_REPRODUCED", (why, rec["repro"])
    assert "judge" not in rec or rec["judge"] is None


def test_each_reproduction_runs_on_a_fresh_copy(seal, monkeypatch):
    """A script that changes its own directory must not see the first run's change."""
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    script = ("if [ -f marker ]; then echo 'BREAK: second'; else touch marker; "
              "echo 'BREAK: first'; fi\n")
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": script, "BREAK.md": "x\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_CONFIRMED"
    assert not (Path(rec["scratch"]) / "alice" / "marker").exists()


def test_her_files_are_hashed_before_anything_else_runs(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    script = "echo tampered >> BREAK.md\necho 'BREAK: x'\n"
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": script, "BREAK.md": "orig\n"}))
    rec = veto.run(seal)
    assert rec["files"]["alice/BREAK.md"] == hashlib.sha256(b"orig\n").hexdigest()
    assert sha(Path(rec["scratch"]) / "alice" / "BREAK.md") == rec["files"]["alice/BREAK.md"]


def test_a_break_wins_over_a_no_break_written_beside_it(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC, "BREAK.md": "x",
                                                     "NO_BREAK.md": "y"}))
    assert veto.run(seal)["outcome"] == "BREAK_CONFIRMED"


def test_an_edit_to_the_seal_copy_is_recorded_as_hers(seal, monkeypatch):
    def edits_the_copy(scratch, prompt):
        (Path(scratch) / ".." / "seal" / "PREREG.md").write_text("rewritten\n")
        (Path(scratch) / "NO_BREAK.md").write_text("y\n")
        return {"model": "fake"}
    monkeypatch.setattr(veto, "ALICE", edits_the_copy)
    rec = veto.run(seal)
    assert set(rec["files"]) == {"seal/PREREG.md", "alice/NO_BREAK.md"}


# ------------------------------------------- order 48b, Part A: reproduction hygiene

def her_dir(tmp_path, files):
    d = tmp_path / "her"
    d.mkdir()
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


def test_reproduction_runs_her_files_in_alice_beside_a_pristine_read_only_seal(seal, monkeypatch):
    """Only her script, BREAK.md and evidence/ are copied; the seal comes from its own dir."""
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    script = ("ls -A | sort | tr '\\n' ' '; echo; ls ../seal | sort | tr '\\n' ' '; echo; "
              "cat evidence/note.txt; "
              "echo x >> ../seal/PREREG.md 2>/dev/null && echo SEAL_WRITABLE || echo SEAL_RO; "
              "echo 'BREAK: read only'\n")

    def alice(scratch, prompt):
        her = Path(scratch)
        (her / "break.sh").write_text(script)
        (her / "BREAK.md").write_text("x\n")
        (her / "evidence").mkdir()
        (her / "evidence" / "note.txt").write_text("evidence ok\n")
        (her / "scratchpad.txt").write_text("not copied\n")
        return {"model": "fake"}
    monkeypatch.setattr(veto, "ALICE", alice)
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_CONFIRMED", rec
    run_dir = Path(rec["scratch"]).parent
    out = (run_dir / "repro1.stdout.txt").read_text().splitlines()
    assert out[0].split() == ["BREAK.md", "break.sh", "evidence"]
    assert out[1].split() == ["FINDINGS.md", "PREREG.md"]
    assert out[2:4] == ["evidence ok", "SEAL_RO"]
    assert sha(run_dir / "repro1" / "seal" / "PREREG.md") == sha(seal)


def test_the_pristine_seal_comes_from_the_seal_dir_not_her_scratch(seal, monkeypatch):
    """Her edited copy of the seal never reaches the reproduction."""
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))

    def alice(scratch, prompt):
        (Path(scratch) / ".." / "seal" / "FINDINGS.md").write_text("her version\n")
        (Path(scratch) / "break.sh").write_text("cat ../seal/FINDINGS.md | head -1\n"
                                               "echo 'BREAK: x'\n")
        return {"model": "fake"}
    monkeypatch.setattr(veto, "ALICE", alice)
    rec = veto.run(seal)
    out = (Path(rec["scratch"]).parent / "repro1.stdout.txt").read_text()
    assert "her version" not in out
    assert out.splitlines()[0] == (seal.parent / "FINDINGS.md").read_text().splitlines()[0]


@pytest.mark.parametrize("script, names", [
    ("echo 'import _hashlib' > hashlib.py\necho 'BREAK: x'\n", ["hashlib.py"]),
    ("touch json.py random.py\necho 'BREAK: x'\n", ["json.py", "random.py"]),
    ("echo 'x' > med.py\necho 'BREAK: x'\n", ["med.py"]),
    ("cp ../seal/FINDINGS.md evidence/f.txt\necho 'BREAK: x'\n", ["evidence/f.txt"]),
    ("mkdir -p evidence/hashlib && touch evidence/hashlib/__init__.py\necho 'BREAK: x'\n",
     ["evidence/hashlib/__init__.py"]),
    ("echo 'BREAK: x'\n", None),
])
def test_the_tamper_check(seal, monkeypatch, script, names):
    """The pre-flight's four names (hashlib.py, json.py, random.py, med.py), a renamed copy
    and a stdlib package; a clean script is not invalid."""
    (seal.parent / "med.py").write_text("def median(xs): return sorted(xs)[len(xs) // 2]\n")
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": "mkdir -p evidence\n" + script,
                                                     "BREAK.md": "x\n"}))
    rec = veto.run(seal)
    if names is None:
        assert rec["outcome"] == "BREAK_CONFIRMED", rec
        assert rec["tamper"] == []
        return
    assert rec["outcome"] == "BREAK_INVALID", rec
    assert rec["why"] == "tamper: " + ", ".join(names)
    assert veto.label(rec) == f"BREAK_INVALID (tamper: {', '.join(names)})"
    assert sorted({t["file"] for t in rec["tamper"]}) == names
    assert {t["run"] for t in rec["tamper"]} == {1, 2}
    assert rec.get("judge") is None                       # an invalid break never reaches the judge


def test_tamper_names_files_her_script_created_and_ones_she_shipped(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({
        "break.sh": "echo 'BREAK: x'\n", "BREAK.md": "x\n",
        "evidence/random.py": "print(4)\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "BREAK_INVALID" and rec["why"] == "tamper: evidence/random.py"


def test_the_stdlib_names_exist_on_this_python():
    assert {"hashlib", "json", "random"} <= sys.stdlib_module_names
    assert "med" not in sys.stdlib_module_names


def test_tamper_report_unit(tmp_path):
    seal = tmp_path / "seal"
    alice = tmp_path / "alice"
    seal.mkdir()
    alice.mkdir()
    (seal / "check.py").write_text("print(1)\n")
    (seal / "empty.txt").write_text("")
    (alice / "copy_of_check.txt").write_text("print(1)\n")
    (alice / "hashlib.pyc").write_bytes(b"x")
    (alice / "also_empty.txt").write_text("")           # an empty file is no one's copy
    (alice / "notes.md").write_text("fine\n")
    pristine = veto.tree_hashes(seal)
    got = veto.tamper_report(seal, alice, pristine)
    assert [(t["file"], t["reason"].split(":")[0]) for t in got] == [
        ("copy_of_check.txt", "a copy of a seal file"),
        ("hashlib.pyc", "named like a standard-library module")]


def test_a_changed_seal_file_is_tamper_even_though_the_mount_is_read_only(tmp_path):
    seal = tmp_path / "seal"
    alice = tmp_path / "alice"
    seal.mkdir()
    alice.mkdir()
    (seal / "check.py").write_text("print(1)\n")
    pristine = veto.tree_hashes(seal)
    (seal / "check.py").write_text("print(2)\n")
    (seal / "extra.py").write_text("x\n")
    got = veto.tamper_report(seal, alice, pristine)
    assert [(t["file"], t["reason"]) for t in got] == [
        ("seal/check.py", "a seal file changed during the run"),
        ("seal/extra.py", "a file appeared in the seal during the run")]


ORDER48_SOUND_BREAK_SHA = "92199f7db6fad909fafb1744c98e118c8fab760a7b62984232258f53429a370c"


def test_positive_control_order48s_sound_break_replayed_is_invalid(tmp_path):
    """Her order-48 sound-seal break.sh (VETOES.jsonl line 1, sha pinned; a copy committed under
    fixtures/) replayed through the new harness: BREAK_INVALID, naming hashlib.py and med.py."""
    script = HERE / "fixtures" / "order48_sound_break.sh"
    assert sha(script) == ORDER48_SOUND_BREAK_SHA
    her = her_dir(tmp_path, {"break.sh": script.read_text(),
                             "BREAK.md": (HERE / "fixtures" / "order48_sound_BREAK.md").read_text()})
    result = veto.reproduce(HERE / "calibration" / "sound" / "PREREG.md", her,
                            tmp_path / "run", "break.sh")
    assert [r["rc"] for r in result["runs"]] == [0, 0]
    assert result["reproduced"] is True                  # it IS deterministic: BREAK: is echoed
    names = sorted({t["file"] for t in result["tamper"]})
    assert names == ["hashlib.py", "med.py"], result["tamper"]


def test_positive_control_a_clean_script_that_only_reads_the_seal_is_not_invalid(tmp_path):
    her = her_dir(tmp_path, {"break.sh": "grep -c B1 ../seal/PREREG.md\n"
                                         "python3 -I -B ../seal/check.py | tail -1\n"
                                         "echo 'BREAK: read only'\n", "BREAK.md": "x\n"})
    result = veto.reproduce(HERE / "calibration" / "sound" / "PREREG.md", her,
                            tmp_path / "run", "break.sh")
    assert result["tamper"] == [], result["tamper"]
    assert result["reproduced"] is True
    assert "VERDICT: PASS" in result["stdout"][0]


# ---------------------------------------------- order 48b, Part B: the instrument

def test_the_instrument_line(tmp_path):
    assert veto.instrument_of("x\nInstrument: check.py\ny\n") == ("check.py", None)
    assert veto.instrument_of("no such line\n") == (None, None)
    assert veto.instrument_of("Instrument: a.py\nInstrument: b.py\n")[1] == \
        "more than one Instrument line"
    assert veto.instrument_of("Instrument: run.sh\n")[1] == "the instrument is not a .py"
    assert veto.instrument_of("Instrument: ../x.py\n")[1] == \
        "the instrument is not a relative path inside the seal's directory"
    assert veto.instrument_of("Instrument: /abs/x.py\n")[1] == \
        "the instrument is not a relative path inside the seal's directory"


def test_no_instrument_is_recorded_as_none(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    assert veto.run(seal)["instrument"] == "none"


def test_the_harness_runs_the_declared_instrument_isolated_read_only(seal, monkeypatch):
    (seal.parent / "probe.py").write_text(
        "import sys, os, pathlib\n"
        "print('isolated', sys.flags.isolated, 'nobytecode', sys.dont_write_bytecode)\n"
        "try:\n    pathlib.Path('w.txt').write_text('x')\n    print('wrote')\n"
        "except OSError as e:\n    print('read-only', e.errno)\n"
        "print('VERDICT: PASS')\n")
    with open(seal, "a") as fh:
        fh.write("\nInstrument: probe.py\n")
    calls = []
    monkeypatch.setattr(veto, "JUDGE", judge_says(True, calls=calls))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC, "BREAK.md": "x"}))
    rec = veto.run(seal)
    ins = rec["instrument"]
    assert ins["path"] == "probe.py" and ins["rc"] == 0, ins
    assert ins["argv"] == ["python3", "-I", "-B", "probe.py"]
    assert ins["last_line"] == "VERDICT: PASS"
    out = Path(ins["stdout_file"]).read_text()
    assert ins["stdout_sha256"] == hashlib.sha256(out.encode()).hexdigest()
    assert out.splitlines()[:2] == ["isolated 1 nobytecode True", "read-only 30"]
    prompt = calls[0][0]
    assert "INSTRUMENT OF RECORD" in prompt and "isolated 1 nobytecode True" in prompt


def test_the_instrument_has_no_network_and_a_cap(seal, monkeypatch):
    (seal.parent / "net.py").write_text(
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 3); print('NET', flush=True)\n"
        "except OSError:\n    print('NO_NET', flush=True)\nimport time; time.sleep(30)\n")
    with open(seal, "a") as fh:
        fh.write("\nInstrument: net.py\n")
    monkeypatch.setattr(veto, "INSTRUMENT_TIMEOUT", 3)
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    ins = veto.run(seal)["instrument"]
    assert ins["rc"] is None and ins["last_line"] == "NO_NET", ins


def test_a_declared_instrument_that_is_not_in_the_seal_is_recorded_as_an_error(seal, monkeypatch):
    with open(seal, "a") as fh:
        fh.write("\nInstrument: missing.py\n")
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    ins = veto.run(seal)["instrument"]
    assert ins == {"path": "missing.py", "error": "missing.py is not in the seal's copy"}


def test_the_instrument_timeout_is_120_s():
    assert veto.INSTRUMENT_TIMEOUT == 120


# --------------------------------------------------- order 48b, Part C: the judge

def test_the_judge_prompt_holds_the_order_question_and_every_input(seal, monkeypatch):
    calls = []
    monkeypatch.setattr(veto, "JUDGE", judge_says(True, calls=calls))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC,
                                                     "BREAK.md": "MY ACCOUNT\n"}))
    rec = veto.run(seal)
    prompt, workdir = calls[0]
    assert veto.JUDGE_QUESTION in prompt
    for words in ('Does this show that the seal AS WRITTEN (pinned files unmodified, checker run '
                  'cleanly) would PASS while its claim is FALSE?',
                  'Answer JSON {"valid": true|false, "why": "..."}'):
        assert words in prompt
    assert seal.read_text() in prompt
    assert "MY ACCOUNT" in prompt and DETERMINISTIC in prompt
    assert prompt.count("BREAK: the band cannot fail") >= 2          # both repro stdouts
    assert "instrument: none" in prompt
    assert "TAMPER REPORT" in prompt and "none" in prompt
    assert (workdir / "prompt.txt").read_text() == prompt         # on disk, for the record
    assert len(prompt.encode()) <= 120_000
    assert rec["judge_prompt_bytes"] == len(prompt.encode())


def test_a_judge_prompt_over_the_cap_is_failed(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC,
                                                     "BREAK.md": "y" * 130_000}))
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED"
    assert rec["why"].startswith("judge: the prompt is over the 120,000-byte cap")


def test_the_judge_session_is_cold_with_a_one_dollar_cap(tmp_path):
    argv, stdin_path, cwd, env = veto.judge_command("SYS", "the prompt", tmp_path)
    assert stdin_path.read_text() == "the prompt"
    assert "the prompt" not in argv                              # on stdin, never argv
    i = argv.index("--max-budget-usd")
    assert argv[i + 1] == "1.00"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--safe-mode",
                 "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "ANTHROPIC_API_KEY" not in env


def test_the_judge_cold_flag_set_is_review_pys():
    """Copied from sealgate/review.py (order 47): the same flags and the same refusal."""
    rv = load("veto_test_review_flags", SEALGATE / "review.py")
    import tempfile
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        theirs = rv.session_command("S", "p", a)[0]
        ours = veto.judge_command("S", "p", b)[0]
    strip = lambda argv: [x for x in argv if x not in ("2.00", "1.00")]
    assert strip(theirs) == strip(ours)
    for init in (None, {"tools": ["Bash"]}, {"mcp_servers": [{"name": "x"}]},
                 {"skills": ["s"]}, {"memory_paths": ["/m"]}, {"tools": []}):
        assert veto.check_cold(init) == rv.check_cold(init)


def test_a_warm_init_event_is_failed(seal, monkeypatch):
    monkeypatch.setattr(veto, "judge_session",
                        lambda system, prompt, workdir: {"init": {"tools": ["Bash"]}, "text": ""})
    with pytest.raises(veto.JudgeFailed, match="NOT COLD"):
        veto.call_judge("p", Path(seal).parent / "j")


# -------------------------------------------------------------------- FAILED

def test_alice_failing_is_failed_never_a_veto(seal, monkeypatch):
    def broken(scratch, prompt):
        (Path(scratch) / "break.sh").write_text(DETERMINISTIC)
        raise veto.AliceFailed("agy exited 1: update failed: read-only file system")
    monkeypatch.setattr(veto, "ALICE", broken)
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED"
    assert rec["why"] == "agy exited 1: update failed: read-only file system"
    assert rec["repro"] == []                       # a failed run is not reproduced
    assert "alice/break.sh" in rec["files"]         # but what she wrote is still recorded
    assert veto.label(rec).startswith("FAILED (agy exited 1")


def test_any_exception_from_alice_is_failed(seal, monkeypatch):
    def boom(scratch, prompt):
        raise OSError("proxy did not come up")
    monkeypatch.setattr(veto, "ALICE", boom)
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED" and "proxy did not come up" in rec["why"]


def test_no_output_is_failed(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"notes.txt": "hmm\n"}))
    rec = veto.run(seal)
    assert rec["outcome"] == "FAILED" and rec["why"].startswith("no output")


class FakeSandbox:
    MODEL = "Gemini 3.1 Pro (High)"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_alice(self, scratch, prompt, timeout, cwd=None, read_only=()):
        self.calls.append((scratch, prompt, timeout, cwd, list(read_only)))
        return self.result


@pytest.mark.parametrize("result, why", [
    ((None, "", "", 900.0), "timed out after 900 s"),
    ((1, "", "self-update failed", 2.0), "agy exited 1: self-update failed"),
    ((0, "not json", "", 2.0), "agy output is not the expected JSON"),
    ((0, json.dumps({"status": "ERROR", "response": ""}), "", 2.0), "agy status 'ERROR'"),
])
def test_call_alice_turns_every_agy_failure_into_failed(tmp_path, monkeypatch, result, why):
    fake = FakeSandbox(result)
    monkeypatch.setattr(veto, "_sandbox", fake)
    with pytest.raises(veto.AliceFailed, match=re.escape(why)):
        veto.call_alice(tmp_path / "alice", "p")
    # order 48b: the whole scratch is bound, her cwd is alice/, the seal's copy is read-only
    assert fake.calls == [(tmp_path, "p", 900, tmp_path / "alice", [tmp_path / "seal"])]


def test_call_alice_success(tmp_path, monkeypatch):
    fake = FakeSandbox((0, json.dumps({"status": "SUCCESS", "response": "wrote NO_BREAK.md"}),
                        "", 5.0))
    monkeypatch.setattr(veto, "_sandbox", fake)
    meta = veto.call_alice(tmp_path / "alice", "p")
    assert meta["model"] == "Gemini 3.1 Pro (High)" and meta["rc"] == 0


def test_the_real_alice_is_agy_in_her_sandbox():
    assert veto.call_alice.__module__ == veto.__name__
    src = (HERE / "veto.py").read_text()
    body = src[src.index("def call_alice"):src.index("ALICE = call_alice")]
    assert "sandbox.run_alice(workdir.parent, prompt, ALICE_TIMEOUT," in body


# -------------------------------------------------------------------- ledger

def test_the_ledger_is_hash_chained_and_append_only(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    for _ in range(3):
        veto.run(seal)
    entries = ledger()
    assert [r["seq"] for _, _, r in entries] == [1, 2, 3]
    assert entries[0][2]["prev"] == veto.GENESIS
    assert veto.verify_chain(entries) == []
    lines = veto.LEDGER.read_text().splitlines()
    lines[1] = lines[1].replace('"NO_BREAK"', '"BREAK_NOT_REPRODUCED"')
    veto.LEDGER.write_text("\n".join(lines) + "\n")
    assert veto.verify_chain(ledger())[0][0] == 3


def test_a_broken_chain_refuses_a_run(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    veto.run(seal)
    veto.run(seal)
    lines = veto.LEDGER.read_text().splitlines()
    veto.LEDGER.write_text(lines[1] + "\n")          # first line deleted
    with pytest.raises(RuntimeError, match="chain is broken"):
        veto.run(seal)
    assert len(ledger()) == 1


def test_the_record_carries_what_the_order_lists(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y\n"}))
    rec = ledger_rec = veto.run(seal)
    for key in ("seal", "body_sha256", "outcome", "files", "scratch", "reviewer",
                "disagreement", "seq", "prev", "ts"):
        assert key in rec
    assert ledger()[-1][2] == ledger_rec


# ------------------------------------------------------------- disagreement

def review_module(tmp_path, text):
    rv = load("veto_test_review", SEALGATE / "review.py")
    rv.LEDGER = veto.REVIEWS_LEDGER
    for name in rv.REVIEWER_NAMES:
        rv.REVIEWERS[name] = lambda prompt, t=text: {"text": t, "meta": {"model": "fake"}}
    return rv


def assigned(seal, reviewer, text):
    """An assign line naming `reviewer` and that reviewer's review, in the temp REVIEWS.jsonl."""
    rv = review_module(seal.parent, text)
    rv.append([{"kind": "assign", "seal": rv.seal_key(seal), "reviewer": reviewer,
                "repeat": False, "throwaway": True}])
    rv.run(seal)


def stamp(seal, reviewer):
    sg = load("veto_test_sealgate", SEALGATE / "sealgate.py")
    block = sg.build_stamp(reviewer, "FINDINGS.md", sha(seal.parent / "FINDINGS.md"), "2026-10-03")
    with open(seal, "a") as fh:
        fh.write("\n" + block + "\n")


REVIEWER_KIND = "alice_vs_reviewer"
JUDGE_KIND = "alice_vs_judge"


@pytest.mark.parametrize("reviewer, review, outcome_files, valid, want", [
    # order 48's reviewer disagreement, now on BREAK_CONFIRMED
    ("Claude-cold", "[F4] Something nobody dispositioned.\n", {"break.sh": DETERMINISTIC}, True,
     REVIEWER_KIND),
    ("Claude-cold", "[F2] The split.\n", {"break.sh": DETERMINISTIC}, True, REVIEWER_KIND),
    ("Claude-cold", "[F1] The bar.\n", {"break.sh": DETERMINISTIC}, True, None),   # ADDRESSED
    ("Claude-cold", "[F3] No positive.\n", {"break.sh": DETERMINISTIC}, True, None),  # ACCEPTED
    ("Alice", "[F4] Something.\n", {"break.sh": DETERMINISTIC}, True, None),        # not Claude
    ("Claude-cold", "[F4] Something.\n", {"NO_BREAK.md": "y"}, True, None),          # no break
    ("Claude-cold", "[F4] x\n", {"break.sh": "echo 'BREAK: '$(date +%N)\n"}, True, None),
    ("Claude-cold", "[F4] x\n", {"break.sh": "touch json.py\necho 'BREAK: x'\n"}, True, None),
    # the judge disputes: Alice against the judge, whoever the reviewer was
    ("Claude-cold", "[F4] x\n", {"break.sh": DETERMINISTIC}, False, JUDGE_KIND),
    ("Alice", "[F1] The bar.\n", {"break.sh": DETERMINISTIC}, False, JUDGE_KIND),
])
def test_disagreement(seal, monkeypatch, reviewer, review, outcome_files, valid, want):
    assigned(seal, reviewer, review)
    stamp(seal, reviewer)
    monkeypatch.setattr(veto, "JUDGE", judge_says(valid))
    monkeypatch.setattr(veto, "ALICE", alice_writes(outcome_files))
    rec = veto.run(seal)
    assert rec["reviewer"] == reviewer
    assert rec["disagreement"] is (want is not None), rec
    assert rec["disagreement_kind"] == want, rec


def test_disagreement_with_no_stamp_means_nothing_was_accepted(seal, monkeypatch):
    assigned(seal, "Claude-cold", "[F1] The bar.\n")
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC}))
    rec = veto.run(seal)
    assert rec["reviewer_findings"] == ["F1"]
    assert rec["reviewer_findings_accepted"] == []
    assert rec["disagreement"] is True and rec["disagreement_kind"] == REVIEWER_KIND


def test_no_assigned_reviewer_is_no_disagreement(seal, monkeypatch):
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC}))
    rec = veto.run(seal)
    assert rec["reviewer"] is None and rec["disagreement"] is False


# --------------------------------------------------------------- nightly line

def test_veto_line_none_yet():
    assert veto.veto_line() == "- veto: none yet"


def test_veto_line_counts(seal, monkeypatch):
    assigned(seal, "Claude-cold", "[F9] x\n")
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y"}))
    veto.run(seal)
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": DETERMINISTIC}))
    monkeypatch.setattr(veto, "JUDGE", judge_says(True))
    veto.run(seal)
    monkeypatch.setattr(veto, "JUDGE", judge_says(False))
    veto.run(seal)
    veto.run(seal)
    monkeypatch.setattr(veto, "ALICE", alice_writes({"break.sh": "touch json.py\necho 'BREAK: x'\n"}))
    veto.run(seal)
    assert veto.veto_line() == \
        "- veto: 5 runs · 1 confirmed · 2 disputed · 1 invalid · chain OK"


def test_veto_line_counts_legacy_break_reproduced_on_its_own(seal, monkeypatch):
    """Order 48's two calibration runs are BREAK_REPRODUCED lines: still read, counted apart."""
    veto.append([{"kind": "veto", "seal": "a", "outcome": "BREAK_REPRODUCED"},
                 {"kind": "veto", "seal": "b", "outcome": "BREAK_REPRODUCED"}])
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y"}))
    veto.run(seal)
    assert veto.veto_line() == \
        "- veto: 3 runs · 0 confirmed · 0 disputed · 0 invalid · 2 legacy · chain OK"


def test_veto_line_broken_chain(seal, monkeypatch):
    monkeypatch.setattr(veto, "ALICE", alice_writes({"NO_BREAK.md": "y"}))
    veto.run(seal)
    veto.run(seal)
    lines = veto.LEDGER.read_text().splitlines()
    veto.LEDGER.write_text(lines[1] + "\n")
    before = veto.LEDGER.read_bytes()
    assert veto.veto_line().startswith("- veto: 1 runs · 0 confirmed · 0 disputed · 0 invalid · "
                                       "🔴 chain BROKEN at line 1")
    assert veto.LEDGER.read_bytes() == before


# ----------------------------------------------------------------------- CLI

def test_run_has_no_flags():
    res = subprocess.run([sys.executable, str(HERE / "veto.py"), "run", "--help"],
                         capture_output=True, text=True)
    opts = re.findall(r"^\s+(-{1,2}[\w-]+)", res.stdout, flags=re.M)
    assert opts == ["-h"], res.stdout


def test_cli_on_a_missing_seal_is_usage():
    res = subprocess.run([sys.executable, str(HERE / "veto.py"), "run", "/nonexistent/P.md"],
                         capture_output=True, text=True)
    assert res.returncode == 2


def test_no_escape_hatch_in_veto():
    src = (HERE / "veto.py").read_text()
    hits = [l for l in src.splitlines() if re.search(r"force|override|bypass|skip_check", l, re.I)]
    assert hits == []
    assert not re.search(r"add_argument\(\s*['\"]--", src)
