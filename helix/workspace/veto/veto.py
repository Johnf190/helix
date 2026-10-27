#!/usr/bin/env python3
"""veto.py — Alice's veto (order 48, amendment 9a).

    veto.py run <PREREG.md>      copy the seal into a fresh scratch dir, give Alice the adversarial
                                 brief in her sandbox, reproduce any break she writes, record the
                                 outcome in VETOES.jsonl

Alice (Gemini via `agy`, not Claude) gets execution in a quarantined sandbox only (sandbox.py): one
writable scratch directory under __BULK__/veto/<UTC>/, no ~/helix, no write access to
anything of Helix's. The scratch holds `seal/` (a copy of the seal, read-only to her) and `alice/`
(her cwd). Her brief (order 50: worded as an audit): find whether the seal's checks could report
PASS when its claim is false, and if so, leave a script that shows it. One automatic retry, only
on a refusal (she wrote no output and her reply matches REFUSAL_PATTERNS); both attempts go in the
one ledger line (`attempts`); a second refusal is FAILED (refusal x2).

Order 48b: a break only counts if it is CLEAN, and a cold judge confirms it.
  1. Reproduction. Her `break.sh` (or `break.py`) runs twice in the reproduction sandbox (no
     network, no agy, cleared environment, PYTHONSAFEPATH=1), 120 s each. Each run gets a FRESH
     dir: a PRISTINE copy of the seal, taken again from the seal's own directory (never from her
     scratch), bound READ-ONLY at `seal/`, and a writable `alice/` holding ONLY her output files
     (`break.sh|break.py`, `BREAK.md`, `evidence/**`); cwd = `alice/`. Reproduced = both exit 0,
     both last stdout lines start `BREAK:`, the two stdouts are identical.
  2. Tamper check, by the harness, after each run, over every file in `alice/` (hers or made by
     her script): a seal file's name, a `.py`/`.pyc` named like a standard-library module (or
     inside a package dir so named), or a seal file's sha256 under another name (a renamed copy;
     the empty file is no one's copy); and every seal file still hashes as it did. Any hit →
     BREAK_INVALID (tamper: <names>).
  3. The seal's own checker. A seal MAY declare one line `Instrument: <relative .py path>`: a fast
     design-checker on synthetic or stand-in data, ≤ 120 s, NEVER the real experiment (running the
     real thing before the token defeats pre-registration). The harness runs it itself,
     `python3 -I -B <path>`, cwd = a read-only pristine copy of the seal, no network, and records
     rc, stdout sha256 and last line: the instrument of record. Not declared → `instrument: none`.
  4. A cold judge (`claude -p`, the cold flag set and init-event refusal copied from sealgate's
     review.py, ≤ $1.00, prompt on stdin from a file, ≤ 120,000 bytes) reads the seal, her
     BREAK.md and script, both stdouts, the instrument of record and the tamper report, and
     answers whether the seal AS WRITTEN would pass while its claim is false. Only a reproduced,
     clean break reaches the judge.

Outcomes:  BREAK_CONFIRMED (reproduced, clean, the judge agrees) · BREAK_DISPUTED (reproduced,
clean, the judge disagrees: Alice and the judge disagree, the user decides) · BREAK_INVALID (tamper) ·
BREAK_NOT_REPRODUCED · NO_BREAK · FAILED (<why>) (agy error, time cap, no output, judge error, a
judge that was not cold or gave no JSON). FAILED is never a veto and never a pass. Order 48's
BREAK_REPRODUCED lines stay in the ledger as they are (legacy) and are still read.

VETOES.jsonl beside this file is append-only and hash-chained, the same chain as sealgate's
REVIEWS.jsonl (order 47): each line carries `prev`, the sha256 of the previous line's raw bytes
(genesis: sha256 of nothing), and `seq`, its line number. Known limit: this directory is not behind
the write wall, so the chain makes an edit VISIBLE, not impossible, and the LAST line has no
successor to witness it. Her files stay in the scratch dir; the ledger records the sha256 of each.

`disagreement` is its own finding, with a `disagreement_kind`:
  alice_vs_judge     the outcome is BREAK_DISPUTED.
  alice_vs_reviewer  (order 47's item 9) the outcome is BREAK_CONFIRMED, the seal's assigned
                     reviewer (sealgate/REVIEWS.jsonl) was Claude-cold, and none of that reviewer's
                     findings is dispositioned ACCEPTED or ADDRESSED in the seal's stamped findings
                     file (Claude saw no fatal flaw; Alice broke it). No stamp, no disposition.

sealgate.py's P10_ALICE_VETO reads this ledger. The nightly's `- veto:` line reports it.

There is no flag that replaces, passes over or stands in for Alice or the judge. Tests inject a
fake Alice and a fake judge with monkeypatch.

Exit codes:  0 recorded (any outcome; read it) · 2 usage · 3 refused (broken chain, unreadable seal)
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

HERE = Path(__file__).resolve().parent
HELIX = HERE.parents[1]                       # ~/helix: this file is helix/workspace/veto/
LEDGER = HERE / "VETOES.jsonl"
REVIEWS_LEDGER = HELIX / "workspace" / "sealgate" / "REVIEWS.jsonl"
SEALGATE_PY = HELIX / "workspace" / "sealgate" / "sealgate.py"
SANDBOX_PY = HERE / "sandbox.py"
BULK = Path("__BULK__/veto")       # bulk output lives on the HDD (153_*)
GENESIS = hashlib.sha256(b"").hexdigest()

ALICE_TIMEOUT = 900                           # 15 min, then her process group is killed
REPRO_TIMEOUT = 120                           # per reproduction run
INSTRUMENT_TIMEOUT = 120                      # the seal's own checker, run by the harness
COPY_CAP = 200_000                            # bytes of the seal dir copied for her
PROMPT_CAP = 120_000                          # her prompt is one argv string (131,072)

OUTCOMES = ("BREAK_CONFIRMED", "BREAK_DISPUTED", "BREAK_INVALID", "BREAK_NOT_REPRODUCED",
            "NO_BREAK", "FAILED")
PASSING = ("NO_BREAK", "BREAK_NOT_REPRODUCED", "BREAK_INVALID")
LEGACY = "BREAK_REPRODUCED"                   # order 48's outcome; its lines are still read
BREAK_SCRIPTS = ("break.sh", "break.py")
# Her output names. A seal that ships one is not copied with it: only what SHE wrote counts.
OUTPUT_NAMES = BREAK_SCRIPTS + ("BREAK.md", "NO_BREAK.md")
CREDENTIAL_RE = re.compile(r"credential|accounts|^\.env", re.I)

PATH_TOKEN_RE = re.compile(r"[\w.~/\-]+")
EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
STAMP_BLOCK_BYTES_RE = re.compile(re.escape(b"<!-- SEALGATE-STAMP BEGIN -->") + rb".*?"
                                  + re.escape(b"<!-- SEALGATE-STAMP END -->"), re.S)
INSTRUMENT_RE = re.compile(r"^Instrument:[ \t]*(\S*)[ \t]*$", re.M)

SEAL_DIR = "seal"                             # the layout of every scratch and repro dir
ALICE_DIR = "alice"
EVIDENCE_DIR = "evidence"

# Order 50: worded as an audit, the same task. The order-48b wording ("Make this seal PASS while
# its claim is FALSE") was refused once ("Sorry, I cannot fulfill your request…", VETOES line 3).
BRIEF = (
    "You are auditing a pre-registration before it is used. `../seal/` holds a read-only copy of "
    "it, the seal (a plan for an experiment, frozen BEFORE any data is collected), and the files "
    "it names. The seal is ../seal/{seal}. Read it and the files beside it. The current directory "
    "is yours.\n\n"
    "Your job is to find out whether its checks could report PASS when its claim is actually "
    "false: a band that cannot fail, a missing control, a circular check, a bar below its own "
    "null. If you find such a weakness, demonstrate it with a deterministic script: write "
    "`break.sh` (or `break.py`) in this directory, plus `BREAK.md`: what it shows and why the "
    "seal would still report PASS. The script's last stdout line must be `BREAK: <one line>`. If "
    "you find no such weakness, write `NO_BREAK.md` saying what you checked.\n\n"
    "How your script is checked: it is run twice, each time in a FRESH directory at a DIFFERENT "
    "path that holds ONLY your `break.sh` (or `break.py`), your `BREAK.md` and anything you put "
    "under `evidence/`, beside a pristine read-only copy of the seal at `../seal/`. It runs as "
    "`sh break.sh` (or `python3 break.py`) with no network, no home directory, a cleared "
    "environment (PYTHONSAFEPATH=1, PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, no PYTHONPATH) "
    "and a {repro} s limit. Use relative paths only. It must exit 0, and both runs must print "
    "exactly the same stdout. Put any helper files your script needs under `evidence/`. Write "
    "only inside this directory.\n\n"
    "A demonstration must hold for the seal AS WRITTEN: its pinned files unmodified and its "
    "checker run cleanly. After each run the harness throws it out as tampering if your directory "
    "holds a file named like a seal file, a `.py` file named like a Python standard-library "
    "module, or a copy of a seal file under another name. A cold judge then reads the seal, your "
    "BREAK.md, your script and both outputs: a demonstration that depends on changing the seal's "
    "files, the interpreter, imports or the environment does not count.")

# Order 50: one automatic retry, only on a refusal: she wrote none of OUTPUT_NAMES and her reply
# carries one of these (case-insensitive, curly apostrophes read as straight). Fixed; no flag.
REFUSAL_PATTERNS = ("cannot fulfill", "can't help with", "I'm unable to", "I cannot assist")
ALICE_ATTEMPTS = 2

JUDGE_QUESTION = (
    "Does this show that the seal AS WRITTEN (pinned files unmodified, checker run cleanly) would "
    "PASS while its claim is FALSE? A break that depends on changing the seal's files, the "
    "interpreter, imports or the environment does not count. Answer JSON "
    "{\"valid\": true|false, \"why\": \"...\"}.")

JUDGE_SYSTEM = ("You are a cold judge. You have no tools and no other context. Follow the "
                "instruction at the top of the message exactly.")

JUDGE_INSTRUCTION = (
    "You are a cold judge of an adversarial break of a sealed pre-registration: a plan for an "
    "experiment, written and frozen BEFORE any data is collected. Alice, an adversarial reviewer, "
    "was asked to make the seal PASS while its claim is FALSE, and wrote a script that she says "
    "demonstrates it. The harness ran her script twice, from a directory holding only her own "
    "files, beside a pristine read-only copy of the seal, and checked afterwards that she had not "
    "planted files that shadow the seal or the standard library. If the seal declares its own "
    "checker (its instrument), the harness ran that itself, isolated, on the pristine seal: that "
    "output is the instrument of record. You have no tools; judge only from the text below.\n\n"
    "The question: " + JUDGE_QUESTION + "\n\n"
    "Answer with that JSON object and nothing else.")

# Copied from sealgate/review.py (order 47): the cold flag set and the init-event refusal.
CLI = Path.home() / ".local" / "bin" / "claude"
CONFIG_DIR = Path.home() / "claude" / "config"
JUDGE_CAP_USD = 1.00
JUDGE_TIMEOUT = 900
JUDGE_STDOUT_TAIL = 20_000                    # bytes of each stdout shown to the judge
JUDGE_EVIDENCE_TAIL = 10_000                  # bytes of each evidence file shown to the judge


# --------------------------------------------------------------------- helpers

def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def body_sha256(raw):
    """sealgate's body hash: the seal with its stamp block region removed, raw bytes."""
    return sha256_bytes(STAMP_BLOCK_BYTES_RE.sub(b"", raw))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def seal_key(path):
    """The path relative to ~/helix, or the absolute path for anything outside it (review.py's)."""
    p = Path(os.path.abspath(path)).resolve()
    try:
        return str(p.relative_to(HELIX.resolve()))
    except ValueError:
        return str(p)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_sandbox = None


