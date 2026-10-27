#!/usr/bin/env python3
"""canary.py — the canary program, and its twin, the acquittal control (order 44, item 12).

"An auditor whose sensitivity is unmeasured is one more self-report." Each run plants known
FALSE things in front of the gates (canaries: they must be CAUGHT) and known TRUE things
(acquittals: they must PASS), and reports recall beside specificity, as counts:

    canary.py run [--json]        one result per case; appends ONE line to CANARY.jsonl

    recall      = canaries caught / canaries that ran
    specificity = acquittals passed / acquittals that ran
    a case that ERRORS (could not run) is in neither rate; it is listed by name

🔴 NOTHING LIVE IS EVER SEEDED. The gates run as byte-identical COPIES (sha256 checked against
the live files) in a temp world laid out like ~/helix — <tmp>/helix/workspace/factgate/…,
<tmp>/helix/workspace/regate/…, <tmp>/helix/workspace/sealgate/…, <tmp>/helix/workspace/
preflight/… — as subprocesses, with HOME set to <tmp>, against temp state. Every path a copy
derives (from where it lives, or from the home directory) therefore lands in the temp world,
and `build_world` refuses to run if any does not. Their refusals log next to the copies. The
gates themselves carry no switch for this: nothing in them was changed.

The live fact base, the factgate ledger, the registry and its ledger, any sealgate token and
both live refusals.log files are hashed before and after every run; the record says whether
they are byte-identical.

Exit codes:  0 every canary caught and every acquittal passed · 2 no run (world not built)
             · 4 a miss, a false refusal or an error
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
import tempfile
import time
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = HERE.parents[1]                       # ~/helix: this file is helix/workspace/canary/
CANARY_LOG = HERE / "CANARY.jsonl"
TIMEOUT = 60                                  # seconds, per gate call

EXIT_OK = 0
EXIT_NO_RUN = 2
EXIT_FINDINGS = 4

# The gate code under test and every sibling it imports or runs that a case reaches:
# sealgate imports preflight (P8), review (P9, order 47) and veto (P10, order 48). regate runs sealgate (R4) and cycle.py
# (score); no case names a prereg, and cycle.py is left out, so `score` reports its cycle as "not measured".
# veto's sandbox.py is left out: no case calls Alice or reproduces a break.
# factgate loads categories.py by path for F5 (order 76) and fails closed without it (order 78).
GATES = {"factgate": "workspace/factgate/factgate.py",
         "regate": "workspace/regate/regate.py",
         "sealgate": "workspace/sealgate/sealgate.py",
         "preflight": "workspace/preflight/preflight.py",
         "review": "workspace/sealgate/review.py",
         "veto": "workspace/veto/veto.py",
         "categories": "workspace/categories/categories.py"}
# Copies that read data files beside themselves (order 78): every module-level Path of theirs in
# their own folder is copied too, if the live file exists. categories.py: CATEGORIES.txt and its
# sidecar CSVs. The list itself must be there.
DATA_OF = {"categories": "DEFAULT"}
# Fixtures the gates' own suites use, read from the live gate repos, never written.
REGATE_FIXTURES = HELIX / "workspace/regate/fixtures"
SEALGATE_FIXTURES = HELIX / "workspace/sealgate/fixtures/complete"


class CanaryError(Exception):
    """The world could not be built safely: no case runs."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------- the live files

def live_files():
    """Every live file the hard rule names. Missing ones are listed too (hashed as absent)."""
    out = []
    facts = HELIX / "memory/facts"
    if facts.is_dir():
        out += sorted(p for p in facts.rglob("*") if p.is_file())
    state = HELIX / "workspace/factgate/state"
    if state.is_dir():
        out += sorted(p for p in state.rglob("*") if p.is_file())
    out += [HELIX / "workspace/seam/registry/registry.json",
            HELIX / "workspace/seam/registry/REGATE_LEDGER.jsonl",
            HELIX / "workspace/regate/refusals.log",
            HELIX / "workspace/sealgate/refusals.log",
            HELIX / "workspace/sealgate/REVIEWS.jsonl",
            HELIX / "workspace/veto/VETOES.jsonl"]
    for root, dirs, files in os.walk(HELIX / "workspace"):         # sealgate tokens
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        out += [Path(root) / f for f in files if f == "RUN_TOKEN.json"]
    return out


