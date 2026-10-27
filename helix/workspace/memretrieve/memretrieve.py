"""ORDER 34 (item 17): per-turn memory retrieval over memory/facts/*.md.

Three arms over the same index:
  bm25    Okapi BM25. Tokenizer, stop list, K1 and B are memeval/score.py's (copied, not imported:
          score.py runs its whole eval at import). test_memretrieve.py pins them equal.
  embed   cosine on BAAI/bge-small-en-v1.5 (ONNX, CPU only). A fact is cut into ~480-token
          chunks, each prefixed with the fact's heading; the fact scores its best chunk.
  hybrid  reciprocal rank fusion (k=60) of the two rankings.

The query is the last few of the user's turns, NEWEST FIRST, weighted 1, 0.5, 0.25. A turn like
"ok go ahead" carries nothing (124_*), so the gate is the NEWEST turn alone: if its own top score
is not above the arm's floor, the patch is empty, not a guess (hybrid: either arm's gate opens).

Model files and the vector cache live on the internal HDD (153_*). Vectors are cached per fact
by the sha256 of its text, so only a changed or new fact is re-embedded.
Reads only. Writes only under CACHE_DIR.

ORDER 35b: memory/archive/ (the facts dir's sibling) is indexed too. An archived fact's scores are
multiplied by ARCHIVE_WEIGHT for ranking, its hits say "(archived)", and the patch points at
memory/archive/. The gate reads the unweighted scores: the floor asks whether the turn has a topic
at all, and a direct question about an archived fact has one. A name in both places is the live one.

ORDER 42c: a superseded or archived fact is never inlined; its replacement is. The supersession map
is derived from the facts on every refresh (supersession()); ranking, the gate and FLOORS are
unchanged. Hits stay (file, score, why); the replacements ride beside them in r["replaced_by"].

ORDER 70: a newer fact can retire ONE PASSAGE of an older one without editing it (retirements());
wherever the older fact is shown in full, the passage is replaced by a marker naming the newer fact.

ORDER 74 (item 29, retrieval misses): three switches, retrieve(..., arms=...), each measured alone in
memeval/RESULT_ORDER74.txt against memeval/PREREG_ORDER74.md; DEFAULT_ARMS are the ones that shipped.
  aliases   `Also called: a; b` in a fact's first 15 lines, plus ALIASES.csv beside this file (file,aliases)
            for facts that can't be edited. Each alias token adds ALIAS_WEIGHT to the fact's bm25 term
            counts; one extra chunk "<heading>\\nAlso called: <aliases>" joins its vectors. Text shown: unchanged.
  adapt     after the k picks, each of the next ADAPT_MAX ranked facts scoring >= ADAPT_RATIO x the k-th
            pick's is added as a head (never full text). Off when there are fewer than k picks.
  catboost  a query word that belongs to only one category in CATEGORIES.txt (name or description)
            multiplies that category's facts' bm25 ranking score by CATEGORY_BOOST. The gate is unchanged.
"""
import csv, hashlib, importlib.util, math, os, re, threading, time
from collections import Counter

FACTS = "__HOME__/helix/memory/facts"
ARCHIVE = "__HOME__/helix/memory/archive"
ARCHIVE_WEIGHT = 0.5                     # ORDER 35b: an archived fact ranks at half its score
BULK = "__BULK__/memretrieve"
MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_REV = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
MODEL_ONNX_SHA256 = "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35"
MODEL_DIR = f"{BULK}/model/bge-small-en-v1.5"
CACHE_DIR = f"{BULK}/vectors"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # bge's own, for short queries
CHUNK_TOKENS = 480
TURN_WEIGHTS = (1.0, 0.5, 0.25)          # newest first
RRF_K = 60
ARMS = ("bm25", "embed", "hybrid")
# Calibrated in memeval/RESULT_ORDER34.txt (method fixed in memeval/PREREG_ORDER34.md): the
# highest newest-turn top score over the contentless calibration turns. Patch only when ABOVE.
FLOORS = {"bm25": 6.83, "embed": 0.701}

# ---- memeval/score.py's tokenizer and BM25 parameters (copied; a test pins them equal)
STOP = set("""a an the and or of to in on for with is are was were be been it its this that these those
i you he she we they me my your our their as at by from into over under about after before if then than
do does did done can could should would will shall may might must not no yes what which who whom whose
when where why how all any both each few more most other some such only own same so too very s t just
don now got get go went make made take taken thing things stuff""".split())
K1, B = 1.5, 0.75

# ---- ORDER 74 switches
HERE = os.path.dirname(os.path.abspath(__file__))
ALIASES = os.path.join(HERE, "ALIASES.csv")
CATEGORIES_PY = os.path.join(os.path.dirname(HERE), "categories", "categories.py")
CATEGORIES_TXT = os.path.join(os.path.dirname(HERE), "categories", "CATEGORIES.txt")
ALL_ARMS = ("aliases", "adapt", "catboost")
DEFAULT_ARMS = ("aliases", "adapt")      # memeval/RESULT_ORDER74.txt: A and B shipped; C (catboost) did not
ALIAS_LINES = 15
ALIAS_WEIGHT = 2                         # added to a fact's bm25 count per alias token occurrence
ADAPT_RATIO = 0.97
ADAPT_MAX = 3
CATEGORY_BOOST = 1.15
_ALSO_RE = re.compile(r"^\s*Also called:\s*(.*)$")


