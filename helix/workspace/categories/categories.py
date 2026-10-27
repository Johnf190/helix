#!/usr/bin/env python3
"""categories.py — ORDER 65 (upgrade item 28, phase A). A fact may declare its own category.

The category names live in CATEGORIES.txt beside this file and nowhere else: no name is written
in Python. A fact declares its category with a line in its first 15 lines:

    Category: project: boats
    Category: project: boats · rules        (at most two: one project + one kind; first is primary)

Reads the list file and, for primary(), one fact. Only `assign` writes, and only ASSIGNED.csv.

    load(path=DEFAULT) -> [Category]        malformed lines are skipped and listed in load_errors
    declared(text) -> (name|None, problem|None)
    declared_all(text) -> ([names], problem|None)
    primary(path_or_text) -> name|None      never raises

ORDER 73b (item 28, phase B): facts are append-only and builders never write them, so the old facts
get their category from a sidecar, ASSIGNED.csv (file,primary,secondary,source), written only by

    categories.py assign [--dry]            BACKFILL_PROPOSAL.csv + OVERRIDES.csv (absent is fine)
    load_assigned(path=ASSIGNED) -> {file: [names]}
    category_of(name, text, cats, assigned) -> ([names], "declared"|"assigned"|None)
    census(facts=FACTS) -> {declared, assigned, none: [files], bad: [(file, problem)]}
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE / "CATEGORIES.txt"
FACTS = HERE.parents[1] / "memory" / "facts"
PROPOSAL = HERE / "BACKFILL_PROPOSAL.csv"
OVERRIDES = HERE / "OVERRIDES.csv"          # written by Odin after the user approves; absent is fine
ASSIGNED = HERE / "ASSIGNED.csv"            # written by assign() only, never by hand
ASSIGNED_FIELDS = ["file", "primary", "secondary", "source"]
NEVER_OPEN = re.compile(r"credential|accounts|\.env", re.I)     # as propose_backfill: never opened
# mindgraph's seven lobes, so the v7 orb keeps seven (test_mindgraph checks they still agree)
LOBES = ("helix", "research", "finance", "music", "mapapp", "making", "life")
HEAD_LINES = 15
MAX_NAMES = 2

Category = namedtuple("Category", "name lobe colour desc")

_KEY = re.compile(r"^Category:\s*(.+)$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_SPLIT = re.compile(r"\s+·\s+|,")

load_errors = []          # [(line number, line, reason)] from the most recent load()


def _norm(name):
    return " ".join(name.split()).lower()


def load(path=DEFAULT):
    """Every well-formed line of the list, in file order. A missing file is an empty list."""
    global load_errors
    errs, out, seen = [], [], set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        load_errors = [(0, str(path), f"unreadable: {e.__class__.__name__}")]
        return []
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            errs.append((i, raw, f"expected 4 fields, got {len(parts)}"))
            continue
        name, lobe, colour, desc = parts
        name = " ".join(name.split())
        if not name:
            errs.append((i, raw, "empty name"))
        elif lobe not in LOBES:
            errs.append((i, raw, f"unknown lobe: {lobe}"))
        elif not _COLOUR.match(colour):
            errs.append((i, raw, f"bad colour: {colour}"))
        elif _norm(name) in seen:
            errs.append((i, raw, f"duplicate name: {name}"))
        else:
            seen.add(_norm(name))
            out.append(Category(name, lobe, colour, desc))
    load_errors = errs
    return out


def declared_all(text, cats=None):
    """([canonical names], problem). Only the first 15 lines are looked at; the key is
    case-sensitive, the names are matched ignoring case and repeated spaces."""
    if cats is None:
        cats = load()
    known = {_norm(c.name): c.name for c in cats}
    for line in (text or "").splitlines()[:HEAD_LINES]:
        m = _KEY.match(line.strip())
        if not m:
            continue
        raw = [p.strip() for p in _SPLIT.split(m.group(1)) if p.strip()]
        if not raw:
            return [], "empty"
        if len(raw) > MAX_NAMES:
            return [], f"too many: {len(raw)} (at most {MAX_NAMES})"
        names = []
        for r in raw:
            if _norm(r) not in known:
                return [], f"unknown: {r}"
            names.append(known[_norm(r)])
        return names, None
    return [], None


def declared(text, cats=None):
    names, problem = declared_all(text, cats)
    return (names[0] if names else None), problem


def primary(path_or_text, cats=None):
    """The declared primary category of a fact file (or of its text). None on anything wrong."""
    try:
        s = os.fspath(path_or_text)
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        if "\n" not in s and (isinstance(path_or_text, os.PathLike) or os.path.isfile(s)
                              or s.endswith(".md")):
            with open(s, "r", encoding="utf-8", errors="replace") as fh:
                s = "".join(fh.readline() for _ in range(HEAD_LINES))
        return declared(s, cats)[0]
    except Exception:
        return None


def by_name(cats=None):
    """{name: Category} for the list."""
    return {c.name: c for c in (load() if cats is None else cats)}


# ── ORDER 73b: the sidecar ──────────────────────────────────────────────────────────────────
def _canon(raw, cats):
    """([canonical names], first unknown name or None) for the non-empty names in raw."""
    known = {_norm(c.name): c.name for c in cats}
    names = []
    for r in raw:
        r = (r or "").strip()
        if not r:
            continue
        if _norm(r) not in known:
            return [], r
        names.append(known[_norm(r)])
    return names, None


def _rows(path):
    """The rows of a CSV file as dicts; a missing file is no rows."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def load_assigned(path=None, cats=None):
    """{file: [names]} from ASSIGNED.csv. Absent → {}. A row naming something no longer on the
    list is dropped (the list may change after assign ran)."""
    if cats is None:
        cats = load()
    out = {}
    for r in _rows(ASSIGNED if path is None else path):
        names, unknown = _canon([r.get("primary"), r.get("secondary")], cats)
        if names and unknown is None and r.get("file"):
            out[r["file"]] = names
    return out


