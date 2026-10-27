"""Tests for coldread.py (order 46/46b). Every test runs the pipeline with a fake model on temp
paths set by the `world` fixture: never the live READS / READS.jsonl, never a `claude` call."""
import builtins
import datetime
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coldread  # noqa: E402

LIVE_READS, LIVE_READS_DIR = coldread.READS, coldread.READS_DIR
REAL_FACTS = Path.home() / "helix" / "memory" / "facts"
TODAY = datetime.date(2026, 11, 1)

COLD_INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "skills": [],
             "slash_commands": [], "plugins": [{"name": "agents-md"}, {"name": "telemetry"}],
             "agents": ["claude"], "memory_paths": None, "model": "claude-opus-5-5",
             "apiKeySource": "none"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    facts = tmp_path / "facts"
    facts.mkdir()
    monkeypatch.setattr(coldread, "FACTS", facts)
    monkeypatch.setattr(coldread, "READS", tmp_path / "READS.jsonl")
    monkeypatch.setattr(coldread, "READS_DIR", tmp_path / "READS")
    return tmp_path


def fact(world, name, text=None):
    p = world / "facts" / name
    p.write_text(text if text is not None else f"# 🟢 {name}\n\nSomething about {name}.\n")
    return p


def some_facts(world, n=6):
    for i in range(1, n + 1):
        fact(world, f"{i}_thing_{i}.md")


class Fake:
    """A fake model. `pick(files)` → the file names stage 1 names; every call is recorded."""

    def __init__(self, pick=None, cost=0.10, init=None, stage2=None):
        self.pick = pick or (lambda files: files[:1])
        self.cost, self.init, self.stage2 = cost, init or COLD_INIT, stage2
        self.calls = []

    def __call__(self, system, prompt, workdir):
        self.calls.append((system, prompt))
        files = re.findall(r"^=== FILE: (\S+) ===$", prompt, re.M)
        if prompt.startswith(coldread.STAGE2_ASK):
            cands = json.loads(prompt.split("<candidates>\n", 1)[1].split("\n</candidates>", 1)[0])
            out = self.stage2 or {"verdict": {"file": cands[0]["file"] if cands else "",
                                              "quote": "q", "why": "w"}, "runners_up": []}
        else:
            out = {"candidates": [{"file": f, "quote": f"quote in {f}", "why": "because",
                                   "confidence": 0.9} for f in self.pick(files)]}
        return {"text": "```json\n" + json.dumps(out) + "\n```", "cost_usd": self.cost,
                "init": self.init}


def run(world, model, today=TODAY, seed=1):
    return coldread.run(model=model, today=today, seed=seed)


# --- 1. snapshot ---------------------------------------------------------------------------

def test_snapshot_excludes_fenced_names_and_never_opens_them(world, monkeypatch, tmp_path):
    some_facts(world, 3)
    for n in ("x_credentials.md", "y_accounts.md", ".env", "Z_CREDENTIAL_note.md"):
        fact(world, n, "SECRET")
    (world / "facts" / "sub").mkdir()
    fact(world, "sub/9_nested.md")
    real_open = builtins.open

    def guarded(file, *a, **k):
        assert not any(f in str(file).lower() for f in ("credential", "accounts", ".env")), file
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: guarded(self).read())
    snap = tmp_path / "snap"
    snap.mkdir()
    kept, excluded = coldread.snapshot(coldread.FACTS, snap)
    assert sorted(kept) == ["1_thing_1.md", "2_thing_2.md", "3_thing_3.md"]
    assert excluded == 3, ".env is not a .md file, so it is never even selected"
    assert sorted(p.name for p in snap.iterdir()) == sorted(kept)


def test_snapshot_counts_excluded_md_by_name(world, tmp_path):
    some_facts(world, 2)
    fact(world, "SECURITY_credentials.md", "SECRET")
    fact(world, "my.env.md", "SECRET")
    snap = tmp_path / "snap"
    snap.mkdir()
    kept, excluded = coldread.snapshot(coldread.FACTS, snap)
    assert len(kept) == 2 and excluded == 2


# --- 3. batching ---------------------------------------------------------------------------

def test_batches_keep_files_whole_and_each_file_in_exactly_one():
    texts = {f"{i:03d}.md": "x" * n for i, n in enumerate([50, 60, 30, 120, 10, 99, 1, 70])}
    batches = coldread.make_batches(texts, cap=100)
    flat = [n for b in batches for n in b]
    assert sorted(flat) == sorted(texts) and len(flat) == len(set(flat))
    assert flat == sorted(texts), "sorted by name"
    for b in batches:
        size = sum(len(texts[n]) for n in b)
        assert size <= 100 or len(b) == 1, (b, size)
    assert ["003.md"] in batches, "a file bigger than the cap rides alone"


