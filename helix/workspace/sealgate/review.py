#!/usr/bin/env python3
"""review.py — reviewer rotation (order 47, item 9).

Every cold review so far was Claude-family reviewing Claude-family. This makes the choice of
reviewer mechanical, so the author of a seal cannot pick a friendly one:

    review.py assign <PREREG.md>     print the reviewer for this seal (and log the call)
    review.py run    <PREREG.md>     call the ASSIGNED reviewer; write its raw review beside
                                     the seal and log it

Reviewers: exactly two, `Alice` (Gemini 3.1 Pro via `agy`) and `Claude-cold` (a cold
`claude -p` session). Assignment is STRICT ALTERNATION over the ledger, not a coin and not a hash
of the seal (a hash can be re-rolled by editing a comma): a new seal gets the other reviewer than
the last NEW assignment; the first ever is Alice. A seal is identified by its path relative to
~/helix (an absolute path if it lives outside; such a seal is labelled `throwaway`). Asking again
for an assigned seal returns its assignment, never a re-roll. Every call appends a line.

REVIEWS.jsonl beside this file is append-only and hash-chained: each line carries `prev`, the
sha256 of the previous line's raw bytes (genesis: sha256 of nothing), and `seq`, its line number.
Known limit: this directory is not behind the write wall, so the chain makes an edit VISIBLE, not
impossible (as regate's ledger was before the vault), and the LAST line has no successor to
witness it. `sealgate.py` P9 reads this ledger; the nightly's `- reviews:` line reports it.

`run` gives the reviewer the cold review brief, the seal's text, and every file the seal names
by a relative path under its own directory (text only, ≤ 200 KB in total; anything left out is
listed, and a path that leaves the seal's directory is refused). Alice's prompt is an argv
string, so it is capped at 120,000 bytes (seal first, then files). The reply is written raw to
REVIEW_<reviewer>_<UTC>.md beside the seal (never overwritten), then a `review` line records its
sha256 and its finding ids ([F1], [F2] …).

There is no flag that fakes, substitutes or passes over a reviewer. Tests inject a fake reviewer
function with monkeypatch.

Exit codes:  0 ok · 2 usage · 3 refused · 5 the reviewer failed (nothing written)
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
import tempfile
import time
from pathlib import Path

VERSION = "1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_REVIEWER = 5

HERE = Path(__file__).resolve().parent
HELIX = HERE.parents[1]                       # ~/helix: this file is helix/workspace/sealgate/
LEDGER = HERE / "REVIEWS.jsonl"
GENESIS = hashlib.sha256(b"").hexdigest()

REVIEWER_NAMES = ("Alice", "Claude-cold")
FIRST = "Alice"

FILES_CAP = 200_000                           # bytes of named files, in total
ALICE_PROMPT_CAP = 120_000                    # bytes; her prompt is one argv string (131,072)

# Alice: as preflight.py calls her.
AGY = "agy"
MODEL = "Gemini 3.1 Pro (High)"
TIMEOUT = 900

# Claude-cold: as coldread.py runs a cold session.
CLI = Path.home() / ".local" / "bin" / "claude"
CONFIG_DIR = Path.home() / "claude" / "config"
SESSION_CAP_USD = 2.00
SESSION_TIMEOUT = 1500

# sealgate.py's stamp region, so body_sha256 here is sealgate's body hash.
STAMP_BLOCK_BYTES_RE = re.compile(re.escape(b"<!-- SEALGATE-STAMP BEGIN -->") + rb".*?"
                                  + re.escape(b"<!-- SEALGATE-STAMP END -->"), re.S)

INSTRUCTION = (
    "You are a cold reviewer of a sealed pre-registration: a plan for an experiment, written and "
    "frozen BEFORE any data is collected. You have no tools and no other context; judge only "
    "from the text you are given.\n"
    "Your one job: find what would make this seal pass when the claim is false. Look for "
    "missing controls, bands that can't fail, circular checks (the thing being changed is the "
    "thing verifying the change), and a bar not derived (a threshold asserted rather than "
    "argued). Be concrete: name the band, control or line, and say how a false claim gets "
    "through.\n"
    "Answer with numbered findings only, each starting on its own line as [F1], [F2], [F3] and "
    "so on, most serious first. If you find nothing, say so in one line with no [F] number.")

SYSTEM = ("You are a cold reviewer. You have no tools and no other context. Follow the "
          "instruction at the top of the message exactly.")

PATH_TOKEN_RE = re.compile(r"[\w.~/\-]+")
EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
NAMED_EXTS = (".py", ".md", ".json", ".csv", ".txt", ".sh")  # order 47b: "named but not found"
FINDING_ID_RE = re.compile(r"\[(F\d+)\]")


class ReviewerFailed(Exception):
    """The reviewer errored, timed out, or was not cold. Nothing is written."""


# --------------------------------------------------------------------- helpers

def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def body_sha256(raw):
    return sha256_bytes(STAMP_BLOCK_BYTES_RE.sub(b"", raw))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def seal_key(path):
    """The path relative to ~/helix, or the absolute path for anything outside it."""
    p = Path(os.path.abspath(path)).resolve()
    try:
        return str(p.relative_to(HELIX.resolve()))
    except ValueError:
        return str(p)


def path_of(key):
    return Path(key) if os.path.isabs(key) else HELIX / key


def finding_ids(text):
    return sorted(set(FINDING_ID_RE.findall(text)), key=lambda f: int(f[1:]))


# ---------------------------------------------------------------------- ledger

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
        rec = dict(rec, seq=seq, prev=prev, ts=utc_now(), review=VERSION)
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out.append(raw)
        written.append(rec)
        prev = sha256_bytes(raw.encode("utf-8"))
    needs_nl = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n" if needs_nl else "") + "".join(l + "\n" for l in out))
    return written


def assignment_of(entries, key):
    """The reviewer of the FIRST assign line for this seal, or None."""
    for _, _, rec in entries:
        if rec and rec.get("kind") == "assign" and rec.get("seal") == key:
            return rec.get("reviewer")
    return None


def next_reviewer(entries):
    """Strict alternation over NEW assignments; the first ever is Alice."""
    new = [rec for _, _, rec in entries
           if rec and rec.get("kind") == "assign" and not rec.get("repeat")]
    if not new:
        return FIRST
    return "Claude-cold" if new[-1].get("reviewer") == "Alice" else "Alice"


def chain_refusal(entries):
    problems = verify_chain(entries)
    if problems:
        return f"REVIEWS.jsonl chain is broken at line {problems[0][0]}: {problems[0][1]}"
    return None


def assign(prereg, ledger=None):
    """(reviewer, new). Appends one line, new or repeat."""
    entries = read_ledger(ledger)
    why = chain_refusal(entries)
    if why:
        raise RuntimeError(why)
    key = seal_key(prereg)
    existing = assignment_of(entries, key)
    reviewer = existing or next_reviewer(entries)
    append([{"kind": "assign", "seal": key, "reviewer": reviewer, "repeat": existing is not None,
             "throwaway": os.path.isabs(key)}], ledger)
    return reviewer, existing is None


# ----------------------------------------------------------- what is reviewed

def named_files(seal_text, seal_dir, cap=None):
    """([(relative name, text)] inlined, [(name, why)] left out) for every file the seal names
    by a relative path under its own directory. Sorted by name; text only; ≤ cap bytes total."""
    cap = FILES_CAP if cap is None else cap
    root = Path(seal_dir).resolve()
    candidates = {}
    for tok in PATH_TOKEN_RE.findall(seal_text):
        tok = tok.rstrip(".,;:-")
        if not EXT_RE.search(tok) or not re.search(r"[A-Za-z]", tok):
            continue
        if tok.startswith(("/", "~")):
            if Path(os.path.expanduser(tok)).is_file():
                candidates[tok] = (None, "an absolute path, not one under the seal's directory")
            continue
        p = (root / tok)
        if not p.is_file():
            continue
        real = p.resolve()
        if real != root and root not in real.parents:
            candidates[tok] = (None, "outside the seal's directory")
            continue
        candidates[str(real.relative_to(root))] = (real, None)
    inlined, left_out, total = [], [], 0
    for name in sorted(candidates):
        real, why = candidates[name]
        if why:
            left_out.append((name, why))
            continue
        if name.startswith("REVIEW_") or real.name == "REVIEWS.jsonl":
            continue
        raw = real.read_bytes()
        try:
            text = raw.decode("utf-8")
            if "\x00" in text:
                raise ValueError
        except ValueError:
            left_out.append((name, "not text"))
            continue
        if total + len(raw) > cap:
            left_out.append((name, f"over the {cap // 1000} KB cap on named files"))
            continue
        inlined.append((name, text))
        total += len(raw)
    return inlined, left_out


def named_not_found(seal_text, seal_dir):
    """Order 47b: path-like tokens (a `/` in them, or a known extension) that name nothing on
    disk, so the run header's `left out: none` cannot overstate what the reviewer saw."""
    root = Path(seal_dir)
    missing = set()
    for tok in PATH_TOKEN_RE.findall(seal_text):
        tok = tok.rstrip(".,;:-")
        if not tok or not ("/" in tok or tok.endswith(NAMED_EXTS)):
            continue
        path = Path(os.path.expanduser(tok)) if tok.startswith(("/", "~")) else root / tok
        if not path.exists():
            missing.add(tok)
    return sorted(missing)


