"""Differential probe for factgate_hook.py. Maintained copy (order 18) of order 17's
quarantine/order17_evidence_20260925/diff_probe.py, which stays untouched as evidence.

Every command in the frozen corpus (order 17's diff_probe_corpus.json, 3211 distinct Bash commands
from Odin's transcripts) is judged by the OLD and the NEW hook at cwd ~/helix AND ~/helix/workspace.
A decision comes from the hook's return value; the rule label comes from the catch line it wrote.

ORDER 18 — the label race (order 17 §4.2). Order 17 loaded the hooks once in the parent and every
pool worker shared ONE temporary CATCHES path, so concurrent truncate/append gave `deny/?` and wrong
rule labels. Now each worker gets its own temporary catches file in the pool initializer (the path
includes the pid), and a label is read from that worker's own file, truncated before each call, so
it can only be that call's line. A deny that did not write exactly one line is labelled `deny/?`.
Flips are sorted before they are written, so two runs give byte-identical output.

Usage (everything in the foreground; a slice fits in one Bash call):
  diff_probe.py --old HOOK --new HOOK --out DIR --slice i/N    judge corpus[i::N]
  diff_probe.py --old HOOK --new HOOK --out DIR --merge N      check coverage, print the result
"""
import argparse, asyncio, importlib.util, json, multiprocessing as mp, os, sys, tempfile
HELIX = os.path.expanduser("~/helix")
CORPUS = os.path.join(HELIX, "build_orders/quarantine/order17_evidence_20260925/diff_probe_corpus.json")
old = new = None


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m); m.BRAIN = HELIX
    m.FACTS = f"{HELIX}/memory/facts"; m.FACTGATE_DIR = f"{HELIX}/workspace/factgate"
    m.FACTGATE = f"{m.FACTGATE_DIR}/factgate.py"; m.FACTGATE_STATE = f"{m.FACTGATE_DIR}/state"
    m.RUNNER = f"{HELIX}/build_orders/run_order.sh"
    # P7's constants are also derived from the file's own location (a quarantine copy would guard
    # the quarantine tree): re-point them at the real build path.
    m.BUILD_ORDERS = f"{HELIX}/build_orders"
    m._P7_FILES = (f"{HELIX}/app.py", f"{HELIX}/start_helix.sh", f"{m.BUILD_ORDERS}/BUILD_PROTOCOL.md",
                   m.RUNNER, f"{m.BUILD_ORDERS}/builder_run.sh")
    m._P7_DIRS = tuple(f"{m.BUILD_ORDERS}/{d}" for d in ("runs", "quarantine", ".git", "reports"))
    m.CATCHES = None                                      # set per process by own_catches()
    return m


def setup(old_path, new_path):
    """Load both hooks into this process, check the re-pointed constants, give them own catches."""
    global old, new
    old, new = load("old", old_path), load("new", new_path)
    ref = importlib.util.module_from_spec(importlib.util.spec_from_file_location("ref", f"{HELIX}/factgate_hook.py"))
    importlib.util.spec_from_file_location("ref", f"{HELIX}/factgate_hook.py").loader.exec_module(ref)
    for m in (old, new):   # the re-pointed constants must equal what the live file computes for itself
        assert (m.BUILD_ORDERS, m._P7_FILES, m._P7_DIRS, m.RUNNER, m.FACTS) == \
               (ref.BUILD_ORDERS, ref._P7_FILES, ref._P7_DIRS, ref.RUNNER, ref.FACTS), m.__name__
    own_catches()


def own_catches():
    """This process's own catches files — one per hook, path includes the pid."""
    d = tempfile.mkdtemp(prefix=f"diff_probe_{os.getpid()}_")
    for m in (old, new):
        m.CATCHES = os.path.join(d, f"{m.__name__}_{os.getpid()}.jsonl")


def dec(m, cmd, cwd):
    open(m.CATCHES, "w").close()
    out = asyncio.run(m.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}, "t", None))
    if not (out or {}).get("hookSpecificOutput", {}).get("permissionDecision"): return "allow"
    lines = open(m.CATCHES).read().splitlines()
    return "deny/" + (json.loads(lines[0])["where"].split()[0] if len(lines) == 1 else "?")


def judge(cmd):
    res = []
    for cwd in (HELIX, f"{HELIX}/workspace"):
        a, b = dec(old, cmd, cwd), dec(new, cmd, cwd)
        if a != b: res.append((a, b, cwd.replace(HELIX, "~/helix"), cmd))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True); ap.add_argument("--new", required=True)
    ap.add_argument("--out", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--slice"); g.add_argument("--merge", type=int)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    allcmds = json.load(open(CORPUS))
    if a.slice:
        si, sn = map(int, a.slice.split("/"))
        mine = allcmds[si::sn]
        setup(a.old, a.new)                               # fail here, not in a worker
        flips = []
        with mp.Pool(max(1, os.cpu_count() - 4), initializer=setup, initargs=(a.old, a.new)) as pool:
            for r in pool.imap_unordered(judge, sorted(mine, key=len), chunksize=8):
                flips += r
        flips.sort()
        json.dump({"slice": si, "of": sn, "total": len(allcmds), "old": a.old, "new": a.new,
                   "judged": mine, "flips": flips},
                  open(os.path.join(a.out, f"diff_probe_flips_slice{si}of{sn}.json"), "w"), indent=1)
        print(f"slice {si}/{sn}: judged {len(mine)} of {len(allcmds)} commands, {len(flips)} flips (cmd x cwd)")
        return
    sn = a.merge; flips, seen = [], []
    for si in range(sn):
        d = json.load(open(os.path.join(a.out, f"diff_probe_flips_slice{si}of{sn}.json")))
        assert d["total"] == len(allcmds), (si, d["total"], len(allcmds))
        assert (d["old"], d["new"]) == (a.old, a.new), (si, d["old"], d["new"])
        print(f"slice {si}/{sn}: {len(d['judged'])} commands, {len(d['flips'])} flips")
        seen += d["judged"]; flips += [tuple(f) for f in d["flips"]]
    print(f"coverage: {len(seen)} judged, {len(set(seen))} distinct, {len(allcmds)} in corpus, "
          f"exactly once: {sorted(seen) == allcmds}")
    assert sorted(seen) == allcmds
    flips.sort()
    json.dump(flips, open(os.path.join(a.out, "diff_probe_flips.json"), "w"), indent=1)
    print(f"{len(allcmds)} distinct Bash commands from Odin's transcripts, each judged at 2 cwds")
    print(f"old={a.old}\nnew={a.new}")
    print(f"deny/? labels: {sum(x == 'deny/?' for f in flips for x in f[:2])}")
    d2a = [f for f in flips if f[0].startswith("deny") and f[1] == "allow"]
    a2d = [f for f in flips if f[0] == "allow" and f[1].startswith("deny")]
    other = [f for f in flips if f not in d2a and f not in a2d]
    for name, v in (("deny->allow", d2a), ("allow->deny", a2d), ("deny->deny, rule changed", other)):
        print(f"== {name}: {len(v)} (cmd x cwd)")
        for x, y, cwd, c in v: print(f"  {x}->{y} [{cwd}] {c[:160]!r}")


if __name__ == "__main__":
    main()
