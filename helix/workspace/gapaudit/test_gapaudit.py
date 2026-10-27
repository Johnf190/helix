"""Tests for the gap audit (order 36, item 4): what the record OMITS.

Every test builds its own small Helix tree under tmp_path (ledgers, records, git repos with
fixed dates) and points the audit at it with `helix=`. Nothing here reads or writes the live
ledgers, except `test_the_live_paths_are_the_ones_the_order_names`, which only compares paths.
"""

import sys

sys.dont_write_bytecode = True

import hashlib
import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gapaudit as G  # noqa: E402

LOCAL = datetime.now().astimezone().tzinfo


def at(s):
    """A local wall-clock time, as an aware datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=LOCAL)


def git(repo, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True,
                          text=True, check=True).stdout.strip()


def commit(repo, subject, when):
    git(repo, "commit", "-q", "--allow-empty", "-m", subject, when=when)
    return git(repo, "rev-parse", "HEAD")


def jl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


PROMPT = "Run the nightly audit: `python3 ~/helix/workspace/seam/nightly.py`. Report every line."


@pytest.fixture
def tree(tmp_path):
    """A small Helix: one item of every kind, half of them covered, half orphans."""
    H = tmp_path / "helix"
    jl(H / "build_orders/runs/RUNS.jsonl", [
        {"order": "17", "started": "2026-09-25T22:52:25+01:00", "ended": "2026-09-25T23:16:14",
         "exit": 0, "unit": "helix-builder-17-a", "trigger": "chat:/build", "report_exists": False},
        {"order": "18", "started": "2026-09-29T22:03:07+01:00", "ended": "2026-09-29T23:08:00",
         "exit": 0, "unit": "helix-builder-18-a", "trigger": "chat:/build", "report_exists": True},
        {"queue": "q-1", "event": "start", "at": "2026-10-01T07:00:00Z", "pid": 1},
        {"queue": "q-1", "event": "order_start", "at": "2026-10-01T07:37:43Z", "order": "29",
         "unit": "helix-builder-29-a"},
        {"order": "29", "started": "2026-10-01T07:37:43Z", "ended": "2026-10-01T07:53:22Z",
         "exit": 0, "unit": "helix-builder-29-a", "trigger": "chat:/build queue q-1",
         "report_exists": True},
        {"queue": "q-1", "event": "order_end", "at": "2026-10-01T07:53:22Z", "order": "29",
         "unit": "helix-builder-29-a", "outcome": "halt", "last_line": "STOP"},
    ])
    write(H / "build_orders/reports/ORDER_18_report.md", "# 18\n")
    write(H / "build_orders/reports/ORDER_29_report.md", "# 29\n")
    write(H / "build_orders/reports/ORDER_17_report.md", "# 17, written by a later run\n")
    jl(H / "workspace/cycle/ATTEMPTS.jsonl", [
        {"scope": "helix/selftest", "kind": "run", "n": 1, "cmd": "true", "cwd": "/", "pid": 1,
         "ts": "2026-10-01T07:18:58Z", "seq": 1, "prev": "genesis"},
        {"scope": "crosscheck/attest", "kind": "seed", "n": 10, "cmd": "python3 run.py", "cwd": "/",
         "pid": 2, "ts": "2026-10-01T09:00:00Z", "seq": 2, "prev": "x"},
    ])
    jl(H / "workspace/cycle/TURNS.jsonl", [
        {"ts": "2026-10-01T03:30:34Z", "lane": "sched", "model": "opus", "cost_usd": 0.5},
        {"ts": "2026-10-01T11:00:00Z", "lane": "work", "model": "opus", "cost_usd": 0.5},
        {"ts": "2026-10-01T12:00:00Z", "lane": "chat", "model": "opus", "cost_usd": 0.5},
    ])
    write(H / "memory/schedules.json", json.dumps([
        {"name": "nightly audit", "prompt": PROMPT, "at": "04:30", "id": "3810a43ef3c7",
         "enabled": True, "last_run": "2026-10-01T04:30:19", "last_turn_id": "b4cebdbfb3a6"},
        {"name": "never ran", "prompt": "x", "at": "05:00", "id": "000000000000",
         "enabled": True, "last_run": None, "last_turn_id": None}]))
    # 03:30Z on 2026-10-01 is 04:30 local in BST; the log heading is local time.
    sched_local = datetime.fromisoformat("2026-10-01T03:30:34+00:00").astimezone(LOCAL)
    write(H / "memory/sessions/log.md",
          "## 2026-09-03 12:40\n**You:** hi\n**ODIN:** hello\n"
          f"\n## {sched_local:%Y-%m-%d %H:%M}\n**You:** {PROMPT}\n**ODIN:** ran clean\n"
          "\n## 2026-09-30 10:00\n**You:** what about helix-builder-17-a?\n**ODIN:** it died\n")
    write(H / "memory/facts/001_x.md", "# a fact\n")
    (H / "memory/archive").mkdir(parents=True)
    jl(H / "workspace/catches/CATCHES.jsonl", [{"n": 1, "what": "nothing"}])
    jl(H / "workspace/seam/registry/REGATE_LEDGER.jsonl", [{"entry": "0000"}])

    repo = H / "workspace/alpha"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    h_old = commit(repo, "before the window", "2026-09-01T10:00:00+01:00")
    h_cov = commit(repo, "covered by a fact", "2026-09-28T10:00:00+01:00")
    h_orph = commit(repo, "nobody wrote this down", "2026-09-29T10:00:00+01:00")
    write(H / "memory/facts/002_y.md", f"Odin: the fix is in `{h_cov[:7]}`.\n")
    bo = H / "build_orders"
    git(bo, "init", "-q", "-b", "main")
    h_bo = commit(bo, "Work order 18", "2026-09-29T21:00:00+01:00")
    write(H / "build_orders/reports/ORDER_18_report.md", f"# 18\ncommit {h_bo}\n")
    return dict(H=H, h_old=h_old, h_cov=h_cov, h_orph=h_orph, h_bo=h_bo)


def run(tree, since="2026-09-15", until="2026-10-02", **kw):
    return G.audit(tree["H"], G.parse_when(since), G.parse_when(until, end=True), **kw)


def by_id(res):
    return {i["id"]: i for i in res["items"]}


# ------------------------------------------------------------------ each source is parsed

def test_builder_runs_one_item_per_unit_queue_events_folded_in(tree):
    items = [i for i in run(tree)["items"] if i["source"] == "runs"]
    assert sorted(i["id"] for i in items) == ["run:helix-builder-17-a", "run:helix-builder-18-a",
                                             "run:helix-builder-29-a"]
    i29 = by_id(run(tree))["run:helix-builder-29-a"]
    assert datetime.fromisoformat(i29["time"]) == datetime(2026, 10, 1, 7, 37, 43, tzinfo=timezone.utc)
    assert "order 29" in i29["desc"]


def test_attempts_are_parsed_with_scope_and_seq(tree):
    items = by_id(run(tree))
    assert items["attempt:1"]["desc"].startswith("helix/selftest seq 1")
    assert items["attempt:2"]["source"] == "attempts"


def test_commits_come_from_every_workspace_repo_and_build_orders(tree):
    items = by_id(run(tree))
    ids = [k for k in items if k.startswith("commit:")]
    assert f"commit:alpha@{tree['h_cov'][:12]}" in ids
    assert f"commit:alpha@{tree['h_orph'][:12]}" in ids
    assert f"commit:build_orders@{tree['h_bo'][:12]}" in ids
    assert f"commit:alpha@{tree['h_old'][:12]}" not in ids, "before --since: outside the window"


def test_the_window_end_is_inclusive_of_the_until_day(tree):
    ids = by_id(run(tree, since="2026-09-29", until="2026-09-29"))
    assert f"commit:alpha@{tree['h_orph'][:12]}" in ids
    assert f"commit:alpha@{tree['h_cov'][:12]}" not in ids


def test_scheduler_runs_come_from_last_run(tree):
    items = by_id(run(tree))
    sched = [i for i in items.values() if i["source"] == "schedules"]
    assert [i["id"] for i in sched] == ["sched:3810a43ef3c7@2026-10-01T04:30:19"]


def test_work_lane_jobs_are_the_work_and_sched_turns_only(tree):
    lanes = sorted(i["id"] for i in run(tree)["items"] if i["source"] == "worklane")
    assert lanes == ["turn:sched@2026-10-01T03:30:34Z", "turn:work@2026-10-01T11:00:00Z"]


# ------------------------------------------------------------------ each coverage rule

def test_a_commit_named_by_7_hex_chars_is_covered(tree):
    i = by_id(run(tree))[f"commit:alpha@{tree['h_cov'][:12]}"]
    assert i["covered_by"] and "002_y.md" in i["covered_by"]


def test_a_commit_nobody_named_is_an_orphan(tree):
    assert by_id(run(tree))[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"] is None


def test_six_hex_chars_are_not_enough(tree):
    write(tree["H"] / "memory/facts/003_z.md", f"see {tree['h_orph'][:6]}\n")
    assert by_id(run(tree))[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"] is None


def test_a_hash_inside_a_longer_hex_string_does_not_count(tree):
    write(tree["H"] / "memory/facts/003_z.md", f"sha256 {tree['h_orph'][:7]}{'0' * 57}\n")
    assert by_id(run(tree))[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"] is None


def test_quoting_the_audits_own_output_covers_nothing(tree):
    """A report that pastes the orphan list, or a log entry that repeats the nightly's gaps line,
    is a quote of the audit, not a record of the work. Ids in the audit's own form don't count."""
    res = run(tree)
    orphans = [i for i in res["items"] if not i["covered_by"]]
    assert orphans
    write(tree["H"] / "build_orders/reports/ORDER_77_report.md", G.render(res) + "\n")
    write(tree["H"] / "memory/facts/004_n.md",
          G.gaps_line(tree["H"], now=at("2026-10-02T04:30:00")) + "\n")
    after = by_id(run(tree))
    assert [i["id"] for i in orphans if after[i["id"]]["covered_by"]] == []


def test_a_plain_hash_next_to_a_quoted_id_still_counts(tree):
    h = tree["h_orph"]
    write(tree["H"] / "memory/facts/004_n.md",
          f"orphan commit:alpha@{h[:12]} — recorded now: {h[:7]} was the hotfix\n")
    assert "004_n.md" in by_id(run(tree))[f"commit:alpha@{h[:12]}"]["covered_by"]


def test_a_full_hash_in_a_report_covers_it(tree):
    i = by_id(run(tree))[f"commit:build_orders@{tree['h_bo'][:12]}"]
    assert "ORDER_18_report.md" in i["covered_by"]


def test_a_run_is_covered_by_its_own_report(tree):
    assert "runs-own-report" in by_id(run(tree))["run:helix-builder-18-a"]["covered_by"]
    assert "runs-own-report" in by_id(run(tree))["run:helix-builder-29-a"]["covered_by"]


def test_a_run_that_wrote_no_report_is_not_covered_by_a_later_runs_report(tree):
    """17's report exists, but this run said report_exists: false: it is not ITS report."""
    write(tree["H"] / "memory/sessions/log.md", "## 2026-09-30 10:00\n**You:** hi\n")
    assert by_id(run(tree))["run:helix-builder-17-a"]["covered_by"] is None


