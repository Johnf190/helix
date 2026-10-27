#!/usr/bin/env python3
"""The nightly audit. Deterministic, read-only, no model in the loop.

Three jobs, in order:
  1. audit  — run ANCHOR over the record; append one row to history.jsonl
  2. cohort — remember when each reference was FIRST SEEN, so forward predictions have a
              population that was not chosen after the fact
  3. score  — when a registered prediction comes due, evaluate it and ask `regate` to
              write the result. This file no longer writes the registry itself: it
              computes, regate decides, and a refusal is reported, never worked around.
Output: NIGHTLY.md, for a human or a scheduled agent turn to read aloud."""
import json, pathlib, sys, datetime, collections, subprocess, re

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path.home()
sys.path.insert(0, str(HOME / "helix" / "workspace" / "anchor"))
import core, anchor as A
sys.path.insert(0, str(HERE))
from rung01 import lexical_class
from heartbeat import heartbeat_line

CORPORA = {"facts": HOME / "helix" / "memory" / "facts",
           "workspace": HOME / "helix" / "workspace"}
HISTORY = HERE / "history.jsonl"
SEEN = HERE / "claims_seen.json"
REG = HERE / "registry" / "registry.json"
REGATE = HOME / "helix" / "workspace" / "regate" / "regate.py"
LEDGER = "REGATE_LEDGER.jsonl"
FACTGATE = HOME / "helix" / "workspace" / "factgate" / "factgate.py"
FACTGATE_STATE = HOME / "helix" / "workspace" / "factgate" / "state"
FACTS = HOME / "helix" / "memory" / "facts"
TODAY = datetime.date.today()
# metrics this file knows how to compute. Anything else is a human's job and is
# reported as overdue, never auto-scored.
AUTOMATIC = ("abs_breaks_more_than_rel", "gated_breaks_less_than_ungated")
# the vault (orders 19-20): the one thing this file may run as root is the add, via one sudoers rule
VAULT_ADD = ["sudo", "-n", "/usr/local/sbin/helix-vault-add"]
VAULT_VERIFY = ["/usr/local/bin/helix-vault-verify"]
VAULT_TIMEOUT = 60
INTERIM_TIMEOUT = 60
# the gap audit (order 36): what the record omits. Report only, never fix.
GAPAUDIT = HOME / "helix" / "workspace" / "gapaudit"
# the correction-rate metric (order 37, item 18): is the record getting righter, or just faster?
METRIC = HOME / "helix" / "workspace" / "metric"
# housekeeping (order 38, item 22): the machine's state and the dry-run plan. Report only.
HOUSEKEEPING = HOME / "helix" / "workspace" / "housekeeping"
# the canary program (order 44, item 12): recall AND specificity of the gates, on temp copies.
CANARY = HOME / "helix" / "workspace" / "canary"
# replay packs (order 45, item 26): one pack at random, analysis stage only, within tolerance.
REPLAY = HOME / "helix" / "workspace" / "replay"
# the cold read (order 46, item 11): monthly, a cold session hunts the biggest FALSE belief.
COLDREAD = HOME / "helix" / "workspace" / "coldread"
# ORDER 47: reviewer rotation (review.py beside sealgate)
SEALGATE = HOME / "helix" / "workspace" / "sealgate"
# ORDER 48: Alice's veto (amendment 9a), veto.py and its VETOES.jsonl
VETO = HOME / "helix" / "workspace" / "veto"
# ORDER 49: seat-swap replication (item 10), seatswap.py and its SWAPS.jsonl
SEATSWAP = HOME / "helix" / "workspace" / "seatswap"
# ORDER 51: the provenance DAG (item 23), computed by regate.py's provenance_line() from REG. Its
# own constant: REGATE above is the subprocess target, which tests point at a shim.
PROVENANCE = HOME / "helix" / "workspace" / "regate"
# ORDER 52: the proposal engine (item 21), propose.py and its three hash-chained ledgers
PROPOSALS = HOME / "helix" / "workspace" / "proposals"
# ORDER 70: retired passages, from memretrieve's retirements() (its one parser); PINNED.txt names the pins
MEMRETRIEVE = HOME / "helix" / "workspace" / "memretrieve"
# ORDER 72: the correction queue (item 30), corrections.py and its hash-chained QUEUE.jsonl
CORRECTIONS = HOME / "helix" / "workspace" / "corrections"
# fact categories (order 73b, item 28 phase B): declared lines + the ASSIGNED.csv sidecar. Report only.
CATEGORIES = HOME / "helix" / "workspace" / "categories"
CATEGORIES_ASSIGNED = CATEGORIES / "ASSIGNED.csv"
PINNED = HOME / "helix" / "memory" / "PINNED.txt"


