#!/usr/bin/env python3
"""ODIN — persistent multi-model brain with voice. v0.1
Default = you + Claude (subscription). '/council' convenes the specialist bench on demand."""
import os, re, subprocess, time, json, hashlib, signal, threading, asyncio, queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory, Response
import requests

BRAIN   = os.path.dirname(os.path.abspath(__file__))
HOME    = os.path.expanduser("~")                      # every path below is derived, nothing is hardcoded
MEMORY  = f"{BRAIN}/memory/BRAIN.md"
CHARTER = f"{HOME}/colab/COLAB.md"
PIPER   = f"{BRAIN}/piper/piper/piper"
VOICE   = f"{BRAIN}/voices/alan.onnx"
TTSDIR  = f"{BRAIN}/tts"; SESS = f"{BRAIN}/memory/sessions"
FACTS   = f"{BRAIN}/memory/facts"   # durable per-fact memory (one file = one verified fact)
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
# If your Claude session lives somewhere other than ~/.claude, point CLAUDE_CONFIG_DIR at it.
# When it is missing the CLI can silently fall back to a stale credential file and die with
# "OAuth session expired and could not be refreshed" — so set the floor here rather than
# relying on the launcher, which is only one of several ways this process gets started.
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
FACTS_BUDGET = 45000       # chars of full-text facts to inline (newest by mtime); rest become an index
DRAWER_CAP   = 20000
TURN_CAP     = 1400        # truncate any single conversation turn
CONVO_CAP    = 14000

def _first_line(txt):
    for ln in txt.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln: return ln[:120]
    return "(untitled)"

def _facts_digest(budget=FACTS_BUDGET):
    # newest facts (by mtime) inlined in full up to `budget`; the rest as a readable index to open on demand
    facts = []
    try:
        for fn in sorted(os.listdir(FACTS)):
            if not fn.endswith(".md"): continue
            p = f"{FACTS}/{fn}"
            try: facts.append((fn, open(p).read().strip(), os.path.getmtime(p)))
            except Exception: pass
    except Exception: pass
    inline, index, used = [], [], 0
    for fn, t, _mt in sorted(facts, key=lambda x: -x[2]):
        if used + len(t) <= budget: inline.append((fn, t)); used += len(t)
        else: index.append((fn, t))
    inline.sort(key=lambda x: x[0]); index.sort(key=lambda x: x[0])   # readable name order
    block = "\n\n".join(f"# {fn}\n{t}" for fn, t in inline)
    if index:
        block += ("\n\n=== OLDER FACTS (index — open any of these in full with your file tools; they live in "
                  + FACTS + ") ===\n" + "\n".join(f"- memory/facts/{fn} — {_first_line(t)}" for fn, t in index))
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

