#!/usr/bin/env python3
"""seatswap.py — seat-swap replication (order 49, item 10).

    seatswap.py run <pack-name>     give a stored analysis to a different mind (Alice), reproduce
                                    her independent re-analysis, compare it with the reported
                                    numbers, record the outcome in SWAPS.jsonl
    seatswap.py check <pack-name>   regate's S7: does SWAPS.jsonl hold an intact AGREE for this
                                    pack's CURRENT manifest? One JSON line; exit 0 yes, 4 no

"Same code, different mind reading it." Every analysis script in Helix is Claude-written, so the
different seat is always Alice (Gemini via `agy`): the only non-Claude mind, and the only seat
that can execute. The pack is a replay pack (order 45, workspace/replay): its inputs, its script,
and a manifest of the numbers it reported, each with a tolerance.

A run:
  1. The pack is checked against its manifest with replay.py's own `verify` (imported, not
     copied). A changed, added or missing file, a moved mtime, or a manifest whose sha256 is not
     the one in PACKS.jsonl → FAILED (pack tampered), and Alice is never asked.
  2. A fresh __BULK__/seatswap/<UTC>/scratch/ gets `pack/`: the pack's files (mtimes
     kept) and a manifest with the reported VALUES STRIPPED (keys, patterns, tolerances, the note,
     the command; no values, no hashes), bound READ-ONLY in her sandbox; and `alice/`, writable,
     her cwd. Credential-named files (`credential`, `accounts`, `.env`) are never copied.
  3. Alice (veto/sandbox.py's sandbox, imported, not copied; 20-minute cap) is asked to write her
     OWN `swap.py` that computes each listed number from the inputs, and `NOTES.md` on anything in
     the original she thinks is wrong. The prompt points at the pack; it does not contain it.
  4. Independence (grep-level, by the harness): any file in `alice/` that names a pack code file's
     module in an import/exec/run/open line, that uses dynamic execution (exec, eval, compile,
     subprocess, importlib, runpy, __import__, os.system/popen/exec*/spawn*), that touches
     sys.path, or that has a pack code file's sha256 → COPIED. It does not count, either way.
  5. Her swap.py (only that file) runs twice in the reproduction sandbox: a fresh dir, a pristine
     read-only copy of the pack at `pack/`, cwd `alice/`, no network, cleared environment,
     PYTHONSAFEPATH=1. Both must exit 0 and print the same last line, a JSON object. Not →
     NOT_REPRODUCED.
  6. Each key is compared with the pack's reported value within the pack's own tolerance →
     AGREE (every key) or DISAGREE (<key>: alice X, packed Y, tol Z; …). A missing key, or one
     that is not a finite number, is a disagreement.
  7. One line is appended to SWAPS.jsonl.

SWAPS.jsonl beside this file is append-only and hash-chained with veto.py's chain (imported):
each line carries `seq` and `prev` = sha256 of the previous line's raw bytes. Not behind the write
wall: an edit is visible, not impossible, and the last line has no successor to witness it.

There is no flag that fakes, skips or stands in for Alice. Tests inject a fake Alice (`ALICE`)
with monkeypatch.

Exit codes:  run: 0 recorded (any outcome; read it) · 2 usage · 3 refused (broken chain, no such
             pack) — check: 0 AGREE for the current manifest · 4 anything else · 2 usage
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

VERSION = "1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_NO = 4

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent                       # ~/helix/workspace: this file is workspace/seatswap/
REPLAY = WORKSPACE / "replay"
VETO = WORKSPACE / "veto"
REPLAY_PY = REPLAY / "replay.py"
VETO_PY = VETO / "veto.py"
SANDBOX_PY = VETO / "sandbox.py"
LEDGER = HERE / "SWAPS.jsonl"
PACKS = REPLAY / "PACKS.jsonl"
PACK_BULK = Path("__BULK__/replay")
BULK = Path("__BULK__/seatswap")   # bulk output lives on the HDD (153_*)

ALICE_TIMEOUT = 1200                          # 20 min, then her process group is killed
REPRO_TIMEOUT = 120                           # per reproduction run
PROMPT_CAP = 120_000                          # her prompt is one argv string (131,072)
MANIFEST = "manifest.json"
SWAP = "swap.py"
NOTES = "NOTES.md"
PACK_DIR = "pack"                             # the layout of every scratch and repro dir
ALICE_DIR = "alice"
CREDENTIAL_RE = re.compile(r"credential|accounts|^\.env", re.I)

OUTCOMES = ("AGREE", "DISAGREE", "COPIED", "NOT_REPRODUCED", "FAILED")
# What she may see of the manifest: never a value, never a hash.
SHOWN_FIELDS = ("name", "cmd", "inputs", "seeds", "tolerance", "note")

# Grep-level independence: a line naming a pack code module beside one of these words …
USE_WORDS = re.compile(r"\b(import|from|exec|execfile|eval|compile|runpy|run_path|run_module|"
                       r"subprocess|Popen|system|popen|spawn\w*|execv\w*|__import__|importlib|"
                       r"open|read_text|read_bytes|source)\b")
# … and these anywhere: a swap.py has no business running other code or moving sys.path.
DYNAMIC = re.compile(r"\b(exec|eval|compile|execfile)\s*\(|\b(subprocess|importlib|runpy|"
                     r"__import__|ctypes)\b|\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)"
                     r"\b|\bsys\.path\b|\bpty\b")

BRIEF = (
    "You are Alice, a second analyst with a shell. `../pack/` holds a read-only copy of a stored "
    "analysis: its inputs, its script ({code}) and `manifest.json`. Here is a stored analysis: "
    "its inputs, its script, and the numbers it reported (keys listed, values WITHHELD). The "
    "keys are: {keys}. `../pack/manifest.json` gives, for each key, the stdout pattern it was "
    "read with, its tolerance, and a note on the method; the script was run as `{cmd}` from "
    "inside the pack directory. Files in ../pack/ keep their stored modification times. The "
    "current directory is yours.\n\n"
    "Write YOUR OWN analysis (`swap.py`) that computes each listed number from the inputs, from "
    "the method described in the manifest note and the script's docstring. Read the original "
    "script to understand the method, but do not import, call, exec or copy it. Print one JSON "
    "object {{key: number}} as the last stdout line. Write `alice/NOTES.md`: anything in the "
    "original analysis you think is wrong (a mis-joined table, an off-by-one, a wrong "
    "denominator). (`alice/` is the current directory; write NOTES.md here, beside swap.py. "
    "If you find nothing wrong, say so in it.)\n\n"
    "How swap.py is checked: it is run twice, each time in a FRESH directory at a DIFFERENT path "
    "that holds ONLY your swap.py, beside a pristine read-only copy of the pack at `../pack/`. "
    "It runs as `python3 swap.py` with no network, no home directory, a cleared environment "
    "(PYTHONSAFEPATH=1, PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, no PYTHONPATH) and a "
    "{repro} s limit. Use relative paths (`../pack/...`) and the standard library only. Both "
    "runs must exit 0 and print exactly the same last line. It is thrown out as a copy, and "
    "does not count, if swap.py or any file you leave here imports, runs, executes or opens the "
    "original script, uses exec/eval/compile/subprocess/importlib/runpy/os.system, touches "
    "sys.path, or is a byte copy of a pack file.")


# --------------------------------------------------------------------- helpers

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mods = {}


def _module(name, path):
    if name not in _mods:
        _mods[name] = load_module(name, path)
    return _mods[name]


def replay_module():
    """replay.py, imported from its file: its `verify` and ledger reader are reused."""
    return _module("seatswap_replay", REPLAY_PY)


def veto_module():
    """veto.py, imported from its file: its hash chain is reused."""
    return _module("seatswap_veto", VETO_PY)


def sandbox_module():
    """veto/sandbox.py, imported from its file: Alice's sandbox and the reproduction sandbox."""
    return _module("seatswap_sandbox", SANDBOX_PY)


