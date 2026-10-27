#!/usr/bin/env python3
"""timeforward.py — ORDER 56 (P-20261004-5): the nightly's registry work, dry-run on the day
after each due date, so a crash waiting at a future due date is found now, not on the night.

Why: catch #3 — a prediction coming due would have crashed the nightly with KeyError 'brier' on
2027-01-31; it was found by reading code, months early. This finds that kind of thing by running.

For every distinct due date of an unscored prediction, on a FRESH temp copy of the registry dir:
  - nightly.REG    → the copy's registry.json (its REGATE_LEDGER.jsonl sits beside it);
  - nightly.REGATE → a temp shim that loads regate.py from its real path, replaces regate.today
                     with the simulated date, points regate's REFUSALS_LOG and SEALGATE (a second
                     shim, its own REFUSALS_LOG temp too) into the temp dir, and calls regate's
                     own main() with the same argv;
  - nightly.TODAY  → the simulated date (the due date + 1 day);
then calls, in main()'s order, every nightly function that reads REG / the ledger or compares a
due date with TODAY (CALLED below). Everything is caught: one result per date, `ok` or the
exception and its last frame. A regate traceback inside the shim is a crash too (the shim records
it before re-raising); so is a line the nightly printed in place of an exception ("did not run").

The date moves ONLY here, on the temp copy. Nothing was added to nightly.py or regate.py to let a
caller choose the date: a date a caller could choose on the real path would be a way past S2
(catch #4). The nightly's constants are put back in a `finally`.

    python3 timeforward.py      # the live registry, dry-run; exit 0 no crashes · 1 crashes
"""
import datetime
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nightly  # noqa: E402

REGISTRY = HERE / "registry"
REGATE = Path.home() / "helix" / "workspace" / "regate" / "regate.py"
SEALGATE = Path.home() / "helix" / "workspace" / "sealgate" / "sealgate.py"
# The nightly functions that read REG / the ledger or compare a due date with TODAY, in the order
# main() calls them. Not called: main() itself (it runs the ANCHOR audit and writes history.jsonl,
# claims_seen.json and NIGHTLY.md) — its inline `next:` line only reads REG and is not a function.
CALLED = ("score", "registry_line", "provenance_line", "summary_lines", "needs_seal_lines")

SEALGATE_SHIM = """import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("sealgate", {gate!r})
sealgate = importlib.util.module_from_spec(spec)
sys.modules["sealgate"] = sealgate
spec.loader.exec_module(sealgate)
sealgate.REFUSALS_LOG = Path({refusals!r})
sys.exit(sealgate.main(sys.argv[1:]))
"""

REGATE_SHIM = """import importlib.util, json, sys, traceback
from pathlib import Path
spec = importlib.util.spec_from_file_location("regate", {gate!r})
regate = importlib.util.module_from_spec(spec)
sys.modules["regate"] = regate
spec.loader.exec_module(regate)
regate.today = lambda: {day!r}
regate.REFUSALS_LOG = Path({refusals!r})
regate.SEALGATE = Path({sealgate!r})
try:
    code = regate.main(sys.argv[1:])
except SystemExit:
    raise
except BaseException as e:
    frame = traceback.extract_tb(e.__traceback__)[-1]
    with open({crash!r}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"error": f"{{type(e).__name__}} {{e}}",
                             "frame": f"{{Path(frame.filename).name}}:{{frame.lineno}} in {{frame.name}}"}}) + "\\n")
    raise
sys.exit(code)
"""


def due_dates(registry_dir):
    """Sorted distinct due dates of the unscored predictions in registry_dir/registry.json."""
    reg = json.loads((Path(registry_dir) / "registry.json").read_text(encoding="utf-8"))
    return sorted({p.get("due") for e in reg.values() for p in e.get("predictions", [])
                   if not p.get("scored")}, key=str)


def _crash(e):
    frames = traceback.extract_tb(e.__traceback__)
    frame = f"{Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}" \
        if frames else "?"
    return {"status": "crash", "error": f"{type(e).__name__} {e}", "frame": frame}


def simulate(due, registry_dir, regate=None):
    """One date: the nightly's registry work on `due` + 1 day, on a temp copy. Never raises."""
    regate = Path(regate or REGATE)
    res = {"due": due, "day": None, "status": "ok", "error": None, "frame": None, "lines": []}
    try:
        day = datetime.date.fromisoformat(due) + datetime.timedelta(days=1)
    except Exception as e:
        return {**res, **_crash(e), "error": f"unreadable due date {due!r}"}
    res["day"] = day.isoformat()
    saved = (nightly.REG, nightly.REGATE, nightly.TODAY)
    with tempfile.TemporaryDirectory(prefix="timeforward-") as tmp:
        tmp = Path(tmp)
        copy = tmp / "registry"
        crash_log = tmp / "regate_crash.jsonl"
        try:
            shutil.copytree(registry_dir, copy)
            if copy.resolve() == Path(registry_dir).resolve():
                raise RuntimeError("the copy is the source")
            sealgate_shim = tmp / "sealgate_shim.py"
            sealgate_shim.write_text(SEALGATE_SHIM.format(
                gate=str(SEALGATE), refusals=str(tmp / "sealgate_refusals.log")))
            shim = tmp / "regate_shim.py"
            shim.write_text(REGATE_SHIM.format(
                gate=str(regate), day=day.isoformat(), refusals=str(tmp / "regate_refusals.log"),
                sealgate=str(sealgate_shim), crash=str(crash_log)))
            nightly.REG, nightly.REGATE, nightly.TODAY = copy / "registry.json", shim, day
            lines = res["lines"]
            scored, refused, overdue = nightly.score([], {})
            lines.append(nightly.registry_line())
            lines.append(nightly.provenance_line())
            lines.extend(nightly.summary_lines(scored, refused, overdue))
            lines.extend(nightly.needs_seal_lines())
        except Exception as e:
            res.update(_crash(e))
        finally:
            nightly.REG, nightly.REGATE, nightly.TODAY = saved
        if res["status"] == "ok" and crash_log.exists():
            first = json.loads(crash_log.read_text(encoding="utf-8").splitlines()[0])
            res.update(status="crash", error=first["error"], frame=first["frame"])
        if res["status"] == "ok":
            hidden = next((l for l in res["lines"] if "did not run" in l), None)
            if hidden:
                res.update(status="crash", error=hidden.strip("- "), frame="caught by the nightly")
    return res


def run(registry_dir=REGISTRY, regate=None):
    """One result per distinct due date of an unscored prediction, earliest first."""
    return [simulate(d, registry_dir, regate or REGATE) for d in due_dates(registry_dir)]


def line_of(results):
    """The `- timeforward:` line for a list of results."""
    crashes = [r for r in results if r["status"] != "ok"]
    if crashes:
        more = f" (+{len(crashes) - 1} more)" if len(crashes) > 1 else ""
        return f"- 🔴 timeforward: {crashes[0]['due']} crashes: {crashes[0]['error']}{more}"
    nxt = results[0]["due"] if results else "—"
    return f"- timeforward: {len(results)} due date(s) simulated (next {nxt}) · 0 crashes"


def timeforward_line(registry_dir=REGISTRY, regate=None):
    """The nightly's `- timeforward:` line."""
    return line_of(run(registry_dir, regate))


def main():
    results = run()
    for r in results:
        what = "ok" if r["status"] == "ok" else f"CRASH {r['error']} ({r['frame']})"
        print(f"{r['due']} (simulated {r['day']}): {what}")
        for line in r["lines"]:
            print(f"    {line}")
    print(line_of(results))
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
