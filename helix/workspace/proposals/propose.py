#!/usr/bin/env python3
"""propose.py — the proposal engine (order 52, item 21; 134_* Part Six).

Helix gathers what went wrong and proposes its own upgrades. It never acts, never judges its own
proposals, never approves them. the user decides; Odin turns an approved proposal into a work order
by hand.

    propose.py harvest [--json]                 the evidence record (files only, no model)
    propose.py draft [--dry]                    one batch, one cold session, ≤ RUN_CAP_USD
    propose.py decide <P-id> approve|reject|park --quote "<the user's words>"
    propose.py status [--json]
    propose.py verify                           the three hash chains

Laws, in code:
  A. No field or verb here says whether a proposal helped. That is read later from the metric
     (order 37), never written here.
  B. Every proposal is ADDS-FRICTION or REMOVES-FRICTION; each batch line carries adds/removes,
     and a batch with ≥ 1 proposal and 0 ADDS-FRICTION is recorded with flag "friction-only"
     (recorded, never refused).
  C. lane "alice": `change` is a need for Alice, never a design.
Evidence: every cited id must be in the run's harvest snapshot. Zero valid ids → LEADS.jsonl.
Some unknown ids → rejected (never repaired). Only ids cited before → rejected "repeat".

Ledgers (beside this file, append-only, each line hash-chained like SWAPS/VETOES):
  PROPOSALS.jsonl  kind "proposal" (id P-<YYYYMMDD>-<k>, status pending) and kind "decision"
  BATCHES.jsonl    one line per draft run: DONE · ABORTED (budget) · NOT_COLD · FAILED · SKIPPED
  LEADS.jsonl      items whose evidence named nothing in the harvest
"""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = Path.home() / "helix"
BULK = Path("__BULK__/proposals")                 # run dirs (153_*)
for _dep in ("coldread", "metric"):
    if str(HELIX / "workspace" / _dep) not in sys.path:
        sys.path.append(str(HELIX / "workspace" / _dep))

# the cold session is coldread's own, by import (order 46b): no tools, no MCP, empty cwd,
# stdin from a file, the init event checked
from coldread import SESSION_CAP_USD, check_cold, claude_session, init_record, parse_json, \
    session_command  # noqa: E402
# the live-target rule for refusals is the metric's own (order 37)
from correction_rate import PATHS as METRIC_PATHS, REFUSAL, TEST_TARGET  # noqa: E402

RUN_CAP_USD = 3.00
MAX_PROPOSALS = 5
MIN_NEW_EVENTS = 3
DECISION_DAYS = 7
TEXT_CHARS = 300
GENESIS = hashlib.sha256(b"").hexdigest()

FIELDS = ("title", "evidence", "change", "acceptance", "moves", "friction", "lane")
MOVES = ("correction-share", "caught-before-vs-after", "time-to-correction", "vintage", "none")
FRICTION = ("ADDS-FRICTION", "REMOVES-FRICTION")
LANES = ("backend", "alice")
DECISIONS = ("approve", "reject", "park")
STATUS_OF = {"approve": "approved", "reject": "rejected", "park": "parked"}
LEDGERS = ("PROPOSALS.jsonl", "BATCHES.jsonl", "LEADS.jsonl")

SOURCES = {
    "catches": METRIC_PATHS["catches"],
    "reports": "build_orders/reports",
    "coldread": "workspace/coldread/READS.jsonl",
    "veto": "workspace/veto/VETOES.jsonl",
    "swap": "workspace/seatswap/SWAPS.jsonl",
    "replay": "workspace/replay/REPLAYS.jsonl",
    "canary": "workspace/canary/CANARY.jsonl",
    "regate": METRIC_PATHS["regate"],
    "sealgate": METRIC_PATHS["sealgate"],
}
LOG = HELIX / METRIC_PATHS["log"]
ORDERS = HELIX / "build_orders"
LOG_HEADING = re.compile(r"^## (\d{4}-\d\d-\d\d \d\d:\d\d)")
YOU = "**You:**"