def test_batches_of_real_shape_size():
    texts = {f"{i:03d}.md": "y" * 5000 for i in range(200)}
    batches = coldread.make_batches(texts, cap=coldread.BATCH_CAP)
    assert all(sum(len(texts[n]) for n in b) <= coldread.BATCH_CAP for b in batches)
    assert len(batches) == 5


# --- 2. plants -----------------------------------------------------------------------------

def test_there_are_at_least_three_plants_each_in_house_style():
    assert len(coldread.PLANTS) >= 3
    for p in coldread.PLANTS:
        text = coldread.plant_text(p, TODAY)
        assert text.startswith("# 🟢 "), text
        assert "2026-11-01" in text
        assert p["claim"] in text


def test_each_plant_names_a_contradicting_real_fact_that_exists():
    names = {p.name for p in REAL_FACTS.iterdir()}
    for p in coldread.PLANTS:
        assert p["contradicted_by"], p["id"]
        for f in p["contradicted_by"]:
            assert f in names, (p["id"], f)


def test_plant_number_is_above_the_highest_never_in_a_gap():
    for names, hi in ((["1_a.md", "2_b.md", "5_c.md", "box2_99_x.md", "body.md"], 5),
                      ([f"{i}_f.md" for i in range(1, 180) if i not in (27, 34, 42)], 179)):
        seen = set()
        for seed in range(200):
            n = coldread.plant_number(names, coldread.random.Random(seed))
            assert hi + 1 <= n <= hi + 9, (n, hi)
            seen.add(n)
        assert len(seen) > 1, "chosen at run time, not fixed"


def test_plant_file_name_uses_the_chosen_number(world):
    some_facts(world, 4)
    fact(world, "179_last.md")
    rec = run(world, Fake())
    num = int(rec["plant"]["file"].split("_", 1)[0])
    assert 180 <= num <= 188
    assert rec["plant"]["file"] not in {p.name for p in (world / "facts").iterdir()}


# --- positive controls ---------------------------------------------------------------------

def test_a_reader_that_names_the_plant_FOUND(world):
    some_facts(world)
    rec = run(world, Fake(pick=lambda files: [f for f in files if "thing" not in f] or files[:1]))
    assert rec["outcome"] == "DONE"
    assert rec["plant"]["found"] is True
    assert rec["plant"]["batch"] == 1
    line = coldread.coldread_line(today=TODAY)
    assert line.startswith("- coldread: 2026-11-01 · plant FOUND · top: "), line
    assert line.endswith(" · next 2026-12-01"), line
    md = (world / "READS" / "2026-11-01.md").read_text()
    assert "FOUND" in md and rec["plant"]["file"] in md


def test_a_reader_that_misses_the_plant_MISSED_and_red(world):
    some_facts(world)
    rec = run(world, Fake(pick=lambda files: [f for f in files if "thing" in f][:1]))
    assert rec["plant"]["found"] is False
    line = coldread.coldread_line(today=TODAY)
    assert line == ("- coldread: 🔴 2026-11-01 · plant MISSED (the read checked nothing)"
                    " · next 2026-12-01"), line


def test_found_counts_the_candidate_file_field_only():
    plant = "180_x.md"
    assert coldread.names_plant({"file": "180_x.md"}, plant)
    assert coldread.names_plant({"file": "`180_x.md`"}, plant)
    assert coldread.names_plant({"file": "facts/180_x.md"}, plant)
    assert not coldread.names_plant({"file": "18_y.md", "why": "contradicts 180_x.md"}, plant)


def test_the_plant_never_reaches_stage_2(world):
    some_facts(world)
    fake = Fake(pick=lambda files: files)            # every file named, the plant too
    rec = run(world, fake)
    plant = rec["plant"]["file"]
    assert rec["plant"]["found"] is True
    stage2 = [p for s, p in fake.calls if p.startswith(coldread.STAGE2_ASK)]
    assert len(stage2) == 1
    assert plant not in stage2[0]
    assert coldread.PLANTS[rec["plant"]["index"]]["claim"] not in stage2[0]


def test_stage_2_also_drops_candidates_whose_quote_or_why_mention_the_plant():
    plant = {"file": "180_x.md", "claim": "the sky is green"}
    cands = [{"file": "1_a.md", "quote": "a", "why": "b"},
             {"file": "2_b.md", "quote": "a", "why": "contradicts 180_x.md"},
             {"file": "3_c.md", "quote": "The sky is green.", "why": "b"},
             {"file": "180_x.md", "quote": "a", "why": "b"}]
    assert [c["file"] for c in coldread.stage2_candidates(cands, plant)] == ["1_a.md"]


