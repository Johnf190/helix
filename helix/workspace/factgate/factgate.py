#!/usr/bin/env python3
"""factgate.py — detection for the fact base.

`~/helix/memory/facts/` has no git and no single write path: `/remember` is a minor one,
and nearly every file there is written by Odin through the SDK's Write/Edit/Bash tools.
So a fact can be reworded, back-dated or deleted and nothing notices. This records what
is there, in an append-only hash-chained ledger, and reports what moved.

    factgate.py baseline <facts_dir>                      record every file, once
    factgate.py check    <facts_dir>                       exit 0 clean / 4 findings
    factgate.py record   <facts_dir> <file> --reason "…"   acknowledge ONE new or changed file
    factgate.py record-delete <facts_dir> <file> --reason "…"

    ORDER 22b — INTERIM by default (a label, never a block):
    factgate.py baseline-legacy <facts_dir> --cutoff-line N --cutoff-sha256 H --vault-seq S
    factgate.py interim  <facts_dir> [--json]                 report; always exit 0
    factgate.py release  <facts_dir> <file> --witness KIND:REF --reason "…"

    ORDER 35b — archive, never delete (the user runs these; Odin's shell is refused them):
    factgate.py archive  <file>... --why "…"   memory/facts/ -> memory/archive/
    factgate.py restore  <file> --why "…"      memory/archive/ -> memory/facts/

🔴 **Never writes to <facts_dir>** — except `archive` and `restore`, which move one recorded
fact between memory/facts/ and memory/archive/ (same bytes, one ledger line each) and which
the user runs. No other verb writes a file, renames one or touches an mtime there. The ledger
lives beside this program, in `state/`, because `memory/**` is read-only and anything
under `memory/facts/` is loaded into Odin's context.

**There is no bulk record.** One file, one reason, one line. A single command that
blesses every change is a rubber stamp, and a rubber stamp is what this replaces.

Hashes are over RAW BYTES: a touched file is unchanged, a CRLF conversion is a change.

Exit codes:  0 clean · 2 usage / unreadable input · 3 refused · 4 findings
"""

import argparse
import getpass
import importlib.util
import re
import subprocess
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

VERSION = "1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_CHECK = 4

LEDGER_NAME = "FACTS_LEDGER.jsonl"
DEFAULT_STATE = Path(__file__).resolve().parent / "state"
GENESIS = hashlib.sha256(b"").hexdigest()

RECORD_PRECONDITIONS = ["F1_LEDGER_INTACT", "F2_FILE_PRESENT", "F3_SOMETHING_CHANGED",
                        "F4_REASON_IN_WRITING", "F5_CATEGORY_DECLARED"]

# ORDER 22b. Every fact that is not legacy is INTERIM — Odin's account — until a witness its
# author did not write releases it at its current sha256.
LEGACY_NAME = "LEGACY_BASELINE.json"
RELEASE_PRECONDITIONS = ["R1_LEDGER_INTACT", "R2_FACT_ON_LEDGER", "R3_FACT_IS_INTERIM",
                         "R4_WITNESS_CHECKED", "R5_REASON_IN_WRITING"]
# Case-insensitive, whole word, anywhere in the fact; backticks and quotes do not hide them.
VERDICT_WORDS = ("proven", "proves", "decisive", "passed", "pass", "confirmed", "settled",
                 "definitive", "conclusive", "breakthrough", "solved", "novel", "established",
                 "validated")
VERDICT_EMOJI = "🟢"                     # counts in the first line only
_VERDICT_RE = re.compile(r"(?<![A-Za-z0-9_])(?=[" + "".join(sorted({w[0] for w in VERDICT_WORDS}))
                         + r"])(" + "|".join(VERDICT_WORDS) + r")(?![A-Za-z0-9_])", re.I)
# Witnesses are looked up from where this program lives, never from a flag: helix/ is two
# levels up. There is no `user:` kind — the user's word typed by Odin is Odin's testimony.
HELIX = Path(__file__).resolve().parents[2]
WITNESS_KINDS = ("regate", "seal", "review", "report")
REPORT_ENDINGS = ("DONE", "READY FOR /restart", "READY FOR USER'S INSTALL")
ODIN_RE = re.compile(r"(?<![a-z])odin(?![a-z])", re.I)

# ORDER 35b. Like the witnesses, every path is found from where this program lives, never
# from a flag: the fact base, the archive beside it, the pin list, the registry.
FACTS_DIR = HELIX / "memory/facts"
ARCHIVE_DIR = HELIX / "memory/archive"
PINNED_TXT = HELIX / "memory/PINNED.txt"
REGISTRY_JSON = HELIX / "workspace/seam/registry/registry.json"
ARCHIVE_PRECONDITIONS = ["A1_LEDGER_INTACT", "A2_IN_MEMORY_FACTS", "A3_NOT_PINNED",
                         "A4_NOT_INTERIM", "A5_NO_OPEN_ENTRY", "A6_ARCHIVE_SLOT_FREE",
                         "A7_WHY_IN_WRITING"]
RESTORE_PRECONDITIONS = ["B1_LEDGER_INTACT", "B2_ARCHIVED_ON_LEDGER", "B3_FACTS_SLOT_FREE",
                         "B4_WHY_IN_WRITING"]
_NN_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])(\d+_)")      # app.py's, for the registry rule
# ORDER 76. A NEW fact must declare its category; the names live in CATEGORIES.txt beside it.
CATEGORIES_PY = HELIX / "workspace/categories/categories.py"


# --------------------------------------------------------------------- basics

def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(path):
    return sha256_bytes(Path(path).read_bytes())