BRIEF = (
    "You read the failure record of a personal AI assistant (Helix) and its build pipeline: "
    "things that were caught, builds that stopped, reports whose claims failed a check, cold "
    "reads of its fact base, vetoes, replication runs, canary misses and gate refusals. Each "
    "event has a stable id. You have no tools and no other context.\n"
    "Propose at most 5 upgrades to the system. Each proposal must cite the ids of the logged "
    "events it would have prevented; a proposal without evidence is worthless. Cite only ids "
    "you were given. An event marked `cited: true` was already cited by an earlier proposal: a "
    "proposal whose ids are all already cited is rejected as a repeat.\n"
    "Tag each proposal ADDS-FRICTION (it tightens: a new check, gate, refusal or test) or "
    "REMOVES-FRICTION (it loosens or streamlines). At least one proposal should tighten "
    "something. An engine that only removes friction is treated as compromised.\n"
    "Say which of these numbers it should move: correction-share, caught-before-vs-after, "
    "time-to-correction, vintage, or none.\n"
    "Lane: backend, or alice for anything visual, UI or generated media. For an alice item, "
    "describe it as a NEED for Alice, never as a design: say what is needed and why, not how "
    "it should look.\n"
    'Answer with JSON only: {"proposals": [{"title": "<short>", "evidence": ["<id>", ...], '
    '"change": "<what to change>", "acceptance": "<how we would know it is built right>", '
    '"moves": "<one of the five>", "friction": "ADDS-FRICTION|REMOVES-FRICTION", '
    '"lane": "backend|alice"}, ...]}')


def one_line(text, n=TEXT_CHARS):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Part A: harvest -------------------------------------------------------------------------

def _jsonl(path):
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _catches(path):
    rows = _jsonl(path)
    retracted = {int(n) for r in rows if r.get("kind") == "retraction"
                 for n in (r.get("retracts") or []) if str(n).isdigit()}
    for r in rows:
        if r.get("kind") == "retraction" or not str(r.get("n", "")).isdigit():
            continue
        if int(r["n"]) in retracted:
            continue
        yield (f"catch:{r['n']}",
               f"{r.get('date')} {r.get('stage')} caught by {r.get('caught_by')} at "
               f"{r.get('where')}: {r.get('what')} (fixed in {r.get('fixed_in')})")


