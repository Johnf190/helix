#!/usr/bin/env python3
"""mindgraph.py — ORDER 58. Helix's memory as a graph: the data behind v7's orb.

One node per memory/facts/*.md, one undirected edge per cross-reference between two facts, laid
out in 3-D by a deterministic force layout. Pure: it reads the facts dir (and, for recalls, the
memory-patch ledger) and writes nothing.

The extraction and the layout are the ones that produced the user's approved mockup
(workspace/ui_lab/helix_mind_v5/APPROVED_by_User_2026-10-04.html), promoted under the rules of
fact 101: every tunable is a named field of MindOptions, and the result carries its own
diagnostics.

    build_graph(opts) -> MindGraph          cached per (facts hash, list mtime, ASSIGNED mtime, options)
    recalls(patches_path, since_ts) -> {"recalls": [{ts, keys}], "skipped": n}
"""
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Filename keyword → category. Copied verbatim from ORDER 58's appendix (the mockup's
# extraction): the filename is lowercased, the first match wins, the default is helix.
CATEGORIES = [
    ("helix", r"helix|order|factgate|hook|build|vault|heartbeat|memory_retrieval|ultracode|model|phone|alice|odin|rule_|preference|promotion|canonical_ui|ui_|lanes|proposal|upgrade|wall|regate|seam|claim|crosscheck|catches|nightly|restart"),
    ("research", r"research|study|experiment|thesis|framework|attest|probe|ratio|observer|index|discipline|corrections|inventory|body_of_work|box2_"),
    ("finance", r"finance|loan|budget|fund|bank|savings|credential|security"),
    ("music", r"music|musicapp|song|chord|studio|lyrics"),
    ("mapapp", r"mapapp|home|map|bird|poster|friend|north|archive"),
    ("making", r"boat|dinghy|frames|workshop"),
    ("life", r"user|family|sidekick|email|agent_program|lead_|field|box1|hdd|machines|desktop"),
]
CAT_NAMES = [c for c, _ in CATEGORIES]
DEFAULT_CAT = "helix"
# ORDER 65 (item 28): a fact that declares "Category: <name>" (workspace/categories) sits in that
# category's lobe, cat_source "declared"; every other fact keeps the filename guess above.
CAT_SOURCE = "filename-guess"
CAT_DECLARED = "declared"
CATEGORIES_PY = Path(__file__).resolve().parent / "workspace" / "categories" / "categories.py"
CATEGORIES_FILE = CATEGORIES_PY.parent / "CATEGORIES.txt"
# ORDER 73b: a fact with no valid declared line takes its category from the sidecar ASSIGNED.csv
# (written only by `categories.py assign`), cat_source "assigned". A declared line still wins.
CAT_ASSIGNED = "assigned"
ASSIGNED_DEFAULT = CATEGORIES_PY.parent / "ASSIGNED.csv"
ASSIGNED_FILE = ASSIGNED_DEFAULT
ASSIGNED_ABSENT = "absent"          # the cache-key token while ASSIGNED.csv does not exist

PREFIX = re.compile(r"(box2_)?(\d+)_")
REF = re.compile(r"(?<![\d.])(\d{1,3})_(?:\*|[a-z])")


@dataclass(frozen=True)
class MindOptions:
    facts_dir: Path
    seed: int = 7                   # layout RNG seed
    iterations: int = 600           # force-layout steps
    repulsion: float = 4e-5         # inverse-square push between every pair
    spring_length: float = 0.15     # rest length of a link
    spring_k: float = 0.02          # link stiffness
    lobe_pull: float = 0.04         # pull of each node toward its category's centre
    lobe_radius: float = 0.5        # category centres sit on a sphere of this radius
    confine_radius: float = 0.88    # nodes are held inside this radius during the layout
    scale_percentile: float = 97    # after centring, this percentile of radii …
    scale_to: float = 0.84          # … is scaled to this radius
    clamp_radius: float = 0.9       # and nothing ends outside this one
    init_spread: float = 0.1        # sd of the initial scatter around the lobe centre
    step_clip: float = 0.03         # max move per axis per step
    dist_floor: float = 1e-2        # added to pair distances so the repulsion stays finite
    title_max: int = 90             # chars kept of a fact's first line


