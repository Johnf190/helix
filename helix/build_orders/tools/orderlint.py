#!/usr/bin/env python3
"""orderlint.py — ORDER 54c. An order is checked against the live disk before a builder starts.

    python3 build_orders/tools/orderlint.py <order.md> [--full] [--json]

Read-only: it never writes a file. Exit 0 clean · 3 hard findings · 2 usage.

The brain it checks against is its OWN location (tools/../..), the way run_order.sh finds its
brain. Nothing in argv or the environment changes what is checked or where.

An order carries one `## ORDER HEADER` section. Paths are relative to the brain unless absolute
(`~` is not expanded: $HOME must not change what is checked):
    EDITS: <path>[ (new)], …
    DOES NOT TOUCH: <path>, …
    FENCE: <path> sha256 <64 hex>        (0+ lines)
    RESTART: yes|no

HARD findings (one line each, `HARD: <what>`):
 1. no `## ORDER HEADER`, more than one, a missing EDITS / DOES NOT TOUCH / RESTART line, an
    unknown key, a duplicate key, or a line that is not `KEY: value`;
 2. an EDITS path inside (or equal to) a DOES NOT TOUCH path;
 3. an EDITS path that does not exist and is not marked `(new)`; a `(new)` path that exists;
 4. a FENCE whose file is missing or whose sha256 differs NOW;
 5. `RESTART: no` while EDITS names app.py or a brain-level module app.py imports (read with
    `ast` from app.py at lint time); or `RESTART: yes` with no `READY FOR /restart` in the order;
 6. the order does not state the last-line rule: it must contain `last line` (any case — orders
    write "very LAST line") and `DONE` (exactly that case).
 A `memory/facts` path in EDITS is HARD too, in both modes: a builder never writes the fact base.

--full (Odin's pre-flight) adds NOTE lines and never changes the exit code: for each EDITS file,
the test files under the brain that name its basename and either define a test whose name holds
`escape_hatch` or contain a `.count(` pin — a checklist with line numbers. The walk leaves out
every directory whose name starts with `.`, plus quarantine/, __pycache__/, node_modules/, and
any file matching *.bak*.

--full also adds four lettered NOTE lines (ORDER 61b), again without changing the exit code:
 (a) a line saying `before` with master / HEAD / main or an existing brain path, and no 7–64 hex string;
 (c) the proposal `P-YYYYMMDD-N` named on the order's first line: its status in
     workspace/proposals/PROPOSALS.jsonl (a decision row decides it, as propose.py does) and its acceptance;
 (d) an existing brain path named in a section whose heading holds `live check` or `acceptance`
     that is neither an EDITS path nor under one;
 (e) a line of an EDITS test_*.py that names live memory together with open( / write_ / >> / append,
     and a line of the order's Tests section that names live memory.
"""
import ast
import fnmatch
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
HEADER = "## ORDER HEADER"
KEYS = ("EDITS", "DOES NOT TOUCH", "FENCE", "RESTART")
REQUIRED = ("EDITS", "DOES NOT TOUCH", "RESTART")
NEW = " (new)"
FENCE_RE = re.compile(r"^(\S+) sha256 ([0-9a-f]{64})$")
TEST_DEF_RE = re.compile(r"^\s*def (test_\w*escape_hatch\w*)\s*\(")
SKIP_DIRS = {"quarantine", "__pycache__", "node_modules"}
PROPOSALS = os.path.join(ROOT, "workspace", "proposals", "PROPOSALS.jsonl")
STATUS_OF = {"approve": "approved", "reject": "rejected", "park": "parked"}     # as propose.py
PROPOSAL_RE = re.compile(r"\bP-\d{8}-\d+\b")
BEFORE_RE = re.compile(r"\bbefore\b", re.I)
MOVING_RE = re.compile(r"\b(?:master|HEAD|main)\b")
PIN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,64}(?![0-9A-Za-z])")
PATH_TOKEN_RE = re.compile(r"[~\w./-]+")
HEADING_RE = re.compile(r"^(#+)\s*(.*)$")
LIVE_MEMORY = ("sessions/log.md", "notepad.json", "memory/sessions", "memory/facts")
WRITE_WORDS = ("open(", "write_", ">>", "append")


def _abs(p):
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(ROOT, p))


def _inside(path, d):
    return path == d or path.startswith(d.rstrip(os.sep) + os.sep)


