#!/usr/bin/env python3
"""coldread.py — the monthly cold read (order 46/46b, item 11).

Monthly, a cold session receives the ENTIRE fact base and one instruction: find the biggest thing
in here that is FALSE. Not anchored claims, not references: beliefs. The output is an OPINION:
it never edits a fact and never scores anything. Odin reads it and puts it to the user.

    coldread.py run [--dry]
    coldread.py status [--json]

1. Snapshot: `memory/facts/*.md` (top level only), selected by NAME, copied to the run's own temp
   dir. A name containing `credential`, `accounts` or `.env` (lowercased) is never opened, hashed
   or copied (BUILD_PROTOCOL §1, ruling of order 45b).
2. Positive control: ONE planted false fact from PLANTS, numbered (highest number in the snapshot)
   + 1..9 at run time (a gap belongs to an archived fact live facts still cite: ruling 3 of 46b).
   The reader is not told. FOUND = any stage-1 candidate's `file` field names the plant's file.
   MISSED → the read is 🔴: its "nothing big is false" means nothing.
3. Stage 1: whole files, sorted by name, packed into batches of ≤ BATCH_CAP chars; each batch to a
   fresh session, which names its 3 most-likely-false statements (JSON).
4. Stage 2: one fresh session gets every stage-1 candidate (the plant's filtered out AFTER FOUND is
   recorded, and any candidate whose quote/why mention it) plus the full text of every file they
   name, and picks the single biggest false thing and a short ranked runner-up list (JSON).
5. Cold: `claude -p` with the batch on stdin FROM A REGULAR FILE (ruling 1: a batch can't be one
   argv string, MAX_ARG_STRLEN = 131,072), the read's own instruction as the system prompt, no
   tools, no MCP, no skills, no slash commands, no settings, no session persistence, an empty temp
   cwd. Every session's init event is checked: tools, MCP servers, skills or memory paths
   non-empty → 🔴 NOT COLD, recorded, no further sessions. The two builtin plugins (`agents-md`,
   `telemetry`) remain, no flag removes them; accepted (ruling 2) and recorded in every run.
6. Cost: ≤ RUN_CAP_USD a run, SESSION_CAP_USD a session. A session is not started if the spend so
   far plus its cap would pass the run's cap → ABORTED (budget). Never retried automatically.
7. Output: READS/<date>.md and one line in READS.jsonl. The temp dir is removed by this script.

Outcomes: DONE · ABORTED (budget) · NOT_COLD · FAILED (a session errored or gave no JSON).
`--dry` runs the whole pipeline with a fake model (no `claude` call, no spend) and writes its
output to a temp dir, never the live ledgers.
"""

import argparse
import calendar
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
READS = HERE / "READS.jsonl"
READS_DIR = HERE / "READS"
FACTS = Path.home() / "helix" / "memory" / "facts"
CLI = Path.home() / ".local" / "bin" / "claude"          # as builder_run.sh finds it
CONFIG_DIR = Path.home() / "claude" / "config"           # as run_order.sh sets it
FENCED = ("credential", "accounts", ".env")              # BUILD_PROTOCOL §1, by name, lowercased
BATCH_CAP = 220_000                                      # chars of file text per stage-1 batch
RUN_CAP_USD = 12.0
SESSION_CAP_USD = 2.50
SESSION_TIMEOUT = 1500                                   # seconds per session
OVERDUE_DAYS = 35
TOP_CHARS = 80

INSTRUCTION = (
    "You are a cold reader. You are given files from a personal AI assistant's fact base: the "
    "beliefs it carries about its owner, its machines, its work and itself. You have no tools and "
    "no other context; judge only from the text you are given.\n"
    "Your one job: find the biggest thing in here that is FALSE. Not anchored claims, not "
    "references: beliefs. A statement is a candidate if it is false, internally contradictory, or "
    "contradicted by another file you were given. Quote exactly. Answer with JSON only.")

STAGE1_ASK = (
    "Below are files from the fact base, each headed `=== FILE: <name> ===`.\n"
    "Name the 3 statements in these files you are most confident are FALSE, internally "
    "contradictory, or contradicted by another file here. For each: the file name, an exact "
    "quote, why it is false, and your confidence from 0 to 1.\n"
    'Answer with JSON only: {"candidates": [{"file": "<name>", "quote": "<exact quote>", '
    '"why": "<reason>", "confidence": <0..1>}, ...]}')

