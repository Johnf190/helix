#!/usr/bin/env python3
"""SEAM rungs 0 and 1: lexical distance (no existence test), then the churn confound.
Pre-registered in PREREG_v2.md. Read-only."""
import collections, json, math, pathlib, re, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path.home() / "helix" / "workspace" / "anchor"))
import core, anchor as A
import numpy as np, statsmodels.api as sm

HOME = pathlib.Path.home()
OTHER_HOST = re.compile(r"__HOME__|__BULK__|/media/|/run/media|//192\.168\.")
WINDOW = 90 * 86400


# ---------------------------------------------------------------- rung 0: lexical distance
def lexical_class(c):
    """The declared prefix of the reference, and nothing else. Never asks the filesystem."""
    if c.kind == "url":
        return "d4"
    if c.kind == "cite":
        return "d1_in"
    t = c.token
    if OTHER_HOST.search(t):
        return "d3"
    if t.startswith("~") or t.startswith("/"):
        return "d2_abs"
    if t.startswith("memory/") or t.startswith("facts/"):
        return "d1_in"
    return "d2_rel"


# ---------------------------------------------------------------- rung 1: churn of the target
_churn_cache = {}


def nearest_existing(tok: str, root: pathlib.Path):
    cands = []
    if tok.startswith("~"):
        cands.append(pathlib.Path(tok).expanduser())
    elif tok.startswith("/"):
        cands.append(pathlib.Path(tok))
    else:
        cands += [root / tok, HOME / "helix" / tok, HOME / tok]
    for c in cands:
        p = c.parent
        for _ in range(6):
            if p.is_dir():
                return p
            p = p.parent
    return None


def churn_of(d: pathlib.Path, now: float):
    """Share of files under this directory touched in the last 90 days (depth-capped), and the
    number of commits touching it if it is inside a repository."""
    key = str(d)
    if key in _churn_cache:
        return _churn_cache[key]
    n = recent = 0
    try:
        for p in d.rglob("*"):
            if p.is_dir() or any(x in p.parts for x in core.SKIP_DIRS):
                continue
            n += 1
            try:
                if now - p.stat().st_mtime < WINDOW:
                    recent += 1
            except OSError:
                pass
            if n > 4000:
                break
    except OSError:
        pass
    frac = recent / n if n else None
    commits = 0
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            since = time.strftime("%Y-%m-%d", time.localtime(now - WINDOW))
            r2 = subprocess.run(["git", "-C", str(d), "log", "--since", since, "--format=%H", "--", "."],
                                capture_output=True, text=True, timeout=15)
            commits = len(r2.stdout.split())
    except Exception:
        pass
    _churn_cache[key] = (frac, n, commits)
    return _churn_cache[key]


def main():
    root = HOME / "helix" / "memory" / "facts"
    core.RULES = core.load_rules()
    r = A.run(root, net=False)
    now = time.time()
    rows = []
    for c in r["claims"]:
        if c.kind not in ("path", "cite", "url") or c.verdict not in ("ok", "broken"):
            continue
        cls = lexical_class(c)
        d = nearest_existing(c.token, root) if c.kind != "url" else None
        frac, n, commits = churn_of(d, now) if d else (None, 0, 0)
        rows.append(dict(cls=cls, broken=int(c.verdict == "broken"), file=c.file,
                         token=c.token, kind=c.kind,
                         churn=frac, churn_n=n, commits=commits,
                         anchor_dir=str(d) if d else ""))
    json.dump(rows, open("rows_rung1.json", "w"))
    print(f"SEAM rungs 0+1 — {len(rows)} claims from {len({x['file'] for x in rows})} files\n")

    # ---- rung 0 table
    print("RUNG 0 — distance by declared prefix only (no existence test)")
    print("  class    |   n  | broken |  P(broken)")
    by = collections.defaultdict(list)
    for x in rows: by[x["cls"]].append(x["broken"])
    for k in ("d1_in", "d2_rel", "d2_abs", "d3", "d4"):
        v = by.get(k, [])
        if v: print(f"  {k:8s} | {len(v):4d} | {sum(v):6d} |  {sum(v)/len(v):7.1%}")
    gap = (sum(by['d2_rel'])/len(by['d2_rel']) - sum(by['d1_in'])/len(by['d1_in'])) * 100
    print(f"\n  K1 band: |d2_rel − d1_in| = {gap:+.1f} points   "
          f"({'K1 FIRES — nothing to explain' if abs(gap) < 5 else 'survives K1'})")

    # ---- rung 1: churn
    have = [x for x in rows if x["churn"] is not None]
    print(f"\nRUNG 1 — churn measured for {len(have)}/{len(rows)} claims "
          f"({len(rows)-len(have)} have no surviving ancestor directory)")
    ch = sorted(x["churn"] for x in have)
    t1, t2 = ch[len(ch)//3], ch[2*len(ch)//3]
    print(f"  churn tertiles: ≤{t1:.2f}, ≤{t2:.2f}, >{t2:.2f}")
    print("  churn band | class    |   n  | broken |  P(broken)")
    for lo, hi, nm in ((-1, t1, "quiet"), (t1, t2, "middle"), (t2, 2, "busy")):
        sub = [x for x in have if lo < x["churn"] <= hi]
        for k in ("d1_in", "d2_rel", "d2_abs", "d3"):
            v = [x["broken"] for x in sub if x["cls"] == k]
            if v: print(f"  {nm:10s} | {k:8s} | {len(v):4d} | {sum(v):6d} |  {sum(v)/len(v):7.1%}")

    # ---- the model, clustered by file
    X_cls = sorted({x["cls"] for x in have})
    base = "d1_in" if "d1_in" in X_cls else X_cls[0]
    names, cols = [], []
    for k in X_cls:
        if k == base: continue
        names.append(k); cols.append([1.0 if x["cls"] == k else 0.0 for x in have])
    y = np.array([x["broken"] for x in have], float)
    groups = np.array([x["file"] for x in have])

    def fit(with_churn):
        nm = list(names); cl = [list(c) for c in cols]
        if with_churn:
            nm.append("log1p(churn)"); cl.append([math.log1p(x["churn"]) for x in have])
        X = sm.add_constant(np.array(cl, float).T, has_constant="add")
        m = sm.Logit(y, X).fit(disp=0, method="bfgs", maxiter=200)
        res = m.get_robustcov_results(cov_type="cluster", groups=groups)
        return nm, res

    for tag, wc in (("without churn", False), ("with churn", True)):
        nm, res = fit(wc)
        print(f"\n  logistic, {tag} (base = {base}, cluster-robust by file)")
        for i, n_ in enumerate(["const"] + nm):
            print(f"    {n_:14s} beta={res.params[i]:+7.3f}  z={res.tvalues[i]:+6.2f}  p={res.pvalues[i]:.4f}")
        if wc:
            b_w = {n_: res.params[i+1] for i, n_ in enumerate(nm)}
        else:
            b_o = {n_: res.params[i+1] for i, n_ in enumerate(nm)}
    print("\n  K2 band — shrinkage of each distance coefficient when churn enters:")
    for k in b_o:
        if k == "log1p(churn)": continue
        if abs(b_o[k]) < 1e-9: continue
        shrink = (1 - b_w[k]/b_o[k]) * 100
        z = [r for r in [None]][0] if False else None
        print(f"    {k:8s}  {b_o[k]:+.3f} → {b_w[k]:+.3f}   shrink {shrink:+.0f}%"
              f"   {'K2 FIRES' if shrink >= 70 else 'survives'}")


if __name__ == "__main__":
    main()
