#!/usr/bin/env python3
"""corrections.py — the correction queue (order 72, item 30; 180_*).

A monthly cold read finds stale or wrong passages. This file turns each finding into a queued
correction, so a finding cannot sit in a READS/ file and be forgotten. It never edits a fact and
never decides one: Odin checks each finding and writes a correcting fact that retires the passage
(order 70); the user approves it, or rejects the finding in his own words.

    corrections.py harvest [--dry]      parse every READS/*.md not yet harvested → kind "finding"
    corrections.py status [--json]      open / closed / rejected, with ages
    corrections.py reject <C-id> --quote "<the user's words>"
    corrections.py verify               the hash chain

States:
  open      neither closed nor rejected. Age = days since the read.
  closed    DERIVED on every status, never written: an applied retirement (memretrieve.retired_in(),
            order 70's one parser) whose A is the finding's file and whose passage overlaps the
            finding's quote. A retirement that later stops resolving re-opens the finding.
  rejected  a kind "rejection" line, witnessed by the user's words in a `**You:**` line of the session
            log (propose.find_quote: the same witness rule and DECISION_DAYS as proposals).

Ledger: QUEUE.jsonl beside this file, append-only, each line hash-chained like PROPOSALS.jsonl.
  kind "finding"    {id C-<YYYYMMDD>-<k>, read, rank, file, quote, why, conf}
  kind "harvest"    {read, report, findings}: names a read once harvested; it is never re-read
  kind "rejection"  {id, quote, log_heading}: one per finding
"""

import argparse
import datetime
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = Path.home() / "helix"
for _dep in ("proposals", "memretrieve"):
    if str(HELIX / "workspace" / _dep) not in sys.path:
        sys.path.append(str(HELIX / "workspace" / _dep))

# the user's words are witnessed the proposals' way (order 52): newest `**You:**` line, DECISION_DAYS
from propose import LOG, find_quote  # noqa: E402
import memretrieve  # noqa: E402

QUEUE = HERE / "QUEUE.jsonl"
READS_DIR = HELIX / "workspace" / "coldread" / "READS"
FACTS = HELIX / "memory" / "facts"
RANKS = ("verdict", "runner-up", "candidate")          # highest first
MIN_COMMON = 20
OLD_DAYS = 30
GENESIS = hashlib.sha256(b"").hexdigest()
READ_NAME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)\.md$")
NONE_QUEUED = "- corrections: none queued (run corrections.py harvest)"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collapse(s):
    return " ".join(str(s or "").split())


# --- the chain (copied from proposals/propose.py, order 52) ----------------------------------

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


def records(kind=None, path=None):
    return [r for _, _, r in read_ledger(path or QUEUE) if r and (kind is None or r.get("kind") == kind)]


def verify(path=None):
    """None when QUEUE.jsonl's chain holds (or it does not exist yet), else the problem."""
    return verify_chain(read_ledger(path or QUEUE))


# --- parsing a read (coldread.py render_md's format) -----------------------------------------

PLANT_LINE = re.compile(r"^- plant: \*\*[^*\n]*\*\* · `([^`]+)`", re.M)
VERDICT = re.compile(r"\A\s*\*\*`([^`]+)`\*\*\n\n> (.*?)\n\n(.*?)\s*\Z", re.S)
RUNNER = re.compile(r"^(\d+)\. ", re.M)
RUNNER_ITEM = re.compile(r"\A`([^`]+)` — \"(.*?)\" — (.*?)\s*\Z", re.S)
CAND_HEAD = re.compile(r"^- batch ", re.M)
CAND_ITEM = re.compile(r"\A- batch [^\n]*? · `([^`]+)` · conf (\S+?)( ← THE PLANT)?\n"
                       r"  - quote: \"(.*?)\"\n  - why: (.*?)\s*\Z", re.S)


def sections(text):
    """{heading: body} for each `## ` section."""
    out = {}
    for part in re.split(r"^## ", text, flags=re.M)[1:]:
        head, _, body = part.partition("\n")
        out[head.strip()] = body
    return out