def test_stage_2_gets_the_full_text_of_each_named_file(world):
    some_facts(world)
    fake = Fake(pick=lambda files: [f for f in files if f.startswith("3_")])
    run(world, fake)
    stage2 = [p for s, p in fake.calls if p.startswith(coldread.STAGE2_ASK)][0]
    assert "=== FILE: 3_thing_3.md ===\n# 🟢 3_thing_3.md\n\nSomething about 3_thing_3.md." in stage2


# --- cost cap ------------------------------------------------------------------------------

def test_budget_that_would_pass_the_cap_aborts_and_calls_nothing_more(world, monkeypatch):
    for i in range(1, 9):
        fact(world, f"{i}_big.md", "z" * 150)
    monkeypatch.setattr(coldread, "BATCH_CAP", 200)              # → 9 batches with the plant
    fake = Fake(cost=5.0)
    rec = run(world, fake)
    assert rec["outcome"] == "ABORTED" and rec["reason"] == "budget"
    assert len(fake.calls) == 2, "5 + 5 + 2.50 would pass 12: the third session never starts"
    assert rec["cost_usd"] == 10.0
    assert coldread.coldread_line(today=TODAY) == "- coldread: 🔴 2026-11-01 ABORTED (budget)"


def test_a_session_that_reports_more_than_the_cap_stops_the_run(world):
    some_facts(world)
    fake = Fake(cost=12.5)
    rec = run(world, fake)
    assert rec["outcome"] == "ABORTED" and len(fake.calls) == 1


def test_a_normal_run_is_seven_sessions_for_six_batches(world, monkeypatch):
    for i in range(1, 13):
        fact(world, f"{i}_f.md", "w" * 100)
    monkeypatch.setattr(coldread, "BATCH_CAP", 230)              # 13 files (12 + plant) → 6+1
    fake = Fake(cost=1.0)
    rec = run(world, fake)
    assert rec["outcome"] == "DONE" and rec["batches"] >= 6
    assert len(fake.calls) == rec["batches"] + 1
    assert rec["sessions"] == len(fake.calls)


# --- cold means cold -----------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [("tools", ["Bash"]),
                                       ("mcp_servers", [{"name": "claude.ai Claude Docs"}]),
                                       ("skills", ["deep-research"]),
                                       ("memory_paths", {"auto": "/x/memory/"})])
def test_a_warm_init_event_is_NOT_COLD_and_stops_the_run(world, key, value):
    some_facts(world)
    fake = Fake(init={**COLD_INIT, key: value})
    rec = run(world, fake)
    assert rec["outcome"] == "NOT_COLD"
    assert key in rec["reason"]
    assert len(fake.calls) == 1, "no further sessions"
    line = coldread.coldread_line(today=TODAY)
    assert line.startswith("- coldread: 🔴 2026-11-01 NOT COLD ("), line
    assert "🔴 NOT COLD" in (world / "READS" / "2026-11-01.md").read_text()


def test_cold_init_is_recorded_with_its_builtin_plugins(world):
    some_facts(world)
    rec = run(world, Fake())
    assert rec["init"]["tools"] == [] and rec["init"]["memory_paths"] is None
    assert rec["init"]["plugins"] == ["agents-md", "telemetry"]
    assert "agents-md" in (world / "READS" / "2026-11-01.md").read_text()


def test_check_cold_on_its_own():
    assert coldread.check_cold(COLD_INIT) == []
    assert coldread.check_cold(None) == ["no init event"]
    assert coldread.check_cold({**COLD_INIT, "tools": ["Read"], "skills": ["x"]}) == \
        ["tools: ['Read']", "skills: ['x']"]


def test_argv_is_cold_and_short_and_the_batch_goes_on_stdin(world, tmp_path):
    fact(world, "1_huge.md", "é" * 219_000)
    fact(world, "2_small.md")
    seen = []

    def spy(system, prompt, workdir):
        argv, stdin_path, cwd, env = coldread.session_command(system, prompt, workdir)
        assert Path(stdin_path).is_file() and Path(stdin_path).read_text() == prompt
        assert not any(Path(cwd).iterdir()), "an empty temp cwd"
        seen.append((argv, stdin_path, cwd, env, prompt))
        return Fake()(system, prompt, workdir)
    run(world, spy)
    assert seen
    for argv, stdin_path, cwd, env, prompt in seen:
        assert all(len(a.encode()) < 100_000 for a in argv), [len(a.encode()) for a in argv]
        assert argv[0] == str(Path.home() / ".local/bin/claude")
        assert argv[1] == "-p" and argv[2].startswith("--"), "no positional prompt"
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--safe-mode",
                     "--no-session-persistence"):
            assert flag in argv
        for flag, val in (("--model", "opus"), ("--tools", ""), ("--setting-sources", ""),
                          ("--max-budget-usd", "2.50"), ("--system-prompt", coldread.INSTRUCTION),
                          ("--output-format", "stream-json")):
            assert argv[argv.index(flag) + 1] == val, flag
        assert "--bare" not in argv
        assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / "claude/config")
        assert "ANTHROPIC_API_KEY" not in env
    assert any(len(p) > 219_000 for *_, p in seen), "the 220k batch went through stdin"