def canonical(rec):
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def die_usage(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(EXIT_USAGE)


def ledger_path(state_dir):
    return Path(state_dir) / LEDGER_NAME


def listing(facts_dir):
    """(regular files by name, subdirectory names). Not recursive, on purpose:
    a fact base is flat, and a directory appearing in it is itself worth reporting."""
    files, dirs = {}, []
    for p in sorted(Path(facts_dir).iterdir()):
        if p.is_dir():
            dirs.append(p.name)
        elif p.is_file():
            files[p.name] = p
    return files, dirs


def read_ledger(state_dir):
    """[(line number, raw text, record)] — raw kept, because the chain is over it."""
    path = ledger_path(state_dir)
    if not path.exists():
        return []
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            out.append((n, raw, json.loads(raw)))
        except ValueError:
            out.append((n, raw, None))
    return out


def verify_chain(entries):
    """Every line intact and linked to the one before it."""
    problems, prev = [], GENESIS
    for n, raw, rec in entries:
        if rec is None:
            return [f"LEDGER TAMPERED: line {n} is not valid JSON"]
        if rec.get("prev") != prev:
            return [f"LEDGER TAMPERED: line {n} does not follow the line before it "
                    f"(chain expected {prev[:12]}, line says {str(rec.get('prev'))[:12]}) "
                    f"— a line was altered or removed"]
        body = {k: v for k, v in rec.items() if k != "self"}
        if rec.get("self") != sha256_bytes(canonical(body).encode("utf-8")):
            return [f"LEDGER TAMPERED: line {n} ({rec.get('file')}) does not hash to its "
                    f"own contents"]
        prev = sha256_bytes(raw.encode("utf-8"))
    return problems


def append_ledger(state_dir, records):
    """Append, never rewrite."""
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_ledger(state_dir)
    prev = sha256_bytes(existing[-1][1].encode("utf-8")) if existing else GENESIS
    line_no = (existing[-1][2]["line"] + 1) if existing and existing[-1][2] else 1
    out = []
    for rec in records:
        rec = dict(rec, line=line_no, prev=prev, ts=now(), factgate=VERSION)
        rec["self"] = sha256_bytes(canonical(rec).encode("utf-8"))
        raw = canonical(rec)
        out.append(raw)
        prev = sha256_bytes(raw.encode("utf-8"))
        line_no += 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(l + "\n" for l in out))
    return len(out)


def ledger_state(entries):
    """The last word the ledger has on each file. A recorded delete or archive means `not
    here`; a restore puts the file back at the sha it left with."""
    state = {}
    for _, _, rec in entries:
        name = rec.get("file")
        if name is None or rec.get("kind") == "release":
            continue
        if rec.get("kind") in ("delete", "archive"):
            state.pop(name, None)
        else:
            state[name] = rec
    return state


def make_record(kind, name, path, reason):
    return {"kind": kind, "file": name, "reason": reason,
            "sha256": file_hash(path) if path is not None else None,
            "size": Path(path).stat().st_size if path is not None else None}


# ---------------------------------------------------------------------- check

def run_check(facts_dir, state_dir):
    """[] when the directory still says what the ledger says it said."""
    entries = read_ledger(state_dir)
    if not entries:
        return ["NO LEDGER — this fact base has not been baselined; nothing is being "
                "watched"], {}
    problems = verify_chain(entries)
    if problems:
        problems.append("the ledger cannot be trusted, so the directory was not compared "
                        "against it")
        return problems, {}
    recorded = ledger_state(entries)
    files, dirs = listing(facts_dir)
    problems = []
    for name in dirs:
        problems.append(f"UNTRACKED DIR: {name} — a fact base is flat; this was not "
                        f"recursed into and nothing in it is watched")
    for name in sorted(files):
        if name not in recorded:
            problems.append(f"NEW OFF-LEDGER: {name} — it appeared without going through "
                            f"`factgate record`")
        else:
            was, is_now = recorded[name]["sha256"], file_hash(files[name])
            if was != is_now:
                problems.append(f"CHANGED OFF-LEDGER: {name} {was[:12]} -> {is_now[:12]}")
    for name in sorted(recorded):
        if name not in files:
            problems.append(f"DELETED OFF-LEDGER: {name} — it is in the record and not on "
                            f"disk")
    return problems, recorded


def verb_check(args):
    problems, recorded = run_check(args.facts_dir, args.state_dir)
    if problems:
        sys.stderr.write(f"CHECK FAILED: {os.path.abspath(args.facts_dir)}\n")
        for p in problems:
            sys.stderr.write(p + "\n")
        sys.stderr.write("a fact that can change with nothing noticing is not a record\n")
        return EXIT_CHECK
    sys.stdout.write(f"OK: {os.path.abspath(args.facts_dir)} still says what the ledger "
                     f"says it said\n"
                     f"  {len(recorded)} recorded, {len(read_ledger(args.state_dir))} "
                     f"ledger line(s), chain intact\n")
    return EXIT_OK


# ------------------------------------------------------------------- baseline

def verb_baseline(args):
    if read_ledger(args.state_dir):
        sys.stderr.write(
            f"REFUSED: {ledger_path(args.state_dir)} already exists.\n"
            "A second baseline would silently adopt whatever the directory says today,\n"
            "which is the opposite of what a baseline is for. Use `record` for each\n"
            "change, or move the ledger aside deliberately if you mean to start over.\n")
        return EXIT_REFUSED
    files, dirs = listing(args.facts_dir)
    if not files:
        die_usage(f"{args.facts_dir} holds no regular files to record")
    n = append_ledger(args.state_dir,
                      [make_record("baseline", name, path, "baseline")
                       for name, path in sorted(files.items())])
    sys.stdout.write(f"BASELINE RECORDED: {ledger_path(args.state_dir)}\n"
                     f"  {n} file(s) from {os.path.abspath(args.facts_dir)}\n")
    for name, path in sorted(files.items())[:5]:
        sys.stdout.write(f"    {name:<50} {file_hash(path)[:12]}  {path.stat().st_size} bytes\n")
    if n > 5:
        sys.stdout.write(f"    … and {n - 5} more\n")
    for name in dirs:
        sys.stdout.write(f"  NOTE: subdirectory {name} was not recursed into\n")
    sys.stdout.write("every file above is now watched; `check` reports anything that moves\n")
    return EXIT_OK