def head(path):
    """The first 15 lines of a fact, opened "r" only. A credentials file is never opened → None."""
    path = Path(path)
    if NEVER_OPEN.search(path.name):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readline() for _ in range(HEAD_LINES))


def category_of(name, text, cats=None, assigned=None):
    """([names], source): a valid declared line → "declared"; else the ASSIGNED.csv row →
    "assigned"; else ([], None). A declared line with an unknown name falls through to ASSIGNED."""
    if cats is None:
        cats = load()
    names, problem = declared_all(text, cats)
    if names and problem is None:
        return names, "declared"
    if assigned is None:
        assigned = load_assigned(None, cats)
    if assigned.get(name):
        return list(assigned[name]), "assigned"
    return [], None


def _facts(facts):
    return sorted(p for p in Path(facts).iterdir() if p.suffix == ".md" and p.is_file())


def census(facts=None, cats=None, assigned=None):
    """Every fact by where its category comes from; bad = declared lines naming something wrong
    (those facts are also counted under assigned or none)."""
    if cats is None:
        cats = load()
    if assigned is None:
        assigned = load_assigned(None, cats)
    out = {"declared": [], "assigned": [], "none": [], "bad": []}
    for p in _facts(FACTS if facts is None else facts):
        text = head(p)
        problem = declared_all(text, cats)[1]
        if problem:
            out["bad"].append((p.name, problem))
        src = category_of(p.name, text, cats, assigned)[1]
        out[src or "none"].append(p.name)
    return out


def _refuse_memory(path):
    p = Path(path).resolve()
    if "memory" in p.parts:
        raise PermissionError(f"refusing to write under memory/: {p}")
    return p


def assign(facts=None, proposal=None, overrides=None, out=None, cats=None, dry=False):
    """Rewrite ASSIGNED.csv for every fact without a valid declared line: an OVERRIDES row wins
    (source override), else a high-confidence proposal row (source proposal), else not assigned
    (needs the user). A row naming something not on the list is not assigned (unknown)."""
    out = _refuse_memory(ASSIGNED if out is None else out)
    if cats is None:
        cats = load()
    prop = {r["file"]: r for r in _rows(PROPOSAL if proposal is None else proposal)}
    over = {r["file"]: r for r in _rows(OVERRIDES if overrides is None else overrides)}
    res = {"rows": [], "declared": [], "needs": [], "unknown": []}
    for p in _facts(FACTS if facts is None else facts):
        f = p.name
        names, problem = declared_all(head(p), cats)
        if names and problem is None:
            res["declared"].append(f)
            continue
        if f in over:
            row, source = over[f], "override"
        elif prop.get(f, {}).get("confidence") == "high":
            row, source = prop[f], "proposal"
        else:
            res["needs"].append(f)
            continue
        names, unknown = _canon([row.get("primary"), row.get("secondary")], cats)
        if unknown is not None or not names:
            res["unknown"].append((unknown or "(empty)", f))
            continue
        res["rows"].append({"file": f, "primary": names[0],
                            "secondary": names[1] if len(names) > 1 else "", "source": source})
    if not dry:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=ASSIGNED_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(res["rows"])
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        os.replace(tmp, out)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="fact categories: the ASSIGNED.csv sidecar (ORDER 73b)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assign", help="rewrite ASSIGNED.csv from the proposal and OVERRIDES.csv")
    a.add_argument("--dry", action="store_true", help="print the counts and lists, write nothing")
    a.add_argument("--facts", default=None)
    a.add_argument("--proposal", default=None)
    a.add_argument("--overrides", default=None)
    a.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    res = assign(args.facts, args.proposal, args.overrides, args.out, dry=args.dry)
    src = [r["source"] for r in res["rows"]]
    total = len(res["declared"]) + len(res["rows"]) + len(res["needs"]) + len(res["unknown"])
    print(f"assign: {total} facts · {len(res['declared'])} declared · {len(src)} assigned "
          f"({src.count('override')} override, {src.count('proposal')} proposal) · "
          f"{len(res['needs'])} needs the user · {len(res['unknown'])} unknown")
    for f in res["needs"]:
        print(f"needs the user: {f}")
    for name, f in res["unknown"]:
        print(f"unknown: {name} ({f})")
    out = ASSIGNED if args.out is None else args.out
    print(f"--dry: nothing written ({out})" if args.dry else f"wrote {out} ({len(src)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