def _rel(path):
    return os.path.relpath(path, ROOT) if _inside(path, ROOT) else path


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _header(lines, hard):
    """The header's lines as (key, value) pairs, or None when there is no header."""
    starts = [i for i, l in enumerate(lines) if l.rstrip() == HEADER]
    if not starts:
        hard.append(f"rule 1 — no '{HEADER}' section")
        return None
    if len(starts) > 1:
        hard.append(f"rule 1 — {len(starts)} '{HEADER}' sections (an order carries one)")
    pairs = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("#"):
            break
        if not line.strip():
            continue
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon:
            hard.append(f"rule 1 — malformed header line: {line.strip()!r}")
        elif key not in KEYS:
            hard.append(f"rule 1 — unknown key {key!r}")
        else:
            pairs.append((key, value.strip()))
    return pairs


def _app_imports():
    """Brain-level modules app.py imports, anywhere in the file (None if app.py cannot be read)."""
    try:
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {os.path.join(ROOT, n + ".py") for n in names if os.path.isfile(os.path.join(ROOT, n + ".py"))}


def check(text):
    """(hard, edits): every HARD finding for this order text, in rule order."""
    hard = []
    pairs = _header(text.splitlines(), hard)
    if pairs is None:
        return hard + _rule_6(text), []

    seen = {}
    for key, value in pairs:
        if key != "FENCE" and key in seen:
            hard.append(f"rule 1 — duplicate key {key!r}")
        seen.setdefault(key, value)
    for key in REQUIRED:
        if key not in seen:
            hard.append(f"rule 1 — missing {key}: line")

    edits = []                                       # (absolute path, written as, marked new)
    for item in seen["EDITS"].split(",") if "EDITS" in seen else []:
        item = item.strip()
        new = item.endswith(NEW)
        p = item[:-len(NEW)].strip() if new else item
        if not p:
            hard.append("rule 1 — empty EDITS item")
            continue
        edits.append((_abs(p), p, new))
    dnt = [(_abs(p.strip()), p.strip()) for p in (seen.get("DOES NOT TOUCH") or "").split(",") if p.strip()]
    fences = []
    for key, value in pairs:
        if key == "FENCE":
            m = FENCE_RE.match(value)
            if m:
                fences.append((_abs(m.group(1)), m.group(1), m.group(2)))
            else:
                hard.append(f"rule 1 — malformed FENCE line: {value!r} (want '<path> sha256 <64 hex>')")
    restart = seen.get("RESTART")
    if restart is not None and restart not in ("yes", "no"):
        hard.append(f"rule 1 — RESTART must be yes or no, got {restart!r}")

    for path, written, _ in edits:                                         # rule 2
        for d, dw in dnt:
            if _inside(path, d):
                hard.append(f"rule 2 — EDITS {written} is inside DOES NOT TOUCH {dw}")

    for path, written, new in edits:                                       # rule 3
        exists = os.path.lexists(path)
        if new and exists:
            hard.append(f"rule 3 — (new) path {written} already exists")
        elif not new and not exists:
            hard.append(f"rule 3 — EDITS {written} does not exist and is not marked (new)")

    for path, written, want in fences:                                     # rule 4
        if not os.path.isfile(path):
            hard.append(f"rule 4 — FENCE file {written} is missing")
        else:
            have = _sha256(path)
            if have != want:
                hard.append(f"rule 4 — FENCE {written} sha256 differs now (disk {have[:12]}…, order {want[:12]}…)")

    if restart == "no":                                                    # rule 5
        startup = _app_imports()
        if startup is None:
            hard.append("rule 5 — RESTART: no, and app.py cannot be read to find its imports")
            startup = set()
        startup.add(os.path.join(ROOT, "app.py"))
        for path, written, _ in edits:
            if path in startup:
                hard.append(f"rule 5 — RESTART: no, but EDITS names {written}, which app.py loads at start-up")
    elif restart == "yes" and "READY FOR /restart" not in text:
        hard.append("rule 5 — RESTART: yes, but the order never says READY FOR /restart")

    hard += _rule_6(text)

    facts = os.path.join(ROOT, "memory", "facts")
    for path, written, _ in edits:
        if _inside(path, facts) or "memory/facts" in written:
            hard.append(f"memory/facts — EDITS names {written}; a builder never writes the fact base")
    return hard, edits


def _rule_6(text):
    if "last line" in text.lower() and "DONE" in text:
        return []
    return ["rule 6 — the order does not state the last-line rule (it needs 'last line' and 'DONE')"]


def _test_files():
    for top, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            if fnmatch.fnmatch(name, "test_*.py") and not fnmatch.fnmatch(name, "*.bak*"):
                yield os.path.join(top, name)