# --------------------------------------------------------------------- record

def refuse(verb, target, results, order=RECORD_PRECONDITIONS):
    sys.stderr.write(f"REFUSED: {os.path.abspath(target)}\n")
    for name in order:
        status, detail = results[name]
        label = {"ok": "ok      ", "failed": "FAILED  ",
                 "not-evaluated": "not-evaluated "}[status]
        sys.stderr.write(f"{label} {name}: {detail}\n")
    failed = [n for n in order if results[n][0] == "failed"]
    sys.stderr.write(f"refused on {len(failed)} precondition(s): {', '.join(failed)}\n")
    return EXIT_REFUSED


def _record(args, deleting):
    name = Path(args.file).name
    path = Path(args.facts_dir) / name
    entries = read_ledger(args.state_dir)
    results = {}

    if not entries:
        results["F1_LEDGER_INTACT"] = ("failed", "no ledger; run `baseline` first")
    else:
        chain = verify_chain(entries)
        results["F1_LEDGER_INTACT"] = (("failed", chain[0]) if chain else
                                       ("ok", f"{len(entries)} line(s), chain intact"))
    recorded = ledger_state(entries) if results["F1_LEDGER_INTACT"][0] == "ok" else {}

    if results["F1_LEDGER_INTACT"][0] != "ok":
        for other in RECORD_PRECONDITIONS[1:]:
            results[other] = ("not-evaluated", "depends on F1_LEDGER_INTACT")
    else:
        if deleting:
            if path.exists():
                results["F2_FILE_PRESENT"] = (
                    "failed", f"{name} is still there; record-delete is for a file that has "
                              f"already gone, and this tool never deletes anything")
            elif name not in recorded:
                results["F2_FILE_PRESENT"] = (
                    "failed", f"{name} is not in the record, so there is no deletion to note")
            else:
                results["F2_FILE_PRESENT"] = ("ok", f"{name} is gone and was recorded")
        else:
            if not path.is_file():
                results["F2_FILE_PRESENT"] = ("failed", f"{name} is not a file in "
                                                        f"{args.facts_dir}")
            else:
                results["F2_FILE_PRESENT"] = ("ok", f"{name}, {path.stat().st_size} bytes")

        if deleting:
            results["F3_SOMETHING_CHANGED"] = (
                ("ok", "the file is gone") if results["F2_FILE_PRESENT"][0] == "ok"
                else ("not-evaluated", "depends on F2_FILE_PRESENT"))
        elif results["F2_FILE_PRESENT"][0] != "ok":
            results["F3_SOMETHING_CHANGED"] = ("not-evaluated", "depends on F2_FILE_PRESENT")
        else:
            was = recorded.get(name, {}).get("sha256")
            is_now = file_hash(path)
            if was == is_now:
                results["F3_SOMETHING_CHANGED"] = (
                    "failed", f"{name} is unchanged since its last ledger line "
                              f"({is_now[:12]}); there is nothing to record")
            else:
                results["F3_SOMETHING_CHANGED"] = (
                    "ok", f"{(was or '<new>')[:12]} -> {is_now[:12]}")

    if not args.reason.strip() or len(args.reason.strip()) < 3:
        results["F4_REASON_IN_WRITING"] = (
            "failed", "a change recorded with no reason is the silent edit this exists to "
                      "replace")
    else:
        results["F4_REASON_IN_WRITING"] = ("ok", f"{len(args.reason.strip())} characters")

    results["F5_CATEGORY_DECLARED"] = category_declared(path, recorded, results, deleting)

    if any(v[0] == "failed" for v in results.values()):
        return refuse("record", args.facts_dir, results)

    append_ledger(args.state_dir, [make_record("delete" if deleting else "record", name,
                                               None if deleting else path,
                                               args.reason.strip())])
    verb = "DELETION RECORDED" if deleting else "RECORDED"
    sys.stdout.write(f"{verb}: {name}\n")
    if not deleting:
        sys.stdout.write(f"  sha256 {file_hash(path)}\n")
    sys.stdout.write(f"  because: {args.reason.strip()}\n")
    for n in RECORD_PRECONDITIONS:
        sys.stdout.write(f"ok       {n}: {results[n][1]}\n")
    return EXIT_OK


def category_declared(path, recorded, results, deleting):
    """F5 (ORDER 76): only a file NEW to the ledger is asked for a `Category:` line. The list
    is categories.py's, loaded by path; if it cannot be loaded the gate fails closed."""
    if deleting:
        return ("ok", "record-delete: not required")
    if results["F3_SOMETHING_CHANGED"][0] != "ok":
        return ("not-evaluated", "depends on F3_SOMETHING_CHANGED")
    if recorded.get(path.name, {}).get("sha256"):
        return ("ok", "not new: not required")
    try:
        categories = _load(CATEGORIES_PY, "categories")
        cats = categories.load()
        if not cats:
            raise ValueError(f"no category names in {categories.DEFAULT}")
        names, problem = categories.declared_all(path.read_text(encoding="utf-8",
                                                                errors="replace"), cats)
    except Exception as e:
        return ("failed", f"cannot load the category list ({CATEGORIES_PY}): "
                          f"{e.__class__.__name__}: {e}")
    if names and not problem:
        return ("ok", f"declares {' · '.join(names)}")
    why = problem or f"no `Category:` line in its first {categories.HEAD_LINES} lines"
    return ("failed", f"a new fact must declare its category: {why}; names: "
                      f"{', '.join(c.name for c in cats)}")