STAGE2_ASK = (
    "Readers of the whole fact base, in batches, each named the statements they were most "
    "confident are false. Their candidates are below, then the full text of every file a "
    "candidate names, each headed `=== FILE: <name> ===`.\n"
    "Pick the single biggest false thing in the fact base, and a short ranked list of up to 5 "
    "runners-up. Quote exactly and say why each is false.\n"
    'Answer with JSON only: {"verdict": {"file": "<name>", "quote": "<exact quote>", '
    '"why": "<reason>"}, "runners_up": [{"file": "<name>", "quote": "<exact quote>", '
    '"why": "<reason>"}, ...]}')

# The positive controls: each states something flatly false, in the house style, and is
# contradicted by NAMED real facts (checked by the tests against memory/facts, by name).
PLANTS = [
    {"id": "box2_worker", "slug": "builders_run_on_box2_worker_node",
     "title": "HELIX'S BUILDERS RUN ON BOX2",
     "claim": "Helix's builders run on BOX2 (`otheruser@192.168.77.5`), which is its worker node.",
     "body": "- Builder sessions are dispatched over SSH; BOX1 only hosts the chat and the nightly.",
     "contradicted_by": ["163_example_4163.md",
                         "164_example_fadb.md"]},
    {"id": "car_loans_sek", "slug": "car_loans_billing",
     "title": "CAR LOANS: CURRENCY AND LENDER",
     "claim": "The two car loans are billed in SEK by Volvo Financial Services.",
     "body": "- Both statements arrive monthly; convert at the day's rate before filing.",
     "contradicted_by": ["103_example_6060.md"]},
    {"id": "ravens", "slug": "odin_alice_name_origin",
     "title": "WHERE THE NAMES ODIN AND ALICE COME FROM",
     "claim": "Odin and Alice are named after the ravens Huginn and Muninn.",
     "body": "- The names were picked for the charter's Norse framing: thought and memory.",
     "contradicted_by": ["174_example_368e.md"]},
]

FILE_HEAD = "=== FILE: {} ==="


class NotCold(Exception):
    """The session's init event shows tools, MCP servers, skills or memory it must not have."""


class Failed(Exception):
    """A session errored or its answer had no JSON."""


# --- 1. snapshot -----------------------------------------------------------------------------

def fenced(name):
    low = name.lower()
    return any(f in low for f in FENCED)


def snapshot(facts, dest):
    """Copy facts/*.md (top level) into dest, by name. → (kept names, number excluded). A fenced
    name is decided on the name alone: never opened, hashed or copied."""
    kept, excluded = [], 0
    for p in sorted(facts.glob("*.md")):
        if fenced(p.name):
            excluded += 1
            continue
        if not p.is_file():
            continue
        shutil.copy2(p, dest / p.name)
        kept.append(p.name)
    return kept, excluded


# --- 2. the plant ----------------------------------------------------------------------------

def plant_number(names, rng):
    """(highest leading number among names) + 1..9, chosen at run time. Never a gap."""
    nums = [int(m.group(1)) for n in names if (m := re.match(r"(\d+)_", n))]
    return max(nums, default=0) + rng.randint(1, 9)


def plant_text(p, today):
    return f"# 🟢 {p['title']} ({today.isoformat()})\n\n{p['claim']}\n{p['body']}\n"


def names_plant(cand, plant_file):
    """FOUND counts the candidate's `file` field only (its basename, backticks stripped)."""
    f = str(cand.get("file", "")).strip().strip("`'\" ")
    return f.rsplit("/", 1)[-1] == plant_file


def stage2_candidates(cands, plant):
    """Drop the plant: by file, and any candidate whose quote or why mention its file or claim."""
    claim = plant["claim"].lower().rstrip(".")
    out = []
    for c in cands:
        text = f"{c.get('quote', '')} {c.get('why', '')}".lower()
        if names_plant(c, plant["file"]) or plant["file"].lower() in text or claim in text:
            continue
        out.append(c)
    return out


# --- 3. batching -----------------------------------------------------------------------------

def make_batches(texts, cap=None):
    """Whole files, sorted by name, packed in order into batches of ≤ cap chars of file text.
    A file bigger than the cap on its own rides alone."""
    cap = BATCH_CAP if cap is None else cap
    batches, cur, size = [], [], 0
    for name in sorted(texts):
        n = len(texts[name])
        if cur and size + n > cap:
            batches.append(cur)
            cur, size = [], 0
        cur.append(name)
        size += n
    if cur:
        batches.append(cur)
    return batches


def render(names, texts):
    return "\n".join(f"{FILE_HEAD.format(n)}\n{texts[n].rstrip()}\n" for n in names)


# --- 5. the session --------------------------------------------------------------------------

