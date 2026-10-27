"""Tests for factgate.py — detection for the fact base.

`~/helix/memory/facts/` has no git and no write path: nearly every file is written by
Odin through the SDK's Write/Edit/Bash tools, so a fact can be reworded, back-dated or
deleted and nothing notices. This records what is there and reports what moved.

Every test works on a directory it made itself, or on a copy. **No test touches
`~/helix/memory/**`** — one of them proves it, by hashing the real directory before and
after running every verb against a copy.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FACTGATE = HERE / "factgate.py"
REAL_FACTS = Path.home() / "helix/memory/facts"
LEDGER = "FACTS_LEDGER.jsonl"

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_CHECK = 0, 2, 3, 4


def run(*args, state=None):
    """--state-dir is a global option, so it goes before the verb, as argparse wants."""
    cmd = [sys.executable, str(FACTGATE)]
    if state is not None:
        cmd += ["--state-dir", str(state)]
    cmd += [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(d):
    """Every regular file's name, bytes and mtime — enough to prove nothing was touched."""
    return sorted((p.name, sha(p), p.stat().st_mtime_ns)
                  for p in Path(d).iterdir() if p.is_file())


@pytest.fixture
def facts(tmp_path):
    d = tmp_path / "facts"
    d.mkdir()
    (d / "001_first.md").write_text("# first\n\nA fact about the first thing.\n")
    (d / "002_second.md").write_text("# second\n\nA fact about the second thing.\n")
    (d / "003_third.md").write_text("# third\n\nA fact about the third thing.\n")
    (d / "notes.txt").write_text("not markdown, still a file\n")
    return d


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "state"
    s.mkdir()
    return s


@pytest.fixture
def based(facts, state):
    assert run("baseline", facts, state=state).returncode == EXIT_OK
    return facts


def ledger_lines(state):
    return [json.loads(l) for l in (Path(state) / LEDGER).read_text().splitlines()]


# --------------------------------------------------------------------- baseline

def test_baseline_records_every_regular_file(facts, state):
    res = run("baseline", facts, state=state)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    lines = ledger_lines(state)
    assert sorted(l["file"] for l in lines) == \
        ["001_first.md", "002_second.md", "003_third.md", "notes.txt"], \
        "every regular file, not only .md"
    assert all(l["kind"] == "baseline" for l in lines)
    for l in lines:
        assert l["sha256"] == sha(facts / l["file"])
        assert l["size"] == (facts / l["file"]).stat().st_size