def sha256_bytes(raw):
    return veto_module().sha256_bytes(raw)


def file_sha(p):
    return sha256_bytes(Path(p).read_bytes())


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Refused(Exception):
    """No run: the ledger chain is broken or there is no such pack. Nothing is written."""


# ---------------------------------------------------------------------- ledger
# veto.py's chain (itself sealgate/review.py's), imported: the same envelope, our own version tag.

GENESIS = hashlib.sha256(b"").hexdigest()


def read_ledger(ledger=None):
    return veto_module().read_ledger(ledger or LEDGER)


def verify_chain(entries):
    return veto_module().verify_chain(entries)


def append(records, ledger=None):
    """Append, never rewrite. Each line carries seq and the hash of the line before it."""
    path = Path(ledger or LEDGER)
    entries = read_ledger(path)
    prev = sha256_bytes(entries[-1][1].encode("utf-8")) if entries else GENESIS
    seq = len(entries)
    out, written = [], []
    for rec in records:
        seq += 1
        rec = dict(rec, seq=seq, prev=prev, ts=utc_now(), seatswap=VERSION)
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out.append(raw)
        written.append(rec)
        prev = sha256_bytes(raw.encode("utf-8"))
    needs_nl = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n" if needs_nl else "") + "".join(l + "\n" for l in out))
    return written


