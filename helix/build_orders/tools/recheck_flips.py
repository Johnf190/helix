"""Re-judge every flip of a diff_probe.py run SERIALLY: one process, no pool, a fresh catches file
truncated before each call. Maintained copy (order 18) of order 17's
quarantine/order17_evidence_20260925/recheck_flips.py, which stays untouched as evidence.
Changes: OLD/NEW/DIR are arguments and the hooks are loaded through diff_probe.setup(); every row's
probe labels are compared with the serial labels and the mismatch count is printed; order 17's
one-off AC3 section is not carried over.

Usage: recheck_flips.py --old HOOK --new HOOK --out DIR   (reads DIR/diff_probe_flips.json)"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_probe as P

ap = argparse.ArgumentParser()
ap.add_argument("--old", required=True); ap.add_argument("--new", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
P.setup(a.old, a.new)
HELIX = P.HELIX
flips = json.load(open(os.path.join(a.out, "diff_probe_flips.json")))
out = []
for x, y, cwd, c in flips:
    real = cwd.replace("~/helix", HELIX)
    P.own_catches()                                       # fresh files for every row
    t0 = time.time(); A = P.dec(P.old, c, real); t1 = time.time(); B = P.dec(P.new, c, real); t2 = time.time()
    out.append({"probe": [x, y], "serial": [A, B], "cwd": cwd, "cmd": c,
                "old_s": round(t1 - t0, 2), "new_s": round(t2 - t1, 2)})
json.dump(out, open(os.path.join(a.out, "recheck_flips.json"), "w"), indent=1)
bad = [r for r in out if r["probe"] != r["serial"]]
print(f"rows: {len(out)}; probe label == serial label on every row: {not bad} ({len(bad)} mismatches)")
for r in bad: print(f"  MISMATCH probe {r['probe']} serial {r['serial']} [{r['cwd']}] {r['cmd'][:110]!r}")
for kind in ("deny->allow", "allow->deny", "other"):
    rows = [r for r in out if (kind == "deny->allow" and r["serial"][0] != "allow" and r["serial"][1] == "allow")
            or (kind == "allow->deny" and r["serial"][0] == "allow" and r["serial"][1] != "allow")
            or (kind == "other" and (r["serial"][0] == "allow") == (r["serial"][1] == "allow"))]
    print(f"== serial {kind}: {len(rows)}")
    for r in rows: print(f"  {r['serial'][0]}->{r['serial'][1]} (probe {r['probe'][0]}->{r['probe'][1]}) [{r['cwd']}] {r['cmd'][:110]!r}")
