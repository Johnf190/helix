"""Tests for propose.py (order 52, item 21). Temp dirs and a fake model only: never the live
ledgers, never a real `claude` call."""
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import propose  # noqa: E402

TODAY = datetime.date(2026, 10, 4)
COLD = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "skills": [],
        "memory_paths": None, "plugins": ["agents-md", "telemetry"]}


# --------------------------------------------------------------------------- fixtures

def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def catch(n, what="a thing was caught", where="somewhere"):
    return {"n": n, "date": "2026-09-30", "stage": "build", "caught_by": "builder",
            "where": where, "what": what, "fixed_in": "order 99"}


@pytest.fixture
def helix(tmp_path):
    """An empty helix tree: every source present, every source empty."""
    h = tmp_path / "helix"
    for rel in ("workspace/catches/CATCHES.jsonl", "workspace/coldread/READS.jsonl",
                "workspace/veto/VETOES.jsonl", "workspace/seatswap/SWAPS.jsonl",
                "workspace/replay/REPLAYS.jsonl", "workspace/canary/CANARY.jsonl",
                "workspace/regate/refusals.log", "workspace/sealgate/refusals.log",
                "memory/sessions/log.md"):
        (h / rel).parent.mkdir(parents=True, exist_ok=True)
        (h / rel).write_text("")
    (h / "build_orders" / "reports").mkdir(parents=True)
    return h


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "ledgers"
    d.mkdir()
    return d


@pytest.fixture
def bulk(tmp_path):
    return tmp_path / "bulk"


def plant_three(helix):
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(1), catch(2), catch(3)])


def answer(*items, cost=0.10, init=COLD, error=None):
    def model(system, prompt, workdir):
        model.calls.append((system, prompt, workdir))
        out = {"text": json.dumps({"proposals": list(items)}), "cost_usd": cost, "init": init}
        if error:
            out["error"] = error
        return out
    model.calls = []
    return model


def item(evidence, title="tighten X", friction="ADDS-FRICTION", lane="backend",
         moves="caught-before-vs-after", **kw):
    it = {"title": title, "evidence": evidence, "change": "do X", "acceptance": "X is red",
          "moves": moves, "friction": friction, "lane": lane}
    it.update(kw)
    return it


def rows(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()] \
        if Path(path).exists() else []


def draft(helix, home, bulk, model, **kw):
    return propose.draft(model=model, helix=helix, home=home, bulk=bulk, today=TODAY, **kw)


# --------------------------------------------------------------------------- harvest

def ids(h):
    return [e["id"] for e in h["events"]]


def test_harvest_drops_retracted_catches_and_retraction_lines(helix):
    jsonl(helix / "workspace/catches/CATCHES.jsonl",
          [catch(1), catch(2), catch(3),
           {"n": 4, "date": "2026-09-30", "kind": "retraction", "retracts": [2], "why": "false"}])
    h = propose.harvest(helix)
    assert ids(h) == ["catch:1", "catch:3"]
    assert "a thing was caught" in h["events"][0]["text"]


def test_harvest_stop_report_and_done_report(helix):
    r = helix / "build_orders/reports"
    (r / "ORDER_47_report.md").write_text("# ORDER 47 report\n\n## 🔴 STOPPED before committing: P9\n"
                                         "body\n\nSTOP\n")
    (r / "ORDER_40_report.md").write_text("# ORDER 40 report: STOPPED at pre-flight\n\nwhy\n\nSTOP\n")
    (r / "ORDER_41_report.md").write_text("# ORDER 41 report\n\n## 🔴 STOP old\n\nDONE\n")
    (r / "ORDER_42_report.md").write_text("# ORDER 42\n\nnot STOP\n")
    h = propose.harvest(helix)
    ev = {e["id"]: e["text"] for e in h["events"]}
    assert sorted(ev) == ["report:40:STOP", "report:47:STOP"]
    assert ev["report:47:STOP"] == "## 🔴 STOPPED before committing: P9"
    assert ev["report:40:STOP"] == "# ORDER 40 report: STOPPED at pre-flight"


