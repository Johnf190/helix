#!/usr/bin/env python3
"""ANCHOR — command line.

    python3 anchor.py <dir> [--net] [--json out.json] [--md REPORT.md] [--top 25]

Reads a tree of markdown, extracts every claim that is forced to commit to something, checks
the commitment, and prints three numbers: anchored, broken, and the share it could not check.
It never writes to the tree it audits.
"""
import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import core
from core import (HELIX, Claim, Resolver, check, dedupe, extract, load_rules,
                  resurrections, walk, weigh)

BAR = "─" * 66


def group(claims):
    """One finding per line of text, listing every commitment that line failed."""
    g = {}
    for c in claims:
        k = (c.file, c.line)
        e = g.setdefault(k, dict(file=c.file, line=c.line, weight=c.weight, detail=c.detail,
                                 text=c.text, tokens=[], kinds=set()))
        e["tokens"].append(c.token)
        e["kinds"].add(c.kind)
        e["weight"] = max(e["weight"], c.weight)
    return sorted(g.values(), key=lambda e: (-e["weight"], e["file"]))


def gather_quotes() -> str:
    hay = []
    for p in [HELIX / "memory" / "sessions" / "log.md", HELIX / "memory" / "notepad.json"]:
        if p.exists():
            hay.append(p.read_text(encoding="utf8", errors="replace"))
    import re
    return re.sub(r"\s+", " ", " ".join(hay)).lower()


DEFAULT_EXCLUDE = ("anchor/REPORT_*.md", "REPORT_*.md", "**/ANCHOR_*.md")


def run(root: pathlib.Path, net=False, top=25, exclude=DEFAULT_EXCLUDE):
    t0 = time.time()
    core.RULES = load_rules()
    files = walk(root, exclude=exclude)
    claims: list[Claim] = []
    for p, rel in files:
        claims += extract(p, rel)
    claims = dedupe(claims)
    import os
    roots = [root, HELIX, HELIX / "workspace", pathlib.Path.home() / "claude" / "projects"]
    # A claim's artifact may live on a disk that is not always attached. Naming the disk is the
    # fix the BOX2 recovery taught: a path without a host is not a location.
    roots += [pathlib.Path(x) for x in os.environ.get("ANCHOR_EXTRA_ROOTS", "").split(":") if x]
    res = Resolver(roots)
    check(claims, root, res, core.RULES, gather_quotes(), net=net)
    weigh(claims)
    by = {}
    for c in claims:
        by.setdefault(c.verdict, []).append(c)
    total = len(claims)
    checked = len(by.get("ok", [])) + len(by.get("broken", []))
    eps = (len(by.get("unanchorable", [])) + len(by.get("skipped", []))) / total if total else 0
    checked = checked + len(by.get("stale", []))
    broken = sorted(by.get("broken", []), key=lambda c: -c.weight)
    stale = sorted(by.get("stale", []), key=lambda c: -c.weight)
    # 216 lines saying "results/probe-study6_L3.json is missing" is one finding, not 216. Cluster the
    # missing artifacts by the tree they belong to, so the reader sees the shape of the hole.
    clusters = {}
    for c in broken:
        if c.kind != "path": continue
        topdir = str(pathlib.PurePosixPath(c.token)).lstrip("~/").split("/")[0]
        clusters.setdefault(topdir, []).append(c)
    clusters = sorted(((k, len(v), sorted({x.file for x in v})[:3]) for k, v in clusters.items()),
                      key=lambda kv: -kv[1])
    res_hits = resurrections(files)
    gaps = {}
    for c in claims:
        if c.verdict == "unanchorable" and c.kind == "count":
            gaps[c.detail] = gaps.get(c.detail, 0) + 1
    return dict(root=str(root), files=len(files), claims=claims, total=total, checked=checked,
                ok=len(by.get("ok", [])), broken=broken, unanchorable=len(by.get("unanchorable", [])),
                skipped=len(by.get("skipped", [])), epsilon=eps, secs=round(time.time() - t0, 1), top=top,
                resurrections=res_hits, gaps=sorted(gaps.items(), key=lambda kv: -kv[1])[:12],
                stale=stale, clusters=clusters[:12])