def sandbox_module():
    """sandbox.py, imported from its file beside this one."""
    global _sandbox
    if _sandbox is None:
        _sandbox = load_module("veto_sandbox", SANDBOX_PY)
    return _sandbox


# ---------------------------------------------------------------------- ledger
# Copied from sealgate/review.py (order 47): the same chain, the same record envelope.

def read_ledger(ledger=None):
    """[(line number, raw line, record or None)]. Raw bytes are kept: the chain is over them."""
    path = Path(ledger or LEDGER)
    if not path.exists():
        return []
    out = []
    for n, raw in enumerate(path.read_bytes().decode("utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
            out.append((n, raw, rec if isinstance(rec, dict) else None))
        except ValueError:
            out.append((n, raw, None))
    return out


def verify_chain(entries):
    """[(line number, problem)] — empty if every line is JSON, numbered in order, and carries
    the hash of the line before it. Stops at the first break."""
    prev, seq = GENESIS, 0
    for n, raw, rec in entries:
        seq += 1
        if rec is None:
            return [(n, f"line {n} is not a JSON record")]
        if rec.get("prev") != prev:
            return [(n, f"line {n} does not carry the hash of the line before it (expected "
                        f"{prev[:12]}, says {str(rec.get('prev'))[:12]}): a line was edited, "
                        f"removed or inserted")]
        if rec.get("seq") != seq:
            return [(n, f"line {n} has seq {rec.get('seq')!r}, expected {seq}")]
        prev = sha256_bytes(raw.encode("utf-8"))
    return []


def append(records, ledger=None):
    """Append, never rewrite. Each line carries seq and the hash of the line before it."""
    path = Path(ledger or LEDGER)
    entries = read_ledger(path)
    prev = sha256_bytes(entries[-1][1].encode("utf-8")) if entries else GENESIS
    seq = len(entries)
    out, written = [], []
    for rec in records:
        seq += 1
        rec = dict(rec, seq=seq, prev=prev, ts=utc_now(), veto=VERSION)
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out.append(raw)
        written.append(rec)
        prev = sha256_bytes(raw.encode("utf-8"))
    needs_nl = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n" if needs_nl else "") + "".join(l + "\n" for l in out))
    return written


def chain_refusal(entries):
    problems = verify_chain(entries)
    if problems:
        return f"VETOES.jsonl chain is broken at line {problems[0][0]}: {problems[0][1]}"
    return None


# ------------------------------------------------------------ the seal's copy

def is_text(raw):
    try:
        return "\x00" not in raw.decode("utf-8")
    except ValueError:
        return False


def named_under(seal_text, root):
    """Files the seal names by a relative path that resolves under its own directory."""
    out = set()
    for tok in PATH_TOKEN_RE.findall(seal_text):
        tok = tok.rstrip(".,;:-")
        if not EXT_RE.search(tok) or tok.startswith(("/", "~")):
            continue
        p = root / tok
        if p.is_file():
            real = p.resolve()
            out.add(str(real.relative_to(root)) if root in real.parents else os.path.normpath(tok))
    return out


def copy_seal(prereg, scratch, cap=None):
    """Copy the seal, the text files beside it and the files it names under its own directory
    into `scratch`. Text only, ≤ cap bytes, credential-named files never copied, nothing that
    resolves outside the seal's directory. → (copied names, [(name, why)] left out)."""
    cap = COPY_CAP if cap is None else cap
    prereg = Path(prereg).resolve()
    root = prereg.parent
    seal_name = prereg.name
    names = [seal_name]
    names += sorted(p.name for p in root.iterdir() if p.is_file() and p.name != seal_name)
    names += sorted(named_under(prereg.read_bytes().decode("utf-8", errors="replace"), root)
                    - set(names))
    copied, left_out, total = [], [], 0
    for name in names:
        src = root / name
        if any(CREDENTIAL_RE.search(part) for part in Path(name).parts):
            left_out.append((name, "credential-named, never copied"))
            continue
        if name in OUTPUT_NAMES:
            left_out.append((name, "a veto output name; only Alice writes those"))
            continue
        real = src.resolve()
        if src.is_symlink() or root not in real.parents:
            left_out.append((name, "outside the seal's directory"))
            continue
        raw = real.read_bytes()
        if not is_text(raw):
            left_out.append((name, "not text"))
            continue
        if total + len(raw) > cap:
            if name == seal_name:
                raise ValueError(f"the seal alone is over the {cap:,}-byte copy cap")
            left_out.append((name, f"over the {cap // 1000} KB cap"))
            continue
        dest = Path(scratch) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        copied.append(name)
        total += len(raw)
    return copied, left_out


def tree_hashes(root):
    """{relative path: sha256} of every regular file under root (symlinks are listed as such)."""
    root = Path(root)
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = str(p.relative_to(root))
            out[rel] = "symlink" if p.is_symlink() else sha256_bytes(p.read_bytes())
    return out


def content_sha(p):
    """sha256 of what reading `p` gives (a symlink is followed), or None if it is not a file."""
    try:
        return sha256_bytes(Path(p).read_bytes()) if Path(p).is_file() else None
    except OSError:
        return None


def build_prompt(seal_name):
    prompt = BRIEF.format(seal=seal_name, repro=REPRO_TIMEOUT)
    if len(prompt.encode("utf-8")) >= PROMPT_CAP:
        raise ValueError(f"the prompt is over the {PROMPT_CAP:,}-byte cap")
    return prompt


# ------------------------------------------------------------------- Alice

class AliceFailed(Exception):
    """agy errored, timed out or said nothing usable: the run is FAILED, never a veto."""


def call_alice(workdir, prompt):
    """Alice in her sandbox: the whole scratch bound, cwd = its alice/, its seal/ read-only.
    → meta dict; raises AliceFailed."""
    sandbox = sandbox_module()
    workdir = Path(workdir)
    rc, out, err, seconds = sandbox.run_alice(workdir.parent, prompt, ALICE_TIMEOUT,
                                              cwd=workdir, read_only=[workdir.parent / SEAL_DIR])
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


def is_refusal(text):
    text = str(text or "").replace("\u2019", "'").lower()
    return any(p.lower() in text for p in REFUSAL_PATTERNS)


def wrote_nothing(workdir):
    """None of her output names exists in her directory."""
    return not any((Path(workdir) / n).exists() for n in OUTPUT_NAMES)


def her_outputs(her, dest):
    """Copy ONLY her output files from `her` into `dest`: the break script(s), BREAK.md and
    regular files under evidence/ (symlinks are not copied). → copied relative paths."""
    her, dest = Path(her), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in BREAK_SCRIPTS + ("BREAK.md",):
        src = her / name
        if src.is_file() and not src.is_symlink():
            shutil.copyfile(src, dest / name)
            copied.append(name)
    ev = her / EVIDENCE_DIR
    if ev.is_dir() and not ev.is_symlink():
        for dirpath, dirs, files in os.walk(ev):
            dirs.sort()
            for f in sorted(files):
                src = Path(dirpath) / f
                if src.is_symlink() or not src.is_file():
                    continue
                rel = src.relative_to(her)
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest / rel)
                copied.append(str(rel))
    return copied


def module_names(rel):
    """The top-level module names a .py/.pyc file could be imported as: its own stem and every
    directory above it (a package)."""
    parts = Path(rel).parts
    if not parts or not parts[-1].endswith((".py", ".pyc")):
        return set()
    return {parts[-1].split(".")[0]} | set(parts[:-1])


def tamper_report(seal_dir, alice_dir, pristine):
    """[{file, reason}] for the files now in alice_dir, and for the seal against its pristine
    hashes. Empty = clean."""
    seal_dir, alice_dir = Path(seal_dir), Path(alice_dir)
    names = {Path(rel).name: rel for rel in pristine}
    shas = {digest: rel for rel, digest in pristine.items() if digest not in (GENESIS, "symlink")}
    out = []
    for dirpath, dirs, files in os.walk(alice_dir):
        dirs.sort()
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = str(p.relative_to(alice_dir))
            stdlib = sorted(m for m in module_names(rel) if m in sys.stdlib_module_names)
            digest = content_sha(p)
            if f in names:
                out.append({"file": rel, "reason": f"named like the seal file {names[f]}"})
            elif stdlib:
                out.append({"file": rel, "reason": "named like a standard-library module: "
                                                   + ", ".join(stdlib)})
            elif digest in shas:
                out.append({"file": rel, "reason": f"a copy of a seal file: {shas[digest]}"})
    now = tree_hashes(seal_dir)
    for rel in sorted(set(pristine) | set(now)):
        if rel not in now:
            out.append({"file": f"{SEAL_DIR}/{rel}", "reason": "a seal file is gone"})
        elif rel not in pristine:
            out.append({"file": f"{SEAL_DIR}/{rel}",
                        "reason": "a file appeared in the seal during the run"})
        elif now[rel] != pristine[rel]:
            out.append({"file": f"{SEAL_DIR}/{rel}", "reason": "a seal file changed during the run"})
    return out


class SealMoved(Exception):
    """The seal's directory no longer gives the copy Alice was given: the run is FAILED."""


def pristine_seal(prereg, dest, expected=None):
    """A fresh copy of the seal from its OWN directory (never from her scratch) → its hashes.
    If `expected` is given, the copy must hash exactly so."""
    Path(dest).mkdir(parents=True)
    copy_seal(prereg, dest)
    hashes = tree_hashes(dest)
    if expected is not None and hashes != expected:
        raise SealMoved("the seal's directory changed during the run: the pristine copy no "
                        "longer matches the copy Alice was given")
    return hashes


def reproduce(prereg, her, run_dir, script, expected=None):
    """Run her script twice in the reproduction sandbox. Each run: a fresh dir with a pristine
    read-only seal/ and an alice/ holding only her output files, cwd = alice/. The tamper check
    runs after each. → {"reproduced", "runs", "stdout", "tamper", "copied"}."""
    sandbox = sandbox_module()
    runs, outs, tamper, copied = [], [], [], []
    for i in (1, 2):
        fresh = Path(run_dir) / f"repro{i}"
        seal_dir, alice_dir = fresh / SEAL_DIR, fresh / ALICE_DIR
        pristine = pristine_seal(prereg, seal_dir, expected)
        copied = her_outputs(her, alice_dir)
        cmd = ["sh", script] if script.endswith(".sh") else ["python3", script]
        rc, out, err, seconds = sandbox.run_reproduction(fresh, cmd, REPRO_TIMEOUT,
                                                         cwd=alice_dir, read_only=[seal_dir])
        (Path(run_dir) / f"repro{i}.stdout.txt").write_text(out, encoding="utf-8")
        lines = out.strip().splitlines()
        last = lines[-1] if lines else ""
        runs.append({"dir": str(fresh), "rc": rc, "last_line": last[:300],
                     "stdout_sha256": sha256_bytes(out.encode("utf-8")), "seconds": seconds,
                     "stderr_tail": err.strip()[-300:]})
        outs.append(out)
        tamper += [dict(t, run=i) for t in tamper_report(seal_dir, alice_dir, pristine)]
    reproduced = (all(r["rc"] == 0 for r in runs)
                  and all(r["last_line"].startswith("BREAK:") for r in runs)
                  and outs[0] == outs[1])
    return {"reproduced": reproduced, "runs": runs, "stdout": outs, "tamper": tamper,
            "copied": copied}


def tamper_names(tamper):
    seen = []
    for t in tamper:
        if t["file"] not in seen:
            seen.append(t["file"])
    return ", ".join(seen)


# ------------------------------------------------------ the seal's instrument

def instrument_of(seal_text):
    """(relative path or None, problem or None) from the seal's one `Instrument:` line."""
    found = INSTRUMENT_RE.findall(seal_text)
    if not found:
        return None, None
    if len(found) > 1:
        return found[0], "more than one Instrument line"
    rel = found[0]
    if not rel.endswith(".py"):
        return rel, "the instrument is not a .py"
    if rel.startswith(("/", "~")) or ".." in Path(rel).parts:
        return rel, "the instrument is not a relative path inside the seal's directory"
    return rel, None


def run_instrument(prereg, run_dir, expected=None):
    """The seal's declared checker, run by the harness: `python3 -I -B <path>`, cwd = a pristine
    read-only copy of the seal, no network, INSTRUMENT_TIMEOUT. → "none" or a record."""
    rel, problem = instrument_of(Path(prereg).read_bytes().decode("utf-8", errors="replace"))
    if rel is None:
        return "none"
    if problem:
        return {"path": rel, "error": problem}
    root = Path(run_dir) / "instrument"
    seal_dir = root / SEAL_DIR
    pristine = pristine_seal(prereg, seal_dir, expected)
    if rel not in pristine:
        return {"path": rel, "error": f"{rel} is not in the seal's copy"}
    argv = ["python3", "-I", "-B", rel]
    rc, out, err, seconds = sandbox_module().run_reproduction(
        root, argv, INSTRUMENT_TIMEOUT, cwd=seal_dir, read_only=[seal_dir])
    stdout_file = root / "stdout.txt"
    stdout_file.write_text(out, encoding="utf-8")
    lines = out.strip().splitlines()
    return {"path": rel, "argv": argv, "rc": rc, "seconds": seconds,
            "stdout_sha256": sha256_bytes(out.encode("utf-8")),
            "last_line": (lines[-1] if lines else "")[:300], "stdout_file": str(stdout_file),
            "stderr_tail": err.strip()[-300:]}


# ------------------------------------------------------------------ the judge

class JudgeFailed(Exception):
    """The judge errored, timed out, was not cold or gave no verdict: the run is FAILED."""


def judge_command(system, prompt, workdir):
    """→ (argv, stdin file, cwd, env). review.py's cold flag set, capped at JUDGE_CAP_USD; the
    prompt is written to a regular file and goes in on stdin, never argv."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stdin_path = workdir / "prompt.txt"
    stdin_path.write_text(prompt, encoding="utf-8")
    cwd = workdir / "cwd"
    cwd.mkdir(exist_ok=True)
    argv = [str(CLI), "-p",
            "--model", "opus",
            "--tools", "",
            "--system-prompt", system,
            "--setting-sources", "",
            "--strict-mcp-config", "--disable-slash-commands", "--safe-mode",
            "--no-session-persistence",
            "--max-budget-usd", f"{JUDGE_CAP_USD:.2f}",
            "--output-format", "stream-json", "--verbose"]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(CONFIG_DIR)
    env.pop("ANTHROPIC_API_KEY", None)
    return argv, stdin_path, cwd, env


def check_cold(init):
    """→ the list of things a cold session must not have (empty = cold). From review.py."""
    if not isinstance(init, dict):
        return ["no init event"]
    return [f"{k}: {init.get(k)!r}" for k in ("tools", "mcp_servers", "skills", "memory_paths")
            if init.get(k)]


def init_record(init):
    if not isinstance(init, dict):
        return None
    plugins = [p.get("name") if isinstance(p, dict) else p for p in init.get("plugins") or []]
    return {"tools": init.get("tools"), "mcp_servers": init.get("mcp_servers"),
            "skills": init.get("skills"), "memory_paths": init.get("memory_paths"),
            "plugins": plugins, "model": init.get("model"),
            "version": init.get("claude_code_version")}


def judge_session(system, prompt, workdir):
    """One real cold session, read as it streams: a warm init event kills it before the model
    is asked anything. From review.py. → {"text", "cost_usd", "init"[, "error"]}."""
    argv, stdin_path, cwd, env = judge_command(system, prompt, workdir)
    err_path = Path(workdir) / "stderr.txt"
    init, result = None, None
    with open(stdin_path, "rb") as fin, open(err_path, "wb") as ferr:
        proc = subprocess.Popen(argv, stdin=fin, stdout=subprocess.PIPE, stderr=ferr, cwd=cwd,
                                env=env, text=True, encoding="utf-8")
        deadline = time.monotonic() + JUDGE_TIMEOUT
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init" and init is None:
                    init = ev
                    if check_cold(init):
                        proc.kill()
                        break
                elif ev.get("type") == "result":
                    result = ev
                if time.monotonic() > deadline:
                    proc.kill()
                    break
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    out = {"init": init, "cost_usd": (result or {}).get("total_cost_usd"), "rc": proc.returncode}
    if check_cold(init):
        out["text"] = ""
        return out
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        why = (result or {}).get("subtype") or f"no result event (rc {proc.returncode})"
        tail = err_path.read_text(errors="replace").strip()[-200:]
        out["error"] = f"{why}{': ' + tail if tail else ''}"
        out["text"] = (result or {}).get("result") or ""
        return out
    out["text"] = result.get("result") or ""
    return out


def call_judge(prompt, workdir):
    """The real judge. → {"text", "meta"}; raises JudgeFailed."""
    out = judge_session(JUDGE_SYSTEM, prompt, workdir)
    meta = {"model": "opus", "via": "claude -p (cold)", "cost_usd": out.get("cost_usd"),
            "init": init_record(out.get("init"))}
    warm = check_cold(out.get("init"))
    if warm:
        raise JudgeFailed("NOT COLD: " + "; ".join(warm))
    if out.get("error"):
        raise JudgeFailed(f"the cold session failed: {out['error']}"[:400])
    return {"text": out.get("text") or "", "meta": meta}


JUDGE = call_judge

JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.S)


def parse_verdict(text):
    """(valid, why) from the judge's reply, or None if it holds no {"valid": bool, "why": str}."""
    for m in reversed(JSON_OBJECT_RE.findall(text or "")):
        try:
            obj = json.loads(m)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("valid"), bool) \
                and isinstance(obj.get("why"), str):
            return obj["valid"], obj["why"]
    return None


