#!/usr/bin/env python3
"""memdigest.py — ORDER 81 (item 33). One digest file per fact category, derived from the facts by code.

No model writes anything here: a digest is each member fact's own first line plus the flags the caller
passes in (app passes its own, so a digest line equals that fact's OLDER FACTS index line byte for byte).
It is rebuilt from memory/facts at every memory load and carries a `source:` hash of its member facts,
so it cannot drift from them; `check` says when one has.

    build(facts_dir, out_dir, flags, cats=None, assigned=None, closed=None) -> {category: {...}}
    check(facts_dir, out_dir, cats=None, assigned=None) -> [digest paths out of sync]   ([] = in sync)
    python3 memdigest.py build|check        (default facts dir and workspace/memdigest/out)

Reads memory/facts, workspace/categories (categories.py is loaded BY PATH; CATEGORIES.txt, ASSIGNED.csv)
and CLOSED.txt. Writes only under out_dir. A fact whose name matches categories.py's never-open rule
(credentials, accounts, .env) is never opened: its line says so and its hash is over its size and mtime.
"""
import hashlib
import importlib.util
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = HERE.parents[1]
FACTS = HELIX / "memory" / "facts"
OUT = HERE / "out"
CLOSED_TXT = HERE / "CLOSED.txt"
CATEGORIES_PY = HELIX / "workspace" / "categories" / "categories.py"
CATEGORIES_TXT = HELIX / "workspace" / "categories" / "CATEGORIES.txt"
ASSIGNED_CSV = HELIX / "workspace" / "categories" / "ASSIGNED.csv"
UNCATEGORISED = "uncategorised"
UNCATEGORISED_DESC = "no category declared or assigned; each one also has its own line"
GENERATED = "GENERATED from memory/facts by memdigest.py — do not edit; rebuilt at every memory load."
NOT_OPENED = "(not opened: the name matches the never-open rule)"
NEVER_OPEN = re.compile(r"credential|accounts|\.env", re.I)     # categories.py's, if it cannot be loaded


def _log(msg):
    print(f"HELIX: [memdigest] {msg}", flush=True)


def first_line(txt):
    """app._first_line, the same rule (test_index_by_category checks they agree)."""
    for ln in txt.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln: return ln[:120]
    return "(untitled)"