def test_harvest_flagged_and_unchecked_checks(helix):
    r = helix / "build_orders/reports"
    (r / "ORDER_28_report.check.json").write_text(json.dumps(
        {"verdict": "FLAGGED", "claims": [{"claim": "commit x 1", "ok": True},
                                          {"claim": "tests a", "ok": False, "said": "passed 3",
                                           "witness": "passed 2"}]}))
    (r / "ORDER_50_report.check.json").write_text(json.dumps({"verdict": "UNCHECKED", "claims": []}))
    (r / "ORDER_51_report.check.json").write_text(json.dumps({"verdict": "VERIFIED", "claims": []}))
    ev = {e["id"]: e["text"] for e in propose.harvest(helix)["events"]}
    assert sorted(ev) == ["check:28:FLAGGED", "check:50:UNCHECKED"]
    assert "tests a" in ev["check:28:FLAGGED"] and "passed 2" in ev["check:28:FLAGGED"]


def test_harvest_coldread_top_and_runners_up(helix):
    c = helix / "workspace/coldread"
    jsonl(c / "READS.jsonl", [{"date": "2026-10-03", "outcome": "DONE", "report": "2026-10-03.md",
                               "top": {"file": "48_x.md", "quote": "1/(1-r) = 50.75"}}])
    (c / "READS").mkdir()
    (c / "READS/2026-10-03.md").write_text(
        "# Cold read\n\n## Verdict\n\nx\n\n## Runners-up\n\n"
        "1. `71_a.md` — \"fish\" — contradicts itself\n"
        "2. `20_b.md` — \"ended\n11 days before\" — 30 days, not 11\n\n"
        "## Stage-1 candidates\n\n- batch 1 · `9_plant.md`\n")
    ev = {e["id"]: e["text"] for e in propose.harvest(helix)["events"]}
    assert sorted(ev) == ["coldread:2026-10-03:0", "coldread:2026-10-03:1", "coldread:2026-10-03:2"]
    assert "48_x.md" in ev["coldread:2026-10-03:0"] and "50.75" in ev["coldread:2026-10-03:0"]
    assert "11 days before" in ev["coldread:2026-10-03:2"] and "\n" not in ev["coldread:2026-10-03:2"]


def test_harvest_canary_miss_and_wrongly_convicted_acquittal(helix):
    jsonl(helix / "workspace/canary/CANARY.jsonl", [
        {"date": "2026-10-03", "cases": {"off-ledger": "caught", "recorded-fact": "acquitted"}},
        {"date": "2026-10-04", "cases": {"off-ledger": "missed", "recorded-fact": "refused",
                                         "silent-edit": "error"}}])
    assert sorted(ids(propose.harvest(helix))) == ["canary:2026-10-04:off-ledger",
                                                   "canary:2026-10-04:recorded-fact"]


def test_harvest_veto_swap_replay(helix):
    w = helix / "workspace"
    jsonl(w / "veto/VETOES.jsonl", [
        {"seq": 1, "kind": "veto", "outcome": "BREAK_REPRODUCED", "seal": "a"},
        {"seq": 2, "kind": "veto", "outcome": "BREAK_CONFIRMED", "seal": "b/PREREG.md",
         "judge": {"why": "the tamper passed"}}])
    jsonl(w / "seatswap/SWAPS.jsonl", [{"seq": 1, "kind": "swap", "outcome": "AGREE"},
                                       {"seq": 2, "kind": "swap", "outcome": "DISAGREE", "pack": "p"}])
    jsonl(w / "replay/REPLAYS.jsonl", [{"name": "p", "result": "MATCH"},
                                       {"name": "p", "result": "TAMPERED"},
                                       {"name": "q", "result": "MISMATCH"}])
    h = propose.harvest(helix)
    assert sorted(ids(h)) == ["replay:2", "replay:3", "swap:2", "veto:2"]
    assert "b/PREREG.md" in {e["id"]: e["text"] for e in h["events"]}["veto:2"]


def test_harvest_refusals_live_targets_only(helix):
    (helix / "workspace/regate/refusals.log").write_text(
        "2026-09-23T22:20:36 exit=4 verb=check target=/tmp/x/registry.json failed=GOALPOST\n"
        "2026-09-24T10:00:00 exit=4 verb=check target=/home/h/seam/registry/registry.json failed=R5\n"
        "2026-09-24T10:00:01 exit=4 verb=check target=/a/fixtures/registry.json failed=R5\n")
    (helix / "workspace/sealgate/refusals.log").write_text(
        "2026-09-23T21:29:20 exit=3 verb=token prereg=/var/tmp/p1/PREREG.md failed=P1\n")
    h = propose.harvest(helix)
    assert ids(h) == ["refusal:regate:2"]
    assert "failed=R5" in h["events"][0]["text"]


def test_harvest_uses_the_metrics_own_refusal_rule():
    import correction_rate
    assert propose.REFUSAL is correction_rate.REFUSAL
    assert propose.TEST_TARGET is correction_rate.TEST_TARGET


