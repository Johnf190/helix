#!/usr/bin/env python3
"""ANCHOR — the Example framework, turned on a body of claims.

The thesis that survived six months of falsification is this: a system that conceals its state
must still commit somewhere the lie cannot reach, and reading THERE catches what surface
inspection misses — with a measured false-negative rate.

Every attempt to aim that at nature died out of sample (velocity, silence, R_I, kappa). Every
instance that held was aimed at something that could LIE: a forged telemetry channel, a
sandbagging model, an exchange covering a hole. Nature does not conceal. Agents do — and so do
records written by agents, including one's own.

So: a claim is a surface. The artifact it would have to commit to, if it were true, is the
forced commitment. ANCHOR extracts the claims, names the commitment each one is forced to,
checks it, and reports three numbers — anchored, broken, and the share it could not check at
all. That last number is the instrument's own epsilon, and it is printed, not hidden.

No dependencies. Reads only; never edits the corpus it audits.
"""
from __future__ import annotations
import dataclasses, hashlib, json, os, pathlib, re, subprocess, sys, time
from typing import Callable

HOME = pathlib.Path.home()
HELIX = HOME / "helix"

# ---------------------------------------------------------------- claim model
@dataclasses.dataclass
class Claim:
    kind: str           # path | cite | git | count | quote | url | verdict | dead
    text: str           # the claim as written, trimmed
    token: str          # the thing to check (a path, a sha, a number, a phrase)
    file: str           # where the claim lives
    line: int
    verdict: str = "pending"   # ok | broken | unanchorable | skipped
    detail: str = ""
    weight: float = 1.0        # load-bearing score, filled in later

    @property
    def key(self):
        return hashlib.sha1(f"{self.kind}|{self.token}|{self.file}|{self.line}".encode()).hexdigest()[:12]

    def as_dict(self):
        d = dataclasses.asdict(self); d["key"] = self.key; return d


# ---------------------------------------------------------------- extraction
# Each pattern answers one question: what artifact would have to exist for this to be true?
RE_CODE  = re.compile(r"`([^`\n]{2,120})`")
RE_PATH  = re.compile(r"(?<![\w/])((?:~|\.{0,2}/)?(?:[\w.\-]+/){0,8}[\w.\-]+\.(?:py|md|json|js|jsonl|html|csv|txt|sh|zip|yaml|yml|pdf|stl))(?![\w/])")
RE_DIR   = re.compile(r"(?<![\w/])((?:~|\.{0,2}/)?(?:[\w.\-]+/){1,8})(?=[\s`,;)\]]|$)")
RE_CITE  = re.compile(r"\[\[([\w.\-]+)\]\]")
RE_FACT  = re.compile(r"(?<![\w/])(facts/[\w.\-]+\.md)")
RE_GIT   = re.compile(r"(?:commit|HEAD|sha)\s*\**\s*`?([0-9a-f]{7,40})`?\**", re.I)
RE_URL   = re.compile(r"https?://[^\s)\"'\]<>]+")
RE_COUNT = re.compile(r"(?<![\w.])(\d[\d.,]{0,12})\s+([A-Za-zÆØÅæøå][\w æøåÆØÅ-]{2,28}?)(?=[\s.,;:)\]]|$)")
# A number is only a CLAIM when it counts an artifact somebody could recount. Everything else is
# prose, and pretending otherwise would drown the epsilon in noise.
COUNTABLE = {
 "fact files","facts","fact-files","files","documents","aircraft","luftfartøjer","registreringer",
 "registrations","unique registrations","photographs","fotografier","fotos","photos","owners",
 "ejere","makers","fabrikker","manufacturers","models","modeller","checks","checks green","tests",
 "tests green","rows","entries","chains","commits","words","ord","lines","pages","pp","airframes",
 "operators","operatører","types","typer","paragraphs","afsnit","dated rows","sources","claims",
}
RE_QUOTE = re.compile(r"[*_]*[\"“»]([^\"”«\n]{12,240})[\"”«][*_]*")
VERDICT_WORDS = re.compile(r"\b(PASS|KILL|KILLED|FALSIFIED|CONFIRMED|OVERTURNED|PROVEN|VERIFIED)\b")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "fotos", "photos", "shots"}


def read_lines(p: pathlib.Path):
    try:
        return p.read_text(encoding="utf8", errors="replace").splitlines()
    except Exception:
        return []


