#!/usr/bin/env python3
"""regate.py — the registry write gate.

A prediction's goalposts are frozen the moment it is registered. This tool makes
that mechanical instead of a promise: it records the frozen fields of every
prediction in an append-only, hash-chained ledger beside the registry, and it
refuses to register, to score, or to bless a registry whose frozen fields have
moved since.

    regate.py baseline <registry.json>
    regate.py check    <registry.json>                     exit 0 / 4
    regate.py register <registry.json> <entry.json>        exit 0 / 3
    regate.py score    <registry.json> <id> --outcome 0|1|none --note "..." [--value <n>]
    regate.py attach-pack <registry.json> <id> <pack-name>     exit 0 / 3
    regate.py attach-deps <registry.json> <entry-id> (--on <id|pack:name> ...|--none)   exit 0 / 3

FROZEN, per prediction — changing any of these after registration is moving the
goalpost, and `check` reports it:

    id, kind, p, due, metric, statement, kill, kill_rule, min_n, prereg, pack

FROZEN, per entry: claim, scope, supersedes, structural_change.

A met kill has a consequence (order 25b): when a scored outcome meets a structured
`kill_rule`, the entry's status becomes INVALID in the same call. INVALID is final,
and it is local to the entry's scope: it never touches another entry, and a new
entry that restates it is refused only in the same scope (R5).

NOT frozen: `status`, entry-level notes, `sealed_code`. Cutting scope and saying so
in `status` is legitimate and must stay possible.

The score fields (`scored`, `outcome`, `brier`, `note`) are frozen ONCE WRITTEN: a
written score is never overwritten, by this tool or by hand without `check` seeing it.

An experiment is scored only after a different seat has re-analysed its numbers (order 49,
item 10): it needs a replay pack attached (`attach-pack`, once) and an intact AGREE for that
pack's current manifest in seatswap's SWAPS.jsonl (S7). A time prediction has no analysis to swap.

An entry declares once what it rests on (order 51, item 23: `attach-deps`): other entries, or
`pack:<name>` for the code/data under it. QUARANTINED is computed on every read, never stored: an
entry is quarantined while anything upstream of it, transitively, is INVALID or quarantined, or is
a pack whose latest replay is MISMATCH/TAMPERED or whose latest seat swap is DISAGREE. A
quarantined entry is not scored (S8) and gets no prereg or pack attached (A_NOT_QUARANTINED).

There is no way to waive any of this. A gate that can be waived is a warning.
Refusals are exit codes and stderr, and every one appends a line to refusals.log
next to this file.

Exit codes:  0 ok · 2 usage / unreadable input · 3 refused · 4 check failed
"""

import argparse
import datetime
import hashlib
import json
import math
import operator
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = "1.7"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_CHECK = 4

LEDGER_NAME = "REGATE_LEDGER.jsonl"
REFUSALS_LOG = Path(__file__).resolve().parent / "refusals.log"
SEALGATE = Path.home() / "helix/workspace/sealgate/sealgate.py"
# Order 27 (item 16): what the cycle cost, measured by a tool whose ledgers Odin's shell
# cannot write. Run as a subprocess, like sealgate. Never imported.
CYCLE = Path.home() / "helix/workspace/cycle/cycle.py"
# Order 60 (P-6): S9 reads the attempts log AS A FILE, read-only, to find a run logged after the
# seal. Only Helix's process writes it (order 28).
ATTEMPTS = Path.home() / "helix/workspace/cycle/ATTEMPTS.jsonl"
TOKEN_NAME = "RUN_TOKEN.json"
# Order 49 (item 10): seat-swap replication. Run as a subprocess (`seatswap.py check`), like
# sealgate. Never imported.
SEATSWAP = Path.home() / "helix/workspace/seatswap/seatswap.py"
# Order 51 (item 23): the provenance DAG reads these ledgers AS FILES, once per call, never by
# importing replay or seatswap and never by a subprocess per node. A line it cannot read, or a
# broken SWAPS chain, quarantines every pack edge (fail closed).
PACKS = Path.home() / "helix/workspace/replay/PACKS.jsonl"
REPLAYS = Path.home() / "helix/workspace/replay/REPLAYS.jsonl"
SWAPS = Path.home() / "helix/workspace/seatswap/SWAPS.jsonl"
PACK_EDGE = "pack:"
DEAD_REPLAYS = ("MISMATCH", "TAMPERED")
DEAD_SWAP = "DISAGREE"

# The goalposts. Absent fields are encoded as ABSENT, never as null.
FROZEN_FIELDS = ("id", "kind", "p", "due", "metric", "statement", "kill", "kill_rule",
                 "min_n", "prereg", "pack")
# `pack` (order 49): the replay pack whose seat-swap AGREE an experiment needs before it is
# scored. Set once by `attach-pack`. Absent on every prediction before order 49, so those hash
# as they always did.
# `value` is the measured number a `kill_rule` on "value" is judged against. Absent
# unless one was given, so a score written before order 25b hashes as it always did.
SCORE_FIELDS = ("scored", "outcome", "brier", "note", "value", "cycle", "k")
# `cycle` (order 27): seal→score (or register→score) wallclock, GPU-minutes, turns and token
# spend in that window, an upper bound. Written with the score and frozen with it. Absent on
# every score written before order 27, so those hash as they always did.
# `k` (order 28, item 24): K = attempts logged for the entry's scope before the score, counted
# by cycle.py from ATTEMPTS.jsonl (which only Helix's process writes), with the GPU ledger as
# witness. Disclosure only: no correction formula. Absent on every score before order 28.
# Frozen at entry level. A claim that can be reworded after registration is not a claim,
# and neither is one whose scope, or the dead claim it says it supersedes, can be swapped.
ENTRY_FROZEN_FIELDS = ("claim", "scope", "supersedes", "structural_change")
# `depends_on` (order 51) is NOT in that list: adding it would make every entry baselined before
# it drift. Its frozen record is the `attach-deps` ledger line, and `check` compares against that.

# --- order 25b: the falsifier has a consequence -------------------------------------
# A kill_rule is {"on": "outcome"|"value", "op": <one of KILL_OPS>, "value": <number>},
# or the literal "prose": allowed, but flagged at registration and never auto-evaluated.
# An existing prediction with no kill_rule is LEGACY prose (0000-0004); it is not
# back-filled, because kill_rule is frozen and adding one now would itself move a goalpost.
KILL_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
            "==": operator.eq}
KILL_ON = ("outcome", "value")
PROSE_RULE = "prose"
INVALID = "INVALID"                       # a status that starts with this is final
# Scope: where a claim is tested. Lower-case, `/`-separated, non-empty. An entry with
# none (0000-0004) reads as `legacy`. INVALID retires a claim in its scope, not everywhere.
SCOPE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
LEGACY_SCOPE = "legacy"
# Re-opening a dead claim needs a new mechanism or a new instrument, said in words.
# A new learning rate, seed, prompt or wording is the same claim run again.
STRUCTURAL_TYPES = ("mechanism", "instrument")
STRUCTURAL_WHAT_MIN = 20
STOPWORDS = frozenset("""
a an the of to in on at by for from with without into onto over under than then as and or
but nor not no is are was were be been being it its this that these those there their they
we our you your he she his her i me my do does did doing has have had having will would
shall should can could may might must if so such which who whom whose what when where
while how per each every any all both either neither one s also only same other own just
""".split())
# R5 resemblance threshold — MEASURED, not guessed (order 25b decision 6; table in README).
# resemblance() = the highest Jaccard over content-word sets, across every pair of
# (claim or statement of the new entry) x (claim or statement of the old one).
#   honest paraphrases of one fixture claim (fixtures/similarity, 3):  0.765 0.706 0.706
#   different claims, same topic (fixtures/similarity, 3):             0.167 0.176 0.167
#   live entries 0000-0004 against each other (10 pairs):              max 0.480 (0000 v
#     0001, two statements built on one template), next 0.256 (0002 v 0003), rest <= 0.128
# No overlap: highest different 0.480, lowest paraphrase 0.706. The threshold is their
# midpoint: margin 0.116 below the paraphrases, 0.110 above 0000 v 0001. A tripwire, not
# a proof — item 12's canary measures it on real attempts later.
R5_THRESHOLD = 0.59

# `time`: the date is the claim, so it cannot be scored before `due`.
# `experiment`: `due` means "run by", so it may be scored early — but only while its
# pre-registration still verifies. Absent kind is read as `time`, the strict reading.
KINDS = ("time", "experiment")
DEFAULT_KIND = "time"

REGISTER_PRECONDITIONS = ["R1_ID_IS_NEXT", "R2_PREDICTION_COMPLETE",
                          "R3_REGISTRY_UNDRIFTED", "R4_PREREG_VERIFIES",
                          "R5_NOT_A_RESURRECTION"]
SCORE_PRECONDITIONS = ["S1_EXISTS_AND_UNDRIFTED", "S2_DUE_HAS_ARRIVED",
                       "S3_NOT_ALREADY_SCORED", "S4_NONE_NEEDS_UNDERPOWERED",
                       "S5_EXPERIMENT_SEALED", "S6_VALUE_MATCHES_RULE", "S7_SEAT_SWAP",
                       "S8_NOT_QUARANTINED", "S9_RUN_AFTER_SEAL"]
ATTACH_PRECONDITIONS = ["A1_PREDICTION_EXISTS_AND_UNDRIFTED", "A2_PREREG_SET_ONCE",
                        "A3_PREREG_VERIFIES", "A_NOT_QUARANTINED"]
PACK_PRECONDITIONS = ["A1_PREDICTION_EXISTS_AND_UNDRIFTED", "A4_PACK_SET_ONCE",
                      "A5_PACK_IN_PACKS_AND_VERIFIES", "A_NOT_QUARANTINED"]
DEPS_PRECONDITIONS = ["D1_ENTRY_EXISTS_AND_UNDRIFTED", "D2_DEPS_SET_ONCE",
                      "D3_EDGES_RESOLVE", "D4_NO_CYCLE"]
