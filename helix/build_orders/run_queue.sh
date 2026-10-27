#!/usr/bin/env bash
# run_queue.sh — run several work orders back to back, each through the unchanged run_order.sh,
# and stop at the first problem (order 24).
#
# the user types `/build 25-30` or `/build 25,26,27b` in Helix's chat; app.py calls
#   run_queue.sh --check NN...     the up-front refusals only; prints every reason
#   run_queue.sh --start NN...     the refusals, then QUEUE.json, then one `systemd-run` of
#   run_queue.sh --run <queue-id>  the loop itself, in its own unit so it outlives a restart
#
# The queue never restarts Helix and never kills a builder. Each order is its own fresh builder,
# launched by run_order.sh with all five of its refusals; the builder never sees the queue.
# Between orders a MECHANICAL gate decides: report's last line, py_compile app.py, the hook
# suite, the builder's exit and its report check (order 26) in RUNS.jsonl. Anything else halts
# the queue. Odin reviews afterwards.
#
# State: runs/QUEUE.json is the only queue state (runs/.queue.lock only serialises writers).
# Every event is one line in runs/RUNS.jsonl. One inbox note on finish or halt.
#
# Test-only overrides (test_build_queue.py): QUEUE_FAKE_INLINE, QUEUE_FAKE_SYSTEMCTL,
# QUEUE_FAKE_CLOCK, QUEUE_FAKE_NIGHTLY_RE, QUEUE_FAKE_MAX_SECONDS, QUEUE_POLL_SECONDS,
# and run_order.sh's own BUILD_FAKE_HHMM.
#
# The logic is Python, but it lives in this file on purpose: P6 refuses Odin's shell running
# `run_queue.sh`, so there is no separate .py that could be run round it.
#
# Order 64: everything below runs inside main(), and the last line is `main "$@"; exit $?`, so bash
# parses the whole file before any of it runs: an order that rewrites this file mid-run cannot
# break the run. Change it only by writing a new file beside it and mv-ing it over this one.
main() {
exec python3 - "$0" "$@" <<'PY'
import datetime, fcntl, glob, json, os, re, subprocess, sys, time

SELF = os.path.realpath(sys.argv[1])
ARGS = sys.argv[2:]
ORDERS = os.path.dirname(SELF)
BRAIN = os.path.dirname(ORDERS)
RUNS = os.path.join(ORDERS, "runs")
QUEUE = os.path.join(RUNS, "QUEUE.json")
QLOCK = os.path.join(RUNS, ".queue.lock")
RUNS_LOG = os.path.join(RUNS, "RUNS.jsonl")
RUNNER = os.path.join(ORDERS, "run_order.sh")

MAX_ORDERS = 6
MAX_SECONDS = int(os.environ.get("QUEUE_FAKE_MAX_SECONDS") or 10 * 3600)
UNIT_MAX_SECONDS = 10 * 3600 + 2 * 3600        # hard backstop; the last builder may start at 9h59
POLL = float(os.environ.get("QUEUE_POLL_SECONDS") or 30)
SYSTEMCTL = os.environ.get("QUEUE_FAKE_SYSTEMCTL") or "systemctl"
NIGHTLY_RE = os.environ.get("QUEUE_FAKE_NIGHTLY_RE") or r"[n]ightly[.]py|[r]etry_optc[.]py"
GOOD_LAST_LINES = ("READY FOR /restart", "DONE")
ID_RE = re.compile(r"[0-9]+[a-z]?")
LIVE = ("starting", "running", "waiting")


def now():
    """UTC with a Z (order 27). A QUEUE.json from before carries naive local times;
    fromisoformat() reads both, and a naive one's .timestamp() is local time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ state
def _read():
    try:
        with open(QUEUE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def update(**kv):
    """Read-modify-write QUEUE.json under the lock, so the user's /build stop is never lost."""
    os.makedirs(RUNS, exist_ok=True)
    with open(QLOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        q = _read() or {}
        q.update(kv)
        tmp = QUEUE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(q, fh, indent=1)
        os.replace(tmp, QUEUE)
        return q


def event(qid, name, **kv):
    os.makedirs(RUNS, exist_ok=True)
    with open(RUNS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"queue": qid, "event": name, "at": now(), **kv}) + "\n")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def queue_active():
    q = _read()
    if not q or q.get("status") not in LIVE:
        return None
    if q.get("pid"):
        return q if pid_alive(q["pid"]) else None
    try:            # "starting": --start wrote it, the unit has not taken over yet
        age = time.time() - datetime.datetime.fromisoformat(q["started"]).timestamp()
    except Exception:
        return None
    return q if age < 120 else None