def test_a_run_named_by_its_unit_in_the_log_is_covered(tree):
    assert "log.md" in by_id(run(tree))["run:helix-builder-17-a"]["covered_by"]


@pytest.mark.parametrize("text,covered", [("Order 17 stopped at its first step.", True),
                                           ("see ORDER_17_review_odin.md", True),
                                           ("order-17: hook fix", True),
                                           ("order 17b is next", False),
                                           ("order 170 and orders 17-19", False)])
def test_a_run_named_by_its_order_number_is_covered(tree, text, covered):
    write(tree["H"] / "memory/sessions/log.md", f"## 2026-09-30 10:00\n**You:** hi\n**ODIN:** {text}\n")
    got = by_id(run(tree))["run:helix-builder-17-a"]["covered_by"]
    assert bool(got) is covered, got


def test_an_attempt_needs_its_scope_and_seq_on_one_line(tree):
    write(tree["H"] / "memory/facts/003_z.md", "helix/selftest\nseq 1\n")
    assert by_id(run(tree))["attempt:1"]["covered_by"] is None
    write(tree["H"] / "memory/facts/003_z.md", "`attempt.py run --scope helix/selftest` -> **seq 1**\n")
    assert "003_z.md" in by_id(run(tree))["attempt:1"]["covered_by"]
    assert by_id(run(tree))["attempt:2"]["covered_by"] is None


