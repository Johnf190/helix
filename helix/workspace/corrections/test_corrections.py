"""Tests for corrections.py (order 72, item 30): cold-read findings become a correction queue.

The live cold read (`coldread/READS/2026-10-03.md`) is only ever READ. Every queue, fact base and
session log here is a temp copy or a fixture; the live QUEUE.jsonl is never written by a test.
"""

import sys

sys.dont_write_bytecode = True

import datetime
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import corrections as C  # noqa: E402
import coldread  # noqa: E402  (on sys.path through corrections → propose)

LIVE_READ = Path.home() / "helix/workspace/coldread/READS/2026-10-03.md"
TODAY = datetime.date(2026, 10, 23)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def render(date, verdict, candidates, plant_file="186_plant.md"):
    """A read in coldread's own rendered format: render_md, not a hand-written imitation."""
    rec = {"date": date, "outcome": "DONE", "reason": None, "files": 3, "excluded": 0, "chars": 100,
           "batches": 1, "batch_chars": [100], "cost_usd": 0.0, "sessions": 2,
           "plant": {"found": True, "file": plant_file, "batch": 1, "claim": "Planted claim.",
                     "contradicted_by": ["174_real.md"]}}
    return coldread.render_md(rec, [], candidates, verdict)


def cand(f, quote, why="why", conf=0.5):
    return {"batch": 1, "file": f, "quote": quote, "why": why, "confidence": conf}


Q48 = "Note `1/(1−ρ²) = 50.25` and `(1+ρ²)/(1−ρ²) = 50.75` at ρ=0.99"
Q71 = "Fish are not in the tool (56 exist as data, no artwork generated, ~$7.75 when wanted)."
Q41 = "This is the first clean PASS this finance thread has produced."


@pytest.fixture
def at(tmp_path, monkeypatch):
    """corrections pointed at a temp READS dir, queue, fact base and session log."""
    reads = tmp_path / "READS"
    reads.mkdir()
    facts = tmp_path / "memory" / "facts"
    facts.mkdir(parents=True)
    log = tmp_path / "log.md"
    log.write_text("# log\n")
    monkeypatch.setattr(C, "READS_DIR", reads)
    monkeypatch.setattr(C, "QUEUE", tmp_path / "QUEUE.jsonl")
    monkeypatch.setattr(C, "FACTS", facts)
    monkeypatch.setattr(C, "LOG", log)

    class At:
        pass
    a = At()
    a.reads, a.facts, a.log, a.queue, a.tmp = reads, facts, log, tmp_path / "QUEUE.jsonl", tmp_path

    def read(date, verdict, candidates, **kw):
        (reads / f"{date}.md").write_text(render(date, verdict, candidates, **kw))
    a.read = read

    def fact(name, text):
        (facts / name).write_text(text)
    a.fact = fact
    return a


def one_read(at):
    at.read("2026-10-03",
            {"verdict": {"file": "48_ratio.md", "quote": Q48, "why": "arithmetic"},
             "runners_up": [{"file": "71_fish.md", "quote": Q71, "why": "stale"}]},
            [cand("48_ratio.md", Q48, conf=0.9), cand("41_ftx.md", Q41, conf=0.85),
             cand("186_plant.md", "Planted claim.", conf=0.92)])


# --- parsing --------------------------------------------------------------------------------