@dataclass
class MindGraph:
    keys: list            # filename without .md, sorted
    n: list               # numeric prefix or ""
    t: list               # first line, '#' and spaces stripped, <= title_max
    c: list               # index into cats
    cat_source: list      # per node; "declared", "assigned" or "filename-guess"
    p: list               # [[x, y, z]] rounded to 3 dp
    edges: list           # [(a, b)] a < b, sorted
    degree: list
    cats: list = field(default_factory=lambda: list(CAT_NAMES))
    diagnostics: dict = field(default_factory=dict)

    def to_json(self):
        """The shape the mockup's orb reads (p c n t e d cats), plus keys and diagnostics."""
        return {"p": self.p, "c": self.c, "n": self.n, "t": self.t,
                "e": [list(e) for e in self.edges], "d": self.degree, "cats": self.cats,
                "k": self.keys, "cat_source": self.cat_source, "diagnostics": self.diagnostics}


def category(filename):
    low = filename.lower()
    for name, rx in CATEGORIES:
        if re.search(rx, low):
            return name
    return DEFAULT_CAT


def _declared_lobe():
    """(filename, text) -> (lobe name, cat_source) or (None, None), from the fact's declared
    category, else its ASSIGNED.csv row. If the categories module or its list cannot be loaded,
    every fact falls back to the filename guess."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("helix_fact_categories", CATEGORIES_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cats = mod.load(CATEGORIES_FILE)
        lobe = {c.name: c.lobe for c in cats if c.lobe in CAT_NAMES}
    except Exception:
        return lambda name, text: (None, None)
    try:
        assigned = mod.load_assigned(ASSIGNED_FILE, cats)
    except Exception:
        assigned = {}

    def find(name, text):
        try:
            names, src = mod.category_of(name, text, cats, assigned)
            return (lobe.get(names[0]), src) if names and lobe.get(names[0]) else (None, None)
        except Exception:
            return None, None
    return find


def _list_mtime():
    try:
        return os.stat(CATEGORIES_FILE).st_mtime_ns
    except OSError:
        return None


def _assigned_mtime():
    try:
        return os.stat(ASSIGNED_FILE).st_mtime_ns
    except OSError:
        return ASSIGNED_ABSENT


def _fact_files(facts_dir):
    """[(filename, mtime_ns)] of every *.md directly in facts_dir, sorted by name."""
    out = []
    with os.scandir(facts_dir) as it:
        for e in it:
            if e.name.endswith(".md") and e.is_file():
                out.append((e.name, e.stat().st_mtime_ns))
    return sorted(out)


def facts_hash(files):
    h = hashlib.sha256()
    for name, mtime in files:
        h.update(f"{name}\0{mtime}\n".encode())
    return h.hexdigest()


def _extract(facts_dir, names, opts):
    """Nodes, edges and the bookkeeping that becomes diagnostics. No layout."""
    keys, nums, titles, cats, box2 = [], [], [], [], []
    texts, sources = [], []
    declared_lobe = _declared_lobe()
    for f in names:
        txt = (Path(facts_dir) / f).read_text(errors="ignore")
        texts.append(txt)
        m = PREFIX.match(f)
        keys.append(f[:-3])
        nums.append(m.group(2) if m else "")
        box2.append(bool(m and m.group(1)))
        first = txt.split("\n", 1)[0]
        titles.append(first.strip("# \n\r")[:opts.title_max])
        lobe, src = declared_lobe(f, txt)
        cats.append(CAT_NAMES.index(lobe or category(f)))
        sources.append(src if lobe else CAT_SOURCE)
    bynum, holders = {}, {}
    for i, num in enumerate(nums):
        if num and not box2[i]:
            bynum.setdefault(num, i)                 # first by sort order wins a collision
            holders.setdefault(num, []).append(keys[i])
    collisions = [{"prefix": num, "files": fs, "used": keys[bynum[num]]}
                  for num, fs in sorted(holders.items()) if len(fs) > 1]
    edges, cited = set(), set()
    for i, txt in enumerate(texts):
        for r in set(REF.findall(txt)):
            j = bynum.get(r)
            if j is not None and j != i:
                edges.add(tuple(sorted((i, j))))
                cited.add(j)
    return keys, nums, titles, cats, sources, sorted(edges), collisions, len(keys) - len(cited)


def _layout(ci, edges, opts):
    """The mockup's force layout (numpy), then centred and scaled. Deterministic per seed."""
    N, K = len(ci), len(CAT_NAMES)
    if N == 0:
        return np.zeros((0, 3))
    ci = np.asarray(ci)
    E = np.asarray(edges, dtype=int).reshape(-1, 2)
    rng = np.random.default_rng(opts.seed)
    cen = []
    for i in range(K):                               # category centres: a Fibonacci sphere
        y = 1 - 2 * (i + .5) / K
        r = np.sqrt(1 - y * y)
        th = i * 2.399963
        cen.append([np.cos(th) * r, y, np.sin(th) * r])
    cen = np.array(cen) * opts.lobe_radius
    P = cen[ci] + rng.normal(0, opts.init_spread, (N, 3))
    for _ in range(opts.iterations):
        D = P[:, None, :] - P[None, :, :]
        dist = np.linalg.norm(D, axis=2) + opts.dist_floor
        F = (D / dist[..., None] ** 3).sum(1) * opts.repulsion
        if len(E):
            a, b = E[:, 0], E[:, 1]
            v = P[b] - P[a]
            L = np.linalg.norm(v, axis=1, keepdims=True) + 1e-6
            f = (L - opts.spring_length) * v / L * opts.spring_k
            np.add.at(F, a, f)
            np.add.at(F, b, -f)
        F += (cen[ci] - P) * opts.lobe_pull
        P = P + np.clip(F, -opts.step_clip, opts.step_clip)
        r = np.linalg.norm(P, axis=1, keepdims=True)
        P = np.where(r > opts.confine_radius, P / np.maximum(r, 1e-12) * opts.confine_radius, P)
    P = P - P.mean(0)
    r = np.linalg.norm(P, axis=1)
    q = float(np.percentile(r, opts.scale_percentile))
    if q > 0:
        P = P * (opts.scale_to / q)
    r = np.linalg.norm(P, axis=1, keepdims=True)
    return np.where(r > opts.clamp_radius, P / np.maximum(r, 1e-12) * opts.clamp_radius, P)