# --- failures ------------------------------------------------------------------------------

def test_unparseable_answer_is_FAILED_and_stops(world):
    some_facts(world)
    calls = []

    def bad(system, prompt, workdir):
        calls.append(1)
        return {"text": "I think everything is fine.", "cost_usd": 0.2, "init": COLD_INIT}
    rec = run(world, bad)
    assert rec["outcome"] == "FAILED" and len(calls) == 1
    assert coldread.coldread_line(today=TODAY).startswith("- coldread: 🔴 2026-11-01 FAILED (")


def test_parse_json_strips_fences_and_prose():
    assert coldread.parse_json('Sure:\n```json\n{"a": [1]}\n```\n') == {"a": [1]}
    with pytest.raises(ValueError):
        coldread.parse_json("no json here")


# --- output and status ---------------------------------------------------------------------

def test_one_md_and_one_jsonl_line_per_run_and_temp_dir_gone(world):
    some_facts(world)
    rec = run(world, Fake())
    lines = (world / "READS.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["date"] == "2026-11-01"
    assert not Path(rec["tmp"]).exists(), "the script removed its own temp snapshot"
    run(world, Fake())
    assert (world / "READS" / "2026-11-01_2.md").exists(), "a second read the same day never overwrites"


def test_status_shapes_none_yet_and_overdue(world):
    assert coldread.coldread_line(today=datetime.date(2026, 10, 3)) == \
        "- coldread: none yet · next 2026-11-01"
    some_facts(world)
    run(world, Fake())
    assert coldread.coldread_line(today=datetime.date(2026, 12, 7)) == \
        "- coldread: 🔴 overdue (last 2026-11-01)"
    st = coldread.status(today=TODAY)
    assert st["last"]["date"] == "2026-11-01" and st["next"] == "2026-12-01"


def test_top_line_is_short(world):
    some_facts(world)
    long = {"verdict": {"file": "2_thing_2.md", "quote": "x" * 300, "why": "w"}, "runners_up": []}
    run(world, Fake(pick=lambda files: files, stage2=long))
    line = coldread.coldread_line(today=TODAY)
    top = line.split("top: ", 1)[1].split(" · next")[0]
    assert top.startswith("2_thing_2.md — ")
    assert len(top.split(" — ", 1)[1]) <= 80


def test_dry_cli_never_touches_the_live_ledgers(world, monkeypatch, capsys):
    some_facts(world)
    monkeypatch.setattr(coldread, "READS", LIVE_READS)
    monkeypatch.setattr(coldread, "READS_DIR", LIVE_READS_DIR)
    before = LIVE_READS.read_bytes() if LIVE_READS.exists() else None
    out = world / "dry_out"
    out.mkdir()
    real_mkdtemp = coldread.tempfile.mkdtemp
    monkeypatch.setattr(coldread.tempfile, "mkdtemp", lambda *a, **k: str(out)
                        if k.get("prefix") == "coldread_dry_" else real_mkdtemp(*a, **k))
    assert coldread.main(["run", "--dry"]) == 0
    assert (out / "READS.jsonl").exists(), "the dry read went to its own temp dir"
    after = LIVE_READS.read_bytes() if LIVE_READS.exists() else None
    assert before == after
    assert "DRY" in capsys.readouterr().out


# --- the schedule --------------------------------------------------------------------------

def unit(name):
    return (HERE / "systemd" / name).read_text().splitlines()


def test_timer_is_monthly_on_the_first_at_ten_and_persistent():
    t = unit("helix-coldread.timer")
    assert "OnCalendar=*-*-01 10:00:00" in t and "Persistent=true" in t
    assert "WantedBy=timers.target" in t


def test_service_env_is_run_order_sh_s():
    s = unit("helix-coldread.service")
    run_order = (Path.home() / "helix/build_orders/run_order.sh").read_text()
    assert '--setenv=CLAUDE_CONFIG_DIR="$HOME/claude/config"' in run_order
    assert "Environment=CLAUDE_CONFIG_DIR=%h/claude/config" in s
    assert "UnsetEnvironment=ANTHROPIC_API_KEY" in s
    assert "StandardInput=null" in s
    assert "ExecStart=/usr/bin/python3 %h/helix/workspace/coldread/coldread.py run" in s
    assert not any(l.startswith("ExecStart=") and "--dry" in l for l in s)
    assert coldread.CONFIG_DIR == Path.home() / "claude/config"
    assert coldread.CLI == Path.home() / ".local/bin/claude"
