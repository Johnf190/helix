#!/usr/bin/env python3
"""propose_backfill.py — ORDER 65 Part D. PROPOSES a category for every fact; writes no fact.

Reads memory/facts/*.md READ-ONLY and writes, beside this file (never under memory/):
    BACKFILL_PROPOSAL.csv   file, primary, secondary, reason, confidence (high/low)
    BACKFILL_PROPOSAL.md    counts per category, then the LOW-confidence rows (the user looks at
                            those), then the rest

Rule-based only: no model, no network. Evidence, in order of weight:
    filename words (3 each) · title-line words (1 each) · the categories of the facts it cites
    (`NN_*`, mindgraph.REF; 1 per citation, at most 3)
A category's vocabulary is the words of its own line in CATEGORIES.txt (the part of the name
after ':' and its description), widened by SYNONYMS below. SYNONYMS is keyed by description
words, never by category names: CATEGORIES.txt stays the only place a name is written.

    python3 propose_backfill.py [--facts DIR] [--out DIR] [--list FILE]
"""
import argparse
import csv
import io
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN = HERE.parents[1]
sys.path.insert(0, str(HERE))
import categories  # noqa: E402

try:                                    # the fallback guess and the citation pattern are mindgraph's
    sys.path.insert(0, str(BRAIN))
    import mindgraph
    REF, PREFIX, lobe_guess = mindgraph.REF, mindgraph.PREFIX, mindgraph.category
except Exception:                       # pragma: no cover - mindgraph needs numpy
    mindgraph = None
    REF = re.compile(r"(?<![\d.])(\d{1,3})_(?:\*|[a-z])")
    PREFIX = re.compile(r"(box2_)?(\d+)_")
    lobe_guess = None

W_FILENAME, W_TITLE, W_CITE, MAX_CITE = 3, 1, 1, 3
HIGH_MARGIN = 2          # high confidence: a filename hit, and ahead of the runner-up by this much
SECONDARY_MIN = 3        # a secondary needs at least one filename hit's worth of evidence
MIN_STEM = 4             # 'rule' matches 'rules'; shorter words must match exactly

# Description words → more words that mean the same thing in this corpus' filenames and titles.
SYNONYMS = {   # description words -> words that mean the same in YOUR filenames and titles. Grow it.
    "boat": ["boat", "frames", "hull"],
    "budget": ["fund", "portfolio", "household", "savings", "loan"],
    "preferences": ["rule", "preference", "discipline", "standing", "wants", "doctrine", "council"],
    "helix": ["order", "orders", "hook", "vault", "ui", "phone", "model", "claude", "builder",
              "nightly", "restart", "seam", "claim", "lanes", "proposal", "promotion", "memory",
              "backup", "queue", "heartbeat", "wall", "regate", "preflight", "image", "repo"],
    "math": ["ratio", "lemma", "proof", "theorem"],
    "music": ["song", "songs", "lyrics", "mixing", "mastering", "music"],
    "family": ["children", "family"],
}
# Description words too common to be evidence for one category.
STOP = {"and", "the", "for", "their", "its", "his", "her", "of", "on", "home", "life", "app",
        "site", "tests", "results", "standard", "kill", "pile", "arc", "network", "security",
        "research", "project", "builds", "machines", "infra", "proven", "audits", "loans",
        "car", "savings", "fund", "songs", "market", "program", "paths", "agent", "chain"}
# Files the build protocol forbids opening: proposed from the filename alone.
NEVER_OPEN = re.compile(r"credential|accounts|\.env", re.I)


def words(s):
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w and not w.isdigit()]


def vocabulary(cats):
    """{category name: set of words}."""
    out = {}
    for c in cats:
        tail = c.name.split(":", 1)[-1]
        base = {w for w in words(tail + " " + c.desc) if len(w) >= 2 and w not in STOP}
        for w in list(base):
            base.update(SYNONYMS.get(w, []))
        out[c.name] = base
    return out