def snapshot(paths):
    return {str(p): (sha256(p) if p.is_file() else None) for p in paths}


# ------------------------------------------------------------------ the world

def run_env(helix):
    """The variables the copies run with: HOME is the temp root, so a path a gate finds from
    the home directory lands in the temp world. Nothing is inherited from the caller."""
    return {"HOME": str(helix.parent), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8"}


def gate(helix, name, *args, cwd=None):
    """Run one gate copy. (exit code, stdout, stderr)."""
    p = subprocess.run([sys.executable, "-B", str(helix / GATES[name]), *map(str, args)],
                       capture_output=True, text=True, timeout=TIMEOUT, env=run_env(helix),
                       cwd=str(cwd or helix))
    return p.returncode, p.stdout, p.stderr


PROBE = """import importlib.util, json, sys
from pathlib import Path
out = {}
for name, path in json.loads(sys.argv[1]).items():
    spec = importlib.util.spec_from_file_location("canary_probe_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key, value in vars(mod).items():
        if isinstance(value, Path):
            out[name + "." + key] = str(value.resolve())
print(json.dumps(out))
"""


def resolved_paths(helix):
    """{"<gate>.<NAME>": resolved path} for every module-level Path each copy derives, loaded
    with the same variables the cases run with."""
    p = subprocess.run([sys.executable, "-B", "-c", PROBE,
                        json.dumps({n: str(helix / rel) for n, rel in GATES.items()})],
                       capture_output=True, text=True, timeout=TIMEOUT, env=run_env(helix),
                       cwd=str(helix))
    if p.returncode != 0:
        raise CanaryError(f"cannot load the gate copies: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout)


def gate_shas(helix):
    return {n: sha256(helix / rel) for n, rel in GATES.items()}


def copy_live(helix, rel, name):
    live, copy = HELIX / rel, helix / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(live, copy)
    if sha256(copy) != sha256(live):
        raise CanaryError(f"the copy of {name} is not the live file")


def copy_data(helix, paths):
    """The data files of DATA_OF, found from the copies' own resolved paths."""
    world = helix.resolve()
    for name, required in DATA_OF.items():
        folder = (world / GATES[name]).parent
        for key, v in sorted(paths.items()):
            mod, _, attr = key.partition(".")
            if mod != name or Path(v).parent != folder:
                continue
            rel = Path(v).relative_to(world)
            if (HELIX / rel).is_file():
                copy_live(helix, rel, f"{name}'s {rel.name}")
            elif attr == required:
                raise CanaryError(f"{name}'s {rel.name} is missing from the live helix")


def build_world(root):
    """<root>/helix with a byte-identical copy of each gate and of the data files DATA_OF names.
    Raises CanaryError if a copy differs from the live file or if any path a copy resolves is
    inside the real ~/helix."""
    helix = Path(root) / "helix"
    for name, rel in GATES.items():
        copy_live(helix, rel, name)
    paths = resolved_paths(helix)
    copy_data(helix, paths)
    real = str(HELIX.resolve())
    inside = sorted(k for k, v in paths.items()
                    if v == real or v.startswith(real + os.sep))
    if inside:
        raise CanaryError(f"a copy resolves into the live helix: {', '.join(inside)}")
    return helix


# ------------------------------------------------------------------ verdicts

FAILED_RE = re.compile(r"^FAILED\s+([A-Z]\d+_[A-Z0-9_]+)", re.M)


def verdict_from_refusal(kind, code, out, err, expected):
    """(outcome, detail). A canary is caught only by the refusal it plants; an acquittal passes
    only on exit 0. Anything else (usage errors, a different precondition) is an error."""
    failed = FAILED_RE.findall(err)
    if kind == "acquittal":
        if code == 0:
            return "acquitted", (out.strip().splitlines() or [""])[0]
        if code in (3, 4):
            return "refused", f"exit {code}: " + (", ".join(failed) or err.strip()[:200])
        return "error", f"exit {code}: {err.strip()[-200:]}"
    if code == 0:
        return "missed", f"exit 0: {(out.strip().splitlines() or [''])[0]}"
    if code == 3 and expected in failed:
        return "caught", f"exit 3: {', '.join(failed)}"
    return "error", f"exit {code}, not the {expected} refusal: " \
                    f"{', '.join(failed) or err.strip()[-200:]}"


def must(result, what):
    code, out, err = result
    if code != 0:
        raise RuntimeError(f"setup step {what} exited {code}: {(err or out).strip()[-200:]}")
    return out


# ------------------------------------------------------------- factgate cases

FACTS = {"01_alpha.md": "# Alpha\nThe nightly runs at 04:30.\nIt reads the fact base.\n",
         "02_beta.md": "# Beta\nThe registry has five entries.\nNone is scored yet.\n",
         "03_gamma.md": "# Gamma\nThe vault is append-only.\n"}


def fact_base(helix, d):
    """A fact base and its baselined ledger: (facts dir, state dir)."""
    facts, state = d / "facts", d / "state"
    facts.mkdir(parents=True)
    for name, text in FACTS.items():
        (facts / name).write_text(text, encoding="utf-8")
    must(gate(helix, "factgate", "--state-dir", state, "baseline", facts), "factgate baseline")
    return facts, state


def factgate_check(helix, facts, state, needle):
    code, out, err = gate(helix, "factgate", "--state-dir", state, "check", facts)
    if code == 4:
        hit = [l for l in err.splitlines() if needle in l]
        return ("caught", hit[0]) if hit else ("error", f"exit 4 without {needle}: {err[-200:]}")
    if code == 0:
        return "missed", "exit 0: " + out.strip().splitlines()[0]
    return "error", f"exit {code}: {err.strip()[-200:]}"


def case_off_ledger(helix, d):
    facts, state = fact_base(helix, d)
    (facts / "04_planted.md").write_text("# Planted\nWritten around the ledger.\n")
    return factgate_check(helix, facts, state, "NEW OFF-LEDGER: 04_planted.md")


def case_silent_edit(helix, d):
    facts, state = fact_base(helix, d)
    f = facts / "02_beta.md"
    f.write_text(f.read_text().replace("five entries", "six entries"))
    return factgate_check(helix, facts, state, "CHANGED OFF-LEDGER: 02_beta.md")


def case_interim_verdict(helix, d):
    """A legacy base (baseline-legacy at its last line), then a recorded fact whose first line
    carries 🟢 and PASSED. `interim --json` must report it, with its verdict words."""
    facts, state = fact_base(helix, d)
    ledger = (state / "FACTS_LEDGER.jsonl").read_bytes()
    must(gate(helix, "factgate", "--state-dir", state, "baseline-legacy", facts,
              "--cutoff-line", len(ledger.splitlines()), "--cutoff-sha256",
              hashlib.sha256(ledger).hexdigest(), "--vault-seq", 0), "factgate baseline-legacy")
    name = "04_result.md"
    (facts / name).write_text("# 🟢 PASSED — the probe reads the planted signal\nOne run.\n"
                              "Category: helix\n", encoding="utf-8")
    must(gate(helix, "factgate", "--state-dir", state, "record", facts, name,
              "--reason", "canary: an interim fact with verdict words"), "factgate record")
    code, out, err = gate(helix, "factgate", "--state-dir", state, "interim", facts, "--json")
    if code != 0:
        return "error", f"interim exit {code}: {err.strip()[-200:]}"
    items = {r["file"]: r for r in json.loads(out)["interim"]}
    if set(items) - {name}:
        return "error", f"legacy facts reported as INTERIM: {sorted(set(items) - {name})}"
    words = [w for _, w in items.get(name, {}).get("verdict", [])]
    if "🟢" in words and "PASSED" in words:
        return "caught", f"INTERIM {name}, verdict words: {', '.join(words)}"
    return "missed", f"{name} reported with verdict words {words}" if name in items \
        else f"{name} not reported as INTERIM"


def case_no_category(helix, d):
    """Order 78: a new fact recorded with no `Category:` line must be refused by F5."""
    facts, state = fact_base(helix, d)
    name = "04_uncategorised.md"
    (facts / name).write_text("# Uncategorised\nRecorded without a category line.\n")
    code, out, err = gate(helix, "factgate", "--state-dir", state, "record", facts, name,
                          "--reason", "canary: a new fact with no category")
    return verdict_from_refusal("canary", code, out, err, "F5_CATEGORY_DECLARED")


def case_recorded_fact(helix, d):
    facts, state = fact_base(helix, d)
    name = "04_recorded.md"
    (facts / name).write_text("# Recorded\nAdded through `factgate record`.\nCategory: helix\n")
    must(gate(helix, "factgate", "--state-dir", state, "record", facts, name,
              "--reason", "canary: a properly recorded new fact"), "factgate record")
    code, out, err = gate(helix, "factgate", "--state-dir", state, "check", facts)
    return verdict_from_refusal("acquittal", code, out, err.replace("\n", " "), None)


# --------------------------------------------------------------- regate cases

def similarity():
    return json.loads((REGATE_FIXTURES / "similarity/groups.json").read_text(encoding="utf-8"))


def entry(eid, text, scope, due, **extra):
    """test_regate.py's make_entry shape."""
    pred = {"id": f"{eid}-p1", "kind": "time", "due": due, "p": 0.6,
            "metric": "latency_reduction_fraction", "statement": text["statements"][0],
            "kill": "A reduction below 0.05 retires the claim in this scope.",
            "kill_rule": {"on": "value", "op": "<", "value": 0.05}}
    body = {"claim": text["claim"], "registered": "2026-10-01", "status": "open",
            "predictions": [pred], "scope": scope}
    body.update(extra)
    return {eid: body}


def killed(helix, d):
    """test_regate.py's `killed`: the fixture registry plus 0005 (scope proj/a), past due and
    baselined, then scored outcome 0 with a value under its kill_rule, so 0005 is INVALID."""
    d.mkdir(parents=True)
    reg = d / "registry.json"
    registry = json.loads((REGATE_FIXTURES / "real_copy/registry.json").read_text("utf-8"))
    registry.update(entry("0005", similarity()["original"], "proj/a", "2026-01-31"))
    reg.write_text(json.dumps(registry, indent=1, ensure_ascii=False), encoding="utf-8")
    must(gate(helix, "regate", "baseline", reg), "regate baseline")
    must(gate(helix, "regate", "score", reg, "0005-p1", "--outcome", "0", "--note", "no gain",
              "--value", "0.03"), "regate score")
    if not json.loads(reg.read_text("utf-8"))["0005"]["status"].startswith("INVALID"):
        raise RuntimeError("setup: 0005 did not become INVALID")
    return reg


def future():
    return (datetime.date.today() + datetime.timedelta(days=365)).isoformat()


def register(helix, d, reg, payload, kind, expected):
    path = d / "entry.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    code, out, err = gate(helix, "regate", "register", reg, path)
    outcome, detail = verdict_from_refusal(kind, code, out, err, expected)
    if outcome == "caught":
        why = [l.strip() for l in err.splitlines() if l.startswith("FAILED")]
        detail += f" — {why[0][:160]}" if why else ""
    return outcome, detail


def case_paraphrase_r5(helix, d):
    reg = killed(helix, d)
    return register(helix, d, reg, entry("0006", similarity()["paraphrases"][0], "proj/a",
                                         future()), "canary", "R5_NOT_A_RESURRECTION")


def case_supersede_open_r5(helix, d):
    """Order 41's rule: a new claim citing open 0004 as superseded is refused R5."""
    reg = killed(helix, d)
    payload = entry("0006", similarity()["different"][0], "proj/a", future(), supersedes="0004",
                    structural_change={"type": "instrument", "what": "a per-request trace "
                                       "instrument replaces the median-of-replay timer"})
    return register(helix, d, reg, payload, "canary", "R5_NOT_A_RESURRECTION")


def case_new_scope_entry(helix, d):
    reg = killed(helix, d)
    return register(helix, d, reg, entry("0006", similarity()["different"][1], "proj/c",
                                         future()), "acquittal", None)


# ------------------------------------------------------------- sealgate cases

def write_preflight(helix, d, prereg):
    """A brief exactly as `preflight.py run` writes it (test_sealgate.py's add_preflight), built
    by the preflight COPY, and the pre-flight section that names it."""
    spec = importlib.util.spec_from_file_location("canary_preflight", helix / GATES["preflight"])
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    sections = ("## 1. Academia\n"
                "- Doe 2004, \"A synthetic probe\" — the closest prior work; does not read the "
                "planted signal\n\n"
                "## 2. GitHub\n- NOTHING FOUND\n- query: synthetic probe planted signal github\n\n"
                "## 3. Known math\n- https://example.org/probe-theory — standard bound on probe "
                "accuracy\n\n"
                "## 4. Known limits\n- NOTHING FOUND\n- query: synthetic probe impossibility\n\n"
                "## 5. Cheapest kill\nRun the answer-free floor first.\n\n"
                "## 6. Verdict\nGO\n")
    text = pf.build_brief(b"Idea: the synthetic probe fixture.\n", sections, searcher="Alice",
                          model="Gemini 3.1 Pro (High)", date_utc="2026-09-30T22:00:00Z",
                          n_queries=4)
    brief = d / "PREFLIGHT_canary.md"
    brief.write_bytes(text.encode("utf-8"))
    with open(prereg, "a", encoding="utf-8") as fh:
        fh.write(f"\n## PRE-FLIGHT\nBrief: {brief.name}\nBrief-sha256: {sha256(brief)}\n")


FIXTURE_REVIEW = ("[F1] The bar of 0.72 is asserted, not derived.\n"
                  "[F2] B2 could be met by an artefact of the split.\n"
                  "[F3] No positive control.\n")


def write_review(helix, p, assigned):
    """Order 47 (P9): an `assign` line naming `assigned` and that reviewer's review, in the TEMP
    world's REVIEWS.jsonl, through the review COPY. The assign line is written directly, so the
    case decides who is assigned; the review goes through the copy's own `run`, with a
    fixture-built reply in place of the reviewer (no reviewer is ever called)."""
    spec = importlib.util.spec_from_file_location("canary_review", helix / GATES["review"])
    rv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rv)
    if not str(rv.LEDGER).startswith(str(helix) + os.sep):
        raise RuntimeError(f"setup: the review copy's ledger {rv.LEDGER} is not in the world")
    rv.append([{"kind": "assign", "seal": rv.seal_key(p), "reviewer": assigned,
                "repeat": False, "throwaway": False}])
    rv.REVIEWERS[assigned] = lambda prompt: {"text": FIXTURE_REVIEW, "meta": {"model": "fixture"}}
    rv.run(p)


NO_BREAK = "fixture: Alice was not called; this record stands in for a NO_BREAK run.\n"
BREAK_LINE = "BREAK: fixture: the band cannot fail"
TAMPERED = ("hashlib.py", "med.py")                 # order 48's sound-seal break, thrown out


def write_veto(helix, p, outcome):
    """Order 48 (P10): a veto record for the seal AS STAMPED, in the TEMP world's VETOES.jsonl,
    through the veto COPY. NO_BREAK goes through the copy's own `run`, its scratch in the world,
    with a fixture in place of Alice (Alice is never called). Every other outcome (order 48b:
    BREAK_CONFIRMED, BREAK_DISPUTED, BREAK_INVALID) is appended directly, as the assign line
    above is: the case decides the outcome, and neither bwrap nor the judge is ever run."""
    spec = importlib.util.spec_from_file_location("canary_veto", helix / GATES["veto"])
    vt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vt)
    for name in ("LEDGER", "REVIEWS_LEDGER"):
        if not str(getattr(vt, name)).startswith(str(helix) + os.sep):
            raise RuntimeError(f"setup: the veto copy's {name} is not in the world")
    vt.BULK = p.parent / "veto_bulk"
    if outcome == "NO_BREAK":
        def alice(scratch, prompt):
            (Path(scratch) / "NO_BREAK.md").write_text(NO_BREAK, encoding="utf-8")
            return {"model": "fixture"}
        vt.ALICE = alice
        rec = vt.run(p)
        if rec["outcome"] != "NO_BREAK":
            raise RuntimeError(f"setup: the veto copy recorded {vt.label(rec)}")
        return
    scratch = vt.BULK / f"fixture_{outcome.lower()}" / "scratch"
    (scratch / "alice").mkdir(parents=True)
    (scratch / "alice" / "break.sh").write_text(f"echo '{BREAK_LINE}'\n", encoding="utf-8")
    raw = p.read_bytes()
    rec = {"kind": "veto", "seal": vt.seal_key(p), "body_sha256": vt.body_sha256(raw),
           "file_sha256": vt.sha256_bytes(raw), "outcome": outcome, "why": None,
           "files": vt.tree_hashes(scratch), "script": "break.sh", "scratch": str(scratch),
           "repro": [{"rc": 0, "last_line": BREAK_LINE}] * 2, "tamper": [],
           "instrument": "none", "judge": None, "reviewer": "Alice",
           "disagreement": False, "disagreement_kind": None}
    if outcome in ("BREAK_CONFIRMED", "BREAK_DISPUTED"):
        rec["judge"] = {"valid": outcome == "BREAK_CONFIRMED", "why": "fixture: no judge was asked",
                        "meta": {"model": "fixture"}}
    if outcome == "BREAK_DISPUTED":
        rec.update(disagreement=True, disagreement_kind="alice_vs_judge")
    if outcome == "BREAK_INVALID":
        rec["tamper"] = [{"file": f, "reason": "fixture", "run": r}
                         for r in (1, 2) for f in TAMPERED]
        rec["why"] = "tamper: " + ", ".join(TAMPERED)
    vt.append([rec])