def build_prompt(key, seal_text, inlined, left_out):
    parts = [INSTRUCTION, "", f"=== SEAL: {key} ===", seal_text.rstrip(), "",
             "=== FILES THE SEAL NAMES ===",
             "inlined: " + (", ".join(n for n, _ in inlined) or "none"),
             "left out: " + ("; ".join(f"{n} ({w})" for n, w in left_out) or "none"), ""]
    for name, text in inlined:
        parts += [f"=== FILE: {name} ===", text.rstrip(), ""]
    return "\n".join(parts)


def prompt_for(reviewer, key, seal_text, inlined, left_out):
    """(prompt, inlined, left_out). Alice's prompt is cut to ALICE_PROMPT_CAP: seal first, then
    files in order until the cap; the rest is listed as left out."""
    if reviewer != "Alice":
        return build_prompt(key, seal_text, inlined, left_out), inlined, left_out
    why = f"over Alice's {ALICE_PROMPT_CAP:,}-byte prompt cap"
    kept, dropped = [], []
    for item in inlined:
        trial = build_prompt(key, seal_text, kept + [item], left_out + dropped)
        if len(trial.encode("utf-8")) <= ALICE_PROMPT_CAP:
            kept.append(item)
        else:
            dropped.append((item[0], why))
    while True:                    # listing what was dropped adds bytes; recheck until it fits
        prompt = build_prompt(key, seal_text, kept, left_out + dropped)
        if len(prompt.encode("utf-8")) <= ALICE_PROMPT_CAP:
            return prompt, kept, left_out + dropped
        if not kept:
            raise ValueError(f"the seal alone is over Alice's {ALICE_PROMPT_CAP:,}-byte prompt cap")
        dropped.append((kept.pop()[0], why))