def tail(text, cap):
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return (f"[... first {len(raw) - cap:,} bytes cut; the last {cap:,} follow ...]\n"
            + raw[-cap:].decode("utf-8", errors="replace"))


def judge_prompt(seal_name, seal_text, her, script, repro, instrument):
    """The judge's whole input, as one text. Raises ValueError over PROMPT_CAP."""
    her = Path(her)
    parts = [JUDGE_INSTRUCTION, f"=== THE SEAL ({seal_name}), as written ===\n{seal_text}"]
    md = her / "BREAK.md"
    parts.append("=== HER BREAK.md ===\n" + (md.read_text(encoding="utf-8", errors="replace")
                                              if md.is_file() else "(she wrote none)"))
    parts.append(f"=== HER SCRIPT ({script}) ===\n"
                 + (her / script).read_text(encoding="utf-8", errors="replace"))
    for rel in repro["copied"]:
        if rel.startswith(EVIDENCE_DIR + "/"):
            parts.append(f"=== HER EVIDENCE FILE {rel} ===\n" + tail(
                (her / rel).read_text(encoding="utf-8", errors="replace"), JUDGE_EVIDENCE_TAIL))
    for r, out in zip(repro["runs"], repro["stdout"]):
        n = repro["runs"].index(r) + 1
        parts.append(f"=== REPRODUCTION RUN {n}: exit {r['rc']}, stdout ===\n"
                     + tail(out, JUDGE_STDOUT_TAIL))
    if instrument == "none":
        parts.append("=== INSTRUMENT OF RECORD ===\ninstrument: none (the seal declares no "
                     "checker; judge from her demonstration alone)")
    elif "error" in instrument:
        parts.append(f"=== INSTRUMENT OF RECORD ===\nthe seal declares `Instrument: "
                     f"{instrument['path']}` but the harness could not run it: "
                     f"{instrument['error']}")
    else:
        out = Path(instrument["stdout_file"]).read_text(encoding="utf-8", errors="replace")
        parts.append(f"=== INSTRUMENT OF RECORD: `{' '.join(instrument['argv'])}`, run by the "
                     f"harness on the pristine read-only seal, no network; exit "
                     f"{instrument['rc']}, stdout ===\n" + tail(out, JUDGE_STDOUT_TAIL))
    parts.append("=== TAMPER REPORT (the harness, after each run) ===\n"
                 + ("none: no file in her directory is named like a seal file or a "
                    "standard-library module or is a copy of a seal file; every seal file "
                    "hashes as before" if not repro["tamper"] else
                    "\n".join(f"run {t['run']}: {t['file']}: {t['reason']}"
                              for t in repro["tamper"])))
    parts.append("=== THE QUESTION ===\n" + JUDGE_QUESTION)
    prompt = "\n\n".join(parts) + "\n"
    if len(prompt.encode("utf-8")) > PROMPT_CAP:
        raise ValueError(f"the prompt is over the {PROMPT_CAP:,}-byte cap "
                         f"({len(prompt.encode('utf-8')):,} bytes)")
    return prompt


