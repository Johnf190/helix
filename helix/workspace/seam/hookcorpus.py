"""ORDER 63 (P-20261004-10): the hook's corpus of logged catches, run every night and after every
build that changes the hook.

Order 55 froze the logged catches into HOOK_CORPUS in test_factgate_hook.py. This runs exactly those
tests (`test_55_every…` and `test_55_the_corpus…`) through the brain's own venv pytest, in the brain
dir, with a 300 s cap. Their `real_paths` fixture sends refusals to a temp catches file, so a run never
touches the live catches log. One JSON record per run goes to HOOKCORPUS.jsonl beside this file (NOT
the catches log): ts, hook sha256, counts, first failing test id.

    python3 hookcorpus.py      prints the line; exit 0 held · 4 changed · 2 not run

`--brain`, `--python` and `--ledger` exist for the tests and the positive control (a temp copy of the
hook); the nightly and builder_run.sh use the defaults or pass the brain they already know.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN = Path.home() / "helix"
LEDGER = HERE / "HOOKCORPUS.jsonl"
TEST_FILE = "test_factgate_hook.py"
SELECT = "test_55_every or test_55_the_corpus"
TIMEOUT = 300
EXIT = {"held": 0, "changed": 4, "not run": 2}

_SUMMARY_RE = re.compile(r"\bin \d+(\.\d+)?s\b")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|xfailed|xpassed|errors?|deselected|skipped)\b")
_FIRST_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)")


def _sha256(p):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def parse(out):
    """Counts off pytest's last summary line, and the first FAILED/ERROR id; None if there is none."""
    lines = out.splitlines()
    summary = next((l for l in reversed(lines) if _SUMMARY_RE.search(l) and _COUNT_RE.search(l)), None)
    if summary is None:
        return None
    counts = {"passed": 0, "failed": 0, "xfailed": 0, "errors": 0}
    for n, word in _COUNT_RE.findall(summary):
        word = "errors" if word.startswith("error") else word
        if word == "xpassed":
            word = "failed"                      # an unexpected pass is a changed verdict too
        if word in counts:
            counts[word] += int(n)
    first = next((m.group(1) for m in map(_FIRST_RE.match, lines) if m), None)
    return {**counts, "first_failure": first}


def _pytest(argv, cwd, timeout):
    """(rc, output). The whole process group is killed on a timeout."""
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        raise TimeoutError(f"no result after {timeout:g} s")
    return p.returncode, out


def run(brain=BRAIN, python=None, ledger=LEDGER, timeout=TIMEOUT):
    """Run the corpus once, append one record to `ledger`, return the record (with `argv`, `status`
    and, when not run, `why`). Never raises on a missing venv, a timeout or an unwritable ledger."""
    brain = Path(brain)
    python = Path(python) if python else brain / ".venv" / "bin" / "python"
    argv = [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST_FILE, "-k", SELECT]
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "hook_sha256": _sha256(brain / "factgate_hook.py"),
           "passed": None, "failed": None, "xfailed": None, "errors": None, "first_failure": None}
    try:
        if not python.exists():
            raise FileNotFoundError(f"no {python}")
        rc, out = _pytest(argv, brain, timeout)
        got = parse(out)
        if got is None:
            raise LookupError(f"no pytest summary, exit {rc}")
        rec.update(got)
        if got["failed"] or got["errors"]:
            rec["status"] = "changed"
        elif not (got["passed"] or got["xfailed"]):
            raise LookupError(f"no corpus test ran, exit {rc}")
        else:
            rec["status"] = "held"
    except Exception as e:
        rec["status"] = "not run"
        rec["why"] = str(e) if isinstance(e, (FileNotFoundError, LookupError, TimeoutError)) \
            else f"{type(e).__name__}: {e}"
    try:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as e:
        rec["ledger_error"] = f"{type(e).__name__}: {e}"
    return {**rec, "argv": argv}


def line(result=None, **kw):
    """The one-line summary for the nightly and the RUNS line. Runs the corpus if not given a result.
    Never raises."""
    try:
        r = run(**kw) if result is None else result
        if r["status"] == "held":
            held = r["passed"] + r["xfailed"]
            return f"- hookcorpus: {held}/{held} verdicts held · hook {(r['hook_sha256'] or 'none')[:12]}"
        if r["status"] == "changed":
            return (f"- 🔴 hookcorpus: {r['failed'] + r['errors']} verdict(s) changed: "
                    f"{r['first_failure'] or '(no id in the output)'}")
        return f"- hookcorpus: not run ({r['why']})"
    except Exception as e:
        return f"- hookcorpus: not run ({type(e).__name__}: {e})"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the hook's corpus of logged catches.")
    ap.add_argument("--brain", default=str(BRAIN))
    ap.add_argument("--python", default=None)
    ap.add_argument("--ledger", default=str(LEDGER))
    a = ap.parse_args(argv)
    try:
        r = run(brain=a.brain, python=a.python, ledger=a.ledger)
    except Exception as e:
        r = {"status": "not run", "why": f"{type(e).__name__}: {e}"}
    print(line(r), flush=True)
    return EXIT.get(r.get("status"), 2)


if __name__ == "__main__":
    sys.exit(main())