# ------------------------------------------------------------------- Alice

def _call_alice(prompt, cwd):
    """(returncode or None on timeout, stdout, stderr). Kills her whole process group.
    Copied from preflight.py."""
    cmd = [AGY, "-p", prompt, "--model", MODEL, "--output-format", "json"]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as exc:
        return -1, "", f"cannot start {AGY}: {exc}"
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        return None, out or "", (err or "") + f"\n[review: timed out after {TIMEOUT} s]"


def call_alice(prompt):
    with tempfile.TemporaryDirectory(prefix="review_alice_") as scratch:
        rc, stdout, stderr = _call_alice(prompt, scratch)
        stray = os.listdir(scratch)
    if rc is None:
        raise ReviewerFailed(f"Alice timed out after {TIMEOUT} s")
    if rc != 0:
        raise ReviewerFailed(f"agy exited {rc}: {stderr.strip()[-300:]}")
    if stray:
        raise ReviewerFailed(f"files appeared in Alice's scratch dir: {stray}")
    try:
        reply = json.loads(stdout)
        response, status = reply["response"], reply.get("status")
    except (ValueError, KeyError, TypeError) as exc:
        raise ReviewerFailed(f"agy output is not the expected JSON ({exc})")
    if status != "SUCCESS" or not isinstance(response, str):
        raise ReviewerFailed(f"agy status {status!r}")
    return {"text": response, "meta": {"model": MODEL, "via": "agy"}}


