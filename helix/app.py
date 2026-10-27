#!/usr/bin/env python3
"""ODIN — persistent multi-model brain with voice. v0.1
Default = you + Claude (subscription). '/council' convenes the specialist bench on demand."""
import os, re, sys, subprocess, time, json, hashlib, signal, threading, asyncio, queue, io, uuid, tempfile
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory, Response, make_response
import scheduler
import frontdesk
import requests

BRAIN   = os.path.dirname(os.path.abspath(__file__))
HOME    = os.path.expanduser("~")                      # ported to this box: was hardcoded __HOME__
MEMORY  = f"{BRAIN}/memory/BRAIN.md"
CHARTER = f"{HOME}/colab/COLAB.md"
PIPER   = f"{BRAIN}/piper/piper/piper"
VOICE   = f"{BRAIN}/voices/alan.onnx"
TTSDIR  = f"{BRAIN}/tts"; SESS = f"{BRAIN}/memory/sessions"
FACTS   = f"{BRAIN}/memory/facts"   # durable per-fact memory (one file = one verified fact)
NOTEPAD = frontdesk.notepad_path(BRAIN)              # Helix's pad — see frontdesk.py
FRONT_ON = os.environ.get("HELIX_NO_FRONT") != "1"   # kill switch: everything goes to Odin
VOSK_MODEL = f"{BRAIN}/models/vosk-model-small-en-us-0.15"   # offline speech-to-text (voice input)
WORKSPACE  = f"{BRAIN}/workspace"   # ODIN's agentic scratch dir (where his tools do file work)
KOKORO_ONNX   = f"{BRAIN}/models/kokoro/kokoro-v1.0.onnx"    # natural CPU TTS (replaced Piper)
KOKORO_VOICES = f"{BRAIN}/models/kokoro/voices-v1.0.bin"
KOKORO_VOICE  = "bm_fable"    # fallback voice name if the blend file is missing
KOKORO_BLEND  = f"{BRAIN}/models/kokoro/odin_voice.npy"   # signature ODIN blend (bm_fable+am_onyx), used if present
KOKORO_SPEED  = 1.05          # deliberate, authoritative pace (Jarvis-ish)
KOKORO_ROBO   = 0.26          # robotic sheen: 0=off, ~0.12 subtle, ~0.20 hint, ~0.26 current, ~0.32 strong
KOKORO_PITCH  = 0.94          # pitch factor <1 = lower + slightly slower (0.94 ≈ 1 semitone down)
# --- HELIX: ALICE (Gemini-driven design/audit mind, the twin of ODIN) ---
ALICE_MD      = f"{BRAIN}/memory/ALICE.md"
ALICE_DRAWER  = f"{BRAIN}/memory/alice"
ALICE_MODEL   = "Gemini 3.1 Pro (High)"           # top reasoning tier via agy
KOKORO_ALICE  = f"{BRAIN}/models/kokoro/alice_voice.npy"   # British-female robo blend
os.makedirs(TTSDIR, exist_ok=True); os.makedirs(SESS, exist_ok=True); os.makedirs(FACTS, exist_ok=True)
os.makedirs(WORKSPACE, exist_ok=True)

# Pin the Claude config dir BEFORE ENV is snapshotted, so every child — the SDK's CLI subprocess
# and the plain `claude -p` calls — resolves the same credentials.
# The live OAuth session lives on the HDD at ~/claude/config. A stale ~/.claude/.credentials.json
# also exists, and when CLAUDE_CONFIG_DIR is missing the CLI silently falls back to it and dies
# with "OAuth session expired and could not be refreshed". start_helix.sh exports this, but any
# other launch path (a hand-run python, cron, an old shell) would not — so don't rely on it.
# An explicit value already in the environment wins; this is only the floor.
_CFG_HDD = f"{HOME}/claude/config"
if not os.environ.get("CLAUDE_CONFIG_DIR") and os.path.isdir(_CFG_HDD):
    os.environ["CLAUDE_CONFIG_DIR"] = _CFG_HDD

ENV = dict(os.environ)
ENV["PATH"] = f"{HOME}/.local/bin:{HOME}/.opencode/bin:" + ENV.get("PATH","")
ENV.pop("ANTHROPIC_API_KEY", None)  # junk key breaks claude -p; CLI uses the OAuth subscription
os.environ.pop("ANTHROPIC_API_KEY", None)      # so the Agent SDK subprocess also uses the subscription
os.environ["PATH"] = ENV["PATH"]               # so the SDK can find the `claude` binary
CLAUDE_CLI = f"{HOME}/.local/bin/claude"

def bashrc_key(name):
    try:
        m = re.search(rf'^export {name}=(.+)$', open(f"{HOME}/.bashrc").read(), re.M)
        return m.group(1).strip().strip('"').strip("'") if m else os.environ.get(name,"")
    except: return os.environ.get(name,"")
OPENROUTER_KEY = bashrc_key("OPENROUTER_API_KEY")

# --- specialist bench (edit IDs freely; failures are shown, never fatal) ---
# Refreshed 2026-08-21. The previous IDs still RESOLVED on OpenRouter but were a whole
# generation behind (r1 -> v4, qwen2.5 -> 3.8, k2 -> k3, minimax-01 -> m3, glm-4.6 -> 5.3).
# Sol is OpenAI's GPT-5.6 flagship (2026-07-09) and leads ARC-AGI-2; it takes the frontier seat.
BENCH = [
    {"id":"openai/gpt-5.6-sol",                "name":"Sol",         "role":"frontier reasoning"},
    {"id":"deepseek/deepseek-v4-pro",          "name":"DeepSeek-V4", "role":"deep logic"},
    {"id":"qwen/qwen3.8-max",                  "name":"Qwen3.8-Max", "role":"code"},
    {"id":"moonshotai/kimi-k3",                "name":"Kimi-K3",     "role":"long-context/agentic"},
    {"id":"minimax/minimax-m3",                "name":"MiniMax-M3",  "role":"efficient agentic"},
    {"id":"z-ai/glm-5.3",                      "name":"GLM-5.3",     "role":"general/agentic"},
]

app = Flask(__name__, static_folder=f"{BRAIN}/static")
HISTORY = []

# ---- memory sizing: newest facts inline, older facts indexed — stays bounded as the body of work grows ----
FACTS_BUDGET = 60000       # chars of full-text facts to inline (tiered, see _select_inline); rest become an index
DRAWER_CAP   = 20000
TURN_CAP     = 1400        # truncate any single conversation turn
CONVO_CAP    = 14000

def _first_line(txt):
    for ln in txt.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln: return ln[:120]
    return "(untitled)"

# A fact whose headline was later qualified is the most dangerous kind: the index shows only
# its first line, so a half-retracted claim keeps reading as settled truth. 33 of 163 facts
# carry a correction in their body (2026-09-06). Flagging them in the index is what tells Odin
# which files he must open rather than cite from memory — the mechanical half of the grounding
# rule in ODIN_PERSONA.
_CORRECTION_FLAG = "  ⚠ LATER CORRECTION INSIDE — open it, do not cite this line"

# Deliberately narrow. "update" alone is ordinary prose ("I'll update the dashboard"); only the
# marked, retrospective forms count, so the flag keeps discriminating instead of lighting up
# every file.
_CORRECTION_MARKERS = (
    "⚠️ update", "⚠ update", "update (20",          # the dated "⚠️ UPDATE (2026-07-15…)" form
    "correction to", "correction (", "## correction",
    "superseded",          # PASSIVE only: this fact was overridden. "supersedes" (active) means
                           # it overrides an OLDER one, which is the opposite of a warning — that
                           # conflation flagged 00_example_72f9.md and 14_finance_audit, both perfectly fresh.
    "walkback", "walked back",
    "is refuted", "was wrong", "no longer true", "turned out to be wrong",
)


def _fact_has_correction(text):
    """Does this fact record a later correction to its own headline claim?"""
    low = (text or "").lower()
    return any(m in low for m in _CORRECTION_MARKERS)


def facts_audit():
    """Hygiene report over memory/facts — what needs a human eye.

    Not a linter that blocks anything: facts are the user's record, written by hand. This just
    surfaces what would otherwise stay invisible — which facts carry corrections, and which
    numeric prefixes collide (two files claiming to be fact 55 makes "see fact 55" ambiguous).
    """
    import collections
    names = sorted(f for f in os.listdir(FACTS) if f.endswith(".md"))
    corrected, prefixes = [], collections.Counter()
    for fn in names:
        m = re.match(r"^(\d+_)", fn)
        if m: prefixes[m.group(1)] += 1
        try:
            if _fact_has_correction(open(f"{FACTS}/{fn}").read()): corrected.append(fn)
        except Exception:
            pass
    return {
        "total": len(names),
        "corrected": corrected,
        "duplicate_prefixes": {k: v for k, v in prefixes.items() if v > 1},
    }


# ORDER 22b — INTERIM by default. A fact that is not legacy and has no witness releasing it is
# Odin's account, and the loader says so. The rule is factgate's own (imported, not copied), so
# the label and the nightly can never disagree. A label, never a block.
FACTGATE_PY = f"{WORKSPACE}/factgate/factgate.py"
_INTERIM_HEADER = "  🟡 INTERIM — Odin's account, no witness yet"
_INTERIM_FLAG = "  🟡 INTERIM"
_INTERIM_VERDICT_FLAG = "  🟡 INTERIM — verdict words unwitnessed"


def _interim_names(facts_dir=None, state_dir=None):
    """{fact name: [verdict words]} for every INTERIM fact, or None if factgate cannot say —
    then the loader works exactly as before, and logs one line."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_helix_factgate", FACTGATE_PY)
        fg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fg)
        items, _notes = fg.interim_facts(facts_dir or FACTS, state_dir or fg.DEFAULT_STATE)
        return {r["file"]: r["verdict"] for r in items}
    except Exception as e:
        print(f"HELIX: [interim] no INTERIM labels this time — {type(e).__name__}: {e}", flush=True)
        return None


# ORDER 33b — which facts are inlined. Recency alone is loud, not load-bearing (124_*: the right fact
# was in context 18% of the time), so the budget fills in three tiers, newest-first inside each:
#   1. the names in PINNED.txt (behavioural standing rules), capped at PIN_SHARE of the budget; a pin
#      over the cap goes to the index flagged 📌 PINNED, it does not compete in tier 3;
#   2. facts named (filename or `NN_` prefix) by a registry entry whose status is exactly "open",
#      capped at OPEN_SHARE; one over the cap falls through to tier 3;
#   3. everything else by mtime, as before.
# A pin whose heading says SUPERSEDED (and that carries a correction) is not a pin: it goes to tier 3.
# PINNED.txt is the user's / a build order's: walled (WALL_PATHS) and guarded by factgate_hook's P7.
PINNED_TXT = f"{BRAIN}/memory/PINNED.txt"
REGISTRY_JSON = f"{WORKSPACE}/seam/registry/registry.json"
PIN_SHARE = 0.5
OPEN_SHARE = 0.2
_PINNED_FLAG = "  📌 PINNED — over the pin cap, open it"
_NN_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])(\d+_)")


def _pinned_names(path=None):
    """The names listed in PINNED.txt; empty (and one log line) if it is missing or unreadable."""
    path = path or PINNED_TXT
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")}
    except Exception as e:
        print(f"HELIX: [facts] no pinned tier this time — {path}: {type(e).__name__}", flush=True)
        return set()


def _open_registry_names(names, path=None):
    """The facts in `names` that an `open` registry entry names, by filename or `NN_` prefix.
    Empty (and one log line) if the registry is missing or unreadable."""
    path = path or REGISTRY_JSON
    try:
        with open(path, encoding="utf-8") as fh:
            reg = json.load(fh)
        texts = [json.dumps(e, ensure_ascii=False) for e in reg.values()
                 if isinstance(e, dict) and e.get("status") == "open"]
    except Exception as e:
        print(f"HELIX: [facts] no registry tier this time — {path}: {type(e).__name__}", flush=True)
        return set()
    out = set()
    for text in texts:
        prefixes = set(_NN_PREFIX_RE.findall(text))
        out |= {fn for fn in names if fn in text or any(fn.startswith(p) for p in prefixes)}
    return out


def _superseded(text):
    """A fact that says, in its heading, that it was superseded — and carries the correction."""
    head = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    return _fact_has_correction(text) and "SUPERSEDED" in head.upper()


def _select_inline(budget=FACTS_BUDGET):
    """THE selection, used by _facts_digest and _mem_state alike. Returns (inline, index, tiers):
    inline [(name, text)] and index [(name, text, pinned_over_cap)] in name order, and the chars
    each tier used: {"pinned", "open", "recent"}. Never raises on a missing source."""
    facts = []
    try:
        for fn in sorted(os.listdir(FACTS)):
            if not fn.endswith(".md"): continue
            p = f"{FACTS}/{fn}"
            try: facts.append((fn, open(p).read().strip(), os.path.getmtime(p)))
            except Exception: pass
    except Exception: pass
    try:                                                  # ORDER 70: a fact's text as SHOWN — retired
        mr = _memretrieve()                               # passages marked — is what is inlined and
        ret = mr.retired_in(FACTS)                        # what the budget counts
        facts = [(fn, mr.apply_retirements(fn, t, ret), mt) for fn, t, mt in facts]
    except Exception: pass
    try: whole = _memretrieve().superseded_by(FACTS)     # ORDER 43: a replaced fact is never inline
    except Exception: whole = {}
    newest = sorted(facts, key=lambda x: -x[2])
    have = {fn for fn, _t, _mt in facts} - set(whole)    # it goes to the index, where its pointer prints
    listed = _pinned_names()                              # a listed name with no fact is skipped
    pins = {fn for fn, t, _mt in facts if fn in listed and fn in have and not _superseded(t)}
    opened = _open_registry_names(sorted(have - pins))
    tiers = {"pinned": 0, "open": 0, "recent": 0}
    inline, taken = [], set()

    def fill(tier, members, cap):
        for fn, t, _mt in newest:
            if fn in members and fn not in taken and tiers[tier] + len(t) <= cap:
                inline.append((fn, t)); taken.add(fn); tiers[tier] += len(t)

    fill("pinned", pins, budget * PIN_SHARE)
    over = pins - taken                                   # over the pin cap: index, flagged
    fill("open", opened, budget * OPEN_SHARE)
    room = budget - tiers["pinned"] - tiers["open"]
    fill("recent", have - over, room)
    index = [(fn, t, fn in over) for fn, t, _mt in facts if fn not in taken]
    inline.sort(key=lambda x: x[0])                       # readable name order (facts already is)
    return inline, index, tiers


# ORDER 81 (item 33) — the per-fact index is the one part of the per-turn memory with no cap. In
# `category` mode it is one line per category (count, newest date, description, digest path), and
# workspace/memdigest/memdigest.py writes each category's per-fact list from the facts at every load:
# mechanical, no model text, so it cannot drift. Per-fact lines stay for facts changed in the last
# RECENT_INDEX_DAYS, pins over the cap and facts with no category. `fact` mode is the index as before.
# The default is RESULT_ORDER81.txt's ship verdict (workspace/memeval/score_81.py).
INDEX_MODE = os.environ.get("HELIX_INDEX_MODE", "category")
RECENT_INDEX_DAYS = 7
MEMDIGEST_PY = f"{WORKSPACE}/memdigest/memdigest.py"
MEMDIGEST_OUT = f"{WORKSPACE}/memdigest/out"
MEMDIGEST_CLOSED = f"{WORKSPACE}/memdigest/CLOSED.txt"
MEMDIGEST_CATEGORIES = f"{WORKSPACE}/categories/CATEGORIES.txt"
MEMDIGEST_ASSIGNED = f"{WORKSPACE}/categories/ASSIGNED.csv"


def _index_flags(fn, t, pinned, whole, interim):
    """What follows a fact's first line on its index line (and on its digest line)."""
    return ((_PINNED_FLAG if pinned else "")
            + (f"  → SUPERSEDED by {whole[fn]}, read that" if fn in whole
               else _CORRECTION_FLAG if _fact_has_correction(t) else "")
            + ((_INTERIM_VERDICT_FLAG if interim[fn] else _INTERIM_FLAG)
               if fn in interim else ""))


def _category_index(inline, index, whole, interim):
    """ORDER 81: the category block for the facts in `index`, or None (and one log line) when
    memdigest fails or misses one of them — then this call falls back to the fact index."""
    shown = dict(inline)
    shown.update((fn, t) for fn, t, _p in index)
    over = {fn for fn, _t, p in index if p}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_helix_memdigest", MEMDIGEST_PY)
        md = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(md)
        md.CATEGORIES_TXT, md.ASSIGNED_CSV = MEMDIGEST_CATEGORIES, MEMDIGEST_ASSIGNED
        md.CLOSED_TXT = MEMDIGEST_CLOSED
        res = md.build(FACTS, MEMDIGEST_OUT, lambda fn, text: _index_flags(
            fn, shown.get(fn, text or ""), fn in over, whole, interim))
        cat_of = {fn: c for c, v in res.items() for fn in v["files"]}
        lost = [fn for fn, _t, _p in index if fn not in cat_of]
        if lost:
            raise LookupError(f"{len(lost)} indexed fact(s) in no digest, first {lost[0]}")
    except Exception as e:
        print(f"HELIX: [facts] no category index this time, fact index instead — {type(e).__name__}: {e}",
              flush=True)
        return None
    indexed = {fn for fn, _t, _p in index}
    cats = []
    for c, v in sorted(res.items()):
        if not indexed & set(v["files"]):                 # every fact of it is inline
            continue
        cats.append(f"- {'CLOSED — ' + v['closed'] + ' · ' if v['closed'] else ''}{c} — {v['count']} facts, "
                    f"newest {v['newest_date']} — {v['description']} → {v['path']}")
    now, recent = time.time(), []
    for fn, t, pinned in index:
        try: young = now - os.path.getmtime(f"{FACTS}/{fn}") <= RECENT_INDEX_DAYS * 86400
        except Exception: young = True
        if young or pinned or cat_of[fn] == md.UNCATEGORISED:
            recent.append(f"- memory/facts/{fn} — {_first_line(t)}" + _index_flags(fn, t, pinned, whole, interim))
    block = ("\n\n=== FACT INDEX BY CATEGORY (each line = one category; open its digest for the per-fact list, "
             'or run python3 ~/helix/workspace/memretrieve/memretrieve.py "<words>") ===\n' + "\n".join(cats))
    if recent:
        block += "\n\n=== RECENT AND FLAGGED FACTS (own line) ===\n" + "\n".join(recent)
    return block


def _facts_digest(budget=FACTS_BUDGET):
    # tiered facts (see _select_inline) inlined in full up to `budget`; the rest as a readable index to open on demand
    interim = _interim_names() or {}
    try: whole = _memretrieve().superseded_by(FACTS)     # ORDER 42c: {fact: its newest replacement}
    except Exception: whole = {}
    inline, index, _tiers = _select_inline(budget)
    block = "\n\n".join(f"# {fn}" + (_INTERIM_HEADER if fn in interim else "") + f"\n{t}"
                         for fn, t in inline)
    if index and INDEX_MODE == "category":
        cat = _category_index(inline, index, whole, interim)
        if cat is not None:
            return block + cat
    if index:
        block += ("\n\n=== OLDER FACTS (index — open any of these in full with your file tools; they live in "
                  + FACTS + ") ===\n" + "\n".join(
                      f"- memory/facts/{fn} — {_first_line(t)}" + _index_flags(fn, t, pinned, whole, interim)
                      for fn, t, pinned in index))
    return block

def load_memory(budget=FACTS_BUDGET):
    # BRAIN.md = operating charter + index (always full). memory/facts/*.md = durable per-fact record:
    # newest inline + older indexed, so priming can't blow past size limits as the work grows.
    charter = ""
    try: charter = open(MEMORY).read()
    except Exception: pass
    return (charter + "\n\n" + _facts_digest(budget)).strip()

# ---- transcript retention -------------------------------------------------------------------
# log.md is SCROLLBACK. The durable record is memory/facts/ — the things you chose to keep, via
# /remember — and those are never touched by any of this.
LOG_KEEP_TURNS = int(os.environ.get("HELIX_LOG_KEEP_TURNS", 400))     # turns kept after a roll
LOG_MAX_BYTES  = int(os.environ.get("HELIX_LOG_MAX_BYTES", 2_000_000))# roll once it passes this
LOG_TAIL_BYTES = int(os.environ.get("HELIX_LOG_TAIL_BYTES", 400_000)) # how much tail to parse

def prune_log(keep=None, force=False):
    """Roll log.md down to the last `keep` turns. Returns how many were dropped, or None.

    Cheap on the common path: it only reads the file once the file is actually big."""
    keep = LOG_KEEP_TURNS if keep is None else keep
    p = f"{SESS}/log.md"
    try:
        if not force and os.path.getsize(p) < LOG_MAX_BYTES: return None
    except Exception: return None
    # Work in BYTES, not characters. The guard above compares os.path.getsize — bytes — while
    # this log is ~1.1% multibyte (em dashes, box rules, check marks). A cut measured in
    # characters therefore leaves a file ~22 KB OVER the byte ceiling and stays there: the guard
    # keeps firing, the whole 2 MB is re-read every turn, and no roll can ever bring it under.
    try: raw = open(p, "rb").read()
    except Exception: return None
    marks = [m.start() for m in re.finditer(rb'^## \d{4}-\d\d-\d\d ', raw, re.M)]
    if len(marks) <= keep and len(raw) <= LOG_MAX_BYTES: return None
    cut = marks[max(0, len(marks) - keep)]
    # Turns alone do not bound the file — these turns carry code, and 400 of them ran past the
    # ceiling, so it would re-read a too-large file every turn forever. Whichever limit bites
    # first wins, and the cut always lands on a turn boundary.
    while cut < len(raw) and len(raw) - cut > LOG_MAX_BYTES:
        nxt = [m for m in marks if m > cut]
        if not nxt: break
        cut = nxt[0]
    if cut <= 0: return None
    # One-time safety net: the first roll archives what it drops rather than destroying a
    # transcript that predates this feature existing. Every roll after that just drops.
    arch = f"{SESS}/log_archive_first_roll.md"
    if not os.path.exists(arch):
        try:
            with open(arch, "wb") as f: f.write(raw[:cut])
        except Exception: pass
    try:
        with open(p, "wb") as f: f.write(raw[cut:])
    except Exception: return None
    return len(marks) - keep

def parse_log(n=24):
    # Read past sessions back from disk so memory survives restarts (HISTORY is RAM-only).
    # Only the TAIL is read: the caller never wants more than the last couple of dozen turns,
    # and reading four megabytes to find them made every turn slower as the file grew.
    try:
        with open(f"{SESS}/log.md", "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="ignore")
    except: return []
    turns = []
    parts = re.split(r'\n\*\*(You|ODIN|ALICE):\*\* ', raw)
    i = 1
    while i + 1 < len(parts):
        role = "you" if parts[i] == "You" else ("alice" if parts[i] == "ALICE" else "odin")
        text = re.split(r'\n## \d', parts[i+1])[0].rstrip()
        if text: turns.append({"role": role, "text": text})
        i += 2
    return turns[-n:]

def recent_sessions(chars=3500):
    # Compact tail of prior sessions to fold into the prompt (so he recalls across restarts).
    turns = parse_log(24)
    blob = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in turns)
    return blob[-chars:]

def context(question, for_council=False):
    charter = open(CHARTER).read() if os.path.exists(CHARTER) else ""
    convo = "\n".join(f"{r.upper()}: {t}" for r,t in HISTORY[-8:]) or recent_sessions()
    task = ("Read the memory and recent conversation, then give YOUR independent opinion on the latest "
            "message. Be concise, critical, flag disagreement." if for_council else
            "You are ODIN — a full Claude Code agent with real tools (bash, read/write files, web) "
            "in your workspace. SPEED MATTERS: for ordinary conversation or anything you can answer "
            "from memory, reply IMMEDIATELY and briefly, WITHOUT using tools. Only reach for tools when "
            "the task genuinely needs action (write & run code, edit files, check something on disk) — "
            "then actually DO it, don't just describe it. Reply with a short spoken-friendly summary "
            "leading with the outcome; put code in ``` blocks (shown in chat, not read aloud). Do "
            "routine reversible work without asking; only pause for destructive or outside-world actions.")
    return (f"{charter}\n\n=== ODIN LONG-TERM MEMORY ===\n{load_memory()}"
            f"\n\n=== RECENT CONVERSATION ===\n{convo}\n\n=== LATEST MESSAGE ===\n{question}\n\n{task}")

def sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return (p.stdout or "").strip() or f"[no output: {(p.stderr or '')[-100:]}]"
    except subprocess.TimeoutExpired: return "[timed out]"
    except Exception as e: return f"[error: {e}]"

# ---- Which Claude model IS Odin. One knob, both call sites. ----
# Set 2026-09-01 (the user): "I see no use in using outdated models and the work I do I have
# need of that one." Fable 5 = Anthropic's most capable widely released model.
#
# Changed 2026-09-09 (the user), after Fable went dark for a full day. Fable is the ONE model on
# this account billed against usage-overage credits rather than the Max plan's included
# allowance (config: tengu_usage_overage_included_models = Fable/Fable 5/Fable 5.1), so it can
# run out on its own while opus and sonnet answer normally. Pinned to it with no fallback,
# Odin went completely silent behind a message that blamed the account. Opus 5 is covered by
# the plan, half the price ($5/$25 vs $10/$50 per MTok), and supports the same xhigh effort
# that gates workflow/ultracode work — so it is the sane default, not a downgrade.
#   Run a Fable session on purpose:  HELIX_MODEL=fable ./start_helix.sh
# NOTE: thinking is always ON for fable and cannot be disabled; turns run longer than opus.
# Which models the sidebar selector offers. Deliberately two: the pair the user actually uses.
MODEL_CHOICES = {
    "opus":  {"label": "Opus 5",  "note": "covered by plan"},
    "fable": {"label": "Fable 5", "note": "50% weekly cap"},
}
UI_STATE = f"{BRAIN}/memory/ui_state.json"      # saved model choice + last-known fable state

def _ui_load():
    """A corrupt or missing file reads as empty — UI state must never stop Helix booting."""
    try:
        d = json.load(open(UI_STATE))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _ui_save(d):
    try:
        os.makedirs(os.path.dirname(UI_STATE) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(UI_STATE) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, UI_STATE)               # atomic: a crash mid-write can't truncate it
    except Exception:
        pass

def _ui_get(key, default=None):
    """One value out of the saved UI state. Same file the model selector uses."""
    return _ui_load().get(key, default)


def _ui_set(key, value):
    d = _ui_load(); d[key] = value; _ui_save(d)


# Precedence: an explicit HELIX_MODEL at launch is deliberate intent and outranks the saved UI
# choice; otherwise the sidebar selection persists across restarts. Without persistence a reboot
# silently reverts to opus, which is exactly the confusion this selector exists to end.
_ENV_MODEL     = os.environ.get("HELIX_MODEL")
_UI            = _ui_load()
ODIN_MODEL     = _ENV_MODEL or _UI.get("odin_model") or "opus"
if ODIN_MODEL not in MODEL_CHOICES:
    ODIN_MODEL = "opus"
ODIN_MODEL_ALT = os.environ.get("HELIX_MODEL_ALT", "opus")   # used when ODIN_MODEL hits a limit

# Last known state of the capped model, learned from turn outcomes — never from probe calls.
FABLE_CAPPED_AT = _UI.get("fable_capped_at")     # ISO date-time, or None when it last worked

def _mark_model_state(model, limited):
    """Record what a finished turn just proved about this model's availability."""
    global FABLE_CAPPED_AT
    if model != "fable":
        return
    was = FABLE_CAPPED_AT
    FABLE_CAPPED_AT = time.strftime("%Y-%m-%dT%H:%M:%S") if limited else None
    if FABLE_CAPPED_AT != was:
        d = _ui_load(); d["fable_capped_at"] = FABLE_CAPPED_AT; _ui_save(d)

# ULTRACODE: one-shot permission to fan out. Armed by hand in the turn bar, spent by the next
# turn, then gone. Never sticky — a forgotten armed toggle is the unattended-spend shape that
# cost 267M tokens on 2026-09-07/08. See FANOUT_TOOLS.
_ULTRA_ARMED = False

def _fanout_tools_now():
    return [] if _ULTRA_ARMED else FANOUT_TOOLS

# ---- What Odin may NOT do on his own. ----
# 2026-09-07/08: Odin picked up the workflow-authoring skill mid-session and answered four
# short messages from the user with 7 workflow runs — 63 fable-5 subagents, 267M tokens, hundreds of dollars of
# API-equivalent spend, none of it asked for. Nothing gated it: he runs with
# bypassPermissions and his charter said nothing about workflows. `disallowed_tools` removes
# the tool from his context entirely, so this is a wall rather than an instruction he can
# reason his way past, and max_budget_usd is the backstop for everything else.
# Deliberate fan-out is still available — the user sets the env var for that session.
ALLOW_FANOUT    = os.environ.get("HELIX_ALLOW_FANOUT", "").lower() in ("1", "true", "yes")
FANOUT_TOOLS    = [] if ALLOW_FANOUT else ["Workflow"]
# Per-turn ceiling, in NOTIONAL API-equivalent dollars — Odin runs on the Max
# subscription, so this is a burn meter, never a charge. It exists only to stop a runaway
# fan-out (each workflow run in the 2026-09-07/08 incident metered ~$70), and the real wall
# against that is FANOUT_TOOLS, which needs a deliberate arming press.
# OFF by default from 2026-09-09 (the user: an ordinary Opus turn cannot exhaust the plan, and a
# job that meters past ten dollars is not a reason to abandon work mid-edit). Set
# HELIX_TURN_BUDGET_USD to a number to re-arm it; 0 or unset means no ceiling.
_budget_env     = os.environ.get("HELIX_TURN_BUDGET_USD", "0")
TURN_BUDGET_USD = float(_budget_env) if _budget_env not in ("", "0") else None

# A model can be individually rate-limited while the account is fine (see ODIN_MODEL above).
# The CLI renders every 429 as "out of usage credits", which reads like the plan is gone.
_LIMIT_HINTS = ("out of usage credits", "usage limit", "rate limit", "429")

def _is_limit(text):
    t = (text or "").lower()
    return any(h in t for h in _LIMIT_HINTS)

def _claude_once(p, model):
    # ODIN's brain = a full Claude Code agent: tools ON (bash/files/web), non-interactive,
    # on the subscription (ENV drops ANTHROPIC_API_KEY), working in his own workspace.
    cmd = ["claude","-p",p,"--model",model,"--permission-mode","bypassPermissions"]
    if FANOUT_TOOLS:
        cmd += ["--disallowedTools", *FANOUT_TOOLS]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=ENV, cwd=WORKSPACE)
    return (r.stdout or "").strip(), (r.stderr or "").strip()

def claude(p):
    try:
        out, err = _claude_once(p, ODIN_MODEL)
        # One retry on the alternate model when THIS model is limited but the account is not.
        # Silence is the failure that actually hurt: a dead assistant reading as a dead plan.
        if _is_limit(out) and ODIN_MODEL_ALT != ODIN_MODEL:
            alt, alt_err = _claude_once(p, ODIN_MODEL_ALT)
            if alt and not _is_limit(alt):
                print(f"HELIX: {ODIN_MODEL} is rate-limited — this turn ran on {ODIN_MODEL_ALT}", flush=True)
                return alt
            err = alt_err or err
        return out or f"[no output: {err[-200:]}]"
    except subprocess.TimeoutExpired: return "[the task ran past 15 min and was stopped]"
    except Exception as e: return f"[error: {e}]"

def gemini(p):  return sh(["agy","-p",p,"--model","Gemini 3.1 Pro (Low)"])

# ---- ODIN's fast local "front desk" (Ollama). Optional: if absent, everything escalates to Claude. ----
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b"
GREET_SYS = ("You are ODIN's fast front desk — warm and brief. Answer GREETINGS, small talk, thanks, and "
    "trivial one-line questions in ONE short sentence, first person as ODIN. For ANYTHING that needs real "
    "thought, tools, code, files, the web, math, research, memory, or multiple steps, reply with EXACTLY "
    "one word: ESCALATE. Never attempt real work. Max 25 words.")
def greet(text):
    # a quick local reply, or None to hand the turn to the Claude agent
    try:
        r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": text, "system": GREET_SYS,
            "stream": False, "options": {"temperature": 0.4, "num_predict": 48}}, timeout=6)
        if r.status_code != 200: return None
        out = (r.json().get("response") or "").strip().strip('"')
        if not out or "ESCALATE" in out.upper() or len(out) > 240: return None
        return out
    except Exception:
        return None


def _front_prompt(text):
    """What the front lane actually sees: one line of world-state, then the user's words.
    Helix has no tools, so this line is the only way it can know Odin is busy."""
    pad = frontdesk.load_pad(NOTEPAD)
    return (frontdesk.status_line(WORK.lock.locked(), pad["notes"], pad["last_job"] is not None)
            + "\n\n" + text)


def front_raw(text):
    """Run ONE turn on the FRONT lane and return its raw reply (markers still in).

    Returns None when the front desk is off or unreachable — the caller then falls through
    to Odin, so a broken front can never make Helix mute. That fallback is the whole
    resilience story: the front is an optimisation, not a dependency.
    """
    if not FRONT_ON:
        return None
    try:
        buf = TurnBuf(uuid.uuid4().hex[:12], text, "front")
        # _run_escalated assumes its caller already holds the lane's turn lock.
        with FRONT.lock:
            _run_escalated(buf, _front_prompt(text), lane=FRONT, quiet=True)
        snap = buf.snapshot()
        out = (snap.get("final") or snap.get("text") or "").strip()
        return out or None
    except Exception as e:
        print(f"HELIX: front lane failed ({e}) — falling through to Odin", flush=True)
        return None


def alice_context(question):
    # Same recency-budget guard as Odin: newest facts inline (via _facts_digest), older ones indexed,
    # so `agy -p <prompt>` can never cross Linux's ~128KB single-arg limit as memory grows.
    persona = open(ALICE_MD).read() if os.path.exists(ALICE_MD) else ""
    facts_block = "=== SHARED BODY OF WORK — recent facts (full text) ===\n" + _facts_digest()
    drawer = []
    try:
        for fn in sorted(os.listdir(ALICE_DRAWER)):
            if fn.endswith(".md"): drawer.append(open(f"{ALICE_DRAWER}/{fn}").read().strip())
    except Exception: pass
    drawer_block = "\n\n".join(drawer)[:DRAWER_CAP]
    turns = []
    for r, t in HISTORY[-8:]:
        turns.append(f"{r.upper()}: {t if len(t) <= TURN_CAP else t[:TURN_CAP] + ' …[truncated]'}")
    convo = ("\n".join(turns) or recent_sessions())[-CONVO_CAP:]
    return (persona + "\n\n" + facts_block +
            "\n\n=== YOUR DRAWER (memory/alice) ===\n" + drawer_block +
            "\n\n=== RECENT CONVERSATION ===\n" + convo +
            "\n\n=== TASK ===\n" + question[:8000] +
            "\n\nAnswer as ALICE — rigorous, concise, spoken-friendly. Lead with the verdict.")

def alice_brain(question):     # Gemini high-reasoning + TOOLS via agy (she can browse the work to audit it)
    cmd = ["agy", "-p", alice_context(question), "--model", ALICE_MODEL, "--dangerously-skip-permissions", "--output-format", "json"]
    for d in (BRAIN, f"{HOME}/Desktop", "__BULK__", "__VAULT_DRIVE__"):
        if os.path.isdir(d): cmd += ["--add-dir", d]      # give her the memory, storage, Desktop, and the USB
    raw = sh(cmd, timeout=900)
    try:
        j = json.loads(raw)
        return j.get("response", raw), j.get("usage", {})
    except Exception:
        return raw, {}

def openrouter(model, p):
    if not OPENROUTER_KEY: return "[no OpenRouter key]"
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization":f"Bearer {OPENROUTER_KEY}"},
            json={"model":model,"messages":[{"role":"user","content":p}],"max_tokens":700}, timeout=120)
        if r.status_code!=200: return f"[{model}: HTTP {r.status_code} {r.text[:80]}]"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e: return f"[{model}: {e}]"

def clean_for_speech(t):
    # Turn markdown into plain words so TTS never reads symbols (* - # | ` >) aloud, and drop code.
    t = re.sub(r'```.*?```', '. code shown in the chat. ', t, flags=re.S)  # code blocks → cue
    t = re.sub(r'`([^`]+)`', r'\1', t)                    # inline code → the word
    t = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', t)      # links/images → their text
    # MARKDOWN TABLES → a cue, never read aloud. Measured 2026-08-10: one 6-row table in a reply
    # became a single 460-char chunk and a 33-SECOND clip of unlistenable number soup
    # ("time what happens look 0:00 to 0:19 headphones on..."). A table is for the eye; say so and move on.
    t = re.sub(r'(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}', '. table shown in the chat. ', t, flags=re.M)
    t = re.sub(r'^\s{0,3}#{1,6}\s*', '', t, flags=re.M)   # headings
    t = re.sub(r'^\s*>\s?', '', t, flags=re.M)            # blockquotes
    t = re.sub(r'^\s*[-*+•]\s+', '', t, flags=re.M)       # bullet markers at line start
    t = re.sub(r'\*\*([^*]+)\*\*', r'\1', t)              # **bold**
    t = re.sub(r'\*([^*]+)\*', r'\1', t)                  # *italic*
    t = re.sub(r'__([^_]+)__', r'\1', t)                  # __bold__
    t = re.sub(r'\s[-–—]+\s', ', ', t)                    # " - " separators → pause (keep word-hyphens)
    t = re.sub(r'[*#`_>|~]', '', t)                       # any leftover md symbols
    t = re.sub(r'\n{2,}', '. ', t)                        # paragraph breaks → pause
    t = re.sub(r'\n', ' ', t)                             # single newlines → space
    t = re.sub(r'[ \t]{2,}', ' ', t)                      # collapse whitespace
    return t.strip()

def prune_tts(keep=120):
    # A wav is played once then never needed. Keep only the newest few so tts/ can't grow unbounded.
    # NOTE: call this ONCE per turn (start of _emit_audio), never per-sentence — otherwise a long reply's
    # early clips get deleted before the browser plays them (404 → silent gaps mid-reply).
    try:
        ws = sorted((f"{TTSDIR}/{f}" for f in os.listdir(TTSDIR) if f.endswith(".wav")),
                    key=os.path.getmtime, reverse=True)
        for old in ws[keep:]: os.remove(old)
    except: pass

def pitch_shift(x, factor):
    # factor < 1 lowers pitch (and lengthens slightly = a touch slower); >1 raises + shortens
    import numpy as np
    x = np.asarray(x, dtype=np.float32); n = len(x)
    return np.interp(np.arange(0, n, factor), np.arange(n), x).astype(np.float32)

def roboize(x, sr, depth=0.26, fc=75.0, metal=0.16):
    # subtle robotic sheen: light ring-modulation + a 6ms metallic doubling, then normalize
    import numpy as np
    x = np.asarray(x, dtype=np.float32); n = x.shape[0]
    t = np.arange(n, dtype=np.float32) / sr
    ring = x * (1.0 - depth + depth * np.cos(2*np.pi*fc*t).astype(np.float32))
    d = int(0.006*sr); m = np.zeros_like(ring)
    if d < n: m[d:] = ring[:-d]
    out = ring + metal * m
    peak = float(np.max(np.abs(out))) or 1.0
    return (out/peak * 0.97).astype(np.float32)

_kok = None; _voice = None; _avoice = None
def _odin_voice():
    # the signature blend if it exists, else the fallback voice name
    global _voice
    if _voice is None:
        if os.path.exists(KOKORO_BLEND):
            import numpy as np; _voice = np.load(KOKORO_BLEND)
        else:
            _voice = KOKORO_VOICE
    return _voice
def _alice_voice():
    global _avoice
    if _avoice is None:
        try: import numpy as np; _avoice = np.load(KOKORO_ALICE)
        except Exception: _avoice = _odin_voice()
    return _avoice
def _helix_voice():
    """Helix speaks in Odin's blend by default, or Alice's if the user picked it. Both blends are
    KEPT — losing a voice was never the point; there is simply one speaker now."""
    return _alice_voice() if _ui_get("helix_voice", "odin") == "alice" else _odin_voice()


def speak(text, tag, voice=None):
    # returns (filename, envelope, duration) — envelope = RMS per ~30ms frame (0..1) for the visualizer
    global _kok
    import soundfile as sf, numpy as np
    fn = f"say_{tag}.wav"
    try:
        if _kok is None:
            from kokoro_onnx import Kokoro
            _kok = Kokoro(KOKORO_ONNX, KOKORO_VOICES)   # loaded once, then cached (~1s first call)
        samples, sr = _kok.create(text[:3000], voice=(voice if voice is not None else _odin_voice()), speed=KOKORO_SPEED, lang="en-us")
        if KOKORO_PITCH != 1.0: samples = pitch_shift(samples, KOKORO_PITCH)
        if KOKORO_ROBO > 0: samples = roboize(samples, sr, KOKORO_ROBO)
        sf.write(f"{TTSDIR}/{fn}", samples, sr)
        x = np.asarray(samples, dtype=np.float32); fr = max(1, int(sr*0.03)); nf = len(x)//fr
        env = []
        if nf > 0:
            rms = np.sqrt((x[:nf*fr].reshape(nf, fr)**2).mean(axis=1))
            peak = float(rms.max()) or 1.0
            env = [round(float(v/peak), 3) for v in rms]
        return (fn, env, float(len(x)/sr)) if os.path.exists(f"{TTSDIR}/{fn}") else (None, [], 0.0)
    except Exception:
        return (None, [], 0.0)

# ---- voice INPUT: record from the mic + transcribe locally (offline, any browser) ----
_whisper = None
# Whisper hallucinates these on silence/noise — never send them through
_HALLUC = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching",
           "bye", "uh", "um", "yeah", "okay", ".", "...", "so", "the", "i"}
def _clean_stt(t):
    t = (t or "").strip()
    low = t.lower().strip(" .,!?-")
    if not low or len(low) < 2 or low in _HALLUC: return ""
    return t

def stt_transcribe(wav_path):
    # faster-whisper base.en int8, VAD-filtered so silence doesn't hallucinate "you"
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
    segs, _info = _whisper.transcribe(wav_path, beam_size=1, vad_filter=True)
    return _clean_stt(" ".join(s.text for s in segs))

def _transcribe_array(audio):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
    segs, _info = _whisper.transcribe(audio, beam_size=1, vad_filter=True)
    return _clean_stt(" ".join(s.text for s in segs))

# always-listening: capture ONE utterance via energy VAD, then transcribe (tunable thresholds)
VAD_START = 330   # int16 RMS to count as speech onset (lower = more sensitive)
VAD_STOP  = 200   # below this = silence
@app.route("/listen/utterance", methods=["POST"])
def listen_utterance():
    import numpy as np
    sr=16000; F=320                                   # 20ms frames
    stop_need=int(0.7*sr/F); max_wait=int(10*sr/F); max_utt=int(14*sr/F)
    proc=subprocess.Popen(["pw-record","--rate","16000","--channels","1","--format","s16","-"],
                          stdout=subprocess.PIPE, env=ENV)
    buf=[]; started=False; silence=0; waited=0
    try:
        while True:
            d=proc.stdout.read(F*2)
            if not d or len(d)<F*2: break
            x=np.frombuffer(d,dtype=np.int16).astype(np.float32)
            rms=float(np.sqrt((x*x).mean()))
            if not started:
                waited+=1
                if rms>VAD_START: started=True; buf.append(x)
                elif waited>=max_wait: break
            else:
                buf.append(x)
                silence = silence+1 if rms<VAD_STOP else 0
                if silence>=stop_need or len(buf)>=max_utt: break
    finally:
        try: proc.send_signal(signal.SIGINT); proc.wait(timeout=2)
        except:
            try: proc.kill()
            except: pass
    if not started or len(buf)<4: return jsonify(text="")
    try: return jsonify(text=_transcribe_array(np.concatenate(buf)/32768.0))
    except Exception as e: return jsonify(text="", error=str(e))

@app.route("/")
def index(): return send_from_directory(app.static_folder, "index.html")
@app.route("/tts/<f>")
def tts(f): return send_from_directory(TTSDIR, f)

@app.route("/ask", methods=["POST"])          # default: you + Claude (subscription)
def ask():
    q=(request.json or {}).get("text","").strip()
    if not q: return jsonify(error="empty"),400
    ans=claude(context(q))
    tag=hashlib.md5((q+str(time.time())).encode()).hexdigest()[:8]
    audio,_env,_dur=speak(clean_for_speech(ans),tag)   # (old one-shot path; streaming uses /ask_stream)
    HISTORY.append(("you",q)); HISTORY.append(("odin",ans))
    with open(f"{SESS}/log.md","a") as f:
        f.write(f"\n## {time.strftime('%F %H:%M')}\n**You:** {q}\n**ODIN:** {ans}\n")
    return jsonify(text=ans, audio=(f"/tts/{audio}" if audio else None))

_mic = None   # the live pw-record process (press-to-start / press-to-stop, no time limit)
@app.route("/listen/start", methods=["POST"])
def listen_start():
    global _mic
    try:
        if _mic and _mic.poll() is None: _mic.send_signal(signal.SIGINT)
    except: pass
    wavp = f"{TTSDIR}/_mic.wav"
    try:
        # pw-record follows the PipeWire DEFAULT source = the user's real mic (the headset)
        _mic = subprocess.Popen(["pw-record","--rate","16000","--channels","1","--format","s16",wavp],
                                env=ENV)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/listen/stop", methods=["POST"])
def listen_stop():
    global _mic
    wavp = f"{TTSDIR}/_mic.wav"
    if _mic:
        try: _mic.send_signal(signal.SIGINT); _mic.wait(timeout=3)   # SIGINT lets pw-record finalize the WAV
        except:
            try: _mic.kill()
            except: pass
        _mic = None
    time.sleep(0.15)
    if not os.path.exists(wavp): return jsonify(text="", error="no audio captured")
    try: return jsonify(text=stt_transcribe(wavp))
    except Exception as e: return jsonify(text="", error=f"stt: {e}")

@app.route("/council", methods=["POST"])      # on demand: the bench reads latest + opines
def council():
    q=(request.json or {}).get("text","") or (HISTORY[-2][1] if len(HISTORY)>=2 else "")
    p=context(q, for_council=True)
    seats={}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut={ex.submit(gemini,p):("Gemini","breadth")}
        for m in BENCH: fut[ex.submit(openrouter,m["id"],p)]=(m["name"],m["role"])
        for f in as_completed(fut):
            n,role=fut[f]; seats[n]={"role":role,"text":f.result()}
    return jsonify(seats=seats)

# ORDER 76: factgate (F5) refuses a NEW fact with no "Category:" line, so /remember writes one. The
# guess is propose_backfill's, loaded from its path in a try; if it fails or guesses nothing, a UI
# note from the user is most often personal (ASSIGNED/OVERRIDES can move it later).
FACT_GUESS_PY = f"{BRAIN}/workspace/categories/propose_backfill.py"