def chain_problem(entries):
    problems = verify_chain(entries)
    if problems:
        return f"SWAPS.jsonl chain is broken at line {problems[0][0]}: {problems[0][1]}"
    return None


# ------------------------------------------------------------------- the pack

def pack_record(name):
    """The pack's last line in PACKS.jsonl (a name is never reused; last wins, as in replay)."""
    found = [r for r in replay_module().read_ledger(PACKS) if r.get("name") == name]
    return found[-1] if found else None


def pack_changes(name, rec):
    """[] when the pack is exactly what was made (replay.py's own check); else what changed."""
    return replay_module().verify(Path(PACK_BULK) / name, rec)


def copy_pack(src, dest):
    """Copy every file of the pack but its manifest, mtimes kept, credential-named never.
    → (copied relative paths, [(relative path, why)] left out)."""
    src, dest = Path(src), Path(dest)
    copied, left_out = [], []
    for dirpath, dirs, files in os.walk(src):
        dirs.sort()
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = str(p.relative_to(src))
            if rel == MANIFEST:
                continue
            if any(CREDENTIAL_RE.search(part) for part in Path(rel).parts):
                left_out.append((rel, "credential-named, never copied"))
                continue
            if p.is_symlink() or not p.is_file():
                left_out.append((rel, "not a regular file"))
                continue
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / rel)
            copied.append(rel)
    return copied, left_out


def shown_manifest(manifest):
    """The manifest as she sees it: keys and patterns, never a value or a hash."""
    shown = {k: manifest[k] for k in SHOWN_FIELDS if k in manifest}
    shown["code"] = [c["file"] for c in manifest.get("code", [])]
    shown["numbers"] = {k: {"pattern": v["pattern"]} for k, v in manifest["numbers"].items()}
    shown["values"] = "WITHHELD"
    return shown


def give_pack(name, dest):
    """A fresh copy of the pack for her (or for a reproduction run), from the pack's own dir.
    → (tree hashes, copied, left out)."""
    src = Path(PACK_BULK) / name
    dest = Path(dest)
    dest.mkdir(parents=True)
    copied, left_out = copy_pack(src, dest)
    manifest = json.loads((src / MANIFEST).read_text())
    (dest / MANIFEST).write_text(json.dumps(shown_manifest(manifest), indent=1,
                                            ensure_ascii=False) + "\n")
    return veto_module().tree_hashes(dest), copied, left_out


def build_prompt(manifest):
    prompt = BRIEF.format(code=", ".join(c["file"] for c in manifest.get("code", [])) or "none",
                          keys=", ".join(manifest["numbers"]), cmd=" ".join(manifest["cmd"]),
                          repro=REPRO_TIMEOUT)
    if len(prompt.encode("utf-8")) >= PROMPT_CAP:
        raise ValueError(f"the prompt is over the {PROMPT_CAP:,}-byte cap")
    return prompt


# ------------------------------------------------------------------- Alice

class AliceFailed(Exception):
    """agy errored, timed out or said nothing usable: the run is FAILED."""


def call_alice(workdir, prompt):
    """Alice in her sandbox: the whole scratch bound, cwd = its alice/, its pack/ read-only.
    → meta dict; raises AliceFailed."""
    sandbox = sandbox_module()
    workdir = Path(workdir)
    rc, out, err, seconds = sandbox.run_alice(workdir.parent, prompt, ALICE_TIMEOUT,
                                              cwd=workdir, read_only=[workdir.parent / PACK_DIR])
    meta = {"model": sandbox.MODEL, "via": "agy (bwrap)", "rc": rc, "seconds": seconds}
    if rc is None:
        raise AliceFailed(f"timed out after {ALICE_TIMEOUT} s")
    if rc != 0:
        raise AliceFailed(f"agy exited {rc}: {err.strip()[-300:]}")
    try:
        reply = json.loads(out)
        status = reply.get("status")
    except (ValueError, AttributeError) as exc:
        raise AliceFailed(f"agy output is not the expected JSON ({exc})")
    if status != "SUCCESS":
        raise AliceFailed(f"agy status {status!r}")
    meta["response_tail"] = str(reply.get("response", ""))[-500:]
    return meta