def test_baseline_refuses_when_a_ledger_already_exists(based, state):
    res = run("baseline", based, state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "already" in res.stderr.lower()
    assert len(ledger_lines(state)) == 4, "it must not append a second baseline"


def test_baseline_hashes_bytes_not_text(facts, state):
    """A fact file with invalid UTF-8 must still be recorded, by its bytes."""
    (facts / "004_bytes.md").write_bytes(b"# odd\n\xff\xfe not valid utf-8\n")
    assert run("baseline", facts, state=state).returncode == EXIT_OK
    line = [l for l in ledger_lines(state) if l["file"] == "004_bytes.md"][0]
    assert line["sha256"] == hashlib.sha256((facts / "004_bytes.md").read_bytes()).hexdigest()


def test_a_subdirectory_is_reported_not_recursed(based, state):
    sub = based / "archive"
    sub.mkdir()
    (sub / "old.md").write_text("an archived fact\n")
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "UNTRACKED DIR: archive" in res.stderr, res.stderr
    assert "old.md" not in res.stderr, "it must not recurse"


# ------------------------------------------------------------------------ check

def test_check_is_clean_straight_after_baseline(based, state):
    res = run("check", based, state=state)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "4 recorded" in res.stdout, res.stdout


def test_check_without_a_ledger_says_so_and_creates_nothing(facts, state):
    res = run("check", facts, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "NO LEDGER" in res.stderr, res.stderr
    assert not (state / LEDGER).exists(), "check must never create a ledger"


def test_a_changed_fact_is_caught(based, state):
    was = sha(based / "002_second.md")
    (based / "002_second.md").write_text("# second\n\nA fact, quietly reworded.\n")
    now = sha(based / "002_second.md")
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert f"CHANGED OFF-LEDGER: 002_second.md {was[:12]} -> {now[:12]}" in res.stderr, res.stderr


def test_a_new_fact_is_caught(based, state):
    (based / "005_new.md").write_text("# new\n\nWritten by something, some time.\n")
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "NEW OFF-LEDGER: 005_new.md" in res.stderr, res.stderr


def test_a_deleted_fact_is_caught(based, state):
    (based / "003_third.md").unlink()
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "DELETED OFF-LEDGER: 003_third.md" in res.stderr, res.stderr


def test_touching_a_file_is_not_a_change(based, state):
    """The hash is over bytes. A re-save that changes nothing is nothing."""
    p = based / "001_first.md"
    before = p.stat().st_mtime_ns
    os.utime(p, (p.stat().st_atime + 3600, p.stat().st_mtime + 3600))
    assert p.stat().st_mtime_ns != before
    res = run("check", based, state=state)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_a_crlf_conversion_is_a_change(based, state):
    p = based / "001_first.md"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "CHANGED OFF-LEDGER: 001_first.md" in res.stderr, res.stderr


def test_a_tampered_ledger_line_stops_everything(based, state):
    path = Path(state) / LEDGER
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["sha256"] = hashlib.sha256(b"a lie").hexdigest()
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    (based / "009_also_new.md").write_text("a new file the tamper would hide\n")
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "LEDGER TAMPERED" in res.stderr, res.stderr
    assert "NEW OFF-LEDGER" not in res.stderr, \
        "a ledger that cannot be trusted is not evidence about the directory"


# ----------------------------------------------------------------------- record

def test_record_acknowledges_a_change_and_check_goes_clean(based, state):
    (based / "002_second.md").write_text("# second\n\nRevised, deliberately.\n")
    res = run("record", based, "002_second.md", "--reason", "corrected the date", state=state)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert run("check", based, state=state).returncode == EXIT_OK
    last = ledger_lines(state)[-1]
    assert last["kind"] == "record" and last["file"] == "002_second.md"
    assert last["reason"] == "corrected the date"
    assert last["sha256"] == sha(based / "002_second.md")


def test_record_acknowledges_a_new_file(based, state):
    (based / "005_new.md").write_text("# new\n\nA fact added on purpose.\nCategory: helix\n")
    assert run("record", based, "005_new.md", "--reason", "new fact from the council",
               state=state).returncode == EXIT_OK
    assert run("check", based, state=state).returncode == EXIT_OK


def test_record_refuses_a_file_that_has_not_changed(based, state):
    res = run("record", based, "001_first.md", "--reason", "nothing happened", state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "unchanged" in res.stderr.lower(), res.stderr


def test_record_refuses_an_empty_reason(based, state):
    (based / "002_second.md").write_text("# second\n\nchanged\n")
    res = run("record", based, "002_second.md", "--reason", "   ", state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr


def test_record_refuses_a_file_that_is_not_there(based, state):
    res = run("record", based, "nope.md", "--reason", "a reason", state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr


def test_record_refuses_while_the_chain_is_broken(based, state):
    path = Path(state) / LEDGER
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["size"] = 99999
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    (based / "002_second.md").write_text("# second\n\nchanged\n")
    res = run("record", based, "002_second.md", "--reason", "a real reason", state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "TAMPERED" in res.stderr, res.stderr


def test_record_delete_acknowledges_a_deletion(based, state):
    (based / "003_third.md").unlink()
    res = run("record-delete", based, "003_third.md", "--reason", "superseded by 004",
              state=state)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert run("check", based, state=state).returncode == EXIT_OK
    last = ledger_lines(state)[-1]
    assert last["kind"] == "delete" and last["file"] == "003_third.md"
    assert last["reason"] == "superseded by 004"


def test_record_delete_refuses_while_the_file_is_still_there(based, state):
    res = run("record-delete", based, "003_third.md", "--reason", "a reason", state=state)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "still" in res.stderr.lower(), res.stderr


def test_a_recorded_deletion_then_reappearing_is_new(based, state):
    (based / "003_third.md").unlink()
    assert run("record-delete", based, "003_third.md", "--reason", "gone", state=state).returncode == EXIT_OK
    (based / "003_third.md").write_text("# third\n\nback again, unannounced\n")
    res = run("check", based, state=state)
    assert res.returncode == EXIT_CHECK, res.stdout + res.stderr
    assert "NEW OFF-LEDGER: 003_third.md" in res.stderr, res.stderr


def test_there_is_no_bulk_record_verb():
    """A one-command way to bless every change is a rubber stamp."""
    help_text = subprocess.run([sys.executable, str(FACTGATE), "--help"],
                               capture_output=True, text=True).stdout
    for forbidden in ("record-all", "record_all", "--all", "--glob", "accept-all"):
        assert forbidden not in help_text, forbidden
    assert "record" in help_text and "record-delete" in help_text


# ------------------------------------------------------------------- read-only

def test_no_verb_writes_to_the_facts_directory(facts, state):
    """Every verb, including the refusals, against one directory."""
    before = fingerprint(facts)
    run("baseline", facts, state=state)
    run("check", facts, state=state)
    run("record", facts, "001_first.md", "--reason", "unchanged so refused", state=state)
    run("record-delete", facts, "002_second.md", "--reason", "still there so refused",
        state=state)
    run("record", facts, "nope.md", "--reason", "missing so refused", state=state)
    assert fingerprint(facts) == before, \
        "no verb may write, rename or even touch a file in the facts directory"


def test_the_real_fact_base_is_never_touched(tmp_path, state):
    """The one test that reaches the real directory — to copy it and prove it did not move."""
    before = sorted((p.name, sha(p)) for p in REAL_FACTS.iterdir() if p.is_file())
    copy = tmp_path / "facts_copy"
    shutil.copytree(REAL_FACTS, copy)
    assert run("baseline", copy, state=state).returncode == EXIT_OK
    assert run("check", copy, state=state).returncode == EXIT_OK
    after = sorted((p.name, sha(p)) for p in REAL_FACTS.iterdir() if p.is_file())
    assert after == before, "the real fact base moved during this test"


def test_factgate_has_no_escape_hatch():
    src = FACTGATE.read_text()
    hits = [(i, l) for i, l in enumerate(src.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], f"escape hatch introduced: {hits}"


# ==========================================================================
# ORDER 22b — INTERIM by default. New facts are labelled INTERIM until a witness the
# author did not write releases them. Legacy = a ledger cutoff whose prefix hash the
# vault anchored. Every release test runs a COPY of factgate.py inside a temp `helix/`
# tree, because the witnesses are looked up relative to factgate's own location.
# ==========================================================================

def prefix_sha(state, n):
    raw = (Path(state) / LEDGER).read_bytes()
    return hashlib.sha256(b"".join(raw.splitlines(keepends=True)[:n])).hexdigest()


def legacy_json(state):
    return json.loads((Path(state) / "LEGACY_BASELINE.json").read_text())


@pytest.fixture
def cut(facts, state):
    """Lines 1-4 baselined; then 004 new and 002 edited, both recorded after the cutoff."""
    assert run("baseline", facts, state=state).returncode == EXIT_OK
    h = prefix_sha(state, 4)
    (facts / "004_new.md").write_text("# new\n\nA decisive breakthrough.\nCategory: helix\n")
    assert run("record", facts, "004_new.md", "--reason", "new one", state=state).returncode == 0
    (facts / "002_second.md").write_text("# second\n\nReworded after the cutoff.\n")
    assert run("record", facts, "002_second.md", "--reason", "edit", state=state).returncode == 0
    return {"facts": facts, "state": state, "h": h}


def legacy(cut, n=4, h=None, seq="1"):
    return run("baseline-legacy", cut["facts"], "--cutoff-line", str(n), "--cutoff-sha256",
               h or cut["h"], "--vault-seq", seq, state=cut["state"])


def interim(facts, state, *extra):
    return run("interim", facts, *extra, state=state)


def summary(res):
    return [l for l in res.stdout.splitlines() if l.startswith("interim: ")][-1]


def test_baseline_legacy_records_only_facts_last_seen_at_or_before_the_cutoff(cut):
    res = legacy(cut)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    j = legacy_json(cut["state"])
    assert sorted(j["legacy"]) == ["001_first.md", "003_third.md", "notes.txt"]
    assert j["interim_at_baseline"] == ["002_second.md", "004_new.md"]
    assert j["cutoff_line"] == 4 and j["cutoff_sha256"] == cut["h"] and j["vault_seq"] == 1


def test_baseline_legacy_refuses_a_cutoff_hash_the_ledger_does_not_have(cut):
    res = legacy(cut, h="0" * 64)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "prefix" in res.stderr.lower()
    assert not (cut["state"] / "LEGACY_BASELINE.json").exists()


def test_baseline_legacy_refuses_a_cutoff_past_the_end_of_the_ledger(cut):
    res = legacy(cut, n=99)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert not (cut["state"] / "LEGACY_BASELINE.json").exists()


def test_baseline_legacy_is_written_once(cut):
    assert legacy(cut).returncode == EXIT_OK
    before = sha(cut["state"] / "LEGACY_BASELINE.json")
    res = legacy(cut)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert sha(cut["state"] / "LEGACY_BASELINE.json") == before


def test_baseline_legacy_refuses_a_broken_chain(cut):
    p = cut["state"] / LEDGER
    lines = p.read_text().splitlines()
    rec = json.loads(lines[5])
    rec["reason"] = "rewritten"
    lines[5] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n")
    assert legacy(cut).returncode == EXIT_REFUSED
    assert not (cut["state"] / "LEGACY_BASELINE.json").exists()


def test_interim_lists_exactly_the_post_cutoff_facts_with_their_verdict_words(cut):
    legacy(cut)
    res = interim(cut["facts"], cut["state"])
    assert res.returncode == EXIT_OK, res.stderr
    assert summary(res) == "interim: 2 facts, 1 with verdict words: 004_new.md"
    assert "INTERIM 002_second.md" in res.stdout and "INTERIM 004_new.md" in res.stdout
    assert "001_first.md" not in res.stdout and "003_third.md" not in res.stdout
    assert "L3 decisive" in res.stdout and "L3 breakthrough" in res.stdout


def test_verdict_words_whole_word_any_case_quotes_count_and_green_only_on_line_one(cut):
    legacy(cut)
    (cut["facts"] / "005_x.md").write_text(
        "# 🟢 a `PROVEN` claim\n\nIt \"Passed\". Compass, passing, passes, unsettled.\n"
        "🟢 later green\nvalidated; Novel. established-ish\n")
    res = interim(cut["facts"], cut["state"], "--json")
    rec = next(r for r in json.loads(res.stdout)["interim"] if r["file"] == "005_x.md")
    assert rec["verdict"] == [[1, "🟢"], [1, "PROVEN"], [3, "Passed"], [5, "validated"],
                              [5, "Novel"], [5, "established"]], rec


def test_every_verdict_word_in_the_constant_is_caught(tmp_path):
    sys.path.insert(0, str(HERE))
    import factgate
    for w in ("proven", "proves", "decisive", "passed", "pass", "confirmed", "settled",
              "definitive", "conclusive", "breakthrough", "solved", "novel", "established",
              "validated"):
        assert w in factgate.VERDICT_WORDS
        assert factgate.verdict_words(f"x\nit is {w.upper()}.\n") == [(2, w.upper())], w


def test_an_edited_legacy_fact_becomes_interim_recorded_or_not(cut):
    legacy(cut)
    (cut["facts"] / "001_first.md").write_text("# first\n\nnow it is proven.\n")
    assert "INTERIM 001_first.md" in interim(cut["facts"], cut["state"]).stdout
    run("record", cut["facts"], "001_first.md", "--reason", "edit", state=cut["state"])
    assert "INTERIM 001_first.md" in interim(cut["facts"], cut["state"]).stdout


def test_with_no_legacy_baseline_every_fact_is_interim_and_it_still_exits_0(cut):
    res = interim(cut["facts"], cut["state"])
    assert res.returncode == EXIT_OK
    assert summary(res).startswith("interim: 5 facts, 1 with verdict words: 004_new.md")
    assert "no legacy baseline" in summary(res)


def test_a_tampered_legacy_file_is_not_trusted(cut):
    legacy(cut)
    p = cut["state"] / "LEGACY_BASELINE.json"
    j = json.loads(p.read_text())
    j["legacy"]["004_new.md"] = sha(cut["facts"] / "004_new.md")
    p.write_text(json.dumps(j))
    res = interim(cut["facts"], cut["state"])
    assert "INTERIM 004_new.md" in res.stdout and "INTERIM 001_first.md" in res.stdout
    assert res.returncode == EXIT_OK


def test_a_rewritten_ledger_prefix_makes_everything_interim(cut):
    legacy(cut)
    p = cut["state"] / LEDGER
    p.write_text(p.read_text().replace("baseline", "baselinX", 1))
    res = interim(cut["facts"], cut["state"])
    assert res.returncode == EXIT_OK
    assert summary(res).startswith("interim: 5 facts")


def test_interim_names_at_most_five_then_an_ellipsis(facts, state):
    assert run("baseline", facts, state=state).returncode == 0
    for i in range(7):
        (facts / f"10{i}_x.md").write_text("passed\n")
    line = summary(interim(facts, state))
    assert line.startswith("interim: 11 facts, 7 with verdict words: 100_x.md, 101_x.md, "
                           "102_x.md, 103_x.md, 104_x.md …"), line


def test_interim_writes_nothing(cut):
    legacy(cut)
    before = (fingerprint(cut["facts"]), fingerprint(cut["state"]))
    interim(cut["facts"], cut["state"])
    interim(cut["facts"], cut["state"], "--json")
    assert (fingerprint(cut["facts"]), fingerprint(cut["state"])) == before


def test_interim_is_importable_for_the_loader(cut):
    legacy(cut)
    sys.path.insert(0, str(HERE))
    import factgate
    got = {r["file"]: r["verdict"] for r in factgate.interim_facts(cut["facts"], cut["state"])[0]}
    assert set(got) == {"002_second.md", "004_new.md"}
    assert got["004_new.md"] == [(3, "decisive"), (3, "breakthrough")]


# ------------------------------------------------------------------ release

STUB_SEALGATE = '''import sys, pathlib
def normalise_name(n): return " ".join(n.replace("*", "").split()).strip(".,;:").lower()
def split_authors(l): return [normalise_name(p) for p in l.split(",") if normalise_name(p)]
if __name__ == "__main__" and sys.argv[1:2] == ["verify"]:
    sys.exit(0 if (pathlib.Path(sys.argv[2]).parent / "VERIFY_OK").exists() else 4)
'''
STUB_REGATE = '''import sys, pathlib
if __name__ == "__main__" and sys.argv[1:2] == ["check"]:
    sys.exit(0 if (pathlib.Path(sys.argv[2]).parent / "CHECK_OK").exists() else 4)
'''


def copy_categories(root):
    """ORDER 76: F5 loads categories.py from the temp helix/, so it goes in with its list."""
    (root / "workspace/categories").mkdir(parents=True)
    for name in ("categories.py", "CATEGORIES.txt"):
        shutil.copy(HERE.parent / "categories" / name, root / "workspace/categories" / name)


@pytest.fixture
def tree(tmp_path):
    """A temp helix/: factgate copied in, the real sealgate for name normalising, a stub
    regate, a reports folder, and a fact base with one legacy fact and one new one."""
    root = tmp_path / "helix"
    fg = root / "workspace/factgate"
    fg.mkdir(parents=True)
    shutil.copy(FACTGATE, fg / "factgate.py")
    (root / "workspace/sealgate").mkdir(parents=True)
    shutil.copy(HERE.parent / "sealgate/sealgate.py", root / "workspace/sealgate/sealgate.py")
    (root / "workspace/regate").mkdir(parents=True)
    (root / "workspace/regate/regate.py").write_text(STUB_REGATE)
    copy_categories(root)
    (root / "workspace/seam/registry").mkdir(parents=True)
    (root / "workspace/seam/registry/registry.json").write_text("{}\n")
    (root / "build_orders/reports").mkdir(parents=True)
    facts = root / "memory/facts"
    facts.mkdir(parents=True)
    (facts / "001_old.md").write_text("# old\n\nlegacy.\n")
    t = {"root": root, "fg": fg / "factgate.py", "facts": facts, "state": fg / "state",
         "reports": root / "build_orders/reports"}
    assert frun(t, "baseline", facts).returncode == 0
    h = prefix_sha(t["state"], 1)
    (facts / "150_new.md").write_text("# ORDER 21 live: decisive breakthrough, proven, PASSED\n"
                                   "Category: helix\n")
    assert frun(t, "record", facts, "150_new.md", "--reason", "new").returncode == 0
    assert frun(t, "baseline-legacy", facts, "--cutoff-line", "1", "--cutoff-sha256", h,
                "--vault-seq", "1").returncode == 0
    return t


def frun(t, *args):
    return subprocess.run([sys.executable, str(t["fg"])] + [str(a) for a in args],
                          capture_output=True, text=True, cwd=str(t["root"]))


def listed(t):
    return [l.split()[1] for l in frun(t, "interim", t["facts"]).stdout.splitlines()
            if l.startswith("INTERIM ")]


def release(t, witness, name="150_new.md", reason="witnessed by the build report"):
    return frun(t, "release", t["facts"], name, "--witness", witness, "--reason", reason)


def report(t, name="ORDER_21_report.md", last="DONE"):
    (t["reports"] / name).write_text(f"# ORDER 21 report\n\nbuilt it.\n\n{last}\n")
    return f"report:build_orders/reports/{name}"


def ledger_sha(t):
    return sha(t["state"] / LEDGER)


def test_release_with_a_valid_report_witness_unlists_and_an_edit_relists(tree):
    assert listed(tree) == ["150_new.md"]
    res = release(tree, report(tree))
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert listed(tree) == []
    with open(tree["facts"] / "150_new.md", "a") as fh:
        fh.write("one more line\n")
    assert listed(tree) == ["150_new.md"]


def test_the_release_event_is_chained_and_records_the_fact_and_witness_hashes(tree):
    w = report(tree)
    assert release(tree, w).returncode == 0
    rec = json.loads((tree["state"] / LEDGER).read_text().splitlines()[-1])
    assert rec["kind"] == "release" and rec["file"] == "150_new.md"
    assert rec["sha256"] == sha(tree["facts"] / "150_new.md")
    assert rec["witness"] == w
    assert rec["witness_sha256"] == sha(tree["reports"] / "ORDER_21_report.md")
    assert rec["reason"] == "witnessed by the build report"
    res = frun(tree, "check", tree["facts"])
    assert res.returncode == EXIT_OK, res.stderr          # no false "ledger broken"


def test_record_still_works_after_a_release(tree):
    assert release(tree, report(tree)).returncode == 0
    res = frun(tree, "record", tree["facts"], "150_new.md", "--reason", "no change")
    assert res.returncode == EXIT_REFUSED and "unchanged" in res.stderr
    (tree["facts"] / "150_new.md").write_text("# changed\n")
    assert frun(tree, "record", tree["facts"], "150_new.md", "--reason", "an edit").returncode == 0
    assert frun(tree, "check", tree["facts"]).returncode == EXIT_OK


@pytest.mark.parametrize("name,last", [
    ("ORDER_21_report.md", "STOP — ambiguity"),
    ("ORDER_21_report.md", "Not READY FOR /restart"),
    ("ORDER_21_review_odin.md", "DONE"),
    ("ORDER_21_attempt1_stopped_report.md", "DONE"),
    ("ORDER_22_report.md", "DONE"),                      # the fact does not name order 22
])
def test_a_report_witness_that_does_not_qualify_is_refused(tree, name, last):
    before = ledger_sha(tree)
    res = release(tree, report(tree, name, last))
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert ledger_sha(tree) == before and listed(tree) == ["150_new.md"]


@pytest.mark.parametrize("last", ["DONE", "READY FOR /restart", "READY FOR USER'S INSTALL"])
def test_each_report_ending_qualifies(tree, last):
    assert release(tree, report(tree, last=last)).returncode == EXIT_OK


def test_a_report_outside_the_reports_folder_is_refused(tree):
    (tree["root"] / "ORDER_21_report.md").write_text("ORDER 21\nDONE\n")
    assert release(tree, "report:ORDER_21_report.md").returncode == EXIT_REFUSED
    assert release(tree, "report:build_orders/reports/../../ORDER_21_report.md").returncode \
        == EXIT_REFUSED


@pytest.mark.parametrize("reviewer", ["Odin", "Claude-Odin", "**Odin**", "Alice, Odin",
                                      "Claude (Odin)", ""])
def test_a_review_naming_odin_or_no_one_is_refused(tree, reviewer):
    f = tree["root"] / "workspace/FINDINGS.md"
    f.write_text(f"# COLD REVIEW\nReviewer: {reviewer}          date: 2026-10-01\n- [F1] x\n")
    before = ledger_sha(tree)
    assert release(tree, "review:workspace/FINDINGS.md").returncode == EXIT_REFUSED
    assert ledger_sha(tree) == before


def test_a_review_by_someone_else_releases(tree):
    f = tree["root"] / "workspace/FINDINGS.md"
    f.write_text("# COLD REVIEW\nReviewer: Alice          date: 2026-10-01\n- [F1] x\n")
    assert release(tree, "review:workspace/FINDINGS.md").returncode == EXIT_OK
    assert listed(tree) == []


def test_a_review_inside_memory_is_refused(tree):
    f = tree["root"] / "memory/FINDINGS.md"
    f.write_text("Reviewer: Alice\n")
    assert release(tree, "review:memory/FINDINGS.md").returncode == EXIT_REFUSED


@pytest.mark.parametrize("w", ["user:said so", "user:", "odin:trust me", "report", ":x",
                               "review:workspace/nope.md", "seal:workspace/nope.json",
                               "regate:9999"])
def test_unknown_or_unbacked_witnesses_are_refused_and_nothing_is_written(tree, w):
    before = ledger_sha(tree)
    res = release(tree, w)
    assert res.returncode == EXIT_REFUSED, res.stdout + res.stderr
    assert "REFUSED" in res.stderr
    assert ledger_sha(tree) == before


def test_a_seal_witness_needs_sealgate_verify_to_pass(tree):
    (tree["root"] / "workspace/sealgate/sealgate.py").write_text(STUB_SEALGATE)
    d = tree["root"] / "workspace/exp"
    d.mkdir()
    (d / "PREREG.md").write_text("# prereg\n")
    (d / "RUN_TOKEN.json").write_text(json.dumps({"prereg": str(d / "PREREG.md")}))
    assert release(tree, "seal:workspace/exp/RUN_TOKEN.json").returncode == EXIT_REFUSED
    (d / "VERIFY_OK").write_text("")
    assert release(tree, "seal:workspace/exp/RUN_TOKEN.json").returncode == EXIT_OK
    assert listed(tree) == []


def test_a_seal_token_pointing_at_a_prereg_elsewhere_is_refused(tree):
    (tree["root"] / "workspace/sealgate/sealgate.py").write_text(STUB_SEALGATE)
    d, e = tree["root"] / "workspace/exp", tree["root"] / "workspace/other"
    d.mkdir(), e.mkdir()
    (e / "PREREG.md").write_text("# prereg\n")
    (e / "VERIFY_OK").write_text("")
    (d / "RUN_TOKEN.json").write_text(json.dumps({"prereg": str(e / "PREREG.md")}))
    assert release(tree, "seal:workspace/exp/RUN_TOKEN.json").returncode == EXIT_REFUSED


def regate_ledger(t, outcome, kind="score", check_ok=True):
    reg = t["root"] / "workspace/seam/registry"
    rec = {"kind": kind, "entry": "0007", "id": "0007-p1",
           "score": {"scored": "2026-10-01", "outcome": outcome, "brier": 0.1, "note": "n"}}
    (reg / "REGATE_LEDGER.jsonl").write_text(json.dumps(rec) + "\n")
    if check_ok:
        (reg / "CHECK_OK").write_text("")


@pytest.mark.parametrize("ref", ["0007", "0007-p1"])
def test_a_regate_witness_with_a_scored_outcome_releases(tree, ref):
    regate_ledger(tree, 1)
    assert release(tree, f"regate:{ref}").returncode == EXIT_OK
    assert listed(tree) == []


@pytest.mark.parametrize("outcome,kind,check_ok", [(None, "score", True), (1, "status", True),
                                                   (0, "score", False)])
def test_a_regate_witness_without_a_checked_scored_outcome_is_refused(tree, outcome, kind,
                                                                      check_ok):
    regate_ledger(tree, outcome, kind, check_ok)
    assert release(tree, "regate:0007").returncode == EXIT_REFUSED


def test_release_refuses_a_legacy_fact_an_already_released_one_and_an_off_ledger_edit(tree):
    w = report(tree)
    assert release(tree, w, name="001_old.md").returncode == EXIT_REFUSED
    assert release(tree, w).returncode == EXIT_OK
    assert release(tree, w).returncode == EXIT_REFUSED
    (tree["facts"] / "150_new.md").write_text("# ORDER 21 edited off-ledger\n")
    assert release(tree, w).returncode == EXIT_REFUSED


def test_release_refuses_an_empty_reason(tree):
    assert release(tree, report(tree), reason=" ").returncode == EXIT_REFUSED


def test_the_new_verbs_never_write_to_the_facts_directory(tree):
    before = fingerprint(tree["facts"])
    release(tree, "user:x")
    release(tree, report(tree))
    frun(tree, "interim", tree["facts"])
    frun(tree, "baseline-legacy", tree["facts"], "--cutoff-line", "1", "--cutoff-sha256",
         "0" * 64, "--vault-seq", "1")
    assert fingerprint(tree["facts"]) == before


def test_help_lists_the_new_verbs_and_no_user_witness():
    help_text = subprocess.run([sys.executable, str(FACTGATE), "--help"],
                               capture_output=True, text=True).stdout
    for verb in ("baseline-legacy", "release", "interim"):
        assert verb in help_text
    sys.path.insert(0, str(HERE))
    import factgate
    assert set(factgate.WITNESS_KINDS) == {"regate", "seal", "review", "report"}


# ==========================================================================
# ORDER 35b — archive and restore. An archived fact moves to memory/archive/ (os.replace),
# stops loading, and keeps its place in the ledger. Refused: a file not in memory/facts/, a
# fact listed in memory/PINNED.txt, an INTERIM fact, a fact an OPEN registry entry names.
# There is no flag that lifts a refusal. Every test runs a COPY of factgate.py inside a temp
# `helix/`, because facts, archive, PINNED.txt and the registry are found from where it lives.
# ==========================================================================

@pytest.fixture
def arch(tmp_path):
    """A temp helix/ with four legacy facts (plain, pinned, named by an open registry entry,
    named by a closed one) and one INTERIM fact recorded after the cutoff."""
    root = tmp_path / "helix"
    fg = root / "workspace/factgate"
    fg.mkdir(parents=True)
    shutil.copy(FACTGATE, fg / "factgate.py")
    copy_categories(root)
    (root / "workspace/seam/registry").mkdir(parents=True)
    (root / "workspace/seam/registry/registry.json").write_text(json.dumps({
        "0040": {"status": "open", "facts": ["030_open.md"]},
        "0041": {"status": "closed", "note": "see 040_ for the close"}}))
    facts = root / "memory/facts"
    facts.mkdir(parents=True)
    (root / "memory/PINNED.txt").write_text("# pins\n\n020_pinned.md\n")
    (facts / "010_plain.md").write_text("# plain\n\nan old closed-limb detail.\n")
    (facts / "020_pinned.md").write_text("# STANDING RULE: be terse\n")
    (facts / "030_open.md").write_text("# open work\n")
    (facts / "040_closed.md").write_text("# DECISION — the limb is closed\n")
    t = {"root": root, "fg": fg / "factgate.py", "facts": facts, "state": fg / "state",
         "archive": root / "memory/archive", "pinned": root / "memory/PINNED.txt",
         "registry": root / "workspace/seam/registry/registry.json"}
    assert frun(t, "baseline", facts).returncode == 0
    h = prefix_sha(t["state"], 4)
    (facts / "150_new.md").write_text("# new, interim\nCategory: helix\n")
    assert frun(t, "record", facts, "150_new.md", "--reason", "new").returncode == 0
    assert frun(t, "baseline-legacy", facts, "--cutoff-line", "4", "--cutoff-sha256", h,
                "--vault-seq", "1").returncode == 0
    return t


def archive(t, *names, why="closed limb, superseded by 65_"):
    return frun(t, "archive", *names, *(("--why", why) if why is not None else ()))


def restore(t, name, why="asked about it again"):
    return frun(t, "restore", name, "--why", why)


def test_archive_moves_the_file_and_check_stays_clean(arch):
    raw = (arch["facts"] / "010_plain.md").read_bytes()
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert not (arch["facts"] / "010_plain.md").exists()
    assert (arch["archive"] / "010_plain.md").read_bytes() == raw
    chk = frun(arch, "check", arch["facts"])
    assert chk.returncode == EXIT_OK, chk.stdout + chk.stderr


def test_the_archive_ledger_line_shape(arch):
    h = sha(arch["facts"] / "010_plain.md")
    assert archive(arch, "010_plain.md", why="closed limb").returncode == 0
    lines = ledger_lines(arch["state"])
    rec = lines[-1]
    assert set(rec) == {"kind", "file", "sha256", "size", "reason", "actor", "line", "prev",
                        "ts", "factgate", "self"}
    assert (rec["kind"], rec["file"], rec["sha256"], rec["reason"]) == \
        ("archive", "010_plain.md", h, "closed limb")
    assert rec["actor"] and rec["size"] == len((arch["archive"] / "010_plain.md").read_bytes())
    assert "restore" not in rec
    assert frun(arch, "check", arch["facts"]).returncode == 0          # chain still verifies


def test_restore_carries_restore_true_and_is_counted(arch):
    assert archive(arch, "010_plain.md").returncode == 0
    res = restore(arch, "010_plain.md")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    rec = ledger_lines(arch["state"])[-1]
    assert (rec["kind"], rec["restore"], rec["file"]) == ("restore", True, "010_plain.md")
    assert rec["reason"] == "asked about it again" and rec["actor"]
    assert "restores: 1" in res.stdout
    assert archive(arch, "010_plain.md").returncode == 0
    assert "restores: 2" in restore(arch, "010_plain.md").stdout
    assert sum(1 for r in ledger_lines(arch["state"]) if r.get("restore") is True) == 2


def test_round_trip_is_byte_identical_and_the_chain_and_legacy_survive(arch):
    before = (arch["facts"] / "010_plain.md").read_bytes()
    interim_before = listed(arch)
    assert archive(arch, "010_plain.md").returncode == 0
    assert restore(arch, "010_plain.md").returncode == 0
    assert (arch["facts"] / "010_plain.md").read_bytes() == before
    assert not (arch["archive"] / "010_plain.md").exists()
    chk = frun(arch, "check", arch["facts"])
    assert chk.returncode == EXIT_OK, chk.stdout + chk.stderr
    assert "chain intact" in chk.stdout
    assert listed(arch) == interim_before == ["150_new.md"]   # a legacy fact stays legacy


def test_archive_refuses_a_pinned_fact_and_nothing_moves(arch):
    res = archive(arch, "020_pinned.md")
    assert res.returncode == EXIT_REFUSED
    assert "FAILED   A3_NOT_PINNED" in res.stderr and "PINNED.txt" in res.stderr
    assert (arch["facts"] / "020_pinned.md").exists() and not arch["archive"].exists()


def test_unpinning_in_pinned_txt_is_the_only_way_to_archive_a_pinned_fact(arch):
    assert archive(arch, "020_pinned.md").returncode == EXIT_REFUSED
    arch["pinned"].write_text("# pins\n\n")
    assert archive(arch, "020_pinned.md").returncode == EXIT_OK


def test_heading_words_do_not_pin(arch):
    """STANDING RULE / DECISION in a heading pin nothing: only PINNED.txt does."""
    assert archive(arch, "040_closed.md").returncode == EXIT_OK


def test_a_missing_pinned_txt_refuses_rather_than_pinning_nothing(arch):
    arch["pinned"].unlink()
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "A3_NOT_PINNED" in res.stderr


def test_archive_refuses_an_interim_fact(arch):
    res = archive(arch, "150_new.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   A4_NOT_INTERIM" in res.stderr


def test_archive_refuses_a_fact_an_open_registry_entry_names(arch):
    res = archive(arch, "030_open.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   A5_NO_OPEN_ENTRY" in res.stderr


def test_a_closed_registry_entry_naming_a_prefix_does_not_hold_a_fact(arch):
    assert archive(arch, "040_closed.md").returncode == EXIT_OK


def test_an_open_entry_naming_the_prefix_holds_the_fact(arch):
    arch["registry"].write_text(json.dumps({"0050": {"status": "open", "note": "per 010_"}}))
    assert archive(arch, "010_plain.md").returncode == EXIT_REFUSED


def test_an_unreadable_registry_refuses(arch):
    arch["registry"].write_text("{not json")
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "A5_NO_OPEN_ENTRY" in res.stderr


@pytest.mark.parametrize("target", ["nope.md", "../PINNED.txt", "/etc/hostname",
                                    "{tmp}/elsewhere.md"])
def test_archive_refuses_a_file_not_in_memory_facts(arch, target, tmp_path):
    (tmp_path / "elsewhere.md").write_text("# not a fact\n")
    res = archive(arch, target.format(tmp=tmp_path))
    assert res.returncode == EXIT_REFUSED and "FAILED   A2_IN_MEMORY_FACTS" in res.stderr


def test_archive_accepts_the_path_spelling_of_a_fact(arch):
    assert archive(arch, str(arch["facts"] / "010_plain.md")).returncode == EXIT_OK


def test_archive_refuses_an_off_ledger_edit(arch):
    """Moving an unrecorded edit out of sight would hide it: only the recorded bytes go."""
    with open(arch["facts"] / "010_plain.md", "a") as fh:
        fh.write("edited behind the ledger's back\n")
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   A2_IN_MEMORY_FACTS" in res.stderr


def test_archive_refuses_a_broken_chain(arch):
    p = arch["state"] / LEDGER
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace('"baseline"', '"Baseline"', 1)
    p.write_text("\n".join(lines) + "\n")
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   A1_LEDGER_INTACT" in res.stderr


@pytest.mark.parametrize("why", ["", "  ", "x"])
def test_archive_and_restore_refuse_an_empty_why(arch, why):
    assert archive(arch, "010_plain.md", why=why).returncode == EXIT_REFUSED
    assert archive(arch, "010_plain.md").returncode == EXIT_OK
    assert restore(arch, "010_plain.md", why=why).returncode == EXIT_REFUSED


def test_archive_without_why_is_a_usage_error(arch):
    assert archive(arch, "010_plain.md", why=None).returncode == EXIT_USAGE


def test_one_refusal_in_a_list_moves_nothing(arch):
    before, ledger_before = fingerprint(arch["facts"]), sha(arch["state"] / LEDGER)
    res = archive(arch, "010_plain.md", "040_closed.md", "020_pinned.md")
    assert res.returncode == EXIT_REFUSED
    assert fingerprint(arch["facts"]) == before and sha(arch["state"] / LEDGER) == ledger_before


def test_a_list_archives_one_line_per_file(arch):
    n = len(ledger_lines(arch["state"]))
    assert archive(arch, "010_plain.md", "040_closed.md").returncode == EXIT_OK
    recs = ledger_lines(arch["state"])[n:]
    assert [(r["kind"], r["file"]) for r in recs] == [("archive", "010_plain.md"),
                                                     ("archive", "040_closed.md")]


def test_archive_refuses_when_the_archive_already_holds_that_name(arch):
    arch["archive"].mkdir()
    (arch["archive"] / "010_plain.md").write_text("an older copy\n")
    res = archive(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "A6_ARCHIVE_SLOT_FREE" in res.stderr
    assert (arch["archive"] / "010_plain.md").read_text() == "an older copy\n"


def test_restore_refuses_a_name_the_ledger_has_not_archived(arch):
    arch["archive"].mkdir()
    (arch["archive"] / "999_planted.md").write_text("# planted\n")
    res = restore(arch, "999_planted.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   B2_ARCHIVED_ON_LEDGER" in res.stderr


def test_restore_refuses_an_archived_file_that_changed(arch):
    assert archive(arch, "010_plain.md").returncode == 0
    with open(arch["archive"] / "010_plain.md", "a") as fh:
        fh.write("edited in the archive\n")
    res = restore(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   B2_ARCHIVED_ON_LEDGER" in res.stderr
    assert not (arch["facts"] / "010_plain.md").exists()


def test_restore_refuses_to_overwrite_a_fact_of_the_same_name(arch):
    assert archive(arch, "010_plain.md").returncode == 0
    (arch["facts"] / "010_plain.md").write_text("# a new fact, same name\n")
    res = restore(arch, "010_plain.md")
    assert res.returncode == EXIT_REFUSED and "FAILED   B3_FACTS_SLOT_FREE" in res.stderr
    assert (arch["facts"] / "010_plain.md").read_text() == "# a new fact, same name\n"


def test_a_released_fact_stays_released_through_a_round_trip(arch):
    """The release is keyed on the sha; the bytes come back the same, so the release holds."""
    sys.path.insert(0, str(HERE))
    import factgate                               # append_ledger is given the temp state dir
    factgate.append_ledger(arch["state"], [{"kind": "release", "file": "150_new.md",
                                            "sha256": sha(arch["facts"] / "150_new.md"),
                                            "size": 1, "witness": "report:x",
                                            "witness_sha256": None, "reason": "test"}])
    assert listed(arch) == []
    assert archive(arch, "150_new.md").returncode == EXIT_OK
    assert restore(arch, "150_new.md").returncode == EXIT_OK
    assert listed(arch) == [] and frun(arch, "check", arch["facts"]).returncode == 0


def test_an_archived_fact_is_not_listed_as_interim_or_seen_by_check(arch):
    assert archive(arch, "010_plain.md").returncode == 0
    assert "010_plain.md" not in frun(arch, "interim", arch["facts"]).stdout
    assert "010_plain.md" not in frun(arch, "check", arch["facts"]).stdout


def test_pinned_reader_rule(tmp_path):
    """One name per line, `#` comments and blanks ignored: order 33b's rule (app._pinned_names).
    test_facts_tiers.py runs both readers on the same files and compares the sets."""
    sys.path.insert(0, str(HERE))
    import factgate
    p = tmp_path / "PINNED.txt"
    p.write_text("# header\n\n  15_a.md  \n   # indented comment\n91_b.md\n\n")
    assert factgate.pinned_names(p) == {"15_a.md", "91_b.md"}
    with pytest.raises(OSError):
        factgate.pinned_names(tmp_path / "missing.txt")


def test_help_lists_archive_and_restore_with_why_and_no_other_option():
    help_text = subprocess.run([sys.executable, str(FACTGATE), "archive", "--help"],
                               capture_output=True, text=True).stdout
    opts = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", help_text))
    assert opts == {"--why", "--help"}, opts
    help_text = subprocess.run([sys.executable, str(FACTGATE), "restore", "--help"],
                               capture_output=True, text=True).stdout
    assert set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", help_text)) == {"--why", "--help"}


def test_the_archive_verbs_never_touch_the_real_fact_base(arch):
    before = sorted((p.name, sha(p)) for p in REAL_FACTS.iterdir() if p.is_file())
    archive(arch, "010_plain.md")
    restore(arch, "010_plain.md")
    archive(arch, "020_pinned.md")
    after = sorted((p.name, sha(p)) for p in REAL_FACTS.iterdir() if p.is_file())
    assert after == before


# --------------------------------------------------------------------- ORDER 76: F5

def test_f5_is_the_last_record_precondition():
    sys.path.insert(0, str(HERE))
    import factgate
    assert factgate.RECORD_PRECONDITIONS == ["F1_LEDGER_INTACT", "F2_FILE_PRESENT",
                                             "F3_SOMETHING_CHANGED", "F4_REASON_IN_WRITING",
                                             "F5_CATEGORY_DECLARED"]


def test_f5_refuses_a_new_fact_with_no_category_line_and_lists_the_names(based, state):
    before = (Path(state) / LEDGER).read_bytes()
    (based / "005_new.md").write_text("# new\n\nno category here.\n")
    res = run("record", based, "005_new.md", "--reason", "new fact", state=state)
    assert res.returncode == EXIT_REFUSED, res.stderr
    assert "FAILED   F5_CATEGORY_DECLARED: a new fact must declare its category" in res.stderr
    assert "names: personal, household finance, rules, helix" in res.stderr
    assert "refused on 1 precondition(s): F5_CATEGORY_DECLARED" in res.stderr
    assert (Path(state) / LEDGER).read_bytes() == before


def test_f5_refuses_an_unknown_or_too_many_names(based, state):
    for text, problem in (("Category: boats\n", "unknown: boats"),
                          ("Category: helix · rules · personal\n", "too many: 3"),
                          ("Category:   \n", "no `Category:` line")):
        (based / "005_new.md").write_text("# new\n" + text)
        res = run("record", based, "005_new.md", "--reason", "new fact", state=state)
        assert res.returncode == EXIT_REFUSED and problem in res.stderr, (text, res.stderr)


def test_f5_records_a_new_fact_that_declares_a_category(based, state):
    (based / "005_new.md").write_text("# new\nCategory: helix\n\na fact.\n")
    res = run("record", based, "005_new.md", "--reason", "new fact", state=state)
    assert res.returncode == EXIT_OK, res.stderr
    assert "ok       F5_CATEGORY_DECLARED: declares helix" in res.stdout


def test_f5_does_not_ask_an_old_fact_for_a_line(based, state):
    (based / "002_second.md").write_text("# second\n\nreworded, still no category.\n")
    res = run("record", based, "002_second.md", "--reason", "an edit", state=state)
    assert res.returncode == EXIT_OK, res.stderr
    assert "ok       F5_CATEGORY_DECLARED: not new: not required" in res.stdout


def test_f5_is_not_asked_on_record_delete(based, state):
    (based / "003_third.md").unlink()
    res = run("record-delete", based, "003_third.md", "--reason", "retired", state=state)
    assert res.returncode == EXIT_OK, res.stderr
    assert "ok       F5_CATEGORY_DECLARED: record-delete: not required" in res.stdout


def test_f5_a_deleted_then_rewritten_fact_is_new_again(based, state):
    (based / "003_third.md").unlink()
    assert run("record-delete", based, "003_third.md", "--reason", "retired",
               state=state).returncode == EXIT_OK
    (based / "003_third.md").write_text("# third\n\nback, with no category.\n")
    res = run("record", based, "003_third.md", "--reason", "back", state=state)
    assert res.returncode == EXIT_REFUSED and "F5_CATEGORY_DECLARED" in res.stderr


def test_f5_fails_closed_when_the_category_list_cannot_load(tree):
    shutil.rmtree(tree["root"] / "workspace/categories")
    (tree["facts"] / "160_new.md").write_text("# new\nCategory: helix\n")
    res = frun(tree, "record", tree["facts"], "160_new.md", "--reason", "new")
    assert res.returncode == EXIT_REFUSED, res.stderr
    assert "FAILED   F5_CATEGORY_DECLARED: cannot load the category list" in res.stderr


def test_f5_fails_closed_on_an_empty_category_list(tree):
    (tree["root"] / "workspace/categories/CATEGORIES.txt").write_text("# nothing\n")
    (tree["facts"] / "160_new.md").write_text("# new\nCategory: helix\n")
    res = frun(tree, "record", tree["facts"], "160_new.md", "--reason", "new")
    assert res.returncode == EXIT_REFUSED, res.stderr
    assert "cannot load the category list" in res.stderr and "no category names" in res.stderr