def toks(s):
    return [w for w in re.findall(r"[a-z0-9_]+", s.lower()) if w not in STOP and len(w) > 1]


# ---- ORDER 74: aliases
def aliases_in(text):
    """The aliases on `Also called: a; b` lines in the fact's first ALIAS_LINES lines."""
    out = []
    for ln in (text or "").splitlines()[:ALIAS_LINES]:
        m = _ALSO_RE.match(ln)
        if m:
            out += [a.strip() for a in m.group(1).split(";") if a.strip()]
    return out


def read_alias_sidecar(path=ALIASES):
    """{file: [aliases]} from the sidecar (file,aliases; aliases `;`-separated). Absent is {}."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                fn = (r.get("file") or "").strip()
                if fn:
                    out.setdefault(fn, []).extend(a.strip() for a in (r.get("aliases") or "").split(";") if a.strip())
    except FileNotFoundError:
        pass
    return out


def merge_aliases(texts, sidecar):
    """texts {name: text}, sidecar {file: [aliases]} -> ({name: [aliases]} for facts that have any,
    the fact's own line first, deduplicated case-folded; [sidecar files that are not facts])."""
    merged = {}
    for fn in sorted(texts):
        seen, out = set(), []
        for a in aliases_in(texts[fn]) + sidecar.get(fn, []):
            if a.casefold() not in seen:
                seen.add(a.casefold())
                out.append(a)
        if out:
            merged[fn] = out
    return merged, sorted(fn for fn in sidecar if fn not in texts)


def aliases_report(facts_dir=FACTS, archive_dir=None, path=ALIASES):
    """{"applied": {fact: [aliases]}, "unknown_files": [sidecar rows naming no fact]} on disk."""
    merged, unknown = merge_aliases({fn: t for fn, (t, _a) in _read_base(facts_dir, archive_dir).items()},
                                    read_alias_sidecar(path))
    return {"applied": merged, "unknown_files": unknown}


def alias_chunk(head, aliases):
    return f"{head}\nAlso called: {'; '.join(aliases)}"


# ---- ORDER 74: category boost
def distinctive_words(cats):
    """{category name: the toks of its name + description that belong to no other category}."""
    words = {c.name: set(toks(f"{c.name} {c.desc}")) for c in cats}
    count = Counter(w for ws in words.values() for w in ws)
    return {n: {w for w in ws if count[w] == 1} for n, ws in words.items()}


def matched_categories(text, cats):
    """The categories with at least one distinctive word among `text`'s toks."""
    q = set(toks(text))
    return {n for n, ws in distinctive_words(cats).items() if ws & q}


def heading(text):
    """The fact's first non-empty line, without the leading #s."""
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln:
            return ln[:160]
    return "(untitled)"


# ---- ORDER 42c: supersession, derived from the facts on every refresh (never hand-kept)
#   whole    A's first heading line says SUPERSEDED by `B` -> A is never inlined; B is.
#   partial  B says it supersedes `A` (active verb, in its own sentence), or names A in a bullet
#            under a "What this supersedes" heading -> A inlines with a warning line naming B.
# A backticked name resolves only if it IS a file name (with or without .md), or is an `NN_*` glob
# matching exactly one file in a sentence that is not in the archive. Anything else is reported in
# "unresolved", never guessed. A pair naming each other is a cycle: reported, both partial.
_WHOLE_RE = re.compile(r"SUPERSEDED\s+(?:by|BY)\s+`([^`]+)`")
_ACTIVE_RE = re.compile(r"\bsupersedes\b", re.I)
_TICK_RE = re.compile(r"`([^`]+)`")
_NAME_RE = re.compile(r"^[\w-]+\.md$|^[\w-]+_[\w-]+$")      # a file name, with or without .md
_GLOB_RE = re.compile(r"^[\w-]+_\*$")                       # NN_* resolves; box2_* never does
_NN_GLOB_RE = re.compile(r"^\d+_\*$")
_SECTION_RE = re.compile(r"^#+\s*what this supersedes\b", re.I)
MAX_CHAIN = 5


def _outside_ticks(s):
    """The (start, end) spans of `s` that are not inside backticks."""
    spans, pos, ticks = [], 0, [m.span() for m in _TICK_RE.finditer(s)]
    for a, b in ticks:
        spans.append((pos, a))
        pos = b
    return spans + [(pos, len(s))]


def _sentence_end(s):
    """Index of the first full stop outside backticks that ends a sentence, or None."""
    for a, b in _outside_ticks(s):
        for i in range(a, b):
            if s[i] == "." and (i + 1 == len(s) or s[i + 1].isspace()):
                return i
    return None


def _names(s):
    """The backticked tokens in `s` that look like a file name or a glob."""
    return [t for t in _TICK_RE.findall(s) if _NAME_RE.match(t) or _GLOB_RE.match(t)]


def _resolve(tok, src, docs):
    """A backticked name in `src`, resolved by ORDER 42c's rule: a file name (with or without .md), or
    an `NN_*` glob matching exactly one fact, never from an archived `src`. None if it does not."""
    if tok in docs:
        return tok
    if tok + ".md" in docs:
        return tok + ".md"
    if _NN_GLOB_RE.match(tok) and not docs[src][1]:
        hit = [n for n in docs if n.startswith(tok[:-1])]
        if len(hit) == 1:
            return hit[0]
    return None


def supersession(docs):
    """docs: {name: (text, archived)}. Returns {"edges", "unresolved", "cycles", "whole", "partial",
    "replaced"}: edges [{kind, a, b, src, line, text}] (A superseded by B), unresolved
    [{src, line, token, text}], cycles [[a, b]], whole {A: B}, partial {A: [B…]}, and replaced
    {A: the newest fact at the end of A's whole chain, MAX_CHAIN deep}."""
    names = set(docs)
    edges, unresolved = [], []

    def resolve(tok, src, ln, text):
        hit = _resolve(tok, src, docs)
        if hit:
            return hit
        unresolved.append({"src": src, "line": ln, "token": tok, "text": text.strip()[:200]})
        return None

    def edge(kind, a, src, ln, text):
        if a and a != src:
            edges.append({"kind": kind, "a": a, "b": src, "src": src, "line": ln, "text": text.strip()[:200]})

    for src in sorted(docs):
        lines = docs[src][0].splitlines()
        first = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("#")), None)
        if first is not None:
            m = _WHOLE_RE.search(lines[first])
            if m:
                # here `src` is A, superseded by the named B
                b = resolve(m.group(1), src, first + 1, lines[first])
                if b and b != src:
                    edges.append({"kind": "whole", "a": src, "b": b, "src": src, "line": first + 1,
                                  "text": lines[first].strip()[:200]})
        section = False
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#"):
                section = bool(_SECTION_RE.match(ln.strip()))
            elif section and ln.lstrip()[:2] in ("- ", "* ") and ln.lstrip()[2:].startswith("`"):
                lead = ln.lstrip()[2:]
                colon = next((j for a, b in _outside_ticks(lead) for j in range(a, b) if lead[j] == ":"), len(lead))
                for tok in _names(lead[:colon]):
                    edge("partial", resolve(tok, src, i + 1, ln), src, i + 1, ln)
            for a, b in _outside_ticks(ln):
                for m in _ACTIVE_RE.finditer(ln, a, b):
                    rest, shown = ln[m.end():], ln
                    if (_sentence_end(rest) is None and not _names(rest) and i + 1 < len(lines)
                            and not ln.lstrip().startswith("#")):
                        rest, shown = rest + " " + lines[i + 1], ln + " " + lines[i + 1].strip()
                    end = _sentence_end(rest)
                    for tok in _names(rest if end is None else rest[:end]):
                        edge("partial", resolve(tok, src, i + 1, shown), src, i + 1, shown)

    whole = {}
    for e in edges:
        if e["kind"] == "whole":
            whole.setdefault(e["a"], e["b"])
    pairs = {(e["a"], e["b"]) for e in edges}
    cycles = sorted({tuple(sorted(p)) for p in pairs if (p[1], p[0]) in pairs})
    for a, b in cycles:
        for x, y in ((a, b), (b, a)):
            if whole.get(x) == y:
                del whole[x]
    partial = {}
    for a, b in sorted(pairs):
        if whole.get(a) != b:
            partial.setdefault(a, []).append(b)
    replaced = {}
    for a in whole:
        x, seen = a, [a]
        for _ in range(MAX_CHAIN):
            nx = whole.get(x)
            if nx is None or nx in seen:
                if nx is not None:
                    cyc = sorted(seen[seen.index(nx):])
                    if cyc not in [list(c) for c in cycles]:
                        cycles.append(tuple(cyc))
                break
            x = nx
            seen.append(x)
        if x != a:
            replaced[a] = x
    return {"edges": edges, "unresolved": unresolved, "cycles": [list(c) for c in cycles],
            "whole": whole, "partial": partial, "replaced": replaced}