ALICE = call_alice


# ------------------------------------------------------------- independence

def independence(alice_dir, manifest, pack_dir):
    """[{file, reason}]: the grep-level copy check over every file in alice/. Empty = independent."""
    alice_dir, pack_dir = Path(alice_dir), Path(pack_dir)
    code = [c["file"] for c in manifest.get("code", [])]
    stems = sorted({Path(f).stem for f in code} | {Path(f).name for f in code})
    code_shas = {file_sha(pack_dir / f): f for f in code if (pack_dir / f).is_file()}
    hits = []
    for dirpath, dirs, files in os.walk(alice_dir):
        dirs.sort()
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = str(p.relative_to(alice_dir))
            if p.is_symlink() or not p.is_file():
                hits.append({"file": rel, "reason": "a symlink"})
                continue
            raw = p.read_bytes()
            digest = sha256_bytes(raw)
            if digest in code_shas:
                hits.append({"file": rel, "reason": f"has the sha256 of the pack's {code_shas[digest]}"})
                continue
            if rel == NOTES:
                continue                      # her prose may name the script it criticises
            text = raw.decode("utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                named = [s for s in stems if re.search(rf"(?<![\w]){re.escape(s)}(?![\w])", line)]
                if named and USE_WORDS.search(line):
                    hits.append({"file": rel, "reason": f"line {n} uses {named[0]}: "
                                                        f"{line.strip()[:120]}"})
                    break
                m = DYNAMIC.search(line)
                if m:
                    hits.append({"file": rel, "reason": f"line {n} uses {m.group(0).strip()}: "
                                                        f"{line.strip()[:120]}"})
                    break
    return hits


# ------------------------------------------------------------- reproduction

class PackMoved(Exception):
    """The pack no longer gives the copy Alice was given: the run is FAILED."""


def last_json(out):
    """The last stdout line parsed as a JSON object, or None."""
    lines = out.strip().splitlines()
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def reproduce(name, her, run_dir, expected):
    """Run her swap.py twice. Each run: a fresh dir with a pristine read-only pack/ and an
    alice/ holding only swap.py, cwd = alice/. → {"reproduced", "runs", "result"}."""
    sandbox = sandbox_module()
    runs, lasts = [], []
    for i in (1, 2):
        fresh = Path(run_dir) / f"repro{i}"
        pack_dir, alice_dir = fresh / PACK_DIR, fresh / ALICE_DIR
        hashes, _c, _l = give_pack(name, pack_dir)
        if hashes != expected:
            raise PackMoved("the pack changed during the run: a fresh copy no longer matches "
                            "the copy Alice was given")
        alice_dir.mkdir(parents=True)
        shutil.copyfile(Path(her) / SWAP, alice_dir / SWAP)
        rc, out, err, seconds = sandbox.run_reproduction(fresh, ["python3", SWAP], REPRO_TIMEOUT,
                                                         cwd=alice_dir, read_only=[pack_dir])
        (Path(run_dir) / f"repro{i}.stdout.txt").write_text(out, encoding="utf-8")
        lines = out.strip().splitlines()
        last = lines[-1] if lines else ""
        runs.append({"dir": str(fresh), "rc": rc, "last_line": last[:2000],
                     "stdout_sha256": sha256_bytes(out.encode("utf-8")), "seconds": seconds,
                     "stderr_tail": err.strip()[-300:]})
        lasts.append(last)
    result = last_json(lasts[0])
    reproduced = (all(r["rc"] == 0 for r in runs) and lasts[0] == lasts[1]
                  and result is not None)
    return {"reproduced": reproduced, "runs": runs, "result": result if reproduced else None}


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def compare(result, manifest):
    """[{key, alice, packed, tol, ok}] in the manifest's order."""
    rows = []
    for key, spec in manifest["numbers"].items():
        got = result.get(key)
        got = got if is_number(got) else None
        packed, tol = float(spec["value"]), float(manifest["tolerance"][key])
        rows.append({"key": key, "alice": got, "packed": packed, "tol": tol,
                     "ok": got is not None and abs(got - packed) <= tol})
    return rows


def _num(x):
    if x is None:
        return "missing"
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def disagreement(rows):
    return "; ".join(f"{r['key']}: alice {_num(r['alice'])}, packed {_num(r['packed'])}, "
                     f"tol {_num(r['tol'])}" for r in rows if not r["ok"])


# ------------------------------------------------------------------------ run

def run(name, ledger=None):
    """One seat-swap run. Appends exactly one line. → the record. Raises Refused."""
    entries = read_ledger(ledger)
    why = chain_problem(entries)
    if why:
        raise RuntimeError(why)
    rec_pack = pack_record(name)
    if rec_pack is None:
        raise Refused(f"no pack named {name!r} in {PACKS}")
    started = utc_now()
    base = {"kind": "swap", "pack": name, "manifest_sha256": rec_pack["manifest_sha256"],
            "started": started}

    changes = pack_changes(name, rec_pack)
    if changes:
        return append([dict(base, outcome="FAILED", why="pack tampered", changes=changes)],
                      ledger)[0]

    manifest = json.loads((Path(PACK_BULK) / name / MANIFEST).read_text())
    run_dir = Path(BULK) / utc_stamp()
    n = 1
    while run_dir.exists():
        n += 1
        run_dir = run_dir.with_name(f"{run_dir.name.split('.')[0]}.{n}")
    scratch = run_dir / "scratch"
    workdir = scratch / ALICE_DIR
    workdir.mkdir(parents=True)
    given, copied, left_out = give_pack(name, scratch / PACK_DIR)
    before = veto_module().tree_hashes(scratch)
    prompt = build_prompt(manifest)

    outcome, failed_why, meta = None, None, {}
    try:
        meta = ALICE(workdir, prompt) or {}
    except AliceFailed as exc:
        outcome, failed_why = "FAILED", str(exc)
    except Exception as exc:                      # anything else is a failed run, not a pass
        outcome, failed_why = "FAILED", f"{type(exc).__name__}: {exc}"[:300]

    # Everything she wrote, hashed before anything else runs.
    after = veto_module().tree_hashes(scratch)
    files = {k: v for k, v in after.items() if before.get(k) != v}
    swap_sha = files.get(f"{ALICE_DIR}/{SWAP}")
    notes_sha = after.get(f"{ALICE_DIR}/{NOTES}")

    hits, repro, rows = [], {}, []
    try:
        if outcome is None:
            if veto_module().tree_hashes(scratch / PACK_DIR) != given:
                outcome, failed_why = "FAILED", "her copy of the pack changed during the run"
            elif not swap_sha or not (workdir / SWAP).is_file() or (workdir / SWAP).is_symlink():
                outcome, failed_why = "FAILED", "no output: no swap.py"
            else:
                hits = independence(workdir, manifest, scratch / PACK_DIR)
                if hits:
                    outcome = "COPIED"
                    failed_why = "; ".join(f"{h['file']}: {h['reason']}" for h in hits)[:600]
                else:
                    repro = reproduce(name, workdir, run_dir, given)
                    if not repro["reproduced"]:
                        outcome, failed_why = "NOT_REPRODUCED", (
                            "rc " + ", ".join(str(r["rc"]) for r in repro["runs"])
                            + ("; last lines differ" if repro["runs"][0]["last_line"]
                               != repro["runs"][1]["last_line"] else "")
                            + ("" if last_json(repro["runs"][0]["last_line"]) is not None
                               else "; last line is not a JSON object"))
                    else:
                        rows = compare(repro["result"], manifest)
                        if all(r["ok"] for r in rows):
                            outcome = "AGREE"
                        else:
                            outcome, failed_why = "DISAGREE", disagreement(rows)
    except PackMoved as exc:
        outcome, failed_why = "FAILED", str(exc)

    rec = dict(base, outcome=outcome, why=failed_why, files=files, swap_sha256=swap_sha,
               notes_sha256=notes_sha, independence=hits, repro=repro.get("runs", []),
               alice_result=repro.get("result"), comparison=rows,
               run_dir=str(run_dir), scratch=str(scratch), copied=copied,
               left_out=[[n_, w] for n_, w in left_out],
               prompt_bytes=len(prompt.encode("utf-8")), alice=meta)
    return append([rec], ledger)[0]


def label(rec):
    if rec.get("outcome") == "AGREE":
        return "AGREE"
    return f"{rec.get('outcome')} ({rec.get('why')})"


# ---------------------------------------------------------------- regate's S7

def check(name, ledger=None):
    """Does SWAPS.jsonl hold an intact AGREE for this pack's CURRENT manifest? A DISAGREE for
    the same manifest refuses even beside an AGREE (union rule, as P10). Reads only.
    → {"pack", "ok", "pack_ok", "manifest_sha256", "outcome", "why"}."""
    res = {"pack": name, "ok": False, "pack_ok": False, "manifest_sha256": None,
           "outcome": "none", "why": ""}
    rec_pack = pack_record(name)
    if rec_pack is None:
        res["why"] = f"pack {name!r} is not in PACKS.jsonl"
        return res
    changes = pack_changes(name, rec_pack)
    if changes:
        res["why"] = "pack tampered: " + "; ".join(changes[:5])
        return res
    current = rec_pack["manifest_sha256"]
    res.update(pack_ok=True, manifest_sha256=current)
    entries = read_ledger(ledger)
    why = chain_problem(entries)
    if why:
        res["why"] = why
        return res
    swaps = [r for _, _, r in entries if r and r.get("kind") == "swap" and r.get("pack") == name]
    mine = [r for r in swaps if r.get("manifest_sha256") == current]
    disagree = [r for r in mine if r.get("outcome") == "DISAGREE"]
    agree = [r for r in mine if r.get("outcome") == "AGREE"]
    if disagree:
        res.update(outcome=label(disagree[-1]), why=f"line {disagree[-1]['seq']} DISAGREE for "
                                                    f"manifest {current[:12]}")
    elif agree:
        res.update(ok=True, outcome="AGREE", why=f"line {agree[-1]['seq']} AGREE for manifest "
                                                 f"{current[:12]}")
    elif mine:
        res.update(outcome=label(mine[-1]), why=f"line {mine[-1]['seq']} for manifest "
                                                f"{current[:12]}; no AGREE")
    elif swaps:
        old = sorted({str(r.get("manifest_sha256"))[:12] for r in swaps})
        res["why"] = (f"no run for the current manifest {current[:12]}; runs only for "
                      f"{', '.join(old)}")
    else:
        res["why"] = f"no seat-swap run for {name}"
    return res


# -------------------------------------------------------------- nightly line

def seatswap_line(ledger=None):
    """`- seatswap: <n> runs · <a> agree · <d> disagree · chain OK`. Reads only."""
    entries = read_ledger(ledger)
    if not entries:
        return "- seatswap: none yet"
    problems = verify_chain(entries)
    if problems:
        return f"- seatswap: 🔴 chain BROKEN at line {problems[0][0]}"
    runs = [rec for _, _, rec in entries if rec and rec.get("kind") == "swap"]
    count = lambda outcome: sum(r.get("outcome") == outcome for r in runs)
    return (f"- seatswap: {len(runs)} runs · {count('AGREE')} agree · "
            f"{count('DISAGREE')} disagree · chain OK")


# ----------------------------------------------------------------------- CLI

def verb_run(args):
    try:
        rec = run(args.pack)
    except (RuntimeError, Refused) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_REFUSED
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_USAGE
    out = [f"SEATSWAP: {label(rec)}",
           f"pack {rec['pack']} (manifest {rec['manifest_sha256'][:12]})"]
    if "scratch" in rec:
        out.append(f"scratch {rec['scratch']}")
        out.append(f"files: {', '.join(sorted(rec['files'])) or 'none'}")
    for r in rec.get("comparison", []):
        out.append(f"  {'ok ' if r['ok'] else 'BAD'} {r['key']}: alice {_num(r['alice'])}, "
                   f"packed {_num(r['packed'])}, tol {_num(r['tol'])}")
    out.append(f"ledger line {rec['seq']}")
    sys.stdout.write("\n".join(out) + "\n")
    return EXIT_OK


def verb_check(args):
    res = check(args.pack)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    return EXIT_OK if res["ok"] else EXIT_NO


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seatswap.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("run", help="Alice re-analyses this pack; the outcome is recorded")
    p.add_argument("pack")
    p.set_defaults(fn=verb_run)
    p = sub.add_parser("check", help="regate's S7: an intact AGREE for the current manifest?")
    p.add_argument("pack")
    p.set_defaults(fn=verb_check)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