def test_the_real_read_parses_verdict_runners_up_no_plant_no_duplicates():
    before = sha(LIVE_READ)
    r = C.parse_read(LIVE_READ.read_text(encoding="utf-8"))
    assert sha(LIVE_READ) == before
    assert r["plant"] == "186_odin_alice_name_origin.md"
    f = r["findings"]
    assert f[0]["rank"] == "verdict" and f[0]["file"] == "48_example_edf1.md"
    assert f[0]["quote"] == Q48 and f[0]["why"].startswith("The second value is an arithmetic error.")
    assert f[0]["conf"] == 0.9                       # its stage-1 twin's confidence
    runners = [x for x in f if x["rank"] == "runner-up"]
    assert [x["file"] for x in runners] == [
        "71_example_0cf6.md", "41_example_14b9.md",
        "81_example_b5a6.md", "20_example_9923.md",
        "123_example_ce2a.md"]
    assert runners[3]["quote"] == "the lab session ended\n11 days before this without touching it"
    assert all("186_odin_alice_name_origin.md" != x["file"] for x in f)
    assert all("Huginn" not in x["quote"] for x in f)
    keys = [(x["file"], " ".join(x["quote"].split())) for x in f]
    assert len(keys) == len(set(keys))
    # 18 stage-1 candidates, minus the plant, minus the 5 that repeat the verdict or a runner-up
    # (41's candidate quotes a different line of 41, so it stays its own finding)
    assert len(f) == 1 + 5 + 12
    assert [x["rank"] for x in f] == ["verdict"] + ["runner-up"] * 5 + ["candidate"] * 12


def test_a_duplicate_collapses_to_the_higher_rank(at):
    one_read(at)
    r = C.parse_read((at.reads / "2026-10-03.md").read_text())
    by = {(x["file"], x["quote"]): x["rank"] for x in r["findings"]}
    assert by == {("48_ratio.md", Q48): "verdict", ("71_fish.md", Q71): "runner-up",
                  ("41_ftx.md", Q41): "candidate"}


def test_whitespace_differences_are_the_same_quote(at):
    at.read("2026-10-03", {"verdict": {"file": "48_ratio.md", "quote": Q48, "why": "w"}, "runners_up": []},
            [cand("48_ratio.md", Q48.replace(" at ", "\nat  "))])
    assert [x["rank"] for x in C.parse_read((at.reads / "2026-10-03.md").read_text())["findings"]] == ["verdict"]


def test_the_plant_is_skipped_by_its_mark_and_by_its_file(at):
    at.read("2026-10-03", {"verdict": {"file": "186_plant.md", "quote": "Another claim about it.", "why": "w"},
                           "runners_up": [{"file": "186_plant.md", "quote": "Third.", "why": "w"}]},
            [cand("186_plant.md", "Planted claim."), cand("06_x.md", "real one")])
    r = C.parse_read((at.reads / "2026-10-03.md").read_text())
    assert [x["file"] for x in r["findings"]] == ["06_x.md"]


def test_a_read_that_did_not_reach_stage_2_still_gives_its_candidates(at):
    at.read("2026-10-03", None, [cand("06_x.md", "real one")])
    r = C.parse_read((at.reads / "2026-10-03.md").read_text())
    assert [(x["rank"], x["file"]) for x in r["findings"]] == [("candidate", "06_x.md")]


def test_an_item_that_does_not_parse_is_refused_not_dropped(at):
    text = render("2026-10-03", None, [cand("06_x.md", "real one")])
    text = text.replace("  - why: why", "  - reason: why")
    with pytest.raises(ValueError):
        C.parse_read(text)


# --- harvest --------------------------------------------------------------------------------

def test_harvest_queues_ids_in_rank_order_and_a_second_harvest_adds_nothing(at):
    one_read(at)
    res = C.harvest()
    assert [(f["id"], f["rank"], f["file"]) for f in res["findings"]] == [
        ("C-20261003-1", "verdict", "48_ratio.md"), ("C-20261003-2", "runner-up", "71_fish.md"),
        ("C-20261003-3", "candidate", "41_ftx.md")]
    recs = C.records()
    assert [r["kind"] for r in recs] == ["finding"] * 3 + ["harvest"]
    assert recs[0] == dict(recs[0], kind="finding", id="C-20261003-1", read="2026-10-03", rank="verdict",
                           file="48_ratio.md", quote=Q48, why="arithmetic", conf=0.9)
    assert recs[-1]["read"] == "2026-10-03" and recs[-1]["findings"] == 3
    assert C.verify() is None
    before = at.queue.read_bytes()
    res2 = C.harvest()
    assert res2["findings"] == [] and res2["reads"] == []
    assert at.queue.read_bytes() == before


def test_harvest_dry_writes_nothing(at):
    one_read(at)
    res = C.harvest(dry=True)
    assert len(res["findings"]) == 3 and not at.queue.exists()