def session_command(system, prompt, workdir):
    """→ (argv, stdin file, cwd, env). The prompt is written to a regular file in the run's temp
    dir and goes in on stdin; no argv element carries it (ruling 1)."""
    workdir = Path(workdir)
    k = len(list(workdir.glob("prompt_*.txt"))) + 1
    stdin_path = workdir / f"prompt_{k}.txt"
    stdin_path.write_text(prompt, encoding="utf-8")
    cwd = workdir / f"cwd_{k}"
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
    """→ the list of things a cold session must not have (empty = cold)."""
    if not isinstance(init, dict):
        return ["no init event"]
    return [f"{k}: {init.get(k)!r}" for k in ("tools", "mcp_servers", "skills", "memory_paths")
            if init.get(k)]


def init_record(init):
    if not isinstance(init, dict):
        return None
    plugins = [p.get("name") if isinstance(p, dict) else p for p in init.get("plugins") or []]
    return {"tools": init.get("tools"), "mcp_servers": init.get("mcp_servers"),
            "skills": init.get("skills"), "slash_commands": init.get("slash_commands"),
            "memory_paths": init.get("memory_paths"), "plugins": plugins,
            "agents": init.get("agents"), "model": init.get("model"),
            "apiKeySource": init.get("apiKeySource"), "version": init.get("claude_code_version")}


def claude_session(system, prompt, workdir):
    """One real cold session. Reads the stream as it comes, so a warm init event kills the
    session before the model is asked anything. → {"text", "cost_usd", "init"}."""
    argv, stdin_path, cwd, env = session_command(system, prompt, workdir)
    err_path = Path(workdir) / f"{stdin_path.stem}.stderr"
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
    cost = (result or {}).get("total_cost_usd")
    out = {"init": init, "cost_usd": cost, "rc": proc.returncode}
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


# --- the pipeline ----------------------------------------------------------------------------

def parse_json(text):
    """The first {...} object in text (code fences and prose around it are ignored)."""
    s = re.sub(r"```(?:json)?", "", text or "")
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in the answer")
    return json.loads(s[i:j + 1])


def next_read(today):
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return datetime.date(y, m, 1)


