#!/usr/bin/env python3
"""The correction-rate metric (order 37, item 18): is the system getting righter, or just faster?

    correction_rate.py report [--n-days 30] [--json] [--helix DIR]

Read-only. It opens files for reading and nothing else; it never imports app.py.

  a. correction share  marked facts ÷ all facts (memory/facts/*.md + memory/archive/**/*.md).
                       "Marked" is app.py's own rule: `_CORRECTION_MARKERS` is read out of app.py
                       with ast (literal_eval of the assigned tuple), and a fact is marked when its
                       lower-cased text contains one of them — what `_fact_has_correction` does.
                       Importing app would open the studio db and start the scheduler (order 32).
  b. caught before / after, per month
                       before = CATCHES.jsonl entries minus the ones a retraction line names
                       (142_*), + regate and sealgate refusals of LIVE targets (their logs are
                       mostly test runs on /tmp and fixtures, which are not catches), + builder runs
                       whose report check was FLAGGED.
                       after = facts whose marker arrived after their first write, per the ledger.
  c. time-to-correction  marker write − first write, from the ledger. The ledger keeps hashes,
                       no text, so a marker is dated only when the fact's first write already
                       carries it (that is "at birth": not a later correction). A fact that starts
                       at the 2026-09-24 baseline is pre-ledger; one whose marker shows up at a later
                       write cannot be told from one born marked. All of those are `unknown`:
                       never mtime, never a guess.
  d. vintage           per month M: of the facts first written in M, the share corrected within N
                       days. Only cohorts whose month ended N or more days ago get a rate; the rest
                       print `censored`.
  e. paralysis watch   leads added (facts whose first heading has the word LEAD) ÷ registry entries
                       opened, per month (Part 8 §2).
  f. specificity slot  canary recall and acquittal, printed as n/a until they are built (Part 8 §3).
memory/sessions/log.md gives dates only: its headings set the months the report spans.
"""
import argparse
import ast
import hashlib
import json
import re
import statistics
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

HELIX = Path.home() / "helix"
N_DAYS = 30
PATHS = {
    "app": "app.py",
    "facts": "memory/facts",
    "archive": "memory/archive",
    "ledger": "workspace/factgate/state/FACTS_LEDGER.jsonl",
    "catches": "workspace/catches/CATCHES.jsonl",
    "registry": "workspace/seam/registry/registry.json",
    "regate_ledger": "workspace/seam/registry/REGATE_LEDGER.jsonl",
    "regate": "workspace/regate/refusals.log",
    "sealgate": "workspace/sealgate/refusals.log",
    "runs": "build_orders/runs/RUNS.jsonl",
    "log": "memory/sessions/log.md",
}
LOG_HEADING = re.compile(r"^## (\d{4}-\d\d-\d\d) \d\d:\d\d", re.M)
REFUSAL = re.compile(r"^(\S+) exit=\d+ .*?\b(?:target|prereg)=(\S+)")
FIRST_HEADING = re.compile(r"^#+ .*$", re.M)
LEAD = re.compile(r"\bLEAD\b")
ISO_DAY = re.compile(r"\b(\d{4}-\d\d-\d\d)\b")
TEST_TARGET = re.compile(r"^/(?:var/)?tmp/|/fixtures/")