def regate(verb, *args):
    """Ask regate. This file computes; regate decides and writes.

    regate takes the date from the system clock, not from us — a caller-supplied
    date would be a way past its own due-date check.
    """
    return subprocess.run([sys.executable, str(REGATE), verb, str(REG), *args],
                          capture_output=True, text=True)


def audit():
    core.RULES = core.load_rules()
    rows, per_corpus = [], {}
    for name, root in CORPORA.items():
        r = A.run(root, net=False)
        per_corpus[name] = dict(files=r["files"], claims=r["total"], ok=r["ok"],
                                broken=len(r["broken"]), stale=len(r["stale"]),
                                unanchorable=r["unanchorable"], epsilon=round(r["epsilon"], 4))
        for c in r["claims"]:
            if c.kind in ("path", "cite", "url") and c.verdict in ("ok", "broken"):
                rows.append(dict(corpus=name, key=f"{name}|{c.file}|{c.kind}|{c.token}",
                                 cls=lexical_class(c), broken=int(c.verdict == "broken"),
                                 gated=int("seam/registry/" in c.file.replace("\\", "/"))))
    return per_corpus, rows


def cohort(rows):
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    new = 0
    for r in rows:
        if r["key"] not in seen:
            seen[r["key"]] = dict(first=TODAY.isoformat(), cls=r["cls"], gated=r["gated"])
            new += 1
    SEEN.write_text(json.dumps(seen, indent=0))
    return seen, new


def rate(rows, pred):
    sub = [r for r in rows if pred(r)]
    return (sum(x["broken"] for x in sub), len(sub))