def claude(p):
    # ODIN's brain = a full Claude Code agent: tools ON (bash/files/web), non-interactive,
    # on the subscription (ENV drops ANTHROPIC_API_KEY), working in his own workspace.
    cmd = ["claude","-p",p,"--model","opus","--permission-mode","bypassPermissions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=ENV, cwd=WORKSPACE)
        out = (r.stdout or "").strip()
        return out or f"[no output: {(r.stderr or '')[-200:]}]"
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
    for d in (BRAIN, f"{HOME}/Desktop"):   # add your own data mounts here
        if os.path.isdir(d): cmd += ["--add-dir", d]      # she can only audit what she can open
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

@app.route("/remember", methods=["POST"])
def remember():
    # Write a discrete fact file instead of growing one blob, so memory is durable + editable per-fact.
    n=(request.json or {}).get("text","").strip()
    if not n: return jsonify(ok=False)
    slug=(re.sub(r'[^a-z0-9]+','-', n.lower()).strip('-')[:40] or "note")
    h=hashlib.md5(n.encode()).hexdigest()[:6]
    fn=f"{FACTS}/{time.strftime('%Y%m%d')}-{slug}-{h}.md"
    with open(fn,"w") as f:
        f.write(f"---\nsaved: {time.strftime('%F')}\nstatus: unverified\n---\n{n}\n")
    return jsonify(ok=True, file=os.path.basename(fn))

@app.route("/history")                        # UI repaints the prior conversation on load
def history():
    turns = parse_log(24)
    if not HISTORY:                           # only warm once, so refreshes don't duplicate the thread
        for t in turns: HISTORY.append((t["role"], t["text"]))
    return jsonify(turns=turns)

# ============ Phase 1: streaming agent brain (Claude Agent SDK → SSE) ============
ODIN_PERSONA = ("You are ODIN — a persistent AI agent with a voice, speaking to ONE human. You have real "
    "tools (bash, read/write files, web) in your workspace; when a task needs action, DO it, don't just "
    "describe it. For ordinary conversation, answer immediately and briefly WITHOUT tools. Reply with a "
    "short, spoken-friendly summary that leads with the outcome; put any code in ``` blocks (shown in the "
    "chat, not read aloud). Do routine reversible work without asking; pause only for destructive or "
    "outside-world actions.")

def _preamble():
    charter = open(CHARTER).read() if os.path.exists(CHARTER) else ""
    return (f"{ODIN_PERSONA}\n\n=== ODIN LONG-TERM MEMORY ===\n{load_memory()}"
            f"\n\n=== RECENT SESSIONS ===\n{recent_sessions()}\n\n{charter}")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_client = None; _primed = False; _interrupting = False
_STREAM_DONE = object()
# There is ONE shared SDK client. Two overlapping /ask_stream requests used to call query() and
# receive_response() on it concurrently, so one turn's messages could be delivered into the other
# turn's stream — the "I asked something else and then the rest of the last answer showed up" bug.
# Turns are now serialized; a second question waits instead of corrupting the first.
_turn_lock = threading.Lock()

# One tool result arrives as one JSON line on the CLI's stdout. The SDK refuses to buffer a
# line past this, and blowing the limit kills its reader task outright — so the ceiling has to
# clear the largest result a turn can realistically produce (a big file read, a wide grep).
# The SDK's own default is 1MB, which a single Read of a large log will exceed.
MAX_MSG_BYTES = int(os.environ.get("HELIX_MAX_MSG_BYTES", 32 * 1024 * 1024))

async def _ensure_client():
    global _client, _primed
    if _client is None:
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        opts = ClaudeAgentOptions(model="opus", permission_mode="bypassPermissions",
                                  cwd=WORKSPACE, cli_path=CLAUDE_CLI,
                                  max_buffer_size=MAX_MSG_BYTES,
                                  # Without this the SDK only hands back COMPLETE assistant messages,
                                  # so text landed in big silent lumps with tool gaps between them.
                                  include_partial_messages=True)
        c = ClaudeSDKClient(opts)
        await c.connect()
        _client = c; _primed = False
    return _client

async def _drop_client():
    """Retire the current client so the next turn reconnects.

    A fatal transport error (oversized message, broken pipe) kills the SDK's reader task but
    leaves the client object alive. Without this the cached client is reused forever and every
    later turn fails — the state that used to force a restart of Helix.
    """
    global _client, _primed
    c, _client, _primed = _client, None, False
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

def _mem_state(budget=FACTS_BUDGET):
    """Which facts actually made it into context, and how much of FACTS_BUDGET they used.
    Mirrors _facts_digest's newest-first selection exactly (same read, same length accounting);
    it does not change what _facts_digest does."""
    facts = []
    try:
        for fn in sorted(os.listdir(FACTS)):
            if not fn.endswith(".md"): continue
            p = f"{FACTS}/{fn}"
            try: facts.append((fn, len(open(p).read().strip()), os.path.getmtime(p)))
            except Exception: pass
    except Exception: pass
    names, used = [], 0
    for fn, sz, _mt in sorted(facts, key=lambda x: -x[2]):
        if used + sz <= budget: names.append(fn); used += sz
    names.sort()
    return {"count": len(facts), "loaded": len(names), "budget": budget,
            "budget_used": used, "indexed": len(facts) - len(names), "names": names}

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

async def _run_turn(text, q):
    global _primed, _interrupting
    from claude_agent_sdk import ResultMessage, StreamEvent
    final_text = []
    streamed = {"any": False}
    _interrupting = False
    try:
        c = await _ensure_client()
        # `resent` says whether the memory block is going over the wire THIS turn or was already
        # primed into the session — otherwise the readout would claim a load that didn't happen.
        try: q.put(("mem", dict(_mem_state(), resent=not _primed)))
        except Exception: pass
        prompt = _crosstalk_odin() + (text if _primed else (_preamble() + "\n\n=== USER ===\n" + text))
        _primed = True
        await c.query(prompt)
        async for msg in c.receive_response():
            if isinstance(msg, ResultMessage):
                if not final_text and getattr(msg, "result", None): final_text.append(msg.result)
                try: q.put(("usage", _usage_payload(msg)))
                except Exception: pass                      # a missing field must not kill the turn
                break
            if isinstance(msg, StreamEvent):
                _emit_delta(msg, q, streamed); continue
            _emit_blocks(msg, q, final_text, suppress_text=streamed["any"])
        q.put(("final", {"text": "".join(final_text)}))
    except Exception as e:
        # A pressed STOP is an ordinary end to a turn — keep the session and its context.
        # Anything else may have left the transport unusable, so retire it and reconnect next turn.
        if not _interrupting:
            try: await _drop_client()
            except Exception: pass
        q.put(("error", {"message": _turn_error_text(e)}))
    finally:
        _interrupting = False
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

@app.route("/ask_stream", methods=["POST"])       # front desk → (fast local) OR (Claude agent stream)
def ask_stream():
    text = ((request.json or {}).get("text", "")).strip()
    mode = ((request.json or {}).get("mode", "odin"))
    if not text: return jsonify(error="empty"), 400
    def gen():
        if mode == "alice":                             # ALICE: Gemini high-reasoning (one-shot, her voice)
            ans, usage = alice_brain(text)
            if usage: yield _sse("usage", usage)
            yield _sse("text", {"text": ans})
            _log_turn(text, ans, "ALICE")
            for ev in _emit_audio(clean_for_speech(ans), text + str(time.time()), _alice_voice()): yield ev
            yield _sse("final", {"text": ans}); yield _sse("done", {}); return
        local = greet(text)                             # ODIN's fast front desk gets first look
        if local is not None:                           # FAST LOCAL PATH — no Claude, no tools
            yield _sse("greet", {})                     # UI: reflex blip (local, not the deep mind)
            yield _sse("text", {"text": local})
            _log_turn(text, local)
            for ev in _emit_audio(clean_for_speech(local), text + str(time.time())): yield ev
            yield _sse("final", {"text": local}); yield _sse("done", {}); return
        # ESCALATE → the Claude agent + tools. Serialized: see _turn_lock.
        if not _turn_lock.acquire(blocking=False):
            yield _sse("text", {"text": "_(one moment — finishing the previous answer first)_\n\n"})
            _turn_lock.acquire()
        try:
            q = queue.Queue()
            logged = False
            tts = _TTSPipeline(text + str(time.time()))
            asyncio.run_coroutine_threadsafe(_run_turn(text, q), _loop)
            while True:
                try:
                    item = q.get(timeout=0.12)
                except queue.Empty:
                    for ev in tts.drain(): yield ev      # keep audio flowing during tool calls
                    continue
                if item is _STREAM_DONE:
                    for ev in tts.drain(block=True): yield ev
                    yield _sse("done", {}); break
                kind, payload = item
                if kind == "delta":
                    tts.feed(payload.get("text", ""))
                    yield _sse("text", payload)
                    for ev in tts.drain(): yield ev
                elif kind == "text":
                    tts.feed(payload.get("text", ""))
                    yield _sse("text", payload)
                elif kind == "final":
                    ans = payload.get("text", "")
                    _log_turn(text, ans); logged = True
                    for ev in tts.finish(fallback_text=ans): yield ev
                    yield _sse("final", {"text": ans})
                else:
                    if kind == "error" and not logged:
                        # Record the question even though it never got an answer. A turn that dies
                        # takes its CLI session with it, so this line is the only thing that tells him
                        # what he was working on when he comes back.
                        _log_turn(text, "(this turn ended before I answered — session was rebuilt)")
                        logged = True
                    yield _sse(kind, payload)
        finally:
            _turn_lock.release()
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.route("/interrupt", methods=["POST"])
def interrupt():
    global _interrupting
    if _client is None: return jsonify(ok=False, error="no active session")
    _interrupting = True          # marks the coming turn-failure as deliberate, not a broken transport
    try:
        asyncio.run_coroutine_threadsafe(_client.interrupt(), _loop).result(timeout=6)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# ============ memory constellation: expose facts grouped into clusters ============
# ============ optional project grouping (memory/projects.json) ============
# Helix derives a fact's date, its stated verdict and its citations from the file itself. The one
# thing it cannot infer is which PROJECT a file belongs to, so that is the one thing you declare.
# Copy memory/projects.example.json -> memory/projects.json and edit it. Read once at import, so
# a change needs a restart. Absent or malformed => one GENERAL bucket, and nothing breaks.
def _load_projects(key):
    try:
        with open(f"{BRAIN}/memory/projects.json", encoding="utf-8") as fh:
            rows = json.load(fh).get(key) or []
        return [(str(a), str(b), [str(k).lower() for k in c]) for a, b, c in rows]
    except Exception:
        return []

MEMORY_CATS = _load_projects("memory")   # (label, color, [fact filenames]) for the memory constellation
def _fact_title(f):
    try:
        for line in open(f"{FACTS}/{f}"):
            s=line.strip()
            if s.startswith("#"): return s.lstrip("# ").strip()[:74]
            if s and not s.startswith("---"): return s[:74]
    except Exception: pass
    return f.replace(".md","")

_cpu_prev = [0, 0]
@app.route("/stat")                               # live telemetry for the HUD gauges (reads /proc, no deps)
def stat():
    global _cpu_prev
    cpu = 0
    try:
        vals = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0); total = sum(vals)
        dt = total - _cpu_prev[0]; di = idle - _cpu_prev[1]; _cpu_prev = [total, idle]
        if dt > 0: cpu = max(0, min(100, int(100 * (1 - di / dt))))
    except Exception: pass
    mu = mt = 0
    try:
        mi = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1); mi[k] = int(v.strip().split()[0])
        mt = mi.get("MemTotal", 0) // 1024
        mu = mt - (mi.get("MemAvailable", mi.get("MemFree", 0)) // 1024)
    except Exception: pass
    return jsonify(cpu=cpu, mem_used=mu, mem_total=mt)

@app.route("/memory")
def memory_map():
    present=set(f for f in os.listdir(FACTS) if f.endswith(".md"))
    used=set(); cats=[]
    for name,color,files in MEMORY_CATS:
        nodes=[{"file":f,"title":_fact_title(f)} for f in files if f in present]
        for n in nodes: used.add(n["file"])
        if nodes: cats.append({"name":name,"color":color,"files":nodes})
    misc=[{"file":f,"title":_fact_title(f)} for f in sorted(present-used)]
    if misc: cats.append({"name":"MISC","color":"#9aa4b2","files":misc})
    return jsonify(cats=cats)

# ============ BLUEPRINT ENGINE: the body of work, derived from what the facts actually say ============
# Nothing here is guessed by word-frequency. Every field traces to something explicit in the corpus:
#   verdict  ← the verdict word YOU put in the filename (KILL/PASS/PRIOR_ART/falsified/walkback…),
#              else the file's own "**Status:** …" line. Body-keyword guessing is the last resort and
#              is reported as low-confidence so the UI can say so rather than pretend.
#   date     ← the real date written in the file (100/106 carry one). This drives the timeline.
#   origin   ← "local", or "imported" for files named with an imported_ prefix (facts
#              synced in from another machine, so you can see which box learned what).
#   edges    ← facts that literally cite another fact's filename. A real citation graph, not name-matching.
BP_PROJECTS = _load_projects("blueprint")   # (label, color, filename keywords) — first match wins
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

_BP_CITE = re.compile(r'\b((?:ws2_)?\d{2,}_[A-Za-z0-9_\-]+\.md)')

@app.route("/blueprint")
def blueprint():
    files = sorted(f for f in os.listdir(FACTS) if f.endswith(".md"))
    nodes, byfile = [], {}
    for f in files:
        try: text = open(f"{FACTS}/{f}").read()
        except Exception: text = ""
        tag  = _bp_tag(text)
        low  = f.lower()
        proj, color = (tag["project"].upper(), _BP_COLOR.get(tag["project"].upper(), "#9aa4b2")) \
                      if tag.get("project") else _bp_project(f)
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
        m = re.match(r'^(ws2_)?(\d+)_', f)
        body = " ".join(text.replace("#", " ").split())
        n = {"file": f, "title": _fact_title(f), "project": proj, "color": color,
             "verdict": verdict, "vlabel": BP_VLABEL.get(verdict, verdict.upper()),
             "vsrc": vsrc, "confident": vsrc != "body",
             "origin": "imported" if f.startswith("imported_") else "local",
             "seq": int(m.group(2)) if m else -1,
             "date": _bp_date(text), "status_line": status_line,
             "snip": body[:420], "words": len(body.split()),
             "cites": [], "cited_by": []}
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
_proc_cpu_prev = {"t": 0.0, "c": 0.0}
_metrics_cache = {"facts": (0.0, None), "tts": (0.0, None)}
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

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
    try:
        f = open("/proc/self/stat").read().rsplit(") ", 1)[1].split()   # comm may contain ')'
        ticks = (int(f[11]) + int(f[12])) / float(_CLK)                 # utime + stime
        pt, pc = _proc_cpu_prev["t"], _proc_cpu_prev["c"]
        _proc_cpu_prev["t"], _proc_cpu_prev["c"] = now, ticks
        if pt and now > pt: cpu = round(max(0.0, 100.0 * (ticks - pc) / (now - pt)), 1)
    except Exception: pass
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
    return jsonify(cpu=cpu, rss_mb=rss, uptime_s=round(now - _T_BOOT, 1),
                   log_bytes=log_b, log_turns_tail=log_turns, log_keep=LOG_KEEP_TURNS,
                   tts_queue=depth, tts_files=tts_n, tts_bytes=tts_b,
                   facts=_fact_count(), signin_ok=ok,
                   signin_h=(float(m.group(1)) if m else None), signin_note=note,
                   turn_busy=_turn_lock.locked())

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

@app.route("/v4")
def v4(): return send_from_directory(app.static_folder, "index_v4.html")

@app.route("/v6")   # the workbench — what Helix.desktop opens, reachable by route as well as path
def v6(): return send_from_directory(app.static_folder, "index_v6.html")

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

if __name__=="__main__":
    _ok, _note = _credentials_state()      # surface a dead sign-in at boot, not mid-sentence
    print(("HELIX auth OK  · " if _ok else "HELIX AUTH PROBLEM · ") + _note, flush=True)
    PORT = int(os.environ.get("HELIX_PORT", "7777"))   # default 7777; override for headless/verification
    print(f"HELIX online → http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