def markers(app_py):
    """`_CORRECTION_MARKERS` from app.py, by ast. Never imports it."""
    for node in ast.parse(Path(app_py).read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "_CORRECTION_MARKERS"
                                                for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise ValueError(f"no _CORRECTION_MARKERS assignment in {app_py}")


def has_correction(text, marks):
    """app._fact_has_correction's rule: a lower-case substring match."""
    low = (text or "").lower()
    return any(m in low for m in marks)


def load_facts(helix, marks):
    helix = Path(helix)
    out = []
    facts = helix / PATHS["facts"]
    archive = helix / PATHS["archive"]
    found = [("facts", facts / n) for n in sorted(p.name for p in facts.iterdir() if p.name.endswith(".md"))]
    if archive.is_dir():
        found += [("archive", p) for p in sorted(archive.rglob("*.md"))]
    for where, p in found:
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        out.append(dict(name=p.name, where=where, path=str(p.relative_to(helix)), text=text,
                        sha256=hashlib.sha256(raw).hexdigest(), marked=has_correction(text, marks),
                        hits=[m for m in marks if m in text.lower()]))
    return out


def _day(ts):
    """A ledger/log timestamp → a local date. `Z` times are converted; naive ones are local."""
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return (t.astimezone() if t.tzinfo else t).date()


def _month(d):
    return f"{d.year:04d}-{d.month:02d}"


def _month_end(m):
    y, mo = int(m[:4]), int(m[5:])
    return date(y, mo, monthrange(y, mo)[1])


def _months(first, last):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class Sources:
    """Reads the inputs once. A missing one is noted, never raised; broken lines are counted."""

    def __init__(self, helix):
        self.helix = Path(helix)
        self.unavailable, self.bad = [], {}

    def text(self, key):
        p = self.helix / PATHS[key]
        try:
            return p.read_text(errors="replace")
        except OSError:
            self.unavailable.append(key)
            return None

    def jsonl(self, key):
        t = self.text(key)
        rows = []
        for line in (t or "").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                self.bad[key] = self.bad.get(key, 0) + 1
        return rows


def classify(f, entries):
    """One fact against its ledger lines (in ledger order)."""
    c = dict(status="unmarked", first_written=None, marked_by=None, ttc_days=None, why="")
    if entries and entries[0].get("kind") != "baseline":
        c["first_written"] = entries[0].get("ts")
    if not f["marked"]:
        return c
    if not entries:
        c.update(status="unknown", why="not in the ledger")
    elif entries[0].get("kind") == "baseline":
        c.update(status="pre-ledger", why=f"first ledger line is the baseline of {entries[0].get('ts')}: "
                                          "written before the ledger, so its dates are unknown")
    else:
        k = max((i for i, e in enumerate(entries) if e.get("sha256") == f["sha256"]), default=None)
        if k is None:
            c.update(status="unknown", why="its current text matches no ledger write")
        elif k == 0:
            c.update(status="at-birth", marked_by=entries[0].get("ts"),
                     why="the marker is in the first write: not a later correction")
        else:
            c.update(status="unknown", marked_by=entries[k].get("ts"),
                     why=f"the marker is in write {k + 1} ({entries[k].get('ts')}); the ledger keeps "
                         f"hashes and no text, so whether writes 1..{k} already had it cannot be told")
    return c


def spread(values):
    if not values:
        return dict(median_days=None, p25_days=None, p75_days=None, min_days=None, max_days=None)
    v = sorted(values)
    q = statistics.quantiles(v, n=4, method="inclusive") if len(v) > 1 else [v[0]] * 3
    return dict(median_days=statistics.median(v), p25_days=q[0], p75_days=q[2],
                min_days=v[0], max_days=v[-1])


def compute(helix=HELIX, today=None, n_days=N_DAYS):
    helix = Path(helix)
    today = today or date.today()
    marks = markers(helix / PATHS["app"])
    src = Sources(helix)
    dates = [today]

    # (a) + (c): the facts and their ledger lines
    ledger_rows = src.jsonl("ledger")
    by_file = {}
    for r in sorted(ledger_rows, key=lambda r: int(r.get("line") or 0)):
        by_file.setdefault(r.get("file"), []).append(r)
    facts = []
    for f in load_facts(helix, marks):
        entries = by_file.get(f["name"], [])
        f.update(classify(f, entries))
        f["_first_entry"] = entries[0].get("kind") if entries else None
        for k in ("first_written", "marked_by"):
            if f[k]:
                dates.append(_day(f[k]))
        facts.append(f)
    share = dict(marked=sum(f["marked"] for f in facts), total=len(facts))
    for w in ("facts", "archive"):
        share[w] = [sum(f["marked"] for f in facts if f["where"] == w), sum(f["where"] == w for f in facts)]
    known = [f["ttc_days"] for f in facts if f["status"] == "after" and f["ttc_days"] is not None]
    ttc = dict(known=len(known),
               unknown=sum(f["status"] in ("unknown", "pre-ledger") for f in facts),
               at_birth=sum(f["status"] == "at-birth" for f in facts), **spread(known))
    baseline_ts = sorted(r.get("ts") for r in ledger_rows if r.get("kind") == "baseline" and r.get("ts"))

    # (b) before: catches minus retractions, live refusals, FLAGGED report checks
    before_events = []
    catches = src.jsonl("catches")
    retractions = [c for c in catches if c.get("kind") == "retraction"]
    retracted = {int(n) for c in retractions for n in (c.get("retracts") or []) if str(n).isdigit()}
    entries = [c for c in catches if c.get("kind") != "retraction"]
    ns = {int(c["n"]) for c in entries if str(c.get("n", "")).isdigit()}
    counted = [c for c in entries if not (str(c.get("n", "")).isdigit() and int(c["n"]) in retracted)]
    for c in counted:
        try:
            before_events.append(_day(c["date"]))
        except (KeyError, ValueError):
            src.bad["catches"] = src.bad.get("catches", 0) + 1
    before = dict(catches=dict(entries=len(entries), retracted=len(retracted & ns), counted=len(counted),
                               retraction_lines=len(retractions),
                               dangling_retractions=sorted(retracted - ns),
                               post_hoc_stage=sum(c.get("stage") == "post-hoc" for c in counted)))
    for key in ("regate", "sealgate"):
        live = tests = unparsed = 0
        for line in (src.text(key) or "").splitlines():
            m = REFUSAL.match(line)
            try:
                d = _day(m.group(1)) if m else None
            except ValueError:
                d = None
            if d is None:
                unparsed += bool(line.strip())
            elif TEST_TARGET.search(m.group(2)):
                tests += 1
            else:
                live += 1
                before_events.append(d)
        before[key] = dict(live=live, test_runs=tests, unparsed=unparsed)
    flagged = 0
    for r in src.jsonl("runs"):
        if "event" in r or "queue" in r or r.get("report_check") != "FLAGGED":
            continue
        try:
            before_events.append(_day(r["started"]))
            flagged += 1
        except (KeyError, ValueError):
            src.bad["runs"] = src.bad.get("runs", 0) + 1
    before["reportcheck_flagged"] = flagged
    dates += before_events

    # (e) leads and registry entries opened
    leads, leads_undated = [], 0
    for f in facts:
        h = FIRST_HEADING.search(f["text"])
        if not (h and LEAD.search(h.group(0))):
            continue
        d = None
        if f["first_written"]:
            d = _day(f["first_written"])
        elif ISO_DAY.search(h.group(0)):
            try:
                d = date.fromisoformat(ISO_DAY.search(h.group(0)).group(1))
            except ValueError:
                d = None
        if d:
            leads.append(d)
        else:
            leads_undated += 1
    reg_text = src.text("registry")
    try:
        registry = json.loads(reg_text) if reg_text is not None else {}
    except ValueError:
        registry, src.bad["registry"] = {}, 1
    first_in_ledger = {}
    for r in src.jsonl("regate_ledger"):
        first_in_ledger.setdefault(r.get("entry"), r.get("ts"))
    opened, opened_undated = [], 0
    for eid, e in (registry.items() if isinstance(registry, dict) else []):
        ts = (e.get("registered") if isinstance(e, dict) else None) or first_in_ledger.get(eid)
        try:
            opened.append(_day(ts))
        except (TypeError, ValueError):
            opened_undated += 1
    dates += leads + opened

    # the month span: log.md's dates (only its dates) and every dated event above
    log = src.text("log") or ""
    sessions = []
    for s in LOG_HEADING.findall(log):
        try:
            sessions.append(date.fromisoformat(s))
        except ValueError:
            pass
    dates += sessions
    months = {m: dict(before=0, after=0, after_unknown=0, written=0, sessions=0, leads=0, opened=0)
              for m in _months(min(dates), today)}

    def bump(d, key):
        if _month(d) in months:
            months[_month(d)][key] += 1

    for d in before_events:
        bump(d, "before")
    for d in sessions:
        bump(d, "sessions")
    for d in leads:
        bump(d, "leads")
    for d in opened:
        bump(d, "opened")
    for f in facts:
        if f["first_written"]:
            bump(_day(f["first_written"]), "written")
        if f["status"] == "after":
            bump(_day(f["marked_by"]), "after")
        elif f["status"] == "unknown" and f["marked_by"]:
            bump(_day(f["marked_by"]), "after_unknown")

    # (d) vintage: fully-aged cohorts only
    vintage = {}
    for m in months:
        ages_on = _month_end(m) + timedelta(days=n_days)
        cohort = [f for f in facts if f["first_written"] and _month(_day(f["first_written"])) == m]
        if today < ages_on:
            vintage[m] = dict(state="censored", ages_on=ages_on.isoformat(), written=len(cohort), rate=None)
            continue
        corrected = sum(f["status"] == "after" and f["ttc_days"] <= n_days for f in cohort)
        vintage[m] = dict(state="aged", written=len(cohort), corrected=corrected,
                          unknown=sum(f["status"] == "unknown" for f in cohort),
                          at_birth=sum(f["status"] == "at-birth" for f in cohort),
                          rate=(corrected / len(cohort)) if cohort else None)

    pre = [f for f in facts if f["_first_entry"] == "baseline"]
    for f in facts:
        del f["text"], f["_first_entry"]
    return dict(today=today.isoformat(), n_days=n_days, markers=list(marks), share=share, ttc=ttc,
                before=before, months=months, vintage=vintage,
                pre_ledger=dict(facts=len(pre), marked=sum(f["marked"] for f in pre)),
                unledgered=sum(1 for f in facts if f["status"] != "pre-ledger" and not f["first_written"]
                               and f["name"] not in by_file),
                ledger_baseline=baseline_ts[0] if baseline_ts else None,
                leads_undated=leads_undated, opened_undated=opened_undated,
                facts=facts, unavailable=src.unavailable, bad_lines=src.bad)


def _pct(a, b):
    return f"{a / b:.1%}" if b else "n/a"


def _ratio(b, a, u):
    return f"{b}:{a}" + (f" (+{u} unknown)" if u else "")


def _ttc_text(t):
    if not t["known"]:
        return f"median n/a (0 known, {t['unknown']} unknown)"
    return (f"median {t['median_days']:.1f} d (p25 {t['p25_days']:.1f} – p75 {t['p75_days']:.1f}, "
            f"min {t['min_days']:.1f}, max {t['max_days']:.1f}; {t['known']} known, {t['unknown']} unknown)")


def render(res):
    s, b, t, n = res["share"], res["before"], res["ttc"], res["n_days"]
    c = b["catches"]
    out = [f"# Correction-rate report — {res['today']} (N = {n} days)",
           "🔴 Read beside the specificity slot (f): these numbers mean nothing alone until item 12 exists.",
           "   A falling rate with no canary to prove the machinery still fires can mean the record went",
           "   quiet, not clean (134_* item 18 refinement ii).",
           "",
           "## (a) correction share",
           f"- {s['marked']}/{s['total']} marked ({_pct(s['marked'], s['total'])}) · facts "
           f"{s['facts'][0]}/{s['facts'][1]} · archive {s['archive'][0]}/{s['archive'][1]}",
           f"- rule: app.py's {len(res['markers'])} `_CORRECTION_MARKERS` (read by ast), lower-case substring",
           "- 134_* baseline: 74/193 = 38% on 2026-09-23, counted with a wider marker set "
           "(correction / superseded / killed / falsified), so not the same ruler",
           "",
           "## (b) caught before vs caught after, per month",
           f"- before = CATCHES {c['counted']} ({c['entries']} entries − {c['retracted']} retracted by "
           f"{c['retraction_lines']} retraction lines; {c['post_hoc_stage']} of them stage=post-hoc)"
           f" + regate live refusals {b['regate']['live']} + sealgate live refusals {b['sealgate']['live']}"
           f" + reportcheck FLAGGED {b['reportcheck_flagged']}",
           f"- not counted: regate {b['regate']['test_runs']} and sealgate {b['sealgate']['test_runs']} refusals "
           "of /tmp or fixture targets (test runs)"
           + (f"; dangling retractions {c['dangling_retractions']}" if c["dangling_retractions"] else ""),
           "- after = facts whose marker arrived after their first write (ledger); unknown = marked, "
           "but the ledger cannot say when"]
    for m, v in res["months"].items():
        out.append(f"- {m}: before:after {_ratio(v['before'], v['after'], v['after_unknown'])}")
    out += ["",
            "## (c) time-to-correction",
            f"- {_ttc_text(t)}",
            f"- at birth: {t['at_birth']} (the marker is in the fact's first write: not a later correction)",
            f"- pre-ledger: {res['pre_ledger']['facts']} facts ({res['pre_ledger']['marked']} marked) start at the "
            f"ledger's baseline ({res['ledger_baseline'] or 'none'}); their dates are unknown",
            f"- not in the ledger: {res['unledgered']} facts",
            "",
            f"## (d) vintage: share of month M's facts corrected within {n} days (fully-aged cohorts only)"]
    for m, v in res["vintage"].items():
        if v["state"] == "censored":
            out.append(f"- {m}: censored ({v['written']} facts written; ages on {v['ages_on']})")
        elif not v["written"]:
            out.append(f"- {m}: aged, 0 facts written — no cohort")
        else:
            out.append(f"- {m}: aged, {v['corrected']}/{v['written']} corrected within {n} d = {v['rate']:.0%}"
                       f" · unknown {v['unknown']} · at birth {v['at_birth']}")
    out.append(f"- pre-ledger: {res['pre_ledger']['facts']} facts, month unknown — not a cohort")
    out += ["", "## (e) paralysis watch: leads added ÷ registry entries opened"]
    for m, v in res["months"].items():
        r = f"{v['leads'] / v['opened']:.2f}" if v["opened"] else "n/a"
        out.append(f"- {m}: {v['leads']} leads / {v['opened']} opened = {r}")
    if res["leads_undated"]:
        out.append(f"- {res['leads_undated']} lead with no date (no ledger write, no date in its heading)")
    if res["opened_undated"]:
        out.append(f"- {res['opened_undated']} registry entries with no date")
    out += ["", "## (f) specificity slot",
            "- canary recall: n/a (item 12 not built)",
            "- acquittal: n/a (not built)"]
    if res["unavailable"] or res["bad_lines"]:
        out += ["", "## sources"]
        out += [f"- {k}: source unavailable" for k in res["unavailable"]]
        out += [f"- {k}: {v} unreadable lines skipped" for k, v in res["bad_lines"].items()]
    return "\n".join(out) + "\n"


def correction_line(helix=HELIX, today=None):
    """The nightly's `- correction:` line. Report only. Never raises."""
    try:
        res = compute(helix, today=today)
        s, n = res["share"], res["n_days"]
        today = date.fromisoformat(res["today"])
        aged = [m for m in res["months"] if today >= _month_end(m) + timedelta(days=n)]
        if aged:
            v = res["months"][aged[-1]]
            ba = f"before:after {aged[-1]}: {_ratio(v['before'], v['after'], v['after_unknown'])}"
        else:
            first = next(iter(res["months"]))
            ba = (f"before:after: no aged month yet ({first} ages on "
                  f"{_month_end(first) + timedelta(days=n)})")
        line = (f"- correction: {s['marked']}/{s['total']} marked ({_pct(s['marked'], s['total'])}) · {ba}"
                f" · time-to-correction: {_ttc_text(res['ttc'])}")
        for k in res["unavailable"]:
            line += f" · {k}: source unavailable"
        return line
    except Exception as e:
        return f"- correction: 🔴 metric failed ({type(e).__name__}: {e})"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="correction_rate.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="print the correction-rate report")
    r.add_argument("--n-days", type=int, default=N_DAYS)
    r.add_argument("--json", action="store_true")
    r.add_argument("--helix", default=str(HELIX))
    a = ap.parse_args(argv)
    if a.n_days < 1:
        ap.error("--n-days must be 1 or more")
    res = compute(Path(a.helix), n_days=a.n_days)
    sys.stdout.write(json.dumps(res, indent=1, ensure_ascii=False) + "\n" if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