def _match(token, vocab):
    if token in vocab:
        return True
    if len(token) < MIN_STEM:
        return False
    # 'rules' ~ 'rule', 'posters' ~ 'poster'; a word is never taken as the start of a longer one
    # ('cross' is not 'crosscheck') beyond a plural 's'
    return any(len(v) >= MIN_STEM and (token.startswith(v) or (v.startswith(token) and len(v) - len(token) <= 1))
               for v in vocab)


def score_words(tokens, vocab, weight):
    sc, hits = Counter(), {}
    for t in tokens:
        for name, vs in vocab.items():
            if _match(t, vs):
                sc[name] += weight
                hits.setdefault(name, []).append(t)
    return sc, hits


def title_of(text):
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("---"):
            return s.lstrip("# ").strip()
    return ""


def read_fact(path):
    """The fact's text, opened "r" only. Credential files are never opened."""
    if NEVER_OPEN.search(path.name):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _fallback(name, cats):
    """mindgraph's filename lobe → the category of that lobe whose name is the lobe, else the first."""
    if lobe_guess is None or not cats:
        return cats[0].name if cats else ""
    lobe = lobe_guess(name)
    same = [c for c in cats if c.lobe == lobe]
    for c in same:
        if c.name == lobe:
            return c.name
    return same[0].name if same else cats[0].name


def _is_kind(name):
    return ":" not in name


def guess(name, text, cats, *, cites=None, vocab=None, why=None):
    """(primary, secondary, "high"|"low") for one fact, from its filename, its title, its own
    `Category:` line and `cites` (a Counter of the categories of the facts it cites; propose()
    passes it, a lone caller need not). `why`, if given, is a list the reasons are appended to.
    text None means the file was not opened."""
    vocab = vocabulary(cats) if vocab is None else vocab
    why = [] if why is None else why
    decl = categories.declared_all(text, cats)[0] if text else []
    if decl:
        why.append("already declares Category:")
        return decl[0], (decl[1] if len(decl) > 1 else ""), "high"
    fs, fh = score_words(words(name[:-3] if name.endswith(".md") else name), vocab, W_FILENAME)
    ts, th = score_words(words(title_of(text)) if text else [], vocab, W_TITLE)
    cs = Counter({k: min(v, MAX_CITE) for k, v in (cites or Counter()).items()})
    total = fs + ts + cs
    if total:
        ranked = sorted(total.items(), key=lambda kv: (-kv[1], -fs.get(kv[0], 0),
                                                       [c.name for c in cats].index(kv[0])))
        prim, ps = ranked[0]
        second = ranked[1] if len(ranked) > 1 else (None, 0)
        high = fs.get(prim, 0) > 0 and ps >= second[1] + HIGH_MARGIN
        sec = ""
        for other, s in ranked[1:]:
            if s >= SECONDARY_MIN and _is_kind(other) != _is_kind(prim):
                sec = other
                break
        for n in [prim] + ([sec] if sec else []):
            bits = []
            if fh.get(n):
                bits.append("filename " + "+".join(dict.fromkeys(fh[n])))
            if th.get(n):
                bits.append("title " + "+".join(dict.fromkeys(th[n])))
            if cs.get(n):
                bits.append(f"cites {cs[n]}")
            why.append(f"{n}: " + ", ".join(bits))
        if second[0] and not high:
            why.append(f"runner-up {second[0]} ({second[1]} vs {ps})")
    else:
        prim, sec, high = _fallback(name, cats), "", False
        why.append("no keyword or citation evidence; mindgraph's filename lobe")
    if text is None:
        high = False
        why.append("not opened (credentials file)")
    return prim, sec, "high" if high else "low"