def _remember_category(name, text):
    """The category a /remember note is written with. Never raises; leaves sys.path as it was."""
    saved = list(sys.path)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("helix_propose_backfill", FACT_GUESS_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.guess(name, text, mod.categories.load())[0] or "personal"
    except Exception:
        return "personal"
    finally:
        sys.path[:] = saved

@app.route("/remember", methods=["POST"])
def remember():
    # Write a discrete fact file instead of growing one blob, so memory is durable + editable per-fact.
    n=(request.json or {}).get("text","").strip()
    if not n: return jsonify(ok=False)
    slug=(re.sub(r'[^a-z0-9]+','-', n.lower()).strip('-')[:40] or "note")
    h=hashlib.md5(n.encode()).hexdigest()[:6]
    fn=f"{FACTS}/{time.strftime('%Y%m%d')}-{slug}-{h}.md"
    cat=_remember_category(os.path.basename(fn), n)
    with open(fn,"w") as f:
        f.write(f"---\nsaved: {time.strftime('%F')}\nstatus: unverified\n---\nCategory: {cat}\n{n}\n")
    # This route writes a fact without going through a tool, so the PostToolUse hook never
    # sees it. Record it here or the nightly reports it as off-ledger tomorrow.
    try:
        import factgate_hook
        ok, detail = factgate_hook.record_remember(fn)
        if not ok:
            print(f"HELIX: [factgate] /remember wrote {os.path.basename(fn)} but it was NOT "
                  f"recorded ({detail}) — tonight's nightly will flag it", flush=True)
    except Exception as e:
        print(f"HELIX: [factgate] /remember could not record {os.path.basename(fn)}: "
              f"{type(e).__name__}: {e}", flush=True)
    return jsonify(ok=True, file=os.path.basename(fn))

@app.route("/history")                        # UI repaints the prior conversation on load
def history():
    turns = parse_log(24)
    if not HISTORY:                           # only warm once, so refreshes don't duplicate the thread
        for t in turns: HISTORY.append((t["role"], t["text"]))
    return jsonify(turns=turns)

# ============ Phase 1: streaming agent brain (Claude Agent SDK → SSE) ============
# GROUNDING RULE added 2026-09-06. Measured first, then changed: over repeated eval runs Odin
# answered questions about recorded results WITHOUT opening the fact file about a third of the
# time — substantively right, but with none of the actual numbers, and once even citing
# `facts/32_*` without reading it. The cause was this very prompt: "answer immediately and
# briefly WITHOUT tools" reads as covering "what did D30-M5 find?", which is not conversation
# but a lookup. The carve-out below is deliberately narrow so ordinary chat stays fast.
ODIN_PERSONA = ("You are ODIN — a persistent AI agent with a voice, speaking to ONE human. You have real "
    "tools (bash, read/write files, web) in your workspace; when a task needs action, DO it, don't just "
    "describe it. For ordinary conversation, answer immediately and briefly WITHOUT tools. "
    "BUT: if he asks what a RECORDED RESULT was — a number, a verdict, what an experiment found, what a "
    "fact file says — OPEN THE FILE AND READ IT FIRST, every time. That is a lookup, not conversation, "
    "and the two seconds are always worth it. Your recollection of the gist is not the record; the file "
    "is. Answering from memory is how a stale number gets repeated as current truth. If you catch "
    "yourself about to cite a fact by name, you already know you should have opened it. "
    "Reply with a "
    "short, spoken-friendly summary that leads with the outcome; put any code in ``` blocks (shown in the "
    "chat, not read aloud). Do routine reversible work without asking; pause only for destructive or "
    "outside-world actions.")

def _preamble():
    charter = open(CHARTER).read() if os.path.exists(CHARTER) else ""
    return (f"{ODIN_PERSONA}\n\n=== ODIN LONG-TERM MEMORY ===\n{load_memory()}"
            f"\n\n=== RECENT SESSIONS ===\n{recent_sessions()}\n\n{charter}")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_STREAM_DONE = object()
# Within ONE lane there is ONE SDK client. Two overlapping /ask_stream requests used to call
# query() and receive_response() on it concurrently, so one turn's messages could be delivered
# into the other turn's stream — the "I asked something else and then the rest of the last
# answer showed up" bug. Turns are serialized PER LANE; a second question waits instead of
# corrupting the first. Different lanes are genuinely independent — see class Lane.
# Idle-gap ceiling for one turn's message stream, in seconds. Resets on every message, so
# it bounds SILENCE, not turn length — a long fable turn with thinking on streams deltas
# throughout and never trips it. Last-resort release valve for a lane's turn lock; see
# _run_escalated. Override with HELIX_STREAM_TIMEOUT.
_TURN_STREAM_TIMEOUT = float(os.environ.get("HELIX_STREAM_TIMEOUT", "900"))

# One tool result arrives as one JSON line on the CLI's stdout. The SDK refuses to buffer a
# line past this, and blowing the limit kills its reader task outright — so the ceiling has to
# clear the largest result a turn can realistically produce (a big file read, a wide grep).
# The SDK's own default is 1MB, which a single Read of a large log will exceed.
MAX_MSG_BYTES = int(os.environ.get("HELIX_MAX_MSG_BYTES", 32 * 1024 * 1024))

# ORDER 23c — the OS-level write wall on Odin's shell (item 25). The hook judges a command's
# TEXT; this judges what the command DOES. Every Bash call on CHAT and WORK runs under bwrap with
# the guarded paths bound read-only, so a write there fails with EROFS however it is spelled.
# The CLI calls CLAUDE_CODE_SHELL_PREFIX with ONE argument, the whole command string (measured
# 2026-10-01: Bash commands, plugin hooks and MCP stdio servers alike). An inline multi-word
# prefix does not work — the CLI treats it as one program — hence a wrapper file, which app.py
# rewrites at every options build and checks by sha256. The wall ro-binds the wrapper and
# factgate_hook's P7 guards it from the editor tools.
# NOT closed, accepted (R7): user D-Bus (`systemd-run --user`), Helix's :7777 routes, other user
# daemons. Odin's own `sudo -n helix-vault-add` fails in here; the nightly runs on SCHED.
WALL_EXEC = f"{BRAIN}/wall_exec.sh"
WALL_PATHS = tuple(os.path.realpath(p) for p in (
    f"{BRAIN}/memory/facts", f"{BRAIN}/workspace/factgate", f"{BRAIN}/workspace/seam/registry",
    f"{BRAIN}/workspace/catches", f"{BRAIN}/factgate_hook.py", f"{BRAIN}/test_factgate_hook.py",
    f"{BRAIN}/app.py", f"{BRAIN}/start_helix.sh", WALL_EXEC, f"{BRAIN}/build_orders",
    f"{BRAIN}/workspace/cycle",      # ORDER 27: the cycle ledgers are written outside this wall
    f"{BRAIN}/nightly_restart.sh", f"{BRAIN}/.lkg",    # ORDER 31: the restart and its rollback copy
    f"{BRAIN}/memory/PINNED.txt",    # ORDER 33b: the pin list (a file under memory/, not memory/facts/)
    f"{BRAIN}/workspace/cycle/PATCHES.jsonl",    # ORDER 34: inside workspace/cycle already; named so it stays walled
    f"{BRAIN}/memory/archive"))      # ORDER 35b: archived facts; only the user's `factgate archive`/`restore` move them
# Self-bound read-write, so each becomes a mount point and `mv ~/helix/workspace elsewhere`
# fails with EBUSY instead of carrying the guarded dirs away and freeing their old names.
WALL_ANCESTORS = tuple(os.path.realpath(p) for p in (
    BRAIN, f"{BRAIN}/memory", f"{BRAIN}/workspace", f"{BRAIN}/workspace/seam"))
_WALL_HEAD = ("#!/bin/bash\n"
              "# Written by app.py (_write_wall) at every options build. Edits are overwritten.\n"
              "[ $# -eq 1 ] || { echo \"WALL: expected one command argument, got $#\" >&2; exit 126; }\n")


def _wall_script(bwrap):
    """The wrapper's text. bwrap=None is the fail-closed form (R6): refuse, loudly."""
    import shlex
    if not bwrap:
        return _WALL_HEAD + "echo 'WALL: bwrap missing, command refused' >&2\nexit 126\n"
    q = lambda p: shlex.quote(os.path.realpath(p))
    # --dev-bind, NOT --dev: under a bare --bind / / every >/dev/null fails (nodev in the userns)
    # and nvidia-smi loses the GPU. No --proc, no network unshare.
    args = [shlex.quote(bwrap), "--bind / /", "--dev-bind /dev /dev"]
    args += [f"--bind {q(a)} {q(a)}" for a in WALL_ANCESTORS]
    args += [f"--ro-bind-try {q(p)} {q(p)}" for p in WALL_PATHS]
    return _WALL_HEAD + "exec " + " \\\n  ".join(args) + ' \\\n  -- /bin/bash -c "$1"\n'


def _put_script(path, text):
    """Write `text` to `path` as 0755 via a rename (never through a symlink), then prove it."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    with open(path, "rb") as fh:
        got = hashlib.sha256(fh.read()).hexdigest()
    return got == hashlib.sha256(text.encode()).hexdigest()


def _write_wall():
    """Write the wrapper; return the path to hand the CLI. Never returns an unwalled shell: if
    the wrapper cannot be written and verified, the answer is a refusal script elsewhere."""
    import shutil
    try:
        if _put_script(WALL_EXEC, _wall_script(shutil.which("bwrap"))):
            return WALL_EXEC
        why = "failed its sha256 check"
    except Exception as e:
        why = f"could not be written ({type(e).__name__})"
    print(f"HELIX WALL: {WALL_EXEC} {why}; Odin's shell is refused until it is fixed", flush=True)
    fd, refuse = tempfile.mkstemp(prefix="helix_wall_refuse_", suffix=".sh")
    os.close(fd)
    text = (_WALL_HEAD + f"echo 'WALL: wall_exec.sh {why}, command refused' >&2\nexit 126\n")
    if not _put_script(refuse, text):
        raise RuntimeError("write wall: cannot write even the refusal script")
    return refuse


def _lane_options(lane):
    """Build the SDK options for ONE lane. Split out of _ensure_client so the tool wall and the
    model pin are testable without opening a session."""
    from claude_agent_sdk import ClaudeAgentOptions
    if lane.no_tools:
        import frontdesk
        # The front desk is a mouth, not a pair of hands. Three independent walls, because this
        # is the model with the least judgement and the most exposure to whatever the user says:
        #   allowed_tools=[]    — nothing is on the menu
        #   permission_mode     — NOT bypassPermissions, so a tool call would need a prompt that
        #                         headless cannot answer, and fails instead of running
        #   setting_sources=[]  — no CLAUDE.md, no skills, no plugins to smuggle one back in
        return ClaudeAgentOptions(model=lane.want_model, cwd=WORKSPACE, cli_path=CLAUDE_CLI,
                                  system_prompt=frontdesk.BRIEF,
                                  allowed_tools=[], setting_sources=[], max_turns=1,
                                  max_buffer_size=MAX_MSG_BYTES,
                                  include_partial_messages=True)
    # The fact gate. Only this branch: the front desk has no tools to gate, and hooks on a
    # lane that can never call one are just a way to break it. matcher=None so the hook sees
    # every tool — the rules are about paths and commands, not tool names, and a name-matcher
    # would have to be kept in step with them. See factgate_hook.py for what it does NOT close.
    from claude_agent_sdk import HookMatcher
    import factgate_hook
    # The write wall (ORDER 23c). CHAT and WORK only: SCHED carries the nightly, which must
    # write the registry, the ledgers and the vault.
    walled = lane in (CHAT, WORK)
    return ClaudeAgentOptions(model=lane.want_model, permission_mode="bypassPermissions",
                              cwd=WORKSPACE, cli_path=CLAUDE_CLI,
                              env={"CLAUDE_CODE_SHELL_PREFIX": _write_wall()} if walled else {},
                              extra_args=CHROME_LANES_FLAG if walled else {},
                              max_buffer_size=MAX_MSG_BYTES,
                              hooks={"PreToolUse": [HookMatcher(matcher=None,
                                         hooks=[factgate_hook.pre_tool_use], timeout=30)],
                                     "PostToolUse": [HookMatcher(matcher=None,
                                         hooks=[factgate_hook.post_tool_use], timeout=30)]},
                              # The fan-out wall and the spend backstop. See FANOUT_TOOLS.
                              disallowed_tools=_fanout_tools_now(),
                              max_budget_usd=TURN_BUDGET_USD,
                              # Without this the SDK only hands back COMPLETE assistant messages,
                              # so text landed in big silent lumps with tool gaps between them.
                              include_partial_messages=True)


async def _ensure_client(lane=None):
    lane = CHAT if lane is None else lane
    if lane.client is not None and lane.reader_task is not None and lane.reader_task.done():
        # The stream reader died between turns (transport failed silently). A client without
        # a live reader can only hang the next turn — rebuild instead.
        await _drop_client(lane)
    want_fanout = _fanout_tools_now()
    # `is not None` matters: a lane whose settings are UNKNOWN (a directly-injected client, or
    # one from before this field existed) has not been proven stale, and dropping a live session
    # on a missing label rather than a differing one throws away a working turn.
    if (lane.client is not None and lane.model is not None
            and (lane.model != lane.want_model or lane.fanout != want_fanout)):
        # Model switched in the sidebar, or ULTRACODE was armed. Both are baked into the CLI
        # session at connect time, so honouring them means a new session. Cheap: _drop_client
        # clears `primed`, so the next turn re-sends memory and conversation — a re-prime, not
        # a context loss.
        await _drop_client(lane)
    if lane.client is None:
        from claude_agent_sdk import ClaudeSDKClient
        c = ClaudeSDKClient(_lane_options(lane))
        await c.connect()
        lane.client = c; lane.primed = False
        lane.model, lane.fanout = lane.want_model, want_fanout
        # One reader owns the stream from the moment the client exists — this is what makes a
        # background job's report arrive as a proactive bubble instead of rotting in the buffer.
        lane.reader_task = asyncio.get_running_loop().create_task(_read_stream(c, lane))
    return lane.client

async def _drop_client(lane=None):
    """Retire this lane's client so its next turn reconnects.

    A fatal transport error (oversized message, broken pipe) kills the SDK's reader task but
    leaves the client object alive. Without this the cached client is reused forever and every
    later turn fails — the state that used to force a restart of Helix.
    """
    lane = CHAT if lane is None else lane
    r, lane.reader_task = lane.reader_task, None
    if r is not None:
        r.cancel()                         # a deliberate drop is not a stream failure
    c, lane.client, lane.primed = lane.client, None, False
    if c is not None:
        try: await c.disconnect()          # don't leak the CLI subprocess
        except Exception: pass

_FATAL_HINTS = ("buffer size", "decode json", "transport", "broken pipe",
                "connection", "closed", "reader")

def _turn_error_text(e):
    """Say what happened in terms of the thing the user did, not the SDK's internals."""
    s = str(e); low = s.lower()
    if "buffer size" in low or "decode json" in low:
        mb = MAX_MSG_BYTES // (1024 * 1024)
        return (f"A tool returned more than the {mb} MB that can be read in one piece, so the "
                f"session dropped. I've reconnected — ask again, and narrow whatever produced "
                f"that much output (read a slice of the file, or pipe it through head).")
    if "authenticate" in low or "oauth" in low or "expired" in low or "unauthorized" in low:
        return ("The Claude sign-in expired, so this turn couldn't run. Sign in again — run "
                "`claude` in a terminal and use /login — then ask me again. I've dropped the "
                "dead session, so I'll pick up the new sign-in on the next message without "
                "needing a restart.")
    return s or "that turn failed"

def _credentials_state():
    """(ok, human-readable note) for the OAuth session the CLI will use. Read-only; never logs
    or returns the token itself."""
    import json as _json
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or f"{HOME}/.claude"
    path = f"{cfg}/.credentials.json"
    try:
        exp = (_json.load(open(path)).get("claudeAiOauth") or {}).get("expiresAt")
    except Exception:
        return False, f"no readable credentials at {path} — run `claude` and /login"
    if not exp:
        return False, f"{path} has no expiry — run `claude` and /login"
    left = exp / 1000 - time.time()
    if left <= 0:
        return False, f"sign-in expired — run `claude` and /login (config: {cfg})"
    return True, f"sign-in valid for {left/3600:.1f}h (config: {cfg})"

def _tool_summary(name, inp):
    inp = inp or {}
    if str(name).lower() == "bash": return str(inp.get("command", ""))[:140]
    for k in ("file_path", "path", "pattern", "url", "query", "prompt"):
        if k in inp: return f"{name}: {str(inp[k])[:120]}"
    return str(name)

# ---- v4 MTRX instrument stream — additive. index.html ignores every field and event below. ----
# How much of a tool call reaches the UI. These ride an SSE stream to a browser on localhost,
# so the limit is legibility, not bandwidth — raise them freely.
TOOL_INPUT_CAP  = int(os.environ.get("HELIX_TOOL_INPUT_CAP",  40000))   # whole input payload
TOOL_VALUE_CAP  = int(os.environ.get("HELIX_TOOL_VALUE_CAP",  16000))   # any single string in it
TOOL_RESULT_CAP = int(os.environ.get("HELIX_TOOL_RESULT_CAP", 24000))   # tool OUTPUT
_tool_t0  = {}             # tool_use_id -> epoch ms it was requested (for the duration readout)
_tts_live = None           # the turn's live _TTSPipeline, so /metrics can read a real queue depth

def _elide(s, cap=TOOL_RESULT_CAP, head=2500, tail=1500):
    """Keep the beginning AND the exit of long output, and SAY how much was cut.
    Silent truncation reads as 'that was all of it', so the cut is always printed."""
    s = s if isinstance(s, str) else str(s)
    if len(s) <= cap: return s
    cut = len(s) - head - tail
    return s[:head] + f"\n\n… {cut:,} chars elided …\n\n" + s[-tail:]

def _elide_values(v, depth=0):
    """Shorten long STRINGS inside the payload, leaving the structure intact.

    Truncating the serialised JSON instead — which is what this used to do — produces text that
    is no longer JSON, so the UI cannot parse out the file path or the old/new strings, and a
    large Edit renders with no diff at all. Cutting the values keeps the document valid."""
    if isinstance(v, str):
        return _elide(v, TOOL_VALUE_CAP, int(TOOL_VALUE_CAP * 0.7), int(TOOL_VALUE_CAP * 0.3))
    if depth > 6: return v
    if isinstance(v, dict):  return {k: _elide_values(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):  return [_elide_values(x, depth + 1) for x in v]
    return v

def _tool_input_json(inp):
    """The WHOLE command / path / patch, not _tool_summary's 140-char ledger line.

    Always valid JSON: long values are cut individually so the UI can still read the structure."""
    try:
        j = json.dumps(_elide_values(inp if inp is not None else {}),
                       ensure_ascii=False, indent=2, default=str)
    except Exception:
        j = str(inp)
    # a backstop for a payload with a great many keys; per-value elision handles the normal case
    if len(j) > TOOL_INPUT_CAP:
        j = _elide(j, TOOL_INPUT_CAP, int(TOOL_INPUT_CAP * 0.7), int(TOOL_INPUT_CAP * 0.3))
    return j

def _result_text(b):
    """ToolResultBlock.content is a str, a list of {type:'text',text:…} parts, or None."""
    c = getattr(b, "content", None)
    if c is None: return ""
    if isinstance(c, str): return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict):
                out.append(part.get("text") or part.get("content") or json.dumps(part, default=str))
            else:
                out.append(getattr(part, "text", None) or str(part))
        return "\n".join(x for x in out if x)
    return str(c)

def _usage_payload(msg):
    """Usage/cost off the SDK's ResultMessage. Field names move between SDK versions, so every
    read is defensive: a missing field is simply absent from the payload — never a fake zero,
    and never an exception that would kill the turn."""
    u = getattr(msg, "usage", None) or {}
    if not isinstance(u, dict):
        u = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens")}
    def g(*names):
        for n in names:
            v = u.get(n)
            if v is None: v = getattr(msg, n, None)
            if v is not None: return v
        return None
    out = {}
    for key, names in (("input_tokens",  ("input_tokens",)),
                       ("output_tokens", ("output_tokens",)),
                       ("cache_read",    ("cache_read_input_tokens", "cache_read")),
                       ("cache_write",   ("cache_creation_input_tokens",)),
                       ("cost_usd",      ("total_cost_usd", "cost_usd")),
                       ("duration_ms",   ("duration_ms",)),
                       ("num_turns",     ("num_turns",))):
        v = g(*names)
        if v is not None: out[key] = v
    return out

# ORDER 27 (item 16) — one line per turn's ResultMessage, so token spend is a number a reviewer
# can count. Odin's shell cannot write it (workspace/cycle is in WALL_PATHS); this process can.
# Only the server writes it: a test that imports app gets None and writes nothing.
TURNS_LEDGER = f"{BRAIN}/workspace/cycle/TURNS.jsonl" if __name__ == "__main__" else None

def _log_turn_usage(lane, usage):
    """Append {"ts": <UTC Z>, "lane", "model", <usage payload>} with one O_APPEND write. A failure
    prints one line and never touches the turn."""
    if not TURNS_LEDGER: return
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "lane": getattr(lane, "name", None),
               "model": getattr(lane, "model", None) or getattr(lane, "want_model", None)}
        if usage is None: raise ValueError("no usage payload for this turn")
        rec.update(usage)
        fd = os.open(TURNS_LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try: os.write(fd, (json.dumps(rec, default=str) + "\n").encode("utf-8"))
        finally: os.close(fd)
    except Exception as e:
        try: print(f"HELIX: [cycle] could not append to {TURNS_LEDGER}: {e}", flush=True)
        except Exception: pass

# ORDER 28 (item 24) — the attempt ledger: every attempt is logged BEFORE its result exists, and
# regate prints K = the attempts counted from it. Odin's shell cannot write workspace/cycle
# (WALL_PATHS); it can only POST /attempt from loopback, and this process appends. No route edits
# or deletes a line. Off (None) unless app.py runs as __main__, like TURNS_LEDGER.
ATTEMPTS_LEDGER = f"{BRAIN}/workspace/cycle/ATTEMPTS.jsonl" if __name__ == "__main__" else None
_attempts_lock = threading.Lock()
# regate.py's SCOPE and scope_problem, copied (not imported at start-up); a test pins them equal.
_ATTEMPT_SCOPE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
_ATTEMPT_KINDS = ("run", "seed", "variant", "prompt")
_ATTEMPT_KEYS = ("scope", "entry", "kind", "n", "cmd", "cwd", "pid")

def _attempt_scope_problem(scope):
    if not isinstance(scope, str) or not _ATTEMPT_SCOPE.match(scope):
        return f"scope {json.dumps(scope)} must be lower-case and `/`-separated, e.g. \"crosscheck/attest\""
    return None

def _attempt_record(body):
    """(record, None) or (None, what is wrong). The server adds ts, seq and prev; a body that
    tries to set them, or anything else unknown, is refused."""
    if not isinstance(body, dict): return None, "body must be a JSON object"
    extra = sorted(set(body) - set(_ATTEMPT_KEYS))
    if extra: return None, f"unknown field(s) {extra}; allowed: {list(_ATTEMPT_KEYS)}"
    is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)
    rec = {"scope": body.get("scope")}
    if "scope" not in body: return None, "scope is required"
    p = _attempt_scope_problem(rec["scope"])
    if p: return None, p
    if "entry" in body:
        if not (isinstance(body["entry"], str) and re.fullmatch(r"\d{4}", body["entry"])):
            return None, "entry must be a regate entry id like \"0005\""
        rec["entry"] = body["entry"]
    rec["kind"] = body.get("kind", "run")
    if rec["kind"] not in _ATTEMPT_KINDS: return None, f"kind must be one of {list(_ATTEMPT_KINDS)}"
    rec["n"] = body.get("n", 1)
    if not is_int(rec["n"]) or rec["n"] < 1: return None, "n must be an integer >= 1"
    rec["cmd"] = body.get("cmd")
    if not isinstance(rec["cmd"], str) or not rec["cmd"] or len(rec["cmd"]) > 300:
        return None, "cmd is required: a non-empty string of at most 300 characters"
    rec["cwd"] = body.get("cwd")
    if not isinstance(rec["cwd"], str) or not rec["cwd"].startswith("/") or len(rec["cwd"]) > 4096:
        return None, "cwd is required: an absolute path"
    if "pid" in body:
        if not is_int(body["pid"]) or body["pid"] < 1: return None, "pid must be an integer >= 1"
        rec["pid"] = body["pid"]
    return rec, None