def test_harvest_missing_source_is_noted_not_raised(helix):
    (helix / "workspace/veto/VETOES.jsonl").unlink()
    h = propose.harvest(helix)
    assert "veto" in h["unavailable"] and h["events"] == []


def test_harvest_is_deterministic(helix):
    plant_three(helix)
    assert propose.snapshot_bytes(propose.harvest(helix)) == \
        propose.snapshot_bytes(propose.harvest(helix))


# --------------------------------------------------------------------------- draft

def test_draft_valid_item_goes_to_the_queue(helix, home, bulk):
    plant_three(helix)
    model = answer(item(["catch:1", "catch:2"]))
    b = draft(helix, home, bulk, model)
    assert b["outcome"] == "DONE"
    p = rows(home / "PROPOSALS.jsonl")
    assert [r["id"] for r in p] == ["P-20261004-1"]
    assert p[0]["status"] == "pending" and p[0]["evidence"] == ["catch:1", "catch:2"]
    assert b["proposals"] == ["P-20261004-1"] and b["adds"] == 1 and b["removes"] == 0
    assert "flag" not in b
    assert len(model.calls) == 1, "one session"


def test_draft_brief_rules_are_in_the_prompt():
    for words in ("at least one proposal should tighten", "need for alice", "json only"):
        assert words in propose.BRIEF.lower(), words
    assert propose.RUN_CAP_USD == 3.00


def test_draft_unknown_ids_only_go_to_leads(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:99", "nope:1"])))
    assert rows(home / "PROPOSALS.jsonl") == []
    leads = rows(home / "LEADS.jsonl")
    assert len(leads) == 1 and leads[0]["title"] == "tighten X"
    assert b["leads"] == 1 and b["accepted"] == 0


def test_draft_partly_unknown_ids_rejected_never_repaired(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:1", "catch:99"])))
    assert rows(home / "PROPOSALS.jsonl") == []
    assert b["rejected"] and "catch:99" in b["rejected"][0]["why"]


def test_draft_missing_moves_rejected(helix, home, bulk):
    plant_three(helix)
    bad = item(["catch:1"])
    del bad["moves"]
    b = draft(helix, home, bulk, answer(bad, item(["catch:2"], friction="NOPE")))
    assert rows(home / "PROPOSALS.jsonl") == []
    assert [r["title"] for r in b["rejected"]] == ["tighten X", "tighten X"]
    assert "moves" in b["rejected"][0]["why"] and "friction" in b["rejected"][1]["why"]


def test_draft_repeat_rejected(helix, home, bulk):
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(n) for n in range(1, 8)])
    draft(helix, home, bulk, answer(item(["catch:1", "catch:2"])))
    b = draft(helix, home, bulk, answer(item(["catch:2"], title="again"), item(["catch:3"])))
    assert [r["why"] for r in b["rejected"]] == ["repeat"]
    assert [r["evidence"] for r in rows(home / "PROPOSALS.jsonl")] == [["catch:1", "catch:2"],
                                                                       ["catch:3"]]


def test_draft_all_removes_batch_is_flagged_not_refused(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:1"], friction="REMOVES-FRICTION"),
                                        item(["catch:2"], friction="REMOVES-FRICTION")))
    assert b["outcome"] == "DONE" and b["flag"] == "friction-only"
    assert b["adds"] == 0 and b["removes"] == 2
    assert len(rows(home / "PROPOSALS.jsonl")) == 2, "recorded, not refused"


def test_draft_alice_lane_counted(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:1"], lane="alice")))
    assert b["for_alice"] == 1


def test_draft_not_cold_stops_nothing_queued(helix, home, bulk):
    plant_three(helix)
    warm = dict(COLD, tools=["Bash"])
    b = draft(helix, home, bulk, answer(item(["catch:1"]), init=warm))
    assert b["outcome"] == "NOT_COLD" and "Bash" in b["reason"]
    assert rows(home / "PROPOSALS.jsonl") == [] and rows(home / "LEADS.jsonl") == []


def test_draft_budget_cap_aborts(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:1"]), cost=3.01))
    assert b["outcome"] == "ABORTED" and b["reason"] == "budget"
    assert rows(home / "PROPOSALS.jsonl") == []


def test_draft_session_cap_over_run_cap_aborts_before_any_session(helix, home, bulk, monkeypatch):
    plant_three(helix)
    monkeypatch.setattr(propose, "RUN_CAP_USD", 1.0)
    model = answer(item(["catch:1"]))
    b = draft(helix, home, bulk, model)
    assert b["outcome"] == "ABORTED" and model.calls == []


def test_draft_failed_session(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(error="error_max_turns"))
    assert b["outcome"] == "FAILED" and rows(home / "PROPOSALS.jsonl") == []


def test_draft_fewer_than_three_new_events_skips_and_spends_nothing(helix, home, bulk):
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(1), catch(2)])
    model = answer(item(["catch:1"]))
    b = draft(helix, home, bulk, model)
    assert b["outcome"] == "SKIPPED" and b["reason"] == "no new evidence"
    assert model.calls == [] and b["cost_usd"] == 0.0