def test_a_new_read_is_harvested_on_its_own(at):
    one_read(at)
    C.harvest()
    at.read("2026-11-03", None, [cand("06_x.md", "real one")])
    res = C.harvest()
    assert res["reads"] == ["2026-11-03"] and [f["id"] for f in res["findings"]] == ["C-20261103-1"]


def test_harvest_refuses_a_broken_chain(at):
    one_read(at)
    C.harvest()
    raw = at.queue.read_text().replace('"rank":"verdict"', '"rank":"candidate"')
    at.queue.write_text(raw)
    assert C.verify() is not None
    at.read("2026-11-03", None, [cand("06_x.md", "real one")])
    before = at.queue.read_bytes()
    with pytest.raises(RuntimeError):
        C.harvest()
    assert at.queue.read_bytes() == before


def test_verify_catches_a_removed_line(at):
    one_read(at)
    C.harvest()
    lines = at.queue.read_text().splitlines(keepends=True)
    at.queue.write_text("".join(lines[1:]))
    assert "hash of the line before" in C.verify()


# --- closed, derived ------------------------------------------------------------------------

A48 = ("# ratio lemma\nSome text first.\nNote `1/(1−ρ²) = 50.25` and `(1+ρ²)/(1−ρ²) = 50.75` at ρ=0.99, near 51.7.\n"
       "Other text that is fine and long enough to retire on its own.\n")


def state_of(cid, today=TODAY):
    return next(f for f in C.status(today=today)["findings"] if f["id"] == cid)


def test_a_finding_is_closed_by_an_overlapping_retirement_and_shows_its_b(at):
    one_read(at)
    C.harvest()
    at.fact("48_ratio.md", A48)
    at.fact("200_fix48.md", '# fix\nRETIRES: `48_*` "and `(1+ρ²)/(1−ρ²) = 50.75` at ρ=0.99"\n')
    f = state_of("C-20261003-1")
    assert f["state"] == "closed" and f["closed_by"] == "200_fix48.md"
    assert state_of("C-20261003-2")["state"] == "open"


def test_closed_when_the_retired_passage_contains_the_quote(at):
    one_read(at)
    C.harvest()
    at.fact("48_ratio.md", A48)
    at.fact("200_fix48.md", f'# fix\nRETIRES: `48_ratio.md` "Some text first. {Q48}, near 51.7."\n')
    assert state_of("C-20261003-1")["state"] == "closed"


def test_not_closed_by_a_different_passage_of_the_same_fact(at):
    one_read(at)
    C.harvest()
    at.fact("48_ratio.md", A48)
    at.fact("200_fix48.md", '# fix\nRETIRES: `48_*` "Other text that is fine and long enough to retire"\n')
    assert C.retired_in_dir(at.facts)["applied"], "the retirement itself applies"
    assert state_of("C-20261003-1")["state"] == "open"


def test_not_closed_by_the_same_passage_retired_in_a_different_fact(at):
    one_read(at)
    C.harvest()
    at.fact("49_other.md", A48)
    at.fact("200_fix49.md", '# fix\nRETIRES: `49_*` "and `(1+ρ²)/(1−ρ²) = 50.75` at ρ=0.99"\n')
    assert C.retired_in_dir(at.facts)["applied"]
    assert state_of("C-20261003-1")["state"] == "open"


def test_an_unresolved_retirement_leaves_or_puts_the_finding_back_open(at):
    one_read(at)
    C.harvest()
    at.fact("48_ratio.md", A48)
    at.fact("200_fix48.md", '# fix\nRETIRES: `48_*` "and `(1+ρ²)/(1−ρ²) = 50.75` at ρ=0.99"\n')
    assert state_of("C-20261003-1")["state"] == "closed"
    at.fact("48_ratio.md", A48 + A48)                 # the passage now occurs twice → unresolved
    assert C.retired_in_dir(at.facts)["unresolved"]
    f = state_of("C-20261003-1")
    assert f["state"] == "open" and f["closed_by"] is None