# ---- ORDER 70: retired passages, derived from the facts on every refresh like supersession()
#   In a newer fact B, one per line, at the start of the line:  RETIRES: `<A>` "<passage>"
#   <A> resolves by 42c's rule (_resolve). The passage is matched on whitespace-collapsed text (every
#   run of whitespace is one space, both sides), case-sensitive, and must be >= MIN_PASSAGE chars and
#   occur in A exactly once. Anything else is unresolved: reported, never applied, never guessed.
#   Where A is shown in full, the span becomes RETIRED_MARK; on disk A is never touched.
_RETIRES_RE = re.compile(r'^RETIRES:\s*`([^`]+)`\s*"(.*)"\s*$')
MIN_PASSAGE = 20
RETIRED_MARK = '⟦RETIRED by {b}: "{head}…"⟧'
NO_RETIREMENTS = {"applied": [], "unresolved": []}


def _collapse(s):
    """`s` with every run of whitespace as one space, and each kept char's (start, end) span in `s`."""
    flat, spans = [], []
    for m in re.finditer(r"\s+|\S", s):
        flat.append(" " if m.group()[0].isspace() else m.group())
        spans.append(m.span())
    return "".join(flat), spans


def _spans_of(flat, spans, passage):
    """Every (start, end) in the original text where the collapsed `passage` occurs (overlaps count)."""
    out, i = [], flat.find(passage)
    while i >= 0:
        out.append((spans[i][0], spans[i + len(passage) - 1][1]))
        i = flat.find(passage, i + 1)
    return out