def test_draft_batch_line_and_snapshot(helix, home, bulk):
    plant_three(helix)
    b = draft(helix, home, bulk, answer(item(["catch:1"])))
    snap = Path(b["run_dir"]) / "harvest.json"
    assert Path(b["run_dir"]).parent == bulk
    assert hashlib.sha256(snap.read_bytes()).hexdigest() == b["harvest_sha256"]
    assert rows(home / "BATCHES.jsonl")[-1]["harvest_sha256"] == b["harvest_sha256"]
    assert b["events"] == 3 and b["new_events"] == 3
    assert b["init"]["tools"] == [] and b["cost_usd"] == 0.10


def test_positive_control_three_identical_p7_refusals(helix, home, bulk):
    """Three identical P7 catches → the fake model's proposal citing them is accepted, and the
    harvest snapshot it cites hashes equal to a fresh harvest of the same log."""
    p7 = "P7 cd ~/helix/build_orders && cp ORDER_x.md /tmp/"
    jsonl(helix / "workspace/catches/CATCHES.jsonl",
          [catch(n, what="The build path changes only through a build order", where=p7)
           for n in (1, 2, 3)])
    b = draft(helix, home, bulk, answer(item(["catch:1", "catch:2", "catch:3"])))
    assert b["accepted"] == 1 and b["rejected"] == [] and b["leads"] == 0
    p = rows(home / "PROPOSALS.jsonl")[0]
    assert p["evidence"] == ["catch:1", "catch:2", "catch:3"]
    assert p["harvest_sha256"] == b["harvest_sha256"]
    fresh = hashlib.sha256(propose.snapshot_bytes(propose.harvest(helix))).hexdigest()
    snap = hashlib.sha256((Path(b["run_dir"]) / "harvest.json").read_bytes()).hexdigest()
    assert fresh == snap == b["harvest_sha256"]


def test_dry_uses_the_real_session_command(helix, home, bulk, monkeypatch, tmp_path):
    plant_three(helix)
    import coldread
    seen = []
    real = coldread.session_command
    monkeypatch.setattr(propose, "session_command", lambda *a: seen.append(a) or real(*a))
    b = draft(helix, home, bulk, propose.dry_model)
    assert b["outcome"] == "DONE" and seen, "the dry model builds the real argv"
    assert b["accepted"] == 1 and b["adds"] == 1
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    argv = real("s", "p", scratch)[0]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv


def test_cli_dry_writes_no_live_ledger(helix, monkeypatch, tmp_path, capsys):
    """`draft --dry` against a live-looking home: the home's files never change."""
    plant_three(helix)
    live = tmp_path / "live"
    live.mkdir()
    (live / "PROPOSALS.jsonl").write_text("")
    monkeypatch.setattr(propose, "HERE", live)
    monkeypatch.setattr(propose, "BULK", tmp_path / "live_bulk")
    assert propose.main(["draft", "--dry", "--helix", str(helix)]) == 0
    assert (live / "PROPOSALS.jsonl").read_text() == ""
    assert sorted(p.name for p in live.iterdir()) == ["PROPOSALS.jsonl"]
    assert not (tmp_path / "live_bulk").exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_the_cold_session_code_is_coldreads_own():
    import coldread
    assert propose.claude_session is coldread.claude_session
    assert propose.check_cold is coldread.check_cold
    assert propose.session_command is coldread.session_command


# --------------------------------------------------------------------------- decide

def log_with(helix, heading_date, line):
    (helix / "memory/sessions/log.md").write_text(
        f"## {heading_date} 09:00\n{line}\n**ODIN:** noted\n\n## 2026-10-04 08:00\n**You:** hi\n")