def test_overlap_rule():
    assert C.overlaps("a" * 5 + "0123456789abcdefghij" + "b" * 5, "zz 0123456789abcdefghij zz")
    assert not C.overlaps("xx0123456789abcdefghixx", "yy0123456789abcdefghiyy")   # 19 in common
    assert C.overlaps("short", "a longer quote with short in it")
    assert C.overlaps("the  lab\nsession", "the lab session ended")


# --- reject ---------------------------------------------------------------------------------

def log_with(at, *lines, heading="2026-10-22 10:00"):
    at.log.write_text(f"# log\n\n## {heading}\n\n" + "".join(l + "\n" for l in lines))


def test_reject_recorded_when_users_words_are_in_a_recent_you_line(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** no, 71 is fine as it is, the fish note is historical")
    ok, msg = C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert ok, msg
    rej = C.records(kind="rejection")
    assert rej == [dict(rej[0], kind="rejection", id="C-20261003-2", quote="71 is fine as it is",
                        log_heading="2026-10-22 10:00")]
    st = C.status(today=TODAY)
    assert state_of("C-20261003-2")["state"] == "rejected"
    assert (st["open"], st["closed"], st["rejected"]) == (2, 0, 1)
    assert C.verify() is None


def test_reject_refused_when_the_quote_is_missing(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** something else entirely")
    ok, msg = C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert not ok and msg.startswith("REFUSED") and not C.records(kind="rejection")


def test_reject_refused_when_the_quote_is_not_in_a_you_line(at):
    one_read(at)
    C.harvest()
    log_with(at, "**Odin:** 71 is fine as it is")
    ok, msg = C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert not ok and "not **You:** lines" in msg and not C.records(kind="rejection")


def test_reject_refused_when_the_quote_is_too_old(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** 71 is fine as it is", heading="2026-10-01 10:00")
    ok, msg = C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert not ok and "days old" in msg


def test_a_second_reject_and_an_unknown_id_and_an_empty_quote_are_refused(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** 71 is fine as it is", "**You:** and 41 too, leave it")
    assert C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)[0]
    ok, msg = C.reject("C-20261003-2", "and 41 too, leave it", today=TODAY)
    assert not ok and "already rejected" in msg
    ok, msg = C.reject("C-20991231-9", "and 41 too, leave it", today=TODAY)
    assert not ok and "no finding" in msg
    ok, msg = C.reject("C-20261003-3", "  ", today=TODAY)
    assert not ok and "empty" in msg
    assert len(C.records(kind="rejection")) == 1


def test_reject_refuses_a_broken_chain(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** 71 is fine as it is")
    at.queue.write_text(at.queue.read_text().replace("arithmetic", "arithmetik"))
    before = at.queue.read_bytes()
    ok, msg = C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert not ok and "chain is broken" in msg
    assert at.queue.read_bytes() == before


def test_the_log_is_only_read(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** 71 is fine as it is")
    before = sha(at.log)
    C.reject("C-20261003-2", "71 is fine as it is", today=TODAY)
    assert sha(at.log) == before


# --- status and the line --------------------------------------------------------------------

def test_status_ages_are_days_since_the_read(at):
    one_read(at)
    C.harvest()
    st = C.status(today=TODAY)
    assert [f["age"] for f in st["findings"]] == [20, 20, 20]
    assert (st["open"], st["closed"], st["rejected"], st["oldest"]) == (3, 0, 0, 20)


def test_the_line_with_nothing_queued(at):
    assert C.corrections_line(today=TODAY) == "- corrections: none queued (run corrections.py harvest)"
    at.queue.write_text("")
    assert C.corrections_line(today=TODAY) == "- corrections: none queued (run corrections.py harvest)"


def test_the_line_with_some_open(at):
    one_read(at)
    C.harvest()
    assert C.corrections_line(today=TODAY) == "- corrections: 3 open (oldest 20 days) · 0 closed · 0 rejected"


def test_the_line_with_nothing_open(at):
    one_read(at)
    C.harvest()
    log_with(at, "**You:** no to all three")
    for cid in ("C-20261003-1", "C-20261003-2", "C-20261003-3"):
        assert C.reject(cid, "no to all three", today=TODAY)[0]
    assert C.corrections_line(today=TODAY) == "- corrections: 0 open · 0 closed · 3 rejected"


def test_the_line_flags_the_oldest_open_past_30_days_with_the_first_three(at):
    one_read(at)
    at.read("2026-11-03", None, [cand("06_x.md", "real one")])
    C.harvest()
    line = C.corrections_line(today=datetime.date(2026, 11, 10))
    assert line == ("- corrections: 🟡 4 open (oldest 38 days) · 0 closed · 0 rejected · oldest open: "
                    "C-20261003-1 48_ratio.md, C-20261003-2 71_fish.md, C-20261003-3 41_ftx.md …")
    assert C.corrections_line(today=datetime.date(2026, 11, 2)).startswith("- corrections: 4 open (oldest 30 days)")


def test_the_line_says_when_the_chain_is_broken(at):
    one_read(at)
    C.harvest()
    at.queue.write_text(at.queue.read_text().replace("arithmetic", "arithmetik"))
    assert "🔴 chain BROKEN" in C.corrections_line(today=TODAY)


# --- CLI ------------------------------------------------------------------------------------

def run_cli(at, *args):
    """The CLI in a child process, pointed at the fixture paths through a shim (no env, no flags)."""
    shim = at.tmp / "shim.py"
    shim.write_text(
        "import sys, datetime\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "import corrections as C\n"
        f"C.READS_DIR = C.Path({str(at.reads)!r}); C.QUEUE = C.Path({str(at.queue)!r})\n"
        f"C.FACTS = C.Path({str(at.facts)!r}); C.LOG = C.Path({str(at.log)!r})\n"
        "sys.exit(C.main(sys.argv[1:]))\n")
    return subprocess.run([sys.executable, "-B", str(shim), *args], capture_output=True, text=True,
                          cwd=at.tmp)


def test_cli_harvest_dry_status_verify(at):
    one_read(at)
    r = run_cli(at, "harvest", "--dry")
    assert r.returncode == 0 and r.stdout.startswith("DRY RUN — 3 findings from 1 read") and not at.queue.exists()
    r = run_cli(at, "harvest")
    assert r.returncode == 0 and "3 findings from 1 read" in r.stdout
    r = run_cli(at, "status")
    assert r.returncode == 0 and "C-20261003-1\topen" in r.stdout and r.stdout.startswith("- corrections:")
    r = run_cli(at, "status", "--json")
    assert json.loads(r.stdout)["open"] == 3
    r = run_cli(at, "verify")
    assert r.returncode == 0 and "chain OK" in r.stdout
    at.queue.write_text(at.queue.read_text().replace("arithmetic", "arithmetik"))
    r = run_cli(at, "verify")
    assert r.returncode == 3 and "BROKEN" in r.stderr
    r = run_cli(at, "harvest")
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_cli_reject_needs_a_quote(at):
    one_read(at)
    run_cli(at, "harvest")
    r = run_cli(at, "reject", "C-20261003-2")
    assert r.returncode != 0 and not C.records(kind="rejection")


# --- the fences -----------------------------------------------------------------------------

def test_the_queue_lives_beside_this_file_and_no_fact_is_written():
    src = (HERE / "corrections.py").read_text()
    assert C.QUEUE == HERE / "QUEUE.jsonl"
    for bad in ("write_text", "open(path, \"w\"", "unlink", "rename", "shutil"):
        assert bad not in src, bad
    assert src.count('open(path, "a"') == 1, "one append, to the queue"


def test_no_escape_hatch_words():
    import re
    src = (HERE / "corrections.py").read_text()
    assert [l for l in src.splitlines() if re.search(r"force|override|bypass|skip_check", l, re.I)] == []


def test_find_quote_and_retired_in_are_the_shared_ones():
    import propose
    import memretrieve
    assert C.find_quote is propose.find_quote
    src = (HERE / "corrections.py").read_text()
    assert "memretrieve.retired_in(" in src and "RETIRES" not in src