STATUS_PRECONDITIONS = ["T1_ENTRY_EXISTS_AND_UNDRIFTED", "T2_STATUS_NOT_EMPTY",
                        "R_INVALID_IS_FINAL"]
NARRATIVE_PRECONDITIONS = ["N1_ENTRY_EXISTS_AND_UNDRIFTED", "N2_NARRATIVE_SET_ONCE",
                           "N3_NARRATIVE_EXISTS"]
REVISE_PRECONDITIONS = ["V1_NARRATIVE_ATTACHED", "V2_REGISTRY_OTHERWISE_UNDRIFTED",
                        "V3_REASON_IN_WRITING"]
PRECONDITION_ORDER = {"register": REGISTER_PRECONDITIONS, "score": SCORE_PRECONDITIONS,
                      "attach-prereg": ATTACH_PRECONDITIONS, "status": STATUS_PRECONDITIONS,
                      "attach-pack": PACK_PRECONDITIONS,
                      "attach-deps": DEPS_PRECONDITIONS,
                      "attach-narrative": NARRATIVE_PRECONDITIONS,
                      "revise-narrative": REVISE_PRECONDITIONS}

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_ID = re.compile(r"^\d{4}$")

GENESIS = hashlib.sha256(b"").hexdigest()


# --------------------------------------------------------------------- basics

def today():
    return datetime.date.today().isoformat()


def utc_now():
    """Order 27: a new ledger `ts` is UTC with a Z. Lines written before carry a naive local
    time; they are never rewritten (the chain is over their raw text), and parse_time()
    reads a naive value as local time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """ISO time -> aware datetime. `Z` / an offset as written; a naive time is local."""
    d = datetime.datetime.fromisoformat(str(text))
    return d.astimezone() if d.tzinfo is None else d


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(obj):
    """The bytes a hash is taken over. Sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def subset(pred, fields):
    """Only the keys that are present. Absent stays absent."""
    return {k: pred[k] for k in fields if k in pred}


def frozen_of(pred):
    return subset(pred, FROZEN_FIELDS)


def entry_frozen_of(entry):
    return subset(entry, ENTRY_FROZEN_FIELDS)


def kind_of(pred):
    """(effective kind, a note for the refusal message). Absent reads as `time`."""
    declared = pred.get("kind")
    if declared in KINDS:
        return declared, ""
    return DEFAULT_KIND, " (no kind declared, treated as time)"


def resolve_prereg(raw, registry_path):
    return Path(raw) if os.path.isabs(str(raw)) else \
        Path(registry_path).resolve().parent / str(raw)


def sealgate_verify(path):
    """(ok, output lines) from the real sealgate, run as a subprocess. Never imported."""
    if not SEALGATE.exists():
        return False, [f"{SEALGATE} is not here to verify it"]
    if not Path(path).is_file():
        return False, [f"prereg {path} does not exist"]
    proc = subprocess.run([sys.executable, str(SEALGATE), "verify", str(path)],
                          capture_output=True, text=True)
    lines = (proc.stderr or proc.stdout).strip().splitlines() or [""]
    return proc.returncode == 0, lines


def seatswap_check(pack):
    """seatswap's own answer, run as a subprocess (never imported): {"ok", "pack_ok",
    "manifest_sha256", "outcome", "why"}. Anything unreadable is a refusal, never a pass."""
    if not SEATSWAP.exists():
        return {"ok": False, "pack_ok": False, "outcome": "none",
                "why": f"{SEATSWAP} is not here to check it"}
    proc = subprocess.run([sys.executable, str(SEATSWAP), "check", str(pack)],
                          capture_output=True, text=True)
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        if not isinstance(res, dict):
            raise ValueError("not a JSON object")
    except (ValueError, IndexError) as exc:
        return {"ok": False, "pack_ok": False, "outcome": "none",
                "why": f"seatswap check gave no answer (exit {proc.returncode}, {exc}): "
                       f"{(proc.stderr or proc.stdout).strip()[-200:]}"}
    res["ok"] = bool(res.get("ok")) and proc.returncode == 0
    res["pack_ok"] = bool(res.get("pack_ok"))
    return res


def score_of(pred):
    s = subset(pred, SCORE_FIELDS)
    return s if "scored" in s else None


# ------------------------------------------------- order 27: the cycle line

def seal_issued_at(pred, registry_path):
    """The prereg token's issued_at: the seal time. Raises when there is none to read."""
    raw = pred.get("prereg")
    if not raw:
        raise ValueError("it names no prereg")
    token = resolve_prereg(raw, registry_path).parent / TOKEN_NAME
    issued = json.loads(token.read_text(encoding="utf-8"))["issued_at"]
    parse_time(issued)
    return issued


def run_after_seal(pred, entry, registry_path):
    """Order 60 (P-6): the S9_RUN_AFTER_SEAL result for an experiment scored before its due
    date. A run of the entry's scope must be logged in ATTEMPTS.jsonl strictly after the seal
    and not after now. Fails closed: an unreadable seal or attempts log is a failure."""
    pid, scope = pred.get("id"), scope_of(entry)
    try:
        issued = seal_issued_at(pred, registry_path)
        seal = parse_time(issued)
    except Exception as e:
        return ("failed", f"{pid} is scored early, but its seal time cannot be read "
                          f"({type(e).__name__}: {e})")
    try:
        lines = ATTEMPTS.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return ("failed", f"{pid} is scored early, but the attempts log {ATTEMPTS} cannot be "
                          f"read ({type(e).__name__}: {e})")
    now = datetime.datetime.now(datetime.timezone.utc)
    after = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if rec.get("scope") != scope:
                continue
            ts = parse_time(rec["ts"])
        except Exception as e:
            return ("failed", f"{pid} is scored early, but {ATTEMPTS} line {n} cannot be read "
                              f"({type(e).__name__}: {e})")
        if seal < ts <= now:
            after.append((ts, rec["ts"]))
    if not after:
        return ("failed", f"{pid} is scored early, but no run in scope {scope} is logged after "
                          f"its seal ({issued}); run it with attempt.py first")
    return ("ok", f"{len(after)} run(s) in {scope} after the seal; first {min(after)[1]}")

def cycle_start(pred, registry_path):
    """(from, ISO start) for this prediction's cycle. A prereg's token issued_at is the seal;
    otherwise its `register` ledger line, else its first ledger line (a `baseline`).
    (None, None) when there is neither."""
    if pred.get("prereg"):
        try:
            return "seal", seal_issued_at(pred, registry_path)
        except Exception:
            pass                                     # fall back to the ledger, below
    first = None
    for _, _, rec in read_ledger(registry_path):
        if not rec or rec.get("id") != pred.get("id") or not rec.get("ts"):
            continue
        if rec.get("kind") == "register":
            return "register", rec["ts"]
        if first is None:
            first = (rec.get("kind") or "ledger", rec["ts"])
    return first if first else (None, None)


def measure_cycle(frm, start):
    """The `cycle` score field. Never raises: a measurement must not block a score."""
    end = utc_now()
    out = {"from": frm, "start": start, "end": end, "upper_bound": True}
    try:
        if start is None:
            raise ValueError("no start time: no seal token and no ledger line")
        proc = subprocess.run([sys.executable, str(CYCLE), "window", str(start), end, "--json",
                               "--label", f"{frm}→score"],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"cycle.py exit {proc.returncode}: "
                               + ((proc.stderr or proc.stdout).strip().splitlines() or [""])[-1])
        w = json.loads(proc.stdout)
        out["wall_s"] = w["wall_s"]
        if w["gpu"].get("ledger"):
            out["gpu_min"] = w["gpu"]["min"]
        if w["turns"].get("ledger"):
            out["turns"] = w["turns"]["n"]
            if "cost_usd" in w["turns"]:
                out["cost_usd"] = w["turns"]["cost_usd"]
        out["line"] = w["line"]
    except Exception as exc:
        out["line"] = f"not measured ({type(exc).__name__}: {exc})"[:300]
    return out


def measure_k(scope, until):
    """The `k` score field: {"k", "since", "until", "witness", "unlogged", "line"}, plus the
    unlogged jobs when the witness disputes K. Never raises; never refuses: a score with K=0
    is printed, because refusing it would push attempts off the record."""
    out = {"k": None, "since": None, "until": until, "witness": "none", "unlogged": 0}
    try:
        proc = subprocess.run([sys.executable, str(CYCLE), "k", "--scope", scope, "--until",
                               until, "--json"], capture_output=True, text=True, timeout=60)
        try:
            r = json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError(f"cycle.py k exit {proc.returncode}: "
                               + ((proc.stderr or proc.stdout).strip().splitlines() or [""])[-1])
        out.update(k=r["k"], since=r["since"], witness=r["witness"], unlogged=r["unlogged"],
                   line=r["line"])
        if r.get("unlogged_jobs"):
            out["unlogged_jobs"] = r["unlogged_jobs"]
    except Exception as exc:
        out["line"] = f"K not measured ({type(exc).__name__}: {exc})"[:300]
    return out


def k_text(pred):
    k = pred.get("k")
    if not isinstance(k, dict):
        return "not recorded (pre-gate)"
    return str(k.get("line"))


def cycle_text(pred):
    c = pred.get("cycle")
    if not isinstance(c, dict):
        return "not recorded (pre-gate)"
    return str(c.get("line"))


# ------------------------------------------------- order 25b: kills, scope, R5

def is_invalid(entry):
    return str(entry.get("status") or "").startswith(INVALID)


def scope_of(entry):
    """An entry with no scope (0000-0004) is `legacy`. Not written back: scope is frozen."""
    return entry.get("scope") or LEGACY_SCOPE


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def kill_rule_problem(rule):
    """None when the rule is well-formed, else what is wrong with it."""
    if rule == PROSE_RULE:
        return None
    if not isinstance(rule, dict):
        return (f"kill_rule {render(rule)} is neither {{\"on\", \"op\", \"value\"}} nor the "
                f"literal {PROSE_RULE!r}")
    if sorted(rule) != ["on", "op", "value"]:
        return f"kill_rule must have exactly the keys on, op, value; got {sorted(rule)}"
    if rule["on"] not in KILL_ON:
        return f"kill_rule on {render(rule['on'])} is not one of {list(KILL_ON)}"
    if rule["op"] not in KILL_OPS:
        return f"kill_rule op {render(rule['op'])} is not one of {list(KILL_OPS)}"
    if not is_number(rule["value"]):
        return f"kill_rule value {render(rule['value'])} is not a finite number"
    if rule["on"] == "outcome" and rule["value"] not in (0, 1):
        return f"kill_rule on outcome compares against 0 or 1, got {render(rule['value'])}"
    return None