@pytest.fixture
def queued(helix, home, bulk):
    plant_three(helix)
    draft(helix, home, bulk, answer(item(["catch:1"])))
    return "P-20261004-1"


def decide(helix, home, pid, verb, quote, today=TODAY):
    return propose.decide(pid, verb, quote, home=home, log=helix / "memory/sessions/log.md",
                          today=today)


def test_decide_recorded_when_quote_in_a_recent_you_line(helix, home, queued):
    log_with(helix, "2026-09-28", "**You:** Yes, approve the P7 one please")
    ok, msg = decide(helix, home, queued, "approve", "approve the P7 one")
    assert ok, msg
    d = [r for r in rows(home / "PROPOSALS.jsonl") if r["kind"] == "decision"]
    assert d[0]["proposal"] == queued and d[0]["decision"] == "approve"
    assert d[0]["log_heading"] == "2026-09-28 09:00"
    assert propose.status(home=home)["proposals"][0]["status"] == "approved"


def test_decide_refused_quote_absent(helix, home, queued):
    log_with(helix, "2026-10-03", "**You:** something else")
    ok, msg = decide(helix, home, queued, "approve", "approve it")
    assert not ok and "not found" in msg


def test_decide_refused_eight_days_old(helix, home, queued):
    log_with(helix, "2026-09-26", "**You:** approve it")
    ok, msg = decide(helix, home, queued, "approve", "approve it")
    assert not ok and "8 days" in msg


def test_decide_refused_in_an_odin_line(helix, home, queued):
    (helix / "memory/sessions/log.md").write_text(
        "## 2026-10-03 09:00\n**You:** hm\n**Odin:** approve it\n**ODIN:** approve it\n")
    ok, msg = decide(helix, home, queued, "approve", "approve it")
    assert not ok and "**You:**" in msg


def test_decide_twice_refused(helix, home, queued):
    log_with(helix, "2026-10-03", "**You:** park it")
    assert decide(helix, home, queued, "park", "park it")[0]
    ok, msg = decide(helix, home, queued, "reject", "park it")
    assert not ok and "already decided" in msg


def test_decide_unknown_proposal_and_bad_verb_refused(helix, home, queued):
    log_with(helix, "2026-10-03", "**You:** approve it")
    assert not decide(helix, home, "P-20261004-9", "approve", "approve it")[0]
    assert not decide(helix, home, queued, "accept", "approve it")[0]


def test_decide_never_writes_the_log(helix, home, queued):
    log_with(helix, "2026-10-03", "**You:** approve it")
    before = (helix / "memory/sessions/log.md").read_bytes()
    decide(helix, home, queued, "approve", "approve it")
    assert (helix / "memory/sessions/log.md").read_bytes() == before


def test_status_names_the_order_that_cites_an_approved_proposal(helix, home, queued, tmp_path):
    log_with(helix, "2026-10-03", "**You:** approve it")
    decide(helix, home, queued, "approve", "approve it")
    orders = tmp_path / "orders"
    orders.mkdir()
    (orders / "ORDER_53_tighten.md").write_text(f"# WORK ORDER 53 — from {queued}\n")
    st = propose.status(home=home, orders=orders)
    assert st["proposals"][0]["status"] == "approved → order 53"


# --------------------------------------------------------------------------- chain + nightly line

def test_chain_edit_in_the_middle_breaks_verify_and_the_line(helix, home, bulk):
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(n) for n in range(1, 8)])
    draft(helix, home, bulk, answer(item(["catch:1"]), item(["catch:2"]), item(["catch:3"])))
    assert propose.verify(home) == {}
    line = propose.proposals_line(home=home)
    assert line.startswith("- proposals: 3 pending · 0 approved · 0 rejected · friction +3/−0 · "
                           "leads 0 · last batch 20") and line.endswith("· chain OK"), line
    p = home / "PROPOSALS.jsonl"
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace("tighten X", "tighten Y")
    p.write_text("\n".join(lines) + "\n")
    assert "PROPOSALS.jsonl" in propose.verify(home)
    line = propose.proposals_line(home=home)
    assert "chain BROKEN" in line and "\n  - 🔴 " in line
    assert propose.main(["verify", "--home", str(home)]) != 0


def test_chain_deleted_line_breaks_verify(helix, home, bulk):
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(n) for n in range(1, 8)])
    draft(helix, home, bulk, answer(item(["catch:1"]), item(["catch:2"])))
    p = home / "PROPOSALS.jsonl"
    p.write_text(p.read_text().splitlines()[1] + "\n")
    assert "PROPOSALS.jsonl" in propose.verify(home)