def _append_attempt(rec):
    """Add ts (UTC Z, this clock), seq (line number) and prev (sha256 of the previous line's text,
    `genesis` first), then one O_APPEND write under a lock. Returns (seq, ts); raises on failure."""
    import fcntl
    with _attempts_lock:
        fd = os.open(ATTEMPTS_LEDGER, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with open(ATTEMPTS_LEDGER, "rb") as fh: old = fh.read().decode("utf-8", "replace").splitlines()
            rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), seq=len(old) + 1,
                       prev=hashlib.sha256(old[-1].encode("utf-8")).hexdigest() if old else "genesis")
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally: os.close(fd)                                  # closing drops the flock
    return rec["seq"], rec["ts"]

@app.route("/attempt", methods=["POST"])
def attempt_log():
    """Log one attempt before it runs. Loopback clients only: `tailscale serve` also arrives from
    127.0.0.1, so a forwarded or Tailscale-marked request is refused too (the phone face)."""
    if request.remote_addr not in ("127.0.0.1", "::1") or any(
            k.lower() == "x-forwarded-for" or k.lower().startswith("tailscale-") for k in request.headers.keys()):
        return jsonify(error="loopback only"), 403
    rec, problem = _attempt_record(request.get_json(silent=True))
    if problem: return jsonify(error=problem), 400
    if not ATTEMPTS_LEDGER: return jsonify(error="attempt ledger is off (app imported, not run)"), 503
    try:
        seq, ts = _append_attempt(rec)
    except Exception as e:
        try: print(f"HELIX: [attempt] could not append to {ATTEMPTS_LEDGER}: {e}", flush=True)
        except Exception: pass
        return jsonify(error=f"could not append: {type(e).__name__}"), 500
    return jsonify(seq=seq, ts=ts)

def _mem_state(budget=FACTS_BUDGET):
    """Which facts actually made it into context, and how much of FACTS_BUDGET they used.
    ORDER 33b: the same _select_inline that _facts_digest calls, so /mem cannot drift from the
    primed preamble; it does not change what _facts_digest does."""
    inline, index, tiers = _select_inline(budget)
    names = [fn for fn, _t in inline]
    used = sum(tiers.values())
    try: whole = _memretrieve().superseded_by(FACTS)     # ORDER 43: /mem shows the index pointers
    except Exception: whole = {}
    try: ret = _memretrieve().retired_in(FACTS)          # ORDER 70: which passages are retired, by whom
    except Exception: ret = {"applied": [], "unresolved": []}
    retired = {}
    for r in ret["applied"]:
        if r["b"] not in retired.setdefault(r["a"], []): retired[r["a"]].append(r["b"])
    return {"count": len(inline) + len(index), "loaded": len(names), "budget": budget,
            "budget_used": used, "indexed": len(index), "names": names, "tiers": tiers,
            "superseded": {fn: whole[fn] for fn, _t, _p in index if fn in whole},
            "retired": retired, "retired_unresolved": ret["unresolved"]}

def _emit_delta(msg, q, streamed):
    """One token-level text delta from the raw Anthropic stream event -> straight to the UI."""
    ev = getattr(msg, "event", None) or {}
    if ev.get("type") != "content_block_delta": return
    d = ev.get("delta") or {}
    if d.get("type") != "text_delta": return
    t = d.get("text") or ""
    if t:
        streamed["any"] = True
        q.put(("delta", {"text": t}))

def _emit_blocks(msg, q, final_text, suppress_text=False):
    from claude_agent_sdk import TextBlock, ToolUseBlock, ToolResultBlock
    c = getattr(msg, "content", None)
    if not isinstance(c, list): return
    for b in c:
        if isinstance(b, TextBlock):
            # final_text is always built from the authoritative complete block. The UI emission is
            # skipped when the same text already went out as deltas, or it would appear twice.
            if b.text.strip():
                final_text.append(b.text)
                if not suppress_text: q.put(("text", {"text": b.text}))
        elif isinstance(b, ToolUseBlock):
            t0 = int(time.time() * 1000)
            _tool_t0[b.id] = t0
            if len(_tool_t0) > 400: _tool_t0.clear()      # bounded: a turn never has this many open calls
            # `summary` is unchanged (index.html reads it). `input` and `t0` are new.
            q.put(("tool", {"id": b.id, "name": b.name,
                            "summary": _tool_summary(b.name, b.input),
                            "input": _tool_input_json(b.input), "t0": t0}))
        elif isinstance(b, ToolResultBlock):
            ok = not bool(getattr(b, "is_error", False))
            t1 = int(time.time() * 1000)
            t0 = _tool_t0.pop(b.tool_use_id, None)
            q.put(("tool_done", {"id": b.tool_use_id, "ok": ok}))     # unchanged — index.html reads this
            # …and the content that used to be discarded, which is what makes the work legible.
            q.put(("tool_result", {"id": b.tool_use_id, "ok": ok,
                                   "content": _elide(_result_text(b)),
                                   "t1": t1, "ms": (t1 - t0) if t0 else None}))

def _crosstalk_odin():
    # what ALICE has said since Odin last spoke — injected so Odin is aware of his twin (no manual relaying)
    out = []
    for r, t in reversed(HISTORY):
        if r == "odin": break
        if r == "alice": out.append(t)
    if not out: return ""
    out.reverse()
    return ("[HELIX cross-talk — ALICE (your Gemini twin) said this since your last turn; take her audit/"
            "design into account, and address it if relevant:\n" + "\n\n---\n\n".join(out) + "\n]\n\n")

# ORDER 34 (item 17) — the per-turn memory patch. Memory is primed once per lane session (124_*),
# so a query-aware pick rides along each turn: up to PATCH_K facts relevant to the user's last turns,
# appended after his text. The retriever is workspace/memretrieve (hybrid = BM25 + a small local
# embedding model; memeval/RESULT_ORDER34.txt is why hybrid and not BM25 alone). Facts already
# sent this session — the preamble's inline set, or an earlier patch — are not sent again.
# Bounded: PATCH_TIMEOUT on retrieval; on a timeout or any error there is no patch and one log
# line, never a failed turn. HELIX_MEMORY_PATCH=0 turns it off. Like the ledgers it is on only when
# app.py runs as the server (a test that imports app gets no patch unless it sets
# HELIX_MEMORY_PATCH=1), so test turns never load the model or touch the live vector cache.
# Each turn's patch (files and scores, or why it is empty) goes to PATCHES.jsonl, which Odin's
# shell cannot write (walled).
MEMRETRIEVE_PY = f"{WORKSPACE}/memretrieve/memretrieve.py"
PATCH_ARM, PATCH_K, PATCH_BUDGET, PATCH_TIMEOUT = "hybrid", 5, 6000, 1.5
PATCHES_LEDGER = f"{BRAIN}/workspace/cycle/PATCHES.jsonl" if __name__ == "__main__" else None
_mr = None
_mr_lock = threading.Lock()
_patch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mempatch")
_PATCH_DEFAULT = __name__ == "__main__"


def _patch_on():
    v = os.environ.get("HELIX_MEMORY_PATCH")
    return _PATCH_DEFAULT if v is None else v != "0"


def _memretrieve():
    """memretrieve, loaded once from its file on first use (not at start-up). The model inside it
    loads once too, lazily, on the first embed."""
    global _mr
    with _mr_lock:
        if _mr is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_helix_memretrieve", MEMRETRIEVE_PY)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _mr = m
        return _mr


def _memory_patch_warm():
    """Load the model and the index once, off the request path (run at server start)."""
    if not _patch_on(): return
    try:
        _patch_pool.submit(lambda: _memretrieve().default_index().retrieve(
            "warm up", arm=PATCH_ARM)).result(timeout=300)
        print("HELIX: [patch] memory patch ready", flush=True)
    except Exception as e:
        print(f"HELIX: [patch] warm-up failed — {type(e).__name__}: {e}", flush=True)


def _user_turns(text, n=3):
    """This turn's text, then the user's previous turns, newest first."""
    return [text] + [t for r, t in reversed(HISTORY) if r == "you"][:n - 1]


def _log_patch(rec):
    """One O_APPEND line to PATCHES.jsonl. Off unless app.py runs as __main__; never raises."""
    if not PATCHES_LEDGER: return
    try:
        fd = os.open(PATCHES_LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try: os.write(fd, (json.dumps(rec, default=str) + "\n").encode("utf-8"))
        finally: os.close(fd)
    except Exception as e:
        try: print(f"HELIX: [patch] could not append to {PATCHES_LEDGER}: {e}", flush=True)
        except Exception: pass


def _memory_patch(lane, text):
    """The patch block for this turn of `lane` ("" when there is none). Never raises."""
    if not _patch_on(): return ""
    skip = set(getattr(lane, "mem_sent", None) or ())
    turns = _user_turns(text)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "lane": getattr(lane, "name", None),
           "arm": PATCH_ARM, "turns": len(turns), "q": (text or "")[:120]}
    t0 = time.monotonic()

    def work():
        mr = _memretrieve()
        ix = mr.default_index()
        r = ix.retrieve(turns, k=PATCH_K, char_budget=PATCH_BUDGET, arm=PATCH_ARM, skip=skip)
        return r, mr.render_patch(ix, r["hits"])
    try:
        r, patch = _patch_pool.submit(work).result(timeout=PATCH_TIMEOUT)
    except Exception as e:
        why = "timeout" if isinstance(e, TimeoutError) else f"error {type(e).__name__}"
        try: print(f"HELIX: [patch] no memory patch this turn — {why}: {e}", flush=True)
        except Exception: pass
        rec.update(reason=why, files=[], chars=0, ms=int((time.monotonic() - t0) * 1000))
        _log_patch(rec)
        return ""
    rec.update(reason=r["reason"], gate={a: round(v, 4) for a, v in r["gate"].items()},
               files=[{"file": f, "score": s, "mode": why.rsplit("; ", 1)[-1], **({"replaced_by": r["replaced_by"][f]} if f in r.get("replaced_by", {}) else {})} for f, s, why in r["hits"]],
               chars=len(patch), ms=int((time.monotonic() - t0) * 1000))
    lane.mem_sent = skip | {f for f, _s, _w in r["hits"]}
    _log_patch(rec)
    return patch


def _inline_names():
    """The facts the preamble inlines (order 33b's selection); empty if it cannot say."""
    try: return {fn for fn, _t in _select_inline()[0]}
    except Exception: return set()


def _turn_prompt(lane, text):
    """What actually goes over the wire for ONE turn of `lane`.

    A no-tools lane — the front desk — is a DIFFERENT PERSON from Odin and is never primed as
    him. _preamble() is ~87k characters of ODIN persona plus the fact digest; on FRONT it both
    drowns the 2.3k-char frontdesk.BRIEF (measured 39:1, which is why the butler stopped
    escalating and started answering as Odin) and costs exactly the latency the front lane
    exists to remove. Its conversational memory is the SDK session itself, not a preamble.
    ORDER 34: a tool lane's turn ends with the memory patch (see _memory_patch); the front desk
    never gets one.
    """
    if getattr(lane, "no_tools", False):
        return text
    if lane.primed:
        body = text
    else:
        body = _preamble() + "\n\n=== USER ===\n" + text
        lane.mem_sent = _inline_names()             # a new session: only the preamble is in it
    patch = _memory_patch(lane, text)
    return _crosstalk_odin() + body + ("\n\n" + patch if patch else "")


async def _run_turn(text, q, lane=None):
    lane = CHAT if lane is None else lane
    from claude_agent_sdk import ResultMessage, StreamEvent
    final_text = []
    streamed = {"any": False}
    lane.interrupting = False
    try:
        c = await _ensure_client(lane)
        # `resent` says whether the memory block is going over the wire THIS turn or was already
        # primed into the session — otherwise the readout would claim a load that didn't happen.
        try: q.put(("mem", dict(_mem_state(), resent=not lane.primed)))
        except Exception: pass
        prompt = _turn_prompt(lane, text)
        lane.primed = True
        # This turn's messages arrive via the long-lived reader (_read_stream), routed here.
        # Registered BEFORE query() so the first message can't slip past into a proactive buf.
        aq = asyncio.Queue()
        lane.router.expect(aq)
        try:
            await c.query(prompt)
            while True:
                # Bounded so a turn that never gets routed unwinds through the `finally`
                # below and hands its registration back, instead of leaking a coroutine
                # that holds the router hostage. Same idle-gap ceiling as _run_escalated.
                msg = await asyncio.wait_for(aq.get(), _TURN_STREAM_TIMEOUT)
                if isinstance(msg, BaseException): raise msg      # reader relayed a dead transport
                if isinstance(msg, ResultMessage):
                    if not final_text and getattr(msg, "result", None): final_text.append(msg.result)
                    usage = None
                    try: usage = _usage_payload(msg); q.put(("usage", usage))
                    except Exception: pass                  # a missing field must not kill the turn
                    _log_turn_usage(lane, usage)            # ORDER 27: TURNS.jsonl; never raises
                    break
                if isinstance(msg, StreamEvent):
                    _emit_delta(msg, q, streamed); continue
                _emit_blocks(msg, q, final_text, suppress_text=streamed["any"])
        finally:
            lane.router.cancel(aq)
        q.put(("final", {"text": "".join(final_text)}))
    except Exception as e:
        # A pressed STOP is an ordinary end to a turn — keep the session and its context.
        # Anything else may have left the transport unusable, so retire it and reconnect next turn.
        if not lane.interrupting:
            try: await _drop_client(lane)
            except Exception: pass
        try: _mark_model_state(getattr(lane, "model", None) or ODIN_MODEL, _is_limit(str(e)))
        except Exception: pass          # telemetry never gets to hide the error it describes
        q.put(("error", {"message": _turn_error_text(e)}))
    else:
        try: _mark_model_state(getattr(lane, "model", None) or ODIN_MODEL, False)
        except Exception: pass          # it answered: this model is live
    finally:
        # ULTRACODE is spent by exactly one turn, however that turn ended. Disarming here rather
        # than on success means a fan-out turn that errors can't leave the permission standing.
        global _ULTRA_ARMED
        if _ULTRA_ARMED:
            _ULTRA_ARMED = False
            try: q.put(("ultracode", {"armed": False}))
            except Exception: pass
        lane.interrupting = False
        q.put(_STREAM_DONE)

def split_sentences(t):
    # break spoken text into ~sentence chunks so TTS can be pipelined (first audio ASAP)
    t = (t or "").strip()
    if not t: return []
    parts = re.split(r'(?<=[.!?])\s+', t)
    chunks, cur = [], ""
    for p in parts:
        cur = (cur + " " + p).strip() if cur else p
        if len(cur) >= 40:              # avoid synthesizing tiny fragments
            chunks.append(cur); cur = ""
    if cur: chunks.append(cur)
    return chunks

def _sse(event, data): return f"event: {event}\ndata: {json.dumps(data)}\n\n"

def _emit_audio(spoken, seed, voice=None):
    # sentence-pipelined TTS emitted as SSE 'audio' events (each sentence as soon as it's synthesized)
    prune_tts()   # prune ONCE per turn here — never inside speak() — so this reply's clips survive until played
    sents = split_sentences(spoken) or ([spoken.strip()] if spoken.strip() else [])
    base = hashlib.md5(seed.encode()).hexdigest()[:8]; n = 0
    for i, sent in enumerate(sents):
        fn, env, dur = speak(sent, f"{base}{i}", voice)
        if fn: yield _sse("audio", {"url": f"/tts/{fn}", "env": env, "dur": dur}); n += 1
    if n == 0 and spoken.strip():
        fn, env, dur = speak(spoken.strip()[:3000], base + "f", voice)
        if fn: yield _sse("audio", {"url": f"/tts/{fn}", "env": env, "dur": dur})

class _TTSPipeline:
    """Synthesize speech WHILE the answer is still being written, instead of after it finishes.

    The old path waited for the complete reply, then rendered every sentence serially before the
    first sound came out — seconds of dead air on a long answer, and it blocked the SSE generator
    the whole time. This starts on sentence 1 as soon as sentence 1 exists.

    One worker on purpose: Kokoro is CPU-bound, and a single worker keeps clips in order for free.
    """
    def __init__(self, seed, voice=None):
        prune_tts()                      # once per turn, never per sentence (clips must outlive playback)
        self.base  = hashlib.md5(seed.encode()).hexdigest()[:8]
        self.voice = voice
        self.buf   = ""                  # raw text not yet released to TTS
        self.futs  = []                  # ordered, one per submitted sentence
        self.n     = 0
        self.ex    = ThreadPoolExecutor(max_workers=1)
        global _tts_live; _tts_live = self          # so /metrics reports a measured queue depth

    def _inside_fence(self, t):
        return t.count("```") % 2 == 1   # hold: clean_for_speech can't strip a fence that isn't closed yet

    def feed(self, delta):
        self.buf += delta
        if self._inside_fence(self.buf): return
        chunks = split_sentences(clean_for_speech(self.buf))
        if len(chunks) > 1:
            for c in chunks[:-1]: self._submit(c)
            self.buf = chunks[-1]

    def _submit(self, sentence):
        if not sentence.strip(): return
        tag = f"{self.base}{self.n}"; self.n += 1
        self.futs.append(self.ex.submit(speak, sentence, tag, self.voice))

    def drain(self, block=False):
        """Emit audio for finished sentences, in order. Never reorders, never skips ahead."""
        while self.futs:
            f = self.futs[0]
            if not block and not f.done(): break
            self.futs.pop(0)
            try: fn, env, dur = f.result()
            except Exception: continue
            if fn: yield _sse("audio", {"url": f"/tts/{fn}", "env": env, "dur": dur})

    def finish(self, fallback_text=""):
        tail = clean_for_speech(self.buf).strip()
        if tail: self._submit(tail)
        self.buf = ""
        if not self.futs and self.n == 0 and fallback_text.strip():
            self._submit(clean_for_speech(fallback_text).strip()[:3000])
        for ev in self.drain(block=True): yield ev
        self.ex.shutdown(wait=False)
        global _tts_live
        if _tts_live is self: _tts_live = None

def _log_turn(text, ans, who="ODIN"):
    HISTORY.append(("you", text)); HISTORY.append((who.lower(), ans))
    del HISTORY[:-80]                      # RAM-side scrollback; context only ever reads [-8:]
    try:
        with open(f"{SESS}/log.md", "a") as f:
            f.write(f"\n## {time.strftime('%F %H:%M')}\n**You:** {text}\n**{who}:** {ans}\n")
    except Exception: pass
    prune_log()                            # roll the transcript; facts are never touched

# ── DURABLE TURNS ───────────────────────────────────────────────────────────
# A turn is produced by a background thread that writes into a TurnBuf, so it
# runs to completion — and gets logged to history — even when the phone's screen
# goes dark and the SSE connection dies. The HTTP stream is just a live
# subscriber; a disconnected phone recovers the answer by polling /turn_state.
_turns = OrderedDict()          # id -> TurnBuf, last few kept for reconnect
_turns_lock = threading.Lock()

# Messages that arrive while an agent turn is RUNNING. They used to block one thread
# each on the lane's turn lock and then run a full stale turn apiece — sending 3-5 messages during
# a long turn meant 3-5 answers to dead questions before the newest one (the user,
# 2026-09-06). Now they wait here and the lock holder drains them as ONE combined turn.

class TurnBuf:
    def __init__(self, tid, text_in, mode):
        self.id = tid; self.text_in = text_in; self.mode = mode
        self.text  = ""            # accumulated streaming text
        self.final = ""            # authoritative full answer once known
        self.meta  = []            # ordered non-text events (mem/usage/greet/tool/error) for replay
        self.status = "running"    # running | done | error
        self.created = time.time()
        self.lock = threading.Lock()
        self.subs = []             # live subscriber queues (SSE readers)
    def emit(self, kind, payload):
        with self.lock:
            if kind in ("delta", "text"): self.text += (payload or {}).get("text", "")
            elif kind == "final":         self.final = (payload or {}).get("text", "")
            else:                         self.meta.append((kind, payload))
            subs = list(self.subs)
        for s in subs:
            try: s.put((kind, payload))
            except Exception: pass
    def finish(self, status="done"):
        with self.lock:
            self.status = status; subs = list(self.subs)
        for s in subs:
            try: s.put(_STREAM_DONE)
            except Exception: pass
    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            snap = dict(text=self.text, final=self.final, meta=list(self.meta),
                        done=self.status != "running")
            self.subs.append(q)
        return q, snap
    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs: self.subs.remove(q)
    def snapshot(self):
        with self.lock:
            return dict(id=self.id, status=self.status,
                        text=(self.final or self.text),
                        done=self.status != "running")

def _register_turn(buf):
    with _turns_lock:
        _turns[buf.id] = buf
        while len(_turns) > 8: _turns.popitem(last=False)   # keep the most recent handful

# ── PROACTIVE REPORTS ───────────────────────────────────────────────────────
# The CLI re-invokes the agent when a background job finishes — while nobody is inside
# receive_response(). Those messages used to sit unread in the SDK's buffer until the next
# question dragged them in (the user, 2026-09-06: "when you say you'll hit me back when the job
# finishes you never do — output doesn't come until I ask"). Now ONE long-lived reader owns
# the stream and routes every message: a turn the user asked for gets its messages via the
# queue registered in _run_turn; a turn arriving with NO user turn waiting is an unsolicited
# report and becomes a proactive TurnBuf, announced to the UI through the /metrics poll.

_NOTE_QUESTION = "(a background job finished — Odin reporting in on his own)"
_note_seq = 0                   # bumps once per delivered proactive bubble; UI watches it
_note_ids = deque(maxlen=5)     # recent proactive turn ids, resolvable via /turn_state


class _NoteBufQ:
    """Adapts the (kind, payload) queue protocol onto a _NoteSink's TurnBuf, so a proactive
    turn reuses _emit_delta/_emit_blocks unchanged."""
    def __init__(self, sink): self.sink = sink
    def put(self, item):
        kind, payload = item
        if kind in ("delta", "text", "tool", "tool_done", "tool_result"): self.sink.seen = True
        self.sink.buf.emit(kind, payload)


class _NoteSink:
    """Accumulates one unsolicited turn. Only registers/announces a bubble if the turn
    actually said something visible — task bookkeeping chatter makes no bubble."""
    def __init__(self):
        self.buf = TurnBuf(uuid.uuid4().hex[:12], _NOTE_QUESTION, "odin")
        self.final = []; self.streamed = {"any": False}; self.seen = False
        self.q = _NoteBufQ(self)

    def feed(self, msg):
        from claude_agent_sdk import ResultMessage, StreamEvent
        if isinstance(msg, ResultMessage):
            if not self.final and getattr(msg, "result", None): self.final.append(msg.result)
            return
        if isinstance(msg, StreamEvent):
            _emit_delta(msg, self.q, self.streamed); return
        _emit_blocks(msg, self.q, self.final, suppress_text=self.streamed["any"])

    def close(self):
        global _note_seq
        ans = "".join(self.final) or self.buf.text
        if not (self.seen and ans.strip()): return       # nothing visible — no bubble
        self.buf.emit("final", {"text": ans})
        _log_turn(_NOTE_QUESTION, ans)
        _register_turn(self.buf)
        self.buf.finish("done")
        _note_seq += 1
        _note_ids.append(self.buf.id)


def _announce(buf):
    """Push a finished turn into the proactive feed so the UI raises it on its own.

    The UI polls /metrics at 1 Hz, notices note_seq moved, and fetches the unseen ids from
    /turn_state. That is the same path a background report uses — so a job nobody is watching
    still lands on screen instead of rotting until the next question.
    """
    global _note_seq
    _note_seq += 1
    _note_ids.append(buf.id)


def _report_job(buf):
    """A WORK job finished. Store Odin's answer whole, then let Helix say the headline.

    Order matters: the answer is persisted BEFORE the headline is attempted, so "give me the
    whole thing" works even if the front desk is down. Helix never retypes a number out of
    Odin's text — it says the outcome and nothing else — which is why it cannot corrupt one.
    """
    snap = buf.snapshot()
    answer = (snap.get("final") or snap.get("text") or "").strip()
    if not answer:
        return
    try:
        frontdesk.set_last_job(NOTEPAD, buf.id, buf.text_in, answer)
    except Exception as e:
        print(f"HELIX: could not store the job answer ({e})", flush=True)
    ask = ("[a job just finished. Tell the user the HEADLINE in your own words — one or two "
           "sentences, outcome first, no detail he did not ask for. Do NOT quote numbers out "
           "of it unless he asked for that number. Do NOT use any marker.]\n\n"
           f"He asked: {buf.text_in}\n\nOdin's answer:\n{answer[:6000]}")
    raw = front_raw(ask)
    if not raw:
        return                                  # leave Odin's own text in the bubble
    spoken, _ctl = frontdesk.parse_control(raw)  # a stray marker is parsed off and DISCARDED
    if not spoken.strip():
        return
    buf.emit("text", {"text": "\n\n" + spoken})
    buf.emit("final", {"text": spoken})


