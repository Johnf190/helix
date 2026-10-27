#!/usr/bin/env python3
"""The gap audit (order 36, item 4): what the record OMITS.

ANCHOR and reportcheck check what the record SAYS. This checks the complement: every piece of
WORK in a window must map to a RECORD, to a declared mapping rule, or to an explicit
"no record needed". What maps to nothing is an orphan, and orphans are listed, never fixed.

    gapaudit.py run --since <date> [--until <date>] [--json] [--helix DIR] [--rules FILE]

Dates are local days (`--until` is inclusive) or ISO timestamps. Read-only: it opens files for
reading and runs `git --no-optional-locks log`, nothing else.

WORK (each item has an id, a time and a short description):
  runs       build_orders/runs/RUNS.jsonl: one item per builder unit (queue events folded in)
  attempts   workspace/cycle/ATTEMPTS.jsonl: one item per line (seq)
  commits    every repo at workspace/*/.git, and build_orders/.git: `git log --all`, committer date
  schedules  memory/schedules.json: the scheduler keeps only `last_run` per schedule, so that is
             the one run per schedule it can see (odin.log's "HELIX scheduler: started" lines
             carry no time)
  worklane   workspace/cycle/TURNS.jsonl, lanes `work` and `sched`: app.py writes one line per
             background turn (since order 27, 2026-10-02). Proactive reports keep only the last
             job (frontdesk.set_last_job → memory/notepad.json), so they are no history.
RECORD:
  memory/facts/*.md, memory/archive/**/*.md, memory/sessions/log.md, build_orders/reports/*.md,
  workspace/catches/CATCHES.jsonl, the regate ledger workspace/seam/registry/REGATE_LEDGER.jsonl.

An item is covered when (first match wins):
  1. workspace/cycle/NO_RECORD_NEEDED.jsonl has a line {"id": <its id, or a commit's hash
     prefix of 7+ chars, or a run's unit>, "why": "..."}. That file is in the write wall since
     order 27 and on the hook's P7 list: the user or a build order writes it, Odin's shell cannot.
  2. a record names it (quotes of this audit's own output don't count, see QUOTED_LINE):
     a commit's hash (7+ hex chars, as a whole word), a run's unit name, an
     attempt's scope and `seq N` on the same line, a run's order number ("order 23", "ORDER_23").
  3. a rule in gapaudit_rules.json covers it. Each rule carries its reason; one without is
     not applied.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = Path.home() / "helix"
RULES = HERE / "gapaudit_rules.json"
SOURCES = ("runs", "attempts", "commits", "schedules", "worklane")
RULE_KINDS = ("own_report", "log_heading_near", "log_heading_after")
WORK_LANES = ("work", "sched")
GIT_TIMEOUT = 20
HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
HEADING = re.compile(r"^## (\d{4}-\d\d-\d\d \d\d:\d\d)[ \t]*$", re.M)
# The audit's own output quoted back into the record (a report pasting the orphan list, a log
# entry repeating the nightly's gaps line) is not a record of the work: it would clear every
# orphan it lists. So its orphan lines and `- gaps:` lines are dropped, and ids in its own
# form (`commit:seam@3e1be51abcd`, `run:<unit>`, …) are blanked, before anything is matched.
QUOTED_LINE = re.compile(r"^[ \t>]*- (?:gaps:|(?:run|attempt|commit|sched|turn):\S+ {2}\d{4}-\d\d-\d\d"
                         r" \d\d:\d\d {2}).*$", re.M)
QUOTED_ID = re.compile(r"\b(?:run|attempt|commit|sched|turn):[^\s,()`'\"]+")


def paths(helix):
    H = Path(helix)
    return {"runs": H / "build_orders/runs/RUNS.jsonl",
            "attempts": H / "workspace/cycle/ATTEMPTS.jsonl",
            "schedules": H / "memory/schedules.json",
            "worklane": H / "workspace/cycle/TURNS.jsonl",
            "optout": H / "workspace/cycle/NO_RECORD_NEEDED.jsonl",
            "workspace": H / "workspace",
            "build_orders": H / "build_orders",
            "facts": H / "memory/facts",
            "archive": H / "memory/archive",
            "log": H / "memory/sessions/log.md",
            "reports": H / "build_orders/reports",
            "catches": H / "workspace/catches/CATCHES.jsonl",
            "regate": H / "workspace/seam/registry/REGATE_LEDGER.jsonl"}


# ------------------------------------------------------------------ time

def ts(value):
    """An ISO time as an aware local datetime. A naive time is local (old RUNS lines, the
    scheduler, log.md headings). Anything else → None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def parse_when(s, end=False):
    """`YYYY-MM-DD` → local midnight (the next one with end=True: --until is inclusive), or an
    ISO timestamp as is. Raises ValueError."""
    if re.fullmatch(r"\d{4}-\d\d-\d\d", s or ""):
        day = datetime.fromisoformat(s)
        return (day + timedelta(days=1) if end else day).astimezone()
    t = ts(s)
    if t is None:
        raise ValueError(f"not a date or ISO time: {s!r}")
    return t