def score(rows, seen):
    """Evaluate any prediction whose due date has arrived. regate writes it, or refuses.

    Returns (scored, refused, overdue_manual):
      scored          [(entry id, the prediction as regate wrote it)]
      refused         [(entry id, prediction, regate's first FAILED line)]
      overdue_manual  [(entry id, prediction)] — due, unscored, metric not ours to compute
    """
    if not REG.exists():
        return [], [], []
    reg = json.loads(REG.read_text())
    scored, refused, overdue = [], [], []
    born_after = {k for k, v in seen.items() if v["first"] > "2026-09-22"}
    for eid, entry in reg.items():
        for p in entry.get("predictions", []):
            if p.get("scored") or p["due"] > TODAY.isoformat():
                continue
            m = p["metric"]
            if m not in AUTOMATIC:
                overdue.append((eid, p))
                continue
            outcome = note = None
            if m == "abs_breaks_more_than_rel":
                a_b, a_n = rate(rows, lambda r: r["key"] in born_after and r["cls"] == "d2_abs")
                r_b, r_n = rate(rows, lambda r: r["key"] in born_after and r["cls"] == "d2_rel")
                if a_n < p.get("min_n", 30):
                    outcome, note = "none", (f"UNDERPOWERED: only {a_n} new absolute "
                                             f"references (needed {p['min_n']})")
                else:
                    gap = (a_b / a_n) - (r_b / r_n if r_n else 0)
                    outcome = 1 if gap > 0 else 0
                    note = (f"abs {a_b}/{a_n}={a_b/max(a_n,1):.1%} vs rel "
                            f"{r_b}/{r_n}={r_b/max(r_n,1):.1%}, gap {gap:+.1%}")
            elif m == "gated_breaks_less_than_ungated":
                g_b, g_n = rate(rows, lambda r: r["key"] in born_after and r["gated"])
                u_b, u_n = rate(rows, lambda r: r["key"] in born_after and not r["gated"])
                if g_n < p.get("min_n", 30):
                    outcome, note = "none", (f"UNDERPOWERED: only {g_n} gated references "
                                             f"(needed {p['min_n']}) — the protocol did not "
                                             f"produce enough claims to be falsifiable")
                else:
                    gap = (g_b / g_n) - (u_b / u_n if u_n else 0)
                    outcome = 1 if gap < 0 else 0
                    note = (f"gated {g_b}/{g_n}={g_b/max(g_n,1):.1%} vs ungated "
                            f"{u_b}/{u_n}={u_b/max(u_n,1):.1%}")

            r = regate("score", p["id"], "--outcome", str(outcome), "--note", note)
            if r.returncode == 0:
                fresh = json.loads(REG.read_text())          # regate wrote it, not us
                written = [q for e in fresh.values() for q in e.get("predictions", [])
                           if q.get("id") == p["id"]]
                scored.append((eid, written[0] if written else p))
            else:
                first = next((l.strip() for l in (r.stderr or r.stdout).splitlines()
                              if l.startswith("FAILED")), (r.stderr or r.stdout).strip())
                refused.append((eid, p, first))
    return scored, refused, overdue


def registry_line():
    """One line on whether the registry still says what its ledger says it said."""
    if not REG.exists():
        return "- registry: no registry.json"
    if not (REG.parent / LEDGER).exists():
        return "- registry: NO LEDGER — not yet baselined"
    r = regate("check")
    if r.returncode == 0:
        # regate's own summary line is the authority on these counts. If it does not
        # give a narrative count, repeat what it did say — a guessed 0 would be this
        # file claiming something regate never said.
        summary = next((l.strip() for l in r.stdout.splitlines()
                        if "prediction(s)" in l), "")
        m = re.search(r"(\d+) prediction\(s\), (\d+) narrative\(s\)", summary)
        if m:
            return f"- registry: OK ({m.group(1)} predictions, {m.group(2)} narratives)"
        return f"- registry: OK ({summary})" if summary else "- registry: OK"
    problems = [l for l in (r.stderr or r.stdout).splitlines()
                if l and not l.startswith(("CHECK FAILED", "a prediction that"))]
    return f"- 🔴 registry: {problems[0] if problems else 'check failed'}"


def facts_line():
    """One line on whether the fact base still says what the ledger says it said.

    Report only. This never records and never writes anything under memory/ — the
    fact base has no write path to gate, so the nightly is the backstop that says
    something moved, not the thing that blesses it.
    """
    if not FACTGATE.exists() or not FACTS.is_dir():
        return "- facts: no factgate"
    r = subprocess.run([sys.executable, str(FACTGATE), "--state-dir", str(FACTGATE_STATE),
                        "check", str(FACTS)], capture_output=True, text=True)
    if r.returncode == 0:
        m = re.search(r"(\d+) recorded", r.stdout)
        return f"- facts: OK ({m.group(1)} recorded)" if m else f"- facts: OK"
    problems = [l for l in (r.stderr or r.stdout).splitlines()
                if l and not l.startswith(("CHECK FAILED", "a fact that"))]
    if any("NO LEDGER" in l for l in problems):
        return "- facts: NO LEDGER — not yet baselined"
    if any("LEDGER TAMPERED" in l for l in problems):
        return f"- 🔴 facts: {problems[0]}"
    counts, names = {"NEW": [], "CHANGED": [], "DELETED": []}, []
    for l in problems:
        for key in counts:
            if l.startswith(f"{key} OFF-LEDGER: "):
                counts[key].append(l.split(": ", 1)[1].split(" ")[0])
    names = counts["NEW"] + counts["CHANGED"] + counts["DELETED"]
    if not names:
        return f"- 🔴 facts: {problems[0]}"
    shown = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    return (f"- 🔴 facts: {len(counts['NEW'])} new, {len(counts['CHANGED'])} changed, "
            f"{len(counts['DELETED'])} deleted off-ledger — {shown}")