def prereg(helix, d, preflight=True, edit=None, assigned="Alice", veto=("NO_BREAK",)):
    """sealgate's fixtures/complete, copied out, optionally edited, reviewed by `assigned` in the
    temp world's ledger, then stamped by Alice, then given a veto record for its body for each
    outcome in `veto`, in order."""
    d.mkdir(parents=True)
    p = d / "PREREG.md"
    shutil.copyfile(SEALGATE_FIXTURES / "PREREG.md", p)
    shutil.copyfile(SEALGATE_FIXTURES / "FINDINGS.md", d / "FINDINGS.md")
    if edit:
        p.write_text(edit(p.read_text(encoding="utf-8")), encoding="utf-8")
    if preflight:
        write_preflight(helix, d, p)
    write_review(helix, p, assigned)
    must(gate(helix, "sealgate", "stamp", p, "--reviewer", "Alice", "--findings", "FINDINGS.md",
              cwd=d), "sealgate stamp")
    for outcome in veto:
        write_veto(helix, p, outcome)
    return p


def token(helix, p, kind, expected):
    code, out, err = gate(helix, "sealgate", "token", p, cwd=p.parent)
    return verdict_from_refusal(kind, code, out, err, expected)


def vacuous(text):
    """No bands, no stop conditions: both sections, heading and body, cut out."""
    start, end = text.index("## BANDS"), text.index("## Scope, recorded")
    if "## STOP CONDITIONS" not in text[start:end]:
        raise RuntimeError("setup: the fixture's sections moved")
    return text[:start] + text[end:]


