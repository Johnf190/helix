"""Tests for review.py — reviewer rotation (order 47, item 9).

No test calls a real reviewer: a fake reviewer function is injected with monkeypatch (never a
flag), and every ledger is a temp file, never the live REVIEWS.jsonl.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import review  # noqa: E402

FIXTURES = HERE / "fixtures"
FAKE_REPLY = ("[F1] The bar of 0.72 is asserted, not derived.\n"
              "[F2] B2 can be met by a split artefact.\n"
              "[F3] No positive control ran on the real rig.\n")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger" / "REVIEWS.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(review, "LEDGER", path)
    return path


def seal(tmp_path, name="seal"):
    d = tmp_path / name
    d.mkdir()
    shutil.copy(FIXTURES / "complete" / "PREREG.md", d / "PREREG.md")
    shutil.copy(FIXTURES / "complete" / "FINDINGS.md", d / "FINDINGS.md")
    return d / "PREREG.md"


def lines(ledger):
    return [json.loads(l) for l in ledger.read_text().splitlines()]


@pytest.fixture
def fake(monkeypatch):
    """Both reviewers replaced by a function that records what it was asked."""
    asked = []

    def make(name):
        def call(prompt):
            asked.append((name, prompt))
            return {"text": FAKE_REPLY, "meta": {"fake": True}}
        return call
    monkeypatch.setitem(review.REVIEWERS, "Alice", make("Alice"))
    monkeypatch.setitem(review.REVIEWERS, "Claude-cold", make("Claude-cold"))
    return asked


# ---------------------------------------------------------------- assignment

def test_strict_alternation_starting_with_alice(ledger, tmp_path):
    got = [review.assign(seal(tmp_path, f"s{i}"))[0] for i in range(5)]
    assert got == ["Alice", "Claude-cold", "Alice", "Claude-cold", "Alice"]
    assert [r["reviewer"] for r in lines(ledger)] == got


def test_a_repeat_assign_does_not_reroll_and_is_logged(ledger, tmp_path):
    a, b = seal(tmp_path, "a"), seal(tmp_path, "b")
    assert review.assign(a) == ("Alice", True)
    assert review.assign(b) == ("Claude-cold", True)
    for _ in range(3):
        assert review.assign(a) == ("Alice", False)
    recs = lines(ledger)
    assert len(recs) == 5, "every call appends a line"
    assert [r["repeat"] for r in recs] == [False, False, True, True, True]
    # the repeats name seal a; the next NEW seal still alternates from the last new one
    assert review.assign(seal(tmp_path, "c")) == ("Alice", True)


def test_editing_the_seal_does_not_reroll(ledger, tmp_path):
    """Not a hash of the seal: a comma edit changes nothing about who reviews it."""
    p = seal(tmp_path)
    assert review.assign(p)[0] == "Alice"
    p.write_text(p.read_text().replace("0.72", "0,72", 1))
    assert review.assign(p) == ("Alice", False)


def test_the_same_seal_by_another_spelling_is_the_same_seal(ledger, tmp_path, monkeypatch):
    p = seal(tmp_path)
    assert review.assign(p)[0] == "Alice"
    monkeypatch.chdir(p.parent)
    assert review.assign(Path("PREREG.md")) == ("Alice", False)
    assert review.assign(p.parent / ".." / p.parent.name / "PREREG.md") == ("Alice", False)


def test_the_seal_key_is_relative_to_helix_inside_it():
    inside = review.HELIX / "workspace" / "x" / "PREREG.md"
    assert review.seal_key(inside) == "workspace/x/PREREG.md"
    assert review.seal_key(Path("/tmp/elsewhere/PREREG.md")) == "/tmp/elsewhere/PREREG.md"


def test_a_seal_outside_helix_is_labelled_throwaway(ledger, tmp_path):
    review.assign(seal(tmp_path))
    assert lines(ledger)[0]["throwaway"] is True


def test_the_cli_assign_prints_the_reviewer(ledger, tmp_path, capsys):
    assert review.main(["assign", str(seal(tmp_path))]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Alice"


# --------------------------------------------------------------------- chain

def three_lines(ledger, tmp_path):
    for i in range(3):
        review.assign(seal(tmp_path, f"s{i}"))
    return ledger.read_text().splitlines()


def test_an_intact_chain_has_no_problems(ledger, tmp_path):
    three_lines(ledger, tmp_path)
    assert review.verify_chain(review.read_ledger(ledger)) == []
    recs = lines(ledger)
    assert recs[0]["prev"] == review.GENESIS
    assert recs[1]["prev"] == hashlib.sha256(ledger.read_text().splitlines()[0].encode()).hexdigest()


def test_the_chain_detects_an_edited_line(ledger, tmp_path):
    raw = three_lines(ledger, tmp_path)
    raw[0] = raw[0].replace('"Alice"', '"Claude-cold"')
    ledger.write_text("\n".join(raw) + "\n")
    problems = review.verify_chain(review.read_ledger(ledger))
    assert problems and problems[0][0] == 2, problems


def test_the_chain_detects_a_deleted_line(ledger, tmp_path):
    raw = three_lines(ledger, tmp_path)
    ledger.write_text("\n".join([raw[0], raw[2]]) + "\n")
    problems = review.verify_chain(review.read_ledger(ledger))
    assert problems and problems[0][0] == 2, problems


def test_the_chain_detects_an_inserted_line(ledger, tmp_path):
    raw = three_lines(ledger, tmp_path)
    forged = json.loads(raw[0])
    forged["seal"] = "/tmp/forged/PREREG.md"
    ledger.write_text("\n".join([raw[0], json.dumps(forged), raw[1], raw[2]]) + "\n")
    problems = review.verify_chain(review.read_ledger(ledger))
    assert problems and problems[0][0] == 2, problems


def test_a_broken_chain_refuses_to_assign(ledger, tmp_path):
    raw = three_lines(ledger, tmp_path)
    ledger.write_text("\n".join([raw[0], raw[2]]) + "\n")
    before = ledger.read_bytes()
    assert review.main(["assign", str(seal(tmp_path, "new"))]) == review.EXIT_REFUSED
    assert ledger.read_bytes() == before


# ----------------------------------------------------------------------- run

def test_run_refuses_with_no_assignment(ledger, tmp_path, fake):
    p = seal(tmp_path)
    assert review.main(["run", str(p)]) == review.EXIT_REFUSED
    assert fake == [], "no reviewer is called for an unassigned seal"
    assert not list(p.parent.glob("REVIEW_*"))
    assert not ledger.exists() or ledger.read_text() == ""


def test_a_fake_reviewer_gives_a_raw_file_and_a_review_line(ledger, tmp_path, fake):
    p = seal(tmp_path)
    review.assign(p)
    assert review.main(["run", str(p)]) == review.EXIT_OK
    assert [n for n, _ in fake] == ["Alice"], "the ASSIGNED reviewer, and only it"
    raws = list(p.parent.glob("REVIEW_Alice_*.md"))
    assert len(raws) == 1
    assert FAKE_REPLY in raws[0].read_text()
    rec = lines(ledger)[-1]
    assert rec["kind"] == "review" and rec["reviewer"] == "Alice"
    assert rec["seal"] == review.seal_key(p)
    assert rec["raw"] == review.seal_key(raws[0])
    assert rec["raw_sha256"] == hashlib.sha256(raws[0].read_bytes()).hexdigest()
    assert rec["findings"] == ["F1", "F2", "F3"]
    assert rec["body_sha256"] == review.body_sha256(p.read_bytes())
    assert review.verify_chain(review.read_ledger(ledger)) == []


def test_run_goes_to_claude_cold_when_that_is_the_assignment(ledger, tmp_path, fake):
    review.assign(seal(tmp_path, "first"))
    p = seal(tmp_path, "second")
    assert review.assign(p)[0] == "Claude-cold"
    assert review.main(["run", str(p)]) == review.EXIT_OK
    assert [n for n, _ in fake] == ["Claude-cold"]
    assert list(p.parent.glob("REVIEW_Claude-cold_*.md"))


def test_the_prompt_carries_the_brief_the_seal_and_its_named_files(ledger, tmp_path, fake):
    p = seal(tmp_path)
    p.write_text(p.read_text() + "\nSee `notes.txt` and data/table.csv.\n")
    (p.parent / "notes.txt").write_text("NOTES-CONTENT\n")
    (p.parent / "data").mkdir()
    (p.parent / "data" / "table.csv").write_text("a,b\n1,2\n")
    review.assign(p)
    assert review.main(["run", str(p)]) == review.EXIT_OK
    prompt = fake[0][1]
    assert "find what would make this seal pass when the claim is false" in prompt
    assert "Synthetic probe" in prompt or "synthetic probe" in prompt
    assert "NOTES-CONTENT" in prompt and "a,b\n1,2" in prompt
    assert lines(ledger)[-1]["files_inlined"] == ["data/table.csv", "notes.txt"]


def test_the_header_lists_files_named_but_not_found(ledger, tmp_path, fake):
    """Order 47b: `left out: none` must not read as complete when the seal names a file that is
    not there. Only path-like tokens count: a `/` in them, or a known extension."""
    p = seal(tmp_path)
    p.write_text(p.read_text() + "\nInputs: notes.txt, data/table.csv, gone.py, sub/missing.json,"
                 " /no/such/abs.md, run.sh, 0.72, e.g. the probe.\n")
    (p.parent / "notes.txt").write_text("here\n")
    (p.parent / "data").mkdir()
    (p.parent / "data" / "table.csv").write_text("a,b\n")
    review.assign(p)
    assert review.main(["run", str(p)]) == review.EXIT_OK
    raw = next(p.parent.glob("REVIEW_Alice_*.md")).read_text()
    line = next(l for l in raw.splitlines() if l.startswith("- named but not found:"))
    named = line.split(": ", 1)[1].split(", ")
    for tok in ("gone.py", "sub/missing.json", "/no/such/abs.md", "run.sh"):
        assert tok in named, line
    for tok in ("notes.txt", "data/table.csv", "0.72", "e.g"):
        assert tok not in named, line
    assert raw.index("- files left out:") < raw.index("- named but not found:") < raw.index("---")


def test_the_header_says_none_when_every_named_file_exists(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "notes.txt").write_text("x\n")
    assert review.named_not_found("See notes.txt and the 0.72 bar.\n", d) == []


def test_a_dotdot_path_is_refused_and_never_inlined(tmp_path):
    p = seal(tmp_path)
    (tmp_path / "secret.txt").write_text("OUTSIDE-THE-SEAL\n")
    p.write_text(p.read_text() + "\nCompare ../secret.txt.\n")
    inlined, left_out = review.named_files(p.read_text(), p.parent)
    assert "OUTSIDE-THE-SEAL" not in "".join(t for _, t in inlined)
    assert ("../secret.txt", "outside the seal's directory") in left_out


def test_a_symlink_out_of_the_seal_dir_is_refused(tmp_path):
    p = seal(tmp_path)
    (tmp_path / "secret.txt").write_text("OUTSIDE-THE-SEAL\n")
    (p.parent / "link.txt").symlink_to(tmp_path / "secret.txt")
    p.write_text(p.read_text() + "\nSee link.txt.\n")
    inlined, left_out = review.named_files(p.read_text(), p.parent)
    assert inlined == [] or all(n != "link.txt" for n, _ in inlined)
    assert ("link.txt", "outside the seal's directory") in left_out


def test_the_200kb_file_cap_holds_and_lists_what_was_left_out(tmp_path):
    p = seal(tmp_path)
    for n in ("a.txt", "b.txt", "c.txt"):
        (p.parent / n).write_text(n[0] * 90_000)
    (p.parent / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    p.write_text(p.read_text() + "\nFiles: a.txt b.txt c.txt bin.dat\n")
    inlined, left_out = review.named_files(p.read_text(), p.parent)
    assert [n for n, _ in inlined] == ["a.txt", "b.txt"]
    assert sum(len(t.encode()) for _, t in inlined) <= review.FILES_CAP
    reasons = dict(left_out)
    assert "200" in reasons["c.txt"] or "cap" in reasons["c.txt"]
    assert "not text" in reasons["bin.dat"]


def test_the_alice_prompt_is_capped_at_120000_bytes(ledger, tmp_path, fake):
    p = seal(tmp_path)
    for n in ("a.txt", "b.txt"):
        (p.parent / n).write_text(n[0] * 70_000)
    p.write_text(p.read_text() + "\nFiles: a.txt b.txt\n")
    review.assign(p)
    assert review.main(["run", str(p)]) == review.EXIT_OK
    name, prompt = fake[0]
    assert name == "Alice"
    assert len(prompt.encode("utf-8")) <= review.ALICE_PROMPT_CAP
    assert "a" * 70_000 in prompt and "b" * 70_000 not in prompt
    rec = lines(ledger)[-1]
    assert rec["files_inlined"] == ["a.txt"]
    assert [n for n, _ in rec["files_left_out"]] == ["b.txt"]
    assert "b.txt" in prompt, "what was left out is listed in the prompt"


def test_claude_cold_gets_more_than_alices_cap(ledger, tmp_path, fake):
    review.assign(seal(tmp_path, "first"))
    p = seal(tmp_path, "second")
    for n in ("a.txt", "b.txt"):
        (p.parent / n).write_text(n[0] * 70_000)
    p.write_text(p.read_text() + "\nFiles: a.txt b.txt\n")
    assert review.assign(p)[0] == "Claude-cold"
    assert review.main(["run", str(p)]) == review.EXIT_OK
    assert lines(ledger)[-1]["files_inlined"] == ["a.txt", "b.txt"]


def test_a_raw_review_is_never_overwritten(ledger, tmp_path, fake, monkeypatch):
    p = seal(tmp_path)
    review.assign(p)
    monkeypatch.setattr(review, "utc_stamp", lambda: "20261003T120000Z")
    assert review.main(["run", str(p)]) == review.EXIT_OK
    raw = p.parent / "REVIEW_Alice_20261003T120000Z.md"
    first = raw.read_bytes()
    assert review.main(["run", str(p)]) != review.EXIT_OK
    assert raw.read_bytes() == first


# -------------------------------------------------- the two real call paths

def test_claude_cold_refuses_a_non_cold_init_event(ledger, tmp_path, monkeypatch):
    review.assign(seal(tmp_path, "first"))
    p = seal(tmp_path, "second")
    assert review.assign(p)[0] == "Claude-cold"
    warm = {"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": [],
            "skills": [], "memory_paths": []}
    monkeypatch.setattr(review, "claude_session",
                        lambda system, prompt, workdir: {"init": warm, "text": FAKE_REPLY,
                                                         "cost_usd": 0.01})
    n = len(lines(ledger))
    assert review.main(["run", str(p)]) == review.EXIT_REVIEWER
    assert not list(p.parent.glob("REVIEW_*")), "a warm session's answer is not a cold review"
    assert len(lines(ledger)) == n


def test_claude_cold_accepts_a_cold_init_event(ledger, tmp_path, monkeypatch):
    review.assign(seal(tmp_path, "first"))
    p = seal(tmp_path, "second")
    review.assign(p)
    cold = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
            "skills": [], "memory_paths": [], "model": "claude-opus"}
    monkeypatch.setattr(review, "claude_session",
                        lambda system, prompt, workdir: {"init": cold, "text": FAKE_REPLY,
                                                         "cost_usd": 0.42})
    assert review.main(["run", str(p)]) == review.EXIT_OK
    rec = lines(ledger)[-1]
    assert rec["reviewer"] == "Claude-cold" and rec["meta"]["cost_usd"] == 0.42


def test_the_claude_cold_command_is_coldreads_cold_flag_set(tmp_path):
    argv, stdin_path, cwd, env = review.session_command("SYSTEM", "PROMPT-BODY", tmp_path)
    assert stdin_path.read_text() == "PROMPT-BODY"
    assert "PROMPT-BODY" not in " ".join(argv), "the prompt goes on stdin, never argv"
    for flag in ("--tools", "--setting-sources", "--strict-mcp-config",
                 "--disable-slash-commands", "--safe-mode", "--no-session-persistence"):
        assert flag in argv, flag
    assert argv[argv.index("--max-budget-usd") + 1] == "2.00"
    assert argv[argv.index("--tools") + 1] == ""
    assert "ANTHROPIC_API_KEY" not in env
    assert list(cwd.iterdir()) == []


def test_alice_is_called_as_preflight_calls_her(monkeypatch):
    seen = {}

    class P:
        pid, returncode = 1, 0

        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, **kw)

        def communicate(self, timeout=None):
            return json.dumps({"status": "SUCCESS", "response": FAKE_REPLY}), ""
    monkeypatch.setattr(review.subprocess, "Popen", P)
    out = review.call_alice("THE PROMPT")
    assert seen["cmd"] == ["agy", "-p", "THE PROMPT", "--model", "Gemini 3.1 Pro (High)",
                           "--output-format", "json"]
    assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
    assert out["text"] == FAKE_REPLY


def test_alice_failure_writes_nothing(ledger, tmp_path, monkeypatch):
    p = seal(tmp_path)
    review.assign(p)
    monkeypatch.setattr(review, "_call_alice", lambda prompt, cwd: (None, "", "timed out"))
    n = len(lines(ledger))
    assert review.main(["run", str(p)]) == review.EXIT_REVIEWER
    assert not list(p.parent.glob("REVIEW_*"))
    assert len(lines(ledger)) == n


# -------------------------------------------------------- nightly line, CLI

def test_reviews_line_none_yet(ledger):
    assert review.reviews_line() == "- reviews: none yet"


def test_reviews_line_counts_and_chain(ledger, tmp_path):
    review.assign(seal(tmp_path, "a"))
    review.assign(seal(tmp_path, "b"))
    review.assign(seal(tmp_path, "a2"))
    review.assign(tmp_path / "a" / "PREREG.md")
    assert review.reviews_line() == "- reviews: 3 seals · Alice 2 / Claude-cold 1 · chain OK"


def test_reviews_line_names_the_broken_line(ledger, tmp_path):
    raw = three_lines(ledger, tmp_path)
    ledger.write_text("\n".join([raw[0], raw[2]]) + "\n")
    assert review.reviews_line() == \
        "- reviews: 2 seals · Alice 2 / Claude-cold 0 · 🔴 chain BROKEN at line 2"


def test_reviews_line_stays_green_when_a_throwaway_raw_review_is_gone(ledger, tmp_path, fake):
    """Order 47b: raw reviews of throwaway seals live in /tmp and vanish at reboot. The nightly
    line checks the chain only, so it stays OK (P9 still needs a real seal's raw file)."""
    p = seal(tmp_path)
    review.assign(p)
    rec = review.run(p)
    assert rec["throwaway"] is True
    review.path_of(rec["raw"]).unlink()
    assert review.reviews_line() == "- reviews: 1 seals · Alice 1 / Claude-cold 0 · chain OK"


def test_finding_ids():
    assert review.finding_ids("[F2] x\n[F1] y\n[F2] again\nF9 not bracketed\n") == ["F1", "F2"]


def test_no_cli_flag_fakes_substitutes_or_skips_a_reviewer():
    for verb in ("assign", "run"):
        res = subprocess.run([sys.executable, str(HERE / "review.py"), verb, "--help"],
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        flags = [w for w in res.stdout.split() if w.startswith("--") and w != "--help"]
        assert flags == [], f"{verb} grew a flag: {flags}"
    src = (HERE / "review.py").read_text()
    assert "add_argument(\"--" not in src and "add_argument('--" not in src