# ---------------------------------------------------------- the disagreement

def reviewer_of(key, reviews_ledger=None):
    """(assigned reviewer or None, that reviewer's finding ids) from sealgate/REVIEWS.jsonl."""
    entries = read_ledger(reviews_ledger or REVIEWS_LEDGER)
    assigned = None
    for _, _, rec in entries:
        if rec and rec.get("kind") == "assign" and rec.get("seal") == key:
            assigned = rec.get("reviewer")
            break
    ids = set()
    for _, _, rec in entries:
        if (rec and rec.get("kind") == "review" and rec.get("seal") == key
                and rec.get("reviewer") == assigned):
            ids |= set(rec.get("findings") or [])
    return assigned, sorted(ids, key=lambda f: int(f[1:]) if f[1:].isdigit() else 0)


def dispositions_of(prereg):
    """{finding id: ADDRESSED|ACCEPTED|DECLINED} from the seal's stamped findings file, read with
    sealgate's own P1 and P3. {} when the seal has no stamp or its findings do not parse."""
    try:
        sg = load_module("veto_sealgate", SEALGATE_PY)
        raw = Path(prereg).read_bytes()
        _, stamp = sg.check_p1(raw.decode("utf-8", errors="replace"), Path(prereg).parent)
        _, dispositions = sg.check_p3(stamp)
    except Exception:
        return {}
    return dispositions or {}