def notes_for(edits):
    """--full: the escape-hatch tests and .count( pins in test files that name an EDITS file."""
    notes = []
    if not edits:
        return notes
    texts = {}
    for path in _test_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts[path] = f.read()
        except OSError:
            continue
    for _, written, _ in edits:
        base = os.path.basename(written.rstrip("/"))
        for path, text in texts.items():
            if base not in text:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if TEST_DEF_RE.match(line) or ".count(" in line:
                    notes.append(f"[ ] {base}: {_rel(path)}:{n}: {line.strip()[:140]}")
    return notes


def _brain_paths(line):
    """Existing paths under the brain named in one line (relative, absolute or ~/helix/…), in order."""
    found = []
    for tok in PATH_TOKEN_RE.findall(line):
        tok = tok.rstrip(".,:;-")
        if tok.startswith("~/helix/"):
            tok = tok[len("~/helix/"):]
        if not tok or tok.startswith("~") or ("/" not in tok and "." not in tok):
            continue
        path = _abs(tok)
        if path != ROOT and _inside(path, ROOT) and os.path.exists(path) and path not in found:
            found.append(path)
    return found


def _sections(lines, pattern):
    """1-based numbers of the lines inside every section whose heading matches pattern (to the next
    heading of the same or a higher level)."""
    out, level = [], None
    for n, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m:
            depth = len(m.group(1))
            if level is not None and depth > level:
                continue
            level = depth if re.search(pattern, m.group(2), re.I) else None
        elif level is not None:
            out.append(n)
    return out


def _proposal_note(first_line):
    m = PROPOSAL_RE.search(first_line)
    if not m:
        return []
    pid = m.group(0)
    try:
        with open(PROPOSALS, encoding="utf-8") as f:
            rows = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return [f"(c) {pid}: cannot read {_rel(PROPOSALS)} ({e.__class__.__name__})"]
    proposal, decision = None, None
    for raw in rows:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("kind") == "proposal" and r.get("id") == pid:
            proposal = r
        elif r.get("kind") == "decision" and r.get("proposal") == pid:
            decision = r.get("decision")
    if proposal is None:
        return [f"(c) {pid} is not in {os.path.basename(PROPOSALS)}"]
    status = STATUS_OF.get(decision) or proposal.get("status") or "pending"
    if status != "approved":
        return [f"(c) {pid} is {status}, not approved"]
    return [f"(c) {pid} approved · acceptance: {proposal.get('acceptance')}"]


def preflight_notes(text, edits):
    """--full (ORDER 61b): the (a), (c), (d) and (e) notes, in that order. Reads only."""
    lines = text.splitlines()
    notes = []
    for n, line in enumerate(lines, 1):                                    # (a)
        if (BEFORE_RE.search(line) and (MOVING_RE.search(line) or _brain_paths(line))
                and not PIN_RE.search(line)):
            notes.append(f"(a) line {n}: a 'before' reference is not pinned to a commit or sha256")

    notes += _proposal_note(lines[0] if lines else "")                     # (c)

    edit_paths = [path for path, _, _ in edits]                            # (d)
    for n in _sections(lines, r"live check|acceptance"):
        for path in _brain_paths(lines[n - 1]):
            if not any(_inside(path, e) for e in edit_paths):
                notes.append(f"(d) line {n}: {_rel(path)} is checked but this order does not change it")

    for path, written, _ in edits:                                         # (e)
        if not fnmatch.fnmatch(os.path.basename(path), "test_*.py") or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body = f.read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(body, 1):
            if any(m in line for m in LIVE_MEMORY) and any(w in line for w in WRITE_WORDS):
                notes.append(f"(e) {written}:{n}: a test may write live memory")
    for n in _sections(lines, r"\btests?\b"):
        named = [m for m in LIVE_MEMORY if m in lines[n - 1]]
        if named:
            notes.append(f"(e) line {n}: the order's Tests section names live memory ({', '.join(named)})")
    return notes


def main(argv):
    opts = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    unknown = [o for o in opts if o not in ("--full", "--json")]
    if unknown or len(args) != 1:
        print("usage: orderlint.py <order.md> [--full] [--json]", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"orderlint: cannot read {args[0]}: {e}", file=sys.stderr)
        return 2
    hard, edits = check(text)
    notes = notes_for(edits) + preflight_notes(text, edits) if "--full" in opts else []
    if "--json" in opts:
        print(json.dumps({"hard": hard, "notes": notes}, indent=1))
    else:
        for h in hard:
            print(f"HARD: {h}")
        for n in notes:
            print(f"NOTE: {n}")
        print(f"orderlint: {len(hard)} hard, {len(notes)} notes — {args[0]}")
    return 3 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
