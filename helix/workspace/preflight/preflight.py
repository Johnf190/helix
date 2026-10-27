#!/usr/bin/env python3
"""preflight.py — item 15a: before any seal, Alice searches for reasons the idea is dead.

Every big kill in this programme was findable in an afternoon and was found months late.
The pre-flight makes that afternoon mandatory and first, and the search is done by Alice
(Gemini), not by the seal's author, because the author is the worst searcher for reasons
their own idea is dead.

Two verbs:

    preflight.py run   <IDEA.md> [--out DIR]    exit 0 brief written / 3 reply refused /
                                                5 Alice failed (timeout, exit, bad JSON,
                                                or she wrote a file) / 2 usage
    preflight.py check <BRIEF.md>               exit 0 well formed / 3 refused / 2 usage

`run` gives Alice the idea text and the web, and nothing else: no `--add-dir`, so she
cannot read our memory (which holds the answers for past ideas and would contaminate
both real runs and the calibration), and no `--dangerously-skip-permissions`. She runs in
an empty scratch directory. If any file appears there, the run is refused and says so.

Alice gets no file-write path. The program writes the brief from her reply, and writes
the header itself (Searcher, Model, Date, Idea-sha256, Queries run, Brief-body-sha256).
A header in her reply is ignored: only text from `## 1. Academia` on is kept.
Her whole reply is kept next to the brief as `<brief>.raw.json`, the unframed artifact,
whether or not the brief is accepted.

`check` enforces the format in TEMPLATE.md. Brief-body-sha256 is over the raw bytes of
sections 1–6 as written: from the `## 1. Academia` line up to the `## Odin's additions`
line (or end of file), trailing newlines dropped. Odin may ADD findings under
`## Odin's additions` at the end; an edit to sections 1–6 is refused.

Exit codes:  0 ok · 2 usage / unreadable input · 3 refused · 5 Alice failed
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_ALICE = 5

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "briefs"

AGY = "agy"                                   # resolved on PATH, as app.py does
MODEL = "Gemini 3.1 Pro (High)"               # app.py ALICE_MODEL
SEARCHER = "Alice"
TIMEOUT = 900                                 # seconds, as alice_brain()
MAX_IDEA_BYTES = 12_000                       # "at most two pages"

TITLE = "# PRE-FLIGHT BRIEF — written by preflight.py, not by hand"
SECTIONS = [
    "## 1. Academia",
    "## 2. GitHub",
    "## 3. Known math",
    "## 4. Known limits",
    "## 5. Cheapest kill",
    "## 6. Verdict",
]
FINDINGS_SECTIONS = SECTIONS[:4]
ADDITIONS = "## Odin's additions"

# The header, in order. The program writes every value; `check` refuses anything else.
HEADER_FIELDS = [
    ("Idea", re.compile(r"^\S.*$")),
    ("Idea-sha256", re.compile(r"^[0-9a-f]{64}$")),
    ("Searcher", re.compile(r"^\S.*$")),
    ("Model", re.compile(r"^\S.*$")),
    ("Date (UTC)", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")),
    ("Queries run", re.compile(r"^[1-9]\d*$")),
    ("Brief-body-sha256", re.compile(r"^[0-9a-f]{64}$")),
]

NOTHING_FOUND = "- NOTHING FOUND"
QUERY_RE = re.compile(r"^- query: \S.*$")
FINDING_RE = re.compile(r"^- (\S.*?) — (\S.*)$")
VERDICT_RE = re.compile(r"^(GO|NO-GO|RESHAPE: (\S.*))$")
START_RE = re.compile(rb"^## 1\. Academia\r?$", re.M)
ADDITIONS_RE = re.compile(rb"^## Odin's additions\r?$", re.M)

# the user must be able to read this. It is the whole of what Alice is told.
PROMPT = """\
You are running a PRE-FLIGHT on an idea that someone is about to spend weeks building.
You are hostile to the idea. Your job is to find reasons it is ALREADY DONE, ALREADY DEAD,
or BOUNDED BY A KNOWN IMPOSSIBILITY, before any work is spent on it.

Do not be encouraging. Do not praise the idea, do not soften a finding, do not suggest how
to make it work unless as a RESHAPE verdict. A missed killer costs months; a false alarm
costs an afternoon.

SEARCH THE WEB, hard, and under the names other fields would use for the same thing:
- Academia: papers, surveys, textbooks, theses, arXiv. Is this a known result, perhaps under
  another name? Has it been proposed and refuted?
- GitHub and other code: has it been built? Did the implementation reveal why it fails?
- Known math: is the claim a standard theorem, a textbook chapter, or a special case of one?
- Known limits: impossibility results, lower bounds, published attacks, negative results
  that bound what this idea can achieve at all.