def label(t, end=False):
    if t.hour == t.minute == t.second == 0:
        return (t - timedelta(days=1) if end else t).date().isoformat()
    return t.isoformat(timespec="minutes")


def iso(t):
    return t.isoformat(timespec="seconds")


# ------------------------------------------------------------------ reading

def read_jsonl(path, problems):
    """The dict lines of a JSONL file, or None if it is missing or unreadable. Bad lines are
    skipped and counted in `problems`."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    rows, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        problems.append(f"{Path(path).name}: {bad} unreadable line(s) skipped")
    return rows


def item(source, iid, t, desc, **extra):
    return dict(source=source, id=iid, time=iso(t), desc=" ".join(str(desc).split())[:100],
                covered_by=None, _t=t, **extra)


def load_runs(p, problems):
    rows = read_jsonl(p["runs"], problems)
    if rows is None:
        return None
    units = {}
    for r in rows:
        unit = r.get("unit")
        if not isinstance(unit, str) or not unit:
            continue                                   # queue events: the units carry the work
        u = units.setdefault(unit, {"order": None, "t": None, "report": False, "trigger": None})
        u["order"] = u["order"] or (str(r["order"]) if r.get("order") is not None else None)
        u["trigger"] = u["trigger"] or r.get("trigger")
        u["report"] = u["report"] or r.get("report_exists") is True
        t = ts(r.get("started")) or (ts(r.get("at")) if r.get("event") == "order_start" else None)
        if t and (u["t"] is None or t < u["t"]):
            u["t"] = t
    out = []
    for unit, u in units.items():
        if u["t"] is None:
            problems.append(f"RUNS.jsonl: unit {unit} has no start time, skipped")
            continue
        out.append(item("runs", f"run:{unit}", u["t"],
                        f"order {u['order']} ({u['trigger'] or 'no trigger'})"
                        + ("" if u["report"] else ", no report written"),
                        unit=unit, order=u["order"], report_exists=u["report"]))
    return out


def load_attempts(p, problems):
    rows = read_jsonl(p["attempts"], problems)
    if rows is None:
        return None
    out = []
    for r in rows:
        t, seq = ts(r.get("ts")), r.get("seq")
        if t is None or not isinstance(seq, int):
            problems.append("ATTEMPTS.jsonl: a line with no ts or seq, skipped")
            continue
        scope = str(r.get("scope", ""))
        out.append(item("attempts", f"attempt:{seq}", t,
                        f"{scope} seq {seq} {r.get('kind', '?')} n={r.get('n', '?')}: "
                        f"{r.get('cmd', '')}"[:100], scope=scope, seq=seq))
    return out


def repos(p):
    found = sorted(d.parent for d in Path(p["workspace"]).glob("*/.git"))
    if (Path(p["build_orders"]) / ".git").exists():
        found.append(Path(p["build_orders"]))
    return found


def load_commits(p, problems):
    try:
        rs = repos(p)
    except OSError:
        rs = []
    out, read = [], 0
    for repo in rs:
        try:
            r = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), "log", "--all",
                                "--format=%H%x09%cI%x09%s"],
                               capture_output=True, text=True, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"commits: repo {repo.name} unreadable ({type(e).__name__})")
            continue
        if r.returncode != 0:
            first = next((l for l in r.stderr.splitlines() if l.strip()), f"exit {r.returncode}")
            if "does not have any commits" not in first:
                problems.append(f"commits: repo {repo.name} unreadable ({first.strip()[:80]})")
                continue
        read += 1
        for line in r.stdout.splitlines():
            h, _, rest = line.partition("\t")
            when, _, subject = rest.partition("\t")
            t = ts(when)
            if t is None or not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            out.append(item("commits", f"commit:{repo.name}@{h[:12]}", t, subject,
                            hash=h, repo=repo.name))
    return out if read else None


def load_schedules(p, problems):
    try:
        data = json.loads(Path(p["schedules"]).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        t = ts(s.get("last_run"))
        if t is None:
            continue                                   # never ran: no work to account for
        out.append(item("schedules", f"sched:{s.get('id')}@{s.get('last_run')}", t,
                        f"{s.get('name', '?')} (last run; the scheduler keeps no earlier ones)",
                        prompt=str(s.get("prompt", ""))))
    return out


def load_worklane(p, problems):
    rows = read_jsonl(p["worklane"], problems)
    if rows is None:
        return None
    out = []
    for r in rows:
        if r.get("lane") not in WORK_LANES:
            continue
        t = ts(r.get("ts"))
        if t is None:
            problems.append("TURNS.jsonl: a work-lane line with no readable ts, skipped")
            continue
        out.append(item("worklane", f"turn:{r['lane']}@{r['ts']}", t,
                        f"{r['lane']} turn, {r.get('model', '?')}, ${r.get('cost_usd', '?')}"))
    return out


LOADERS = {"runs": load_runs, "attempts": load_attempts, "commits": load_commits,
           "schedules": load_schedules, "worklane": load_worklane}


def load_records(p, problems):
    """[(name, text)] for every record file there is. A missing record source is said."""
    files = []
    for key, pattern in (("facts", "*.md"), ("archive", "**/*.md"), ("reports", "*.md")):
        d = Path(p[key])
        if d.is_dir():
            files += sorted(f for f in d.glob(pattern) if f.is_file())
        else:
            problems.append(f"record unavailable: {d}")
    for key in ("log", "catches", "regate"):
        f = Path(p[key])
        if f.is_file():
            files.append(f)
        else:
            problems.append(f"record unavailable: {f}")
    out = []
    for f in files:
        try:
            text = QUOTED_ID.sub(" ", QUOTED_LINE.sub("", f.read_text(errors="replace")))
            out.append((f.name if f.parent.name != "reports" else f"reports/{f.name}", text))
        except OSError:
            problems.append(f"record unreadable: {f}")
    return out


def log_entries(text):
    """[(heading time, first '**You:**' line)] from log.md."""
    out = []
    marks = list(HEADING.finditer(text))
    for k, m in enumerate(marks):
        t = ts(m.group(1).replace(" ", "T"))
        if t is None:
            continue
        body = text[m.end(): marks[k + 1].start() if k + 1 < len(marks) else len(text)]
        you = next((l[len("**You:** "):] for l in body.splitlines() if l.startswith("**You:** ")), "")
        out.append((t, you))
    return out


def load_rules(path, problems):
    try:
        rules = json.loads(Path(path).read_text())["rules"]
        assert isinstance(rules, list)
    except Exception as e:
        problems.append(f"rules: {path} unreadable ({type(e).__name__}), no mapping rules applied")
        return []
    ok = []
    for r in rules:
        if not isinstance(r, dict):
            problems.append("rules: a rule that is not an object, not applied")
            continue
        rid = r.get("id", "?")
        why = ("no reason" if not str(r.get("reason") or "").strip() else
               f"unknown source {r.get('source')!r}" if r.get("source") not in SOURCES else
               f"unknown kind {r.get('kind')!r}" if r.get("kind") not in RULE_KINDS else None)
        if why:
            problems.append(f"rule {rid}: not applied ({why})")
        else:
            ok.append(r)
    return ok


def load_optout(path, problems):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"NO_RECORD_NEEDED.jsonl line {n}: unreadable, ignored")
            continue
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip()
                and isinstance(row.get("why"), str) and row["why"].strip()):
            problems.append(f"NO_RECORD_NEEDED.jsonl line {n}: needs an id and a why, ignored")
            continue
        out.append((row["id"].strip(), row["why"].strip()))
    return out


# ------------------------------------------------------------------ coverage

def opted_out(i, optout):
    for oid, why in optout:
        if (oid == i["id"] or oid == i.get("unit")
                or (i["source"] == "commits" and len(oid) >= 7
                    and re.fullmatch(r"[0-9a-f]+", oid) and i["hash"].startswith(oid))):
            return f"no record needed: {why}"
    return None


def named(i, records, hexes):
    if i["source"] == "commits":
        for n in range(7, 41):
            if i["hash"][:n] in hexes:
                return f"named in {hexes[i['hash'][:n]]}"
        return None
    if i["source"] == "runs":
        hit = next((f"named in {name}" for name, text in records if i["unit"] in text), None)
        if hit or not i["order"]:
            return hit
        # "order 23", "ORDER_23", "order-23" — but not "order 23b", "order 230" or "orders 23"
        num = re.compile(rf"\border[ _-]?{re.escape(i['order'])}(?![0-9A-Za-z])", re.I)
        return next((f"order {i['order']} named in {name}" for name, text in records
                     if num.search(text)), None)
    if i["source"] == "attempts" and i["scope"]:
        seq = re.compile(rf"\bseq\W{{0,4}}{i['seq']}(?!\d)")
        for name, text in records:
            if i["scope"] in text and any(i["scope"] in l and seq.search(l)
                                          for l in text.splitlines()):
                return f"named in {name}"
    return None


def by_rule(i, rules, p, log):
    for r in rules:
        if r["source"] != i["source"]:
            continue
        mins = timedelta(minutes=float(r.get("minutes", 0)))
        floor = i["_t"].replace(second=0, microsecond=0)
        if r["kind"] == "own_report":
            if i.get("report_exists") and (Path(p["reports"]) / f"ORDER_{i['order']}_report.md").is_file():
                return f"rule {r['id']}: ORDER_{i['order']}_report.md"
        elif r["kind"] == "log_heading_near":
            hit = next((t for t, _ in log if abs(t - floor) <= mins), None)
            if hit:
                return f"rule {r['id']}: log.md {hit:%Y-%m-%d %H:%M}"
        elif r["kind"] == "log_heading_after":
            head = i.get("prompt", "")[:int(r.get("prompt_chars", 60))].strip()
            hit = next((t for t, you in log if floor <= t <= i["_t"] + mins
                        and head and you.startswith(head)), None)
            if hit:
                return f"rule {r['id']}: log.md {hit:%Y-%m-%d %H:%M}"
    return None


def audit(helix=HELIX, since=None, until=None, rules_path=RULES):
    """Every work item in [since, until) with what covers it (None = orphan)."""
    p, problems, unavailable, items = paths(helix), [], {}, []
    until = until or datetime.now().astimezone()
    since = since or until - timedelta(days=7)
    for src in SOURCES:
        try:
            got = LOADERS[src](p, problems)
        except Exception as e:                          # a source never stops the others
            got, why = None, f"{type(e).__name__}: {e}"
        else:
            why = "no readable repo" if src == "commits" else f"{p[src]} missing or unreadable"
        if got is None:
            unavailable[src] = why
            continue
        items += [i for i in got if since <= i["_t"] < until]
    records = load_records(p, problems)
    hexes = {}
    for name, text in records:
        for m in HEX.finditer(text):
            hexes.setdefault(m.group(0), name)
    log = log_entries(next((t for n, t in records if n == "log.md"), ""))
    rules = load_rules(rules_path, problems)
    optout = load_optout(p["optout"], problems)
    for i in items:
        i["covered_by"] = (opted_out(i, optout) or named(i, records, hexes)
                           or by_rule(i, rules, p, log))
    items.sort(key=lambda i: (SOURCES.index(i["source"]), i["_t"]))
    return {"since": iso(since), "until": iso(until), "since_label": label(since),
            "until_label": label(until, end=True), "items": items,
            "unavailable": unavailable, "problems": problems}


# ------------------------------------------------------------------ output

def render(res):
    items = res["items"]
    orphans = [i for i in items if not i["covered_by"]]
    out = [f"gapaudit {res['since_label']} → {res['until_label']}: {len(items)} work items, "
           f"{len(items) - len(orphans)} covered, {len(orphans)} orphans"]
    counts = [f"{s} {sum(i['source'] == s for i in items)}"
              f" ({sum(i['source'] == s for i in orphans)} orphans)"
              for s in SOURCES if s not in res["unavailable"]]
    out.append("by source: " + " · ".join(counts) if counts else "by source: none")
    out += [f"{s}: source unavailable ({why})" for s, why in res["unavailable"].items()]
    out += [f"! {w}" for w in res["problems"]]
    for s in SOURCES:
        mine = [i for i in orphans if i["source"] == s]
        if mine:
            out.append(f"## {s} — {len(mine)} orphans")
            out += [f"- {i['id']}  {i['_t']:%Y-%m-%d %H:%M}  {i['desc']}" for i in mine]
    return "\n".join(out)


def as_json(res):
    return dict(res, items=[{k: v for k, v in i.items() if k != "_t"} for i in res["items"]])


def gaps_line(helix=HELIX, now=None):
    """The nightly's `- gaps:` line. Report only. Never raises.

    "New since yesterday" = orphans whose own time is in the last 24 hours. The audit keeps
    no state, so an older item that has lost its record since yesterday is counted, but not
    as new."""
    try:
        now = now or datetime.now().astimezone()
        res = audit(helix, now - timedelta(days=7), now)
        orphans = sorted((i for i in res["items"] if not i["covered_by"]), key=lambda i: i["_t"])
        new = sum(i["_t"] >= now - timedelta(days=1) for i in orphans)
        line = f"- gaps: {len(orphans)} orphans in the last 7 days (new since yesterday: {new})"
        if orphans:
            line += " · first 3 by age: " + ", ".join(f"{i['id']} ({i['_t']:%Y-%m-%d})"
                                                       for i in orphans[:3])
        for s in res["unavailable"]:
            line += f" · {s}: source unavailable"
        return line
    except Exception as e:
        return f"- gaps: 🔴 audit failed ({type(e).__name__}: {e})"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gapaudit.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="list the orphans in a window")
    r.add_argument("--since", required=True)
    r.add_argument("--until")
    r.add_argument("--json", action="store_true")
    r.add_argument("--helix", default=str(HELIX))
    r.add_argument("--rules", default=str(RULES))
    a = ap.parse_args(argv)
    try:
        since = parse_when(a.since)
    except ValueError as e:
        print(f"gapaudit: bad --since: {e}", file=sys.stderr)
        return 2
    try:
        until = parse_when(a.until, end=True) if a.until else None
    except ValueError as e:
        print(f"gapaudit: bad --until: {e}", file=sys.stderr)
        return 2
    res = audit(Path(a.helix), since, until, rules_path=Path(a.rules))
    print(json.dumps(as_json(res), indent=1, ensure_ascii=False) if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