def interim_line():
    """One line (order 22b): how many facts are INTERIM — Odin's account, no witness yet — and
    which carry verdict words. factgate's own summary, repeated. Report only; never raises."""
    if not FACTGATE.exists():
        return "- interim: no factgate"
    try:
        r = subprocess.run([sys.executable, str(FACTGATE), "--state-dir", str(FACTGATE_STATE),
                            "interim", str(FACTS)], capture_output=True, text=True,
                           timeout=INTERIM_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"- interim: unavailable (timed out after {INTERIM_TIMEOUT} s)"
    except OSError as e:
        return f"- interim: unavailable (could not run: {e.strerror})"
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("interim: ")), None)
    if line is None:
        return f"- interim: unavailable (exit {r.returncode}, no summary line)"
    return f"- {line}"


def _retired_now():
    """memretrieve.retirements() over the fact base (and its archive). Reads only; may raise."""
    if str(MEMRETRIEVE) not in sys.path:
        sys.path.append(str(MEMRETRIEVE))
    import memretrieve
    return memretrieve.retirements(memretrieve._read_base(str(FACTS)))


def retired_pairs():
    """Tonight's retirements as sorted "B→A" pairs, for the history row; None if they cannot be read."""
    try:
        return sorted({f"{r['b']}→{r['a']}" for r in _retired_now()["applied"]})
    except Exception:
        return None


def _three(items):
    return ", ".join(items[:3]) + (" …" if len(items) > 3 else "")


def retired_line():
    """ORDER 70: one line on passages a newer fact has retired in an older one — how many, in how many
    facts, which lines could not be resolved (🟡, first 3), and what is new since the previous history
    row. A new retirement in a PINNED fact is named every night it is new. Report only; never raises."""
    try:
        ret = _retired_now()
        applied, unres = ret["applied"], ret["unresolved"]
        line = f"- retired: {len(applied)} passages in {len({r['a'] for r in applied})} facts · "
        if unres:
            line += f"🟡 {len(unres)} unresolved (" + "; ".join(
                f"{u['b']} line {u['line']}: {u['why']}" for u in unres[:3]) + (" …" if len(unres) > 3 else "") + ")"
        else:
            line += "0 unresolved"
        rows = [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()] if HISTORY.exists() else []
        prev = [r for r in rows if r.get("date", "") < TODAY.isoformat()]
        before = set((prev[-1].get("retired") or []) if prev else [])
        new = [p for p in sorted({f"{r['b']}→{r['a']}" for r in applied}) if p not in before]
        if new:
            line += f" · new since yesterday: {len(new)} ({_three(new)})"
            pins = ({l.strip() for l in PINNED.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.lstrip().startswith("#")} if PINNED.exists() else set())
            pinned = [p for p in new if p.split("→", 1)[1] in pins]
            if pinned:
                line += " · 📌 pinned fact changed: " + ", ".join(pinned)
        return line
    except Exception as e:
        return f"- retired: 🔴 did not run ({type(e).__name__}: {e})"