def retirements(docs):
    """docs: {name: (text, archived)}. Returns {"applied": [{a, b, passage, start, end}], "unresolved":
    [{b, line, why}]}: start/end index A's text; passage is the collapsed one. B is taken in name
    order, so of two overlapping spans in one A the later B's is the unresolved one."""
    applied, unresolved, flat = [], [], {}
    for b in sorted(docs):
        text, archived = docs[b]
        for i, ln in enumerate(text.splitlines()):
            if not ln.startswith("RETIRES:"):
                continue
            why, m = None, _RETIRES_RE.match(ln)
            if not m:
                why = 'malformed: want RETIRES: `<fact>` "<passage>"'
            elif archived:
                why = "B archived"
            else:
                tok, passage = m.group(1), re.sub(r"\s+", " ", m.group(2))
                a = _resolve(tok, b, docs)
                if len(passage) < MIN_PASSAGE:
                    why = f"passage < {MIN_PASSAGE} chars"
                elif a is None:
                    why = f"no such fact `{tok}`"
                elif a == b:
                    why = "retires itself"
                else:
                    if a not in flat:
                        flat[a] = _collapse(docs[a][0])
                    hits = _spans_of(*flat[a], passage)
                    if len(hits) != 1:
                        why = f"{len(hits)} matches in {a}"
                    else:
                        (start, end), = hits
                        clash = next((r for r in applied if r["a"] == a and r["start"] < end and start < r["end"]), None)
                        if clash:
                            why = f"overlaps {clash['b']}'s retirement in {a}"
                        else:
                            applied.append({"a": a, "b": b, "passage": passage, "start": start, "end": end})
            if why:
                unresolved.append({"b": b, "line": i + 1, "why": why})
    return {"applied": applied, "unresolved": unresolved}


def apply_retirements(name, text, ret):
    """The text of fact `name` as shown, given `ret` = retirements(): each of its applied passages
    replaced by RETIRED_MARK. The span is found again in `text` (collapsed, exactly once), so a caller's
    own read of the fact is fine. A fact with no applied retirement comes back as it went in."""
    mine = [r for r in (ret or NO_RETIREMENTS)["applied"] if r["a"] == name]
    if not mine:
        return text
    flat, spans = _collapse(text)
    cuts = []
    for r in mine:
        hits = _spans_of(flat, spans, r["passage"])
        if len(hits) == 1 and not any(s < hits[0][1] and hits[0][0] < e for s, e, _m in cuts):
            cuts.append((*hits[0], RETIRED_MARK.format(b=r["b"], head=r["passage"][:60])))
    for s, e, mark in sorted(cuts, reverse=True):
        text = text[:s] + mark + text[e:]
    return text


def _read_base(facts_dir, archive_dir=None):
    """{name: (text, archived)} for the facts and the archive beside them; a name in both is live."""
    archive_dir = archive_dir or os.path.join(os.path.dirname(os.path.abspath(facts_dir)), "archive")
    out = {}
    for d, archived in ((facts_dir, False), (archive_dir, True)):
        try:
            entries = sorted(os.scandir(d), key=lambda de: de.name)
        except FileNotFoundError:
            if archived:
                continue
            raise
        for de in entries:
            if de.name.endswith(".md") and de.is_file() and de.name not in out:
                out[de.name] = (open(de.path, "rb").read().decode("utf-8", "replace").strip(), archived)
    return out


def superseded_by(facts_dir=FACTS, archive_dir=None):
    """{A: newest replacement} for the facts on disk (app's index lines). Never raises: on any
    error, an empty map and one log line."""
    try:
        return supersession(_read_base(facts_dir, archive_dir))["replaced"]
    except Exception as e:
        print(f"HELIX: [facts] no supersession map this time — {type(e).__name__}: {e}", flush=True)
        return {}


def retired_in(facts_dir=FACTS, archive_dir=None):
    """ORDER 70: retirements() for the facts on disk (app's preamble, /mem, the nightly). Never
    raises: on any error, no retirements and one log line."""
    try:
        return retirements(_read_base(facts_dir, archive_dir))
    except Exception as e:
        print(f"HELIX: [facts] no retirements map this time — {type(e).__name__}: {e}", flush=True)
        return {"applied": [], "unresolved": []}