# ------------------------------------------------------------- Claude-cold

def session_command(system, prompt, workdir):
    """→ (argv, stdin file, cwd, env). coldread.py's cold flag set; the prompt is written to a
    regular file and goes in on stdin, never argv."""
    workdir = Path(workdir)
    stdin_path = workdir / "prompt.txt"
    stdin_path.write_text(prompt, encoding="utf-8")
    cwd = workdir / "cwd"
    cwd.mkdir()
    argv = [str(CLI), "-p",
            "--model", "opus",
            "--tools", "",
            "--system-prompt", system,
            "--setting-sources", "",
            "--strict-mcp-config", "--disable-slash-commands", "--safe-mode",
            "--no-session-persistence",
            "--max-budget-usd", f"{SESSION_CAP_USD:.2f}",
            "--output-format", "stream-json", "--verbose"]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(CONFIG_DIR)
    env.pop("ANTHROPIC_API_KEY", None)
    return argv, stdin_path, cwd, env


def check_cold(init):
    """→ the list of things a cold session must not have (empty = cold). From coldread.py."""
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


def claude_session(system, prompt, workdir):
    """One real cold session, read as it streams: a warm init event kills it before the model
    is asked anything. From coldread.py. → {"text", "cost_usd", "init"[, "error"]}."""
    argv, stdin_path, cwd, env = session_command(system, prompt, workdir)
    err_path = Path(workdir) / "stderr.txt"
    init, result = None, None
    with open(stdin_path, "rb") as fin, open(err_path, "wb") as ferr:
        proc = subprocess.Popen(argv, stdin=fin, stdout=subprocess.PIPE, stderr=ferr, cwd=cwd,
                                env=env, text=True, encoding="utf-8")
        deadline = time.monotonic() + SESSION_TIMEOUT
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


def call_claude_cold(prompt):
    with tempfile.TemporaryDirectory(prefix="review_cold_") as tmp:
        out = claude_session(SYSTEM, prompt, tmp)
    meta = {"model": "opus", "via": "claude -p (cold)", "cost_usd": out.get("cost_usd"),
            "init": init_record(out.get("init"))}
    warm = check_cold(out.get("init"))
    if warm:
        raise ReviewerFailed("NOT COLD: " + "; ".join(warm))
    if out.get("error"):
        raise ReviewerFailed(f"the cold session failed: {out['error']}"[:400])
    return {"text": out.get("text") or "", "meta": meta}


REVIEWERS = {"Alice": call_alice, "Claude-cold": call_claude_cold}


# ------------------------------------------------------------------------ run