def run(model=None, today=None, seed=None, out_dir=None):
    """The whole read. model(system, prompt, workdir) → {"text", "cost_usd", "init"[, "error"]}.
    Writes one READS/<date>.md and one READS.jsonl line (into out_dir when given). → the record."""
    model = model or claude_session
    today = today or datetime.date.today()
    rng = random.Random(seed)
    reads, reads_dir = (READS, READS_DIR) if out_dir is None else \
        (Path(out_dir) / "READS.jsonl", Path(out_dir) / "READS")
    started = datetime.datetime.now(datetime.timezone.utc)
    rec = {"date": today.isoformat(), "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "outcome": None, "reason": None, "cost_usd": 0.0, "sessions": 0, "cap_usd": RUN_CAP_USD}
    sessions, candidates, verdict = [], [], None
    with tempfile.TemporaryDirectory(prefix="coldread_") as tmp:
        tmp = Path(tmp)
        rec["tmp"] = str(tmp)
        snap = tmp / "snapshot"
        snap.mkdir()
        kept, excluded = snapshot(FACTS, snap)
        pidx = rng.randrange(len(PLANTS))
        p = PLANTS[pidx]
        pfile = f"{plant_number(kept, rng)}_{p['slug']}.md"
        (snap / pfile).write_text(plant_text(p, today), encoding="utf-8")
        texts = {n: (snap / n).read_text(encoding="utf-8", errors="replace") for n in kept + [pfile]}
        batches = make_batches(texts)
        pbatch = next(i for i, b in enumerate(batches, 1) if pfile in b)
        plant = {"index": pidx, "id": p["id"], "file": pfile, "claim": p["claim"],
                 "contradicted_by": p["contradicted_by"], "batch": pbatch, "found": None}
        rec.update(files=len(kept), excluded=excluded, chars=sum(len(texts[n]) for n in kept),
                   batches=len(batches), plant=plant,
                   batch_chars=[sum(len(texts[n]) for n in b) for b in batches])

        def session(label, prompt):
            if rec["cost_usd"] + SESSION_CAP_USD > RUN_CAP_USD:
                raise Aborted()
            out = model(INSTRUCTION, prompt, tmp)
            rec["sessions"] += 1
            cost = out.get("cost_usd")
            rec["cost_usd"] = round(rec["cost_usd"] + (cost or 0.0), 6)
            sessions.append({"label": label, "cost_usd": cost, "init": init_record(out.get("init")),
                             "error": out.get("error")})
            warm = check_cold(out.get("init"))
            if warm:
                raise NotCold("; ".join(warm))
            if out.get("error"):
                raise Failed(f"{label}: {out['error']}"[:300])
            try:
                ans = parse_json(out.get("text"))
            except ValueError as e:
                raise Failed(f"{label}: {e}")
            if rec["cost_usd"] > RUN_CAP_USD:
                raise Aborted()
            return ans

        try:
            for i, b in enumerate(batches, 1):
                ans = session(f"batch {i}", STAGE1_ASK + "\n\n" + render(b, texts))
                for c in ans.get("candidates") or []:
                    if isinstance(c, dict):
                        candidates.append({**c, "batch": i})
            plant["found"] = any(names_plant(c, pfile) for c in candidates)
            real = stage2_candidates(candidates, plant)
            named = sorted({str(c.get("file", "")).strip().strip("`'\" ").rsplit("/", 1)[-1]
                            for c in real} & set(kept))
            prompt = (STAGE2_ASK + "\n\n<candidates>\n"
                      + json.dumps([{k: c.get(k) for k in ("file", "quote", "why", "confidence")}
                                    for c in real], ensure_ascii=False, indent=1)
                      + "\n</candidates>\n\n" + render(named, texts))
            verdict = session("verdict", prompt)
            rec["outcome"] = "DONE"
        except Aborted:
            rec["outcome"], rec["reason"] = "ABORTED", "budget"
        except NotCold as e:
            rec["outcome"], rec["reason"] = "NOT_COLD", str(e)
        except Failed as e:
            rec["outcome"], rec["reason"] = "FAILED", str(e)
    if plant["found"] is None and candidates:
        plant["found"] = any(names_plant(c, pfile) for c in candidates)
    rec["init"] = next((s["init"] for s in sessions if s["init"]), None)
    rec["all_cold"] = bool(sessions) and all(not check_cold(s["init"]) for s in sessions)
    if verdict:
        v = verdict.get("verdict") or {}
        rec["top"] = {"file": v.get("file"), "quote": v.get("quote")}
    rec["ended"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reads_dir.mkdir(parents=True, exist_ok=True)
    md = reads_dir / f"{today.isoformat()}.md"
    k = 2
    while md.exists():
        md = reads_dir / f"{today.isoformat()}_{k}.md"
        k += 1
    rec["report"] = md.name
    md.write_text(render_md(rec, sessions, candidates, verdict), encoding="utf-8")
    with open(reads, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


class Aborted(Exception):
    """The next session could pass the run's cost cap."""


def render_md(rec, sessions, candidates, verdict):
    p = rec["plant"]
    found = {True: "FOUND", False: "🔴 MISSED (the read checked nothing)", None: "not decided"}
    head = {"DONE": "", "ABORTED": f" 🔴 ABORTED ({rec['reason']})",
            "NOT_COLD": " 🔴 NOT COLD", "FAILED": " 🔴 FAILED"}[rec["outcome"]]
    L = [f"# Cold read — {rec['date']}{head}", "",
         "An OPINION from a cold session (order 46/46b, item 11). It edits nothing and scores "
         "nothing. Odin reads it and puts it to the user.", "",
         f"- outcome: **{rec['outcome']}**" + (f" ({rec['reason']})" if rec["reason"] else ""),
         f"- plant: **{found[p['found']]}** · `{p['file']}` in batch {p['batch']} · "
         f"\"{p['claim']}\" · contradicted by {', '.join(f'`{f}`' for f in p['contradicted_by'])}",
         f"- facts: {rec['files']} read, {rec['excluded']} excluded by name, {rec['chars']:,} chars,"
         f" {rec['batches']} batches ({', '.join(f'{c:,}' for c in rec['batch_chars'])} chars)",
         f"- cost: ${rec['cost_usd']:.4f} over {rec['sessions']} sessions (cap ${RUN_CAP_USD:.2f},"
         f" ${SESSION_CAP_USD:.2f} a session)", ""]
    if rec["outcome"] == "NOT_COLD":
        L += [f"🔴 NOT COLD: {rec['reason']}. No further sessions were run.", ""]
    L += ["## Verdict", ""]
    if verdict:
        v = verdict.get("verdict") or {}
        L += [f"**`{v.get('file')}`**", "", f"> {v.get('quote')}", "", f"{v.get('why')}", "",
              "## Runners-up", ""]
        for i, r in enumerate(verdict.get("runners_up") or [], 1):
            if isinstance(r, dict):
                L += [f"{i}. `{r.get('file')}` — \"{r.get('quote')}\" — {r.get('why')}"]
        L += [""]
    else:
        L += ["(none: the read did not reach stage 2)", ""]
    L += ["## Stage-1 candidates (every one, the plant's included)", ""]
    for c in candidates:
        mark = " ← THE PLANT" if names_plant(c, p["file"]) else ""
        L += [f"- batch {c.get('batch')} · `{c.get('file')}` · conf {c.get('confidence')}{mark}",
              f"  - quote: \"{c.get('quote')}\"", f"  - why: {c.get('why')}"]
    L += ["", "## Init events (what each cold session actually had)", ""]
    for s in sessions:
        L += [f"- {s['label']} · ${s['cost_usd'] if s['cost_usd'] is not None else '?'}"
              f"{' · ERROR ' + s['error'] if s['error'] else ''}",
              f"  - `{json.dumps(s['init'], ensure_ascii=False)}`"]
    L += ["", "The builtin plugins (`agents-md`, `telemetry`) remain in a cold session; no flag removes"
          " them. Accepted by ruling 2 of order 46b."]
    return "\n".join(L) + "\n"


# --- the dry model ---------------------------------------------------------------------------

def dry_model(system, prompt, workdir):
    """No spend: builds the real command (so --dry exercises it) and answers by naming the first
    file of each batch, and the plant if it is there."""
    session_command(system, prompt, workdir)
    files = re.findall(r"^=== FILE: (\S+) ===$", prompt, re.M)
    if prompt.startswith(STAGE2_ASK):
        first = files[0] if files else ""
        out = {"verdict": {"file": first, "quote": "(dry)", "why": "(dry)"}, "runners_up": []}
    else:
        pick = files[:1] + [f for f in files if any(f.endswith(f"_{p['slug']}.md") for p in PLANTS)]
        out = {"candidates": [{"file": f, "quote": "(dry)", "why": "(dry)", "confidence": 0.5}
                              for f in pick]}
    return {"text": json.dumps(out), "cost_usd": 0.0,
            "init": {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                     "skills": [], "memory_paths": None, "plugins": []}}


# --- status ----------------------------------------------------------------------------------

def last_read():
    if not READS.exists():
        return None
    rows = [json.loads(l) for l in READS.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-1] if rows else None


def status(today=None):
    today = today or datetime.date.today()
    last = last_read()
    return {"last": last, "next": next_read(today).isoformat(),
            "overdue": bool(last) and
            (today - datetime.date.fromisoformat(last["date"])).days > OVERDUE_DAYS}


def coldread_line(today=None):
    """The nightly's line. Reads READS.jsonl only; never starts a read."""
    st = status(today)
    last, nxt = st["last"], st["next"]
    if not last:
        return f"- coldread: none yet · next {nxt}"
    d = last["date"]
    if st["overdue"]:
        return f"- coldread: 🔴 overdue (last {d})"
    if last["outcome"] == "ABORTED":
        return f"- coldread: 🔴 {d} ABORTED ({last['reason']})"
    if last["outcome"] == "NOT_COLD":
        return f"- coldread: 🔴 {d} NOT COLD ({last['reason'][:80]}) · next {nxt}"
    if last["outcome"] == "FAILED":
        return f"- coldread: 🔴 {d} FAILED ({last['reason'][:80]}) · next {nxt}"
    if not last["plant"]["found"]:
        return f"- coldread: 🔴 {d} · plant MISSED (the read checked nothing) · next {nxt}"
    top = last.get("top") or {}
    quote = " ".join(str(top.get("quote") or "").split())
    if len(quote) > TOP_CHARS:
        quote = quote[:TOP_CHARS - 1] + "…"
    return f"- coldread: {d} · plant FOUND · top: {top.get('file')} — {quote} · next {nxt}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coldread.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1) if a.json else coldread_line())
        return 0
    if a.dry:
        out = Path(tempfile.mkdtemp(prefix="coldread_dry_"))
        rec = run(model=dry_model, out_dir=out)
        print(f"DRY RUN — no claude call, no spend · {rec['outcome']} · {rec['files']} files, "
              f"{rec['excluded']} excluded, {rec['batches']} batches "
              f"({', '.join(f'{c:,}' for c in rec['batch_chars'])} chars) · plant {rec['plant']['file']}"
              f" in batch {rec['plant']['batch']} · found {rec['plant']['found']} · output {out}")
        return 0
    rec = run()
    print(coldread_line())
    return 0 if rec["outcome"] == "DONE" and rec["plant"]["found"] else 4


if __name__ == "__main__":
    sys.exit(main())