def test_the_line_with_nothing_yet(home):
    assert propose.proposals_line(home=home) == (
        "- proposals: 0 pending · 0 approved · 0 rejected · friction +0/−0 · leads 0 · "
        "last batch never · chain OK")


def test_the_line_goes_red_on_friction_only_and_not_done(helix, home, bulk):
    plant_three(helix)
    draft(helix, home, bulk, answer(item(["catch:1"], friction="REMOVES-FRICTION")))
    assert "\n  - 🔴 last batch friction-only" in propose.proposals_line(home=home)
    jsonl(helix / "workspace/catches/CATCHES.jsonl", [catch(n) for n in range(1, 8)])
    draft(helix, home, bulk, answer(item(["catch:5"]), init=dict(COLD, mcp_servers=["x"])))
    assert "\n  - 🔴 last batch NOT_COLD" in propose.proposals_line(home=home)


def test_the_line_never_raises(home):
    (home / "BATCHES.jsonl").write_text("not json\n")
    line = propose.proposals_line(home=home)
    assert line.startswith("- proposals:") and "🔴" in line


# --------------------------------------------------------------------------- Law A and scans

SRC = (HERE / "propose.py").read_text()


def test_law_a_no_judging_verb_or_field():
    hits = [(i, l) for i, l in enumerate(SRC.splitlines(), 1)
            if re.search(r"score|outcome_of|worked|effect", l, flags=re.I)]
    assert hits == [], hits


def test_escape_hatch_scan():
    hits = [(i, l) for i, l in enumerate(SRC.splitlines(), 1)
            if re.search(r"force|override|bypass|skip_check", l, flags=re.I)]
    assert hits == [], hits


def test_no_decide_verb_beyond_approve_reject_park():
    assert propose.DECISIONS == ("approve", "reject", "park")


def test_open_mode_scan_writes_only_its_own_files():
    """Every write site in propose.py is one of the three that write its own ledgers or its run
    dir, and none names metric/regate/sealgate/catches/memory."""
    writes = [l.strip() for l in SRC.splitlines()
              if re.search(r"""open\([^)]*["'][wax+]|write_text|write_bytes|\.mkdir\(""", l)]
    assert writes == ['with open(path, "a", encoding="utf-8") as fh:',
                      "run_dir.mkdir(parents=True, exist_ok=True)",
                      '(run_dir / "harvest.json").write_bytes(snap)',
                      '(run_dir / "answer.txt").write_text(out.get("text") or "", encoding="utf-8")'], \
        writes
    for bad in ("metric", "regate", "sealgate", "CATCHES", "memory"):
        assert not any(bad in w for w in writes), bad
    assert "subprocess" not in SRC and "import app" not in SRC


def test_a_full_run_leaves_every_source_byte_identical(helix, home, bulk):
    plant_three(helix)
    (helix / "workspace/regate/refusals.log").write_text(
        "2026-09-24T10:00:00 exit=4 verb=check target=/home/h/registry.json failed=R5\n")
    log_with(helix, "2026-10-03", "**You:** approve it")
    before = {p: p.read_bytes() for p in helix.rglob("*") if p.is_file()}
    draft(helix, home, bulk, answer(item(["catch:1"])))
    decide(helix, home, "P-20261004-1", "approve", "approve it")
    propose.proposals_line(home=home)
    after = {p: p.read_bytes() for p in helix.rglob("*") if p.is_file()}
    assert before == after


def test_systemd_units_weekly_sunday_and_subscription_env():
    svc = (HERE / "systemd/helix-proposals.service").read_text()
    tmr = (HERE / "systemd/helix-proposals.timer").read_text()
    assert "OnCalendar=Sun *-*-* 10:00:00" in tmr and "Persistent=true" in tmr
    assert "UnsetEnvironment=ANTHROPIC_API_KEY" in svc
    assert "Environment=CLAUDE_CONFIG_DIR=%h/claude/config" in svc
    assert "propose.py draft" in svc and "--dry" not in svc


def test_cli_harvest_json(helix, capsys):
    plant_three(helix)
    assert propose.main(["harvest", "--json", "--helix", str(helix)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in out["events"]] == ["catch:1", "catch:2", "catch:3"]


def test_the_readme_quotes_the_brief_verbatim():
    readme = (HERE / "README.md").read_text()
    for line in propose.BRIEF.split("\n"):
        assert f"> {line}\n" in readme, line
    assert "questions Odin could not answer from memory" in readme