def verb_record(args):
    return _record(args, deleting=False)


def verb_record_delete(args):
    return _record(args, deleting=True)


# ------------------------------------------------- ORDER 22b: legacy, interim, release

def legacy_path(state_dir):
    return Path(state_dir) / LEGACY_NAME


def ledger_prefix_sha(state_dir, n):
    """(lines in the ledger, sha256 of the raw bytes of its first n lines)."""
    raw = ledger_path(state_dir).read_bytes() if ledger_path(state_dir).exists() else b""
    lines = raw.splitlines(keepends=True)
    return len(lines), sha256_bytes(b"".join(lines[:n]))


def last_events(entries):
    """{file: (line, record)} — the last baseline/record/delete line for each file. Archive
    and restore move the same bytes, so they are not events here: a legacy fact that goes to
    the archive and comes back unchanged is still legacy."""
    out = {}
    for n, _, rec in entries:
        if rec and rec.get("file") is not None \
                and rec.get("kind") not in ("release", "archive", "restore"):
            out[rec["file"]] = (n, rec)
    return out


def derived_legacy(entries, cutoff, files):
    """Per ruling 1: last event at or before the cutoff line, and the file still has that sha."""
    out = {}
    for name, (n, rec) in last_events(entries).items():
        if n <= cutoff and rec.get("kind") != "delete" and name in files \
                and file_hash(files[name]) == rec.get("sha256"):
            out[name] = rec["sha256"]
    return out


def load_legacy(state_dir, entries, files):
    """({name: sha} trusted as legacy, note or None). Trust needs three things to agree: the
    file's own hash, the ledger prefix the vault anchored, and the rule re-derived now."""
    p = legacy_path(state_dir)
    if not p.exists():
        return {}, "no legacy baseline: every fact counts as INTERIM"
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        body = {k: v for k, v in j.items() if k != "self"}
        if j.get("self") != sha256_bytes(canonical(body).encode("utf-8")):
            return {}, f"{LEGACY_NAME} does not hash to its own contents: not trusted"
        n_lines, prefix = ledger_prefix_sha(state_dir, int(j["cutoff_line"]))
        if n_lines < int(j["cutoff_line"]) or prefix != j["cutoff_sha256"]:
            return {}, "the ledger's first lines no longer hash to the anchored cutoff"
        now = derived_legacy(entries, int(j["cutoff_line"]), files)
        return {k: v for k, v in j["legacy"].items() if now.get(k) == v}, None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, f"{LEGACY_NAME} unreadable ({type(exc).__name__}): not trusted"


def verdict_words(text):
    """[(line number, word as written)] — the verdict words, and 🟢 on the first line."""
    first = text.split("\n", 1)[0]
    out = [(1, VERDICT_EMOJI)] if VERDICT_EMOJI in first else []
    line, pos = 1, 0                     # one pass over the whole text; line numbers counted
    for m in _VERDICT_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        out.append((line, m.group(1)))
    return out


def interim_facts(facts_dir, state_dir=DEFAULT_STATE):
    """([{file, sha256, verdict}], [notes]). INTERIM = not legacy at this sha and not
    released at this sha. Read-only. The loader in app.py imports this, so the rule lives
    in one place."""
    entries = read_ledger(state_dir)
    files, _ = listing(facts_dir)
    notes = []
    chain_ok = bool(entries) and not verify_chain(entries)
    if not chain_ok:
        notes.append("the ledger is missing or broken: no legacy fact or release is trusted")
        legacy, released = {}, {}
    else:
        legacy, note = load_legacy(state_dir, entries, files)
        notes += [note] if note else []
        released = {}
        for _, _, rec in entries:
            if rec.get("kind") == "release":
                released.setdefault(rec.get("file"), set()).add(rec.get("sha256"))
    out = []
    for name, path in sorted(files.items()):
        raw = path.read_bytes()
        h = sha256_bytes(raw)
        if legacy.get(name) == h or h in released.get(name, ()):
            continue
        out.append({"file": name, "sha256": h,
                    "verdict": verdict_words(raw.decode("utf-8", errors="replace"))})
    return out, notes


def interim_summary(items, notes):
    flagged = [r["file"] for r in items if r["verdict"]]
    line = f"interim: {len(items)} facts, {len(flagged)} with verdict words"
    if flagged:
        line += ": " + ", ".join(flagged[:5]) + (" …" if len(flagged) > 5 else "")
    if notes:
        line += " (" + "; ".join(notes) + ")"
    return line


def verb_interim(args):
    """A report: exit 0 whatever it finds, or whatever goes wrong."""
    try:
        items, notes = interim_facts(args.facts_dir, args.state_dir)
    except Exception as exc:                              # a report never stops its caller
        sys.stdout.write(f"interim: unavailable — {type(exc).__name__}: {exc}\n")
        return EXIT_OK
    if args.json:
        sys.stdout.write(json.dumps({"interim": items, "notes": notes,
                                     "summary": interim_summary(items, notes)},
                                    ensure_ascii=False, indent=1) + "\n")
        return EXIT_OK
    for r in items:
        sys.stdout.write(f"INTERIM {r['file']}\n")
        if r["verdict"]:
            sys.stdout.write("  verdict words: " + ", ".join(f"L{n} {w}" for n, w in r["verdict"])
                             + "\n")
    sys.stdout.write(interim_summary(items, notes) + "\n")
    return EXIT_OK