def categories_line():
    """ORDER 73b (item 28, phase B): where each fact's category comes from — D declared (its own
    Category: line), A assigned (the ASSIGNED.csv sidecar), U none (🟡, first 3) — and B declared
    lines naming something not on the list (🟡, first 3). Read only, via categories.census (no second
    parser). Report only; never raises."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("nightly_fact_categories", CATEGORIES / "categories.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cats = mod.load(CATEGORIES / "CATEGORIES.txt")
        c = mod.census(FACTS, cats, mod.load_assigned(CATEGORIES_ASSIGNED, cats))
        line = f"- categories: {len(c['declared'])} declared · {len(c['assigned'])} assigned · "
        line += f"🟡 {len(c['none'])} none ({_three(c['none'])})" if c["none"] else "0 none"
        if c["bad"]:
            line += f" · 🟡 {len(c['bad'])} bad declared lines ({_three([f for f, _ in c['bad']])})"
        return line
    except Exception as e:
        return f"- categories: 🔴 did not run ({type(e).__name__}: {e})"


def vault_line():
    """One line: anchor the three ledgers in the root-owned vault, then verify it.

    The add's exit code is reported, never worked around; verify runs whatever the add did.
    Nothing here may raise — a vault failure must not stop the rest of the nightly.
    """
    if VAULT_VERIFY == ["/usr/local/bin/helix-vault-verify"] and not __import__("os").path.exists(VAULT_VERIFY[0]):
        return "- vault: not installed (optional, see vault/README.md)"
    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=VAULT_TIMEOUT)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return None, "", f"timed out after {VAULT_TIMEOUT} s"
        except OSError as e:
            return None, "", f"could not run: {e.strerror}"

    def first(text, default):
        return next((l.strip() for l in text.splitlines() if l.strip()), default)

    code, out, err = run(VAULT_ADD)
    drive = next((l[len("drive: "):] for l in out.splitlines() if l.startswith("drive: ")), "(no drive line)")
    if code == 0:
        try:
            add = f"seq {json.loads(out.splitlines()[0])['seq']} anchored · drive {drive}"
        except (ValueError, IndexError, KeyError, TypeError):
            add = f"🔴 add exit 0 but no anchor line: {first(out, 'no output')}"
    elif code is None:
        add = f"🔴 add {err}"
    else:
        add = f"🔴 add exit {code}: {first(err, first(out, 'no output'))}"

    code, out, err = run(VAULT_VERIFY)
    if code == 0:
        verify = "verify OK"
    elif code is None:
        verify = f"verify FAIL: {err}"
    else:
        ok = re.compile(r"^(chain OK|[\w ]+: (OK|NOT ANCHORED|BEHIND)\b)")
        verify = "verify FAIL: " + next((l.strip() for l in out.splitlines() if l.strip() and not ok.match(l)),
                                        first(err, f"exit {code}"))
    return f"- vault: {add} · {verify}"


def machine_line():
    """ORDER 67 (item 32): the machine Helix runs on. machine.py reads disks, temperature, GPU, failed
    units, reboot-required and updates, and runs SMART / update / clean as root only through the
    helix-machine allowlist. One line, 🔴/🟡 sub-lines per problem. Never stops the nightly."""
    try:
        import machine
        return machine.machine_line()
    except Exception as e:
        return f"- machine: 🔴 machine check did not run ({type(e).__name__}: {e})"


def gaps_line():
    """ORDER 36: orphans of the last 7 days — work no record names. Never stops the nightly."""
    try:
        if str(GAPAUDIT) not in sys.path:
            sys.path.append(str(GAPAUDIT))
        import gapaudit
        return gapaudit.gaps_line()
    except Exception as e:
        return f"- gaps: 🔴 gap audit did not run ({type(e).__name__}: {e})"


def _correction_rate():
    if str(METRIC) not in sys.path:
        sys.path.append(str(METRIC))
    import correction_rate
    return correction_rate


def correction_line():
    """ORDER 37: share, before:after for the last aged month, median time-to-correction."""
    try:
        return _correction_rate().correction_line(today=TODAY)
    except Exception as e:
        return f"- correction: 🔴 metric did not run ({type(e).__name__}: {e})"


def correction_report_lines():
    """ORDER 37: the full correction-rate report, on the first night of the month only."""
    if TODAY.day != 1:
        return []
    try:
        m = _correction_rate()
        return ["", *m.render(m.compute(today=TODAY)).rstrip("\n").splitlines()]
    except Exception as e:
        return ["", f"- correction report: 🔴 did not run ({type(e).__name__}: {e})"]


def house_line():
    """ORDER 38: disks, RAM, temps, drive, and what housekeeping WOULD move (dry run). It writes
    today's plan and nothing else. Anomalies are reported, never fixed. Never stops the nightly."""
    try:
        if str(HOUSEKEEPING) not in sys.path:
            sys.path.append(str(HOUSEKEEPING))
        import housekeeping
        return housekeeping.house_line(today=TODAY)
    except Exception as e:
        return f"- house: 🔴 housekeeping did not run ({type(e).__name__}: {e})"