def render(r) -> str:
    L = [f"# ANCHOR — {r['root']}", "",
         f"{r['files']} documents · {r['total']} claims · {r['secs']}s", "",
         "| | claims | share |", "|---|---|---|",
         f"| **anchored** — checked, and the artifact is there | {r['ok']} | {r['ok']/max(r['total'],1):.1%} |",
         f"| **broken** — checked, and it is not | **{len(r['broken'])}** | {len(r['broken'])/max(r['total'],1):.1%} |",
         f"| unanchorable — no commitment this tool can reach | {r['unanchorable']} | {r['unanchorable']/max(r['total'],1):.1%} |",
         f"| stale — true when written, not now | {len(r['stale'])} | {len(r['stale'])/max(r['total'],1):.1%} |",
         f"| skipped — needs a flag (network) | {r['skipped']} | {r['skipped']/max(r['total'],1):.1%} |",
         "",
         f"**ε = {r['epsilon']:.1%}** — the share of claims this instrument cannot check at all. "
         "It is printed, not hidden: an auditor that reports only what it can see is the failure "
         "mode this whole framework exists to catch.", ""]
    if r["broken"]:
        L += [f"## Broken, heaviest first", "",
              "Weight is how much the record leans on the file a claim lives in — how many other "
              "documents cite it — times how hard the claim is.", "",
              "| w | kind | claim | what is wrong | where |", "|---|---|---|---|---|"]
        for g in group(r["broken"])[:r["top"]]:
            t = g["text"].replace("|", "\\|")[:100]
            L.append(f"| {g['weight']} | {'/'.join(sorted(g['kinds']))} | {t} | "
                     f"{', '.join(g['tokens'])[:60]} — {g['detail'][:45]} | `{g['file']}:{g['line']}` |")
        L.append("")
    if r.get("gaps"):
        L += ["## What the instrument cannot yet check", "",
              "Each line is a counting rule that does not exist yet. This is the backlog that "
              "lowers ε — it is deliberately visible.", "", "| missing rule | claims |", "|---|---|"]
        L += [f"| {g} | {n} |" for g, n in r["gaps"]]
        L.append("")
    if r.get("resurrections"):
        L += ["## Graveyard breaches", "",
              "Operating discipline, rule 6: a killed claim may not come back quietly. These lines "
              "assert one without marking it dead.", "", "| claim | where | line |", "|---|---|---|"]
        for h in r["resurrections"]:
            L.append(f"| {h['claim']} | `{h['file']}:{h['line']}` | {h['text'][:90].replace('|','\\|')} |")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=str(HELIX / "memory" / "facts"))
    ap.add_argument("--net", action="store_true", help="also check URLs")
    ap.add_argument("--json", dest="js")
    ap.add_argument("--md", dest="md")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDE),
                    help="glob patterns, relative to the root, to leave out")
    a = ap.parse_args()
    r = run(pathlib.Path(a.root).expanduser(), net=a.net, top=a.top, exclude=tuple(a.exclude))
    print(BAR)
    print(f"ANCHOR  {r['root']}")
    print(f"  {r['files']:4d} documents   {r['total']:5d} claims   {r['secs']}s")
    print(f"  anchored      {r['ok']:5d}")
    print(f"  BROKEN        {len(r['broken']):5d}")
    print(f"  unanchorable  {r['unanchorable']:5d}   (ε = {r['epsilon']:.1%})")
    print(f"  stale         {len(r['stale']):5d}   (true when written, not now)")
    print(f"  skipped       {r['skipped']:5d}")
    print(BAR)
    if r["clusters"]:
        print("  MISSING ARTIFACTS, by tree")
        for topdir, n, files in r["clusters"]:
            print(f"    {n:4d}  {topdir + '/':28s} cited in {', '.join(f[:34] for f in files)}")
        print(BAR)
    if r["stale"]:
        print("  STALE NUMBERS")
        for c in r["stale"][:8]:
            print(f"    {c.file}:{c.line}  {c.detail}")
            print(f"      {c.text[:92]}")
        print(BAR)
    for g in group(r["broken"])[:r["top"]]:
        print(f"  w{g['weight']:<6} {g['file']}:{g['line']}")
        print(f"          {g['detail']}")
        print(f"          missing: {', '.join(g['tokens'])[:96]}")
    if r["resurrections"]:
        print(BAR)
        print(f"  GRAVEYARD — {len(r['resurrections'])} line(s) assert a killed claim without saying it is dead")
        for h in r["resurrections"][:8]:
            print(f"    {h['file']}:{h['line']}  [{h['claim']}]")
            print(f"      {h['text'][:96]}")
    if a.md:
        pathlib.Path(a.md).write_text(render(r)); print(f"\nwrote {a.md}")
    if a.js:
        def plain(v):
            if isinstance(v, list):
                return [plain(x) for x in v]
            if isinstance(v, tuple):
                return [plain(x) for x in v]
            if isinstance(v, set):
                return sorted(v)
            if hasattr(v, "as_dict"):
                return v.as_dict()
            return v
        pathlib.Path(a.js).write_text(json.dumps({k: plain(v) for k, v in r.items()},
                                                 ensure_ascii=False, indent=1))
        print(f"wrote {a.js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