def case_vacuous_prereg_p5(helix, d):
    return token(helix, prereg(helix, d, edit=vacuous), "canary", "P5_BANDS_AND_STOP_CONDITIONS")


def case_no_preflight_p8(helix, d):
    return token(helix, prereg(helix, d, preflight=False), "canary", "P8_PREFLIGHT")


def case_wrong_reviewer_p9(helix, d):
    """Assigned Claude-cold, stamped Alice: the author picked the friendlier reviewer."""
    return token(helix, prereg(helix, d, assigned="Claude-cold"), "canary",
                 "P9_ASSIGNED_REVIEWER")


def case_vetoed_seal_p10(helix, d):
    """Alice broke this seal, the break reproduced cleanly and the judge confirmed it (order 48b):
    one confirmed break vetoes it."""
    return token(helix, prereg(helix, d, veto=("BREAK_CONFIRMED",)), "canary", "P10_ALICE_VETO")


def case_disputed_seal_p10(helix, d):
    """Alice broke it cleanly, the judge disagrees (order 48b): blocked until the user decides."""
    return token(helix, prereg(helix, d, veto=("BREAK_DISPUTED",)), "canary", "P10_ALICE_VETO")


def case_complete_prereg(helix, d):
    """A NO_BREAK run and, beside it, a tampered break thrown out as BREAK_INVALID (order 48b):
    the invalid break must not veto it."""
    return token(helix, prereg(helix, d, veto=("NO_BREAK", "BREAK_INVALID")), "acquittal", None)