RULES = None          # filled by anchor.py before extraction; see load_rules()


def extract(path: pathlib.Path, rel: str) -> list[Claim]:
    """Pull every checkable commitment out of one document."""
    out: list[Claim] = []
    in_fence = False
    for i, raw in enumerate(read_lines(path), 1):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        text = line.strip()[:400]

        url_spans = [m.span() for m in RE_URL.finditer(line)]
        def in_url(span):
            return any(a <= span[0] and span[1] <= b for a, b in url_spans)

        seen_paths = set()
        for m in list(RE_PATH.finditer(line)) + [m for m in RE_CODE.finditer(line) if RE_PATH.fullmatch(m.group(1) or "")]:
            tok = (m.group(1) or "").strip()
            if not tok or tok in seen_paths or tok.startswith("http") or in_url(m.span()):
                continue
            seen_paths.add(tok)
            out.append(Claim("path", text, tok, rel, i))
        for m in RE_CITE.finditer(line):
            out.append(Claim("cite", text, m.group(1), rel, i))
        for m in RE_FACT.finditer(line):
            out.append(Claim("cite", text, pathlib.Path(m.group(1)).stem, rel, i))
        for m in RE_GIT.finditer(line):
            out.append(Claim("git", text, m.group(1), rel, i))
        for m in RE_URL.finditer(line):
            out.append(Claim("url", text, m.group(0).rstrip(".,;"), rel, i))
        for m in RE_QUOTE.finditer(line):
            q = m.group(1).strip()
            if re.search(r"\b(verbatim|hans ord|his words|the user|han sagde|sagde han|i hans egne)\b", line, re.I):
                out.append(Claim("quote", text, q, rel, i))
        for label, pat, _fn in (RULES or []):
            m = pat.search(line)
            if m:
                out.append(Claim("count", text, f"{m.group(1)}|{label}", rel, i))
        if VERDICT_WORDS.search(line):
            out.append(Claim("verdict", text, VERDICT_WORDS.search(line).group(1), rel, i))
    return out


def dedupe(claims: list[Claim]) -> list[Claim]:
    seen, out = set(), []
    for c in claims:
        k = (c.file, c.line, c.kind, c.token)
        if k in seen:
            continue
        seen.add(k); out.append(c)
    return out


def walk(root: pathlib.Path, exts=(".md",), exclude=()) -> list[tuple[pathlib.Path, str]]:
    """Excluding the tool's own reports by default is not vanity: a report that quotes a missing
    path would otherwise be read as a new claim that the path should exist."""
    import fnmatch
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_dir() or p.suffix.lower() not in exts:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = str(p.relative_to(root))
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            continue
        files.append((p, rel))
    return files


# ---------------------------------------------------------------- the commitments
class Resolver:
    """Where an artifact could live, and whether it does."""

    def __init__(self, roots: list[pathlib.Path]):
        self.roots = [r for r in roots if r.exists()]
        self.index: dict[str, list[pathlib.Path]] = {}
        for r in self.roots:
            for p in r.rglob("*"):
                if p.is_dir() or any(part in SKIP_DIRS for part in p.parts):
                    continue
                self.index.setdefault(p.name, []).append(p)
        self.repos = [g.parent for r in self.roots for g in list(r.glob("*/.git")) + list(r.glob(".git"))]

    def path(self, tok: str, near: pathlib.Path | None = None):
        t = tok.strip().strip("`").rstrip(".,;:")
        cands = []
        if t.startswith("~"):
            cands.append(pathlib.Path(os.path.expanduser(t)))
        elif t.startswith("/"):
            cands.append(pathlib.Path(t))
        else:
            if near is not None:
                cands.append(near.parent / t)
            cands += [r / t for r in self.roots]
            cands += [HELIX / t, HOME / t]
        for c in cands:
            try:
                if c.exists():
                    return True, str(c)
            except OSError:
                continue
        hits = self.index.get(pathlib.Path(t).name, [])
        if hits and ("/" not in t):
            return True, str(hits[0])
        if hits and t.endswith(tuple(pathlib.Path(h).name for h in [t])):
            for h in hits:
                if str(h).endswith(t):
                    return True, str(h)
        return False, ""

    def git(self, sha: str):
        for repo in self.repos:
            try:
                r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                                   capture_output=True, timeout=8)
                if r.returncode == 0:
                    return True, repo.name
            except Exception:
                continue
        return False, ""


