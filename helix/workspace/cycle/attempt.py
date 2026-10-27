#!/usr/bin/env python3
"""attempt.py — log an attempt BEFORE its result exists (order 28, item 24).

    attempt.py run  --scope S [--entry ID] [--kind run] [--n N] -- <command…>
        POST /attempt first; only with a seq back, exec the command in this same pid with
        HELIX_ATTEMPT_SEQ=<seq> in its environment. Helix down or refusing -> exit 5, and the
        command is NOT run.
    attempt.py log  --scope S --kind run|seed|variant|prompt [--entry ID] [--n N] --cmd "…" [--cwd D]
        an attempt that is not a shell command: a prompt variant, a hand run
    attempt.py list [--scope S]
        read ATTEMPTS.jsonl (reading is allowed through the wall) and check its chain

Only Helix's own process writes ATTEMPTS.jsonl; this client can only ask it to, over loopback.
`regate.py score` prints K = these attempts, counted (cycle.py k), next to the score.

Exit codes: 0 ok (run: the command's own code) · 2 usage · 4 chain broken (list) ·
5 no attempt line, nothing run · 127 logged, but the command could not be started
"""
import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycle  # noqa: E402

KINDS = ("run", "seed", "variant", "prompt")
CMD_MAX = 300
EXIT_NO_SEQ = 5


def default_url():
    return os.environ.get("HELIX_URL") or \
        f"http://127.0.0.1:{os.environ.get('HELIX_PORT', '7777')}"


def post(url, body):
    """{"seq", "ts"} from Helix, or raise with the reason. No proxy: loopback only."""
    req = urllib.request.Request(url.rstrip("/") + "/attempt", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Helix said {e.code}: "
                           f"{e.read().decode('utf-8', 'replace').strip()[:300]}")
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Helix not reachable at {url}: {e}")
    if not isinstance(out, dict) or not isinstance(out.get("seq"), int):
        raise RuntimeError(f"Helix answered without a seq: {str(out)[:200]}")
    return out


def body_of(a, cmd, cwd, pid=None):
    body = {"scope": a.scope, "kind": a.kind, "n": a.n, "cmd": cmd[:CMD_MAX], "cwd": cwd}
    if a.entry:
        body["entry"] = a.entry
    if pid is not None:
        body["pid"] = pid
    return body


def verb_run(a):
    argv = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not argv:
        sys.stderr.write("attempt.py run: no command after --\n")
        return 2
    try:
        got = post(a.url, body_of(a, shlex.join(argv), os.getcwd(), pid=os.getpid()))
    except RuntimeError as e:
        sys.stderr.write(f"attempt.py: NO ATTEMPT LINE, command not run: {e}\n")
        return EXIT_NO_SEQ
    sys.stderr.write(f"attempt.py: seq {got['seq']} logged {got.get('ts')} · {a.scope}\n")
    sys.stderr.flush()
    os.environ["HELIX_ATTEMPT_SEQ"] = str(got["seq"])
    try:
        os.execvp(argv[0], argv)              # same pid: the logged pid is the job's pid
    except OSError as e:
        sys.stderr.write(f"attempt.py: seq {got['seq']} is logged, but {argv[0]!r} could not "
                         f"start: {e}\n")
        return 127


def verb_log(a):
    try:
        got = post(a.url, body_of(a, a.cmd, os.path.abspath(a.cwd or os.getcwd())))
    except RuntimeError as e:
        sys.stderr.write(f"attempt.py: NO ATTEMPT LINE: {e}\n")
        return EXIT_NO_SEQ
    print(f"seq {got['seq']} logged {got.get('ts')} · {a.scope} · {a.kind} × {a.n}")
    return 0


def verb_list(a):
    path = cycle.cycle_dir() / cycle.ATTEMPTS
    recs, broken = cycle.read_attempts(path)
    if recs is None:
        # show what is there, raw, up to the break: nothing past it can be trusted
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in raw[:broken - 1]:
            print("  " + line[:200])
        print(f"chain BROKEN at seq {broken}: {path} has been edited or truncated; "
              f"no K can be counted from it")
        return 4
    shown = [r for r in recs if a.scope is None or r["scope"] == a.scope]
    for r in shown:
        print(f"seq {r['seq']:<5} {r['ts']}  {r['scope']:<24} {r.get('kind', '?'):<7} "
              f"n={r['n']:<3} pid={r.get('pid', '-')!s:<8} {r.get('cwd', '')}  "
              f"{str(r.get('cmd', ''))[:100]}")
    print(f"chain OK ({len(recs)} line(s)) · {len(shown)} shown · {path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="attempt.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    for name in ("run", "log"):
        p = sub.add_parser(name)
        p.add_argument("--scope", required=True)
        p.add_argument("--entry")
        p.add_argument("--n", type=int, default=1)
        p.add_argument("--url", default=default_url())
        if name == "run":
            p.add_argument("--kind", choices=KINDS, default="run")
            p.add_argument("command", nargs=argparse.REMAINDER)
        else:
            p.add_argument("--kind", choices=KINDS, required=True)
            p.add_argument("--cmd", required=True)
            p.add_argument("--cwd")
    p = sub.add_parser("list")
    p.add_argument("--scope")
    a = ap.parse_args(argv)
    return {"run": verb_run, "log": verb_log, "list": verb_list}[a.verb](a)


if __name__ == "__main__":
    sys.exit(main())
