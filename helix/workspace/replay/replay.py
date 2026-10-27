#!/usr/bin/env python3
"""replay.py — replay packs (order 45/45b, item 26).

A result is stored with everything needed to recompute its reported numbers; the nightly replays
one pack at random, analysis stage only, and compares within a tolerance declared per number.

    replay.py make <name> --cmd "<command>" --inputs <paths…> --numbers <numbers.json>
                          --tolerance <tol.json> [--seeds <seeds.json>] [--note "<text>"]
    replay.py run [<name>|--random] [--json]

numbers.json  {"<number>": {"value": <packed value>, "pattern": "<regex, one group>"}, …}
              The pattern must match the command's stdout exactly once; its group is the number
              (thousands commas are dropped before it is read).
tol.json      {"<number>": <absolute tolerance>, …}. Every number needs one, or `make` refuses.

`make` copies the inputs and the code files the command runs (every command token after the
interpreter that is an existing file) into one flat pack dir, mtimes preserved, records sha256,
size and mtime_ns of every file, the interpreter's python and package versions, the git commit of
each code file (when it is tracked and clean; else its sha256 is the record), the seeds and the
tolerances. It runs the command once on a temp copy of the pack and refuses if the declared
numbers do not come back within tolerance. Only then is the pack written to BULK/<name> and one
line appended to PACKS.jsonl (name, path, manifest sha256, made_at). A pack is never edited after
`make`; a name is never reused (a new version is a new name).

`run` checks the pack against its manifest first (a changed, added or missing file, a moved mtime,
or a manifest whose sha256 is not the one in PACKS.jsonl = TAMPERED, not a replay), then runs the
stored command on a temp copy of the pack, reads the numbers, compares each within its tolerance,
and appends one line to REPLAYS.jsonl. It never repairs and never rewrites a pack.

🔴 FENCED_INPUT (BUILD_PROTOCOL §1, ruling of order 45b): `make` refuses any input or code file,
or any file or dir inside an input dir, whose name contains `credential`, `accounts` or `.env`
(case-insensitive). Names only: such a file is never opened, hashed or copied. It is not
dropped silently either: the caller must leave it out.

Pack contents live on the HDD (BULK); only the two small ledgers live here. BULK not available
(its disk not mounted) → `run` says so, exits 2 and writes no result line.

Exit codes:  0 MATCH (or no packs yet) · 2 no run (bulk store not available) · 3 refused (make)
             · 4 TAMPERED, MISMATCH or FAILED
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKS = HERE / "PACKS.jsonl"
REPLAYS = HERE / "REPLAYS.jsonl"
BULK = Path("__BULK__/replay")
MOUNT = Path("__BULK__")                      # BULK's disk; None = no mount to check (tests)
TIMEOUT = 45                                  # seconds per command run; the nightly line < 60 s
MANIFEST = "manifest.json"
FENCED = ("credential", "accounts", ".env")   # BUILD_PROTOCOL §1, by name, lowercased

EXIT_OK, EXIT_NO_RUN, EXIT_REFUSED, EXIT_FINDINGS = 0, 2, 3, 4

# Run by the pack's own interpreter, so the record is of what runs the command, not of us.
_PLATFORM_PROBE = (
    "import sys, json, platform, importlib.metadata as m\n"
    "pk = sorted({f\"{d.metadata['Name']}=={d.version}\" for d in m.distributions()"
    " if d.metadata['Name']})\n"
    "print(json.dumps({'python': sys.version, 'executable': sys.executable,"
    " 'platform': platform.platform(), 'packages': pk}))\n")


class Refused(Exception):
    """make will not build this pack; the message names why."""


def fenced(name):
    low = name.lower()
    return any(k in low for k in FENCED)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_ledger(path):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def append_line(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def bulk_available():
    """(ok, why). The HDD must be mounted and BULK must exist."""
    if MOUNT is not None and not os.path.ismount(MOUNT):
        return False, f"bulk store not mounted: {MOUNT}"
    if not Path(BULK).is_dir():
        return False, f"bulk store missing: {BULK}"
    return True, ""


def check_fence(paths):
    """Names only — nothing here opens a file. Raises Refused naming the first fenced path."""
    for p in paths:
        p = Path(p)
        if fenced(p.name):
            raise Refused(f"FENCED_INPUT {p}")
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                for n in sorted(dirs) + sorted(files):
                    if fenced(n):
                        raise Refused(f"FENCED_INPUT {Path(root) / n}")


def file_records(pack_dir):
    """{relative path: {sha256, size, mtime_ns}} for every file under pack_dir but the manifest."""
    out = {}
    for root, _dirs, files in os.walk(pack_dir):
        for n in files:
            p = Path(root) / n
            rel = str(p.relative_to(pack_dir))
            if rel == MANIFEST:
                continue
            st = p.stat()
            out[rel] = {"sha256": sha256(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return dict(sorted(out.items()))


def git_commit_of(path):
    """HEAD of the repo holding path, if the file is tracked and unmodified there; else None."""
    d = str(Path(path).parent)
    try:
        tracked = subprocess.run(["git", "-C", d, "ls-files", "--error-unmatch", Path(path).name],
                                 capture_output=True, text=True, timeout=10)
        if tracked.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", Path(path).name],
                               capture_output=True, text=True, timeout=10)
        if dirty.returncode != 0 or dirty.stdout.strip():
            return None
        head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        return head.stdout.strip() if head.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def platform_record(interpreter):
    r = subprocess.run([interpreter, "-c", _PLATFORM_PROBE], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        raise Refused(f"interpreter {interpreter!r} did not report its versions: {r.stderr.strip()}")
    return json.loads(r.stdout)


def parse_numbers(stdout, numbers):
    """{name: float or None}; None when the pattern does not match exactly once."""
    got = {}
    for n, spec in numbers.items():
        found = re.findall(spec["pattern"], stdout, flags=re.M)
        if len(found) != 1:
            got[n] = None
            continue
        try:
            got[n] = float(str(found[0]).replace(",", ""))
        except ValueError:
            got[n] = None
    return got


def compare(got, numbers, tolerance):
    """[{name, got, packed, tol, ok}] in declared order."""
    rows = []
    for n, spec in numbers.items():
        g, v, t = got.get(n), float(spec["value"]), float(tolerance[n])
        rows.append({"name": n, "got": g, "packed": v, "tol": t,
                     "ok": g is not None and abs(g - v) <= t})
    return rows


def run_cmd(argv, cwd):
    """(returncode, stdout, stderr); a timeout is returncode None."""
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {TIMEOUT} s"


def run_in_copy(pack_dir, argv):
    with tempfile.TemporaryDirectory(prefix="replay_") as tmp:
        work = Path(tmp) / "pack"
        shutil.copytree(pack_dir, work, copy_function=shutil.copy2)
        return run_cmd(argv, work)


# ---------------------------------------------------------------------------------------- make

def make(name, cmd, inputs, numbers, tolerance, seeds=None, note=""):
    """Build a pack. Returns the PACKS.jsonl record. Raises Refused; on refusal nothing is written."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or ""):
        raise Refused(f"bad pack name {name!r}")
    ok, why = bulk_available()
    if not ok:
        raise Refused(why)
    if any(r["name"] == name for r in read_ledger(PACKS)) or (Path(BULK) / name).exists():
        raise Refused(f"pack {name!r} exists; a pack is never edited, a new version is a new name")

    # every number declares its tolerance, and every tolerance is for a declared number
    if not numbers:
        raise Refused("no numbers declared")
    missing = [n for n in numbers if n not in tolerance]
    if missing:
        raise Refused(f"NO_TOLERANCE for {missing}")
    extra = [n for n in tolerance if n not in numbers]
    if extra:
        raise Refused(f"tolerance for undeclared numbers {extra}")
    for n, spec in numbers.items():
        if not isinstance(spec, dict) or "value" not in spec or "pattern" not in spec:
            raise Refused(f"number {n!r} needs a value and a pattern")
        if re.compile(spec["pattern"]).groups != 1:
            raise Refused(f"number {n!r}: pattern needs exactly one group")
        t = tolerance[n]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0:
            raise Refused(f"tolerance for {n!r} must be a number >= 0")

    inputs = [Path(p).resolve() for p in inputs]
    for p in inputs:
        if not p.exists():
            raise Refused(f"input not found: {p}")

    # the command: first token is the interpreter, kept as given; every later token that is an
    # input becomes the input's name in the pack, every other existing file is a code file
    argv = shlex.split(cmd)
    if not argv:
        raise Refused("empty command")
    by_real = {str(p): p for p in inputs}
    code, packed_argv = [], [argv[0]]
    for tok in argv[1:]:
        real = str(Path(tok).resolve())
        if real in by_real:
            packed_argv.append(by_real[real].name)
        elif Path(tok).is_file():
            code.append(Path(tok).resolve())
            packed_argv.append(Path(tok).name)
        else:
            packed_argv.append(tok)

    check_fence(code + inputs)                # before anything is read or copied
    names = [p.name for p in code + inputs]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup or MANIFEST in names:
        raise Refused(f"name clash in the flat pack: {dup or [MANIFEST]}")

    with tempfile.TemporaryDirectory(prefix="replay_make_") as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        for p in code + inputs:
            if p.is_dir():
                shutil.copytree(p, stage / p.name, copy_function=shutil.copy2)
            else:
                shutil.copy2(p, stage / p.name)

        manifest = {
            "name": name,
            "made_at": now(),
            "cmd": packed_argv,
            "cmd_as_given": cmd,
            "code": [{"file": p.name, "source": str(p), "sha256": sha256(p),
                      "git_commit": git_commit_of(p)} for p in code],
            "inputs": [{"as": p.name, "source": str(p), "kind": "dir" if p.is_dir() else "file"}
                       for p in inputs],
            "files": file_records(stage),
            "seeds": seeds if seeds is not None else "none declared",
            "platform": platform_record(argv[0]),
            "numbers": numbers,
            "tolerance": tolerance,
            "note": note,
        }
        # the declared numbers must come back from the pack itself before it is kept
        rc, out, err = run_in_copy(stage, packed_argv)
        if rc != 0:
            raise Refused(f"command failed on the staged pack (exit {rc}): {err.strip()[-300:]}")
        rows = compare(parse_numbers(out, numbers), numbers, tolerance)
        bad = [r for r in rows if not r["ok"]]
        if bad:
            raise Refused("declared numbers do not reproduce: " + "; ".join(
                f"{r['name']}: got {r['got']}, declared {r['packed']}, tol {r['tol']}" for r in bad))

        (stage / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
        final = Path(BULK) / name
        shutil.copytree(stage, final, copy_function=shutil.copy2)

    if file_records(final) != manifest["files"]:
        raise Refused(f"pack at {final} does not match its manifest after copying")
    rec = {"name": name, "path": str(final), "manifest_sha256": sha256(final / MANIFEST),
           "made_at": manifest["made_at"]}
    append_line(PACKS, rec)
    return rec


# ----------------------------------------------------------------------------------------- run

def verify(pack, rec):
    """[] when the pack is exactly what was made; else what changed."""
    pack = Path(pack)
    mpath = pack / MANIFEST
    if not mpath.is_file():
        return ["manifest missing"]
    if sha256(mpath) != rec["manifest_sha256"]:
        return ["manifest sha256 differs from PACKS.jsonl"]
    want = json.loads(mpath.read_text())["files"]
    have = file_records(pack)
    diffs = [f"missing {f}" for f in want if f not in have]
    diffs += [f"added {f}" for f in have if f not in want]
    for f in want:
        if f in have:
            for k in ("sha256", "size", "mtime_ns"):
                if have[f][k] != want[f][k]:
                    diffs.append(f"{f}: {k} changed")
                    break
    return diffs


def run(name=None):
    """Replay one pack (by name, or at random). Returns (exit code, result dict)."""
    ok, why = bulk_available()
    if not ok:
        return EXIT_NO_RUN, {"result": "NO_RUN", "why": why}
    packs = read_ledger(PACKS)
    if not packs:
        return EXIT_OK, {"result": "NO_PACKS"}
    if name is None:
        rec = random.choice(packs)
    else:
        found = [r for r in packs if r["name"] == name]
        if not found:
            return EXIT_NO_RUN, {"result": "NO_RUN", "why": f"no pack named {name!r}"}
        rec = found[-1]

    pack = Path(BULK) / rec["name"]
    t0 = time.monotonic()
    res = {"at": now(), "name": rec["name"], "path": str(pack)}
    diffs = verify(pack, rec)
    if diffs:
        res.update(result="TAMPERED", changes=diffs, seconds=round(time.monotonic() - t0, 2))
        append_line(REPLAYS, res)
        return EXIT_FINDINGS, res

    manifest = json.loads((pack / MANIFEST).read_text())
    rc, out, err = run_in_copy(pack, manifest["cmd"])
    if rc != 0:
        res.update(result="FAILED", exit=rc, stderr=err.strip()[-500:])
    else:
        rows = compare(parse_numbers(out, manifest["numbers"]), manifest["numbers"],
                       manifest["tolerance"])
        res.update(result="MATCH" if all(r["ok"] for r in rows) else "MISMATCH", numbers=rows)
    res["seconds"] = round(time.monotonic() - t0, 2)
    append_line(REPLAYS, res)
    return (EXIT_OK if res["result"] == "MATCH" else EXIT_FINDINGS), res


def _num(x):
    if x is None:
        return "no match"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def line_for(res):
    r = res["result"]
    if r == "NO_PACKS":
        return "- replay: no packs yet"
    if r == "NO_RUN":
        return f"- replay: 🔴 replay did not run ({res['why']})"
    if r == "TAMPERED":
        return f"- replay: 🔴 {res['name']} TAMPERED"
    if r == "FAILED":
        return f"- replay: 🔴 {res['name']} FAILED (exit {res['exit']})"
    rows = res["numbers"]
    if r == "MATCH":
        return f"- replay: {res['name']} MATCH ({len(rows)}/{len(rows)} within tolerance)"
    bad = [x for x in rows if not x["ok"]]
    return (f"- replay: 🔴 {res['name']} MISMATCH ("
            + "; ".join(f"{x['name']}: got {_num(x['got'])}, packed {_num(x['packed'])}, "
                        f"tol {_num(x['tol'])}" for x in bad) + ")")


def replay_line():
    """The nightly's one line: one pack at random, analysis stage only."""
    _code, res = run(None)
    return line_for(res)


# ----------------------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="replay.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    m = sub.add_parser("make")
    m.add_argument("name")
    m.add_argument("--cmd", required=True)
    m.add_argument("--inputs", nargs="+", required=True)
    m.add_argument("--numbers", required=True)
    m.add_argument("--tolerance", required=True)
    m.add_argument("--seeds")
    m.add_argument("--note", default="")
    r = sub.add_parser("run")
    r.add_argument("name", nargs="?")
    r.add_argument("--random", action="store_true")
    r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.verb == "make":
        try:
            rec = make(a.name, a.cmd, a.inputs, json.loads(Path(a.numbers).read_text()),
                       json.loads(Path(a.tolerance).read_text()),
                       json.loads(Path(a.seeds).read_text()) if a.seeds else None, a.note)
        except Refused as e:
            print(f"🔴 make refused: {e}", file=sys.stderr)
            return EXIT_REFUSED
        print(json.dumps(rec))
        return EXIT_OK

    if a.name and a.random:
        ap.error("give a name or --random, not both")
    if not a.name and not a.random:
        ap.error("give a name or --random")
    code, res = run(a.name)
    print(json.dumps(res, ensure_ascii=False) if a.json else line_for(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