# ------------------------------------------------------------------- the run

Case = namedtuple("Case", "name kind gate fn")
CASES = [
    Case("off-ledger", "canary", "factgate", case_off_ledger),
    Case("silent-edit", "canary", "factgate", case_silent_edit),
    Case("interim-verdict", "canary", "factgate", case_interim_verdict),
    Case("no-category", "canary", "factgate", case_no_category),
    Case("paraphrase-r5", "canary", "regate", case_paraphrase_r5),
    Case("supersede-open-r5", "canary", "regate", case_supersede_open_r5),
    Case("vacuous-prereg-p5", "canary", "sealgate", case_vacuous_prereg_p5),
    Case("no-preflight-p8", "canary", "sealgate", case_no_preflight_p8),
    Case("wrong-reviewer-p9", "canary", "sealgate", case_wrong_reviewer_p9),
    Case("vetoed-seal-p10", "canary", "sealgate", case_vetoed_seal_p10),
    Case("disputed-seal-p10", "canary", "sealgate", case_disputed_seal_p10),
    Case("recorded-fact", "acquittal", "factgate", case_recorded_fact),
    Case("new-scope-entry", "acquittal", "regate", case_new_scope_entry),
    Case("complete-prereg", "acquittal", "sealgate", case_complete_prereg),
]