def disagreement_of(outcome, reviewer, findings, dispositions):
    """→ the disagreement kind, or None."""
    if outcome == "BREAK_DISPUTED":
        return "alice_vs_judge"
    if outcome != "BREAK_CONFIRMED" or reviewer != "Claude-cold":
        return None
    if any(dispositions.get(f) in ("ACCEPTED", "ADDRESSED") for f in findings):
        return None
    return "alice_vs_reviewer"


# ------------------------------------------------------------------------ run

def run(prereg, ledger=None, bulk=None, reviews_ledger=None):
    """One veto run. Appends exactly one line. → the record."""
    prereg = Path(prereg)
    entries = read_ledger(ledger)
    why = chain_refusal(entries)
    if why:
        raise RuntimeError(why)
    raw_seal = prereg.read_bytes()
    key = seal_key(prereg)
    run_dir = Path(bulk or BULK) / utc_stamp()
    n = 1
    while run_dir.exists():
        n += 1
        run_dir = run_dir.with_name(f"{run_dir.name.split('.')[0]}.{n}")
    scratch = run_dir / "scratch"
    workdir = scratch / ALICE_DIR
    workdir.mkdir(parents=True)
    copied, left_out = copy_seal(prereg, scratch / SEAL_DIR)
    seal_given = tree_hashes(scratch / SEAL_DIR)
    before = tree_hashes(scratch)
    prompt = build_prompt(prereg.name)
    started = utc_now()

    outcome, failed_why, meta, attempts = None, None, {}, []
    for n_ in range(1, ALICE_ATTEMPTS + 1):
        meta = {}
        try:
            meta = ALICE(workdir, prompt) or {}
        except AliceFailed as exc:
            outcome, failed_why = "FAILED", str(exc)
        except Exception as exc:                  # anything else is a failed run, not a pass
            outcome, failed_why = "FAILED", f"{type(exc).__name__}: {exc}"[:300]
        refused = outcome is None and wrote_nothing(workdir) \
            and is_refusal(meta.get("response_tail"))
        attempts.append({"n": n_, "refusal": refused, "why": failed_why, "alice": meta})
        if not refused:                           # anything but a refusal: no retry
            break
    else:
        outcome, failed_why = "FAILED", "refusal x2"

    # Step 2: everything she wrote, hashed before anything else runs.
    after = tree_hashes(scratch)
    files = {k: v for k, v in after.items() if before.get(k) != v}

    def hers(name):
        return f"{ALICE_DIR}/{name}" in files and (workdir / name).is_file()

    repro, tamper, instrument, verdict, judge_bytes = {}, [], "none", None, None
    script = next((s_ for s_ in BREAK_SCRIPTS if hers(s_)), None)
    try:
        if outcome is None:
            instrument = run_instrument(prereg, run_dir, seal_given)
            if script:
                repro = reproduce(prereg, workdir, run_dir, script, seal_given)
                tamper = repro["tamper"]
                if tamper:
                    outcome, failed_why = "BREAK_INVALID", "tamper: " + tamper_names(tamper)
                elif not repro["reproduced"]:
                    outcome = "BREAK_NOT_REPRODUCED"
                else:
                    outcome, failed_why, verdict, judge_bytes = judged(
                        prereg, raw_seal, workdir, script, repro, instrument, run_dir)
            elif hers("NO_BREAK.md"):
                outcome = "NO_BREAK"
            else:
                outcome, failed_why = "FAILED", "no output: no break script and no NO_BREAK.md"
    except SealMoved as exc:
        outcome, failed_why = "FAILED", str(exc)

    reviewer, findings = reviewer_of(key, reviews_ledger)
    dispositions = dispositions_of(prereg) if outcome == "BREAK_CONFIRMED" else {}
    accepted = sorted(f for f in findings if dispositions.get(f) in ("ACCEPTED", "ADDRESSED"))
    kind = disagreement_of(outcome, reviewer, findings, dispositions)
    rec = {"kind": "veto", "seal": key, "body_sha256": body_sha256(raw_seal),
           "file_sha256": sha256_bytes(raw_seal), "outcome": outcome, "why": failed_why,
           "files": files, "script": script, "repro": repro.get("runs", []),
           "repro_copied": repro.get("copied", []), "tamper": tamper,
           "instrument": instrument, "judge": verdict, "judge_prompt_bytes": judge_bytes,
           "scratch": str(scratch), "copied": copied, "left_out": [[n_, w] for n_, w in left_out],
           "prompt_bytes": len(prompt.encode("utf-8")), "alice": meta, "attempts": attempts,
           "started": started,
           "reviewer": reviewer, "reviewer_findings": findings,
           "reviewer_findings_accepted": accepted,
           "disagreement": kind is not None, "disagreement_kind": kind}
    return append([rec], ledger)[0]


