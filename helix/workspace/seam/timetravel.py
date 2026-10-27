#!/usr/bin/env python3
"""Does claim decay exist as a measurable object? Time-travel an ANCHOR audit through a repo.

For every commit: unpack that tree into a temp dir, run the audit with the resolver pointed ONLY
at that tree, and record the verdict of every claim. A claim that was anchored at commit i and
broken at commit j>i is a DECAY EVENT — evidence that the record and the world drifted apart
while nobody was looking. No decay events, no framework: that is the null this has to be able to
return."""
import json, pathlib, shutil, subprocess, sys, tempfile, collections

sys.path.insert(0, str(pathlib.Path.home() / "helix" / "workspace" / "anchor"))
import core, anchor as A


def commits(repo):
    out = subprocess.run(["git", "-C", repo, "log", "--reverse", "--format=%H %ct"],
                         capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
    return [(l.split()[0], int(l.split()[1])) for l in out]


def audit_at(repo, sha):
    d = pathlib.Path(tempfile.mkdtemp(prefix="tt_"))
    try:
        tar = subprocess.run(["git", "-C", repo, "archive", sha], capture_output=True, timeout=120).stdout
        subprocess.run(["tar", "-x", "-C", str(d)], input=tar, timeout=120)
        core.RULES = core.load_rules()
        files = core.walk(d)
        claims = core.dedupe(sum([core.extract(p, rel) for p, rel in files], []))
        res = core.Resolver([d])                      # the world AS IT WAS, nothing else
        core.check(claims, d, res, core.RULES, "", net=False)
        keep = {}
        for c in claims:
            if c.kind in ("path", "cite"):            # the two kinds a tree can answer
                keep[(c.file, c.text[:120], c.token)] = c.verdict
        return keep, len(files)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(repo, label):
    cs = commits(repo)
    print(f"{label}: {len(cs)} commits")
    hist, sizes = [], []
    for i, (sha, ts) in enumerate(cs):
        k, n = audit_at(repo, sha)
        hist.append((sha, ts, k))
        sizes.append(n)
        ok = sum(1 for v in k.values() if v == "ok")
        br = sum(1 for v in k.values() if v == "broken")
        print(f"  {i:3d} {sha[:8]} {n:4d} docs  {len(k):5d} claims  ok={ok:5d} broken={br:4d}")
    # decay events: anchored once, broken later
    first_ok, decayed, healed = {}, [], []
    for sha, ts, k in hist:
        for key, v in k.items():
            if v == "ok" and key not in first_ok:
                first_ok[key] = (sha, ts)
            elif v == "broken" and key in first_ok:
                decayed.append(dict(key=list(key), ok_at=first_ok[key][0][:8],
                                    ok_ts=first_ok[key][1], broke_at=sha[:8], broke_ts=ts))
                del first_ok[key]
    out = dict(repo=repo, commits=len(cs), decay_events=decayed,
               still_ok=len(first_ok), n_decayed=len(decayed))
    print(f"\n  claims ever anchored : {len(first_ok) + len(decayed)}")
    print(f"  DECAY EVENTS         : {len(decayed)}")
    for d in decayed[:12]:
        span = (d["broke_ts"] - d["ok_ts"]) / 86400
        print(f"    {d['key'][2][:48]:50s} ok@{d['ok_at']} → broke@{d['broke_at']}  after {span:.1f} d")
    pathlib.Path(f"{label}_decay.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "repo")