# ------------------------------------------------------------------ the world outside
def hhmm():
    f = os.environ.get("QUEUE_FAKE_CLOCK")
    if f:
        return int(open(f).read().strip())
    return int(os.environ.get("BUILD_FAKE_HHMM") or time.strftime("%H%M"))


def in_window(t):
    return 330 <= t <= 530                     # the same window run_order.sh refuses


def nightly_running():
    return subprocess.run(["pgrep", "-af", NIGHTLY_RE], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def builders_active():
    """Live builder units, plus run_order.sh's launch lock if its pid is alive."""
    out = []
    try:
        r = subprocess.run([SYSTEMCTL, "--user", "list-units", "--state=active", "--no-legend",
                            "--plain", "helix-builder-*"], capture_output=True, text=True,
                           timeout=30)
        out += [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
    except Exception as e:
        out.append(f"(could not ask systemctl: {type(e).__name__})")
    try:
        pid = open(os.path.join(RUNS, ".lock")).read().strip()
        if pid and pid_alive(pid):
            out.append(f"run_order.sh pid {pid}")
    except FileNotFoundError:
        pass
    return out


def unit_alive(unit):
    r = subprocess.run([SYSTEMCTL, "--user", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating", "reloading", "deactivating")


def runs_record(unit):
    """The builder's own RUNS line (written by builder_run.sh), not one of ours."""
    try:
        lines = open(RUNS_LOG, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return None
    for l in reversed(lines):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("unit") == unit and "queue" not in r:
            return r
    return None


def report_path(nn):
    return os.path.join(ORDERS, "reports", f"ORDER_{nn}_report.md")


def last_line(path):
    try:
        lines = [l.strip() for l in open(path, encoding="utf-8", errors="replace")]
    except FileNotFoundError:
        return None
    lines = [l for l in lines if l]
    return lines[-1] if lines else ""


# ------------------------------------------------------------------ up front
def reasons(orders):
    why = []
    if not orders:
        why.append("no orders given")
    if len(orders) > MAX_ORDERS:
        why.append(f"{len(orders)} orders — a queue takes at most {MAX_ORDERS}")
    seen = set()
    for nn in orders:
        if not ID_RE.fullmatch(nn):
            why.append(f"order id must be digits (optionally one letter), got '{nn}'")
            continue
        if nn in seen:
            why.append(f"order {nn} is listed twice")
        seen.add(nn)
        files = sorted(glob.glob(os.path.join(glob.escape(ORDERS), f"ORDER_{nn}_*.md")))
        if not files:
            why.append(f"no ORDER_{nn}_*.md in build_orders")
        elif len(files) > 1:
            why.append(f"{len(files)} files match ORDER_{nn}_*.md — "
                       + " ".join(os.path.basename(f) for f in files))
        if os.path.exists(report_path(nn)):
            why.append(f"reports/ORDER_{nn}_report.md already exists")
    b = builders_active()
    if b:
        why.append("a builder is already active: " + ", ".join(b))
    q = queue_active()
    if q:
        why.append(f"a queue is already active: {q.get('id')} ({q.get('status')})")
    return why


def refuse(why):
    for w in why:
        print(f"refused: {w}")
    sys.exit(1)


def start(orders):
    why = reasons(orders)
    if why:
        refuse(why)
    qid = "q-" + time.strftime("%Y%m%d_%H%M%S")
    trigger = os.environ.get("BUILD_TRIGGER", "cli")
    with open(QLOCK, "a") as lk:               # a fresh QUEUE.json replaces the last one
        fcntl.flock(lk, fcntl.LOCK_EX)
        tmp = QUEUE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"id": qid, "orders": orders, "position": 0, "current": None,
                       "status": "starting", "started": now(), "ended": None, "pid": None,
                       "trigger": trigger, "stop_requested": False, "waiting_for": None,
                       "results": [], "halt_reason": None}, fh, indent=1)
        os.replace(tmp, QUEUE)
    event(qid, "queued", orders=orders, trigger=trigger)
    print(f"started queue {qid}: {' '.join(orders)}", flush=True)
    if os.environ.get("QUEUE_FAKE_INLINE"):
        run(qid)
        return
    log = os.path.join(RUNS, f"{qid}.log")
    r = subprocess.run(
        ["systemd-run", "--user", f"--unit=helix-queue-{qid}", "--collect",
         f"--property=RuntimeMaxSec={UNIT_MAX_SECONDS}", f"--working-directory={BRAIN}",
         "--property=StandardInput=null", f"--property=StandardOutput=append:{log}",
         f"--property=StandardError=append:{log}", "bash", SELF, "--run", qid],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        msg = f"systemd-run failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}"
        update(status="halted", halt_reason=msg, ended=now())
        event(qid, "halt", reason=msg)
        print(f"refused: {msg}")
        sys.exit(1)


# ------------------------------------------------------------------ the loop
def gate(nn, unit, rec):
    """Every failed check, by name. Empty list = continue."""
    failed = []
    line = last_line(report_path(nn))
    if line is None:
        failed.append("report missing")
    elif line not in GOOD_LAST_LINES:
        failed.append(f"report's last line is {line!r}, not 'READY FOR /restart' or 'DONE'")
    r = subprocess.run(["python3", "-m", "py_compile", "app.py"], cwd=BRAIN,
                       capture_output=True, text=True)
    if r.returncode != 0:
        failed.append("py_compile app.py failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    try:
        r = subprocess.run(["python3", "-m", "pytest", "-q", "test_factgate_hook.py"], cwd=BRAIN,
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            tail = (r.stdout.strip().splitlines() or ["?"])[-1]
            failed.append(f"pytest test_factgate_hook.py failed: {tail}")
    except subprocess.TimeoutExpired:
        failed.append("pytest test_factgate_hook.py timed out")
    if rec is None:
        failed.append(f"no RUNS.jsonl line for unit {unit} (builder exit unknown)")
    elif rec.get("exit") != 0:
        failed.append(f"builder exit {rec.get('exit')}")
    # order 26: the next order builds on this one, so its report's claims must have been checked
    # and held. FLAGGED, UNCHECKED, ERROR or no verdict at all (an older builder_run.sh) halts.
    if rec is not None and rec.get("report_check") != "VERIFIED":
        failed.append(f"report check {rec.get('report_check')!r}, not VERIFIED "
                      f"(read reports/ORDER_{nn}_report.check.json)")
    return line, failed


def wait_for_builder(unit):
    while True:
        rec = runs_record(unit)
        if rec:
            return rec
        if not unit_alive(unit):
            time.sleep(min(POLL, 5))            # builder_run.sh writes its line just before exit
            return runs_record(unit)
        time.sleep(POLL)


def note(q, outcome):
    res = q.get("results", [])
    marks = " ".join(f"{r['order']} {'✓' if r['outcome'] == 'ok' else '✗'}" for r in res)
    done = [r["order"] for r in res if r["outcome"] == "ok"]
    want = [r["order"] for r in res if r["outcome"] == "ok" and r.get("last_line") == "READY FOR /restart"]
    if outcome == "done":
        tail = ("all done — ready for /restart" if want else "all done — no order asked for /restart")
    else:
        rest = q["orders"][len(res):]
        tail = f"HALTED: {q.get('halt_reason')}"
        if rest:
            tail += f" — {', '.join(rest)} not started"
        tail += (f". {', '.join(want)} ready for /restart." if want else ". Restart not needed yet.")
    text = (f"queue {q['id']} : {marks} {tail} Odin: review every report into "
            f"reports/QUEUE_{q['id']}_review_odin.md before /restart.").replace("  ", " ")
    try:
        sys.path.insert(0, BRAIN)
        import frontdesk
        frontdesk.add_note(frontdesk.notepad_path(BRAIN), text)
    except Exception as e:
        print(f"run_queue: could not post the note ({type(e).__name__}: {e})", flush=True)


def halt(qid, why):
    q = update(status="halted", halt_reason=why, current=None, waiting_for=None, ended=now())
    event(qid, "halt", reason=why)
    note(q, "halted")
    print(f"queue {qid} halted: {why}", flush=True)


def run(qid):
    q = _read()
    if not q or q.get("id") != qid:
        print(f"refused: runs/QUEUE.json is not queue {qid}")
        sys.exit(2)
    q = update(pid=os.getpid(), status="running")
    event(qid, "start", pid=os.getpid())
    t0 = time.time()
    orders = q["orders"]
    for i, nn in enumerate(orders):
        waited = False
        while True:
            if _read().get("stop_requested"):
                return halt(qid, f"stopped by /build stop before order {nn}")
            if time.time() - t0 >= MAX_SECONDS:
                return halt(qid, f"10-hour cap reached before order {nn}")
            t = hhmm()
            busy = ("the nightly window (03:30-05:30)" if in_window(t)
                    else "a running nightly" if nightly_running() else None)
            if not busy:
                break
            if not waited:
                update(status="waiting", position=i, waiting_for=busy)
                event(qid, "wait", order=nn, why=busy)
                waited = True
            time.sleep(POLL)
        update(status="running", position=i, current=nn, waiting_for=None)
        try:
            r = subprocess.run(["bash", RUNNER, nn], capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL,
                               env={**os.environ, "BUILD_TRIGGER": f"chat:/build queue {qid}"})
            out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            rc = r.returncode
        except Exception as e:
            out, rc = [f"{type(e).__name__}: {e}"], -1
        last = out[-1] if out else f"run_order.sh exited {rc} with no output"
        m = re.search(r"\(unit ([^)\s]+)\)", last)
        if rc != 0 or not m:
            res = _read()["results"] + [{"order": nn, "outcome": "refused", "why": last}]
            update(results=res)
            event(qid, "order_refused", order=nn, why=last)
            return halt(qid, f"run_order.sh refused order {nn}: {last}")
        unit = m.group(1)
        event(qid, "order_start", order=nn, unit=unit)
        rec = wait_for_builder(unit)
        line, failed = gate(nn, unit, rec)
        outcome = "halt" if failed else "ok"
        res = _read()["results"] + [{"order": nn, "unit": unit, "outcome": outcome,
                                     "exit": rec.get("exit") if rec else None,
                                     "last_line": line, "failed": failed}]
        update(results=res, current=None)
        event(qid, "order_end", order=nn, unit=unit, outcome=outcome,
              exit=rec.get("exit") if rec else None, last_line=line, failed=failed)
        if failed:
            return halt(qid, f"order {nn}: " + "; ".join(failed))
    q = update(status="done", current=None, ended=now())
    event(qid, "done")
    note(q, "done")
    print(f"queue {qid} done", flush=True)


# ------------------------------------------------------------------ main
if len(ARGS) >= 1 and ARGS[0] == "--check":
    why = reasons(ARGS[1:])
    if why:
        refuse(why)
    print("ok")
elif len(ARGS) >= 1 and ARGS[0] == "--start":
    start(ARGS[1:])
elif len(ARGS) == 2 and ARGS[0] == "--run":
    run(ARGS[1])
else:
    print("usage: run_queue.sh --check NN... | --start NN... | --run <queue-id>")
    sys.exit(2)
PY
}
main "$@"; exit $?