def slug(name):
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def _categories_module():
    try:
        spec = importlib.util.spec_from_file_location("_memdigest_categories", str(CATEGORIES_PY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        _log(f"categories.py not loaded, every fact is {UNCATEGORISED} — {type(e).__name__}: {e}")
        return None


def load_closed(path=None):
    """{category: decision} from CLOSED.txt (`category | decision`). Missing → {} and one log line."""
    path = CLOSED_TXT if path is None else path
    out = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception as e:
        _log(f"no closed decisions this time — {path}: {type(e).__name__}")
        return out
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#") or "|" not in ln:
            continue
        name, decision = ln.split("|", 1)
        out[" ".join(name.split())] = decision.strip()
    return out


def _sources(mod, cats, assigned):
    """(cats, assigned) with the defaults filled in from the module constants. Never raises."""
    if mod is None:
        return [], {}
    if cats is None:
        try:
            cats = mod.load(CATEGORIES_TXT)
            if not cats:
                _log(f"no categories on the list — {CATEGORIES_TXT}")
        except Exception as e:
            _log(f"category list unreadable — {CATEGORIES_TXT}: {type(e).__name__}")
            cats = []
    if assigned is None:
        try:
            assigned = mod.load_assigned(ASSIGNED_CSV, cats)
        except Exception as e:
            _log(f"ASSIGNED.csv unreadable — {ASSIGNED_CSV}: {type(e).__name__}")
            assigned = {}
    return cats, assigned


def members(facts_dir=None, cats=None, assigned=None):
    """[(file, category, text or None, mtime, sha256)] for every *.md in facts_dir, in name order."""
    mod = _categories_module()
    cats, assigned = _sources(mod, cats, assigned)
    return _members(facts_dir, mod, cats, assigned)


def _members(facts_dir, mod, cats, assigned):
    facts_dir = Path(FACTS if facts_dir is None else facts_dir)
    never = getattr(mod, "NEVER_OPEN", NEVER_OPEN)
    try:
        names = sorted(p.name for p in facts_dir.iterdir() if p.suffix == ".md" and p.is_file())
    except Exception as e:
        _log(f"facts dir unreadable — {facts_dir}: {type(e).__name__}")
        return []
    out = []
    for fn in names:
        p = facts_dir / fn
        try:
            st = p.stat()
            if never.search(fn):
                text, h = None, hashlib.sha256(f"not opened {st.st_size} {st.st_mtime_ns}".encode()).hexdigest()
            else:
                raw = p.read_bytes()
                text, h = raw.decode("utf-8", "replace").strip(), hashlib.sha256(raw).hexdigest()
        except Exception as e:
            _log(f"fact skipped — {fn}: {type(e).__name__}")
            continue
        cat = UNCATEGORISED
        if mod is not None:
            try:
                names_, _src = mod.category_of(fn, text or "", cats, assigned)
                cat = names_[0] if names_ else UNCATEGORISED
            except Exception as e:
                _log(f"no category for {fn} — {type(e).__name__}")
        out.append((fn, cat, text, st.st_mtime, h))
    return out


def source_hash(rows):
    """sha256 over the sorted (filename, sha256(file bytes)) of a category's member facts."""
    h = hashlib.sha256()
    for fn, fh in sorted((r[0], r[4]) for r in rows):
        h.update(f"{fn}\0{fh}\n".encode("utf-8"))
    return h.hexdigest()


def _groups(rows):
    g = {}
    for r in rows:
        g.setdefault(r[1], []).append(r)
    return g


def render(category, rows, flags, closed=None):
    """The digest text of one category: header, then one line per fact, newest mtime first."""
    lines = [f"# DIGEST — {category}", GENERATED, f"source: {source_hash(rows)}"]
    if closed:
        lines.append(f"closed: {closed}")
    for fn, _c, text, _mt, _h in sorted(rows, key=lambda r: (-r[3], r[0])):
        head = NOT_OPENED if text is None else first_line(text)
        lines.append(f"- memory/facts/{fn} — {head}{flags(fn, text)}")
    return "\n".join(lines) + "\n"


def _write(path, text):
    """Atomically, and only if the content changed."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except Exception:
        pass
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _retire(path, out_dir):
    """A digest whose category no longer exists moves to out_dir/_old/ (never deleted)."""
    old = out_dir / "_old"
    try:
        old.mkdir(exist_ok=True)
        dest = old / path.name
        if dest.exists():
            dest = old / f"{path.stem}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.md"
        os.replace(path, dest)
        _log(f"category gone, digest moved — {path.name} -> _old/{dest.name}")
    except Exception as e:
        _log(f"could not move {path.name} to _old/ — {type(e).__name__}")


def build(facts_dir=None, out_dir=None, flags=None, cats=None, assigned=None, closed=None):
    """Write out_dir/<slug>.md per category; return {category: {count, newest_date, path, source,
    closed, files, description}} (files newest first; description from CATEGORIES.txt). `closed` is {category: decision}; None reads CLOSED.txt."""
    out_dir = Path(OUT if out_dir is None else out_dir)
    flags = flags or (lambda fn, text: "")
    closed = load_closed() if closed is None else closed
    mod = _categories_module()
    cats, assigned = _sources(mod, cats, assigned)
    desc = {c.name: c.desc for c in cats}
    rows = _members(facts_dir, mod, cats, assigned)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    for cat, rs in sorted(_groups(rows).items()):
        path = out_dir / f"{slug(cat)}.md"
        text = render(cat, rs, flags, closed.get(cat))
        _write(path, text)
        newest = sorted(rs, key=lambda r: (-r[3], r[0]))
        res[cat] = {"count": len(rs), "newest_date": time.strftime("%Y-%m-%d", time.localtime(newest[0][3])),
                    "path": str(path), "source": source_hash(rs), "closed": closed.get(cat),
                    "files": [r[0] for r in newest], "description": desc.get(cat, UNCATEGORISED_DESC)}
    keep = {Path(v["path"]).name for v in res.values()}
    for p in sorted(out_dir.glob("*.md")):
        if p.name not in keep:
            _retire(p, out_dir)
    return res


def _source_line(path):
    try:
        for ln in Path(path).read_text(encoding="utf-8").splitlines()[:4]:
            if ln.startswith("source: "):
                return ln[len("source: "):].strip()
    except Exception:
        pass
    return None


def check(facts_dir=None, out_dir=None, cats=None, assigned=None):
    """Digest paths whose `source:` differs from a fresh computation, missing ones, and ones for a
    category that no longer exists. [] = in sync."""
    out_dir = Path(OUT if out_dir is None else out_dir)
    groups = _groups(members(facts_dir, cats, assigned))
    bad, want = [], set()
    for cat, rs in sorted(groups.items()):
        path = out_dir / f"{slug(cat)}.md"
        want.add(path.name)
        if _source_line(path) != source_hash(rs):
            bad.append(str(path))
    if out_dir.is_dir():
        bad += [str(p) for p in sorted(out_dir.glob("*.md")) if p.name not in want]
    return bad


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["build"], ["check"]):
        print("usage: memdigest.py build|check", file=sys.stderr)
        return 2
    if argv == ["build"]:
        res = build()
        print(f"built {len(res)} digests in {OUT} ({sum(v['count'] for v in res.values())} facts; "
              "no flags from the command line — app's next memory load rebuilds them with its flags)")
        return 0
    bad = check()
    if bad:
        print(f"OUT OF SYNC ({len(bad)}):")
        for p in bad:
            print(f"  {p}")
        return 1
    print(f"in sync: {len(list(OUT.glob('*.md')))} digests in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
