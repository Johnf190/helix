#!/usr/bin/env python3
"""cycle.py — the cycle-time ledger (order 27, item 16): what a window of time cost.

    cycle.py window <start> <end> [--json] [--dir D]
        wallclock, GPU-minutes by process, Helix turns + tokens + cost between two times
    cycle.py runstats <stream.jsonl> [--started T --ended T]
        one build's cost, turns and tokens, from the builder's own stream-json log
    cycle.py k --scope S --until <ts|now> [--json] [--dir D]
        K = attempts logged for a scope before a time (order 28, item 24), counted from
        ATTEMPTS.jsonl, with the GPU ledger as the witness for attempts never logged

The ledgers it reads are written by processes OUTSIDE Odin's shell, which cannot write them
(workspace/cycle is inside app.py's WALL_PATHS):
    GPU.jsonl    one line per GPU process per minute, by gpu_sample.py under a systemd user timer
    TURNS.jsonl  one line per Helix turn, by app.py when a ResultMessage arrives
    ATTEMPTS.jsonl  one line per attempt, by app.py's POST /attempt, BEFORE its result exists

An UPPER BOUND, labelled as such: a window counts every GPU sample and every turn in that
period, not only one experiment's. Attribution per experiment needs item 24's attempt log.
A missing ledger is reported as missing, never as zero.

Times: written as UTC with a Z. Read: Z or an offset as written; a naive time is local time.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GPU = "GPU.jsonl"
TURNS = "TURNS.jsonl"
ATTEMPTS = "ATTEMPTS.jsonl"
GENESIS = "genesis"
TOKEN_KEYS = (("input", "input_tokens"), ("output", "output_tokens"),
              ("cache_read", "cache_read_input_tokens"),
              ("cache_write", "cache_creation_input_tokens"))
# TURNS.jsonl carries app.py's _usage_payload names
TURN_TOKEN_KEYS = (("input", "input_tokens"), ("output", "output_tokens"),
                   ("cache_read", "cache_read"), ("cache_write", "cache_write"))


def cycle_dir():
    return Path(os.environ.get("HELIX_CYCLE_DIR") or HERE)


# ------------------------------------------------------------------ time

def parse_time(text):
    """ISO time -> aware datetime. `Z` / an offset as written; a naive time is local."""
    d = datetime.datetime.fromisoformat(str(text))
    return d.astimezone() if d.tzinfo is None else d


def epoch(text):
    return parse_time(text).timestamp()


def utc_z(ts=None):
    ts = datetime.datetime.now().timestamp() if ts is None else ts
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_dur(seconds):
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ------------------------------------------------------------------ reading

def read_jsonl(path):
    """[records] with a parseable ts, in file order; None when the ledger does not exist."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    out = []
    for raw in text.splitlines():
        try:
            rec = json.loads(raw)
            rec["_t"] = epoch(rec["ts"])
        except Exception:
            continue
        out.append(rec)
    return out