def start_job(text, mode="odin", lane=None):
    """Run `text` as a background job on the WORK lane and return its TurnBuf immediately.
    `lane=SCHED` is for the scheduler only: the same job, on the unwalled lane (ORDER 23c).

    The caller does NOT wait. The job holds WORK's turn lock for as long as it needs; CHAT is
    untouched, so Odin stays answerable throughout. When it finishes it announces itself.
    """
    lane = WORK if lane is None else lane
    buf = TurnBuf(uuid.uuid4().hex[:12], text, mode)
    _register_turn(buf)

    def run():
        try:
            _produce(buf, lane=lane)
            try: _report_job(buf)   # Helix says the headline; Odin's full text is kept on the pad
            except Exception as e: print(f"HELIX: report failed ({e})", flush=True)
        finally:
            _announce(buf)          # announce even on failure — a job that died is news too
            buf.announced = True    # logged and pushed: the nightly restart waits for this (ORDER 31)

    threading.Thread(target=run, daemon=True).start()
    return buf


class _DropSink:
    """Swallows the rest of a turn whose consumer gave up (turn errored client-side)."""
    def feed(self, msg): pass
    def close(self): pass


def _opens_a_turn(msg):
    """Can this message legitimately BEGIN an unsolicited turn? Only real content can.
    A SystemMessage (session init/status) or a bare ResultMessage carries nothing to
    report, so it must not open a _NoteSink — an opened sink with no ResultMessage behind
    it never closes, and everything after it lands in the wrong bubble."""
    return msg.__class__.__name__ not in ("SystemMessage", "ResultMessage")


class _MsgRouter:
    """Routes every message from the one long-lived stream. Sinks switch ONLY at
    ResultMessage boundaries — the CLI serializes turns on the session, so everything
    between two ResultMessages belongs to one turn. Runs on the loop thread only.

    Known narrow race, accepted: if a background report starts at the exact moment a user
    turn queries, the first arriving messages are attributed to whoever is waiting. Worst
    case the report and the answer land in swapped bubbles — both still DELIVERED, which
    beats the old behaviour (report silently buffered until the next question)."""
    def __init__(self):
        self.waiting = None     # asyncio.Queue registered by _run_turn before query()
        self.current = None     # the active sink: that queue, a _NoteSink, or a _DropSink

    def expect(self, aq):
        """Register the queue for a user turn that is about to query.

        If an unsolicited sink is open but has produced nothing visible, the user turn
        TAKES IT OVER. The CLI emits a bare session-init message while idle; that opened a
        _NoteSink which no ResultMessage ever closed, so it stayed `current` forever and
        swallowed the next user turn — the answer surfaced as a silent proactive bubble
        while the turn's own queue starved and the lane's turn lock was never released
        (the user, 2026-09-06: "no voice", every later message stuck on "waiting — folding")."""
        self.waiting = aq
        cur = self.current
        if isinstance(cur, _NoteSink) and not cur.seen:
            self.current = None

    def cancel(self, aq):
        """The user turn stopped consuming. Drop its registration; if its turn is still
        streaming, swallow the tail instead of misfiling it as a proactive report."""
        if self.waiting is aq: self.waiting = None
        if self.current is aq: self.current = _DropSink()

    def handle(self, msg):
        end = msg.__class__.__name__ == "ResultMessage"
        if self.current is None:
            if self.waiting is not None:
                self.current, self.waiting = self.waiting, None
            elif _opens_a_turn(msg):
                self.current = _NoteSink()
            else:
                return          # idle bookkeeping (session init): nothing to attribute it
                                # to, and opening a sink here strands one that never closes
        cur = self.current
        try:
            if hasattr(cur, "feed"): cur.feed(msg)
            else: cur.put_nowait(msg)
        except Exception:
            pass                                     # one bad message must not kill the reader
        if end:
            if hasattr(cur, "close"):
                try: cur.close()
                except Exception: pass
            self.current = None

    def fail(self, exc):
        """Transport died: wake any user turn so it errors instead of hanging forever."""
        for s in (self.current, self.waiting):
            if s is not None and not hasattr(s, "feed"):
                try: s.put_nowait(exc)
                except Exception: pass
        for s in (self.current,):
            if s is not None and hasattr(s, "close"):
                try: s.close()
                except Exception: pass
        self.current = self.waiting = None


class Lane:
    """One independent line of thought.

    A lane owns everything that must not be shared: its own CLI session (one client = one
    conversation with its own context), the single reader that drains that session's stream,
    the router that decides whose queue each message belongs to, and the lock that serializes
    turns WITHIN the lane.

    Why lanes exist (2026-09-06): there used to be exactly one of each, module-global. That is
    correct for chat — one human, one thread of conversation, so a second question should wait
    rather than interleave. It is wrong for background work: a job holding the single turn lock
    for an hour left Odin unable to answer anything for that hour. Separate lanes let a long
    job run while chat stays live.

    Lanes deliberately do NOT share context. The worker cannot see the chat conversation and
    vice versa — each primes its own memory. A job's result therefore comes back through the
    proactive-report path, as a bubble Odin raises himself.
    """

    def __init__(self, name, model=None, no_tools=False):
        self.name = name
        # A lane may PIN its model (the front desk is always Haiku, whatever the sidebar says
        # Odin is running). model=None means "follow ODIN_MODEL", which is every lane but FRONT.
        self.model_pin = model
        self.no_tools = no_tools      # FRONT talks and nothing else — see frontdesk.BRIEF
        self.client = None            # the SDK client == this lane's CLI session
        self.primed = False           # has the memory preamble gone over the wire this session?
        self.model = None             # what this lane's live session was CONNECTED with —
        self.fanout = None            # a change in either has to rebuild it (see _ensure_client)
        self.reader_task = None       # the long-lived consumer of this lane's stream
        self.interrupting = False     # a pressed STOP is an ordinary end, not a dead transport
        self.router = _MsgRouter()
        self.lock = threading.Lock()          # serializes turns within this lane
        self.pending = []                     # [(TurnBuf, text)] coalescing queue, per lane
        self.pending_lock = threading.Lock()

    def __repr__(self):
        return f"<Lane {self.name} busy={self.lock.locked()} primed={self.primed}>"

    @property
    def want_model(self):
        """What this lane SHOULD be connected with right now."""
        return self.model_pin or ODIN_MODEL


# The lane that serves the user. Everything that existed before lanes is this one.
CHAT = Lane("chat")

# The lane long background work runs on. Its whole purpose is to be somewhere other than CHAT:
# a job can hold this lock for an hour and the user can still talk to Odin the entire time.
WORK = Lane("work")

# The lane the user actually talks to. Haiku, no tools, no memory — only frontdesk.BRIEF and the
# last few turns of talk. Its whole job is to answer in under a second and decide one thing:
# is this talk, or is this a job for Odin? It reaches Odin through start_job() on WORK.
FRONT_MODEL = os.environ.get("HELIX_FRONT_MODEL", "haiku")
FRONT = Lane("front", model=FRONT_MODEL, no_tools=True)

# The scheduler's lane (ORDER 23c). A tools lane like WORK but OUTSIDE the write wall: the 04:30
# nightly writes the registry, REGATE_LEDGER and catches and anchors the vault with sudo, none of
# which works under bwrap. Only scheduler_tick and the run-now route put work here.
SCHED = Lane("sched")

# ORDER 59: Claude in Chrome for Odin (the user, 2026-10-05) — `--chrome` on CHAT and WORK only, never SCHED or
# FRONT. Page content is untrusted input; the fact-gate hook still sees every tool call, browser ones included.
CHROME_LANES_FLAG = {"chrome": None}


async def _read_stream(c, lane=None):
    """The one consumer of ONE lane's SDK stream. Lives as long as that client does."""
    lane = CHAT if lane is None else lane
    try:
        async for msg in c.receive_messages():
            lane.router.handle(msg)
        lane.router.fail(RuntimeError("the message stream closed"))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        lane.router.fail(e)


def _run_escalated(buf, run_text, log_text=None, lane=None, quiet=False):
    """Run ONE Claude-agent turn (caller already holds the lane's turn lock) and emit into `buf`.
    `run_text` is what the agent sees (may be a folded batch prompt); `log_text` is what
    gets written to the transcript for this bubble (defaults to run_text)."""
    lane = CHAT if lane is None else lane
    # `quiet` is an EXPLICIT flag, not "log_text is None": None already means "log run_text"
    # and every ordinary caller relies on it. Only the front desk passes quiet=True, so its
    # chatter never reaches the transcript that gets replayed into Odin's context.
    log_text = run_text if log_text is None else log_text
    q = queue.Queue(); logged = False; status = "done"
    asyncio.run_coroutine_threadsafe(_run_turn(run_text, q, lane), _loop)
    while True:
        try:
            item = q.get(timeout=_TURN_STREAM_TIMEOUT)
        except queue.Empty:
            # DEADLOCK BREAKER (2026-09-06). This is an IDLE timeout — it resets on every
            # message, so a long thinking turn never trips it. If nothing arrives at all,
            # the stream was lost, and hanging here would hold the lane's turn lock forever: from
            # then on every message Odin gets only "waiting — folding into the current
            # turn" and he has no voice, until Helix is restarted. Ending the turn lets
            # the caller's `finally` give the lock back.
            if not logged:
                if not quiet:
                    _log_turn(log_text, "(this turn ended before I answered — the stream was lost)")
                logged = True
            status = "error"
            buf.emit("error", {"message": "that turn's stream was lost — ending it so Helix "
                                          "stays usable. Ask again."})
            break
        if item is _STREAM_DONE: break
        kind, payload = item
        if kind == "final":
            ans = payload.get("text", "")
            if not quiet: _log_turn(log_text, ans)
            logged = True
            buf.emit("final", {"text": ans})
        elif kind == "error" and not logged:
            # Record the question even though it never got answered, so a turn that dies
            # still tells him what he was working on when he comes back.
            if not quiet:
                _log_turn(log_text, "(this turn ended before I answered — session was rebuilt)")
            logged = True; status = "error"
            buf.emit(kind, payload)
        else:
            if kind == "error": status = "error"
            buf.emit(kind, payload)
    buf.finish(status)

# the user keeps a way past the front desk (spec §8): naming Odin at the START of a message sends
# it straight to CHAT — no routing decision, no summary, his reply comes back whole. Only at
# the start: "ask odin about X" is the user talking TO Helix, not to Odin.
import re as _re
_DIRECT_RE = _re.compile(r"^\s*odin\b[\s,:;!?.\-]*", _re.I)


def _is_direct_to_odin(text):
    m = _DIRECT_RE.match(text or "")
    if not m:
        return (False, text)
    return (True, (text[m.end():]).strip())


def _dispatch_notes(pad_path):
    """Send every waiting note as ONE job, then clear the pad. One job, not N, because they
    were collected as one train of thought and Odin answers them better together."""
    pad = frontdesk.load_pad(pad_path)
    notes = pad["notes"]
    if not notes:
        return None
    text = ("the user left these while you were working — do them together:\n"
            + "\n".join(f"- {n['text']}" for n in notes))
    buf = start_job(text)
    frontdesk.clear_notes(pad_path)
    return buf


def _produce(buf, lane=None):
    """Runs in a daemon thread. Produces the answer into `buf` regardless of who is
    (or is not) listening on the HTTP side. This is what survives the screen going dark."""
    lane = CHAT if lane is None else lane
    text, mode = buf.text_in, buf.mode
    try:
        if mode == "alice":                             # ALICE: Gemini one-shot
            ans, usage = alice_brain(text)
            if usage: buf.emit("usage", usage)
            buf.emit("text", {"text": ans})
            _log_turn(text, ans, "ALICE")
            buf.emit("final", {"text": ans}); buf.finish("done"); return
        direct, stripped = _is_direct_to_odin(text)
        if mode == "helix" and not direct:
            raw = front_raw(text)
            if raw is not None:
                spoken, ctl = frontdesk.parse_control(raw)
                if ctl["kind"] == "job":
                    if WORK.lock.locked():
                        frontdesk.add_note(NOTEPAD, ctl["text"])
                    else:
                        start_job(ctl["text"])
                elif ctl["kind"] == "send":
                    _dispatch_notes(NOTEPAD)
                elif ctl["kind"] == "full":
                    last = frontdesk.load_pad(NOTEPAD)["last_job"]
                    spoken = ((spoken + "\n\n" + last["answer"]) if last
                              else (spoken + "\n\nI have nothing of Odin's on hand, sir."))
                buf.emit("front", {})
                buf.emit("text", {"text": spoken})
                _log_turn(text, spoken)
                buf.emit("final", {"text": spoken}); buf.finish("done"); return
            # front_raw returned None — the front desk is off or broken. Fall through to Odin
            # so the system is never mute. This is the resilience path from spec §7.
        if direct:
            text = stripped or text
        local = greet(text)                             # ODIN fast front desk
        if local is not None:
            buf.emit("greet", {})
            buf.emit("text", {"text": local})
            _log_turn(text, local)
            buf.emit("final", {"text": local}); buf.finish("done"); return
        # ESCALATE → Claude agent + tools. Serialized on the LANE's turn lock, held by THIS thread.
        # If a turn is already running, we DON'T queue a second full turn. We park in
        # _pending; the running turn's holder drains us when it finishes, folding the
        # batch into ONE combined turn whose answer lands on the LATEST message — instead
        # of N stale answers to dead questions (the user, 2026-09-06).
        if not lane.lock.acquire(blocking=False):
            with lane.pending_lock:
                lane.pending.append((buf, text))
            buf.emit("text", {"text": "_(waiting — folding this into the current turn…)_\n\n"})
            lane.lock.acquire()            # blocks until a holder hands off the lock to us
            # The previous holder normally resolves us before releasing. If our buf is
            # already done, we were folded/answered — just release and go.
            with buf.lock:
                resolved = buf.status != "running"
            if resolved:
                lane.lock.release()
                buf.finish(buf.status)     # idempotent; wakes any lingering subscriber
                return
            # Rare: we still hold `text` unrun (e.g. holder crashed before draining). Run it.
            with lane.pending_lock:
                if (buf, text) in lane.pending: lane.pending.remove((buf, text))
        try:
            # Run this buf's own turn, then keep draining anything that piled up while it ran,
            # so newly-arrived messages get folded rather than each spawning a stale turn.
            _run_escalated(buf, text, lane=lane)
            while True:
                with lane.pending_lock:
                    batch = lane.pending[:]; lane.pending.clear()
                if not batch:
                    break
                target, ttext = batch[-1]          # newest message carries the real answer
                fold = ("_(folded into your latest message so I answer that one directly — "
                        "no stale reply to this)_")
                for (b, _t) in batch[:-1]:
                    b.text = ""
                    b.emit("final", {"text": fold}); b.finish("done")
                convo = "\n\n".join(f"- {t}" for (b, t) in batch)
                run_text = (("[The user sent several messages in quick succession while you were "
                             "working. Address them together, but treat the LAST one as the current "
                             "question and lead your answer with it:]\n" + convo)
                            if len(batch) > 1 else ttext)
                _run_escalated(target, run_text, log_text=ttext, lane=lane)
        finally:
            lane.lock.release()
        buf.finish("done")
    except Exception as e:
        buf.emit("error", {"message": _turn_error_text(e)})
        buf.finish("error")

# ============ the user's build commands (order 15) ============
# `/build NN`, `/build status` and `/restart`, typed in the ordinary chat box. They are handled
# HERE, before any model is called: they are the user's instructions to the machine, not questions for
# Odin, and routing them through a turn would put a model between the user and a build.
#
# Only an EXACT match is intercepted. "remind me to /build 16 later" is a sentence, and goes to the
# model like any other. Odin cannot reach these at all: factgate_hook's P6 refuses the shell forms.
BUILD_RUNNER = f"{BRAIN}/build_orders/run_order.sh"
BUILD_RUNS = f"{BRAIN}/build_orders/runs"
# ORDER 24 — the queue: `/build 25-30` or `/build 25,26,27b` runs the orders back to back, each
# through the unchanged run_order.sh, and halts at the first problem. run_queue.sh does the
# refusing, the waiting and the gate; app.py only parses the command and shows QUEUE.json.
BUILD_QUEUE_RUNNER = f"{BRAIN}/build_orders/run_queue.sh"
BUILD_QUEUE_MAX = 6

def _build_command(text):
    """(handled, reply) for an exact build command; (False, None) for anything else."""
    t = (text or "").strip()
    if t == "/restart":
        return True, _do_restart()
    if t == "/build status":
        return True, _build_status()
    if t == "/build stop":
        return True, _build_stop()
    m = re.fullmatch(r"/build\s+([0-9]+[a-z]?)", t)
    if m:
        return True, _build_start(m.group(1))
    m = re.fullmatch(r"/build\s+([0-9]+)-([0-9]+)", t)          # a range: digits only
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            return True, f"refused: range {a}-{b} runs backwards"
        if b - a + 1 > 100:
            return True, f"refused: {b - a + 1} orders — a queue takes at most {BUILD_QUEUE_MAX}"
        return True, _build_queue_start([str(n) for n in range(a, b + 1)])
    m = re.fullmatch(r"/build\s+([0-9]+[a-z]?(?:\s*,\s*[0-9]+[a-z]?)+)", t)   # a list
    if m:
        return True, _build_queue_start(re.split(r"\s*,\s*", m.group(1)))
    if t == "/build":
        return True, ("usage: /build <NN>, /build 25-30, /build 25,26,27b, /build status, "
                      "or /build stop")
    return False, None


def _builder_active():
    """(pid, unit) of a live builder, or (None, None). The lock is written by run_order.sh."""
    lock = f"{BUILD_RUNS}/.lock"
    try:
        pid = int(open(lock).read().strip())
        os.kill(pid, 0)
    except Exception:
        return None, None
    unit = ""
    try:
        last = [l for l in open(f"{BUILD_RUNS}/RUNS.jsonl") if l.strip()][-1]
        unit = json.loads(last).get("unit", "")
    except Exception:
        pass
    return pid, unit


def _build_start(nn):
    import subprocess as _sp
    try:
        r = _sp.run(["bash", BUILD_RUNNER, nn], capture_output=True, text=True, timeout=60,
                    env={**os.environ, "BUILD_TRIGGER": "chat:/build"})
    except Exception as e:
        return f"could not start the builder: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return (out[-1] if out else f"run_order.sh exited {r.returncode} with no output")


def _build_status():
    pid, unit = _builder_active()
    lines = []
    try:
        recs = [json.loads(l) for l in open(f"{BUILD_RUNS}/RUNS.jsonl") if l.strip()]
        recs = [r for r in recs if "queue" not in r]       # the queue's own event lines (order 24)
        if recs:
            r = recs[-1]
            lines.append(f"last run: order {r.get('order')} {r.get('started')} → "
                         f"{r.get('ended')} exit {r.get('exit')}, report "
                         f"{'written' if r.get('report_exists') else 'MISSING'}")
    except FileNotFoundError:
        lines.append("no runs yet")
    except Exception as e:
        lines.append(f"could not read RUNS.jsonl ({type(e).__name__})")
    if pid:
        lines.append(f"a builder is ACTIVE now (pid {pid}{', ' + unit if unit else ''})")
    else:
        lines.append("no builder running")
    lines.append(_queue_status())
    return "\n".join(lines)


def _queue_read():
    try:
        return json.load(open(f"{BUILD_RUNS}/QUEUE.json"))
    except Exception:
        return None


def _queue_live(q):
    """Is this queue still going? Its unit's pid, or a 'starting' queue younger than 2 minutes."""
    if not q or q.get("status") not in ("starting", "running", "waiting"):
        return False
    if q.get("pid"):
        try:
            os.kill(int(q["pid"]), 0)
            return True
        except Exception:
            return False
    from datetime import datetime
    try:
        return time.time() - datetime.fromisoformat(q["started"]).timestamp() < 120
    except Exception:
        return False


def _queue_status():
    q = _queue_read()
    if not q:
        return "no queue"
    orders = q.get("orders", [])
    live = _queue_live(q)
    if live:
        head = (f"queue {q.get('id')} {q.get('status')}: order {q.get('position', 0) + 1} of "
                f"{len(orders)} ({' '.join(orders)})")
        if q.get("current"):
            head += f", current: {q['current']}"
        if q.get("waiting_for"):
            head += f", waiting for {q['waiting_for']} to end"
        if q.get("stop_requested"):
            head += " — /build stop received: no further orders will start"
    else:
        stale = q.get("status") in ("starting", "running", "waiting")
        head = (f"no queue running. Last queue {q.get('id')}: "
                f"{'ended without a word (unit gone)' if stale else q.get('status')}")
        if q.get("halt_reason"):
            head += f" — {q['halt_reason']}"
    out = [head]
    for r in q.get("results", []):
        last = "(no report)"
        try:
            ls = [l.strip() for l in open(f"{BUILD_RUNS}/../reports/ORDER_{r['order']}_report.md")
                  if l.strip()]
            last = ls[-1] if ls else "(empty report)"
        except Exception:
            pass
        out.append(f"  {'✓' if r.get('outcome') == 'ok' else '✗'} {r['order']}: {last}")
    return "\n".join(out)


def _queue_lock():
    import fcntl
    lk = open(f"{BUILD_RUNS}/.queue.lock", "a")
    fcntl.flock(lk, fcntl.LOCK_EX)
    return lk


def _build_stop():
    """Let the current order finish; start no further ones. Never kills a builder."""
    q = _queue_read()
    if not _queue_live(q):
        return "no queue is running"
    with _queue_lock():
        q = _queue_read() or {}
        q["stop_requested"] = True
        tmp = f"{BUILD_RUNS}/QUEUE.json.tmp"
        with open(tmp, "w") as fh:
            json.dump(q, fh, indent=1)
        os.replace(tmp, f"{BUILD_RUNS}/QUEUE.json")
    cur = q.get("current")
    return (f"queue {q.get('id')}: " + (f"order {cur} will finish; " if cur else "")
            + "no further orders will start.")


def _build_queue_start(orders):
    import subprocess as _sp
    try:
        r = _sp.run(["bash", BUILD_QUEUE_RUNNER, "--start", *orders], capture_output=True,
                    text=True, timeout=60, env={**os.environ, "BUILD_TRIGGER": "chat:/build"})
    except Exception as e:
        return f"could not start the queue: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return out or f"run_queue.sh exited {r.returncode} with no output"


NAMECHECK = f"{BRAIN}/build_orders/tools/namecheck.py"
NAMECHECK_TIMEOUT = 30


def _namecheck():
    """(exit code, output lines) of namecheck.py with no args: app.py and its start-up imports.
    0 clean · 4 findings · 2 unavailable. A timeout or a failure to run it is a 2."""
    import subprocess as _sp
    try:
        r = _sp.run([sys.executable, NAMECHECK], capture_output=True, text=True,
                    timeout=NAMECHECK_TIMEOUT, stdin=_sp.DEVNULL, cwd=BRAIN)
    except _sp.TimeoutExpired:
        return 2, [f"namecheck unavailable: timed out after {NAMECHECK_TIMEOUT} s"]
    except Exception as e:
        return 2, [f"namecheck unavailable: {type(e).__name__}: {e}"]
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip().splitlines()


