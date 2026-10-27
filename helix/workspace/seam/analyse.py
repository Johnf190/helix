#!/usr/bin/env python3
"""SEAM — measure P(broken) against referential distance. Pre-registered in PREREG.md."""
import json, math, pathlib, re, sys, statistics, collections, datetime
sys.path.insert(0, str(pathlib.Path.home() / "helix" / "workspace" / "anchor"))
import core, anchor as A

OTHER_HOST = re.compile(r"__HOME__|__BULK__|/media/|/run/media|//192\.168\.")
DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


def distance(c, root: pathlib.Path):
    """How far is the artifact from the sentence that claims it?"""
    if c.kind == "url":
        return 4
    if c.kind == "cite":
        return 1                      # a fact citing a fact: same tree
    t = c.token
    if OTHER_HOST.search(t):
        return 3
    if t.startswith("~") or t.startswith("/"):
        return 2                      # this machine, outside the corpus tree
    # a relative path: d1 if it resolves inside the corpus, else it was written about another tree
    p = (root / t)
    return 1 if p.exists() else 2


def age_days(path: pathlib.Path, today=None):
    today = today or datetime.date.today()
    txt = path.read_text(encoding="utf8", errors="replace")[:4000]
    ds = [datetime.date(int(a), int(b), int(c)) for a, b, c in DATE.findall(txt)
          if 2024 <= int(a) <= today.year]
    d = min(ds) if ds else datetime.date.fromtimestamp(path.stat().st_mtime)
    return max(0, (today - d).days)


def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n
    c = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    d = 1 + z * z / n
    return ((p + z * z / (2 * n) - c) / d, (p + z * z / (2 * n) + c) / d)


def run(root: pathlib.Path):
    core.RULES = core.load_rules()
    r = A.run(root, net=False)
    rows = []
    for c in r["claims"]:
        if c.kind not in ("path", "cite", "url"):
            continue
        if c.verdict not in ("ok", "broken"):
            continue
        d = distance(c, root)
        rows.append(dict(d=d, broken=1 if c.verdict == "broken" else 0,
                         age=age_days(root / c.file), file=c.file, token=c.token, kind=c.kind))
    return rows


def table(rows, key="d", label="d"):
    by = collections.defaultdict(list)
    for r in rows: by[r[key]].append(r["broken"])
    print(f"\n  {label:>3} | n     | broken |   P(broken)  | 95% CI")
    print("  " + "-" * 56)
    for k in sorted(by):
        v = by[k]; n = len(v); b = sum(v)
        lo, hi = wilson(b, n)
        print(f"  {k:>3} | {n:5d} | {b:6d} | {b/n:11.1%}  | {lo:.1%}–{hi:.1%}")
    return by


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home()/"helix"/"memory"/"facts")
    rows = run(root)
    print(f"SEAM — {root}\n  {len(rows)} checkable claims")
    by = table(rows)
    small = [k for k, v in by.items() if len(v) < 30]
    if small:
        print(f"\n  ⚠ classes with <30 claims: {small} — the pre-registration says do not lean on these")

    # stratify by age tertiles: does distance survive the control?
    ages = sorted(r["age"] for r in rows)
    t1, t2 = ages[len(ages)//3], ages[2*len(ages)//3]
    print(f"\n  age tertiles: ≤{t1}d, ≤{t2}d, >{t2}d")
    for lo, hi, name in ((0, t1, f"young ≤{t1}d"), (t1, t2, f"middle ≤{t2}d"), (t2, 10**6, f"old >{t2}d")):
        sub = [r for r in rows if lo <= r["age"] < hi]
        if not sub: continue
        print(f"\n  --- {name}  (n={len(sub)}) ---")
        table(sub)

    # H3: does distance rank better than recency?
    N = max(1, len(rows) // 10)
    by_dist = sorted(rows, key=lambda r: (-r["d"], -r["age"]))[:N]
    by_rec  = sorted(rows, key=lambda r: r["age"])[:N]
    import random
    random.seed(7)
    rnd = random.sample(rows, N)
    print(f"\n  H3 — top decile (n={N}) hit rate")
    for nm, s in (("by distance", by_dist), ("by recency", by_rec), ("random", rnd)):
        print(f"    {nm:12s} broken found = {sum(x['broken'] for x in s):4d}  ({sum(x['broken'] for x in s)/N:.1%})")
    json.dump(rows, open("rows_facts.json", "w"))