def describe_rule(rule):
    if isinstance(rule, dict):
        return f"{rule.get('on')} {rule.get('op')} {render(rule.get('value'))}"
    return "prose" if rule == PROSE_RULE else "absent (legacy prose)"


def scope_problem(scope):
    if not isinstance(scope, str) or not SCOPE.match(scope):
        return (f"scope {render(scope)} must be lower-case and `/`-separated, e.g. "
                f"\"crosscheck/attest\"")
    return None


def content_words(text):
    """Lower-cased word set with a short stopword list taken out."""
    words = re.findall(r"[a-z0-9]+(?:[.,][0-9]+)*", str(text).lower())
    return {w for w in words if w not in STOPWORDS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def resemblance(claim_a, statements_a, claim_b, statements_b):
    """The highest Jaccard over every pair of (claim or statement) of A x of B.

    A tripwire for a dead claim reworded, not a proof that two claims are the same.
    """
    texts_a = [content_words(t) for t in [claim_a, *statements_a]]
    texts_b = [content_words(t) for t in [claim_b, *statements_b]]
    return max((jaccard(a, b) for a in texts_a for b in texts_b), default=0.0)


def statements_of(entry):
    return [p.get("statement", "") for p in entry.get("predictions", []) or []
            if isinstance(p, dict)]


def check_r5(entry, registry):
    """(problems, note, resembles) — a dead claim is not re-opened by rewording it.

    `resembles` lists INVALID entries in OTHER scopes that this one resembles: allowed,
    because a kill retires a claim where it was tested, but recorded, never silent.
    """
    if "supersedes" in entry:
        old_id = entry["supersedes"]
        if not isinstance(old_id, str) or old_id not in registry:
            return [f"supersedes {render(old_id)} names no entry in this registry"], "", []
        if not is_invalid(registry[old_id]):
            # Order 41: refused, not noted. A live claim is not superseded, and accepting the
            # citation skipped the resemblance check below: a dead claim reworded could cite
            # any open entry and never be compared with the INVALID one it repeats.
            return [f"supersedes {old_id}, which is not INVALID: only an INVALID claim can "
                    f"be superseded"], "", []
        change = entry.get("structural_change")
        if not isinstance(change, dict):
            return [f"supersedes {old_id}, which is INVALID, so a structural_change "
                    f"{{\"type\": \"mechanism\"|\"instrument\", \"what\": \"...\"}} is "
                    f"required: a dead claim is re-opened by a new mechanism or a new "
                    f"instrument, never by running it again"], "", []
        if change.get("type") not in STRUCTURAL_TYPES:
            return [f"structural_change type {render(change.get('type'))} is not one of "
                    f"{list(STRUCTURAL_TYPES)}: a new parameter, seed, prompt or wording is "
                    f"the same claim run again"], "", []
        what = change.get("what")
        if not isinstance(what, str) or len(what.strip()) < STRUCTURAL_WHAT_MIN:
            return [f"structural_change what must say what changed in at least "
                    f"{STRUCTURAL_WHAT_MIN} characters, got {render(what)}"], "", []
        return [], f"supersedes INVALID {old_id} with a new {change['type']}", []

    mine = scope_of(entry)
    problems, resembles = [], []
    for old_id, old in registry.items():
        if not isinstance(old, dict) or not is_invalid(old):
            continue
        sim = resemblance(entry.get("claim", ""), statements_of(entry),
                          old.get("claim", ""), statements_of(old))
        if sim < R5_THRESHOLD:
            continue
        if scope_of(old) == mine:
            problems.append(f"resembles INVALID {old_id} in the same scope {mine} "
                            f"(similarity {sim:.3f} >= {R5_THRESHOLD}): cite it with "
                            f"\"supersedes\": \"{old_id}\" and declare the structural_change")
        else:
            resembles.append({"id": old_id, "scope": scope_of(old),
                              "similarity": round(sim, 3)})
    note = (f"resembles no INVALID entry in scope {mine}" +
            (f"; resembles {len(resembles)} in other scopes" if resembles else ""))
    return problems, note, resembles


def render(value):
    if isinstance(value, str) and len(value) > 60:
        return json.dumps(value[:57] + "...", ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def render_change(old, new):
    """`old -> new`, showing where they actually differ.

    Two long strings that share their first 57 characters both truncate to the same
    thing, and the message then reads as though nothing changed. A refusal that does
    not show the difference is barely a refusal, so for long strings this shows a
    window around the first character that differs.
    """
    if old == "<absent>" or new == "<absent>" or \
            not (isinstance(old, str) and isinstance(new, str)) or \
            (len(old) <= 60 and len(new) <= 60):
        return (old if old == "<absent>" else render(old),
                new if new == "<absent>" else render(new))
    i = next((k for k in range(min(len(old), len(new))) if old[k] != new[k]),
             min(len(old), len(new)))
    start = max(0, i - 25)

    def window(text):
        chunk = text[start:start + 50]
        return ("..." if start else "") + chunk + ("..." if start + 50 < len(text) else "")

    return json.dumps(window(old), ensure_ascii=False), json.dumps(window(new),
                                                                   ensure_ascii=False)


def die_usage(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(EXIT_USAGE)


def read_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        die_usage(f"cannot read {what} {path}: {exc}")
    except ValueError as exc:
        die_usage(f"{what} {path} is not valid JSON: {exc}")


def predictions_of(registry):
    """[(entry_id, entry, prediction)] in registry order."""
    out = []
    for eid, entry in registry.items():
        for pred in entry.get("predictions", []):
            out.append((eid, entry, pred))
    return out


def log_refusal(verb, target, exit_code, reasons):
    line = (f"{datetime.datetime.now().isoformat(timespec='seconds')} "
            f"exit={exit_code} verb={verb} target={os.path.abspath(target)} "
            f"failed={','.join(reasons) or 'UNSPECIFIED'}\n")
    try:
        with open(REFUSALS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        sys.stderr.write(f"WARNING: could not append to {REFUSALS_LOG}: {exc}\n")


# --------------------------------------------------------------------- ledger

def ledger_path(registry_path):
    return Path(registry_path).resolve().parent / LEDGER_NAME


def read_ledger(registry_path):
    """[(line_number, raw_text, record)] — raw text kept, because the chain is over it."""
    path = ledger_path(registry_path)
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


def append_ledger(registry_path, records):
    """Append, never rewrite. Each line carries the hash of the line before it."""
    path = ledger_path(registry_path)
    existing = read_ledger(registry_path)
    prev = sha256_bytes(existing[-1][1].encode("utf-8")) if existing else GENESIS
    line_no = existing[-1][2]["line"] + 1 if existing and existing[-1][2] else len(existing) + 1
    lines = []
    for rec in records:
        rec = dict(rec, line=line_no, prev=prev,
                   ts=utc_now(),
                   regate=VERSION)
        raw = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        lines.append(raw)
        prev = sha256_bytes(raw.encode("utf-8"))
        line_no += 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(l + "\n" for l in lines))
    return len(lines)


def ledger_record(kind, eid, entry, pred=None, **extra):
    """One line. `pred` is None for an entry-level event such as a status change."""
    frozen = frozen_of(pred) if pred is not None else None
    score = score_of(pred) if pred is not None else None
    ef = entry_frozen_of(entry)
    rec = {
        "kind": kind,
        "entry": eid,
        "id": pred.get("id") if pred is not None else None,
        "frozen": frozen,
        "frozen_sha256": sha256_bytes(canonical(frozen)) if frozen is not None else None,
        "score": score,
        "score_sha256": sha256_bytes(canonical(score)) if score is not None else None,
        "entry_frozen": ef,
        "entry_frozen_sha256": sha256_bytes(canonical(ef)),
        "status": entry.get("status"),
    }
    rec.update(extra)
    return rec


def verify_chain(entries):
    """Every line intact and linked to the one before it. Returns a list of problems."""
    problems, prev = [], GENESIS
    for n, raw, rec in entries:
        if rec is None:
            problems.append(f"LEDGER TAMPERED: line {n} is not valid JSON")
            return problems
        if rec.get("prev") != prev:
            problems.append(f"LEDGER TAMPERED: line {n} does not follow the line before it "
                            f"(chain expected {prev[:12]}, line says "
                            f"{str(rec.get('prev'))[:12]}) — a line was altered or removed")
            return problems
        if rec.get("frozen") is not None and \
                rec.get("frozen_sha256") != sha256_bytes(canonical(rec["frozen"])):
            problems.append(f"LEDGER TAMPERED: line {n} ({rec.get('id')}) does not hash to "
                            f"its own recorded frozen fields")
            return problems
        if rec.get("entry_frozen") is not None and \
                rec.get("entry_frozen_sha256") != sha256_bytes(canonical(rec["entry_frozen"])):
            problems.append(f"LEDGER TAMPERED: line {n} (entry {rec.get('entry')}) does not "
                            f"hash to its own recorded claim")
            return problems
        score = rec.get("score")
        expected = sha256_bytes(canonical(score)) if score is not None else None
        if rec.get("score_sha256") != expected:
            problems.append(f"LEDGER TAMPERED: line {n} ({rec.get('id')}) does not hash to "
                            f"its own recorded score")
            return problems
        prev = sha256_bytes(raw.encode("utf-8"))
    return problems


def narrative_hash(path):
    """sha256 over the prose file's RAW BYTES. A reworded claim is a changed claim."""
    return sha256_bytes(Path(path).read_bytes())


def ledger_state(entries):
    """The last word the ledger has on each prediction, and on each entry."""
    frozen, scores, entry_state = {}, {}, {}
    for _, _, rec in entries:
        eid = rec.get("entry")
        if eid is not None:
            st = entry_state.setdefault(eid, {})
            if rec.get("entry_frozen") is not None:
                st["entry_frozen"] = rec["entry_frozen"]
            st["status"] = rec.get("status")
            if rec.get("narrative_path"):
                st["narrative_path"] = rec["narrative_path"]
                st["narrative_sha256"] = rec["narrative_sha256"]
            if rec.get("kind") == "attach-deps":                   # order 51
                st["depends_on"] = (rec.get("frozen") or {}).get("depends_on")
        pid = rec.get("id")
        if pid is None:
            continue
        frozen[pid] = rec.get("frozen", {})
        if rec.get("score") is not None:
            scores[pid] = rec["score"]
    return frozen, scores, entry_state


# ---------------------------------------------------------------------- check

def run_check(registry_path):
    """[] when the registry still says what the ledger says it said."""
    registry = read_json(registry_path, "registry")
    entries = read_ledger(registry_path)
    problems = verify_chain(entries)
    if problems:
        problems.append("the ledger cannot be trusted, so nothing else was compared "
                        "against it")
        return problems
    recorded, scored, entry_state = ledger_state(entries)
    live = {pred.get("id"): pred for _, _, pred in predictions_of(registry)}

    for eid, entry in registry.items():
        st = entry_state.get(eid)
        if st is None:
            continue                      # its predictions are reported UNRECORDED already
        for field in ENTRY_FROZEN_FIELDS:
            was = (st.get("entry_frozen") or {}).get(field, "<absent>")
            now = entry.get(field, "<absent>")
            if was != now:
                a, b = render_change(was, now)
                problems.append(f"GOALPOST MOVED: {eid} field {field} {a} -> {b}")
        # Order 51: the declared edges are frozen by their attach-deps line, not by the list above.
        was, now = st.get("depends_on", "<absent>"), entry.get("depends_on", "<absent>")
        if was != now:
            a, b = render_change(was, now)
            problems.append(f"GOALPOST MOVED: {eid} field depends_on {a} -> {b}")
        raw_path = st.get("narrative_path")
        if raw_path:
            path = resolve_prereg(raw_path, registry_path)     # same relative-to-registry rule
            if not path.is_file():
                problems.append(f"NARRATIVE MISSING: {eid} {raw_path} — the prose the entry "
                                f"points at is not there any more")
            else:
                now = narrative_hash(path)
                if now != st.get("narrative_sha256"):
                    problems.append(
                        f"NARRATIVE CHANGED OFF-LEDGER: {eid} {raw_path} "
                        f"{str(st.get('narrative_sha256'))[:12]} -> {now[:12]} — a reworded "
                        f"claim goes through `regate.py revise-narrative` so the record has it")
        if st.get("status") != entry.get("status") and \
                str(st.get("status") or "").startswith(INVALID) and not is_invalid(entry):
            problems.append(f"GOALPOST MOVED: {eid} status left INVALID "
                            f"{render(st.get('status'))} -> {render(entry.get('status'))} "
                            f"— INVALID is final; a dead claim comes back as a new entry "
                            f"that supersedes it with a structural change")
        elif st.get("status") != entry.get("status"):
            problems.append(f"STATUS CHANGED OFF-LEDGER: {eid} status "
                            f"{render(st.get('status'))} -> {render(entry.get('status'))} "
                            f"— a scope cut is legitimate, but it goes through "
                            f"`regate.py status` so the record has it")

    for pid in recorded:
        if pid not in live:
            problems.append(f"DELETED: {pid} is in the ledger and not in the registry")
    for pid, pred in live.items():
        if pid not in recorded:
            problems.append(f"UNRECORDED: {pid} is in the registry with no ledger line — "
                            f"it was added by hand, around this gate")
            continue
        was, now = recorded[pid], frozen_of(pred)
        if sha256_bytes(canonical(was)) != sha256_bytes(canonical(now)):
            for field in FROZEN_FIELDS:
                old = was.get(field, "<absent>")
                new = now.get(field, "<absent>")
                if old != new:
                    a, b = render_change(old, new)
                    problems.append(f"GOALPOST MOVED: {pid} field {field} {a} -> {b}")
        if pid in scored:

            now_score = score_of(pred)
            if now_score is None:
                problems.append(f"SCORE REWRITTEN: {pid} was scored and the score is now gone")
            elif sha256_bytes(canonical(scored[pid])) != sha256_bytes(canonical(now_score)):
                for field in SCORE_FIELDS:
                    old, new = scored[pid].get(field, "<absent>"), now_score.get(field, "<absent>")
                    if old != new:
                        a, b = render_change(old, new)
                        problems.append(f"SCORE REWRITTEN: {pid} field {field} {a} -> {b}")
    return problems


# ------------------------------------------- order 51: the provenance DAG (item 23)

def read_jsonl_file(path):
    """([(line number, record)], problem or None) — a line that is not a JSON object is a
    problem, never skipped: the caller fails closed on it."""
    path = Path(path)
    if not path.exists():
        return [], None
    out = []
    for n, raw in enumerate(path.read_bytes().decode("utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            return out, f"{path.name} unreadable at line {n}"
        out.append((n, rec))
    return out, None


def swaps_chain_problem(path):
    """seatswap's chain rule (prev = sha256 of the line before, seq counts up), re-read here as a
    file: regate never imports seatswap. None when intact or absent."""
    path = Path(path)
    if not path.exists():
        return None
    prev, seq = GENESIS, 0
    for n, raw in enumerate(path.read_bytes().decode("utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        seq += 1
        try:
            rec = json.loads(raw)
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or rec.get("prev") != prev or rec.get("seq") != seq:
            return f"{path.name} chain broken at line {n}"
        prev = sha256_bytes(raw.encode("utf-8"))
    return None


def pack_names():
    """(names in PACKS.jsonl, problem or None)."""
    recs, problem = read_jsonl_file(PACKS)
    return {r.get("name") for _, r in recs}, problem


def pack_verdicts():
    """A function pack name -> why its edge is dead ("MISMATCH", "DISAGREE", ...) or None.
    The LATEST replay and the LATEST seat swap decide, so a pack recovers when it matches or
    agrees again. A ledger that cannot be read kills every pack edge."""
    replays, r_problem = read_jsonl_file(REPLAYS)
    swaps, s_problem = read_jsonl_file(SWAPS)
    s_problem = s_problem or swaps_chain_problem(SWAPS)
    last_replay, last_swap = {}, {}
    for _, rec in replays:
        if rec.get("name") is not None:
            last_replay[rec["name"]] = rec.get("result")
    for _, rec in swaps:
        if rec.get("kind") == "swap" and rec.get("pack") is not None:
            last_swap[rec["pack"]] = rec.get("outcome")

    def dead(name):
        if r_problem or s_problem:
            return r_problem or s_problem
        why = [w for w in (last_replay.get(name) if last_replay.get(name) in DEAD_REPLAYS
                           else None,
                           DEAD_SWAP if last_swap.get(name) == DEAD_SWAP else None) if w]
        return " + ".join(why) or None
    return dead


def find_cycle(deps, start):
    """The first path start → ... → start along declared entry edges, as a list, or None."""
    def walk(node, path):
        for nxt in deps.get(node) or []:
            if nxt.startswith(PACK_EDGE):
                continue
            if nxt == start:
                return path + [nxt]
            if nxt in path:
                continue
            found = walk(nxt, path + [nxt])
            if found:
                return found
        return None
    return walk(start, [start])


def provenance(registry_path):
    """Everything the DAG says, computed now from the registry and the ledgers; reads only.
    {"entries", "edges", "undeclared", "deps", "quarantined": {id: path}, "cycles", "problem"}.
    Edges come from the attach-deps LEDGER lines (the frozen record); status from the registry,
    or from the ledger if that says INVALID (fail closed either way)."""
    out = {"entries": [], "edges": 0, "undeclared": [], "deps": {}, "quarantined": {},
           "cycles": [], "problem": None}
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["problem"] = f"registry unreadable: {exc}"
        return out
    entries = read_ledger(registry_path)
    broken = verify_chain(entries)
    if broken:
        out["problem"] = broken[0]
        return out
    _, _, state = ledger_state(entries)
    deps = {eid: list(st["depends_on"]) for eid, st in state.items()
            if isinstance(st.get("depends_on"), list)}
    out.update(entries=list(registry), deps=deps,
               edges=sum(len(deps.get(eid, [])) for eid in registry),
               undeclared=[eid for eid in registry if eid not in deps])

    def invalid(eid):
        return (isinstance(registry.get(eid), dict) and is_invalid(registry[eid])) or \
            str((state.get(eid) or {}).get("status") or "").startswith(INVALID)

    pack_dead = pack_verdicts()
    memo = {}

    def dead_path(eid, stack):
        """The first dead path upstream of eid, as `eid ← ... WHY`, or None."""
        if eid in memo:
            return memo[eid]
        found = None
        for edge in deps.get(eid) or []:
            if edge.startswith(PACK_EDGE):
                why = pack_dead(edge[len(PACK_EDGE):])
                found = f"{eid} ← {edge} {why}" if why else None
            elif edge in stack:
                found = f"{eid} ← {edge} CYCLE"
            elif edge not in registry:
                found = f"{eid} ← {edge} MISSING"
            elif invalid(edge):
                found = f"{eid} ← {edge} INVALID"
            else:
                up = dead_path(edge, stack | {edge})
                found = f"{eid} ← {up}" if up else None
            if found:
                break
        memo[eid] = found
        return found

    for eid in registry:
        path = dead_path(eid, {eid})
        if path:
            out["quarantined"][eid] = path
    seen = set()
    for eid in sorted(deps):
        cyc = find_cycle(deps, eid)
        if cyc and frozenset(cyc) not in seen:
            seen.add(frozenset(cyc))
            out["cycles"].append(" → ".join(cyc))
    return out


def quarantine_result(eid, prov):
    """The A_NOT_QUARANTINED / S8_NOT_QUARANTINED result for one entry (same wording in both)."""
    if prov["problem"]:
        return ("failed", f"provenance cannot be computed: {prov['problem']}")
    path = prov["quarantined"].get(eid)
    if path:
        return ("failed", f"{eid} is quarantined: {path}. An upstream claim is dead; a dead "
                          f"claim comes back as a new entry that supersedes it, never by "
                          f"editing the edge")
    if eid in prov["undeclared"]:
        return ("ok", f"{eid} has no dependencies declared (legacy), so nothing upstream")
    return ("ok", f"nothing upstream of {eid} is dead "
                  f"({len(prov['deps'].get(eid, []))} edge(s) declared)")


def plural(n, word, words):
    return f"{n} {word if n == 1 else words}"


def provenance_line(registry_path):
    """The nightly's `- provenance:` line, with 🔴 sub-lines for each quarantined path and any
    cycle. Reads only."""
    if not Path(registry_path).exists():
        return "- provenance: no registry.json"
    p = provenance(registry_path)
    if p["problem"]:
        return f"- provenance: 🔴 {p['problem']}"
    cycles = "no cycles" if not p["cycles"] else f"🔴 {plural(len(p['cycles']), 'cycle', 'cycles')}"
    lines = [f"- provenance: {plural(len(p['entries']), 'entry', 'entries')} · "
             f"{plural(p['edges'], 'edge', 'edges')} · {len(p['undeclared'])} undeclared · "
             f"{len(p['quarantined'])} quarantined · {cycles}"]
    lines += [f"  - 🔴 QUARANTINED: {path}" for path in p["quarantined"].values()]
    lines += [f"  - 🔴 cycle: {c}" for c in p["cycles"]]
    return "\n".join(lines)


# ------------------------------------------------------------- writing safely

def write_registry(registry_path, registry):
    """Temp file then rename, so the registry is never half-written.

    `indent=1, ensure_ascii=False` is the format `seam/nightly.py` writes, so a file
    this tool touches does not come back as a whole-file diff.
    """
    path = Path(registry_path)
    tmp = path.with_name(path.name + ".regate-partial")
    text = json.dumps(registry, indent=1, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def refuse(verb, target, results, extra=""):
    sys.stderr.write(f"REFUSED: {os.path.abspath(target)}\n")
    order = PRECONDITION_ORDER[verb]
    for name in order:
        status, detail = results[name]
        label = {"ok": "ok      ", "failed": "FAILED  ",
                 "not-evaluated": "not-evaluated "}[status]
        sys.stderr.write(f"{label} {name}: {detail}\n")
    if extra:
        sys.stderr.write(extra)
    failed = [n for n in order if results[n][0] == "failed"]
    sys.stderr.write(f"refused on {len(failed)} precondition(s): {', '.join(failed)}\n")
    log_refusal(verb, target, EXIT_REFUSED, failed)
    return EXIT_REFUSED


# ----------------------------------------------------------------- the verbs

def verb_baseline(args):
    registry = read_json(args.registry, "registry")
    preds = predictions_of(registry)
    if not preds:
        die_usage(f"{args.registry} holds no predictions to record")
    n = append_ledger(args.registry, [ledger_record("baseline", eid, entry, pred)
                                      for eid, entry, pred in preds])
    sys.stdout.write(f"BASELINE RECORDED: {ledger_path(args.registry)}\n")
    for eid, _, pred in preds:
        frozen = frozen_of(pred)
        sys.stdout.write(f"  {pred.get('id'):<10} {sha256_bytes(canonical(frozen))[:12]}  "
                         f"frozen fields: {', '.join(sorted(frozen))}"
                         f"{'  [already scored]' if score_of(pred) else ''}\n")
    sys.stdout.write(f"{n} line(s) appended. The goalposts above are now fixed; "
                     f"`check` reports any that move.\n")
    return EXIT_OK


def verb_check(args):
    problems = run_check(args.registry)
    if problems:
        sys.stderr.write(f"CHECK FAILED: {os.path.abspath(args.registry)}\n")
        for p in problems:
            sys.stderr.write(p + "\n")
        sys.stderr.write("a prediction that can be edited after it is registered is not "
                         "a prediction\n")
        log_refusal("check", args.registry, EXIT_CHECK,
                    sorted({p.split(":")[0] for p in problems if ":" in p}))
        return EXIT_CHECK
    entries = read_ledger(args.registry)
    _, _, state = ledger_state(entries)
    narratives = sum(1 for v in state.values() if v.get("narrative_path"))
    preds = predictions_of(read_json(args.registry, "registry"))
    legacy = sum(1 for _, _, pred in preds if "kill_rule" not in pred)
    # `, N legacy prose kills` goes LAST: nightly.registry_line() matches
    # "(\d+) prediction\(s\), (\d+) narrative\(s\)" and must keep matching.
    sys.stdout.write(f"OK: {os.path.abspath(args.registry)} still says what the ledger "
                     f"says it said\n")
    # order 27: the number prints itself next to every scored claim. Before the summary,
    # which stays the last line (and the only one nightly.registry_line() matches).
    for _, _, pred in preds:
        if score_of(pred) is not None:
            sys.stdout.write(f"  {pred.get('id')} cycle: {cycle_text(pred)}\n")
            sys.stdout.write(f"  {pred.get('id')} K: {k_text(pred)}\n")      # order 28
    # Order 51: quarantine is computed here, now, and never written. Not drift (exit 0), but every
    # quarantined entry is named with its path, and every seal resting on one is dropped (R4 side;
    # sealgate itself knows nothing about the registry).
    registry = read_json(args.registry, "registry")
    for eid, path in provenance(args.registry)["quarantined"].items():
        sys.stdout.write(f"  QUARANTINED: {path}\n")
        for raw in prereg_paths(registry.get(eid) or {}):
            sys.stdout.write(f"  seal dropped: {raw} (entry quarantined: {path})\n")
    sys.stdout.write(f"  {len(preds)} prediction(s), {narratives} narrative(s), "
                     f"{len(entries)} ledger line(s), chain intact, {legacy} legacy prose "
                     f"kills\n")
    return EXIT_OK


def check_r2(entry, entry_id):
    """Every prediction complete, p in (0,1), due after today, kill non-empty, a
    well-formed kill_rule; and the entry names its scope."""
    problems = []
    if "scope" not in entry:
        problems.append("the entry has no scope: name the project/domain it is tested in, "
                        "e.g. \"crosscheck/attest\", since a kill retires a claim there and "
                        "nowhere else")
    elif scope_problem(entry["scope"]):
        problems.append(scope_problem(entry["scope"]))
    preds = entry.get("predictions")
    if not isinstance(preds, list) or not preds:
        return problems + ["the entry has no predictions, so there is nothing to freeze"]
    for i, pred in enumerate(preds):
        where = pred.get("id") or f"predictions[{i}]"
        for field in ("id", "kind", "p", "due", "metric", "statement", "kill", "kill_rule"):
            if field not in pred:
                problems.append(f"{where}: missing required field {field!r}")
        if "kind" in pred:
            if pred["kind"] not in KINDS:
                problems.append(f"{where}: kind {render(pred['kind'])} is not one of "
                                f"{list(KINDS)}")
            elif pred["kind"] == "experiment" and not pred.get("prereg"):
                problems.append(f"{where}: an experiment must name a prereg when it is "
                                f"registered, so that scoring it early can be checked "
                                f"against a seal")
        if "id" in pred and not re.match(rf"^{re.escape(entry_id)}-p\d+$", str(pred["id"])):
            problems.append(f"{where}: id must be {entry_id}-p<n>, since the ledger files "
                            f"predictions by id and this one would be filed under another "
                            f"entry")
        if "p" in pred:
            p = pred["p"]
            if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 < p < 1:
                problems.append(f"{where}: p must be a probability strictly inside (0,1), "
                                f"got {render(p)}")
        if "due" in pred:
            due = pred["due"]
            if not isinstance(due, str) or not ISO_DATE.match(due):
                problems.append(f"{where}: due must be an ISO date, got {render(due)}")
            elif due <= today():
                problems.append(f"{where}: due {due} is not after today ({today()}); "
                                f"a prediction that is already due is not a prediction")
        if "kill" in pred and not str(pred["kill"]).strip():
            problems.append(f"{where}: kill is empty; a prediction with no kill condition "
                            f"cannot fail")
        if "statement" in pred and not str(pred["statement"]).strip():
            problems.append(f"{where}: statement is empty")
        if "kill_rule" in pred and kill_rule_problem(pred["kill_rule"]):
            problems.append(f"{where}: {kill_rule_problem(pred['kill_rule'])}")
    return problems


def prereg_paths(entry):
    paths = []
    if entry.get("prereg"):
        paths.append(entry["prereg"])
    for pred in entry.get("predictions", []) or []:
        if isinstance(pred, dict) and pred.get("prereg"):
            paths.append(pred["prereg"])
    return paths


def check_r4(entry, registry_path):
    """Any prereg the entry names must pass `sealgate.py verify`."""
    paths = prereg_paths(entry)
    if not paths:
        return [], "no prereg named by this entry"
    problems, notes = [], []
    for raw in paths:
        path = resolve_prereg(raw, registry_path)
        ok, out = sealgate_verify(path)
        if ok:
            notes.append(f"sealgate verify {path} -> exit 0")
        else:
            problems.append(f"sealgate verify {path} did not pass:")
            problems.extend("    " + l for l in out[:4])
    return problems, "; ".join(notes)


def verb_register(args):
    registry = read_json(args.registry, "registry")
    payload = read_json(args.entry, "entry")
    results = {}

    if not isinstance(payload, dict) or len(payload) != 1:
        die_usage(f"{args.entry} must be a JSON object with exactly one key, the new entry "
                  f"id (as the registry itself is keyed)")
    new_id, entry = next(iter(payload.items()))
    if not isinstance(entry, dict):
        die_usage(f"{args.entry}: entry {new_id!r} must be an object")

    # R1 — new, and the next number
    existing = sorted(k for k in registry if ENTRY_ID.match(k))
    expected = f"{int(existing[-1]) + 1:04d}" if existing else "0000"
    if new_id in registry:
        results["R1_ID_IS_NEXT"] = ("failed", f"entry {new_id} already exists in the registry")
    elif not ENTRY_ID.match(new_id) or new_id != expected:
        results["R1_ID_IS_NEXT"] = ("failed", f"entry id {new_id!r} is not the next in "
                                              f"sequence (expected {expected!r})")
    else:
        results["R1_ID_IS_NEXT"] = ("ok", f"{new_id} is new and follows {existing[-1]}")

    # R2 — the prediction is a prediction
    r2 = check_r2(entry, new_id)
    results["R2_PREDICTION_COMPLETE"] = (("failed", "; ".join(r2)) if r2 else
                                         ("ok", f"{len(entry['predictions'])} prediction(s), "
                                                f"each with a kill condition and a future due "
                                                f"date"))

    # R3 — you cannot add to a registry that has already drifted
    drift = run_check(args.registry)
    results["R3_REGISTRY_UNDRIFTED"] = (("failed", f"{len(drift)} problem(s) in the registry "
                                                   f"as it stands:\n         "
                                         + "\n         ".join(drift)) if drift else
                                        ("ok", "the registry still matches its ledger"))

    # R4 — any prereg it names must verify
    r4, note = check_r4(entry, args.registry)
    results["R4_PREREG_VERIFIES"] = (("failed", "\n         ".join(r4)) if r4
                                     else ("ok", note))

    # R5 — a dead claim is not re-opened by rewording it (same scope only)
    r5, note, resembles = check_r5(entry, registry)
    results["R5_NOT_A_RESURRECTION"] = (("failed", "\n         ".join(r5)) if r5
                                        else ("ok", note))

    if any(v[0] == "failed" for v in results.values()):
        return refuse("register", args.registry, results)

    registry[new_id] = entry
    write_registry(args.registry, registry)
    append_ledger(args.registry, [ledger_record("register", new_id, entry, pred,
                                                kill_prose=pred["kill_rule"] == PROSE_RULE,
                                                resembles_invalid=resembles)
                                  for pred in entry["predictions"]])
    sys.stdout.write(f"REGISTERED: {new_id} in {os.path.abspath(args.registry)}\n")
    for name in REGISTER_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    for pred in entry["predictions"]:
        sys.stdout.write(f"  {pred['id']:<10} frozen at "
                         f"{sha256_bytes(canonical(frozen_of(pred)))[:12]}, due {pred['due']}\n")
        if pred["kill_rule"] == PROSE_RULE:
            sys.stdout.write(f"PROSE KILL: {pred['id']} (not auto-evaluable)\n")
    for r in resembles:
        sys.stdout.write(f"RESEMBLES INVALID {r['id']} (scope {r['scope']}): allowed, "
                         f"different scope (similarity {r['similarity']})\n")
    return EXIT_OK


def verb_score(args):
    registry = read_json(args.registry, "registry")
    results = {}
    target = None
    for eid, entry, pred in predictions_of(registry):
        if pred.get("id") == args.id:
            target = (eid, entry, pred)
            break

    drift = run_check(args.registry)
    if target is None:
        results["S1_EXISTS_AND_UNDRIFTED"] = ("failed", f"no prediction {args.id!r} in this "
                                                        f"registry")
    elif drift:
        results["S1_EXISTS_AND_UNDRIFTED"] = ("failed", f"{len(drift)} problem(s) in the "
                                              f"registry as it stands:\n         "
                                              + "\n         ".join(drift))
    else:
        results["S1_EXISTS_AND_UNDRIFTED"] = ("ok", f"{args.id} exists and the registry "
                                                    f"matches its ledger")

    if target is None:
        for name in SCORE_PRECONDITIONS[1:]:
            results[name] = ("not-evaluated", "depends on S1_EXISTS_AND_UNDRIFTED")
    else:
        pred = target[2]
        due = pred.get("due", "")
        kind, kind_note = kind_of(pred)
        early = ISO_DATE.match(str(due)) and today() < due
        if not ISO_DATE.match(str(due)):
            results["S2_DUE_HAS_ARRIVED"] = ("failed", f"due {render(due)} is not an ISO date")
        elif early and kind == "time":
            results["S2_DUE_HAS_ARRIVED"] = ("failed", f"due {due} has not arrived "
                                                       f"(today is {today()}); this is a time "
                                                       f"prediction{kind_note}, so the date is "
                                                       f"the claim and scoring early is "
                                                       f"scoring something you can still "
                                                       f"change")
        elif early:
            results["S2_DUE_HAS_ARRIVED"] = ("ok", f"due {due} has not arrived, but this is an "
                                                   f"experiment, where due means run-by; "
                                                   f"S5 decides")
        else:
            results["S2_DUE_HAS_ARRIVED"] = ("ok", f"due {due}, today {today()}")

        if not early:
            results["S5_EXPERIMENT_SEALED"] = ("ok", "due has arrived; no seal needed to score")
        elif kind == "time":
            results["S5_EXPERIMENT_SEALED"] = ("not-evaluated", "only an experiment may be "
                                                               "scored before its due date")
        else:
            raw = pred.get("prereg")
            if not raw:
                results["S5_EXPERIMENT_SEALED"] = (
                    "failed", f"{args.id} is an experiment being scored before its due date, "
                              f"but it names no prereg; attach one with `attach-prereg` "
                              f"(which refuses unless sealgate verify passes)")
            else:
                ok, out = sealgate_verify(resolve_prereg(raw, args.registry))
                results["S5_EXPERIMENT_SEALED"] = (
                    ("ok", f"sealgate verify {raw} -> exit 0") if ok else
                    ("failed", f"the prereg {raw} no longer verifies: {out[0]}"))

        if score_of(pred) is not None:
            results["S3_NOT_ALREADY_SCORED"] = (
                "failed", f"{args.id} was scored on {pred.get('scored')} with outcome "
                          f"{render(pred.get('outcome'))}. A written score is never "
                          f"overwritten — not by a flag, not by this tool")
        else:
            results["S3_NOT_ALREADY_SCORED"] = ("ok", "not scored yet")

        if args.outcome == "none" and not args.note.startswith("UNDERPOWERED:"):
            results["S4_NONE_NEEDS_UNDERPOWERED"] = (
                "failed", "outcome none needs a note starting 'UNDERPOWERED:' saying what "
                          "was not there, as nightly.py writes it")
        else:
            results["S4_NONE_NEEDS_UNDERPOWERED"] = ("ok", f"outcome {args.outcome}")

        rule = pred.get("kill_rule")
        if isinstance(rule, dict) and rule.get("on") == "value":
            if args.value is None:
                results["S6_VALUE_MATCHES_RULE"] = (
                    "failed", f"{args.id}'s kill_rule is on the measured value "
                              f"({describe_rule(rule)}), so --value is required")
            elif not math.isfinite(args.value):
                results["S6_VALUE_MATCHES_RULE"] = (
                    "failed", f"--value {args.value} is not a finite number")
            else:
                results["S6_VALUE_MATCHES_RULE"] = ("ok", f"value {render(args.value)}")
        elif args.value is not None:
            results["S6_VALUE_MATCHES_RULE"] = (
                "failed", f"--value is only for a prediction whose kill_rule is on value; "
                          f"{args.id}'s is {describe_rule(rule)}")
        else:
            results["S6_VALUE_MATCHES_RULE"] = ("ok", "no value needed")

        # Order 49 (item 10): same code, different mind reading it, before final scoring.
        if kind != "experiment":
            results["S7_SEAT_SWAP"] = ("ok", f"time prediction{kind_note}, no analysis to swap")
        elif not pred.get("pack"):
            results["S7_SEAT_SWAP"] = (
                "failed", f"{args.id} is an experiment with no replay pack attached; attach one "
                          f"with `attach-pack`, then `seatswap.py run <pack>` until it AGREEs")
        else:
            swap = seatswap_check(pred["pack"])
            results["S7_SEAT_SWAP"] = (
                ("ok", f"seat swap AGREE for pack {pred['pack']} (manifest "
                       f"{str(swap.get('manifest_sha256'))[:12]})") if swap["ok"] else
                ("failed", f"no intact seat-swap AGREE for pack {pred['pack']}'s current "
                           f"manifest: outcome {swap.get('outcome')} — {swap.get('why')}"))

        # Order 51 (item 23): nothing upstream of the claim may be dead. Computed now, never stored.
        prov = provenance(args.registry)
        results["S8_NOT_QUARANTINED"] = (
            ("not-evaluated", "the ledger cannot be trusted (S1)") if prov["problem"] else
            quarantine_result(target[0], prov))

        # Order 60 (P-6): an early score needs a run logged after the seal, so a result known
        # before sealing cannot be sealed and scored minutes later (catch #7).
        if not (early and kind == "experiment"):
            results["S9_RUN_AFTER_SEAL"] = ("ok", "not an early experiment score")
        elif results["S5_EXPERIMENT_SEALED"][0] == "failed":
            results["S9_RUN_AFTER_SEAL"] = ("not-evaluated", "depends on S5_EXPERIMENT_SEALED")
        else:
            results["S9_RUN_AFTER_SEAL"] = run_after_seal(pred, target[1], args.registry)

    if any(v[0] == "failed" for v in results.values()):
        return refuse("score", args.registry, results)

    eid, entry, pred = target
    if args.outcome == "none":
        pred.update(scored=today(), outcome=None, brier=None, note=args.note)
    else:
        outcome = int(args.outcome)
        pred.update(scored=today(), outcome=outcome,
                    brier=round((pred["p"] - outcome) ** 2, 4), note=args.note)
    if args.value is not None:
        pred["value"] = args.value
    pred["cycle"] = measure_cycle(*cycle_start(pred, args.registry))
    pred["k"] = measure_k(scope_of(entry), pred["cycle"]["end"])   # order 28: same `until`
    records = [ledger_record("score", eid, entry, pred)]

    # The falsifier has a consequence: a met structured kill sets the entry INVALID in
    # this same call — the score and the status land in one registry write, or neither.
    # Only this entry: INVALID is local to its scope and never propagates.
    rule, verdict = pred.get("kill_rule"), None
    if args.outcome == "none":
        verdict = "UNDERPOWERED — kill not evaluated: an underpowered score is not evidence"
    elif not isinstance(rule, dict):
        verdict = "PROSE KILL — not evaluated by regate; decide by hand via status"
    else:
        actual = pred["outcome"] if rule["on"] == "outcome" else pred["value"]
        if not KILL_OPS[rule["op"]](actual, rule["value"]):
            verdict = f"kill not met: {rule['on']} {render(actual)} {rule['op']} " \
                      f"{render(rule['value'])} is false"
        elif is_invalid(entry):
            verdict = f"kill met, and {eid} is already INVALID; its status stays as it was"
        else:
            old_status = entry.get("status")
            entry["status"] = (f"{INVALID} — killed by {args.id}: {rule['on']} "
                               f"{render(actual)} {rule['op']} {render(rule['value'])} "
                               f"(regate, {today()})")
            records.append(ledger_record("auto_invalid", eid, entry, pred,
                                         old_status=old_status, new_status=entry["status"],
                                         rule=rule, actual=actual))
            verdict = f"AUTO-INVALID: {eid} status {render(old_status)} -> " \
                      f"{json.dumps(entry['status'], ensure_ascii=False)}"

    write_registry(args.registry, registry)
    append_ledger(args.registry, records)
    sys.stdout.write(f"SCORED: {args.id} outcome {render(pred['outcome'])}, "
                     f"brier {render(pred['brier'])} (computed here from p="
                     f"{render(pred['p'])}, never supplied)\n")
    for name in SCORE_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    sys.stdout.write(verdict + "\n")
    sys.stdout.write(f"cycle: {pred['cycle']['line']}\n")
    sys.stdout.write(f"{pred['k']['line']}\n")
    for j in pred["k"].get("unlogged_jobs", []):
        sys.stdout.write(f"  unlogged GPU job: pid {j['pid']}  first seen {j['first_seen']}  "
                         f"cwd {j['cwd']}  {str(j['cmd'])[:120]}\n")
    return EXIT_OK


def verb_attach_prereg(args):
    """Set a prediction's prereg, once, and only while sealgate verifies it.

    An experiment can be registered before it is sealed — 0004-p1 is exactly that —
    so this is the sanctioned way for `prereg` to go from absent to a path. Absent to
    a path is the ONLY move: a path to a different path is a moved goalpost.
    """
    registry = read_json(args.registry, "registry")
    results, target = {}, None
    for eid, entry, pred in predictions_of(registry):
        if pred.get("id") == args.id:
            target = (eid, entry, pred)
            break
    drift = run_check(args.registry)
    if target is None:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"no prediction {args.id!r} in this registry")
    elif drift:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"{len(drift)} problem(s) in the registry as it stands:\n         "
                      + "\n         ".join(drift))
    else:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "ok", f"{args.id} exists and the registry matches its ledger")

    if target is None:
        for name in ATTACH_PRECONDITIONS[1:]:
            results[name] = ("not-evaluated", "depends on A1_PREDICTION_EXISTS_AND_UNDRIFTED")
    else:
        pred = target[2]
        if pred.get("prereg"):
            results["A2_PREREG_SET_ONCE"] = (
                "failed", f"{args.id} already names the prereg {render(pred['prereg'])}. A "
                          f"prereg is set once; pointing a registered prediction at a "
                          f"different seal is moving the goalpost")
        else:
            results["A2_PREREG_SET_ONCE"] = ("ok", "no prereg set yet")

        ok, out = sealgate_verify(resolve_prereg(args.prereg, args.registry))
        results["A3_PREREG_VERIFIES"] = (
            ("ok", f"sealgate verify {args.prereg} -> exit 0") if ok else
            ("failed", f"sealgate verify {args.prereg} did not pass: {out[0]}"))
        results["A_NOT_QUARANTINED"] = quarantine_result(target[0],
                                                         provenance(args.registry))  # order 51

    if any(v[0] == "failed" for v in results.values()):
        return refuse("attach-prereg", args.registry, results)

    eid, entry, pred = target
    pred["prereg"] = str(args.prereg)
    write_registry(args.registry, registry)
    append_ledger(args.registry, [ledger_record("attach-prereg", eid, entry, pred)])
    sys.stdout.write(f"PREREG ATTACHED: {args.id} -> {args.prereg}\n")
    for name in ATTACH_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    sys.stdout.write(f"  {args.id} re-frozen at "
                     f"{sha256_bytes(canonical(frozen_of(pred)))[:12]}\n")
    return EXIT_OK


def verb_status(args):
    """Change an entry's status and leave the change in the ledger.

    `status` is not frozen — a scope cut after a cold review is legitimate, and #0002
    and #0003 both did it. What was missing is any trace of when it changed and what
    it said before.
    """
    registry = read_json(args.registry, "registry")
    results = {}
    entry = registry.get(args.entry)
    drift = run_check(args.registry)
    if entry is None:
        results["T1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"no entry {args.entry!r} in this registry")
    elif drift:
        results["T1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"{len(drift)} problem(s) in the registry as it stands:\n         "
                      + "\n         ".join(drift))
    else:
        results["T1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "ok", f"entry {args.entry} exists and the registry matches its ledger")
    if not args.text.strip():
        results["T2_STATUS_NOT_EMPTY"] = ("failed", "a status of nothing records nothing")
    else:
        results["T2_STATUS_NOT_EMPTY"] = ("ok", f"{len(args.text)} characters")
    if entry is not None and is_invalid(entry):
        results["R_INVALID_IS_FINAL"] = (
            "failed", f"entry {args.entry} is {render(entry.get('status'))}. INVALID is "
                      f"final: a dead claim comes back as a new entry that supersedes it "
                      f"with a structural change, never by rewriting its status")
    else:
        results["R_INVALID_IS_FINAL"] = ("ok", "the entry is not INVALID")

    if any(v[0] == "failed" for v in results.values()):
        return refuse("status", args.registry, results)

    previous = entry.get("status")
    entry["status"] = args.text
    write_registry(args.registry, registry)
    append_ledger(args.registry, [ledger_record("status", args.entry, entry,
                                                prev_status=previous)])
    sys.stdout.write(f"STATUS RECORDED: {args.entry}\n"
                     f"  was: {render(previous)}\n"
                     f"  now: {render(args.text)}\n")
    for name in STATUS_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    return EXIT_OK


def verb_attach_narrative(args):
    """Bind an entry's prose file to the ledger by path and content hash, once.

    The narrative is recorded in the LEDGER, never in the registry JSON: the JSON is
    the frozen claim, and this is a statement about a file beside it.
    """
    registry = read_json(args.registry, "registry")
    results = {}
    entry = registry.get(args.entry)
    drift = run_check(args.registry)
    entries = read_ledger(args.registry)
    _, _, state = ledger_state(entries)

    if entry is None:
        results["N1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"no entry {args.entry!r} in this registry")
    elif drift:
        results["N1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"{len(drift)} problem(s) in the registry as it stands:\n         "
                      + "\n         ".join(drift))
    else:
        results["N1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "ok", f"entry {args.entry} exists and the registry matches its ledger")

    already = (state.get(args.entry) or {}).get("narrative_path")
    if already:
        results["N2_NARRATIVE_SET_ONCE"] = (
            "failed", f"entry {args.entry} already has the narrative {render(already)}. A "
                      f"narrative is set once; pointing an entry at a different prose file "
                      f"is not an edit, it is a substitution")
    else:
        results["N2_NARRATIVE_SET_ONCE"] = ("ok", "no narrative attached yet")

    path = resolve_prereg(args.narrative, args.registry)
    if not path.is_file():
        results["N3_NARRATIVE_EXISTS"] = ("failed", f"{args.narrative} does not exist "
                                                    f"(looked at {path})")
    else:
        results["N3_NARRATIVE_EXISTS"] = ("ok", f"{path}, {path.stat().st_size} bytes, "
                                                f"sha256 {narrative_hash(path)[:12]}")

    if any(v[0] == "failed" for v in results.values()):
        return refuse("attach-narrative", args.registry, results)

    append_ledger(args.registry, [ledger_record(
        "narrative", args.entry, entry,
        narrative_path=str(args.narrative), narrative_sha256=narrative_hash(path))])
    sys.stdout.write(f"NARRATIVE ATTACHED: {args.entry} -> {args.narrative}\n"
                     f"  sha256 {narrative_hash(path)}\n"
                     f"  recorded in the ledger; {os.path.basename(args.registry)} unchanged\n")
    for name in NARRATIVE_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    return EXIT_OK


def verb_revise_narrative(args):
    """Record that an entry's prose was deliberately edited, and why.

    This is the one verb that must work while `check` is failing, because the thing it
    records is exactly what `check` is complaining about. It tolerates that one
    mismatch and nothing else.
    """
    registry = read_json(args.registry, "registry")
    results = {}
    entry = registry.get(args.entry)
    entries = read_ledger(args.registry)
    _, _, state = ledger_state(entries)
    recorded = (state.get(args.entry) or {}).get("narrative_path")

    if entry is None or not recorded:
        results["V1_NARRATIVE_ATTACHED"] = (
            "failed", f"entry {args.entry!r} has no narrative attached; use "
                      f"`attach-narrative` first")
    else:
        results["V1_NARRATIVE_ATTACHED"] = ("ok", f"{recorded}")

    path = resolve_prereg(recorded, args.registry) if recorded else None
    mine = (f"NARRATIVE CHANGED OFF-LEDGER: {args.entry} {recorded}" if recorded else None)
    others = [d for d in run_check(args.registry)
              if not (mine and d.startswith(mine))]
    if others:
        results["V2_REGISTRY_OTHERWISE_UNDRIFTED"] = (
            "failed", f"{len(others)} problem(s) that are not this narrative's own change:"
                      f"\n         " + "\n         ".join(others))
    elif recorded and (path is None or not path.is_file()):
        results["V2_REGISTRY_OTHERWISE_UNDRIFTED"] = (
            "failed", f"{recorded} is not there to hash")
    else:
        results["V2_REGISTRY_OTHERWISE_UNDRIFTED"] = (
            "ok", "nothing else in the registry has drifted")

    if not args.reason.strip() or len(args.reason.strip()) < 3:
        results["V3_REASON_IN_WRITING"] = (
            "failed", "a revision with no reason is the edit this verb exists to prevent")
    else:
        results["V3_REASON_IN_WRITING"] = ("ok", f"{len(args.reason)} characters")

    if any(v[0] == "failed" for v in results.values()):
        return refuse("revise-narrative", args.registry, results)

    was = (state.get(args.entry) or {}).get("narrative_sha256")
    now = narrative_hash(path)
    append_ledger(args.registry, [ledger_record(
        "revise-narrative", args.entry, entry, narrative_path=recorded,
        narrative_sha256=now, prev_narrative_sha256=was, reason=args.reason)])
    sys.stdout.write(f"NARRATIVE REVISION RECORDED: {args.entry} {recorded}\n"
                     f"  was: {was}\n"
                     f"  now: {now}\n"
                     f"  because: {args.reason}\n")
    for name in REVISE_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    return EXIT_OK


def verb_attach_pack(args):
    """Set a prediction's replay pack, once, and only while the pack is in PACKS.jsonl and its
    manifest verifies (asked of seatswap, as a subprocess). Absent to a name is the ONLY move."""
    registry = read_json(args.registry, "registry")
    results, target = {}, None
    for eid, entry, pred in predictions_of(registry):
        if pred.get("id") == args.id:
            target = (eid, entry, pred)
            break
    drift = run_check(args.registry)
    if target is None:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"no prediction {args.id!r} in this registry")
    elif drift:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"{len(drift)} problem(s) in the registry as it stands:\n         "
                      + "\n         ".join(drift))
    else:
        results["A1_PREDICTION_EXISTS_AND_UNDRIFTED"] = (
            "ok", f"{args.id} exists and the registry matches its ledger")

    if target is None:
        for name in PACK_PRECONDITIONS[1:]:
            results[name] = ("not-evaluated", "depends on A1_PREDICTION_EXISTS_AND_UNDRIFTED")
    else:
        pred = target[2]
        if pred.get("pack"):
            results["A4_PACK_SET_ONCE"] = (
                "failed", f"{args.id} already names the pack {render(pred['pack'])}. A pack is "
                          f"set once; pointing a registered prediction at a different analysis "
                          f"is moving the goalpost")
        else:
            results["A4_PACK_SET_ONCE"] = ("ok", "no pack set yet")
        swap = seatswap_check(args.pack)
        results["A5_PACK_IN_PACKS_AND_VERIFIES"] = (
            ("ok", f"{args.pack} is in PACKS.jsonl and verifies (manifest "
                   f"{str(swap.get('manifest_sha256'))[:12]})") if swap["pack_ok"] else
            ("failed", f"pack {args.pack!r} does not verify: {swap.get('why')}"))
        results["A_NOT_QUARANTINED"] = quarantine_result(target[0],
                                                         provenance(args.registry))  # order 51

    if any(v[0] == "failed" for v in results.values()):
        return refuse("attach-pack", args.registry, results)

    eid, entry, pred = target
    pred["pack"] = str(args.pack)
    write_registry(args.registry, registry)
    append_ledger(args.registry, [ledger_record("attach-pack", eid, entry, pred,
                                                manifest_sha256=swap.get("manifest_sha256"))])
    sys.stdout.write(f"PACK ATTACHED: {args.id} -> {args.pack}\n")
    for name in PACK_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    sys.stdout.write(f"  {args.id} re-frozen at "
                     f"{sha256_bytes(canonical(frozen_of(pred)))[:12]}\n")
    return EXIT_OK


def verb_attach_deps(args):
    """Declare, once, what an entry rests on (order 51, item 23): other entries, or `pack:<name>`
    for the code/data under it. `--none` is a declaration too: an empty list, unlike "never
    declared". The shape of attach-prereg; the ledger line's `frozen` is the record `check` holds
    the registry's `depends_on` to."""
    registry = read_json(args.registry, "registry")
    results = {}
    entry = registry.get(args.entry)
    edges = [] if args.none else list(args.on)
    drift = run_check(args.registry)
    if entry is None:
        results["D1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"no entry {args.entry!r} in this registry")
    elif drift:
        results["D1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "failed", f"{len(drift)} problem(s) in the registry as it stands:\n         "
                      + "\n         ".join(drift))
    else:
        results["D1_ENTRY_EXISTS_AND_UNDRIFTED"] = (
            "ok", f"entry {args.entry} exists and the registry matches its ledger")

    if entry is None:
        for name in DEPS_PRECONDITIONS[1:]:
            results[name] = ("not-evaluated", "depends on D1_ENTRY_EXISTS_AND_UNDRIFTED")
        return refuse("attach-deps", args.registry, results)

    _, _, state = ledger_state(read_ledger(args.registry))
    already = (state.get(args.entry) or {}).get("depends_on")
    if already is not None or "depends_on" in entry:
        shown = already if already is not None else entry.get("depends_on")
        results["D2_DEPS_SET_ONCE"] = (
            "failed", f"entry {args.entry} already declared depends_on {render(shown)}. "
                      f"Dependencies are set once; re-pointing a claim at different evidence "
                      f"is moving the goalpost, and a dead upstream is left by a new entry that "
                      f"supersedes, never by editing the edge")
    else:
        results["D2_DEPS_SET_ONCE"] = ("ok", "no dependencies declared yet")

    problems, packs, packs_problem = [], None, None
    for i, edge in enumerate(edges):
        if edge in edges[:i]:
            problems.append(f"{edge} is named twice")
        elif edge.startswith(PACK_EDGE):
            if packs is None:
                packs, packs_problem = pack_names()
            name = edge[len(PACK_EDGE):]
            if packs_problem:
                problems.append(f"{edge}: {packs_problem}")
            elif not name or name not in packs:
                problems.append(f"{edge} is not in PACKS.jsonl")
        elif not ENTRY_ID.match(edge) or edge not in registry:
            problems.append(f"{edge} is not an entry in this registry (nor pack:<name>)")
    results["D3_EDGES_RESOLVE"] = (
        ("failed", "; ".join(problems)) if problems else
        ("ok", f"{len(edges)} edge(s), each an entry in this registry or a pack in PACKS.jsonl"
         if edges else "--none: an explicit declaration that this claim rests on nothing"))

    if args.entry in edges:
        results["D4_NO_CYCLE"] = ("failed", f"self-dependency: {args.entry} depends on itself")
    else:
        graph = {eid: list(st["depends_on"]) for eid, st in state.items()
                 if isinstance(st.get("depends_on"), list)}
        graph[args.entry] = edges
        cyc = find_cycle(graph, args.entry)
        results["D4_NO_CYCLE"] = (("failed", f"cycle: {' → '.join(cyc)}") if cyc else
                                  ("ok", "no path leads back to this entry"))

    if any(v[0] == "failed" for v in results.values()):
        return refuse("attach-deps", args.registry, results)

    entry["depends_on"] = edges
    write_registry(args.registry, registry)
    frozen = {"depends_on": edges}
    append_ledger(args.registry, [ledger_record("attach-deps", args.entry, entry, frozen=frozen,
                                                frozen_sha256=sha256_bytes(canonical(frozen)))])
    sys.stdout.write(f"DEPENDENCIES DECLARED: {args.entry} -> "
                     f"{', '.join(edges) if edges else 'none'}\n")
    for name in DEPS_PRECONDITIONS:
        sys.stdout.write(f"ok       {name}: {results[name][1]}\n")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="regate.py",
        description="A prediction's goalposts are frozen when it is registered. There is "
                    "no way to waive a precondition.")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("baseline", help="record the frozen fields of every prediction")
    p.add_argument("registry")
    p.set_defaults(fn=verb_baseline)

    p = sub.add_parser("check", help="has anything frozen changed?")
    p.add_argument("registry")
    p.set_defaults(fn=verb_check)

    p = sub.add_parser("register", help="add one new entry, or refuse")
    p.add_argument("registry")
    p.add_argument("entry", help="JSON file: one key, the new entry id")
    p.set_defaults(fn=verb_register)

    p = sub.add_parser("attach-prereg", help="set a prediction's prereg, once")
    p.add_argument("registry")
    p.add_argument("id")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_attach_prereg)

    p = sub.add_parser("attach-pack", help="set a prediction's replay pack, once")
    p.add_argument("registry")
    p.add_argument("id")
    p.add_argument("pack")
    p.set_defaults(fn=verb_attach_pack)

    p = sub.add_parser("attach-deps", help="declare what an entry rests on, once")
    p.add_argument("registry")
    p.add_argument("entry")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="append", metavar="ID|pack:NAME",
                       help="an entry id, or pack:<replay-pack-name>; repeat for each edge")
    group.add_argument("--none", action="store_true",
                       help="declare explicitly that this entry rests on nothing")
    p.set_defaults(fn=verb_attach_deps)

    p = sub.add_parser("attach-narrative", help="bind an entry's prose file to the ledger, once")
    p.add_argument("registry")
    p.add_argument("entry")
    p.add_argument("narrative")
    p.set_defaults(fn=verb_attach_narrative)

    p = sub.add_parser("revise-narrative", help="record a deliberate edit to an entry's prose")
    p.add_argument("registry")
    p.add_argument("entry")
    p.add_argument("reason")
    p.set_defaults(fn=verb_revise_narrative)

    p = sub.add_parser("status", help="change an entry's status, on the record")
    p.add_argument("registry")
    p.add_argument("entry")
    p.add_argument("text")
    p.set_defaults(fn=verb_status)

    p = sub.add_parser("score", help="record an outcome once its due date has arrived")
    p.add_argument("registry")
    p.add_argument("id")
    p.add_argument("--outcome", required=True, choices=["0", "1", "none"])
    p.add_argument("--note", required=True)
    p.add_argument("--value", type=float, default=None,
                   help="the measured number, when the kill_rule is on value")
    p.set_defaults(fn=verb_score)

    args = parser.parse_args(argv)
    if not Path(args.registry).is_file():
        sys.stderr.write(f"no such registry: {args.registry}\n")
        return EXIT_USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