def canary_line():
    """ORDER 44 (item 12): planted false things the gates must catch, true things they must let
    through, run on temp copies of the gates; nothing live is seeded. Appends one line to
    CANARY.jsonl. Never stops the nightly."""
    try:
        if str(CANARY) not in sys.path:
            sys.path.append(str(CANARY))
        import canary
        return canary.canary_line()
    except Exception as e:
        return f"- canary: 🔴 canary did not run ({type(e).__name__}: {e})"


def replay_line():
    """ORDER 45 (item 26): replay ONE stored pack at random, analysis stage only (stored inputs
    → reported numbers), compared within each number's declared tolerance. Appends one line to
    REPLAYS.jsonl; never repairs or rewrites a pack. Never stops the nightly."""
    try:
        if str(REPLAY) not in sys.path:
            sys.path.append(str(REPLAY))
        import replay
        return replay.replay_line()
    except Exception as e:
        return f"- replay: 🔴 replay did not run ({type(e).__name__}: {e})"


def coldread_line():
    """ORDER 46 (item 11): the last monthly cold read, from READS.jsonl only: the plant FOUND or
    MISSED, the top false thing, the next date. It never starts a read. Never stops the nightly."""
    try:
        if str(COLDREAD) not in sys.path:
            sys.path.append(str(COLDREAD))
        import coldread
        return coldread.coldread_line()
    except Exception as e:
        return f"- coldread: 🔴 coldread did not run ({type(e).__name__}: {e})"


def corrections_line():
    """ORDER 72 (item 30): cold-read findings as a correction queue. Harvests a new monthly read into
    QUEUE.jsonl first (findings only; it never edits a fact), then reports open / closed / rejected,
    🟡 with the first 3 open when the oldest is over 30 days. Report only; never raises."""
    try:
        if str(CORRECTIONS) not in sys.path:
            sys.path.append(str(CORRECTIONS))
        import corrections
        corrections.harvest()
        return corrections.corrections_line(TODAY)
    except Exception as e:
        return f"- corrections: unavailable — {type(e).__name__}: {e}"


def reviews_line():
    """ORDER 47 (item 9): reviewer rotation, from REVIEWS.jsonl only: seals assigned, Alice vs
    Claude-cold, and whether the hash chain holds. Reads only. Never stops the nightly."""
    try:
        if str(SEALGATE) not in sys.path:
            sys.path.append(str(SEALGATE))
        import review
        return review.reviews_line()
    except Exception as e:
        return f"- reviews: 🔴 reviews did not run ({type(e).__name__}: {e})"


def veto_line():
    """ORDER 48 (amendment 9a): Alice's veto, from VETOES.jsonl only: runs, and (order 48b) breaks
    confirmed / disputed / invalid, order 48's legacy reproduced breaks, and whether the hash
    chain holds. Reads only; never calls Alice or the judge. Never stops the nightly."""
    try:
        if str(VETO) not in sys.path:
            sys.path.append(str(VETO))
        import veto
        return veto.veto_line()
    except Exception as e:
        return f"- veto: 🔴 veto did not run ({type(e).__name__}: {e})"