def test_seq_1_does_not_cover_seq_12(tree):
    write(tree["H"] / "memory/facts/003_z.md", "helix/selftest seq 12\n")
    assert by_id(run(tree))["attempt:1"]["covered_by"] is None


def test_a_scheduler_run_is_covered_by_its_log_entry(tree):
    i = by_id(run(tree))["sched:3810a43ef3c7@2026-10-01T04:30:19"]
    assert "schedule-logged" in i["covered_by"]


def test_a_scheduler_run_with_no_matching_prompt_in_the_log_is_an_orphan(tree):
    log = tree["H"] / "memory/sessions/log.md"
    log.write_text(log.read_text().replace(PROMPT, "something else"))
    assert by_id(run(tree))["sched:3810a43ef3c7@2026-10-01T04:30:19"]["covered_by"] is None


def test_a_work_lane_turn_is_covered_by_a_log_heading_within_two_minutes(tree):
    items = by_id(run(tree))
    assert "worklane-logged" in items["turn:sched@2026-10-01T03:30:34Z"]["covered_by"]
    assert items["turn:work@2026-10-01T11:00:00Z"]["covered_by"] is None


def test_catches_regate_ledger_archive_and_reports_are_records(tree):
    H = tree["H"]
    for i, path in enumerate([H / "workspace/catches/CATCHES.jsonl",
                              H / "workspace/seam/registry/REGATE_LEDGER.jsonl",
                              H / "memory/archive/090_old.md",
                              H / "build_orders/reports/ORDER_99_report.md"]):
        h = commit(H / "workspace/alpha", f"extra {i}", f"2026-09-30T1{i}:00:00+01:00")
        with open(path, "a") as f:
            f.write(json.dumps({"note": f"fixed in {h[:9]}"}) + "\n")
        assert by_id(run(tree))[f"commit:alpha@{h[:12]}"]["covered_by"], path