def load_rules():
    """A counting rule is a PHRASE plus a recount. Scoping matters more than coverage here:
    a rule that fires on a bare noun will happily "correct" 12 phone registrations into 160
    aircraft, and an integrity tool that invents errors is worse than none. Each rule states
    the exact sentence shape it recognises; anything else stays unanchorable, on purpose."""
    P = HELIX / "workspace" / "friend" / "site" / "src"

    def facts_count():
        return len(list((HELIX / "memory" / "facts").glob("*.md")))

    def js(sym, field=None, file="data.js"):
        def f():
            p = P / file
            if not p.exists(): return None
            t = p.read_text(encoding="utf8", errors="replace")
            try:
                i = t.index(f"const {sym} = ") + len(f"const {sym} = "); j = t.index(";\n", i)
                obj = json.loads(t[i:j])
            except Exception:
                return None
            return obj.get(field) if field else len(obj)
        return f

    def makes(sym):
        def f():
            p = P / "makes.js"
            if not p.exists(): return None
            t = p.read_text(encoding="utf8", errors="replace")
            return t.count('\n {k:"') if sym == "MAKES" else len(re.findall(r'\n "[A-Z0-9]+": ?\{mk:', t))
        return f

    # (label, line pattern with one capturing number, recount)
    return [
        ("fact files",        re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*)\s+fact files", re.I), facts_count),
        ("fleet size",        re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*)\s+(?:unique )?(?:aircraft|luftfartøjer|registreringer|registrations)\b(?=[^.]{0,40}(?:register|registret|arkiv|archive|fleet|flåde))", re.I), js("FLEET")),
        ("photographs total", re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*)\s+(?:photographs?|fotografier)\b(?=[^.]{0,40}(?:over|across|fordelt|in the archive|i arkivet))", re.I), js("NUM", "photos")),
        ("makers",            re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*)\s+(?:makers|fabrikker|manufacturers)\b(?=[^.]{0,24}(?:·|,)\s*\d+\s*(?:models|modeller))", re.I), makes("MAKES")),
        ("models",            re.compile(r"(?:·|,)\s*(\d[\d,]*)\s+(?:models|modeller)\b", re.I), makes("MODELS")),
        ("owners indexed",    re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*)\s+(?:owners|ejere)\b(?=[^.]{0,30}(?:index|indekset|indexed))", re.I), js("NUM", "owners")),
    ]


def check(claims: list[Claim], root: pathlib.Path, res: Resolver, rules, quotes_hay: str, net=False):
    for c in claims:
        if c.kind == "path":
            # "SCALING.md says X" names a document, not a location. Only a path that commits to
            # WHERE the artifact lives can be broken; a bare filename is an unanchored reference.
            ok, where = res.path(c.token, near=root / c.file)
            if ok:
                c.verdict, c.detail = "ok", where
            elif "/" not in c.token:
                c.verdict, c.detail = "unanchorable", "a filename with no location given"
            else:
                c.verdict, c.detail = "broken", "no such file anywhere under the roots"
        elif c.kind == "cite":
            name = c.token if c.token.endswith(".md") else c.token + ".md"
            p = HELIX / "memory" / "facts" / name
            c.verdict, c.detail = ("ok", str(p)) if p.exists() else ("broken", f"memory/facts/{name} does not exist")
        elif c.kind == "git":
            ok, repo = res.git(c.token)
            c.verdict, c.detail = ("ok", f"in {repo}") if ok else ("broken", "no such commit in any known repo")
        elif c.kind == "count":
            num, label = c.token.split("|", 1)
            fn = dict((l, f) for l, _p, f in rules).get(label)
            want = int(num.replace(",", ""))
            try:
                actual = fn() if fn else None
            except Exception:
                actual = None
            if 1900 <= want <= 2100 and label not in ("fact files",):
                c.verdict, c.detail = "unanchorable", "looks like a year, not a count"
            elif actual is None:
                c.verdict, c.detail = "unanchorable", f"rule '{label}' could not recompute here"
            elif int(actual) == want:
                c.verdict, c.detail = "ok", f"recounted {actual}"
            else:
                # A record says what was true when it was written. The useful verdict is not
                # "false" but "no longer true" — and that is the number worth showing a reader.
                c.verdict, c.detail = "stale", f"was {want}, is now {actual}"
        elif c.kind == "quote":
            q = re.sub(r"\s+", " ", c.token).strip().lower()
            c.verdict, c.detail = ("ok", "found in the session log") if q[:60] in quotes_hay else \
                                  ("unanchorable", "not found in the session log")
        elif c.kind == "url":
            if not net:
                c.verdict, c.detail = "skipped", "network check not requested"
            else:
                c.verdict, c.detail = url_ok(c.token)
        elif c.kind == "verdict":
            same = [x for x in claims if x.file == c.file and x.line == c.line and x.kind in ("path", "cite", "git", "count")]
            c.verdict, c.detail = ("ok", f"{len(same)} receipt(s) on the line") if same else \
                                  ("unanchorable", "a verdict with no receipt on its line")
    return claims


