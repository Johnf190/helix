#!/usr/bin/env python3
"""namecheck.py — ORDER 56 (P-20261004-5). Names that would crash Helix, found before a restart.

    python3 build_orders/tools/namecheck.py [files…]

With no args: app.py plus every brain-level module app.py imports (read with `ast` at run time,
anywhere in the file, so a lazy `import factgate_hook` inside a function counts). With args:
those files.

Runs pyflakes from the linter venv on the HDD (pinned in requirements.txt beside it) and keeps
ONLY undefined names, undefined locals ("referenced before assignment") and syntax errors.
Everything else pyflakes says (unused imports, unused locals, …) is ignored.

Why: catch #13 — a builder put `log()` (undefined) into /remember; `ast.parse` passed it and only
a human read caught it before the restart.

Exit 0 clean · 4 findings (one per line) · 2 unavailable (prints `namecheck unavailable: …`).
Read-only. The brain is this file's own location (tools/../..); nothing in argv or the
environment changes the linter.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[2]
VENV = Path("__BULK__/venvs/lint")
TIMEOUT = 25                      # app.py's /restart waits 30 s for the whole of this
EXIT_CLEAN, EXIT_UNAVAILABLE, EXIT_FINDINGS = 0, 2, 4
KEEP = re.compile(r"undefined name |referenced before assignment")
# pyflakes writes syntax errors (and undecodable files) to stderr as `<path>:<line>:<col>: <msg>`
LOCATED = re.compile(r"^(.+?):\d+(:\d+)?: ")


class Unavailable(Exception):
    pass


def startup_files(brain=BRAIN):
    """app.py, then the brain's own modules it imports, sorted by name."""
    app = Path(brain) / "app.py"
    if not app.is_file():
        raise Unavailable(f"no app.py in {brain}")
    try:
        tree = ast.parse(app.read_text(encoding="utf-8"), str(app))
    except SyntaxError:
        return [app]              # pyflakes reports the syntax error itself
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    mods = sorted(Path(brain) / f"{n}.py" for n in names if (Path(brain) / f"{n}.py").is_file())
    return [app] + mods


def findings(files, venv=VENV):
    """The kept pyflakes messages for `files`, one string each. Raises Unavailable."""
    py = Path(venv) / "bin" / "python"
    if not py.exists():
        raise Unavailable(f"no linter at {py}")
    missing = [str(f) for f in files if not Path(f).is_file()]
    if missing:
        raise Unavailable(f"no such file: {missing[0]}")
    try:
        ver = subprocess.run([str(py), "-m", "pyflakes", "--version"], capture_output=True,
                             text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        if ver.returncode != 0:
            why = (ver.stderr.strip() or ver.stdout.strip() or "no output").splitlines()[-1]
            raise Unavailable(f"pyflakes not runnable in {venv}: {why}")
        p = subprocess.run([str(py), "-m", "pyflakes", *map(str, files)], capture_output=True,
                           text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"pyflakes timed out after {TIMEOUT} s")
    except OSError as e:
        raise Unavailable(f"{type(e).__name__}: {e}")
    if p.returncode == 0:
        return []
    out = [l for l in p.stdout.splitlines() if l.strip()]
    err = [l for l in p.stderr.splitlines() if LOCATED.match(l)]
    if p.returncode != 1 or not (out or err):
        why = (p.stderr.strip() or "no output").splitlines()[-1]
        raise Unavailable(f"pyflakes exit {p.returncode}: {why}")
    return [l for l in out if KEEP.search(l)] + [f"{l} (syntax error)" for l in err]


def main(argv=None, venv=VENV):
    argv = sys.argv[1:] if argv is None else argv
    try:
        files = [Path(a) for a in argv] if argv else startup_files()
        found = findings(files, venv)
    except Unavailable as e:
        print(f"namecheck unavailable: {e}")
        return EXIT_UNAVAILABLE
    for line in found:
        print(line)
    return EXIT_FINDINGS if found else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
