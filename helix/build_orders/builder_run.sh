#!/usr/bin/env bash
# builder_run.sh — the unit's actual command. Runs ONE builder, then records what happened.
#
# This exists so `RUNS.jsonl` records the BUILDER's outcome rather than whether `systemd-run`
# managed to start. It also posts the completion note through Helix's existing note path
# (frontdesk.add_note → the pad the front desk reads), so nothing new had to be invented and a
# note survives Helix being down: it waits in notepad.json.
#
# Order 26: before the RUNS line, workspace/reportcheck/reportcheck.py checks the report's CLAIMS
# section; the verdict goes into the line ("report_check") and the note. It never blocks.
#
# Order 27: times are UTC with a Z, and the line carries what the run cost — wall_s, and
# cost_usd / turns / tokens off the builder's own stream log (its final {"type":"result"} line,
# read by workspace/cycle/cycle.py). A field the log does not carry is absent, never zero.
#
# Called only by run_order.sh, which does all the refusing.
#   builder_run.sh <NN> <order_file> <unit> <stream> <report> <protocol_sha> <trigger> <budget>
#
# Order 64: everything below runs inside main(), and the last line is `main "$@"; exit $?`, so bash
# parses the whole file before any of it runs: an order that rewrites this file mid-run cannot
# break the run. Change it only by writing a new file beside it and mv-ing it over this one.
main() {
set -uo pipefail

NN="$1"; ORDER_FILE="$2"; UNIT="$3"; STREAM="$4"; REPORT="$5"
PSHA="$6"; TRIGGER="$7"; BUDGET="$8"

BRAIN="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
RUNS="$BRAIN/build_orders/runs"
PROTOCOL="$BRAIN/build_orders/BUILD_PROTOCOL.md"
CLI="$HOME/.local/bin/claude"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # UTC, as is `ended` (order 27)
RC=99                            # 99 = never got to set it (killed before the CLI returned)
HOOK_SHA0="$(sha256sum "$BRAIN/factgate_hook.py" 2>/dev/null | cut -d' ' -f1)"   # order 63
DONE=0

finish() {
  [ "$DONE" = 1 ] && return
  DONE=1
  python3 - "$RUNS/RUNS.jsonl" "$NN" "$STARTED" "$RC" "$UNIT" "$TRIGGER" "$BUDGET" "$REPORT" "$PSHA" "$BRAIN" "$STREAM" "$HOOK_SHA0" <<'PY'
import datetime, hashlib, json, os, re, signal, subprocess, sys
log, nn, started, rc, unit, trigger, budget, report, psha, brain, stream, hook_sha0 = sys.argv[1:13]
ended = datetime.datetime.now(datetime.timezone.utc)
rec = {"order": nn, "started": started,
       "ended": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
       "exit": int(rc), "unit": unit, "trigger": trigger, "budget_usd": float(budget),
       "report_exists": os.path.exists(report), "protocol_sha256": psha}

# Order 27: time and money per run. Best effort: nothing in here may stop the line being written.
try:
    rec["wall_s"] = int(round(ended.timestamp() - datetime.datetime.fromisoformat(started).timestamp()))
except Exception:
    pass
try:
    sys.path.insert(0, os.path.join(brain, "workspace", "cycle"))
    import cycle
    rec.update(cycle.run_stats(stream))
except Exception as e:
    rec["run_stats"] = f"ERROR: {type(e).__name__}: {e}"[:200]

# Order 26: the report's CLAIMS are checked against witnesses the report did not write (git, the
# disk, a re-run) BEFORE the RUNS line is written, so the line carries the verdict. Flag, never
# block: whatever happens in here, the RUNS line and the note below are still written. systemd
# stops a unit with SIGTERM to the whole cgroup, so a stop during the check becomes an ERROR
# verdict here instead of killing this script before the line exists.
class Stopped(Exception):
    pass

def _stopped(signum, frame):
    raise Stopped(f"signal {signum} during the report check")

signal.signal(signal.SIGTERM, _stopped)
checker = os.path.join(brain, "workspace", "reportcheck", "reportcheck.py")
tail = ""
try:
    # a stopped run has seconds left before SIGKILL; a finished one may re-run its tests
    limit = 60 if int(rc) in (99, 143) else 900
    p = subprocess.run(["python3", checker, "check", report, "--run-record", json.dumps(rec)],
                       capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL,
                       cwd=brain)
    first = (p.stdout.strip().splitlines() or [""])[0]
    verdict = {0: "VERIFIED", 5: "FLAGGED", 6: "UNCHECKED"}.get(p.returncode)
    if verdict == "VERIFIED":
        m = re.search(r"\((\d+) claims", first)
        tail = f" · report check: VERIFIED ({m.group(1) if m else '?'} claims)"
    elif verdict == "FLAGGED":
        tail = f" · ⚠ REPORT FLAGGED: {first.partition('FLAGGED: ')[2][:400]}"
    elif verdict == "UNCHECKED":
        tail = " · report check: UNCHECKED (no CLAIMS section)"
    else:
        why = (p.stderr.strip() or p.stdout.strip() or "no output").splitlines()[-1]
        verdict = f"ERROR: reportcheck exit {p.returncode}: {why}"[:400]
    rec["report_check"] = verdict
except BaseException as e:
    rec["report_check"] = f"ERROR: {type(e).__name__}: {e}"[:400]

# Order 56: the run is name-checked (app.py and its start-up imports, by namecheck.py). Findings
# FLAG the report check, so a /build queue halts as it does on anything but VERIFIED; a missing
# linter is recorded as unavailable and flags nothing. Flag, never block: the line is still written.
namecheck = os.path.join(brain, "build_orders", "tools", "namecheck.py")
try:
    if not os.path.exists(namecheck):
        raise FileNotFoundError(f"no {namecheck}")
    p = subprocess.run(["python3", namecheck], capture_output=True, text=True,
                       timeout=30 if int(rc) in (99, 143) else 60,
                       stdin=subprocess.DEVNULL, cwd=brain)
    found = p.stdout.strip().splitlines()
    if p.returncode == 0:
        rec["namecheck"] = "clean"
    elif p.returncode == 4 and found:
        rec["namecheck"] = "\n".join(found)[:2000]
        was = rec["report_check"]
        rec["report_check"] = (f"FLAGGED: namecheck: {found[0]}"
                               + ("" if was == "VERIFIED" else f" (report check: {was})"))[:400]
        tail = f" · ⚠ REPORT FLAGGED: namecheck: {found[0]}"[:440]
    else:
        why = (p.stdout.strip() or p.stderr.strip() or "no output").splitlines()[-1]
        rec["namecheck"] = ("unavailable: " + (why.partition("namecheck unavailable: ")[2]
                                               or f"exit {p.returncode}: {why}"))[:400]
except BaseException as e:
    rec["namecheck"] = f"unavailable: {type(e).__name__}: {e}"[:400]

# Order 63: a run that changed factgate_hook.py (sha256 at the end differs from the start) re-runs the
# hook's corpus of logged catches (workspace/seam/hookcorpus.py). Changed verdicts FLAG the report
# check, so a /build queue halts; a corpus that could not run (no venv, timeout) flags nothing.
# Flag, never block: the line is still written.
try:
    hook = os.path.join(brain, "factgate_hook.py")
    try:
        with open(hook, "rb") as fh:
            hook_sha1 = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        hook_sha1 = ""
    if hook_sha1 == hook_sha0:
        rec["hookcorpus"] = "hook unchanged"
    else:
        hookcorpus = os.path.join(brain, "workspace", "seam", "hookcorpus.py")
        if not os.path.exists(hookcorpus):
            raise FileNotFoundError(f"no {hookcorpus}")
        p = subprocess.run(["python3", hookcorpus, "--brain", brain], capture_output=True, text=True,
                           timeout=30 if int(rc) in (99, 143) else 330,
                           stdin=subprocess.DEVNULL, cwd=brain)
        got = (p.stdout.strip().splitlines() or [""])[0]
        if not got.startswith(("- hookcorpus:", "- 🔴 hookcorpus:")):
            why = (p.stderr.strip() or p.stdout.strip() or "no output").splitlines()[-1]
            got = f"- hookcorpus: not run (exit {p.returncode}: {why})"
        rec["hookcorpus"] = got[:2000]
        if p.returncode == 4:
            first = got.partition("changed: ")[2] or got
            was = rec["report_check"]
            rec["report_check"] = (f"FLAGGED: hookcorpus: {first}"
                                   + ("" if was == "VERIFIED" else f" (report check: {was})"))[:400]
            tail = f" · ⚠ REPORT FLAGGED: hookcorpus: {first}"[:440]
except BaseException as e:
    rec["hookcorpus"] = f"- hookcorpus: not run ({type(e).__name__}: {e})"[:400]
signal.signal(signal.SIGTERM, signal.SIG_IGN)     # the line below is written whole or not at all
if rec["report_check"].startswith("ERROR"):
    tail = f" · ⚠ report check {rec['report_check']}"

os.makedirs(os.path.dirname(log), exist_ok=True)
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")

# The completion note, through the note path app.py already uses. Best effort: a failure here
# must never change the run's outcome, and the RUNS line above is the durable record either way.
verdict = ("wrote its report" if rec["report_exists"] else "did NOT write a report")
if int(rc) == 99:
    verdict += " (killed before it finished — timeout or stop)"
spent = ""
if "wall_s" in rec:
    spent += f" · {round(rec['wall_s'] / 60)} min"
if "cost_usd" in rec:
    spent += f" · ${rec['cost_usd']:.2f}"
note = f"builder: order {nn} ended exit {rc}, {verdict}. Log: {os.path.basename(sys.argv[1])}.{spent}{tail}"
try:
    sys.path.insert(0, brain)
    import frontdesk
    frontdesk.add_note(frontdesk.notepad_path(brain), note)
except Exception as e:
    print(f"builder_run: could not post the note ({type(e).__name__}: {e})", flush=True)
PY
}
trap 'finish' EXIT
trap 'RC=143; finish; exit 143' TERM INT

PROMPT="Execute work order ${ORDER_FILE} exactly per the protocol. One order, then stop and write the report."

# stdin closed: the CLI waits 3s for piped input otherwise (measured 2026-09-25).
"$CLI" -p "$PROMPT" \
  --append-system-prompt-file "$PROTOCOL" \
  --model opus \
  --output-format stream-json --verbose \
  --max-budget-usd "$BUDGET" \
  --permission-mode bypassPermissions < /dev/null
RC=$?
exit "$RC"
}
main "$@"; exit $?