def propose(facts_dir, cats):
    """[{file, primary, secondary, reason, confidence}] for every *.md in facts_dir, sorted."""
    facts_dir = Path(facts_dir)
    vocab = vocabulary(cats)
    files = sorted(p for p in facts_dir.iterdir() if p.suffix == ".md" and p.is_file())
    first = {}
    for p in files:
        text = read_fact(p)
        stem = p.name[:-3]
        fs, fh = score_words(words(stem), vocab, W_FILENAME)
        tt = words(title_of(text)) if text else []
        ts, th = score_words(tt, vocab, W_TITLE)
        decl = categories.declared_all(text, cats)[0] if text else []
        first[p.name] = {"text": text, "fs": fs, "fh": fh, "ts": ts, "th": th, "decl": decl}
    # what each prefix resolves to, as mindgraph resolves it: first non-box2 file by sort order
    bynum = {}
    for p in files:
        m = PREFIX.match(p.name)
        if m and not m.group(1):
            bynum.setdefault(m.group(2), p.name)

    def kw_primary(n):
        e = first[n]
        if e["decl"]:
            return e["decl"][0]
        tot = e["fs"] + e["ts"]
        return tot.most_common(1)[0][0] if tot and e["fs"] else None

    rows = []
    for p in files:
        e = first[p.name]
        cs = Counter()
        if e["text"] and not e["decl"]:
            for r in REF.findall(e["text"]):
                j = bynum.get(r)
                if j and j != p.name:
                    c = kw_primary(j)
                    if c:
                        cs[c] += W_CITE
        why = []
        prim, sec, conf = guess(p.name, e["text"], cats, cites=cs, vocab=vocab, why=why)
        rows.append({"file": p.name, "primary": prim, "secondary": sec,
                     "reason": "; ".join(why), "confidence": conf})
    return rows


def _refuse_memory(path):
    """No output may land under a memory/ directory (the facts live there)."""
    p = Path(path).resolve()
    if "memory" in p.parts:
        raise PermissionError(f"refusing to write under memory/: {p}")
    return p


def write_out(rows, out_dir, cats):
    out = _refuse_memory(Path(out_dir))
    csv_p, md_p = _refuse_memory(out / "BACKFILL_PROPOSAL.csv"), _refuse_memory(out / "BACKFILL_PROPOSAL.md")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["file", "primary", "secondary", "reason", "confidence"])
    w.writeheader()
    w.writerows(rows)
    csv_p.write_text(buf.getvalue(), encoding="utf-8")
    counts = Counter(r["primary"] for r in rows)
    low = [r for r in rows if r["confidence"] == "low"]
    high = [r for r in rows if r["confidence"] == "high"]
    md = ["# Category backfill — PROPOSAL (ORDER 65). No fact has been written.", "",
          f"{len(rows)} facts: {len(high)} high, {len(low)} low confidence. "
          "Phase B writes the lines through factgate only after the user has read this list.", "",
          "## Proposed primary category, per category", "", "| category | lobe | facts |",
          "|---|---|---|"]
    for c in cats:
        md.append(f"| {c.name} | {c.lobe} | {counts.get(c.name, 0)} |")
    for title, rs in (("## LOW confidence — the user, these are the ones to look at", low),
                      ("## High confidence", high)):
        md += ["", title, "", "| file | primary | secondary | reason |", "|---|---|---|---|"]
        for r in rs:
            md.append(f"| {r['file']} | {r['primary']} | {r['secondary']} | "
                      f"{r['reason'].replace('|', '/')} |")
    md_p.write_text("\n".join(md) + "\n", encoding="utf-8")
    return csv_p, md_p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--facts", default=str(BRAIN / "memory" / "facts"))
    ap.add_argument("--out", default=str(HERE))
    ap.add_argument("--list", default=str(categories.DEFAULT))
    a = ap.parse_args(argv)
    _refuse_memory(a.out)                       # before anything is read
    cats = categories.load(a.list)
    for ln, raw, why in categories.load_errors:
        print(f"list line {ln} skipped: {why}", file=sys.stderr)
    if not cats:
        print("no categories loaded", file=sys.stderr)
        return 2
    rows = propose(a.facts, cats)
    write_out(rows, a.out, cats)
    h = sum(r["confidence"] == "high" for r in rows)
    print(f"proposed {len(rows)} facts: {h} high, {len(rows) - h} low confidence; 0 fact files written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
