#!/usr/bin/env python3
"""Tests for ANCHOR. Plain asserts, no framework, no network.

The one that matters most is test_it_can_return_nothing_here: an instrument that cannot
produce a null is a horoscope (operating discipline, rule 3)."""
import json, os, pathlib, shutil, sys, tempfile, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import core, anchor as A

PASS = FAIL = 0
def ok(cond, msg):
    global PASS, FAIL
    if cond: PASS += 1
    else:
        FAIL += 1
        print("  FAIL ", msg)

def corpus(files: dict) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="anchor_t_"))
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d

def run(d, **kw):
    core.RULES = core.load_rules()
    return A.run(d, **kw)

# ---------------------------------------------------------------- extraction
d = corpus({"a.md": "See `sub/real.py` and [[zz_no_such_fact]] at commit `abc1234`.\n"
                     "Link https://example.com/x and 169 fact files.\n",
            "sub/real.py": "x = 1\n"})
core.RULES = core.load_rules()
claims = core.dedupe(sum([core.extract(p, rel) for p, rel in core.walk(d)], []))
kinds = {c.kind for c in claims}
ok({"path", "cite", "git", "url"} <= kinds, f"all kinds extracted, got {kinds}")
ok(sum(1 for c in claims if c.kind == "path") == 1, "one path claim, not one per regex")

# ---------------------------------------------------------------- the three verdicts
r = run(d)
by = {c.token: c for c in r["claims"]}
ok(by["sub/real.py"].verdict == "ok", "a path that exists is anchored")
ok(any(c.verdict == "broken" for c in r["claims"] if c.kind == "cite"), "a missing citation is broken")
ok(all(c.verdict == "ok" for c in run(corpus({"z.md": "[[001_how_helix_remembers]]\n"}))["claims"] if c.kind == "cite"),
   "a citation that resolves is anchored")

d2 = corpus({"b.md": "The result is in `outputs/gone.json`, described in GONE.md.\n"})
r2 = run(d2)
v = {c.token: c.verdict for c in r2["claims"]}
ok(v.get("outputs/gone.json") == "broken", "a path with a location that is not there is broken")
ok(v.get("GONE.md") == "unanchorable", "a bare filename names a document, not a location")

# ---------------------------------------------------------------- counts
real = len(list((core.HELIX / "memory" / "facts").glob("*.md")))
d3 = corpus({"c.md": f"There are {real} fact files today.\n", "d.md": "There are 2 fact files.\n",
             "e.md": "Released in 2026 models of the thing.\n"})
r3 = run(d3)
cv = {(c.file, c.verdict) for c in r3["claims"] if c.kind == "count"}
ok(("c.md", "ok") in cv, "a count that recomputes is anchored")
ok(("d.md", "stale") in cv, "a count that no longer matches is STALE, not false")
ok(not any(f == "e.md" for f, _ in cv), "a year is not a count")

# ---------------------------------------------------------------- the graveyard
d4 = corpus({"live.md": "We can now use velocity-beats-level as the main signal.\n",
             "dead.md": "velocity-beats-level was KILLED out of sample, 0 wins / 4 fields.\n",
             "near.md": "The velocity-beats-level result\nis dead and stays dead.\n"})
res = core.resurrections(core.walk(d4))
hit = {h["file"] for h in res}
ok("live.md" in hit, "a dead claim asserted plainly is flagged")
ok("dead.md" not in hit, "a dead claim marked dead is not flagged")
ok("near.md" not in hit, "a kill marker on the next line counts (context window)")

# ---------------------------------------------------------------- the null
d5 = corpus({"clean.md": "A paragraph with no claims at all, just prose about the weather.\n"})
r5 = run(d5)
ok(len(r5["broken"]) == 0, "it can return nothing here")
ok(r5["total"] >= 0, "and still reports a total")

# ---------------------------------------------------------------- read-only
d6 = corpus({"x.md": "`nope/nothing.json`\n"})
before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d6.rglob("*") if p.is_file()}
run(d6)
after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d6.rglob("*") if p.is_file()}
ok(before == after, "the audited tree is never written to")

# ---------------------------------------------------------------- epsilon is reported
ok(0.0 <= r["epsilon"] <= 1.0 and isinstance(r["epsilon"], float), "epsilon is a real fraction")
ok(r["ok"] + len(r["broken"]) + r["unanchorable"] + r["skipped"] + len(r["stale"]) == r["total"],
   "the buckets partition the claims — nothing is silently dropped")

for d_ in (d, d2, d3, d4, d5, d6):
    shutil.rmtree(d_, ignore_errors=True)
print(f"\nANCHOR tests: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