def _conf(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_read(text):
    """→ {"plant": file or None, "findings": [{rank, file, quote, why, conf}]} in rank order, the plant
    dropped and each (file, collapsed quote) kept once, at its highest rank. An item that does not
    parse raises ValueError: a read is harvested whole or not at all."""
    m = PLANT_LINE.search(text)
    plant = m.group(1).rsplit("/", 1)[-1] if m else None
    sec = sections(text)
    raw = []
    v = next((b for h, b in sec.items() if h == "Verdict"), None)
    if v is not None and not v.strip().startswith("(none"):
        vm = VERDICT.match(v)
        if not vm:
            raise ValueError("the Verdict block does not parse")
        raw.append({"rank": "verdict", "file": vm.group(1), "quote": vm.group(2), "why": vm.group(3),
                    "conf": None, "plant": False})
    r = next((b for h, b in sec.items() if h == "Runners-up"), "")
    parts = RUNNER.split(r)
    for k, body in zip(parts[1::2], parts[2::2]):
        rm = RUNNER_ITEM.match(body)
        if not rm:
            raise ValueError(f"runner-up {k} does not parse")
        raw.append({"rank": "runner-up", "file": rm.group(1), "quote": rm.group(2), "why": rm.group(3),
                    "conf": None, "plant": False})
    c = next((b for h, b in sec.items() if h.startswith("Stage-1 candidates")), "")
    starts = [mm.start() for mm in CAND_HEAD.finditer(c)] + [len(c)]
    for i, (s, e) in enumerate(zip(starts, starts[1:]), 1):
        cm = CAND_ITEM.match(c[s:e])
        if not cm:
            raise ValueError(f"stage-1 candidate {i} does not parse")
        raw.append({"rank": "candidate", "file": cm.group(1), "quote": cm.group(4), "why": cm.group(5),
                    "conf": _conf(cm.group(2)), "plant": bool(cm.group(3))})
    confs = {}
    for x in raw:
        if x["conf"] is not None:
            key = (x["file"], collapse(x["quote"]))
            confs[key] = max(confs.get(key, x["conf"]), x["conf"])
    seen, findings = set(), []
    for x in sorted(raw, key=lambda x: RANKS.index(x["rank"])):          # stable: document order kept
        if x["plant"] or x["file"].rsplit("/", 1)[-1] == plant:
            continue
        key = (x["file"], collapse(x["quote"]))
        if key in seen:
            continue
        seen.add(key)
        findings.append({"rank": x["rank"], "file": x["file"], "quote": x["quote"], "why": x["why"],
                         "conf": confs.get(key)})
    return {"plant": plant, "findings": findings}


# --- harvest ---------------------------------------------------------------------------------

def harvest(dry=False):
    """Queue every READS/<date>.md not named by a harvest line yet. → {"reads": [dates], "findings":
    [records]}. dry: the same answer, nothing written. A broken chain is refused (RuntimeError)."""
    problem = verify()
    if problem:
        raise RuntimeError(f"QUEUE.jsonl chain is broken ({problem}); nothing harvested")
    done = {r.get("read") for r in records("harvest")}
    reads, out = [], []
    if Path(READS_DIR).is_dir():
        for p in sorted(Path(READS_DIR).iterdir()):
            m = READ_NAME.match(p.name)
            date = p.name[:-3]
            if not (m and p.is_file()) or date in done:
                continue
            parsed = parse_read(p.read_text(encoding="utf-8", errors="replace"))
            new = [{"kind": "finding", "id": f"C-{''.join(m.groups())}-{k}", "read": date, **f}
                   for k, f in enumerate(parsed["findings"], 1)]
            reads.append(date)
            out += new
            if not dry:
                append(QUEUE, new + [{"kind": "harvest", "read": date, "report": p.name,
                                      "findings": len(new)}])
    return {"reads": reads, "findings": out}


# --- closed, derived -------------------------------------------------------------------------

def overlaps(passage, quote):
    """Whitespace-collapsed: either contains the other, or ≥ MIN_COMMON consecutive chars in common."""
    a, b = collapse(passage), collapse(quote)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size >= MIN_COMMON


def retired_in_dir(facts=None):
    return memretrieve.retired_in(str(facts or FACTS))


def closed_by(finding, ret):
    """The B of the first applied retirement that closes this finding, else None."""
    for r in ret["applied"]:
        if r["a"] == finding["file"] and overlaps(r["passage"], finding["quote"]):
            return r["b"]
    return None


# --- reject ----------------------------------------------------------------------------------

def reject(cid, quote, today=None):
    """→ (recorded?, message). One decision per finding, witnessed by the user's own words."""
    today = today or datetime.date.today()
    if not (quote or "").strip():
        return False, "REFUSED: --quote is empty"
    problem = verify()
    if problem:
        return False, f"REFUSED: QUEUE.jsonl chain is broken ({problem})"
    if not any(r.get("id") == cid for r in records("finding")):
        return False, f"REFUSED: no finding {cid}"
    if any(r.get("id") == cid for r in records("rejection")):
        return False, f"REFUSED: {cid} already rejected"
    heading, why = find_quote(quote, LOG, today)
    if not heading:
        return False, f"REFUSED: {why}"
    append(QUEUE, [{"kind": "rejection", "id": cid, "quote": quote, "log_heading": heading}])
    return True, f"{cid}: rejected (the user, {heading})"


# --- status and the line ---------------------------------------------------------------------

def _order(f):
    return f["read"], int(f["id"].rsplit("-", 1)[-1])


def status(today=None):
    today = today or datetime.date.today()
    ret = retired_in_dir()
    rejected = {r.get("id"): r for r in records("rejection")}
    out = []
    for f in records("finding"):
        b = closed_by(f, ret)
        st = "rejected" if f["id"] in rejected else "closed" if b else "open"
        out.append({"id": f["id"], "read": f["read"], "rank": f["rank"], "file": f["file"],
                    "quote": f["quote"], "state": st, "closed_by": b,
                    "rejection": (rejected.get(f["id"]) or {}).get("log_heading"),
                    "age": (today - datetime.date.fromisoformat(f["read"])).days})
    out.sort(key=_order)
    count = lambda s: sum(f["state"] == s for f in out)
    opened = [f for f in out if f["state"] == "open"]
    return {"findings": out, "open": count("open"), "closed": count("closed"),
            "rejected": count("rejected"), "oldest": max((f["age"] for f in opened), default=None),
            "reads": [r.get("read") for r in records("harvest")], "chain": verify()}


def corrections_line(today=None):
    """`- corrections: …` for the nightly. Reads only; the nightly runs harvest() before it."""
    if not records():
        return NONE_QUEUED
    st = status(today)
    opened = [f for f in st["findings"] if f["state"] == "open"]
    tail = f" · {st['closed']} closed · {st['rejected']} rejected"
    if not opened:
        line = f"- corrections: 0 open{tail}"
    elif st["oldest"] > OLD_DAYS:
        line = (f"- corrections: 🟡 {st['open']} open (oldest {st['oldest']} days){tail} · oldest open: "
                + ", ".join(f"{f['id']} {f['file']}" for f in opened[:3]) + (" …" if len(opened) > 3 else ""))
    else:
        line = f"- corrections: {st['open']} open (oldest {st['oldest']} days){tail}"
    if st["chain"]:
        line += f" · 🔴 chain BROKEN ({st['chain']})"
    return line


# --- CLI -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="corrections.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--dry", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("reject")
    r.add_argument("cid")
    r.add_argument("--quote", required=True)
    sub.add_parser("verify")
    a = ap.parse_args(argv)

    if a.cmd == "harvest":
        try:
            res = harvest(dry=a.dry)
        except (RuntimeError, ValueError) as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 3
        n = len(res["reads"])
        print(("DRY RUN — " if a.dry else "") + f"{len(res['findings'])} findings from {n} read"
              + ("" if n == 1 else "s") + (f" ({', '.join(res['reads'])})" if res["reads"] else "")
              + (" · nothing written" if a.dry else ""))
        for f in res["findings"]:
            print(f"{f['id']}\t{f['rank']}\t{f['file']}\t{collapse(f['quote'])}")
        return 0
    if a.cmd == "reject":
        ok, msg = reject(a.cid, a.quote)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 3
    if a.cmd == "verify":
        problem = verify()
        if problem:
            print(f"BROKEN QUEUE.jsonl: {problem}", file=sys.stderr)
            return 3
        print("chain OK: QUEUE.jsonl")
        return 0
    st = status()
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    print(corrections_line())
    for f in st["findings"]:
        extra = f"closed by {f['closed_by']}" if f["state"] == "closed" else \
            f"rejected ({f['rejection']})" if f["state"] == "rejected" else collapse(f["quote"])
        print(f"{f['id']}\t{f['state']}\t{f['age']} days\t{f['rank']}\t{f['file']}\t{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