def test_rules_live_in_a_json_file_and_each_has_a_reason():
    rules = json.loads((HERE / "gapaudit_rules.json").read_text())["rules"]
    assert {r["id"] for r in rules} == {"runs-own-report", "worklane-logged", "schedule-logged"}
    for r in rules:
        assert r["reason"].strip() and r["source"] in G.SOURCES and r["kind"] in G.RULE_KINDS


def test_a_rule_without_a_reason_is_not_applied_and_is_said(tree, tmp_path):
    rules = json.loads((HERE / "gapaudit_rules.json").read_text())
    for r in rules["rules"]:
        if r["id"] == "runs-own-report":
            r["reason"] = " "
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules))
    res = run(tree, rules_path=p)
    assert by_id(res)["run:helix-builder-18-a"]["covered_by"] is None
    assert any("runs-own-report" in w for w in res["problems"])
    assert "runs-own-report" in G.render(res)


def test_a_missing_rules_file_is_said_and_nothing_crashes(tree, tmp_path):
    res = run(tree, rules_path=tmp_path / "nope.json")
    assert any("rules" in w for w in res["problems"])
    assert by_id(res)[f"commit:alpha@{tree['h_cov'][:12]}"]["covered_by"]


# ------------------------------------------------------------------ the opt-out

def test_the_opt_out_is_honoured_by_id(tree):
    jl(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
       [{"id": "turn:work@2026-10-01T11:00:00Z", "why": "a test turn", "by": "the user"}])
    i = by_id(run(tree))["turn:work@2026-10-01T11:00:00Z"]
    assert i["covered_by"] == "no record needed: a test turn"