Cite only what you actually found in this search. Give a URL or a full citation for each.
Never invent a citation. If a section turns up nothing, you must show the queries you ran
for it: NOTHING FOUND without the queries is not accepted.

You have no files to read and you must not create, edit or save any file. Your reply text
is the whole of your answer; a program turns it into the brief and writes the header.

REPLY IN EXACTLY THIS FORMAT, with nothing before QUERIES and nothing after the verdict:

QUERIES:
- <every web search query you ran, one per line>

## 1. Academia
- <citation or URL> — <one line: why it matters to this idea>

## 2. GitHub
- <citation or URL> — <one line: why it matters to this idea>

## 3. Known math
- <citation or URL> — <one line: why it matters to this idea>

## 4. Known limits
- <citation or URL> — <one line: why it matters to this idea>

## 5. Cheapest kill
<the cheapest experiment or reading that would kill the idea, in a few plain lines>

## 6. Verdict
<exactly one line: GO, or NO-GO, or RESHAPE: <one line>>

Rules for the format:
- Sections 1-4: one finding per line, `- ` then the citation or URL, then " — " (an em dash
  with a space either side), then one line on why it matters. As many findings as you have.
- A section 1-4 with nothing found is written as the line `- NOTHING FOUND` followed by one
  line `- query: <query>` for each query you ran for that section.
- No other headings, no sub-headings, no bold, no tables, no code fences, no header lines.
- Section 6 is exactly one line. GO means you searched hard and found nothing that shows the
  idea is done, dead, or bounded. NO-GO means you did. RESHAPE means a narrower version may
  survive; say which in one line.

THE IDEA, verbatim, between the markers:
<<<IDEA
{idea}
IDEA>>>
"""


# --------------------------------------------------------------------- helpers

def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def body_region(raw):
    """Sections 1–6 as written, raw bytes: `## 1. Academia` up to `## Odin's additions`."""
    m = START_RE.search(raw)
    if not m:
        return None
    end = ADDITIONS_RE.search(raw, m.start())
    return raw[m.start():end.start() if end else len(raw)].rstrip(b"\r\n")


def idea_line(idea_raw):
    for line in idea_raw.decode("utf-8", errors="replace").splitlines():
        line = re.sub(r"^\s*Idea\s*:\s*", "", line.strip(), flags=re.I)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line[:200]
    return "(empty idea file)"


def split_response(response):
    """(queries, sections text) from Alice's reply. Anything above section 1 is dropped."""
    lines = response.replace("\r\n", "\n").split("\n")
    queries, in_q = [], False
    for line in lines:
        s = line.strip()
        if s.upper() == "QUERIES:":
            in_q = True
            continue
        if in_q:
            if s.startswith("- ") and s[2:].strip():
                queries.append(s[2:].strip())
            elif s == "" and not queries:
                continue
            else:
                break
    try:
        start = [l.rstrip() for l in lines].index(SECTIONS[0])
    except ValueError:
        return queries, ""
    return queries, "\n".join(lines[start:]).rstrip() + "\n"


def build_brief(idea_raw, sections_md, *, searcher, model, date_utc, n_queries):
    """The brief as the program writes it: its own header, then her sections 1–6."""
    sections = sections_md.rstrip("\r\n") + "\n"
    body_sha = sha256_bytes(sections.encode("utf-8").rstrip(b"\r\n"))
    header = [
        TITLE,
        f"Idea: {idea_line(idea_raw)}",
        f"Idea-sha256: {sha256_bytes(idea_raw)}",
        f"Searcher: {searcher}",
        f"Model: {model}",
        f"Date (UTC): {date_utc}",
        f"Queries run: {n_queries}",
        f"Brief-body-sha256: {body_sha}",
    ]
    return "\n".join(header) + "\n\n" + sections


# ----------------------------------------------------------------------- check

def _check_header(lines, problems, info):
    header = [l for l in lines if l.strip()]
    if not header or header[0] != TITLE:
        problems.append(f"header: first line is not the program's title line {TITLE!r}")
        header = header[1:] if header and header[0].startswith("# ") else header
    else:
        header = header[1:]
    if len(header) != len(HEADER_FIELDS):
        problems.append(f"header: {len(header)} field lines, a program-written header has "
                        f"{len(HEADER_FIELDS)} ({', '.join(n for n, _ in HEADER_FIELDS)})")
    for i, (name, pattern) in enumerate(HEADER_FIELDS):
        if i >= len(header):
            problems.append(f"header: field {name!r} missing")
            continue
        line = header[i]
        prefix = f"{name}: "
        if not line.startswith(prefix):
            problems.append(f"header: line {i + 2} should be {name!r}, found {line[:80]!r}")
            continue
        value = line[len(prefix):]
        if not pattern.match(value):
            problems.append(f"header: {name} has a value the program would never write: "
                            f"{value[:80]!r}")
            continue
        info[name] = value