def _do_restart():
    """Restart Helix from the chat box. start_helix.sh pkills the old app.py first — that is the
    'close the old one down' step, and it stays where it is."""
    import subprocess as _sp
    pid, unit = _builder_active()
    if pid:
        return f"not restarting: a builder is active (pid {pid}). Wait for it or check /build status."
    # Lane has no `.busy` attribute — `__repr__` computes it from the turn lock, and that lock
    # is the thing that actually says a turn is in flight.
    busy = [l.name for l in (CHAT, WORK, SCHED) if l.lock.locked()]
    if busy:
        return f"not restarting: a turn is in flight ({', '.join(busy)}). Try again in a moment."
    # ORDER 56: names that would crash the new app.py refuse the restart. A missing or slow
    # linter never does — it must not lock the user out of /restart — the reply says so instead.
    rc, found = _namecheck()
    if rc == 4:
        return ("not restarting: undefined names would crash the new app.py:\n"
                + "\n".join(found[:5]))
    note = ""
    if rc != 0:
        why = next((l for l in found if l.startswith("namecheck unavailable")),
                   f"namecheck unavailable: exit {rc}")
        note = f" ({why})"
    # Its own session, NOT a systemd-run unit: `--collect` reaps the whole cgroup when
    # start_helix.sh exits, taking the new app.py with it (order 15 attempt 2, measured).
    # A new session survives start_helix.sh pkilling this process (order 15, amendment 2).
    from pathlib import Path
    try:
        _sp.Popen(["bash", str(Path.home()/"helix/start_helix.sh")],
                  env={**os.environ, "HELIX_NO_BROWSER": "1"},
                  start_new_session=True, close_fds=True,
                  stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:
        return f"could not restart: {type(e).__name__}: {e}"
    return "restarting… the page reconnects on its own in a few seconds." + note


@app.route("/ask_stream", methods=["POST"])       # front desk → (fast local) OR (Claude agent stream)
def ask_stream():
    text = ((request.json or {}).get("text", "")).strip()
    # the user's build commands never become a turn (order 15).
    handled, reply = _build_command(text)
    if handled:
        def _plain():
            yield _sse("text", {"text": reply})
            yield _sse("final", {"text": reply})
            yield _sse("done", {})
        return Response(_plain(), mimetype="text/event-stream")
    # the user, 2026-09-19: Helix is the default front; Odin is the override (spec §8).
    mode = ((request.json or {}).get("mode", "helix"))
    # PHONE: text-first. The mobile client asks for voice:false and requests audio later via
    # /speak, on a tap. Kokoro is CPU-bound with ONE worker, so synthesizing clips nobody plays
    # would also slow the desktop's speech. Default stays True so the desktop is unchanged.
    want_voice = bool((request.json or {}).get("voice", True))
    if not text: return jsonify(error="empty"), 400
    # The producer runs in its own thread and writes into a durable TurnBuf, so the answer
    # completes and gets logged even if this HTTP connection dies (phone screen goes dark).
    buf = TurnBuf(uuid.uuid4().hex[:12], text, mode)
    _register_turn(buf)
    threading.Thread(target=_produce, args=(buf,), daemon=True).start()

    def gen():
        # Announce the turn id first so the phone can recover this exact turn after a disconnect.
        yield _sse("turn", {"id": buf.id})
        q, snap = buf.subscribe()
        tts = _TTSPipeline(text + str(time.time()),
                           _alice_voice() if mode == "alice" else _helix_voice()) if want_voice else _NullTTS()
        try:
            # Replay anything already produced before this subscriber attached.
            for (k, p) in snap["meta"]: yield _sse(k, p)
            if snap["text"]:
                tts.feed(snap["text"]); yield _sse("text", {"text": snap["text"]})
            if snap["final"]:
                for ev in tts.finish(fallback_text=snap["final"]): yield ev
                yield _sse("final", {"text": snap["final"]})
            if snap["done"]:
                for ev in tts.drain(block=True): yield ev
                yield _sse("done", {}); return
            while True:
                try:
                    item = q.get(timeout=0.12)
                except queue.Empty:
                    for ev in tts.drain(): yield ev          # keep audio flowing during tool calls
                    continue
                if item is _STREAM_DONE:
                    for ev in tts.drain(block=True): yield ev
                    yield _sse("done", {}); break
                kind, payload = item
                if kind in ("delta", "text"):
                    tts.feed(payload.get("text", ""))
                    yield _sse("text", payload)
                    for ev in tts.drain(): yield ev
                elif kind == "final":
                    ans = payload.get("text", "")
                    for ev in tts.finish(fallback_text=ans): yield ev
                    yield _sse("final", {"text": ans})
                else:
                    yield _sse(kind, payload)
        finally:
            buf.unsubscribe(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.route("/turn_state")                         # phone reconnect: recover a turn after the screen slept
def turn_state():
    tid = request.args.get("id", "")
    buf = _turns.get(tid)
    if not buf: return jsonify(found=False)
    return jsonify(found=True, **buf.snapshot())

# ── STUDIO — mounted INTO Helix, not run beside it ────────────────────────────────────────
# Studio was a second Flask app on 7910 with its own DB, its own gate and its own UI, and
# app.py referred to it nowhere. The cost the user actually felt: `tailscale serve` proxies only
# 127.0.0.1:7777, so his PHONE could not reach Studio at all — the render tiers were
# desktop-only. Mounting the blueprint here gives one process, one port, one gate, and the
# phone gets Studio over the tunnel that already exists.
#
# `studio/gate.py` is deliberately NOT installed: its own docstring says it mirrors
# _mobile_gate, so mounting DELETES the duplicate auth surface rather than relocating it.
#
# render_daemon on 7900 stays a separate process on purpose — it holds GPU models resident and
# must be able to restart without touching a live conversation. Not everything separate is drift.
STUDIO_DIR = f"{BRAIN}/studio"
_studio_worker = None


def _mount_studio():
    """Register Studio's blueprint, config and worker onto the Helix app. Best-effort:
    Studio failing to load must never stop Odin from talking."""
    global _studio_worker
    if not os.path.isdir(STUDIO_DIR):
        return
    already = "api" in app.blueprints          # idempotent: re-mounting would double-register
    
    sys.path.insert(0, STUDIO_DIR)          # studio's modules import each other flatly
    try:
        import json as _json
        cfg = _json.load(open(f"{STUDIO_DIR}/config.json"))
        import db as sdb                     # noqa: E402  (studio/db.py)
        from api import api as studio_api, close_conn   # noqa: E402

        app.config["DB_PATH"] = cfg.get("db_abs") or f"{STUDIO_DIR}/{cfg['db']}"
        app.config["OUT_ROOT"] = cfg["OUT_ROOT"] if "OUT_ROOT" in cfg else cfg["out_root"]
        app.config["DAEMON_URL"] = cfg.get("daemon", "http://127.0.0.1:7900")
        if not already:
            app.register_blueprint(studio_api, url_prefix="/studio/api")
            app.teardown_appcontext(close_conn)

        want_worker = os.environ.get("HELIX_NO_STUDIO_WORKER") != "1"
        conn = sdb.connect(app.config["DB_PATH"])
        try:
            sdb.init_db(conn)
            # recover_on_boot requeues jobs a crash left mid-flight, so a worker can pick them
            # up. With NO worker starting there is nothing to pick them up, and running it
            # anyway mutates the real database from every test process that imports app —
            # which is exactly how the user's in-flight job #7 got knocked back to 'queued'
            # (2026-09-07). Only recover when we are actually going to work.
            if want_worker:
                requeued, failed = sdb.recover_on_boot(conn)
                if requeued or failed:
                    print(f"HELIX studio: recover_on_boot requeued={requeued} failed={failed}", flush=True)
        finally:
            conn.close()

        if want_worker:
            from studio import build_executors      # noqa: E402  (studio/studio.py)
            from queue_worker import Worker         # noqa: E402
            _studio_worker = Worker(app.config["DB_PATH"], build_executors(app.config["DB_PATH"]))
            _studio_worker.start()
        print("HELIX studio: mounted at /studio", flush=True)
    except Exception as e:
        print(f"HELIX studio: NOT mounted ({e})", flush=True)


def studio_queued():
    """Queue depth, for /metrics. Never raises — a metrics call must not fail on Studio."""
    try:
        import db as sdb
        conn = sdb.connect(app.config["DB_PATH"])
        try:
            return conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')").fetchone()["c"]
        finally:
            conn.close()
    except Exception:
        return 0


@app.route("/studio")
def studio_page():
    """The review surface — a full window, opened by a button in Helix, not a side panel."""
    return send_from_directory(app.static_folder, "studio.html")


_mount_studio()


# ── SCHEDULING — the half of autonomy that lets Odin START ────────────────────────────────
# The work lane gave him somewhere to run without muting the user. This gives him a reason to
# begin. Due-logic lives in scheduler.py as pure functions; this owns only the clock and the
# routes. Deliberately not cron: two shapes (every N minutes, daily at HH:MM) cover overnight
# work, and expressiveness was never the missing piece.
SCHEDULES = f"{BRAIN}/memory/schedules.json"
_SCHED_POLL_S = 30


def scheduler_tick(now=None):
    """One pass. Starts at most ONE job, then returns.

    Jobs run on SCHED, outside the write wall (ORDER 23c). Skips entirely while WORK or SCHED is
    busy: the lane is serial, so firing anyway would build a
    backlog of stale jobs that all run at once when it frees. A schedule that misses its slot
    waits for the next one — `last_run` is only stamped when a job actually starts, so nothing
    is silently marked done.
    """
    from datetime import datetime
    now = now or datetime.now()
    try:
        items = scheduler.load(SCHEDULES)
    except Exception:
        return None
    if not items or WORK.lock.locked() or SCHED.lock.locked():
        return None

    for sched in scheduler.due(items, now):
        try:
            buf = start_job(sched["prompt"], lane=SCHED)
        except Exception as e:
            print(f"HELIX scheduler: {sched.get('name')!r} failed to start: {e}", flush=True)
            return None
        sched["last_run"] = now.isoformat(timespec="seconds")
        sched["last_turn_id"] = getattr(buf, "id", None)
        try: scheduler.save(SCHEDULES, items)
        except Exception: pass
        print(f"HELIX scheduler: started {sched.get('name')!r}", flush=True)
        if sched.get("id") == NIGHTLY_RESTART_SCHEDULE_ID:      # ORDER 31: only this path arms it
            _nightly_restart_arm(buf)
        return sched                     # one per tick — the lane is serial anyway
    return None


def _scheduler_loop():
    while True:
        try: scheduler_tick()
        except Exception: pass           # a bad schedule must never kill the ticker
        time.sleep(_SCHED_POLL_S)


# ── ORDER 31 (item 20) — THE NIGHTLY RESTART ──────────────────────────────────────────────
# Shipped code and saved facts only take effect after a restart, so the nightly ends with one:
# the LAST step, only when idle, health-checked, with one rollback and a loud alert.
# Narrow by construction — Odin can edit schedules.json and curl localhost, so neither is enough:
#   · armed ONLY by scheduler_tick, for the schedule whose id is this constant. No route arms it
#     (the run-now route does not either). Not a schedule field, env var or setting.
#   · fires only after that job FINISHED and was logged and pushed (`buf.announced`), + 30 s grace;
#   · only inside 04:00–06:59 local, at most one sequence per local calendar day (read from
#     RESTARTS.jsonl), and only when idle (5-minute re-checks, give up at 06:59).
# nightly_restart.sh does the restart, the health check and the rollback; this process writes the
# trigger and skip lines and announces the outcome. The ledger is off unless run as __main__.
NIGHTLY_RESTART_SCHEDULE_ID = "3810a43ef3c7"
NIGHTLY_RESTART_GRACE_S = 30
NIGHTLY_RESTART_RECHECK_S = 300
NIGHTLY_RESTART_SCRIPT = f"{BRAIN}/nightly_restart.sh"
RESTARTS_LEDGER = f"{BRAIN}/workspace/cycle/RESTARTS.jsonl" if __name__ == "__main__" else None
NIGHTLY_ATTEMPTS = f"{BRAIN}/workspace/cycle/ATTEMPTS.jsonl"       # read only (order 28's ledger)
# ORDER 67 (item 32): reboot instead of restart, only when machine.py's line for TODAY says one is
# due AND gdm logs __USER__ in by itself (otherwise Helix would stay down until the user logs in).
# helix-machine re-checks the reason itself as root; this process cannot supply one.
NIGHTLY_MACHINE = f"{BRAIN}/workspace/seam/MACHINE.jsonl"          # read only (machine.py writes it)
NIGHTLY_GDM_CONF = "/etc/gdm3/custom.conf"                           # read only
NIGHTLY_AUTOLOGIN_USER = "__USER__"
NIGHTLY_REBOOT = ["sudo", "-n", "/usr/local/sbin/helix-machine", "reboot"]
NIGHTLY_REBOOT_TIMEOUT_S = 20
NIGHTLY_GPU_REASON = "GPU driver mismatch"
# Helix's own resident GPU processes: this process, and render_daemon.py (it keeps its models
# loaded). A process is render_daemon when its argv[1] resolves to this exact file.
NIGHTLY_RESIDENT_GPU = (os.path.realpath(f"{BRAIN}/render_daemon.py"),)
# A browser's GPU process holds the GPU whenever the user is logged in: not work. Skipped only when its
# cmdline (Chrome joins it into one string) has this whole token AND its first token is a browser.
NIGHTLY_IGNORED_GPU_ARGS = ("--type=gpu-process",)
NIGHTLY_BROWSER_BINARIES = ("chrome", "brave", "chromium", "chromium-browser")
from datetime import time as _dtime
_NR_FROM, _NR_GIVE_UP, _NR_UNTIL = _dtime(4, 0), _dtime(6, 59), _dtime(6, 59, 59)
_NR_WINDOW_TEXT = "04:00–06:59"
_NR_NOTE_QUESTION = "(Helix — the nightly restart reporting)"
_NR_OUTCOMES = ("ok", "rolled_back", "down", "skipped")
_NR_LAUNCHED = set()             # local dates this process launched on (the ledger is the record)
_nr_lock = threading.RLock()
try:
    with open(os.path.abspath(__file__), "rb") as _fh: _BOOT_APP_SHA = hashlib.sha256(_fh.read()).hexdigest()
except Exception:
    _BOOT_APP_SHA = None


def _nr_in_window(t):
    return _NR_FROM <= t.time() <= _NR_UNTIL


def _nr_read(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for l in fh:
                try: out.append(json.loads(l))
                except ValueError: pass
    except OSError:
        pass
    return out


def _nr_append(path, rec, t):
    """One chained line, ATTEMPTS-style: ts (UTC Z), local, seq, prev = sha256 of the previous
    line's text (`genesis` first). flock on the ledger: nightly_restart.sh appends too."""
    import fcntl
    from datetime import timezone
    with _nr_lock:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with open(path, "rb") as fh: old = fh.read().decode("utf-8", "replace").splitlines()
            rec = dict(rec)
            rec.setdefault("local", t.isoformat(timespec="seconds"))
            rec.update(ts=t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), seq=len(old) + 1,
                       prev=hashlib.sha256(old[-1].encode("utf-8")).hexdigest() if old else "genesis")
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally: os.close(fd)
    return rec


def _nr_ran_today(path, t):
    day = t.date().isoformat()
    return t.date() in _NR_LAUNCHED or any(
        r.get("event") in ("trigger", "start") and str(r.get("local", ""))[:10] == day
        for r in _nr_read(path))


def _nr_gpu_pids():
    r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0: raise RuntimeError(f"exit {r.returncode}")
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def _nr_argv(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as fh:
        argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    return argv, os.readlink(f"/proc/{pid}/cwd")


def _nr_gpu_others():
    """GPU compute processes that are not Helix's own resident ones. nvidia-smi failing → busy."""
    try: pids = _nr_gpu_pids()
    except Exception as e: return [f"GPU unknown (nvidia-smi failed: {type(e).__name__})"]
    out = []
    for pid in pids:
        if pid == os.getpid(): continue
        try:
            argv, cwd = _nr_argv(pid)
            if len(argv) > 1 and os.path.realpath(os.path.join(cwd, argv[1])) in NIGHTLY_RESIDENT_GPU:
                continue
            tokens = " ".join(argv).split()
            if (tokens and os.path.basename(tokens[0]) in NIGHTLY_BROWSER_BINARIES
                    and any(t in NIGHTLY_IGNORED_GPU_ARGS for t in tokens)):
                continue
        except Exception:
            pass                                 # unreadable: not proven resident
        out.append(f"GPU pid {pid}")
    return out


def _nr_proc_started(pid):
    """Epoch seconds the process started, from /proc (field 22 is ticks after boot)."""
    with open(f"/proc/{pid}/stat") as fh: rest = fh.read().rsplit(")", 1)[1].split()
    with open("/proc/stat") as fh:
        btime = next(int(l.split()[1]) for l in fh if l.startswith("btime "))
    return btime + int(rest[19]) / os.sysconf("SC_CLK_TCK")


def _nr_live_attempts(path=None):
    """Logged attempts whose pid is still running. A pid that started more than 60 s after its
    attempt line is a reused pid, not the attempt."""
    from datetime import datetime
    out, seen = [], set()
    for r in _nr_read(path or NIGHTLY_ATTEMPTS):
        pid = r.get("pid")
        if not isinstance(pid, int) or pid in seen: continue
        if not os.path.exists(f"/proc/{pid}"): continue
        try:
            logged = datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00")).timestamp()
            if _nr_proc_started(pid) > logged + 60: continue
        except Exception:
            pass                                 # cannot tell: count it as running
        seen.add(pid)
        out.append(f"attempt pid {pid} ({r.get('scope')})")
    return out


def _nightly_restart_busy():
    """What is busy, by name; [] when idle."""
    busy = [l.name.upper() for l in (CHAT, WORK, SCHED) if l.lock.locked()]
    pid, _unit = _builder_active()
    if pid: busy.append(f"builder pid {pid}")
    if _queue_live(_queue_read()): busy.append("build queue")
    return busy + _nr_live_attempts() + _nr_gpu_others()


def _nr_post_note(text):
    """One proactive bubble that also lands in memory/sessions/log.md."""
    buf = TurnBuf(uuid.uuid4().hex[:12], _NR_NOTE_QUESTION, "odin")
    buf.emit("final", {"text": text})
    _log_turn(_NR_NOTE_QUESTION, text)
    _register_turn(buf)
    buf.finish("done")
    _announce(buf)


def _nr_note_text(recs):
    last = recs[-1]
    ev = last.get("event")
    if ev == "skipped":
        return f"Nightly restart skipped: {last.get('reason')}"
    start = next((r for r in reversed(recs[:-1]) if r.get("event") == "start"), last)
    hhmm = str(start.get("local", ""))[11:16] or "??:??"
    if ev == "ok":
        trig = next((r for r in reversed(recs) if r.get("event") == "trigger"
                     and r.get("seq", 0) < start.get("seq", 0)), {})
        changed = ("changed since last start" if trig.get("running_sha") != start.get("app_sha")
                   else "unchanged")
        return f"🔁 Nightly restart {hhmm}: healthy in {last.get('seconds')} s · app.py {changed}"
    if ev == "rolled_back":
        return (f"🔴 Nightly restart {hhmm}: the new app.py did not come up healthy — rolled back "
                f"to last known good (bad copy kept as {last.get('failed_copy')})")
    if ev == "down":
        return (f"🔴 Nightly restart {hhmm}: Helix did not come back ({last.get('why')}). "
                "See ~/Desktop/HELIX_DID_NOT_COME_BACK.txt")
    return None


def _nightly_restart_announce(ledger=None, sleep=None, wait_s=300):
    """If the ledger's last line is an outcome not yet announced, post ONE note and mark it.
    Called at boot, and right after a skip. At boot the script may still be health-checking this
    very process, so a sequence in progress (last line trigger/start) is waited for."""
    from datetime import datetime
    led = ledger or RESTARTS_LEDGER
    sleep = sleep or time.sleep
    if not led: return None
    recs, waited = _nr_read(led), 0
    while recs and recs[-1].get("event") in ("trigger", "start") and waited < wait_s:
        sleep(5); waited += 5
        recs = _nr_read(led)
    if not recs or recs[-1].get("event") == "announced": return None
    with _nr_lock:
        recs = _nr_read(led)
        if not recs or recs[-1].get("event") == "announced": return None
        last = recs[-1]
        if last.get("event") in ("trigger", "start"):
            hhmm = str(last.get("local", ""))[11:16] or "??:??"
            text = (f"🔴 Nightly restart {hhmm}: started but recorded no outcome after {waited} s "
                    "(check RESTARTS.jsonl and odin.log)")
        else:
            text = _nr_note_text(recs)
        if not text: return None
        try: _nr_post_note(text)
        except Exception as e: print(f"HELIX: [restart] could not post the note ({e})", flush=True)
        _nr_append(led, {"event": "announced", "of": last.get("seq")}, datetime.now())
        print(f"HELIX: [restart] {text}", flush=True)
        return text


def _nr_skip(led, reason, t):
    _nr_append(led, {"event": "skipped", "reason": reason}, t)
    try: _nightly_restart_announce(led, sleep=lambda s: None)
    except Exception as e: print(f"HELIX: [restart] could not announce the skip ({e})", flush=True)
    return f"skipped: {reason}"


def _nightly_restart_launch():
    """nightly_restart.sh in its OWN session, like _do_restart: start_helix.sh pkills this
    process, and the script has to outlive it. The tests' override variables are stripped."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HELIX_NR_TEST")}
    env["HELIX_NO_BROWSER"] = "1"
    subprocess.Popen(["bash", NIGHTLY_RESTART_SCRIPT], env=env, start_new_session=True,
                     close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _nr_machine_today(t, path):
    """The LAST line of MACHINE.jsonl, if it is from t's local date; else None."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        rec = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("date") == t.date().isoformat() else None


def _nr_autologin(path):
    """True only when [daemon] has AutomaticLoginEnable=true AND AutomaticLogin=__USER__."""
    section, kv = None, {}
    try:
        with open(path, encoding="utf-8") as fh: lines = fh.read().splitlines()
    except OSError:
        return False
    for l in (x.strip() for x in lines):
        if not l or l.startswith(("#", ";")): continue
        if l.startswith("[") and l.endswith("]"):
            section = l[1:-1].strip()
        elif section == "daemon" and "=" in l:
            k, v = l.split("=", 1)
            kv[k.strip()] = v.strip()
    return (kv.get("AutomaticLoginEnable", "").lower() == "true"
            and kv.get("AutomaticLogin") == NIGHTLY_AUTOLOGIN_USER)


def _nr_reboot(led, reason, t, run):
    """Ask helix-machine to reboot. True when it accepted; otherwise one skip line with its exit
    code, and False (the caller falls through to the normal restart)."""
    _nr_append(led, {"event": "reboot", "reason": reason}, t)
    try:
        r = run(NIGHTLY_REBOOT, capture_output=True, text=True, timeout=NIGHTLY_REBOOT_TIMEOUT_S)
        code, why = r.returncode, ((r.stderr or r.stdout or "").strip().splitlines() or [""])[0]
    except subprocess.TimeoutExpired:
        code, why = None, f"timed out after {NIGHTLY_REBOOT_TIMEOUT_S} s"
    except OSError as e:
        code, why = None, f"could not run: {e.strerror}"
    if code == 0:
        _NR_LAUNCHED.add(t.date())
        print(f"HELIX: [restart] night reboot requested ({reason})", flush=True)
        return True
    _nr_append(led, {"event": "skipped", "exit": code,
                     "reason": f"reboot not done: helix-machine exit {code} ({why})"}, t)
    return False


def _nightly_restart_try(now, sleep, run=None, machine=None, gdm=None):
    """The nightly is done. Restart if in the window, not yet today, and idle. ORDER 67: reboot
    instead when today's machine line says one is due and the machine logs in by itself."""
    from datetime import datetime
    led = RESTARTS_LEDGER
    if not led: return "off"
    run = run or subprocess.run
    with _nr_lock:
        t = now()
        if not _nr_in_window(t):
            return _nr_skip(led, f"outside {_NR_WINDOW_TEXT} ({t:%H:%M})", t)
        if _nr_ran_today(led, t):
            return _nr_skip(led, "already ran today", t)
        today = _nr_machine_today(t, machine or NIGHTLY_MACHINE)
        due = bool(today and today.get("reboot_due"))
        reason = str(today.get("reboot_reason") or "reboot due") if due else None
        auto = due and _nr_autologin(gdm or NIGHTLY_GDM_CONF)
        tried = False
        if due and auto and NIGHTLY_GPU_REASON in reason:
            # a dead GPU makes the busy check below say busy until 06:59: reboot before it
            tried = True
            if _nr_reboot(led, reason, t, run): return "reboot"
        while True:
            busy = _nightly_restart_busy()
            if not busy: break
            if t.time() >= _NR_GIVE_UP or not _nr_in_window(t):
                return _nr_skip(led, f"busy ({', '.join(busy)})", t)
            left = (datetime.combine(t.date(), _NR_GIVE_UP) - t).total_seconds()
            sleep(max(1, min(NIGHTLY_RESTART_RECHECK_S, left)))
            t = now()
        note = {}
        if due and auto and not tried:
            if _nr_reboot(led, reason, t, run): return "reboot"
        elif due and not auto:
            note = {"machine": "reboot due, not done: no automatic login", "reboot_reason": reason}
        _nr_append(led, {"event": "trigger", "reason": "nightly", "running_sha": _BOOT_APP_SHA,
                         "pid": os.getpid(), **note}, t)
        _NR_LAUNCHED.add(t.date())
        try:
            _nightly_restart_launch()
        except Exception as e:
            return _nr_skip(led, f"could not start nightly_restart.sh ({type(e).__name__}: {e})", t)
        print("HELIX: [restart] nightly restart started", flush=True)
        return "started"


def _nightly_restart_after(buf, now=None, sleep=None):
    """Wait for the nightly's job to finish and be logged and pushed, then the grace, then try."""
    from datetime import datetime
    now = now or datetime.now
    sleep = sleep or time.sleep
    led = RESTARTS_LEDGER
    if not led: return "off"
    t = now()
    if not _nr_in_window(t):
        return _nr_skip(led, f"outside {_NR_WINDOW_TEXT} ({t:%H:%M})", t)
    while not (getattr(buf, "status", "running") != "running" and getattr(buf, "announced", False)):
        if now().time() >= _NR_GIVE_UP:
            return _nr_skip(led, "the nightly had not finished by 06:59", now())
        sleep(10)
    sleep(NIGHTLY_RESTART_GRACE_S)
    return _nightly_restart_try(now, sleep)


def _nightly_restart_arm(buf):
    """Called by scheduler_tick only. Off when the ledger is (app imported, e.g. by tests)."""
    if not RESTARTS_LEDGER: return None
    def run():
        try: _nightly_restart_after(buf)
        except Exception as e: print(f"HELIX: [restart] nightly restart failed ({e})", flush=True)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


@app.route("/model", methods=["GET"])
def model_get():
    """What is answering, what Alice runs on, and what the last turn proved about fable."""
    return jsonify(
        odin=ODIN_MODEL,
        odin_label=MODEL_CHOICES.get(ODIN_MODEL, {}).get("label", ODIN_MODEL),
        choices=[dict(id=k, **v) for k, v in MODEL_CHOICES.items()],
        alice=ALICE_MODEL,                       # info only: a Gemini seat, not switchable here
        fable_capped_at=FABLE_CAPPED_AT,
        env_pinned=bool(_ENV_MODEL),             # HELIX_MODEL at launch outranks the selector
        ultracode_armed=_ULTRA_ARMED,
    )


def _lane_busy():
    return CHAT.lock.locked() or WORK.lock.locked() or SCHED.lock.locked()


@app.route("/model", methods=["POST"])
def model_set():
    global ODIN_MODEL
    m = (request.json or {}).get("model", "")
    if m not in MODEL_CHOICES:
        return jsonify(ok=False, error=f"unknown model {m!r}"), 400
    if _ENV_MODEL:
        return jsonify(ok=False, error=f"HELIX_MODEL={_ENV_MODEL} was set at launch and wins; "
                                       "restart without it to use the selector"), 409
    # Refuse mid-turn: switching drops the lane, which would kill an answer in flight.
    if _lane_busy():
        return jsonify(ok=False, error="a turn is in flight — press STOP first"), 409
    if m != ODIN_MODEL:
        ODIN_MODEL = m
        d = _ui_load(); d["odin_model"] = m; _ui_save(d)
        _disarm_ultracode()                      # an arm must not survive a change of seat
        print(f"HELIX: odin model -> {m}", flush=True)
    return jsonify(ok=True, odin=ODIN_MODEL, ultracode_armed=_ULTRA_ARMED)


def _disarm_ultracode():
    global _ULTRA_ARMED
    _ULTRA_ARMED = False


HELIX_VOICES = ("odin", "alice")


@app.route("/front/voice", methods=["GET"])
def front_voice_get():
    return jsonify(voice=_ui_get("helix_voice", "odin"), choices=list(HELIX_VOICES))


@app.route("/front/voice", methods=["POST"])
def front_voice_set():
    v = ((request.json or {}).get("voice") or "").lower()
    if v not in HELIX_VOICES:
        return jsonify(ok=False, error=f"voice must be one of {HELIX_VOICES}"), 400
    _ui_set("helix_voice", v)
    return jsonify(ok=True, voice=v)


@app.route("/ultracode", methods=["POST"])
def ultracode_set():
    """Arm fan-out for exactly ONE upcoming turn, or stand it back down.

    This is the permission gate the 2026-09-07/08 incident lacked: Odin cannot reach the
    Workflow tool unless the user pressed this first, and the turn that uses it spends it.
    """
    global _ULTRA_ARMED
    if _lane_busy():
        return jsonify(ok=False, error="a turn is in flight — press STOP first"), 409
    want = bool((request.json or {}).get("armed", True))
    _ULTRA_ARMED = want
    print(f"HELIX: ultracode {'ARMED for the next turn' if want else 'stood down'}", flush=True)
    return jsonify(ok=True, ultracode_armed=_ULTRA_ARMED)


@app.route("/schedules", methods=["GET"])
def schedules_list():
    return jsonify(schedules=scheduler.load(SCHEDULES))


@app.route("/schedules", methods=["POST"])
def schedules_create():
    body = request.json or {}
    sched = {"name": (body.get("name") or "").strip(),
             "prompt": (body.get("prompt") or "").strip(),
             "every_minutes": body.get("every_minutes"),
             "at": body.get("at")}
    ok, err = scheduler.validate(sched)
    if not ok: return jsonify(ok=False, error=err), 400
    sched.update(id=uuid.uuid4().hex[:12], enabled=True, last_run=None, last_turn_id=None)
    items = scheduler.load(SCHEDULES); items.append(sched)
    scheduler.save(SCHEDULES, items)
    return jsonify(ok=True, id=sched["id"])


@app.route("/schedules/<sid>", methods=["DELETE"])
def schedules_delete(sid):
    items = [s for s in scheduler.load(SCHEDULES) if s.get("id") != sid]
    scheduler.save(SCHEDULES, items)
    return jsonify(ok=True)


@app.route("/schedules/<sid>/toggle", methods=["POST"])
def schedules_toggle(sid):
    items = scheduler.load(SCHEDULES)
    for s in items:
        if s.get("id") == sid: s["enabled"] = not s.get("enabled", True)
    scheduler.save(SCHEDULES, items)
    return jsonify(ok=True)


@app.route("/schedules/<sid>/run", methods=["POST"])
def schedules_run_now(sid):
    """Run one on demand — the same path the ticker uses, so testing a schedule tests the
    real thing rather than an approximation of it."""
    from datetime import datetime
    items = scheduler.load(SCHEDULES)
    for s in items:
        if s.get("id") == sid:
            buf = start_job(s["prompt"], lane=SCHED)
            s["last_run"] = datetime.now().isoformat(timespec="seconds")
            s["last_turn_id"] = buf.id
            scheduler.save(SCHEDULES, items)
            return jsonify(ok=True, id=buf.id)
    return jsonify(ok=False, error="no such schedule"), 404


@app.route("/job", methods=["POST"])
def job():
    """Hand Odin a long task without occupying the conversation.

    Returns the turn id straight away; poll /turn_state?id=… or just wait for the bubble the
    announce feed raises when it finishes.
    """
    text = ((request.json or {}).get("text", "")).strip()
    if not text: return jsonify(ok=False, error="empty"), 400
    buf = start_job(text)
    return jsonify(ok=True, id=buf.id)


@app.route("/interrupt", methods=["POST"])
def interrupt():
    if CHAT.client is None: return jsonify(ok=False, error="no active session")
    CHAT.interrupting = True      # marks the coming turn-failure as deliberate, not a broken transport
    try:
        asyncio.run_coroutine_threadsafe(CHAT.client.interrupt(), _loop).result(timeout=6)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# ============ memory constellation: expose facts grouped into clusters ============
MEMORY_CATS = [   # (label, colour, [fact filenames]) for the memory constellation. Edit to taste.
    ("HOW HELIX WORKS", "#22d3ee", ["001_how_helix_remembers.md", "003_example_superseded.md", "004_example_correction.md"]),
    ("RULES",           "#a855f7", ["002_show_before_you_build.md"]),
]
def _fact_title(f):
    try:
        for line in open(f"{FACTS}/{f}"):
            s=line.strip()
            if s.startswith("#"): return s.lstrip("# ").strip()[:74]
            if s and not s.startswith("---"): return s[:74]
    except Exception: pass
    return f.replace(".md","")

_cpu_prev = [0, 0]
# ORDER 66: one sampler for every caller. The desktop polls at 1 s and the phone at 5 s; without
# this each caller's delta covered whatever was left since the OTHER caller's poll.
_cpu_cache = {"t": 0.0, "v": 0}
_cpu_lock = threading.Lock()
@app.route("/stat")                               # live telemetry for the HUD gauges (reads /proc, no deps)
def stat():
    global _cpu_prev
    now = time.time()
    with _cpu_lock:
        if _cpu_cache["t"] and now - _cpu_cache["t"] < 0.8: cpu = _cpu_cache["v"]
        else:
            cpu = 0
            try:
                vals = [int(x) for x in open("/proc/stat").readline().split()[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0); total = sum(vals)
                dt = total - _cpu_prev[0]; di = idle - _cpu_prev[1]; _cpu_prev = [total, idle]
                if dt > 0: cpu = max(0, min(100, int(100 * (1 - di / dt))))
            except Exception: pass
            _cpu_cache["t"], _cpu_cache["v"] = now, cpu
    mu = mt = 0
    try:
        mi = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1); mi[k] = int(v.strip().split()[0])
        mt = mi.get("MemTotal", 0) // 1024
        mu = mt - (mi.get("MemAvailable", mi.get("MemFree", 0)) // 1024)
    except Exception: pass
    # The phone shows Odin's state on the orb, so it needs the same two facts the front
    # desk uses: is he working, and is anything waiting on his notepad.
    try:    _notes = len(frontdesk.load_pad(NOTEPAD)["notes"])
    except Exception: _notes = 0
    return jsonify(cpu=cpu, mem_used=mu, mem_total=mt,
                   work_busy=WORK.lock.locked(), notes=_notes,
                   # proactive background-report feed (same contract as /metrics): the phone
                   # polls this at 5 s and fetches unseen note_ids via /turn_state
                   note_seq=_note_seq, note_ids=list(_note_ids))

# ORDER 65 (item 28): a fact may declare "Category: <name>" in its first 15 lines; the names and
# colours come from workspace/categories/CATEGORIES.txt only. The module is loaded inside the
# routes, from its path, in a try: if it or the list ever broke, /memory and /blueprint fall back
# to exactly the filename guesses below, never a 500.
FACT_CATEGORIES_PY = f"{BRAIN}/workspace/categories/categories.py"
# ORDER 73b: a fact with no valid declared line takes its category from this sidecar (written only by
# `categories.py assign`). A declared line still wins; an absent file assigns nothing.
FACT_ASSIGNED = f"{BRAIN}/workspace/categories/ASSIGNED.csv"

def _fact_categories():
    """(module, [Category]) or (None, None) if anything about loading them fails."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("helix_fact_categories", FACT_CATEGORIES_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, mod.load()
    except Exception:
        return None, None

def _fact_assigned(mod, cats):
    """{file: [names]} from ASSIGNED.csv, or {} if it is absent or anything fails."""
    if mod is None: return {}
    try: return mod.load_assigned(FACT_ASSIGNED, cats)
    except Exception: return {}

def _fact_declared(mod, cats, f, text=None, assigned=None):
    """(primary category, "declared" | "assigned") or (None, None). Never raises."""
    if mod is None: return None, None
    try:
        if text is None:
            with open(f"{FACTS}/{f}", "r", errors="replace") as fh: text = "".join(fh.readline() for _ in range(15))
        names, src = mod.category_of(f, text, cats, assigned or {})
        return (names[0], src) if names else (None, None)
    except Exception:
        return None, None

@app.route("/memory")
def memory_map():
    present=set(f for f in os.listdir(FACTS) if f.endswith(".md"))
    mod, fcats = _fact_categories()
    assigned = _fact_assigned(mod, fcats)
    declared = {}
    for f in sorted(present):
        d = _fact_declared(mod, fcats, f, assigned=assigned)
        if d[0]: declared[f] = d
    used=set(); cats=[]
    for c in (fcats or []):                       # declared or assigned first, in the list's order
        nodes=[{"file":f,"title":_fact_title(f),"cat_source":declared[f][1]} for f in sorted(declared) if declared[f][0]==c.name]
        for n in nodes: used.add(n["file"])
        if nodes: cats.append({"name":c.name,"color":c.colour,"files":nodes})
    src = {} if mod is None else {"cat_source":"filename-guess"}   # module broken → today's output exactly
    for name,color,files in MEMORY_CATS:
        nodes=[{"file":f,"title":_fact_title(f),**src} for f in files if f in present and f not in used]
        for n in nodes: used.add(n["file"])
        if nodes: cats.append({"name":name,"color":color,"files":nodes})
    misc=[{"file":f,"title":_fact_title(f),**src} for f in sorted(present-used)]
    if misc: cats.append({"name":"MISC","color":"#9aa4b2","files":misc})
    return jsonify(cats=cats)

# ============ BLUEPRINT ENGINE: the body of work, derived from what the facts actually say ============
# Nothing here is guessed by word-frequency. Every field traces to something explicit in the corpus:
#   verdict  ← the verdict word YOU put in the filename (KILL/PASS/PRIOR_ART/falsified/walkback…),
#              else the file's own "**Status:** …" line. Body-keyword guessing is the last resort and
#              is reported as low-confidence so the UI can say so rather than pretend.
#   date     ← the real date written in the file (100/106 carry one). This drives the timeline.
#   origin   ← box1 (recorded here) vs box2_ prefix (imported from the other machine).
#   edges    ← facts that literally cite another fact's filename. A real citation graph, not name-matching.
BP_PROJECTS = [   # (label, color, filename keywords) - first match wins, so order matters. Edit to taste.
    ("HELIX",    "#64748b", ["helix", "order", "rule", "preference"]),
    ("RESEARCH", "#eab308", ["research", "study", "test", "experiment"]),
    ("PROJECTS", "#4be3a0", ["project", "build"]),
    ("LIFE",     "#c9a5ff", ["user", "family", "home"]),
]
_BP_COLOR = {n: c for n, c, _ in BP_PROJECTS}
# Verdict vocabulary, taken from the words actually used across the corpus. Order = precedence:
# a file marked both "falsified" and "confirmed" is a KILL, because that's the headline finding.
BP_VERDICTS = [
    ("kill",      "#ff6b81", ["kill", "killed", "falsif", "disproven", "dead", "didnt_work",
                              "did_not_work", "exhausted", "lost", "collapsed", "refuted", "null"]),
    ("prior_art", "#a855f7", ["prior_art", "priorart"]),
    ("correction","#f0883e", ["correction", "corrects", "walkback", "suspect", "remapped",
                              "reframed", "contradicts", "stale"]),
    ("pass",      "#4be3a0", ["pass", "confirmed", "holds", "proven", "validated", "verified",
                              "floorholds", "survives"]),
]
BP_VLABEL = {"kill": "KILLED", "pass": "HOLDS", "prior_art": "PRIOR ART",
             "correction": "CORRECTED", "open": "OPEN"}

def _bp_project(fn):
    low = fn.lower()
    for name, color, kws in BP_PROJECTS:
        if any(k in low for k in kws): return name, color
    return "GENERAL", "#9aa4b2"

def _bp_verdict_from(s):
    """Match the verdict vocabulary against a string. Returns None if nothing matches."""
    low = s.lower()
    for name, color, kws in BP_VERDICTS:
        if any(k in low for k in kws): return name
    return None

def _bp_status_line(text):
    """The file's own status sentence, if it wrote one. 88/106 do."""
    m = re.search(r'^\s*\**\s*(?:🔴\s*)?status[^\n]{0,200}', text, re.I | re.M)
    if not m: return ""
    return " ".join(m.group(0).replace("*", " ").split())[:180]

def _bp_date(text):
    """The fact's own date. Prefer one stated in the opening lines (the title usually carries it),
    else the latest date mentioned anywhere."""
    head = "\n".join(text.split("\n")[:8])
    m = re.search(r'20\d{2}-[01]\d-[0-3]\d', head)
    if m: return m.group(0)
    all_d = re.findall(r'20\d{2}-[01]\d-[0-3]\d', text)
    return max(all_d) if all_d else ""

def _bp_tag(text):
    m = re.search(r'<!--\s*blueprint:\s*(.*?)-->', text, re.S)
    out = {}
    if m:
        for kv in m.group(1).split():
            if "=" in kv: k, v = kv.split("=", 1); out[k.strip()] = v.strip()
    return out

_BP_CITE = re.compile(r'\b((?:box2_)?\d{2,}_[A-Za-z0-9_\-]+\.md)')

@app.route("/blueprint")
def blueprint():
    files = sorted(f for f in os.listdir(FACTS) if f.endswith(".md"))
    mod, fcats = _fact_categories()
    fcolour = {c.name: c.colour for c in (fcats or [])}
    assigned = _fact_assigned(mod, fcats)
    nodes, byfile = [], {}
    for f in files:
        try: text = open(f"{FACTS}/{f}").read()
        except Exception: text = ""
        tag  = _bp_tag(text)
        low  = f.lower()
        decl, dsrc = _fact_declared(mod, fcats, f, text, assigned)
        # project: the fact's declared (or assigned) category → its blueprint tag → the filename keywords
        if decl:
            proj, color, csrc = decl.upper(), fcolour.get(decl, "#9aa4b2"), dsrc
        elif tag.get("project"):
            proj, color, csrc = tag["project"].upper(), _BP_COLOR.get(tag["project"].upper(), "#9aa4b2"), "tag"
        else:
            (proj, color), csrc = _bp_project(f), "filename-guess"
        # verdict: explicit tag → your filename word → the file's own status line → body (low confidence)
        status_line = _bp_status_line(text)
        verdict, vsrc = tag.get("status"), "tag"
        if not verdict:
            verdict, vsrc = _bp_verdict_from(low), "filename"
        if not verdict and status_line:
            verdict, vsrc = _bp_verdict_from(status_line), "status"
        if not verdict:
            verdict, vsrc = _bp_verdict_from(" ".join(text.lower().split()[:120])), "body"
        if not verdict:
            verdict, vsrc = "open", "default"
        m = re.match(r'^(box2_)?(\d+)_', f)
        body = " ".join(text.replace("#", " ").split())
        n = {"file": f, "title": _fact_title(f), "project": proj, "color": color,
             "verdict": verdict, "vlabel": BP_VLABEL.get(verdict, verdict.upper()),
             "vsrc": vsrc, "confident": vsrc != "body",
             "origin": "box2" if f.startswith("box2_") else "box1",
             "seq": int(m.group(2)) if m else -1,
             "date": _bp_date(text), "status_line": status_line,
             "snip": body[:420], "words": len(body.split()),
             "cites": [], "cited_by": []}
        if mod is not None: n["cat_source"] = csrc      # module broken → today's output exactly
        nodes.append(n); byfile[f] = n
    # edges = one fact citing another's filename in its text. Real references only.
    edges, seen = [], set()
    for n in nodes:
        try: text = open(f"{FACTS}/{n['file']}").read()
        except Exception: continue
        for ref in set(_BP_CITE.findall(text)):
            if ref == n["file"] or ref not in byfile: continue
            key = (ref, n["file"])
            if key in seen: continue
            seen.add(key)
            edges.append({"from": ref, "to": n["file"]})       # cited → citer
            n["cites"].append(ref); byfile[ref]["cited_by"].append(n["file"])
    # undated facts still need a place on the timeline: give them the corpus' earliest date
    dated = sorted({n["date"] for n in nodes if n["date"]})
    for n in nodes:
        if not n["date"]: n["date"] = dated[0] if dated else ""
    projs = {}
    for n in nodes:
        pr = projs.setdefault(n["project"], {"name": n["project"], "color": n["color"], "count": 0})
        pr["count"] += 1
    counts = {"total": len(nodes), "edges": len(edges),
              "unsure": sum(1 for n in nodes if not n["confident"])}
    for v, _c, _k in BP_VERDICTS: counts[v] = sum(1 for n in nodes if n["verdict"] == v)
    counts["open"] = sum(1 for n in nodes if n["verdict"] == "open")
    return jsonify(nodes=nodes, edges=edges, counts=counts, days=dated,
                   projects=sorted(projs.values(), key=lambda p: -p["count"]))

# ============ v4 MTRX: machine metrics, a streaming council, and readable facts ============
_T_BOOT = time.time()
_proc_cpu_prev = {"t": 0.0, "c": 0.0, "v": None}   # "v": the cached reading (ORDER 66, 0.8 s rule)
_proc_cpu_lock = threading.Lock()
_metrics_cache = {"facts": (0.0, None), "tts": (0.0, None)}
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# ORDER 66: real per-seat CPU for the ODIN / ALICE meters. A seat is a set of app.py's descendant
# processes: an `agy` and everything under it is Alice, every other descendant (the claude CLI and
# its tool shells) is Odin. app.py's own threads are not a seat — /metrics' `cpu` already has them.
_seat_prev = {"t": 0.0, "ticks": {}, "val": (None, None)}
_seat_lock = threading.Lock()

def _proc_snapshot():
    """{(pid, starttime): (ppid, utime+stime ticks, argv[0] basename)} for every process in /proc.
    (pid, starttime) is the key so a recycled pid is a different process. Raises if /proc itself
    cannot be listed; a pid that exits mid-walk is simply skipped."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit(): continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                f = fh.read().rsplit(") ", 1)[1].split()      # comm may contain ')'
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv0 = fh.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            # after comm: state(0) ppid(1) … utime(11) stime(12) … starttime(19)
            out[(int(d), int(f[19]))] = (int(f[1]), int(f[11]) + int(f[12]), os.path.basename(argv0))
        except Exception: continue
    return out

def _seat_split(snap, root):
    """{key: 'odin'|'alice'} for every descendant of pid `root` in `snap` (root itself excluded)."""
    kids = {}
    for k, (ppid, _, _) in snap.items(): kids.setdefault(ppid, []).append(k)
    seat, stack = {}, [(k, False) for k in kids.get(root, [])]
    while stack:
        k, alice = stack.pop()
        if k in seat: continue
        alice = alice or snap[k][2] == "agy"
        seat[k] = "alice" if alice else "odin"
        stack.extend((c, alice) for c in kids.get(k[0], []))
    return seat

def _seat_cpu(root=None, now=None):
    """(odin, alice): % of ONE core each seat used since the previous sample, 1 decimal, or None
    when there is no reading (first sample, or any failure). Cached for 0.8 s like /stat's cpu.
    Only pids present in BOTH samples count: a process that starts and ends between two samples
    is not seen at all, and one that starts or ends inside the window loses that part of its time."""
    now = time.time() if now is None else now
    with _seat_lock:
        if _seat_prev["t"] and now - _seat_prev["t"] < 0.8: return _seat_prev["val"]
        try:
            snap = _proc_snapshot()
            cur = {k: (s, snap[k][1]) for k, s in _seat_split(snap, os.getpid() if root is None else root).items()}
            val, pt, prev = (None, None), _seat_prev["t"], _seat_prev["ticks"]
            if pt and now > pt:
                tot = {"odin": 0, "alice": 0}
                for k, (s, ticks) in cur.items():
                    if k in prev: tot[s] += max(0, ticks - prev[k][1])
                val = tuple(round(100.0 * tot[s] / _CLK / (now - pt), 1) for s in ("odin", "alice"))
        except Exception:
            cur, val = {}, (None, None)
        _seat_prev.update(t=now, ticks=cur, val=val)
        return val

def _tts_dir_state():
    """(file count, bytes) for tts/. Cached — a 1 Hz poll must not become a 1 Hz stat storm."""
    now = time.time(); ts, val = _metrics_cache["tts"]
    if val is not None and now - ts < 5: return val
    n = sz = 0
    try:
        for e in os.scandir(TTSDIR):
            if e.name.endswith(".wav"):
                n += 1
                try: sz += e.stat().st_size
                except Exception: pass
    except Exception: pass
    _metrics_cache["tts"] = (now, (n, sz)); return (n, sz)

def _fact_count():
    now = time.time(); ts, val = _metrics_cache["facts"]
    if val is not None and now - ts < 30: return val       # spec: cache for 30 s, no walk per poll
    try: val = sum(1 for f in os.listdir(FACTS) if f.endswith(".md"))
    except Exception: val = None
    _metrics_cache["facts"] = (now, val); return val

@app.route("/metrics")
def metrics():
    """Cheap 1 Hz instrument feed. No subprocesses, no per-poll disk walk. Anything that cannot
    be measured comes back null so the surface can print '—' instead of inventing a zero."""
    now = time.time()
    cpu = rss = None
    with _proc_cpu_lock:                    # ORDER 66: one sample per 0.8 s for every caller
        if _proc_cpu_prev["t"] and now - _proc_cpu_prev["t"] < 0.8: cpu = _proc_cpu_prev["v"]
        else:
            try:
                f = open("/proc/self/stat").read().rsplit(") ", 1)[1].split()   # comm may contain ')'
                ticks = (int(f[11]) + int(f[12])) / float(_CLK)                 # utime + stime
                pt, pc = _proc_cpu_prev["t"], _proc_cpu_prev["c"]
                _proc_cpu_prev["t"], _proc_cpu_prev["c"] = now, ticks
                if pt and now > pt: cpu = round(max(0.0, 100.0 * (ticks - pc) / (now - pt)), 1)
            except Exception: pass
            _proc_cpu_prev["v"] = cpu
    odin_cpu, alice_cpu = _seat_cpu()
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"): rss = int(line.split()[1]) // 1024; break   # MB
    except Exception: pass
    tts_n, tts_b = _tts_dir_state()
    depth = 0
    try:
        if _tts_live is not None: depth = len(_tts_live.futs)
    except Exception: depth = None
    ok, note = _credentials_state()                 # reuse — do not re-read the credential file here
    m = re.search(r"valid for ([\d.]+)h", note)
    log_b = log_turns = None
    try:
        log_b = os.path.getsize(f"{SESS}/log.md")
        with open(f"{SESS}/log.md", "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            log_turns = f.read().decode("utf-8", "ignore").count("\n## ")
    except Exception: pass
    return jsonify(cpu=cpu, odin_cpu=odin_cpu, alice_cpu=alice_cpu,
                   rss_mb=rss, uptime_s=round(now - _T_BOOT, 1),
                   log_bytes=log_b, log_turns_tail=log_turns, log_keep=LOG_KEEP_TURNS,
                   tts_queue=depth, tts_files=tts_n, tts_bytes=tts_b,
                   facts=_fact_count(), signin_ok=ok,
                   signin_h=(float(m.group(1)) if m else None), signin_note=note,
                   turn_busy=CHAT.lock.locked(), work_busy=WORK.lock.locked(),
                   schedules=len(scheduler.load(SCHEDULES)),
                   studio_queued=studio_queued(),
                   # proactive background-report feed: the UI watches note_seq at 1 Hz and
                   # fetches unseen note_ids via /turn_state to render bubbles Odin initiated
                   note_seq=_note_seq, note_ids=list(_note_ids))

@app.route("/council_stream", methods=["POST"])
def council_stream():
    """The same bench as /council, but each seat reports the moment it lands instead of the whole
    rack arriving at once ~34 s later. /council is untouched — index.html still uses it."""
    q_text = (request.json or {}).get("text", "") or (HISTORY[-2][1] if len(HISTORY) >= 2 else "")
    p = context(q_text, for_council=True)
    seats = [("Gemini", "breadth", (lambda: gemini(p)))] + \
            [(m["name"], m["role"], (lambda mid=m["id"]: openrouter(mid, p))) for m in BENCH]
    def gen():
        yield _sse("roster", {"seats": [{"name": n, "role": r} for n, r, _ in seats]})
        t_all = time.time(); started = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut = {}
            for n, r, call in seats:
                fut[ex.submit(call)] = (n, r); started[n] = time.time()
                yield _sse("seat_start", {"seat": n, "role": r})
            for f in as_completed(fut):
                n, r = fut[f]
                try: text = f.result()
                except Exception as e: text = f"[{n}: {e}]"
                yield _sse("seat_done", {"seat": n, "role": r, "text": text,
                                         "ms": int((time.time() - started[n]) * 1000)})
        yield _sse("done", {"ms": int((time.time() - t_all) * 1000)})
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

@app.route("/fact/<name>")
def fact(name):
    """Read-only text of one memory fact, so clicking a name in the memory module opens the real
    file in the inspector. Basename-only and .md-only: nothing outside memory/facts is reachable."""
    name = os.path.basename(name)
    if not name.endswith(".md"): return jsonify(error="not a fact"), 400
    p = f"{FACTS}/{name}"
    if not os.path.isfile(p): return jsonify(error="no such fact"), 404
    try: return jsonify(file=name, text=open(p).read())
    except Exception as e: return jsonify(error=str(e)), 500

# ══════════════════════════════════════════════════════════════════════════════════════════════
# MOBILE — a second face on the same brain (same sessions, same memory, same turn lock).
# Reached over Tailscale only; app.py still binds 127.0.0.1 and nothing is exposed to the LAN.
# ══════════════════════════════════════════════════════════════════════════════════════════════

class _NullTTS:
    """Stand-in for _TTSPipeline when the client asked for text only. Same shape, no audio."""
    def feed(self, text): pass
    def drain(self, block=False): return iter(())
    def finish(self, fallback_text=""): return iter(())

MOBILE_TOKEN_FILE = f"{BRAIN}/memory/.mobile_token"
_mtok = None
def mobile_token():
    """Secondary gate. The PRIMARY boundary is Tailscale: only devices on your tailnet can reach
    this port at all. The token exists so a borrowed/lost phone, or another device you later add
    to the tailnet, is not automatically a shell on this machine."""
    global _mtok
    if _mtok is None:
        try:
            _mtok = open(MOBILE_TOKEN_FILE).read().strip()
        except Exception:
            _mtok = ""
        if not _mtok:
            import secrets
            _mtok = secrets.token_urlsafe(24)
            try:
                with open(MOBILE_TOKEN_FILE, "w") as f: f.write(_mtok)
                os.chmod(MOBILE_TOKEN_FILE, 0o600)
            except Exception: pass
    return _mtok

@app.before_request
def _mobile_gate():
    """A request that arrived through `tailscale serve` carries X-Forwarded-For; one typed into
    the desktop browser on this box does not. So the desktop UI is untouched and remote traffic
    must present the token.
    NOTE: the X-Forwarded-For assumption is verifiable at /whoami once Tailscale is live."""
    if not request.headers.get("X-Forwarded-For"): return None      # local desktop, unchanged
    if request.path == "/whoami": return None                        # so we can debug the gate itself
    # 🟢 PRIMARY remote identity (added 2026-09-02, the fact-100 candidate
    # hardening): `tailscale serve` injects Tailscale-User-Login after
    # authenticating the device cryptographically on the tailnet. It cannot be
    # forged from outside, and header spoofing from inside would require a
    # device already logged in as the user. This is what makes a bare link (a PDF,
    # an image) tappable from his phone without carrying the token in the URL.
    # Verified live at /whoami on 2026-09-01: the header arrives as expected.
    if request.headers.get("Tailscale-User-Login", "").lower() == os.environ.get("HELIX_TAILSCALE_LOGIN", "-").lower():
        return None
    tok = request.cookies.get("hxm") or request.args.get("k", "")   # fallback: the token
    if tok and tok == mobile_token(): return None
    return Response("forbidden", status=403, mimetype="text/plain")

@app.route("/render", methods=["POST"])
def render_image():
    """Helix's inbuilt image generator (2026-09-03). Proxies to the local
    render daemon (render_daemon.py on 127.0.0.1:7900) — local, private, free.
    Start the daemon with: ~/render_env/bin/python ~/helix/render_daemon.py"""
    import requests as _rq
    try:
        r = _rq.post("http://127.0.0.1:7900/gen", json=request.get_json(force=True),
                     timeout=600)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify(error=f"render daemon not running: {e}"), 503

@app.route("/whoami")
def whoami():
    """Empirical check on the gate: what does a request actually look like through the tunnel?"""
    return jsonify(remote_addr=request.remote_addr,
                   forwarded_for=request.headers.get("X-Forwarded-For"),
                   host=request.headers.get("Host"),
                   scheme=request.headers.get("X-Forwarded-Proto") or request.scheme,
                   tailscale_headers={k: v for k, v in request.headers.items()
                                      if k.lower().startswith(("tailscale", "x-forwarded"))},
                   gate_would_allow=bool(not request.headers.get("X-Forwarded-For")
                                         or (request.cookies.get("hxm") or request.args.get("k", "")) == mobile_token()))

@app.route("/m")
def mobile_page():
    """The phone face. ?k=<token> once; after that the cookie carries it."""
    k = request.args.get("k", "")
    resp = make_response(send_from_directory(app.static_folder, "mobile.html"))
    if k and k == mobile_token():
        resp.set_cookie("hxm", k, max_age=60*60*24*365, samesite="Lax", path="/")
    return resp

def decode_phone_audio(raw):
    """Decode audio RECORDED ON THE PHONE to float32 mono @16 kHz for Whisper.

    Android Chrome's MediaRecorder emits webm/opus; iOS Safari emits mp4/aac. PyAV handles both,
    which matters because there is no ffmpeg binary on this box. VERIFIED by round-tripping
    Kokoro speech through webm/opus and back out of Whisper, word for word."""
    import numpy as np, av
    container = av.open(io.BytesIO(raw))
    res = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for frame in container.decode(audio=0):
        for r in res.resample(frame): chunks.append(r.to_ndarray().reshape(-1))
    for r in (res.resample(None) or []): chunks.append(r.to_ndarray().reshape(-1))
    container.close()
    if not chunks: return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0

@app.route("/listen/phone", methods=["POST"])
def listen_phone():
    """Transcribe audio recorded ON THE PHONE.

    NOT the same as /listen/utterance, which shells out to `pw-record` and captures THIS PC's
    microphone — from a phone that would record an empty room. Transcription is local
    (faster-whisper), so the audio never leaves this machine."""
    f = request.files.get("audio")
    raw = f.read() if f is not None else request.get_data()
    if not raw or len(raw) < 1200: return jsonify(text="", error="too short")
    try:
        audio = decode_phone_audio(raw)
    except Exception as e:
        return jsonify(text="", error=f"decode failed: {e}")
    if len(audio) < 16000 * 0.25: return jsonify(text="", error="too short")
    try:
        return jsonify(text=_transcribe_array(audio), secs=round(len(audio) / 16000.0, 2))
    except Exception as e:
        return jsonify(text="", error=str(e))

# ── PHONE ATTACHMENTS ─────────────────────────────────────────────────────────────────────────
# the user, on the phone: "I'm really missing the attach file and camera functions here now."
# Design choice that matters: an upload does NOT get stuffed into the model's context. It LANDS
# IN MY WORKSPACE and I open it with my real tools — which is the whole architecture of this
# thing. A photographed document therefore becomes a file I can read, OCR, grep or archive,
# not a one-shot image that vanishes with the turn.
INBOX = f"{WORKSPACE}/inbox"
UPLOAD_MAX = 64 * 1024 * 1024          # per file; a phone photo is ~2-6 MB, a PDF rarely more
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

def _safe_name(name):
    """A filename from a phone is untrusted input. Strip every path component and every character
    that could escape the inbox, and never let the result be empty or hidden."""
    name = os.path.basename(name or "").strip()
    name = _UNSAFE.sub("_", name).lstrip(".")
    return (name or "upload")[:120]

@app.route("/upload", methods=["POST"])
def upload_from_phone():
    """Take files (or a camera photo) from the phone into ODIN's workspace inbox.

    Gated by the same @app.before_request token as every other remote route."""
    os.makedirs(INBOX, exist_ok=True)
    files = request.files.getlist("files") or ([request.files["file"]] if "file" in request.files else [])
    if not files: return jsonify(error="no files"), 400
    stamp, saved, errs = time.strftime("%Y%m%d-%H%M%S"), [], []
    for i, f in enumerate(files):
        try:
            raw = f.read()
            if not raw:                       errs.append(f"{f.filename}: empty"); continue
            if len(raw) > UPLOAD_MAX:         errs.append(f"{f.filename}: too big ({len(raw)//1048576} MB)"); continue
            nm   = _safe_name(f.filename)
            path = f"{INBOX}/{stamp}-{i}-{nm}" if len(files) > 1 else f"{INBOX}/{stamp}-{nm}"
            with open(path, "wb") as out: out.write(raw)
            saved.append({"path": path, "name": nm, "bytes": len(raw),
                          "type": f.mimetype or "application/octet-stream"})
        except Exception as e:
            errs.append(f"{getattr(f, 'filename', '?')}: {e}")
    if not saved: return jsonify(error="; ".join(errs) or "nothing saved"), 400
    return jsonify(saved=saved, errors=errs, dir=INBOX)

@app.route("/inbox")
def inbox_list():
    """What is sitting in the inbox — so the phone can show it and I can be asked about it."""
    try:
        os.makedirs(INBOX, exist_ok=True)
        out = []
        for n in sorted(os.listdir(INBOX), reverse=True)[:50]:
            fp = f"{INBOX}/{n}"
            if os.path.isfile(fp):
                st = os.stat(fp)
                out.append({"name": n, "bytes": st.st_size,
                            "when": time.strftime("%F %H:%M", time.localtime(st.st_mtime))})
        return jsonify(files=out, dir=INBOX)
    except Exception as e:
        return jsonify(files=[], error=str(e))

@app.route("/speak", methods=["POST"])
def speak_on_demand():
    """Read a FINISHED reply aloud, on request. Streams clips as SSE so playback can start on
    sentence one instead of waiting for the whole thing to render."""
    d = request.json or {}
    text = clean_for_speech((d.get("text") or "")[:6000])
    who = (d.get("who") or "odin").lower()
    voice = _alice_voice() if who == "alice" else _helix_voice()
    if not text.strip(): return jsonify(error="empty"), 400
    def gen():
        try:
            for ev in _emit_audio(text, (text[:64] + str(time.time())), voice): yield ev
        except Exception as e:
            yield _sse("error", {"text": str(e)})
        yield _sse("done", {})
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.route("/v4")
def v4(): return send_from_directory(app.static_folder, "index_v4.html")

@app.route("/v6")   # the workbench — the fallback since order 58, reachable by route as well as path
def v6(): return send_from_directory(app.static_folder, "index_v6.html")

@app.route("/v7")   # ORDER 58: the mind UI — what Helix.desktop opens (by its static path)
def v7(): return send_from_directory(app.static_folder, "index_v7.html")

# ── ORDER 58: the mind, the recalls and the build queue for v7. All three are read-only. ──────
# mindgraph is imported inside the routes: if numpy or the module ever broke, only /api/mind
# would fail, never Helix's start-up.
MIND_PATCHES = f"{BRAIN}/workspace/cycle/PATCHES.jsonl"   # read here, written only by _log_patch
MIND_RECALLS_TAIL = 20                                     # without ?since=, the newest N only

@app.route("/api/mind")
def api_mind():
    import mindgraph
    try:
        g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=FACTS))
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(g.to_json())

@app.route("/api/mind/recalls")
def api_mind_recalls():
    import mindgraph
    since = request.args.get("since") or None
    try:
        r = mindgraph.recalls(MIND_PATCHES, since)
    except ValueError as e:
        return jsonify(error=f"bad since: {e}"), 400
    if since is None:
        r["recalls"] = r["recalls"][-MIND_RECALLS_TAIL:]
    return jsonify(r)

BUILDER_RUN_SH = f"{BRAIN}/build_orders/builder_run.sh"

def _builders_running():
    """Live builder_run.sh processes from /proc: [{pid, order, unit}]. run_order.sh's .lock only
    exists while it launches the unit, so it cannot say a builder is running; the process can."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit(): continue
        try: argv, _cwd = _nr_argv(d)
        except Exception: continue
        if len(argv) > 4 and argv[1] == BUILDER_RUN_SH:
            out.append({"pid": int(d), "order": argv[2], "unit": argv[4]})
    return out

@app.route("/api/queue")
def api_queue():
    q = _queue_read()
    try: running = _builders_running()
    except Exception: running = []
    return jsonify(queue=q, live=_queue_live(q), running=running)

# ── LIVE WORKSPACE ────────────────────────────────────────────────────────────────────────────
# UI_LIVE_WORKSPACE_SPEC.md — the left column reports which files are being worked on, so the
# state of the project is readable without asking us for it.
#
# The naive walk is not survivable: this tree is 19,490 files and 4.5 GB, of which .venv alone
# is 17,237. Stat-ing that every five seconds would cost more than everything else the server
# does. So the walk is bounded three ways — a skip list, a depth cap and a node cap — and when
# a cap bites the response says so, because a tree that silently stops listing is a tree that
# lies about what is on disk.
WS_ROOT      = os.environ.get("HELIX_WS_ROOT", BRAIN)
WS_MAX_DEPTH = int(os.environ.get("HELIX_WS_DEPTH", "3"))
WS_MAX_NODES = int(os.environ.get("HELIX_WS_NODES", "900"))
WS_ACTIVE_S  = int(os.environ.get("HELIX_WS_ACTIVE", "180"))   # "being modified" = touched this recently

# Directories that are never the work: dependencies, model blobs, caches, generated audio.
WS_SKIP_DIRS = {".venv", "venv", "__pycache__", "node_modules", ".git", ".mypy_cache",
                ".pytest_cache", ".ruff_cache", ".cache", "models", "piper", "voices", "tts",
                "memory_backups", "dist", "build", ".idea", ".vscode"}
# Binary blobs: listing them is noise, and none of them is something either mind edits.
WS_SKIP_EXT  = {".pyc", ".pyo", ".so", ".o", ".a", ".bin", ".onnx", ".npy", ".npz", ".pt", ".pth",
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip", ".gz",
                ".tar", ".xz", ".bz2", ".7z", ".wav", ".mp3", ".ogg", ".flac", ".mp4", ".mkv",
                ".woff", ".woff2", ".ttf", ".otf", ".sqlite", ".db", ".pack", ".idx"}

def _ws_skip(name, is_dir):
    if name.startswith("."):  return True          # hidden entries, .git included
    if is_dir:                return name in WS_SKIP_DIRS
    return os.path.splitext(name)[1].lower() in WS_SKIP_EXT

def _ws_walk(path, depth, now, budget):
    """One directory level. `budget` is a one-element list so the node cap is shared across the
    whole recursion rather than reset per branch."""
    out = []
    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False),
                                                          e.name.lower()))
    except (PermissionError, OSError):
        return out
    for e in entries:
        if budget[0] <= 0: break
        try:    is_dir = e.is_dir(follow_symlinks=False)
        except OSError: continue
        if _ws_skip(e.name, is_dir): continue
        budget[0] -= 1
        try:    st = e.stat(follow_symlinks=False)
        except OSError: continue
        node = {"name": e.name, "dir": is_dir,
                # Age in SECONDS, computed here. The browser's clock is not this machine's clock,
                # and a file that looks stale because of a timezone is worse than no marker.
                "age": max(0, int(now - st.st_mtime))}
        if is_dir:
            node["children"] = (_ws_walk(e.path, depth + 1, now, budget)
                                if depth + 1 < WS_MAX_DEPTH else [])
            # A directory pruned by the depth cap is not an empty directory. Say which it is.
            node["deep"] = (depth + 1 >= WS_MAX_DEPTH)
            # A directory's age is the freshest thing INSIDE it, not its own mtime — a dir's
            # mtime only moves when entries are added or removed, so editing a file three levels
            # down leaves every directory above it looking untouched. Without this the panel
            # cannot show where work is happening unless that exact file is already on screen.
            # (A directory cut off by the depth cap can only report its own mtime; `deep` says so.)
            if node["children"]:
                node["age"] = min([node["age"]] + [c["age"] for c in node["children"]])
        else:
            node["size"] = st.st_size
        out.append(node)
    return out

@app.route("/api/workspace")
def api_workspace():
    now = time.time()
    budget = [WS_MAX_NODES]
    tree = _ws_walk(WS_ROOT, 0, now, budget)
    shown = WS_MAX_NODES - budget[0]
    return jsonify(root=os.path.basename(WS_ROOT.rstrip("/")) or WS_ROOT,
                   tree=tree, count=shown,
                   truncated=budget[0] <= 0, cap=WS_MAX_NODES,
                   depth=WS_MAX_DEPTH, active_s=WS_ACTIVE_S)

def _warmup():
    # Pre-load the voice models in the background so the first spoken reply / mic use is instant.
    global _kok, _whisper
    try:
        from kokoro_onnx import Kokoro; _kok = Kokoro(KOKORO_ONNX, KOKORO_VOICES)
    except Exception: pass
    try:
        from faster_whisper import WhisperModel; _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
    except Exception: pass
    if os.environ.get("HELIX_NO_PRECONNECT") != "1":   # skip persistent SDK pre-connect in headless/nested runs
        try:
            asyncio.run_coroutine_threadsafe(_ensure_client(), _loop)   # pre-connect the agent
        except Exception: pass
if os.environ.get("HELIX_NO_WARMUP") != "1":   # allow a bare-server boot for headless verification
    threading.Thread(target=_warmup, daemon=True).start()
if os.environ.get("HELIX_NO_SCHEDULER") != "1":   # off in tests, which drive scheduler_tick directly
    threading.Thread(target=_scheduler_loop, daemon=True).start()

if __name__=="__main__":
    _ok, _note = _credentials_state()      # surface a dead sign-in at boot, not mid-sentence
    print(("HELIX auth OK  · " if _ok else "HELIX AUTH PROBLEM · ") + _note, flush=True)
    PORT = int(os.environ.get("HELIX_PORT", "7777"))   # default 7777; override for headless/verification
    print(f"HELIX online → http://127.0.0.1:{PORT}")
    try:                                   # phone face: generate the token at boot, not lazily,
        _t = mobile_token()                # so the link is printable before anything hits it
        _ts = subprocess.run(["tailscale","status","--json"], capture_output=True, text=True, timeout=4)
        _host = ""
        if _ts.returncode == 0:
            _self = json.loads(_ts.stdout).get("Self", {})
            _host = (_self.get("DNSName") or "").rstrip(".")
        print(f"HELIX mobile → https://{_host}/m?k={_t}" if _host
              else f"HELIX mobile → http://127.0.0.1:{PORT}/m?k={_t}  (tailscale not up yet)")
    except Exception as _e:
        print(f"HELIX mobile → /m?k={mobile_token()}  (tailscale lookup failed: {_e})")
    # Silence werkzeug's per-request access log: the UI polls /metrics 1x/s and the
    # access lines were 98% of a 61 MB odin.log (measured 2026-09-06). Real errors
    # (ERROR level, tracebacks) still print.
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    # ORDER 31: say how last night's restart went (or that it was skipped), once.
    threading.Thread(target=_nightly_restart_announce, daemon=True).start()
    # ORDER 34: load the memory patch's model and index now, so the first turn is not the one to pay.
    threading.Thread(target=_memory_patch_warm, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