def test_the_opt_out_takes_a_commit_hash_prefix(tree):
    jl(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
       [{"id": tree["h_orph"][:8], "why": "typo fix", "by": "order 36"}])
    assert "typo fix" in by_id(run(tree))[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"]


def test_an_opt_out_with_no_why_is_ignored_and_said(tree):
    jl(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
       [{"id": tree["h_orph"][:8], "why": ""}])
    with open(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl", "a") as f:
        f.write("{not json\n")
    res = run(tree)
    assert by_id(res)[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"] is None
    assert sum("NO_RECORD_NEEDED" in w for w in res["problems"]) == 2


def test_a_short_hash_prefix_in_the_opt_out_matches_nothing(tree):
    jl(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
       [{"id": tree["h_orph"][:5], "why": "too short"}])
    assert by_id(run(tree))[f"commit:alpha@{tree['h_orph'][:12]}"]["covered_by"] is None


# ------------------------------------------------------------------ missing and broken sources

@pytest.mark.parametrize("src,rel", [("runs", "build_orders/runs/RUNS.jsonl"),
                                     ("attempts", "workspace/cycle/ATTEMPTS.jsonl"),
                                     ("schedules", "memory/schedules.json"),
                                     ("worklane", "workspace/cycle/TURNS.jsonl")])
def test_a_missing_source_is_reported_as_source_unavailable(tree, src, rel):
    (tree["H"] / rel).unlink()
    res = run(tree)
    assert src in res["unavailable"]
    assert f"{src}: source unavailable" in G.render(res)
    assert not [i for i in res["items"] if i["source"] == src]


def test_no_repos_at_all_is_source_unavailable(tmp_path):
    res = G.audit(tmp_path, G.parse_when("2026-09-15"), G.parse_when("2026-10-02", end=True))
    assert set(res["unavailable"]) == set(G.SOURCES)
    assert res["items"] == []
    assert "source unavailable" in G.render(res)


def test_a_missing_record_source_is_said(tree):
    (tree["H"] / "workspace/catches/CATCHES.jsonl").unlink()
    res = run(tree)
    assert any("CATCHES.jsonl" in w for w in res["problems"])


def test_corrupt_lines_are_skipped_and_counted_never_raised(tree):
    for rel in ("build_orders/runs/RUNS.jsonl", "workspace/cycle/ATTEMPTS.jsonl",
                "workspace/cycle/TURNS.jsonl"):
        with open(tree["H"] / rel, "a") as f:
            f.write("{broken\n[1, 2]\n{\"ts\": 5, \"lane\": \"work\"}\n")
    (tree["H"] / "memory/schedules.json").write_text("{nope")
    res = run(tree)
    assert "schedules" in res["unavailable"]
    assert any("RUNS.jsonl" in w and "unreadable" in w for w in res["problems"])
    assert by_id(res)["run:helix-builder-18-a"]


def test_a_broken_repo_does_not_stop_the_others(tree):
    bad = tree["H"] / "workspace/broken"
    (bad / ".git").mkdir(parents=True)
    res = run(tree)
    assert by_id(res)[f"commit:alpha@{tree['h_cov'][:12]}"]
    assert any("broken" in w for w in res["problems"])


def test_the_audit_writes_nothing(tree):
    """Run on a tree with every file and directory read-only: no error, and every byte the same."""
    H = tree["H"]

    def snap():
        return sorted((str(p.relative_to(H)), hashlib.sha256(p.read_bytes()).hexdigest())
                      for p in H.rglob("*") if p.is_file())
    before = snap()
    paths = sorted(H.rglob("*"), key=lambda p: -len(p.parts))
    for p in paths + [H]:
        p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        res = run(tree)
        assert res["items"] and not res["unavailable"]
        G.gaps_line(H, now=at("2026-10-02T04:30:00"))
        assert snap() == before
    finally:
        for p in [H] + paths:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)


# ------------------------------------------------------------------ output

def test_render_groups_orphans_by_source_one_line_each(tree):
    res = run(tree)
    out = G.render(res)
    orphans = [i for i in res["items"] if not i["covered_by"]]
    assert out.splitlines()[0].startswith(f"gapaudit 2026-09-15 → 2026-10-02: {len(res['items'])} "
                                          f"work items, {len(res['items']) - len(orphans)} covered, "
                                          f"{len(orphans)} orphans")
    for i in orphans:
        assert sum(1 for l in out.splitlines() if l.startswith(f"- {i['id']} ")) == 1
    assert out.index("## commits") < out.index(f"- commit:alpha@{tree['h_orph'][:12]}")


def test_cli_text_and_json(tree, capsys):
    assert G.main(["run", "--since", "2026-09-15", "--until", "2026-10-02",
                   "--helix", str(tree["H"])]) == 0
    assert "orphans" in capsys.readouterr().out
    assert G.main(["run", "--since", "2026-09-15", "--json", "--helix", str(tree["H"])]) == 0
    d = json.loads(capsys.readouterr().out)
    assert {"items", "unavailable", "problems", "since", "until"} <= set(d)
    assert any(i["id"] == f"commit:alpha@{tree['h_orph'][:12]}" and i["covered_by"] is None
               for i in d["items"])


def test_cli_as_a_script(tree):
    r = subprocess.run([sys.executable, str(HERE / "gapaudit.py"), "run", "--since", "2026-09-15",
                        "--until", "2026-10-02", "--helix", str(tree["H"])],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "orphans" in r.stdout, r.stderr


def test_a_bad_date_is_an_error_not_a_traceback(capsys):
    assert G.main(["run", "--since", "last tuesday"]) == 2
    assert "since" in capsys.readouterr().err


# ------------------------------------------------------------------ the nightly line

def test_the_gaps_line_counts_new_and_lists_the_oldest_three(tree):
    line = G.gaps_line(tree["H"], now=at("2026-10-02T04:30:00"))
    res = G.audit(tree["H"], at("2026-09-25T04:30:00"), at("2026-10-02T04:30:00"))
    t = lambda i: datetime.fromisoformat(i["time"])  # noqa: E731
    orphans = sorted((i for i in res["items"] if not i["covered_by"]), key=t)
    new = [i for i in orphans if t(i) >= at("2026-10-01T04:30:00")]
    assert len(orphans) >= 3 and 0 < len(new) < len(orphans)
    assert line.startswith(f"- gaps: {len(orphans)} orphans in the last 7 days "
                           f"(new since yesterday: {len(new)})"), line
    first3 = line.split(" · first 3 by age: ")[1].split(", ")
    assert len(first3) == 3
    assert first3[0].startswith(orphans[0]["id"])


def test_the_gaps_line_with_no_orphans(tree):
    jl(tree["H"] / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
       [{"id": i["id"], "why": "t"} for i in run(tree)["items"]])
    assert G.gaps_line(tree["H"], now=at("2026-10-02T04:30:00")) == \
        "- gaps: 0 orphans in the last 7 days (new since yesterday: 0)"


def test_the_gaps_line_names_unavailable_sources(tree):
    (tree["H"] / "workspace/cycle/ATTEMPTS.jsonl").unlink()
    assert "attempts: source unavailable" in G.gaps_line(tree["H"], now=at("2026-10-02T04:30:00"))


@pytest.mark.parametrize("helix", ["file", "missing"])
def test_the_gaps_line_never_raises(tmp_path, helix):
    p = tmp_path / "h"
    if helix == "file":
        p.write_text("x")
    line = G.gaps_line(p, now=at("2026-10-02T04:30:00"))
    assert line.startswith("- gaps: ")


def test_the_gaps_line_reports_an_internal_error_instead_of_raising(tree, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(G, "audit", boom)
    assert G.gaps_line(tree["H"]) == "- gaps: 🔴 audit failed (RuntimeError: kaboom)"


# ------------------------------------------------------------------ the seeded orphan (Part C in small)

def test_a_seeded_commit_is_caught_then_cleared_by_a_record(tree):
    h = commit(tree["H"] / "workspace/alpha", "seeded, unrecorded", "2026-10-01T15:00:00+01:00")
    iid = f"commit:alpha@{h[:12]}"
    assert by_id(run(tree))[iid]["covered_by"] is None
    with open(tree["H"] / "memory/sessions/log.md", "a") as f:
        f.write(f"\n## 2026-10-01 15:05\n**You:** recorded {h[:7]}\n**ODIN:** ok\n")
    assert "log.md" in by_id(run(tree))[iid]["covered_by"]


# ------------------------------------------------------------------ the live layout

def test_the_live_paths_are_the_ones_the_order_names():
    H = Path.home() / "helix"
    p = G.paths(H)
    assert p["optout"] == H / "workspace/cycle/NO_RECORD_NEEDED.jsonl"   # walled since order 27
    assert p["runs"] == H / "build_orders/runs/RUNS.jsonl"
    assert p["attempts"] == H / "workspace/cycle/ATTEMPTS.jsonl"
    assert p["worklane"] == H / "workspace/cycle/TURNS.jsonl"
    assert p["schedules"] == H / "memory/schedules.json"
    assert G.HELIX == H and G.RULES == HERE / "gapaudit_rules.json"
