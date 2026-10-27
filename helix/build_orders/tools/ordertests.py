#!/usr/bin/env python3
"""ordertests.py — ORDER 61b (P-20261004-7's (b)): the suites an order touches are green BEFORE the build.

    python3 build_orders/tools/ordertests.py <order.md>

Odin runs it by hand in pre-flight. It reads the order's EDITS with orderlint's own parser, finds the
test files under the brain that name each EDITS file's basename (orderlint's own walk), groups them by
directory and runs `python3 -m pytest -q -p no:cacheprovider <files>` in that directory, 600 s cap per
directory. The brain root runs under <brain>/.venv/bin/python (the hook tests need it).

It never edits a file itself; the tests run where they live, as they do in a build.
Exit 0 all green · 4 any red · 2 usage.
"""
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True                  # importing the lint must not write tools/__pycache__
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import orderlint

ROOT = orderlint.ROOT
TIMEOUT = 600
COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?)\b")


def test_dirs(edits):
    """{directory: [test file names]} for every test file that names an EDITS basename."""
    texts = {}
    for path in orderlint._test_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts[path] = f.read()
        except OSError:
            continue
    dirs = {}
    for _, written, _ in edits:
        base = os.path.basename(written.rstrip("/"))
        for path, text in texts.items():
            if base in text:
                names = dirs.setdefault(os.path.dirname(path), [])
                if os.path.basename(path) not in names:
                    names.append(os.path.basename(path))
    return dirs


def run_dir(d, files):
    """→ (line, green)."""
    label = orderlint._rel(d) if d != ROOT else "."
    python = "python3"
    if d == ROOT:
        python = os.path.join(ROOT, ".venv", "bin", "python")
        if not os.path.isfile(python):
            return f"{label}: cannot run, no .venv/bin/python", False
    try:
        r = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files], cwd=d,
                           capture_output=True, text=True, timeout=TIMEOUT,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return f"{label}: timed out after {TIMEOUT} s", False
    except OSError as e:
        return f"{label}: cannot run ({e})", False
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    counts = {"passed": 0, "failed": 0, "errors": 0}
    for n, word in COUNT_RE.findall(tail):
        counts["errors" if word.startswith("error") else word] += int(n)
    line = f"{label}: {counts['passed']} passed, {counts['failed']} failed"
    if counts["errors"]:
        line += f", {counts['errors']} errors"
    green = r.returncode == 0 and not counts["failed"] and not counts["errors"]
    if not green and not any(counts.values()):
        line += f" (pytest exit {r.returncode})"
    return line, green


def main(argv):
    if len(argv) != 1 or argv[0].startswith("--"):
        print("usage: ordertests.py <order.md>", file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"ordertests: cannot read {argv[0]}: {e}", file=sys.stderr)
        return 2
    _, edits = orderlint.check(text)
    dirs = test_dirs(edits)
    if not dirs:
        print("ordertests: all green (no test files name an EDITS file)")
        return 0
    red = []
    for d in sorted(dirs):
        line, green = run_dir(d, dirs[d])
        print(line, flush=True)
        if not green:
            red.append(orderlint._rel(d) if d != ROOT else ".")
    if red:
        print(f"ordertests: RED in {', '.join(red)}")
        return 4
    print("ordertests: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