def judged(prereg, raw_seal, her, script, repro, instrument, run_dir):
    """The cold judge on a reproduced, clean break.
    → (outcome, why, judge record or None, prompt bytes or None)."""
    try:
        prompt = judge_prompt(Path(prereg).name, raw_seal.decode("utf-8", errors="replace"),
                              her, script, repro, instrument)
    except ValueError as exc:
        return "FAILED", f"judge: {exc}", None, None
    size = len(prompt.encode("utf-8"))
    workdir = Path(run_dir) / "judge"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
    try:
        reply = JUDGE(prompt, workdir)
    except JudgeFailed as exc:
        return "FAILED", f"judge: {exc}"[:400], None, size
    except Exception as exc:                      # any other judge error is FAILED, not a verdict
        return "FAILED", f"judge: {type(exc).__name__}: {exc}"[:400], None, size
    text = (reply or {}).get("text") or ""
    (workdir / "reply.txt").write_text(text, encoding="utf-8")
    verdict = parse_verdict(text)
    if verdict is None:
        return "FAILED", f"judge: no JSON verdict in its reply ({text.strip()[:200]!r})", \
            {"text": text[:2000], "meta": (reply or {}).get("meta")}, size
    valid, why = verdict
    rec = {"valid": valid, "why": why, "meta": (reply or {}).get("meta")}
    return ("BREAK_CONFIRMED" if valid else "BREAK_DISPUTED"), None, rec, size