def url_ok(u: str):
    import urllib.request, urllib.error
    req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": "anchor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return ("ok", f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        return ("ok", f"HTTP {e.code}") if e.code in (403, 405, 406, 429) else ("broken", f"HTTP {e.code}")
    except Exception as e:
        return ("broken", type(e).__name__)


# ---------------------------------------------------------------- load-bearing
def weigh(claims: list[Claim]):
    """A broken claim matters more when the file it lives in is one the rest of the record leans on.
    Citations are the cheapest measure of that: who points at whom."""
    cited: dict[str, set[str]] = {}
    for c in claims:
        if c.kind == "cite":
            cited.setdefault(c.token.replace(".md", ""), set()).add(c.file)
    KIND = {"count": 3.0, "path": 2.0, "git": 2.0, "quote": 2.0, "verdict": 1.5, "cite": 1.0, "url": 1.0}
    for c in claims:
        stem = pathlib.Path(c.file).stem
        c.weight = round(KIND.get(c.kind, 1.0) * (1 + len(cited.get(stem, ()))), 2)
    return claims


# ---------------------------------------------------------------- the graveyard
# Operating discipline, rule 6: "A claim once killed needs new evidence + a dated re-listed entry
# to return — never a quiet reintroduction." That rule has never been enforceable, because nothing
# read the whole record at once. This does.
GRAVE = [
 ("velocity-beats-level", r"velocity[- ]beats[- ]level|velocity beats level"),
 ("silence-not-amplification", r"silence[- ]not[- ]amplification"),
 ("R_I cross-domain precursor", r"R_I\s+(?:cross-domain|precursor)"),
 ("universal kappa law", r"κ\s*=\s*1\s*/\s*δ|kappa\s*=\s*1/δ|universal κ|κ≈0\.2 universal"),
 ("one law of fragility across domains", r"one law of fragility|universal law of fragility"),
 ("R5 ratio-memory to finance", r"\bR5\b.{0,40}finance"),
 ("33-probe add=sub≠mul at 100%", r"33-probe|add=sub≠mul"),
 ("markets κ=0.19 measured", r"κ\s*=\s*0\.19"),
 ("30d AUC 0.81 → 365d 0.55", r"0\.81.{0,20}0\.55"),
 ("walk-forward Sharpe 0.52 vs 0.49", r"Sharpe\s*0\.52"),
]
KILL_MARK = re.compile(
    r"(\bKILL(ED)?\b|\bFALSIFIED\b|\bdead\b|\bretracted\b|do NOT re-assert|graveyard|"
    r"does not survive|\bdied\b|overturned|refuted|0 wins|no artifact|came back wrong|"
    r"wrong direction|conflation|\bFALSE\b|unconfirmed|not novel|prior art|absent|"
    r"⊘|✗|❌|🔴|\bmiscited\b|unsourced|do not re-assert|deleted|withdrawn)", re.I)


def resurrections(files: list[tuple[pathlib.Path, str]], window: int = 2) -> list[dict]:
    """A dead claim asserted again, with nothing anywhere near it saying it is dead.

    The window matters: the graveyard files themselves name these claims constantly, and the
    kill word is often on the line above or below. Flagging those would train the reader to
    ignore the check, which is how a safety instrument dies."""
    out = []
    for p, rel in files:
        lines = read_lines(p)
        for i, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith(">"):
                continue
            ctx = " ".join(lines[max(0, i - 1 - window): i + window])
            for label, pat in GRAVE:
                if re.search(pat, line, re.I) and not KILL_MARK.search(ctx):
                    out.append(dict(claim=label, file=rel, line=i, text=line[:200]))
    return out