class BgeEmbedder:
    """bge-small-en-v1.5 through onnxruntime on the CPU. CLS pooling, L2-normalised (the model's
    own 1_Pooling config). Loaded once, on first use."""
    tag = "bge-small-en-v1.5@" + MODEL_REV[:12]

    def __init__(self, model_dir=MODEL_DIR, threads=4):
        import numpy as np, onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(f"{model_dir}/onnx/model.onnx", sess_options=so,
                                         providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(f"{model_dir}/tokenizer.json")
        self.tok.enable_truncation(max_length=512)
        self.raw = Tokenizer.from_file(f"{model_dir}/tokenizer.json")   # no truncation: chunking
        self.raw.no_truncation()
        self.raw.no_padding()

    def chunks(self, text, head):
        """~CHUNK_TOKENS-token windows of `text`, each prefixed with the fact's heading."""
        enc = self.raw.encode(text, add_special_tokens=False)
        offs = enc.offsets
        if not offs:
            return [head]
        out = []
        for i in range(0, len(offs), CHUNK_TOKENS):
            j = min(i + CHUNK_TOKENS, len(offs)) - 1
            body = text[offs[i][0]:offs[j][1]]
            out.append(body if i == 0 else f"{head}\n{body}")
        return out

    def embed(self, texts, query=False, batch=16):
        np = self.np
        if query:
            texts = [QUERY_PREFIX + t for t in texts]
        vecs = []
        for i in range(0, len(texts), batch):
            self.tok.enable_padding()
            encs = self.tok.encode_batch(texts[i:i + batch])
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            out = self.sess.run(None, {"input_ids": ids, "attention_mask": mask,
                                       "token_type_ids": np.zeros_like(ids)})[0]
            cls = out[:, 0, :]
            vecs.append(cls / np.linalg.norm(cls, axis=1, keepdims=True))
        return np.concatenate(vecs).astype(np.float32) if vecs else np.zeros((0, 384), np.float32)


class Index:
    """The fact base, indexed for all three arms. refresh() is cheap when nothing changed (a stat
    per file); a changed file is re-read, re-tokenized and, for the embed arms, re-embedded unless
    its sha256 is already in the vector cache."""

    def __init__(self, facts_dir=FACTS, cache_dir=CACHE_DIR, embedder=None, embedder_factory=None,
                 archive_dir=None, aliases_path=ALIASES, categories_py=CATEGORIES_PY,
                 categories_txt=CATEGORIES_TXT, assigned_path=None):
        self.facts_dir, self.cache_dir = facts_dir, cache_dir
        self.archive_dir = archive_dir or os.path.join(
            os.path.dirname(os.path.abspath(facts_dir)), "archive")
        self._embedder = embedder
        self._factory = embedder_factory or BgeEmbedder
        self.docs = {}                # name -> {sha, text, head, tf, n, stat, archived}
        self.vecs = {}                # name -> np.array (chunks x dim)
        self.df, self.avgdl = Counter(), 1.0
        self.embedded = 0             # facts actually embedded (not served from cache), lifetime
        self.sup = None               # ORDER 42c: supersession(), rebuilt when the base changes
        self.ret = None               # ORDER 70: retirements(), rebuilt with it
        # ORDER 74
        self.aliases_path = aliases_path
        self.alias, self.alias_unknown, self._alias_key = {}, [], False   # {name: [aliases]}
        self.atf = {}                 # name -> Counter of alias tokens, ALIAS_WEIGHT each
        self.adf, self.aavgdl = Counter(), 1.0
        self.avecs = {}               # name -> (sha256 of its alias chunk, np.array 1 x dim)
        self.categories_py, self.categories_txt, self.assigned_path = categories_py, categories_txt, assigned_path
        self._cat, self._cat_key, self._gen = None, None, 0
        self.lock = threading.RLock()

    # ---- the fact base
    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = self._factory()
        return self._embedder

    def _entries(self):
        """(DirEntry, archived) for every fact: the live ones, then the archive's (missing is
        fine). A name already live is not taken from the archive."""
        live = [(de, False) for de in os.scandir(self.facts_dir)]
        names = {de.name for de, _ in live}
        try:
            arch = [(de, True) for de in os.scandir(self.archive_dir) if de.name not in names]
        except FileNotFoundError:
            arch = []
        return live + arch

    def refresh(self, need_vectors=False):
        with self.lock:
            seen, changed = set(), False
            for de, archived in self._entries():
                if not de.name.endswith(".md") or not de.is_file():
                    continue
                st = de.stat()
                key = (st.st_mtime_ns, st.st_size)
                seen.add(de.name)
                d = self.docs.get(de.name)
                if d and d["archived"] != archived:      # moved: os.replace keeps the stat key
                    d["archived"] = archived
                    d["stat"] = None
                    self.sup = None                      # a glob's resolution reads `archived`
                if d and d["stat"] == key:
                    continue
                raw = open(de.path, "rb").read()
                sha = hashlib.sha256(raw).hexdigest()
                if d and d["sha"] == sha:
                    d["stat"] = key
                    continue
                text = raw.decode("utf-8", "replace").strip()
                tf = Counter(toks(de.name.replace("_", " ") + " " + text))
                self.docs[de.name] = {"sha": sha, "text": text, "head": heading(text), "tf": tf,
                                      "n": sum(tf.values()), "stat": key, "archived": archived}
                self.vecs.pop(de.name, None)
                changed = True
            for gone in set(self.docs) - seen:
                del self.docs[gone]
                self.vecs.pop(gone, None)
                changed = True
            if changed or not self.df:
                self.df = Counter()
                for d in self.docs.values():
                    self.df.update(d["tf"].keys())
                self.avgdl = (sum(d["n"] for d in self.docs.values()) / len(self.docs)) if self.docs else 1.0
            if changed or self.sup is None or self.ret is None:
                base = {fn: (d["text"], d["archived"]) for fn, d in self.docs.items()}
                self.sup = supersession(base)
                self.ret = retirements(base)
                self._gen += 1
            self._aliases(changed)
            if need_vectors:
                self._vectors()

    def _aliases(self, changed):
        """ORDER 74: re-merge the aliases when a fact or the sidecar changed (sidecar keyed by stat)."""
        try:
            st = os.stat(self.aliases_path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if not changed and key == self._alias_key:
            return
        self._alias_key = key
        self.alias, self.alias_unknown = merge_aliases({fn: d["text"] for fn, d in self.docs.items()},
                                                       read_alias_sidecar(self.aliases_path))
        self.atf = {}
        for fn, al in self.alias.items():
            c = Counter()
            for a in al:
                for w in toks(a):
                    c[w] += ALIAS_WEIGHT
            if c:
                self.atf[fn] = c
        self.adf = Counter()
        for fn, d in self.docs.items():
            self.adf.update((d["tf"] + self.atf.get(fn, Counter())).keys())
        self.aavgdl = ((sum(d["n"] + sum(self.atf.get(fn, Counter()).values()) for fn, d in self.docs.items())
                        / len(self.docs)) if self.docs else 1.0)
        for fn in set(self.avecs) - set(self.alias):
            del self.avecs[fn]

    def aliases_report(self):
        """ORDER 74: {"applied": {fact: [aliases]}, "unknown_files": [sidecar rows naming no fact]}."""
        with self.lock:
            return {"applied": {fn: list(a) for fn, a in self.alias.items()},
                    "unknown_files": list(self.alias_unknown)}

    def _alias_vectors(self, cdir):
        np = __import__("numpy")
        for fn, al in self.alias.items():
            chunk = alias_chunk(self.docs[fn]["head"], al)
            sha = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            if self.avecs.get(fn, (None,))[0] == sha:
                continue
            p = f"{cdir}/alias_{sha}.npy"
            try:
                v = np.load(p)
            except Exception:
                v = self.embedder.embed([chunk])
                tmp = f"{p}.tmp.{os.getpid()}.npy"
                np.save(tmp, v)
                os.replace(tmp, p)
            self.avecs[fn] = (sha, v)

    def _vectors(self):
        np = __import__("numpy")
        missing = [fn for fn in self.docs if fn not in self.vecs]
        stale = any(self.avecs.get(fn, (None,))[0] != hashlib.sha256(
            alias_chunk(self.docs[fn]["head"], al).encode("utf-8")).hexdigest() for fn, al in self.alias.items())
        if not missing and not stale:
            return
        cdir = f"{self.cache_dir}/{getattr(self.embedder, 'tag', 'embedder')}"
        os.makedirs(cdir, exist_ok=True)
        self._alias_vectors(cdir)
        for fn in missing:
            d = self.docs[fn]
            p = f"{cdir}/{d['sha']}.npy"
            try:
                self.vecs[fn] = np.load(p)
                continue
            except Exception:
                pass
            v = self.embedder.embed(self.embedder.chunks(d["text"], d["head"]))
            self.embedded += 1
            tmp = f"{p}.tmp.{os.getpid()}.npy"
            np.save(tmp, v)
            os.replace(tmp, p)
            self.vecs[fn] = v

    # ---- the arms (each returns {name: score} over the whole base)
    def bm25(self, text, aliases=False):
        q = toks(text)
        N = len(self.docs)
        df, avgdl = (self.adf, self.aavgdl) if aliases else (self.df, self.avgdl)
        out = {}
        for fn, d in self.docs.items():
            s = 0.0
            atf = self.atf.get(fn) if aliases else None
            n = d["n"] + sum(atf.values()) if atf else d["n"]
            for w in q:
                f = d["tf"].get(w, 0) + (atf.get(w, 0) if atf else 0)
                if not f:
                    continue
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * n / avgdl))
            out[fn] = s
        return out

    def embed_turns(self, turns, weights, aliases=False):
        np = __import__("numpy")
        qv = self.embedder.embed(list(turns), query=True)
        vecs = self.vecs
        if aliases and self.avecs:
            vecs = {fn: np.vstack([v, self.avecs[fn][1]]) if fn in self.avecs else v for fn, v in vecs.items()}
        newest = {fn: float(np.max(v @ qv[0])) for fn, v in vecs.items()}
        w = np.array(weights[:len(turns)], dtype=np.float32)[:, None]
        q = (qv * w).sum(0)
        q /= np.linalg.norm(q)
        return newest, {fn: float(np.max(v @ q)) for fn, v in vecs.items()}

    def _categories(self):
        """ORDER 74: (the category list, {fact: {its category names}}) via order 73's categories.load
        and category_of, by path; rebuilt when a fact, the list or ASSIGNED.csv changes. None when
        anything fails to load (no boost); never raises."""
        def mtime(p):
            try:
                return os.stat(p).st_mtime_ns
            except OSError:
                return None
        key = (self._gen, self.categories_py, mtime(self.categories_txt),
               mtime(self.assigned_path) if self.assigned_path else None)
        if key == self._cat_key:
            return self._cat
        self._cat_key = key
        try:
            spec = importlib.util.spec_from_file_location("_memretrieve_categories", self.categories_py)
            cm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cm)
            cats = cm.load(self.categories_txt)
            if not cats:
                raise ValueError(f"no categories in {self.categories_txt}")
            assigned = cm.load_assigned(self.assigned_path, cats)
            of = {fn: set(cm.category_of(fn, d["text"], cats, assigned)[0]) for fn, d in self.docs.items()}
            self._cat = (cats, of)
        except Exception as e:
            print(f"HELIX: [memretrieve] no category boost — {type(e).__name__}: {e}", flush=True)
            self._cat = None
        return self._cat

    def _boosted(self, turns):
        """ORDER 74: the facts whose category a query word names (distinctive words only)."""
        cat = self._categories()
        if not cat:
            return set()
        cats, of = cat
        hit = matched_categories(" ".join(turns), cats)
        return {fn for fn, names in of.items() if names & hit} if hit else set()

    # ---- the query
    def retrieve(self, turns, k=5, char_budget=6000, arm="hybrid", skip=(), floors=None, arms=None):
        """turns: a string, or the user's last turns NEWEST FIRST (up to 3 used).
        Returns {"hits": [(file, score, why)], "reason": str, "gate": {arm: newest top score},
        "replaced_by": {A: B}}. A hit's `why` ends with "full" (whole text fits the budget), "head"
        (heading + path) or "pointer" (ORDER 42c: archived or whole-superseded, never inlined). The
        first k ranked hits are picked exactly as before; a pointer's replacement B (newest of its
        chain) is then added right after it, unless already picked or in `skip`.
        ORDER 74: `arms` (default DEFAULT_ARMS) switches on "aliases", "adapt" and "catboost"; with
        arms=() the result is exactly the one before order 74."""
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}")
        arms = DEFAULT_ARMS if arms is None else tuple(arms)
        if set(arms) - set(ALL_ARMS):
            raise ValueError(f"arms must be among {ALL_ARMS}")
        al = "aliases" in arms
        turns = [turns] if isinstance(turns, str) else [t for t in turns if t is not None]
        turns = [t for t in turns[:len(TURN_WEIGHTS)]] or [""]
        floors = dict(FLOORS, **(floors or {}))
        weights = TURN_WEIGHTS
        with self.lock:
            self.refresh(need_vectors=arm != "bm25")
            if not self.docs:
                return {"hits": [], "reason": "no facts", "gate": {}, "replaced_by": {}}
            ranked, gate, scores = {}, {}, {}
            dw = {fn: ARCHIVE_WEIGHT if d["archived"] else 1.0 for fn, d in self.docs.items()}
            if arm in ("bm25", "hybrid"):
                per = [self.bm25(t, aliases=al) for t in turns]
                gate["bm25"] = max(per[0].values())
                s = {fn: dw[fn] * sum(w * p[fn] for w, p in zip(weights, per)) for fn in self.docs}
                if "catboost" in arms:
                    boost = self._boosted(turns)
                    s = {fn: v * CATEGORY_BOOST if fn in boost else v for fn, v in s.items()}
                scores["bm25"] = s
                ranked["bm25"] = sorted(s, key=lambda f: (-s[f], f))
            if arm in ("embed", "hybrid"):
                newest, s = self.embed_turns(turns, weights, aliases=al)
                gate["embed"] = max(newest.values())
                s = {fn: dw[fn] * v for fn, v in s.items()}
                scores["embed"] = s
                ranked["embed"] = sorted(s, key=lambda f: (-s[f], f))
            opened = [a for a in gate if gate[a] > floors[a]]
            if not opened:
                return {"hits": [], "reason": "below floor", "gate": gate, "replaced_by": {}}
            if arm == "hybrid":
                pos = {a: {f: i + 1 for i, f in enumerate(ranked[a])} for a in ranked}
                rrf = {f: sum(1.0 / (RRF_K + pos[a][f]) for a in ranked) for f in self.docs}
                order = sorted(rrf, key=lambda f: (-rrf[f], f))
                final = rrf
                why_of = lambda f: (f"hybrid rrf {rrf[f]:.4f} (bm25 #{pos['bm25'][f]} {scores['bm25'][f]:.2f}, "
                                    f"embed #{pos['embed'][f]} {scores['embed'][f]:.3f})")
            else:
                order, final = ranked[arm], scores[arm]
                why_of = lambda f: f"{arm} {final[f]:.3f}"
            picked = []
            skip = set(skip or ())
            for f in order:
                if len(picked) >= k:
                    break
                if f in skip:
                    continue
                picked.append(f)
            extra = []
            if "adapt" in arms and k > 0 and len(picked) == k and final[picked[-1]] > 0:
                nxt = [f for f in order[order.index(picked[-1]) + 1:] if f not in skip][:ADAPT_MAX]
                extra = [f for f in nxt if final[f] >= ADAPT_RATIO * final[picked[-1]]]
            pointer = lambda f: self.docs[f]["archived"] or self.replacement_of(f) is not None
            rows, sent, replaced_by = [], set(picked), {}
            for f in picked:
                rows.append((f, None))
                b = self.replacement_of(f)
                if b:
                    replaced_by[f] = b
                    if b not in sent and b not in skip:
                        rows.append((b, f))
                        sent.add(b)
            for f in extra:
                if f not in sent:
                    rows.append((f, "adapt"))
                    sent.add(f)
            hits, used = [], 0
            for f, for_ in rows:
                label = " (archived)" if self.docs[f]["archived"] else ""
                if for_ == "adapt":           # ORDER 74: a head only; a pointer stays a pointer
                    mode = f"close to #{k}; pointer" if pointer(f) else f"head (close to #{k})"
                    hits.append((f, round(final[f], 4), f"{why_of(f)}{label}; {mode}"))
                    continue
                n = len(self.docs[f]["text"])
                mode = "pointer" if pointer(f) else "full" if used + n <= char_budget else "head"
                if mode == "full":
                    used += n
                also = f"; replacement for {for_}" if for_ else ""
                hits.append((f, round(final[f], 4), f"{why_of(f)}{label}{also}; {mode}"))
            return {"hits": hits, "reason": "ok" if hits else "all skipped", "gate": gate,
                    "replaced_by": replaced_by}

    def query(self, text, k=5, char_budget=6000, arm="hybrid", skip=()):
        return self.retrieve(text, k=k, char_budget=char_budget, arm=arm, skip=skip)["hits"]

    def text_of(self, name):
        return self.docs[name]["text"]

    def head_of(self, name):
        return self.docs[name]["head"]

    def replacement_of(self, name):
        """ORDER 42c: the newest fact replacing `name` (whole supersession, chains followed), or None."""
        return (self.sup or {}).get("replaced", {}).get(name)

    def partial_of(self, name):
        """ORDER 42c: the facts that supersede part of `name`, by name."""
        return list((self.sup or {}).get("partial", {}).get(name, ()))

    def apply_retirements(self, name, text=None):
        """ORDER 70: `name`'s text as shown, its retired passages marked (apply_retirements())."""
        return apply_retirements(name, self.text_of(name) if text is None else text, self.ret)

    def where_of(self, name):
        """The path the patch shows, and its label: memory/archive/ for an archived fact."""
        if self.docs[name].get("archived"):
            return f"memory/archive/{name}", " (archived)"
        return f"memory/facts/{name}", ""