def label(rec):
    if rec.get("outcome") in ("FAILED", "BREAK_INVALID"):
        return f"{rec.get('outcome')} ({rec.get('why')})"
    return rec.get("outcome")


# -------------------------------------------------------------- nightly line

def veto_line(ledger=None):
    """`- veto: <n> runs · <c> confirmed · <d> disputed · <i> invalid · chain OK`, with
    `· <r> legacy` before the chain when order 48's BREAK_REPRODUCED lines are present.
    Reads only."""
    entries = read_ledger(ledger)
    if not entries:
        return "- veto: none yet"
    runs = [rec for _, _, rec in entries if rec and rec.get("kind") == "veto"]
    count = lambda outcome: sum(r.get("outcome") == outcome for r in runs)
    legacy = count(LEGACY)
    problems = verify_chain(entries)
    chain = f"🔴 chain BROKEN at line {problems[0][0]}" if problems else "chain OK"
    return (f"- veto: {len(runs)} runs · {count('BREAK_CONFIRMED')} confirmed · "
            f"{count('BREAK_DISPUTED')} disputed · {count('BREAK_INVALID')} invalid · "
            + (f"{legacy} legacy · " if legacy else "") + chain)


# ----------------------------------------------------------------------- CLI

def instrument_label(ins):
    if ins == "none" or not isinstance(ins, dict):
        return "none"
    if "error" in ins:
        return f"{ins['path']}: {ins['error']}"
    return f"{ins['path']} rc {ins['rc']}, last line {ins['last_line']!r}"