def verb_baseline_legacy(args):
    """Once. Legacy = every fact whose LAST ledger event is at or before the cutoff line and
    whose bytes still hash to that event — the cutoff being a prefix hash the vault anchored."""
    p = legacy_path(args.state_dir)
    problems = []
    if p.exists():
        problems.append(f"{p} already exists; a legacy baseline is written once")
    entries = read_ledger(args.state_dir)
    chain = verify_chain(entries) if entries else ["no ledger"]
    problems += chain
    n_lines, prefix = ledger_prefix_sha(args.state_dir, args.cutoff_line)
    if args.cutoff_line < 1 or n_lines < args.cutoff_line:
        problems.append(f"cutoff line {args.cutoff_line} is not inside the ledger "
                        f"({n_lines} lines)")
    elif prefix != args.cutoff_sha256:
        problems.append(f"the ledger's first {args.cutoff_line} lines hash to {prefix}, not "
                        f"the anchored {args.cutoff_sha256}: prefix mismatch")
    if problems:
        sys.stderr.write(f"REFUSED: baseline-legacy {os.path.abspath(args.facts_dir)}\n")
        for line in problems:
            sys.stderr.write(f"FAILED   {line}\n")
        return EXIT_REFUSED
    files, _ = listing(args.facts_dir)
    legacy = derived_legacy(entries, args.cutoff_line, files)
    body = {"kind": "legacy-baseline", "factgate": VERSION, "created": now(),
            "facts_dir": os.path.abspath(args.facts_dir),
            "cutoff_line": args.cutoff_line, "cutoff_sha256": args.cutoff_sha256,
            "vault_seq": args.vault_seq, "ledger_lines_at_baseline": n_lines,
            "legacy": dict(sorted(legacy.items())),
            "interim_at_baseline": sorted(set(files) - set(legacy))}
    body["self"] = sha256_bytes(canonical(body).encode("utf-8"))
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        sys.stderr.write(f"REFUSED: {p} already exists\n")
        return EXIT_REFUSED
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.write(f"LEGACY BASELINE RECORDED: {p}\n"
                     f"  cutoff line {args.cutoff_line}, sha256 {args.cutoff_sha256}, "
                     f"vault seq {args.vault_seq}\n"
                     f"  {len(legacy)} legacy, {len(body['interim_at_baseline'])} INTERIM: "
                     f"{', '.join(body['interim_at_baseline'])}\n"
                     f"  sha256 {file_hash(p)}\n")
    return EXIT_OK