def _check_findings_section(name, items, problems):
    label = name[3:]
    if not items:
        problems.append(f"section {label!r} is empty")
        return
    if items[0] == NOTHING_FOUND:
        rest = items[1:]
        if not rest:
            problems.append(f"section {label!r}: NOTHING FOUND without the queries that "
                            f"were run for it")
        bad = [r for r in rest if not QUERY_RE.match(r)]
        if bad:
            problems.append(f"section {label!r}: after NOTHING FOUND only `- query: <text>` "
                            f"lines are allowed, found {bad[0][:80]!r}")
        return
    for item in items:
        if item == NOTHING_FOUND:
            problems.append(f"section {label!r}: NOTHING FOUND mixed with findings")
        elif QUERY_RE.match(item) or not FINDING_RE.match(item):
            problems.append(f"section {label!r}: not `- <citation or URL> — <why it "
                            f"matters>`: {item[:80]!r}")


def check_brief(raw):
    """(problems, info). An empty problem list means the brief is well formed."""
    problems, info = [], {}
    text = raw.decode("utf-8", errors="replace")
    lines = [l.rstrip("\r") for l in text.split("\n")]

    first_h2 = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    _check_header(lines[:first_h2], problems, info)

    add_idx = next((i for i, l in enumerate(lines) if l == ADDITIONS), len(lines))
    main = lines[first_h2:add_idx]
    headings = [(i, l) for i, l in enumerate(main) if l.startswith("#")]
    names = [l for _, l in headings]
    for h in SECTIONS:
        n = names.count(h)
        if n == 0:
            problems.append(f"missing section {h[3:]!r}")
        elif n > 1:
            problems.append(f"section {h[3:]!r} appears {n} times")
    for h in names:
        if h not in SECTIONS:
            problems.append(f"unexpected heading inside sections 1-6: {h[:80]!r}")
    present = [h for h in names if h in SECTIONS]
    if present != sorted(present, key=SECTIONS.index):
        problems.append("sections are out of order")

    bodies = {}
    for k, (i, h) in enumerate(headings):
        end = headings[k + 1][0] if k + 1 < len(headings) else len(main)
        if names.count(h) == 1:
            bodies[h] = [l.strip() for l in main[i + 1:end] if l.strip()]

    for h in FINDINGS_SECTIONS:
        if h in bodies:
            _check_findings_section(h, bodies[h], problems)
    if SECTIONS[4] in bodies and not bodies[SECTIONS[4]]:
        problems.append("section '5. Cheapest kill' is empty")
    if SECTIONS[5] in bodies:
        v = bodies[SECTIONS[5]]
        m = VERDICT_RE.match(v[0]) if len(v) == 1 else None
        if not m:
            problems.append(f"section '6. Verdict' must be exactly one line: GO, NO-GO or "
                            f"RESHAPE: <one line>; found {v!r}"[:200])
        else:
            info["verdict"] = m.group(1).split(":")[0]
            info["reshape"] = m.group(2)

    tail = lines[add_idx + 1:]
    for l in tail:
        if l in SECTIONS or l == ADDITIONS:
            problems.append(f"{ADDITIONS!r} may only ADD findings; it restates {l!r}")

    region = body_region(raw)
    if region is not None and "Brief-body-sha256" in info:
        now = sha256_bytes(region)
        if now != info["Brief-body-sha256"]:
            problems.append(f"Brief-body-sha256 mismatch: sections 1-6 changed after the "
                            f"program wrote them (header {info['Brief-body-sha256'][:12]}, "
                            f"now {now[:12]})")
    info["searcher"] = info.get("Searcher")
    return problems, info