def run_case(case, helix):
    """One case in its own directory of the world. An exception is an error, never a verdict."""
    d = Path(helix) / "cases" / case.name
    n = 1
    while d.exists():
        n += 1
        d = Path(helix) / "cases" / f"{case.name}.{n}"
    try:
        outcome, detail = case.fn(Path(helix), d)
    except Exception as exc:
        outcome, detail = "error", f"{type(exc).__name__}: {exc}"[:300]
    return {"name": case.name, "kind": case.kind, "gate": case.gate, "outcome": outcome,
            "detail": detail}


def run_cases(helix, cases=None):
    return [run_case(c, helix) for c in (CASES if cases is None else cases)]


def tally(results):
    can = [r for r in results if r["kind"] == "canary" and r["outcome"] != "error"]
    acq = [r for r in results if r["kind"] == "acquittal" and r["outcome"] != "error"]
    return {"caught": sum(r["outcome"] == "caught" for r in can), "canaries": len(can),
            "missed": [r["name"] for r in can if r["outcome"] != "caught"],
            "acquitted": sum(r["outcome"] == "acquitted" for r in acq), "acquittals": len(acq),
            "refused": [r["name"] for r in acq if r["outcome"] != "acquitted"],
            "errors": [r["name"] for r in results if r["outcome"] == "error"]}