def _compute(opts, files, fh):
    names = [n for n, _ in files]
    keys, nums, titles, cats, sources, edges, collisions, unreferenced = _extract(opts.facts_dir, names, opts)
    t0 = time.perf_counter()
    P = _layout(cats, edges, opts)
    layout_ms = round((time.perf_counter() - t0) * 1000, 1)
    deg = [0] * len(keys)
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1
    counts = {c: 0 for c in CAT_NAMES}
    for c in cats:
        counts[CAT_NAMES[c]] += 1
    diag = {"nodes": len(keys), "edges": len(edges), "cats": counts, "layout_ms": layout_ms,
            "prefix_collisions": collisions, "unreferenced_count": unreferenced,
            "facts_hash": fh}
    return MindGraph(keys=keys, n=nums, t=titles, c=cats, cat_source=sources,
                     p=np.round(P, 3).tolist(), edges=[tuple(e) for e in edges], degree=deg,
                     diagnostics=diag)


# Cache: one graph per (facts hash, category list mtime, ASSIGNED.csv mtime, options). A new or
# touched fact, an edited category list, or a new `assign` changes the key, so the next request
# lays out afresh; an unchanged tree never pays for the layout twice.
_CACHE = {}
_CACHE_MAX = 4
_LOCK = threading.Lock()


def clear_cache():
    with _LOCK:
        _CACHE.clear()


def build_graph(opts):
    files = _fact_files(opts.facts_dir)
    fh = facts_hash(files)
    key = (fh, _list_mtime(), _assigned_mtime(), opts)
    with _LOCK:                    # held through the compute, so two requests never both lay out
        g = _CACHE.get(key)
        if g is None:
            g = _compute(opts, files, fh)
            while len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = g
        return g


def _parse_ts(s):
    """ISO-8601 (a trailing Z allowed) → aware datetime. ValueError if it is not one."""
    if not isinstance(s, str):
        raise ValueError(f"not a timestamp: {s!r}")
    d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def recalls(patches_path, since_ts=None):
    """The facts each turn's memory patch put in context, from PATCHES.jsonl (read-only).

    Entries strictly after `since_ts` (ISO-8601) when given. A line that is not a JSON object
    with a parseable `ts` and a `files` list is skipped and counted. A bad `since_ts` raises
    ValueError; a missing ledger is an empty result."""
    since = _parse_ts(since_ts) if since_ts is not None else None
    out, skipped = [], 0
    try:
        fh = open(patches_path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"recalls": [], "skipped": 0}
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ts = rec["ts"]
                when = _parse_ts(ts)
                files = rec["files"]
                if not isinstance(files, list):
                    raise ValueError("files is not a list")
                keys = []
                for f in files:
                    name = f.get("file") if isinstance(f, dict) else None
                    if isinstance(name, str) and name:
                        keys.append(name[:-3] if name.endswith(".md") else name)
            except (ValueError, KeyError, TypeError, AttributeError):
                skipped += 1
                continue
            if since is None or when > since:
                out.append({"ts": ts, "keys": keys})
    return {"recalls": out, "skipped": skipped}