def judge_label(verdict):
    if not verdict:
        return "not asked"
    if "valid" not in verdict:
        return "no verdict"
    return f"valid={str(verdict['valid']).lower()}: {verdict['why']}"


def verb_run(args):
    try:
        rec = run(args.prereg)
    except RuntimeError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_REFUSED
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_USAGE
    sys.stdout.write(f"VETO: {label(rec)}\n"
                     f"seal {rec['seal']} (body {rec['body_sha256'][:12]})\n"
                     f"scratch {rec['scratch']}\n"
                     f"files: {', '.join(sorted(rec['files'])) or 'none'}\n"
                     f"instrument: {instrument_label(rec['instrument'])}\n"
                     f"judge: {judge_label(rec['judge'])}\n"
                     f"reviewer {rec['reviewer'] or 'none assigned'}, disagreement "
                     f"{str(rec['disagreement']).lower()}"
                     f"{' (' + rec['disagreement_kind'] + ')' if rec['disagreement_kind'] else ''}"
                     f", ledger line {rec['seq']}\n")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="veto.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("run", help="Alice tries to break this seal; the outcome is recorded")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_run)
    args = ap.parse_args(argv)
    if not Path(args.prereg).is_file():
        sys.stderr.write(f"no such pre-registration: {args.prereg}\n")
        return EXIT_USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