def run_stats(stream):
    """The last {"type":"result"} line of a builder's stream-json log -> cost, turns, tokens.
    A field the line does not carry is absent, never a fake zero. No result line -> {}."""
    last = None
    try:
        with open(stream, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if '"result"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "result":
                    last = rec
    except OSError:
        return {}
    if last is None:
        return {}
    out = {}
    if isinstance(last.get("total_cost_usd"), (int, float)):
        out["cost_usd"] = last["total_cost_usd"]
    if isinstance(last.get("num_turns"), int):
        out["turns"] = last["num_turns"]
    usage = last.get("usage") if isinstance(last.get("usage"), dict) else {}
    tokens = {k: usage[src] for k, src in TOKEN_KEYS if isinstance(usage.get(src), int)}
    if tokens:
        out["tokens"] = tokens
    return out


# ------------------------------------------------------------------ the window

def window(start, end, directory=None):
    d = Path(directory) if directory else cycle_dir()
    t0, t1 = epoch(start), epoch(end)
    out = {"start": utc_z(t0), "end": utc_z(t1), "wall_s": int(round(t1 - t0))}

    gpu = read_jsonl(d / GPU)
    if gpu is None:
        out["gpu"] = {"ledger": False}
    else:
        procs, errors = {}, 0
        for r in gpu:
            if not (t0 <= r["_t"] <= t1):
                continue
            if "error" in r:
                errors += 1
            elif "pid" in r:
                key = (r["pid"], r.get("cmd") or "")
                procs[key] = procs.get(key, 0) + 1
        by = sorted(({"pid": p, "cmd": c, "min": n} for (p, c), n in procs.items()),
                    key=lambda x: (-x["min"], x["pid"]))
        first = gpu[0]["ts"] if gpu else None
        out["gpu"] = {"ledger": True, "min": sum(x["min"] for x in by), "by_process": by,
                      "errors": errors, "sampled_from": first,
                      "partial": not gpu or gpu[0]["_t"] > t0}

    turns = read_jsonl(d / TURNS)
    if turns is None:
        out["turns"] = {"ledger": False}
    else:
        inside = [r for r in turns if t0 <= r["_t"] <= t1]
        t = {"ledger": True, "n": len(inside), "logged_from": turns[0]["ts"] if turns else None,
             "partial": not turns or turns[0]["_t"] > t0}
        costs = [r["cost_usd"] for r in inside if isinstance(r.get("cost_usd"), (int, float))]
        if costs:
            t["cost_usd"] = round(sum(costs), 6)
        tokens = {}
        for k, src in TURN_TOKEN_KEYS:
            vals = [r[src] for r in inside if isinstance(r.get(src), int)]
            if vals:
                tokens[k] = sum(vals)
        t["tokens"] = tokens
        out["turns"] = t
    return out


def line(label, w):
    """`seal→score 7m38s · GPU 0 min · 14 turns · $3.10 (token spend in window)`"""
    parts = [f"{label} {fmt_dur(w['wall_s'])}"]
    g = w["gpu"]
    if not g.get("ledger"):
        parts.append("GPU not sampled")
    else:
        parts.append(f"GPU {g['min']} min" + (f" (sampled from {g['sampled_from']})"
                                             if g["partial"] and g["sampled_from"] else
                                             " (no samples yet)" if g["partial"] else ""))
    t = w["turns"]
    if not t.get("ledger"):
        parts.append("turns not logged")
    else:
        parts.append(f"{t['n']} turns" + (f" (turns logged from {t['logged_from']})"
                                          if t["partial"] and t["logged_from"] else
                                          " (none logged yet)" if t["partial"] else ""))
        if "cost_usd" in t:
            parts.append(f"${t['cost_usd']:.2f} (token spend in window)")
    return " · ".join(parts)


# ------------------------------------------------------------------ order 28 (item 24): K

def line_hash(raw):
    """What the next ATTEMPTS line's `prev` holds: sha256 of this line's text, newline excluded.
    app.py's POST /attempt computes it the same way."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_attempts(path):
    """(records, None) when every line chains to the one before it, (None, seq) at the first
    line that does not: wrong `seq`, wrong `prev`, or not a JSON object with a readable ts.
    No ledger -> ([], None): nothing has been logged."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], None
    out, prev = [], GENESIS
    for seq, raw in enumerate(text.splitlines(), 1):
        try:
            rec = json.loads(raw)
            ok = (isinstance(rec, dict) and rec.get("seq") == seq and rec.get("prev") == prev
                  and isinstance(rec.get("n"), int) and isinstance(rec.get("scope"), str))
            rec["_t"] = epoch(rec["ts"])
        except Exception:
            ok = False
        if not ok:
            return None, seq
        out.append(rec)
        prev = line_hash(raw)
    return out, None


def _under(path, roots):
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    path = os.path.normpath(path)
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _env_seq(pid):
    """HELIX_ATTEMPT_SEQ of a process still running, or None. Read now, so only a job that is
    still alive when K is counted can be tied this way."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    for kv in env:
        if kv.startswith(b"HELIX_ATTEMPT_SEQ="):
            try:
                return int(kv.split(b"=", 1)[1])
            except ValueError:
                return None
    return None


def witness(attempts, mine, t1, gpu):
    """GPU jobs that ran in this scope's directories inside [first attempt, until) and that no
    attempt line accounts for: not by pid (logged at or before the job was first seen), not by
    HELIX_ATTEMPT_SEQ (a child of a logged job). [{pid, first_seen, cmd, cwd}], one per pid."""
    t0 = min(r["_t"] for r in mine)
    roots = {os.path.normpath(r["cwd"]) for r in mine
             if isinstance(r.get("cwd"), str) and r["cwd"].startswith("/")}
    seqs = {r["seq"] for r in attempts}
    logged_at = {}
    for r in attempts:
        if isinstance(r.get("pid"), int):
            logged_at[r["pid"]] = min(logged_at.get(r["pid"], r["_t"]), r["_t"])
    jobs = {}
    for s in gpu:
        if "pid" not in s or not (t0 <= s["_t"] < t1):
            continue
        cmd = s.get("cmd") or ""
        if not (_under(s.get("cwd"), roots) or any(_under(tok, roots) for tok in cmd.split())):
            continue
        pid = s["pid"]
        if pid in jobs or (pid in logged_at and logged_at[pid] <= s["_t"]):
            continue
        if s.get("attempt_seq") in seqs or _env_seq(pid) in seqs:
            continue
        jobs[pid] = {"pid": pid, "first_seen": s["ts"], "cmd": cmd, "cwd": s.get("cwd")}
    return sorted(jobs.values(), key=lambda j: (j["first_seen"], j["pid"]))


def count_k(scope, until, directory=None):
    """K for `scope` before `until`: the sum of `n` over its ATTEMPTS lines with ts < until.
    Never a number from a broken chain. The witness can only push K up: it lists GPU jobs the
    log does not account for; it never lowers K."""
    d = Path(directory) if directory else cycle_dir()
    t1 = epoch(until)
    out = {"scope": scope, "until": utc_z(t1)}
    attempts, broken = read_attempts(d / ATTEMPTS)
    if attempts is None:
        out.update(k=None, since=None, chain="broken", broken_at=broken, witness="unverifiable",
                   unlogged=0, unlogged_jobs=[],
                   line=f"K unverifiable (chain broken at seq {broken})")
        return out
    mine = [r for r in attempts if r["scope"] == scope and r["_t"] < t1]
    out.update(chain="ok", attempts=len(mine), k=sum(r["n"] for r in mine))
    if not mine:
        out.update(since=None, witness="none", unlogged=0, unlogged_jobs=[],
                   line="K=0 · no attempts logged")
        return out
    first = min(r["_t"] for r in mine)
    out["since"] = utc_z(first)
    head = (f"K={out['k']} (since "
            f"{datetime.datetime.fromtimestamp(first, datetime.timezone.utc):%Y-%m-%dT%H:%MZ})")
    gpu = read_jsonl(d / GPU)
    if gpu is None:
        out.update(witness="none", unlogged=0, unlogged_jobs=[],
                   line=head + " · K not witnessed (GPU not sampled)")
        return out
    jobs = witness(attempts, mine, t1, gpu)
    out.update(witness="disputed" if jobs else "ok", unlogged=len(jobs), unlogged_jobs=jobs,
               line=head + (f" · K disputed: +{len(jobs)} unlogged GPU job(s)" if jobs
                            else " · K witnessed (GPU)"))
    return out


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="cycle.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("window", help="what a window of time cost (an upper bound)")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", help="ledger folder (default: HELIX_CYCLE_DIR or this folder)")
    p.add_argument("--label", default="window")
    p = sub.add_parser("runstats", help="one build's cost/turns/tokens from its stream log")
    p.add_argument("stream")
    p.add_argument("--started")
    p.add_argument("--ended")
    p = sub.add_parser("k", help="K: attempts logged for a scope before a time, witnessed")
    p.add_argument("--scope", required=True)
    p.add_argument("--until", required=True, help="ISO time, or `now`")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", help="ledger folder (default: HELIX_CYCLE_DIR or this folder)")
    a = ap.parse_args(argv)

    if a.verb == "k":
        until = utc_z() if a.until == "now" else a.until
        try:
            epoch(until)
        except ValueError as e:
            sys.stderr.write(f"not an ISO time: {e}\n")
            return 2
        r = count_k(a.scope, until, a.dir)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            print(r["line"])
            for j in r["unlogged_jobs"]:
                print(f"  unlogged GPU job: pid {j['pid']}  first seen {j['first_seen']}  "
                      f"cwd {j['cwd']}  {j['cmd'][:120]}")
        return 4 if r["chain"] == "broken" else 0

    if a.verb == "runstats":
        out = {}
        if a.started and a.ended:
            out["wall_s"] = int(round(epoch(a.ended) - epoch(a.started)))
        out.update(run_stats(a.stream))
        print(json.dumps(out))
        return 0

    try:
        if epoch(a.end) < epoch(a.start):
            sys.stderr.write(f"end {a.end} is before start {a.start}\n")
            return 2
    except ValueError as e:
        sys.stderr.write(f"not an ISO time: {e}\n")
        return 2
    w = window(a.start, a.end, a.dir)
    w["line"] = line(a.label, w)
    if a.json:
        print(json.dumps(w, ensure_ascii=False))
        return 0
    print(w["line"])
    print(f"  window {w['start']} → {w['end']} (an upper bound: everything in the window, "
          f"not only one experiment's)")
    for p in w["gpu"].get("by_process", []):
        print(f"  GPU {p['min']:>4} min  pid {p['pid']}  {p['cmd'][:120]}")
    if w["gpu"].get("errors"):
        print(f"  GPU sampler errors in window: {w['gpu']['errors']}")
    tok = w["turns"].get("tokens")
    if tok:
        print("  tokens  " + "  ".join(f"{k} {v}" for k, v in tok.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