def seatswap_line():
    """ORDER 49 (item 10): seat-swap replication, from SWAPS.jsonl only: runs, agree, disagree, and
    whether the hash chain holds. Reads only; never calls Alice. Never stops the nightly."""
    try:
        if str(SEATSWAP) not in sys.path:
            sys.path.append(str(SEATSWAP))
        import seatswap
        return seatswap.seatswap_line()
    except Exception as e:
        return f"- seatswap: 🔴 seatswap did not run ({type(e).__name__}: {e})"


def provenance_line():
    """ORDER 51 (item 23): the provenance DAG over the registry's entries: entries, declared edges,
    undeclared (legacy, reported not refused), quarantined (each path on a 🔴 sub-line), cycles.
    Computed by regate from the registry and its ledgers; reads only. Never stops the nightly."""
    try:
        if str(PROVENANCE) not in sys.path:
            sys.path.append(str(PROVENANCE))
        import regate as regate_gate
        return regate_gate.provenance_line(REG)
    except Exception as e:
        return f"- provenance: 🔴 provenance did not run ({type(e).__name__}: {e})"


def proposals_line():
    """ORDER 52 (item 21): the proposal engine, from PROPOSALS / BATCHES / LEADS only: pending,
    approved, rejected, the friction ratio, leads, the last batch, and whether the chains hold.
    Never runs a batch. Never stops the nightly."""
    try:
        if str(PROPOSALS) not in sys.path:
            sys.path.append(str(PROPOSALS))
        import propose
        return propose.proposals_line()
    except Exception as e:
        return f"- proposals: 🔴 proposals did not run ({type(e).__name__}: {e})"


def timeforward_line():
    """ORDER 56 (P-20261004-5): this file's registry work, dry-run on the day after each unscored due
    date by timeforward.py, on a temp copy of the registry with the date moved there only. Nothing
    here chooses a date. Never stops the nightly."""
    try:
        import timeforward
        return timeforward.timeforward_line(REG.parent)
    except Exception as e:
        return f"- timeforward: 🔴 timeforward did not run ({type(e).__name__}: {e})"


def hookcorpus_line():
    """ORDER 63 (P-20261004-10): the hook's corpus of logged catches (HOOK_CORPUS in
    test_factgate_hook.py), run by hookcorpus.py through the brain's venv pytest. Its refusals go to
    a temp catches file; its record goes to HOOKCORPUS.jsonl, never the catches log. Never stops
    the nightly."""
    try:
        import hookcorpus
        return hookcorpus.line()
    except Exception as e:
        return f"- hookcorpus: 🔴 hookcorpus did not run ({type(e).__name__}: {e})"


def needs_seal_lines():
    """An unsealed experiment coming due needs a seal attached BEFORE it does.

    An experiment may only be scored early while its pre-registration verifies, so one
    with no prereg and a due date approaching is a deadline nobody is watching. Reads
    the registry; writes nothing. Past due is already the OVERDUE (manual) line, so the
    window starts strictly after today and this never doubles up with it.
    """
    if not REG.exists():
        return []
    lines = []
    for eid, entry in json.loads(REG.read_text()).items():
        for p in entry.get("predictions", []):
            if p.get("scored") or p.get("kind") != "experiment" or p.get("prereg"):
                continue
            try:
                due = datetime.date.fromisoformat(p["due"])
            except (KeyError, ValueError):
                continue
            days = (due - TODAY).days
            if 0 < days <= 30:
                lines.append(f"  - 🟡 NEEDS SEAL: #{eid} {p['id']} due {p['due']} "
                             f"({days} days), no prereg attached")
    return lines


def summary_lines(scored, refused, overdue):
    """The indented per-prediction lines for NIGHTLY.md."""
    lines = []
    for eid, p, why in refused:
        lines.append(f"  - 🔴 SCORE REFUSED #{eid} {p['id']}: {why}")
    for eid, p in scored:
        lines.append(f"  - **#{eid}** {p['id']}: brier={p['brier']} — {p['note']}")
    for eid, p in overdue:
        lines.append(f"  - 🟡 OVERDUE (manual): #{eid} {p['id']}, due {p['due']}")
    return lines