def verb_check(args):
    try:
        raw = Path(args.brief).read_bytes()
    except OSError as exc:
        sys.stderr.write(f"cannot read brief {args.brief}: {exc}\n")
        return EXIT_USAGE
    problems, info = check_brief(raw)
    if problems:
        sys.stderr.write(f"REFUSED: {os.path.abspath(args.brief)}\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        return EXIT_REFUSED
    sys.stdout.write(f"OK: {os.path.abspath(args.brief)} is a well-formed pre-flight brief "
                     f"(searcher {info['searcher']}, verdict {info['verdict']})\n")
    return EXIT_OK


# ------------------------------------------------------------------------- run

def _call_alice(prompt, cwd):
    """(returncode or None on timeout, stdout, stderr). Kills her whole process group."""
    cmd = [AGY, "-p", prompt, "--model", MODEL, "--output-format", "json"]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as exc:
        return -1, "", f"cannot start {AGY}: {exc}"
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        return None, out or "", (err or "") + f"\n[preflight: timed out after {TIMEOUT} s]"


def _files_under(d):
    found = []
    for root, dirs, files in os.walk(d):
        for f in dirs + files:
            found.append(os.path.relpath(os.path.join(root, f), d))
    return sorted(found)


def verb_run(args):
    idea_path = Path(args.idea)
    try:
        idea_raw = idea_path.read_bytes()
    except OSError as exc:
        sys.stderr.write(f"cannot read idea {args.idea}: {exc}\n")
        return EXIT_USAGE
    if len(idea_raw) > MAX_IDEA_BYTES:
        sys.stderr.write(f"idea file is {len(idea_raw)} bytes; a pre-flight idea is at most "
                         f"two pages ({MAX_IDEA_BYTES} bytes)\n")
        return EXIT_USAGE
    out = Path(args.out) if args.out else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", idea_path.stem)
    base = out / f"PREFLIGHT_{stem}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    brief_path, raw_path = Path(f"{base}.md"), Path(f"{base}.raw.json")

    scratch = tempfile.mkdtemp(prefix="preflight_alice_")
    prompt = PROMPT.replace("{idea}", idea_raw.decode("utf-8", errors="replace").rstrip("\n"))
    rc, stdout, stderr = _call_alice(prompt, scratch)
    finished = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    cwd_files = _files_under(scratch)

    record = {
        "preflight_version": VERSION,
        "idea_path": os.path.abspath(idea_path),
        "idea_sha256": sha256_bytes(idea_raw),
        "argv": [AGY, "-p", "<PROMPT with the idea>", "--model", MODEL, "--output-format", "json"],
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "started_utc": now.isoformat().replace("+00:00", "Z"),
        "finished_utc": finished.isoformat().replace("+00:00", "Z"),
        "timeout_s": TIMEOUT,
        "exit": rc,
        "alice_cwd": scratch,
        "alice_cwd_files": cwd_files,
        "agy_stdout": stdout,
        "agy_stderr": stderr,
    }
    with open(raw_path, "x", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    if rc is None:
        sys.stderr.write(f"FAILED: Alice timed out after {TIMEOUT} s; no brief written "
                         f"(raw: {raw_path})\n")
        return EXIT_ALICE
    if rc != 0:
        sys.stderr.write(f"FAILED: agy exited {rc}; no brief written (raw: {raw_path})\n"
                         f"{stderr[-500:]}\n")
        return EXIT_ALICE
    if cwd_files:
        sys.stderr.write(f"FAILED: files appeared in Alice's scratch dir {scratch}: "
                         f"{cwd_files}. She has no write path; no brief written "
                         f"(raw: {raw_path})\n")
        return EXIT_ALICE
    try:
        reply = json.loads(stdout)
        response = reply["response"]
        status = reply.get("status")
    except (ValueError, KeyError, TypeError) as exc:
        sys.stderr.write(f"FAILED: agy output is not the expected JSON ({exc}); no brief "
                         f"written (raw: {raw_path})\n")
        return EXIT_ALICE
    if status != "SUCCESS" or not isinstance(response, str):
        sys.stderr.write(f"FAILED: agy status {status!r}; no brief written (raw: {raw_path})\n")
        return EXIT_ALICE

    queries, sections_md = split_response(response)
    brief = build_brief(idea_raw, sections_md, searcher=SEARCHER, model=MODEL,
                        date_utc=now.isoformat().replace("+00:00", "Z"),
                        n_queries=len(queries))
    problems, info = check_brief(brief.encode("utf-8"))
    if problems:
        sys.stderr.write(f"REFUSED: Alice's reply does not make a well-formed brief; none "
                         f"written. Her reply is in {raw_path}\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        return EXIT_REFUSED
    with open(brief_path, "x", encoding="utf-8") as fh:
        fh.write(brief)
    sys.stdout.write(f"BRIEF: {brief_path}\nRAW:   {raw_path}\n"
                     f"verdict {info['verdict']}, {len(queries)} queries\n")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="Alice's hostile search before any seal. The brief is written by the "
                    "program from her reply; she gets no file-write path.")
    sub = parser.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("run", help="ask Alice for a pre-flight brief on an idea file")
    p.add_argument("idea")
    p.add_argument("--out", default=None, help=f"output directory (default {DEFAULT_OUT})")
    p.set_defaults(fn=verb_run)
    p = sub.add_parser("check", help="is this brief well formed and unedited?")
    p.add_argument("brief")
    p.set_defaults(fn=verb_check)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