def _under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _witness_file(ref):
    """A witness path, relative to helix/ unless absolute."""
    p = Path(os.path.expanduser(ref))
    return p if p.is_absolute() else HELIX / p


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def witness_regate(ref, fact_text):
    registry = HELIX / "workspace/seam/registry/registry.json"
    ledger = registry.parent / "REGATE_LEDGER.jsonl"
    if not ledger.is_file():
        return False, f"no regate ledger at {ledger}", None
    r = subprocess.run([sys.executable, str(HELIX / "workspace/regate/regate.py"), "check",
                        str(registry)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"regate check exits {r.returncode}: the registry is not intact", None
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") == "score" and ref in (rec.get("entry"), rec.get("id")) \
                and isinstance(rec.get("score"), dict) and rec["score"].get("outcome") in (0, 1):
            return True, f"{rec.get('id')} scored outcome {rec['score']['outcome']}", \
                file_hash(ledger)
    return False, f"no score line with outcome 0 or 1 for {ref!r} in {ledger}", None


def witness_seal(ref, fact_text):
    token = _witness_file(ref)
    if token.name != "RUN_TOKEN.json" or not token.is_file():
        return False, f"{token} is not a RUN_TOKEN.json", None
    try:
        prereg = Path(json.loads(token.read_text(encoding="utf-8"))["prereg"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, f"{token} names no prereg", None
    if os.path.realpath(prereg.parent) != os.path.realpath(token.parent):
        return False, f"{token} is not the token beside {prereg}", None
    r = subprocess.run([sys.executable, str(HELIX / "workspace/sealgate/sealgate.py"), "verify",
                        str(prereg)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"sealgate verify {prereg} exits {r.returncode}", None
    return True, f"sealgate verify {prereg} exits 0", file_hash(token)


def witness_review(ref, fact_text):
    f = _witness_file(ref)
    if not f.is_file():
        return False, f"{f} does not exist", None
    if _under(f, HELIX / "memory"):
        return False, f"{f} is inside memory/: Odin's own record cannot witness itself", None
    m = re.search(r"^\s*[*_]{0,2}Reviewer[*_]{0,2}\s*:(.*)$",
                  f.read_text(encoding="utf-8", errors="replace"), re.I | re.M)
    # `Reviewer: Alice          date: …` — the name ends at the first run of 2+ spaces, so
    # a blank name followed by a padded `date:` is a blank name, not a reviewer called "date".
    raw = re.split(r"\s{2,}|\t", re.sub(r"^[*_]*\s?[*_]*", "", m.group(1)))[0].strip() if m else ""
    if not raw:
        return False, f"{f} has no `Reviewer:` line naming anyone", None
    sg = _load(HELIX / "workspace/sealgate/sealgate.py", "_factgate_sealgate")
    names = sg.split_authors(raw)
    if not names or ODIN_RE.search(raw) or any(ODIN_RE.search(n) for n in names):
        return False, f"reviewer {raw!r} is Odin, or no one: the author cannot witness", None
    return True, f"reviewed by {', '.join(names)}", file_hash(f)


def witness_report(ref, fact_text):
    f = _witness_file(ref)
    reports = HELIX / "build_orders/reports"
    m = re.fullmatch(r"ORDER_(\d+[a-z]?)_report\.md", f.name)
    if not _under(f, reports) or os.path.dirname(os.path.realpath(f)) != os.path.realpath(reports):
        return False, f"{f} is not in {reports}", None
    if not m or f.name.endswith(("_review_odin.md", "_stopped_report.md")):
        return False, f"{f.name} is not a builder's ORDER_NN_report.md", None
    if not f.is_file():
        return False, f"{f} does not exist", None
    lines = [l.strip() for l in f.read_text(encoding="utf-8", errors="replace").splitlines()
             if l.strip()]
    if not lines or lines[-1] not in REPORT_ENDINGS:
        return False, f"{f.name} does not end with one of {', '.join(REPORT_ENDINGS)}", None
    nn = m.group(1)
    if not re.search(r"(?<![a-z0-9])order[\s_-]*0*" + re.escape(nn) + r"(?![a-z0-9])",
                     fact_text, re.I):
        return False, f"the fact does not name order {nn}", None
    return True, f"{f.name} ends {lines[-1]!r} and the fact names order {nn}", file_hash(f)


WITNESSES = {"regate": witness_regate, "seal": witness_seal, "review": witness_review,
             "report": witness_report}


def verb_release(args):
    name = Path(args.file).name
    path = Path(args.facts_dir) / name
    entries = read_ledger(args.state_dir)
    results = {}
    chain = verify_chain(entries) if entries else ["no ledger; run `baseline` first"]
    results["R1_LEDGER_INTACT"] = (("failed", chain[0]) if chain else
                                   ("ok", f"{len(entries)} line(s), chain intact"))
    h = file_hash(path) if path.is_file() else None
    if chain:
        results["R2_FACT_ON_LEDGER"] = ("not-evaluated", "depends on R1_LEDGER_INTACT")
        results["R3_FACT_IS_INTERIM"] = ("not-evaluated", "depends on R1_LEDGER_INTACT")
    else:
        was = ledger_state(entries).get(name, {}).get("sha256")
        if h is None:
            results["R2_FACT_ON_LEDGER"] = ("failed", f"{name} is not a file in {args.facts_dir}")
        elif was != h:
            results["R2_FACT_ON_LEDGER"] = (
                "failed", f"{name} is {h[:12]}, the ledger says {(was or '<nothing>')[:12]}: "
                          f"only a recorded version can be released")
        else:
            results["R2_FACT_ON_LEDGER"] = ("ok", f"{name} {h[:12]} is what the ledger says")
        if results["R2_FACT_ON_LEDGER"][0] != "ok":
            results["R3_FACT_IS_INTERIM"] = ("not-evaluated", "depends on R2_FACT_ON_LEDGER")
        elif name in {r["file"] for r in interim_facts(args.facts_dir, args.state_dir)[0]}:
            results["R3_FACT_IS_INTERIM"] = ("ok", f"{name} is INTERIM at {h[:12]}")
        else:
            results["R3_FACT_IS_INTERIM"] = (
                "failed", f"{name} is legacy or already released at {h[:12]}: nothing to release")
    kind, sep, ref = args.witness.partition(":")
    witness_sha = None
    if not sep or kind not in WITNESSES or not ref.strip():
        results["R4_WITNESS_CHECKED"] = (
            "failed", f"{args.witness!r} is not a witness; the kinds are "
                      f"{', '.join(k + ':' for k in WITNESS_KINDS)} and nothing else")
    elif h is None:
        results["R4_WITNESS_CHECKED"] = ("not-evaluated", "there is no fact to witness")
    else:
        try:
            ok, detail, witness_sha = WITNESSES[kind](ref.strip(), path.read_text(
                encoding="utf-8", errors="replace"))
        except Exception as exc:
            ok, detail = False, f"the witness could not be checked: {type(exc).__name__}: {exc}"
        results["R4_WITNESS_CHECKED"] = ("ok" if ok else "failed", detail)
    if len(args.reason.strip()) < 3:
        results["R5_REASON_IN_WRITING"] = ("failed", "a release with no reason is a rubber stamp")
    else:
        results["R5_REASON_IN_WRITING"] = ("ok", f"{len(args.reason.strip())} characters")

    if any(v[0] != "ok" for v in results.values()):
        return refuse("release", args.facts_dir, results, RELEASE_PRECONDITIONS)
    append_ledger(args.state_dir, [{"kind": "release", "file": name, "sha256": h,
                                    "size": path.stat().st_size, "witness": args.witness,
                                    "witness_sha256": witness_sha,
                                    "reason": args.reason.strip()}])
    sys.stdout.write(f"RELEASED: {name}\n  sha256 {h}\n  witness {args.witness}\n"
                     f"  because: {args.reason.strip()}\n")
    for n in RELEASE_PRECONDITIONS:
        sys.stdout.write(f"ok       {n}: {results[n][1]}\n")
    return EXIT_OK


# ------------------------------------------------------- ORDER 35b: archive, restore

def pinned_names(path=PINNED_TXT):
    """The names listed in PINNED.txt: one per line, `#` comments and blanks ignored — order
    33b's rule (app._pinned_names; test_facts_tiers.py checks the two agree). Raises if the
    file cannot be read: `archive` refuses rather than treating a lost list as no pins."""
    with open(path, encoding="utf-8") as fh:
        return {ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")}


def open_registry_names(names, path=REGISTRY_JSON):
    """The names an entry whose status is exactly "open" names, by filename or `NN_` prefix —
    app._open_registry_names's rule. Raises if the registry cannot be read."""
    with open(path, encoding="utf-8") as fh:
        reg = json.load(fh)
    texts = [json.dumps(e, ensure_ascii=False) for e in reg.values()
             if isinstance(e, dict) and e.get("status") == "open"]
    out = set()
    for text in texts:
        prefixes = set(_NN_PREFIX_RE.findall(text))
        out |= {fn for fn in names if fn in text or any(fn.startswith(p) for p in prefixes)}
    return out


def _actor():
    """Odin's shell is refused these verbs by factgate_hook (P5), so whoever runs them is at a
    terminal: the user, or a build order. Both run as the same Unix user; this records which."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _why(args, results, key):
    why = (args.why or "").strip()
    results[key] = (("failed", "a move with no reason is the silent change this replaces")
                    if len(why) < 3 else ("ok", f"{len(why)} characters"))
    return why


def _chain(entries, results, key):
    chain = verify_chain(entries) if entries else ["no ledger; run `baseline` first"]
    results[key] = ("failed", chain[0]) if chain else ("ok", f"{len(entries)} line(s), "
                                                             f"chain intact")
    return not chain


def _archive_one(target, entries, chain_ok, pins, opened, interim):
    """{precondition: (status, detail)} for one file, A2-A6."""
    r = {}
    given = Path(os.path.expanduser(target))
    path = FACTS_DIR / given.name
    in_facts = (not given.parent.parts or given.parent == Path(".")
                or os.path.realpath(given.parent) == os.path.realpath(FACTS_DIR))
    if not in_facts:
        r["A2_IN_MEMORY_FACTS"] = ("failed", f"{target} is not in {FACTS_DIR}")
    elif path.is_symlink() or not path.is_file():
        r["A2_IN_MEMORY_FACTS"] = ("failed", f"{given.name} is not a file in {FACTS_DIR}")
    elif not chain_ok:
        r["A2_IN_MEMORY_FACTS"] = ("not-evaluated", "depends on A1_LEDGER_INTACT")
    else:
        was = ledger_state(entries).get(given.name, {}).get("sha256")
        h = file_hash(path)
        r["A2_IN_MEMORY_FACTS"] = (
            ("ok", f"{given.name} {h[:12]} is what the ledger says") if was == h else
            ("failed", f"{given.name} is {h[:12]}, the ledger says {(was or '<nothing>')[:12]}: "
                       f"only a recorded version is archived, or the edit goes out of sight"))
    name = given.name
    if isinstance(pins, str):
        r["A3_NOT_PINNED"] = ("failed", pins)
    else:
        r["A3_NOT_PINNED"] = (("failed", f"{name} is listed in {PINNED_TXT}; only the user or a "
                                         f"build order takes it off that list")
                              if name in pins else ("ok", "not in PINNED.txt"))
    if isinstance(interim, str):
        r["A4_NOT_INTERIM"] = ("failed", interim)
    else:
        r["A4_NOT_INTERIM"] = (("failed", f"{name} is INTERIM: release it or leave it")
                               if name in interim else ("ok", "legacy or released"))
    if isinstance(opened, str):
        r["A5_NO_OPEN_ENTRY"] = ("failed", opened)
    else:
        r["A5_NO_OPEN_ENTRY"] = (("failed", f"an open registry entry names {name}")
                                 if name in opened else ("ok", "no open entry names it"))
    r["A6_ARCHIVE_SLOT_FREE"] = (
        ("failed", f"{ARCHIVE_DIR / name} already exists; nothing is overwritten")
        if os.path.lexists(ARCHIVE_DIR / name) else ("ok", f"{ARCHIVE_DIR / name} is free"))
    return r


def verb_archive(args):
    """Every named file is checked first; one refusal and nothing moves. Then each moves with
    os.replace and gets its own ledger line."""
    entries = read_ledger(args.state_dir)
    shared = {}
    chain_ok = _chain(entries, shared, "A1_LEDGER_INTACT")
    why = _why(args, shared, "A7_WHY_IN_WRITING")
    names = [Path(os.path.expanduser(t)).name for t in args.files]
    try:
        pins = pinned_names(PINNED_TXT)
    except Exception as exc:
        pins = f"{PINNED_TXT} unreadable ({type(exc).__name__}): pins unknown, so none move"
    try:
        opened = open_registry_names(names, REGISTRY_JSON)
    except Exception as exc:
        opened = f"{REGISTRY_JSON} unreadable ({type(exc).__name__}): open entries unknown"
    try:
        interim = {r["file"] for r in interim_facts(FACTS_DIR, args.state_dir)[0]}
    except Exception as exc:
        interim = f"INTERIM state unknown ({type(exc).__name__})"
    per = []
    for target in args.files:
        results = dict(shared, **_archive_one(target, entries, chain_ok, pins, opened, interim))
        per.append((target, results))
    if len(set(names)) != len(names):
        sys.stderr.write("REFUSED: the same file is named twice\n")
        return EXIT_REFUSED
    bad = [(t, r) for t, r in per if any(v[0] != "ok" for v in r.values())]
    if bad:
        for t, r in bad:
            refuse("archive", t, r, ARCHIVE_PRECONDITIONS)
        sys.stderr.write(f"nothing moved: {len(bad)} of {len(per)} file(s) refused\n")
        return EXIT_REFUSED
    ARCHIVE_DIR.mkdir(exist_ok=True)
    actor = _actor()
    for target, _ in per:
        name = Path(target).name
        src, dst = FACTS_DIR / name, ARCHIVE_DIR / name
        rec = {"kind": "archive", "file": name, "sha256": file_hash(src),
               "size": src.stat().st_size, "reason": why, "actor": actor}
        os.replace(src, dst)
        try:
            append_ledger(args.state_dir, [rec])
        except Exception:
            os.replace(dst, src)                     # no line, no move
            raise
        sys.stdout.write(f"ARCHIVED: {name} -> {dst}\n  sha256 {rec['sha256']}\n")
    sys.stdout.write(f"  because: {why}\n  actor: {actor}\n")
    return EXIT_OK


def verb_restore(args):
    name = Path(os.path.expanduser(args.file)).name
    src, dst = ARCHIVE_DIR / name, FACTS_DIR / name
    entries = read_ledger(args.state_dir)
    results = {}
    chain_ok = _chain(entries, results, "B1_LEDGER_INTACT")
    last = None
    for _, _, rec in entries if chain_ok else ():
        if rec.get("file") == name and rec.get("kind") != "release":
            last = rec
    if not chain_ok:
        results["B2_ARCHIVED_ON_LEDGER"] = ("not-evaluated", "depends on B1_LEDGER_INTACT")
    elif not last or last.get("kind") != "archive":
        results["B2_ARCHIVED_ON_LEDGER"] = ("failed", f"the ledger has not archived {name}")
    elif src.is_symlink() or not src.is_file():
        results["B2_ARCHIVED_ON_LEDGER"] = ("failed", f"{name} is not a file in {ARCHIVE_DIR}")
    elif file_hash(src) != last["sha256"]:
        results["B2_ARCHIVED_ON_LEDGER"] = (
            "failed", f"{name} is {file_hash(src)[:12]} in the archive, it left as "
                      f"{last['sha256'][:12]}: a changed file does not come back this way")
    else:
        results["B2_ARCHIVED_ON_LEDGER"] = ("ok", f"{name} {last['sha256'][:12]}, as archived")
    results["B3_FACTS_SLOT_FREE"] = (
        ("failed", f"{dst} already exists; nothing is overwritten") if os.path.lexists(dst)
        else ("ok", f"{dst} is free"))
    why = _why(args, results, "B4_WHY_IN_WRITING")
    if any(v[0] != "ok" for v in results.values()):
        return refuse("restore", args.file, results, RESTORE_PRECONDITIONS)
    rec = {"kind": "restore", "restore": True, "file": name, "sha256": last["sha256"],
           "size": src.stat().st_size, "reason": why, "actor": _actor()}
    os.replace(src, dst)
    try:
        append_ledger(args.state_dir, [rec])
    except Exception:
        os.replace(dst, src)
        raise
    count = sum(1 for _, _, r in read_ledger(args.state_dir) if r and r.get("restore") is True)
    sys.stdout.write(f"RESTORED: {name} -> {dst}\n  sha256 {rec['sha256']}\n"
                     f"  because: {why}\n  restores: {count} (if this climbs, archiving is "
                     f"too eager)\n")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="factgate.py",
        description="Detection for the fact base. Never writes to it, except archive and "
                    "restore, which the user runs. One file per record, one reason, one line — "
                    "there is no bulk record.")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE),
                        help="where the ledger lives (default: factgate/state). It is never "
                             "inside the fact base.")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("baseline", help="record every file in the directory, once")
    p.add_argument("facts_dir")
    p.set_defaults(fn=verb_baseline)

    p = sub.add_parser("check", help="has anything changed since the ledger last saw it?")
    p.add_argument("facts_dir")
    p.set_defaults(fn=verb_check)

    p = sub.add_parser("record", help="acknowledge ONE new or changed file")
    p.add_argument("facts_dir")
    p.add_argument("file")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=verb_record)

    p = sub.add_parser("record-delete", help="acknowledge ONE file that has gone")
    p.add_argument("facts_dir")
    p.add_argument("file")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=verb_record_delete)

    p = sub.add_parser("baseline-legacy",
                       help="once: mark as legacy every fact last recorded at or before a "
                            "ledger line whose prefix hash the vault anchored")
    p.add_argument("facts_dir")
    p.add_argument("--cutoff-line", type=int, required=True)
    p.add_argument("--cutoff-sha256", required=True)
    p.add_argument("--vault-seq", type=int, required=True)
    p.set_defaults(fn=verb_baseline_legacy)

    p = sub.add_parser("interim", help="report: every INTERIM fact and its verdict words "
                                       "(always exit 0)")
    p.add_argument("facts_dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=verb_interim)

    p = sub.add_parser("release", help="release ONE INTERIM fact on a witness its author "
                                       "did not write: regate:, seal:, review:, report:")
    p.add_argument("facts_dir")
    p.add_argument("file")
    p.add_argument("--witness", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=verb_release)

    p = sub.add_parser("archive", help="move recorded facts from memory/facts/ to "
                                       "memory/archive/, one ledger line each (the user runs it)")
    p.add_argument("files", nargs="+", metavar="file")
    p.add_argument("--why", required=True)
    p.set_defaults(fn=verb_archive, facts_dir=str(FACTS_DIR))

    p = sub.add_parser("restore", help="move ONE archived fact back to memory/facts/, "
                                       "byte-identical; counted (the user runs it)")
    p.add_argument("file")
    p.add_argument("--why", required=True)
    p.set_defaults(fn=verb_restore, facts_dir=str(FACTS_DIR))

    args = parser.parse_args(argv)
    if not Path(args.facts_dir).is_dir():
        if args.verb == "interim":
            sys.stdout.write(f"interim: unavailable — no such directory: {args.facts_dir}\n")
            return EXIT_OK
        sys.stderr.write(f"no such directory: {args.facts_dir}\n")
        return EXIT_USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