def _reports(d):
    for p in sorted(d.glob("ORDER_*_report.md")):
        lines = p.read_text(encoding="utf-8", errors="replace").rstrip("\n").split("\n")
        if lines[-1] != "STOP":
            continue
        nn = p.name[len("ORDER_"):-len("_report.md")]
        head = next((l for l in lines if l.startswith("## 🔴 STOP")), lines[0])
        yield f"report:{nn}:STOP", head
    for p in sorted(d.glob("ORDER_*_report.check.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        verdict = rec.get("verdict")
        if verdict not in ("FLAGGED", "UNCHECKED"):
            continue
        nn = p.name[len("ORDER_"):-len("_report.check.json")]
        bad = next((c for c in rec.get("claims") or [] if isinstance(c, dict) and not c.get("ok")),
                   None)
        text = (f"{bad.get('claim')} — said {bad.get('said')} — witness {bad.get('witness')}"
                if bad else f"{verdict}: no failing claim recorded")
        yield f"check:{nn}:{verdict}", text


RUNNER = re.compile(r"^(\d+)\. ", re.M)


def _coldread(path):
    for r in _jsonl(path):
        date, top = r.get("date"), r.get("top")
        if not date or not top:
            continue
        yield f"coldread:{date}:0", f"top: `{top.get('file')}` — {top.get('quote')}"
        md = path.parent / "READS" / str(r.get("report") or "")
        if not (r.get("report") and md.is_file()):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^## Runners-up\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        if not m:
            continue
        parts = RUNNER.split(m.group(1))
        for k, body in zip(parts[1::2], parts[2::2]):
            yield f"coldread:{date}:{k}", f"runner-up {k}: {body}"


def _veto(path):
    for i, r in enumerate(_jsonl(path), 1):
        if r.get("outcome") == "BREAK_CONFIRMED":
            why = (r.get("judge") or {}).get("why") if isinstance(r.get("judge"), dict) else None
            yield f"veto:{r.get('seq', i)}", f"BREAK_CONFIRMED on {r.get('seal')}: {why or r.get('why')}"


def _swap(path):
    for i, r in enumerate(_jsonl(path), 1):
        if r.get("outcome") == "DISAGREE":
            yield f"swap:{r.get('seq', i)}", f"DISAGREE on pack {r.get('pack')}: {r.get('why')}"


def _replay(path):
    for i, r in enumerate(_jsonl(path), 1):
        if r.get("result") in ("MISMATCH", "TAMPERED"):
            yield f"replay:{i}", f"{r.get('result')} replaying {r.get('name')} at {r.get('at')}"


def _canary(path):
    for r in _jsonl(path):
        for case, got in sorted((r.get("cases") or {}).items()):
            if got == "missed":
                yield f"canary:{r.get('date')}:{case}", f"canary {case} missed (run {r.get('ts')})"
            elif got == "refused":
                yield (f"canary:{r.get('date')}:{case}",
                       f"acquittal {case} wrongly refused (run {r.get('ts')})")


def _refusals(gate):
    def read(path):
        for k, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = REFUSAL.match(line)
            if m and not TEST_TARGET.search(m.group(2)):
                yield f"refusal:{gate}:{k}", line
    return read


READERS = {"catches": _catches, "reports": _reports, "coldread": _coldread, "veto": _veto,
           "swap": _swap, "replay": _replay, "canary": _canary,
           "regate": _refusals("regate"), "sealgate": _refusals("sealgate")}


def harvest(helix=None):
    """→ {"events": [{id, source, text}], "unavailable": [source]}. Reads files only; no model,
    no clock: the same files give the same bytes. A missing source is noted, never raised."""
    helix = Path(helix or HELIX)
    events, seen, unavailable = [], set(), []
    for source, rel in SOURCES.items():
        path = helix / rel
        if not path.exists():
            unavailable.append(source)
            continue
        for eid, text in READERS[source](path):
            if eid in seen:
                continue
            seen.add(eid)
            events.append({"id": eid, "source": source, "text": one_line(text)})
    return {"events": events, "unavailable": unavailable}


def snapshot_bytes(h):
    return (json.dumps(h, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


# --- the chains ------------------------------------------------------------------------------

def read_ledger(path):
    """[(line number, raw, record or None)]. Raw bytes are kept: the chain is over them."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for n, raw in enumerate(path.read_bytes().decode("utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            rec = None
        out.append((n, raw, rec if isinstance(rec, dict) else None))
    return out


def verify_chain(entries):
    """None if every line is JSON, numbered in order, and carries the hash of the line before."""
    prev, seq = GENESIS, 0
    for n, raw, rec in entries:
        seq += 1
        if rec is None:
            return f"line {n} is not a JSON record"
        if rec.get("prev") != prev:
            return f"line {n} does not carry the hash of the line before it: edited, removed or inserted"
        if rec.get("seq") != seq:
            return f"line {n} has seq {rec.get('seq')!r}, expected {seq}"
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return None


def verify(home=None):
    """{ledger name: problem} for every broken chain; {} when all three hold."""
    home = Path(home or HERE)
    out = {}
    for name in LEDGERS:
        problem = verify_chain(read_ledger(home / name))
        if problem:
            out[name] = problem
    return out


def append(path, records):
    """Append, never rewrite. A broken chain is refused before anything is written."""
    entries = read_ledger(path)
    problem = verify_chain(entries)
    if problem:
        raise RuntimeError(f"{Path(path).name} chain is broken ({problem}); nothing appended")
    prev = hashlib.sha256(entries[-1][1].encode("utf-8")).hexdigest() if entries else GENESIS
    seq, out, written = len(entries), [], []
    for rec in records:
        seq += 1
        rec = dict(rec, seq=seq, prev=prev, ts=utc_now())
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out.append(raw)
        written.append(rec)
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(l + "\n" for l in out))
    return written


def records(path, kind=None):
    return [r for _, _, r in read_ledger(path) if r and (kind is None or r.get("kind") == kind)]


# --- Part B: draft ---------------------------------------------------------------------------

class Aborted(Exception):
    pass


def check_item(it, known, cited):
    """→ ("queue" | "lead" | "reject", why). Never repairs an item."""
    if not isinstance(it, dict):
        return "reject", "not an object"
    for f in FIELDS:
        v = it.get(f)
        if f == "evidence":
            if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
                return "reject", "evidence: missing or not a list of ids"
        elif not isinstance(v, str) or not v.strip():
            return "reject", f"{f}: missing"
    for f, allowed in (("moves", MOVES), ("friction", FRICTION), ("lane", LANES)):
        if it[f] not in allowed:
            return "reject", f"{f}: {it[f]!r} is not one of {', '.join(allowed)}"
    unknown = [x for x in it["evidence"] if x not in known]
    if len(unknown) == len(it["evidence"]):
        return "lead", "no cited id is in the harvest"
    if unknown:
        return "reject", f"cites ids not in the harvest: {', '.join(unknown)}"
    if set(it["evidence"]) <= cited:
        return "reject", "repeat"
    return "queue", None


def draft(model=None, helix=None, home=None, bulk=None, today=None):
    """One batch. model(system, prompt, workdir) → {"text", "cost_usd", "init"[, "error"]}.
    Appends one BATCHES.jsonl line (and any proposals / leads) under home. → the batch line."""
    model = model or claude_session
    home, bulk = Path(home or HERE), Path(bulk or BULK)
    today = today or datetime.date.today()
    for name, problem in verify(home).items():
        raise RuntimeError(f"{name} chain is broken ({problem}); no batch run")
    h = harvest(helix)
    snap = snapshot_bytes(h)
    known = {e["id"] for e in h["events"]}
    cited = {x for r in records(home / "PROPOSALS.jsonl", "proposal") for x in r.get("evidence", [])}
    new = sorted(known - cited)
    rec = {"kind": "batch", "date": today.isoformat(), "started": utc_now(),
           "harvest_sha256": hashlib.sha256(snap).hexdigest(), "events": len(known),
           "new_events": len(new), "unavailable": h["unavailable"], "cap_usd": RUN_CAP_USD,
           "cost_usd": 0.0, "sessions": 0, "init": None, "outcome": None, "reason": None,
           "accepted": 0, "leads": 0, "rejected": [], "adds": 0, "removes": 0, "for_alice": 0,
           "proposals": [], "run_dir": None}
    if len(new) < MIN_NEW_EVENTS:
        rec.update(outcome="SKIPPED", reason="no new evidence")
        append(home / "BATCHES.jsonl", [rec])
        return rec
    run_dir = bulk / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    k = 2
    while run_dir.exists():
        run_dir = run_dir.with_name(run_dir.name.split("_")[0] + f"_{k}")
        k += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "harvest.json").write_bytes(snap)
    rec["run_dir"] = str(run_dir)
    listing = [{"id": e["id"], "source": e["source"], "text": e["text"],
                **({"cited": True} if e["id"] in cited else {})} for e in h["events"]]
    prompt = ("The logged events, as JSON:\n\n" + json.dumps(listing, ensure_ascii=False, indent=1)
              + "\n\nPropose at most 5 upgrades, as JSON only, in the shape the instructions give.")
    queue, leads = [], []
    try:
        if rec["cost_usd"] + SESSION_CAP_USD > RUN_CAP_USD:
            raise Aborted()
        with tempfile.TemporaryDirectory(prefix="proposals_") as tmp:
            out = model(BRIEF, prompt, tmp)
        rec["sessions"] = 1
        rec["cost_usd"] = round(out.get("cost_usd") or 0.0, 6)
        rec["init"] = init_record(out.get("init"))
        (run_dir / "answer.txt").write_text(out.get("text") or "", encoding="utf-8")
        warm = check_cold(out.get("init"))
        if warm:
            rec.update(outcome="NOT_COLD", reason="; ".join(warm))
        elif out.get("error"):
            rec.update(outcome="FAILED", reason=one_line(out["error"]))
        elif rec["cost_usd"] > RUN_CAP_USD:
            raise Aborted()
        else:
            try:
                items = parse_json(out.get("text")).get("proposals")
            except (ValueError, AttributeError) as e:
                items, rec["outcome"], rec["reason"] = None, "FAILED", one_line(e)
            if items is not None and not isinstance(items, list):
                items, rec["outcome"], rec["reason"] = None, "FAILED", "proposals is not a list"
            if items is not None:
                seen = set(cited)
                for i, it in enumerate(items):
                    title = one_line(it.get("title") if isinstance(it, dict) else None, 120)
                    if i >= MAX_PROPOSALS:
                        rec["rejected"].append({"title": title, "why": f"beyond the first {MAX_PROPOSALS}"})
                        continue
                    where, why = check_item(it, known, seen)
                    if where == "reject":
                        rec["rejected"].append({"title": title, "why": why})
                    elif where == "lead":
                        leads.append({k: it[k] for k in FIELDS})
                    else:
                        queue.append({k: it[k] for k in FIELDS})
                        seen |= set(it["evidence"])
                rec["outcome"] = "DONE"
    except Aborted:
        rec.update(outcome="ABORTED", reason="budget")
    if rec["outcome"] == "DONE":
        stamp = today.strftime("%Y%m%d")
        n = sum(r.get("id", "").startswith(f"P-{stamp}-")
                for r in records(home / "PROPOSALS.jsonl", "proposal"))
        props = []
        for j, it in enumerate(queue, n + 1):
            props.append(dict(it, kind="proposal", id=f"P-{stamp}-{j}", status="pending",
                              harvest_sha256=rec["harvest_sha256"]))
        if props:
            append(home / "PROPOSALS.jsonl", props)
        if leads:
            append(home / "LEADS.jsonl", [dict(it, kind="lead", date=today.isoformat(),
                                               harvest_sha256=rec["harvest_sha256"]) for it in leads])
        rec.update(accepted=len(props), leads=len(leads), proposals=[p["id"] for p in props],
                   adds=sum(p["friction"] == "ADDS-FRICTION" for p in props),
                   removes=sum(p["friction"] == "REMOVES-FRICTION" for p in props),
                   for_alice=sum(p["lane"] == "alice" for p in props))
        if props and not rec["adds"]:
            rec["flag"] = "friction-only"
    rec["ended"] = utc_now()
    append(home / "BATCHES.jsonl", [rec])
    return rec


def dry_model(system, prompt, workdir):
    """No spend: builds the real command (so --dry exercises it) and proposes one tightening
    citing the first three events."""
    session_command(system, prompt, workdir)
    first = re.findall(r'"id": "([^"]+)"', prompt)[:3]
    out = {"proposals": [{"title": "(dry) tighten", "evidence": first, "change": "(dry)",
                          "acceptance": "(dry)", "moves": "caught-before-vs-after",
                          "friction": "ADDS-FRICTION", "lane": "backend"}]}
    return {"text": json.dumps(out), "cost_usd": 0.0,
            "init": {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                     "skills": [], "memory_paths": None, "plugins": []}}


# --- Part C: decide --------------------------------------------------------------------------

def find_quote(quote, log, today):
    """→ (heading, None) for the newest `**You:**` line within DECISION_DAYS that holds the quote
    verbatim, else (None, why). log.md is read, never written."""
    heading, older, elsewhere = None, None, False
    found = None
    for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
        m = LOG_HEADING.match(line)
        if m:
            heading = m.group(1)
            continue
        if quote not in line:
            continue
        if not line.startswith(YOU):
            elsewhere = True
            continue
        if heading is None:
            continue
        age = (today - datetime.date.fromisoformat(heading[:10])).days
        if 0 <= age <= DECISION_DAYS:
            found = heading
        else:
            older = (heading, age)
    if found:
        return found, None
    if older:
        return None, (f"the quote is only in a {YOU} line dated {older[0]} ({older[1]} days old; "
                      f"the limit is {DECISION_DAYS})")
    if elsewhere:
        return None, f"the quote is only in lines that are not {YOU} lines"
    return None, f"the quote was not found in any {YOU} line of the session log"


def decide(pid, verb, quote, home=None, log=None, today=None):
    """→ (recorded?, message). One decision per proposal, witnessed by the user's own words."""
    home, log = Path(home or HERE), Path(log or LOG)
    today = today or datetime.date.today()
    if verb not in DECISIONS:
        return False, f"REFUSED: {verb!r} is not one of {', '.join(DECISIONS)}"
    if not (quote or "").strip():
        return False, "REFUSED: --quote is empty"
    path = home / "PROPOSALS.jsonl"
    problem = verify_chain(read_ledger(path))
    if problem:
        return False, f"REFUSED: PROPOSALS.jsonl chain is broken ({problem})"
    if not any(r.get("id") == pid for r in records(path, "proposal")):
        return False, f"REFUSED: no proposal {pid}"
    done = [r for r in records(path, "decision") if r.get("proposal") == pid]
    if done:
        return False, f"REFUSED: {pid} already decided ({done[0].get('decision')})"
    heading, why = find_quote(quote, log, today)
    if not heading:
        return False, f"REFUSED: {why}"
    append(path, [{"kind": "decision", "proposal": pid, "decision": verb, "quote": quote,
                   "log_heading": heading}])
    return True, f"{pid}: {STATUS_OF[verb]} (the user, {heading})"


# --- Part D: status and the nightly line -----------------------------------------------------

def order_citing(pid, orders):
    for p in sorted(Path(orders).glob("ORDER_*.md")):
        if pid in p.read_text(encoding="utf-8", errors="replace"):
            m = re.match(r"ORDER_([0-9]+[a-z]?)_", p.name)
            return m.group(1) if m else p.name
    return None


def status(home=None, orders=None):
    home = Path(home or HERE)
    orders = Path(orders or ORDERS)
    path = home / "PROPOSALS.jsonl"
    decided = {r.get("proposal"): r.get("decision") for r in records(path, "decision")}
    props = []
    for r in records(path, "proposal"):
        st = STATUS_OF.get(decided.get(r["id"]), "pending")
        if st == "approved" and orders.is_dir():
            nn = order_citing(r["id"], orders)
            if nn:
                st = f"approved → order {nn}"
        props.append({"id": r["id"], "title": r.get("title"), "status": st,
                      "friction": r.get("friction"), "lane": r.get("lane"),
                      "moves": r.get("moves"), "evidence": r.get("evidence")})
    batches = records(home / "BATCHES.jsonl", "batch")
    return {"proposals": props, "leads": len(records(home / "LEADS.jsonl", "lead")),
            "last_batch": batches[-1] if batches else None, "chains": verify(home)}


def proposals_line(home=None):
    """`- proposals: …`, with 🔴 sub-lines. Reads the three ledgers only; never runs a batch and
    never raises."""
    try:
        st = status(home, orders=Path("/nonexistent"))
        props, last, broken = st["proposals"], st["last_batch"], st["chains"]
        count = lambda s: sum(p["status"].startswith(s) for p in props)
        line = (f"- proposals: {count('pending')} pending · {count('approved')} approved · "
                f"{count('rejected')} rejected · friction "
                f"+{sum(p['friction'] == 'ADDS-FRICTION' for p in props)}/"
                f"−{sum(p['friction'] == 'REMOVES-FRICTION' for p in props)} · leads {st['leads']}"
                f" · last batch {last['date'] if last else 'never'} · "
                f"{'chain BROKEN' if broken else 'chain OK'}")
        subs = [f"🔴 chain BROKEN: {name} {problem}" for name, problem in broken.items()]
        if last and last.get("flag") == "friction-only":
            subs.append(f"🔴 last batch friction-only (0 ADDS-FRICTION of {last.get('accepted')})")
        if last and last.get("outcome") != "DONE":
            subs.append(f"🔴 last batch {last.get('outcome')}"
                        + (f" ({one_line(last.get('reason'), 80)})" if last.get("reason") else ""))
        return line + "".join(f"\n  - {s}" for s in subs)
    except Exception as e:
        return f"- proposals: 🔴 proposals did not run ({type(e).__name__}: {e})"


# --- CLI -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="propose.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--json", action="store_true")
    h.add_argument("--helix", default=None)
    d = sub.add_parser("draft")
    d.add_argument("--dry", action="store_true")
    d.add_argument("--helix", default=None)
    c = sub.add_parser("decide")
    c.add_argument("pid")
    c.add_argument("verb", choices=DECISIONS)
    c.add_argument("--quote", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--home", default=None)
    a = ap.parse_args(argv)

    if a.cmd == "harvest":
        res = harvest(a.helix)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0
        by = {}
        for e in res["events"]:
            by[e["source"]] = by.get(e["source"], 0) + 1
        print(f"{len(res['events'])} events · " + " · ".join(f"{k} {n}" for k, n in by.items())
              + (f" · unavailable: {', '.join(res['unavailable'])}" if res["unavailable"] else ""))
        for e in res["events"]:
            print(f"{e['id']}\t{e['text']}")
        return 0
    if a.cmd == "draft":
        if a.dry:
            tmp = Path(tempfile.mkdtemp(prefix="proposals_dry_"))
            for name in LEDGERS:
                if (HERE / name).exists():
                    shutil.copy2(HERE / name, tmp / name)
            rec = draft(model=dry_model, helix=a.helix, home=tmp, bulk=tmp / "runs")
            print(f"DRY RUN — no claude call, no spend · {rec['outcome']} · {rec['events']} events "
                  f"({rec['new_events']} new) · harvest {rec['harvest_sha256'][:12]} · "
                  f"accepted {rec['accepted']} · leads {rec['leads']} · rejected {len(rec['rejected'])}"
                  f" · friction +{rec['adds']}/−{rec['removes']} · output {tmp}")
            return 0 if rec["outcome"] in ("DONE", "SKIPPED") else 4
        rec = draft(helix=a.helix)
        print(f"{rec['outcome']}" + (f" ({rec['reason']})" if rec["reason"] else "")
              + f" · {rec['events']} events ({rec['new_events']} new) · harvest "
              f"{rec['harvest_sha256'][:12]} · ${rec['cost_usd']:.4f} · accepted {rec['accepted']}"
              f" {rec['proposals']} · leads {rec['leads']} · rejected {len(rec['rejected'])} · "
              f"friction +{rec['adds']}/−{rec['removes']} · for Alice {rec['for_alice']}"
              + (f" · 🔴 {rec['flag']}" if rec.get("flag") else ""))
        for r in rec["rejected"]:
            print(f"  rejected: {r['title']} — {r['why']}")
        return 0 if rec["outcome"] in ("DONE", "SKIPPED") else 4
    if a.cmd == "decide":
        ok, msg = decide(a.pid, a.verb, a.quote)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 3
    if a.cmd == "verify":
        broken = verify(a.home)
        for name, problem in broken.items():
            print(f"BROKEN {name}: {problem}", file=sys.stderr)
        if not broken:
            print("chains OK: " + ", ".join(LEDGERS))
        return 3 if broken else 0
    st = status()
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    print(proposals_line())
    for p in st["proposals"]:
        print(f"{p['id']}\t{p['status']}\t{p['friction']}\t{p['lane']}\t{p['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