def _single_disk_filter(lines):
    """memory/machine.json (written by install.sh) says whether the big-files folder is its own disk.
    If it is not, "not mounted" is the normal state, so those alarms are dropped, not shown red."""
    try:
        m = json.loads((HOME / "helix" / "memory" / "machine.json").read_text())
    except Exception:
        return lines
    if m.get("bulk_is_separate_disk"):
        return lines
    out = []
    for ln in lines:
        if ln.startswith("- house:"):
            ln = " · ".join(s for s in ln.split(" · ") if "not mounted" not in s.lower())
        if ln.startswith("- replay:") and "not mounted" in ln.lower():
            ln = "- replay: skipped (the big-files folder is not a separate disk)"
        out.append(ln)
    return out


def main():
    per_corpus, rows = audit()
    seen, new = cohort(rows)
    scored, refused, overdue = score(rows, seen)
    row = dict(date=TODAY.isoformat(), corpora=per_corpus, claims=len(rows), new_today=new,
               by_class={k: [sum(x["broken"] for x in rows if x["cls"] == k),
                             sum(1 for x in rows if x["cls"] == k)]
                         for k in sorted({x["cls"] for x in rows})},
               retired=retired_pairs())
    with HISTORY.open("a") as f:
        f.write(json.dumps(row) + "\n")

    hist = [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()]
    prev = hist[-2] if len(hist) > 1 else None
    # 🔴 This audit was off from 2026-09-08 to 2026-09-22 and nothing noticed. A run that
    # follows a gap says so on its own first line; silence is the failure mode that hid the
    # last one. (It cannot see a gap while it is not running — only when it comes back.)
    gap_note = ""
    if prev:
        d0 = datetime.date.fromisoformat(prev["date"])
        gap = (TODAY - d0).days
        if gap > 2:
            gap_note = f"  🔴 **{gap} days since the previous run** — the audit was not running."

    b_now = sum(v["broken"] for v in per_corpus.values())
    e_now = per_corpus["facts"]["epsilon"]
    d_b = f"{b_now - sum(v['broken'] for v in prev['corpora'].values()):+d}" if prev else "—"
    d_e = f"{e_now - prev['corpora']['facts']['epsilon']:+.1%}" if prev else "—"
    lines = [f"# Nightly audit — {TODAY.isoformat()}" + gap_note,
             f"- broken references: **{b_now}** ({d_b} since last run)",
             f"- ε on the fact base: **{e_now:.1%}** ({d_e}) — the share the instrument cannot check",
             f"- claims tracked: {len(rows)}, of which {new} first seen today",
             f"- predictions due: {len(scored) + len(refused) + len(overdue)}",
             registry_line(),
             facts_line(),
             interim_line(),
             retired_line(),
             categories_line(),
             vault_line(),
             heartbeat_line(HERE / "HEARTBEAT.jsonl"),
             machine_line(),
             gaps_line(),
             correction_line(),
             house_line(),
             canary_line(),
             replay_line(),
             coldread_line(),
             corrections_line(),
             reviews_line(),
             veto_line(),
             seatswap_line(),
             provenance_line(),
             proposals_line(),
             timeforward_line(),
             hookcorpus_line(),
             ]
    lines.extend(summary_lines(scored, refused, overdue))
    lines.extend(needs_seal_lines())
    if not (scored or refused or overdue):
        nxt = sorted([(p["due"], eid, p["id"]) for eid, e in
                      (json.loads(REG.read_text()).items() if REG.exists() else [])
                      for p in e.get("predictions", []) if not p.get("scored")])
        if nxt:
            lines.append(f"  - next: #{nxt[0][1]} {nxt[0][2]} on {nxt[0][0]}")
    lines.extend(correction_report_lines())
    lines = _single_disk_filter(lines)
    (HERE / "NIGHTLY.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