PATCH_HEADER = "=== MEMORY PATCH (facts likely relevant to this turn — open in full before citing) ==="


def render_patch(index, hits):
    """The block appended after the user's text. Full text for hits marked `full`, otherwise the
    heading and the path. Empty string for no hits. ORDER 42c: a `pointer` hit is one line and
    never its body (whole-superseded: the replacement's path; archived: its heading); a `full` hit
    that newer facts supersede in part gets one warning line per such fact above its text.
    ORDER 70: a `full` hit's text is shown with its retired passages marked."""
    if not hits:
        return ""
    parts = [PATCH_HEADER]
    for fn, _score, why in hits:
        path, label = index.where_of(fn)
        if why.endswith("; pointer"):
            b = index.replacement_of(fn)
            if b:
                bpath, blabel = index.where_of(b)
                parts.append(f"- {path}{label} — SUPERSEDED by {bpath}{blabel} (newer, used instead)")
            else:
                parts.append(f"- {path}{label} — {index.head_of(fn)}")
        elif why.endswith("full"):
            warn = "".join(f"⚠ newer: `{b}` supersedes part of this — where they disagree, {b} wins.\n"
                           for b in index.partial_of(fn))
            parts.append(f"# {path}{label}\n{warn}{index.apply_retirements(fn)}")
        else:
            parts.append(f"- {path}{label} — {index.head_of(fn)} (not inlined: over the patch budget; open it)")
    return "\n\n".join(parts)


_DEFAULT = None
_DEFAULT_LOCK = threading.Lock()


def default_index():
    """One process-wide index (the model loads once, lazily)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Index()
        return _DEFAULT


def query(text, k=5, char_budget=6000, arm="hybrid", skip=()):
    """Module-level form of Index.query on the live fact base: [(file, score, why)]."""
    return default_index().query(text, k=k, char_budget=char_budget, arm=arm, skip=skip)


if __name__ == "__main__":
    import sys
    t0 = time.time()
    arm = os.environ.get("ARM", "hybrid")
    for h in query(sys.argv[1:] and " ".join(sys.argv[1:]) or "how do I start the local picture generator", arm=arm):
        print(h)
    print(f"{time.time() - t0:.2f}s")