def summary(results):
    """`11/11 caught · 3/3 acquitted`, with 🔴 and the names on any miss, refusal or error."""
    t = tally(results)
    rec = f"{t['caught']}/{t['canaries']} caught"
    if t["missed"]:
        rec = f"🔴 {rec} (missed: {', '.join(t['missed'])})"
    spec = f"{t['acquitted']}/{t['acquittals']} acquitted"
    if t["refused"]:
        spec = f"🔴 {spec} (refused: {', '.join(t['refused'])})"
    parts = [rec, spec]
    if t["errors"]:
        parts.append(f"🔴 errored: {', '.join(t['errors'])}")
    return " · ".join(parts)


def run_once(log=None):
    """Build a world, run every case, check the live files, append one record. (results, rec)."""
    started = time.monotonic()
    watched = live_files()
    before = snapshot(watched)
    with tempfile.TemporaryDirectory(prefix="canary-") as tmp:
        helix = build_world(tmp)
        shas = gate_shas(helix)
        results = run_cases(helix)
    after = snapshot(set(watched) | set(live_files()))
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    t = tally(results)
    rec = {"date": datetime.date.today().isoformat(),
           "ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "cases": {r["name"]: r["outcome"] for r in results},
           "recall": f"{t['caught']}/{t['canaries']}",
           "specificity": f"{t['acquitted']}/{t['acquittals']}",
           "errors": t["errors"], "gates": shas,
           "live_untouched": not changed, "live_changed": changed,
           "live_files": len(before), "seconds": round(time.monotonic() - started, 2),
           "summary": summary(results)}
    with open(log or CANARY_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return results, rec


def canary_line(log=None):
    """The nightly's one line. Raises if no run happened: the nightly reports that itself."""
    results, rec = run_once(log)
    line = "- canary: " + rec["summary"]
    if not rec["live_untouched"]:
        line += f" · 🔴 {len(rec['live_changed'])} live file(s) changed during the run"
    return line


LABEL = {"caught": "CAUGHT", "missed": "MISSED", "acquitted": "ACQUITTED",
         "refused": "REFUSED", "error": "ERROR"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="canary.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("run", help="run every canary and acquittal on temp copies of the gates")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        results, rec = run_once()
    except CanaryError as exc:
        sys.stderr.write(f"canary did not run: {exc}\n")
        return EXIT_NO_RUN
    if args.json:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        for r in results:
            sys.stdout.write(f"{LABEL[r['outcome']]:<10} {r['kind']:<9} {r['gate']:<8} "
                             f"{r['name']:<18} {r['detail']}\n")
        sys.stdout.write(rec["summary"] + "\n")
        sys.stdout.write(f"live files: {rec['live_files']} hashed before and after, "
                         + ("byte-identical\n" if rec["live_untouched"] else
                            f"🔴 CHANGED: {', '.join(rec['live_changed'])}\n"))
        sys.stdout.write(f"{rec['seconds']} s\n")
    clean = rec["live_untouched"] and not (rec["errors"] or "🔴" in rec["summary"])
    return EXIT_OK if clean else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