def run(prereg, ledger=None):
    """Call the assigned reviewer; write the raw review; append the review line. → the line."""
    prereg = Path(prereg)
    entries = read_ledger(ledger)
    why = chain_refusal(entries)
    if why:
        raise RuntimeError(why)
    key = seal_key(prereg)
    reviewer = assignment_of(entries, key)
    if reviewer is None:
        raise LookupError(f"{key} has no assigned reviewer: run `review.py assign` first")
    if reviewer not in REVIEWERS:
        raise LookupError(f"{key} is assigned to {reviewer!r}, which is not a reviewer")
    raw_seal = prereg.read_bytes()
    seal_text = raw_seal.decode("utf-8", errors="replace")
    inlined, left_out = named_files(seal_text, prereg.parent)
    prompt, inlined, left_out = prompt_for(reviewer, key, seal_text, inlined, left_out)
    started = utc_now()
    out_path = prereg.parent / f"REVIEW_{reviewer}_{utc_stamp()}.md"
    if out_path.exists():
        raise FileExistsError(f"{out_path} already exists; a raw review is never overwritten")
    reply = REVIEWERS[reviewer](prompt)
    meta = reply.get("meta") or {}
    header = [f"# REVIEW — {reviewer} — written by review.py; raw, never edited after",
              f"- seal: {key}",
              f"- seal-body-sha256: {body_sha256(raw_seal)}",
              f"- reviewer: {reviewer} ({meta.get('model', '?')})",
              f"- started: {started}",
              f"- files inlined: {', '.join(n for n, _ in inlined) or 'none'}",
              f"- files left out: {'; '.join(f'{n} ({w})' for n, w in left_out) or 'none'}",
              f"- named but not found: "
              f"{', '.join(named_not_found(seal_text, prereg.parent)) or 'none'}",
              "", "---", ""]
    with open(out_path, "x", encoding="utf-8") as fh:
        fh.write("\n".join(header) + reply["text"].rstrip() + "\n")
    raw_out = out_path.read_bytes()
    ids = finding_ids(raw_out.decode("utf-8", errors="replace"))
    rec = append([{"kind": "review", "seal": key, "reviewer": reviewer,
                   "body_sha256": body_sha256(raw_seal), "file_sha256": sha256_bytes(raw_seal),
                   "raw": seal_key(out_path), "raw_sha256": sha256_bytes(raw_out),
                   "findings": ids, "files_inlined": [n for n, _ in inlined],
                   "files_left_out": [[n, w] for n, w in left_out],
                   "prompt_bytes": len(prompt.encode("utf-8")), "meta": meta,
                   "throwaway": os.path.isabs(key)}], ledger)[0]
    return rec


# -------------------------------------------------------------- nightly line

def reviews_line(ledger=None):
    """`- reviews: <n> seals · Alice <a> / Claude-cold <c> · chain OK`. Reads only."""
    entries = read_ledger(ledger)
    if not entries:
        return "- reviews: none yet"
    new = [rec for _, _, rec in entries
           if rec and rec.get("kind") == "assign" and not rec.get("repeat")]
    seals = {rec.get("seal") for _, _, rec in entries if rec and rec.get("kind") == "assign"}
    a = sum(r.get("reviewer") == "Alice" for r in new)
    c = sum(r.get("reviewer") == "Claude-cold" for r in new)
    problems = verify_chain(entries)
    chain = f"🔴 chain BROKEN at line {problems[0][0]}" if problems else "chain OK"
    return f"- reviews: {len(seals)} seals · Alice {a} / Claude-cold {c} · {chain}"


# ----------------------------------------------------------------------- CLI

def verb_assign(args):
    try:
        reviewer, new = assign(args.prereg)
    except RuntimeError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_REFUSED
    sys.stdout.write(f"{reviewer}\n")
    sys.stdout.write(f"{'assigned' if new else 'already assigned (not re-rolled)'}: "
                     f"{seal_key(args.prereg)} -> {reviewer}\n")
    return EXIT_OK


def verb_run(args):
    try:
        rec = run(args.prereg)
    except (RuntimeError, LookupError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_REFUSED
    except FileExistsError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_USAGE
    except ValueError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_USAGE
    except ReviewerFailed as exc:
        sys.stderr.write(f"FAILED: {exc}; nothing written\n")
        return EXIT_REVIEWER
    sys.stdout.write(f"REVIEW: {path_of(rec['raw'])}\n"
                     f"reviewer {rec['reviewer']}, findings {', '.join(rec['findings']) or 'none'}"
                     f", ledger line {rec['seq']}\n")
    if not rec["findings"]:
        sys.stderr.write("WARNING: the reviewer returned no numbered findings\n")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="review.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("assign", help="the reviewer for this seal (strict alternation)")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_assign)
    p = sub.add_parser("run", help="call the ASSIGNED reviewer and record the raw review")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_run)
    args = ap.parse_args(argv)
    if not Path(args.prereg).is_file():
        sys.stderr.write(f"no such pre-registration: {args.prereg}\n")
        return EXIT_USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
