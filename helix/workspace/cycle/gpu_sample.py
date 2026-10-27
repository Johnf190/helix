#!/usr/bin/env python3
"""gpu_sample.py — ONE sample of what is on the GPU, appended to GPU.jsonl (order 27, item 16).

Run every 60 s by the systemd user timer helix-gpu-sampler.timer, which is not Odin's shell:
the ledger is the independent witness item 24 counts GPU work against.

    {"ts": "…Z", "pid": 1234, "cmd": "python3 train.py …", "cwd": "/home/…", "mem_mib": 812}
        one line per GPU compute process (cmd = /proc cmdline, truncated to 300 chars)
    {"ts": "…Z", "idle": true}
        no GPU process; written at most every 10 minutes, so the sampler is seen alive
    {"ts": "…Z", "error": "…"}
        nvidia-smi missing or failing: one line per failed sample

One sample is about one minute of GPU time. A job shorter than a minute can be missed.
Every write is a single O_APPEND write of whole lines.

    gpu_sample.py [--ledger PATH] [--smi PATH]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "GPU.jsonl"
SMI = "nvidia-smi"
QUERY = ["--query-compute-apps=pid,used_memory", "--format=csv,noheader"]
IDLE_EVERY_S = 600
CMD_MAX = 300


def utc_z(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _proc(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmd = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:CMD_MAX]
    except OSError:
        cmd = None
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    return cmd, cwd


def _last_line(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
        return json.loads(tail[-1]) if tail else None
    except (OSError, ValueError, IndexError):
        return None


def _epoch_utc(text):
    import calendar
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def _append(path, recs):
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def sample(ledger=LEDGER, smi=SMI, now=None):
    now = time.time() if now is None else now
    ts = utc_z(now)
    try:
        r = subprocess.run([smi, *QUERY], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except Exception as e:
        _append(ledger, [{"ts": ts, "error": f"{type(e).__name__}: {e}"[:300]}])
        return 1
    if r.returncode != 0:
        why = (r.stderr.strip() or r.stdout.strip() or "no output").splitlines()[-1]
        _append(ledger, [{"ts": ts, "error": f"nvidia-smi exit {r.returncode}: {why}"[:300]}])
        return 1
    recs, bad = [], []
    for raw in r.stdout.splitlines():
        if not raw.strip():
            continue
        try:
            pid_s, mem_s = [x.strip() for x in raw.split(",", 1)]
            pid = int(pid_s)
            mem = int(mem_s.split()[0])
        except ValueError:
            bad.append(raw.strip())
            continue
        cmd, cwd = _proc(pid)
        recs.append({"ts": ts, "pid": pid, "cmd": cmd, "cwd": cwd, "mem_mib": mem})
    if bad and not recs:
        _append(ledger, [{"ts": ts, "error": f"unparsed nvidia-smi output: {bad[0]}"[:300]}])
        return 1
    if recs:
        _append(ledger, recs)
        return 0
    last = _last_line(ledger)
    if last and last.get("idle") and "ts" in last:
        try:
            if now - _epoch_utc(last["ts"]) < IDLE_EVERY_S:
                return 0
        except ValueError:
            pass
    _append(ledger, [{"ts": ts, "idle": True}])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpu_sample.py", description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--smi", default=SMI)
    a = ap.parse_args(argv)
    try:
        return sample(a.ledger, a.smi)
    except OSError as e:                  # the ledger itself cannot be written: say so, once
        sys.stderr.write(f"gpu_sample: cannot write {a.ledger}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
