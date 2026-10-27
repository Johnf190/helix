#!/usr/bin/env python3
"""reportcheck — a build report's checkable claims, checked against witnesses it did not write.

Order 26 (item 3 of `134_*`). A builder's report ends with a `## CLAIMS` section, one claim per
line, in exactly these forms (BUILD_PROTOCOL.md §6 item 7):

    - commit <repo dir> <hash>
    - changed <repo dir> <path relative to repo>
    - sha256 <absolute path> <hex>
    - created <absolute path>
    - tests <cwd> :: <command> :: passed <N> failed <M>
    - output <cwd> :: <command> :: contains "<text>"
    - base <repo dir> <hash>

`base` (order 53) is the commit a run started FROM: it resolves, its committer time is before the
run window, and it is an ancestor of every `commit` claimed for that repo — or, if none is
claimed, it is the repo's HEAD now (how a STOP report says "nothing committed here").

Each one is checked against git, the filesystem, or a re-run; the builder's RUNS record supplies
the run's window and its exit. Anything else in the section is a malformed line, and a malformed
line is a flag, never skipped. The report itself is never edited.

    reportcheck.py check <report> [--run-record <json or file>] [--out <path>]
    reportcheck.py claims --since <ISO UTC> <repo dir>... [--sha256 <abs path>]...

Writes <report minus .md>.check.json beside the report, or to --out <path> instead (order 41: a
shell that cannot write beside the report writes it elsewhere). Exit 0 VERIFIED, 5 FLAGGED,
6 UNCHECKED (no CLAIMS section), 2 usage. `claims` (order 53) prints the commit / changed / base /
sha256 lines for a run that began at --since, and writes nothing. See README.md for what this does NOT witness.
"""
import datetime, hashlib, json, os, re, shlex, signal, subprocess, sys, time
from pathlib import Path

HELIX = Path.home() / "helix"
SLACK = 60                       # seconds either side of the run window
PER_CMD_TIMEOUT = 300
TOTAL_TIMEOUT = 600
FINAL_LINES = ("DONE", "READY FOR /restart")
EXIT = {"VERIFIED": 0, "FLAGGED": 5, "UNCHECKED": 6}

CLAIMS_HEAD = re.compile(r"^##\s+CLAIMS\s*$")
FORMS = {
    "commit": re.compile(r"^- commit (\S+) ([0-9a-f]{7,40})$"),
    "changed": re.compile(r"^- changed (\S+) (\S+)$"),
    "sha256": re.compile(r"^- sha256 (\S+) ([0-9a-f]{64})$"),
    "created": re.compile(r"^- created (\S+)$"),
    "tests": re.compile(r"^- tests (\S+) :: (.+?) :: passed (\d+) failed (\d+)$"),
    "output": re.compile(r'^- output (\S+) :: (.+?) :: contains "(.*)"$'),
    "base": re.compile(r"^- base (\S+) ([0-9a-f]{7,40})$"),
}
HEADING = re.compile(r"^#{1,6}\s")
HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
SUMMARY = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)\b")


def iso(ts):
    """Epoch → UTC with a Z (order 27: every build timestamp is UTC)."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    """ISO time → epoch. `Z` or an offset as written; a naive time (a RUNS line from before
    order 27) is local time."""
    d = datetime.datetime.fromisoformat(text)
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def path_of(text):
    return Path(os.path.expanduser(text))


# ------------------------------------------------------------------ parsing
def headings(lines):
    """Indexes of markdown heading lines, skipping fenced code blocks (a `# comment` inside a
    ``` block is not a heading, so it can neither end a section nor hide a hash)."""
    out, fence = [], False
    for i, l in enumerate(lines):
        if l.lstrip().startswith("```"):
            fence = not fence
        elif not fence and HEADING.match(l):
            out.append(i)
    return out


def sections(lines):
    """[(heading, [lines])] for every heading; text before the first is heading ''."""
    idx = headings(lines)
    out = [("", lines[:idx[0]] if idx else lines)]
    for k, i in enumerate(idx):
        end = idx[k + 1] if k + 1 < len(idx) else len(lines)
        out.append((lines[i], lines[i + 1:end]))
    return out


def claims_block(lines):
    """(the CLAIMS section's lines, problems) or (None, []). The report's last non-blank line,
    if it is a final line (DONE / READY FOR /restart / STOP…), is not part of the section."""
    idx = headings(lines)
    heads = [i for i in idx if CLAIMS_HEAD.match(lines[i].rstrip())]
    if not heads:
        return None, []
    problems = [f"{len(heads)} '## CLAIMS' headings; only the last one is read"] if len(heads) > 1 else []
    start = heads[-1]
    end = next((i for i in idx if i > start), len(lines))
    body = [l.rstrip() for l in lines[start + 1:end]]
    nonblank = [i for i, l in enumerate(body) if l.strip()]
    if nonblank and end == len(lines):
        last = body[nonblank[-1]].strip()
        if last in FINAL_LINES or last.startswith("STOP"):
            body[nonblank[-1]] = ""
    return [l for l in body if l.strip()], problems


def parse(line):
    for kind, rx in FORMS.items():
        m = rx.match(line)
        if m:
            g = m.groups()
            if kind in ("sha256", "created") and not path_of(g[0]).is_absolute():
                return None
            return kind, g
    return None


# ------------------------------------------------------------------ re-running
def _brain_venv_python(exe):
    """ORDER 63: is `exe` the brain's own venv python by its absolute path? Its directory is compared
    after realpath and its name exactly: the venv's python is itself a symlink to the system one, so
    comparing the resolved file would let every python3.12 on the machine through."""
    return (os.path.isabs(exe) and os.path.basename(exe) == "python"
            and os.path.realpath(os.path.dirname(exe)) == os.path.realpath(HELIX / ".venv" / "bin"))


def allowed_command(cmd):
    """(ok, argv or reason). Only `python3 -m pytest …`, `python3 <path>/test_*.py`, and the same
    two through `.venv/bin/python` or (order 63) the brain's own venv python by its absolute path.
    No shell: the argv is executed as split."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return False, f"cannot split ({e})"
    if len(argv) < 2 or not (argv[0] in ("python3", ".venv/bin/python") or _brain_venv_python(argv[0])):
        return False, "not one of the allowed forms (python3 / .venv/bin/python)"
    if argv[1:3] == ["-m", "pytest"]:
        return True, argv
    if len(argv) == 2 and re.fullmatch(r"test_[^/]*\.py", os.path.basename(argv[1])):
        return True, argv
    return False, "not one of the allowed forms (-m pytest … / <path>/test_*.py)"


class Budget:
    def __init__(self, per_cmd, total):
        self.per_cmd, self.left = per_cmd, total

    def run(self, cwd, argv):
        """(rc, output) or raises TimeoutError. The whole process group dies on a timeout."""
        allow = min(self.per_cmd, self.left)
        if allow <= 0:
            raise TimeoutError("the 600 s total re-run budget is spent")
        exe = argv[0] if argv[0] == "python3" else str(Path(cwd) / argv[0])
        t0 = time.time()
        p = subprocess.Popen([exe, *argv[1:]], cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", start_new_session=True)
        try:
            out, _ = p.communicate(timeout=allow)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            self.left -= time.time() - t0
            raise TimeoutError(f"no result after {allow:.0f} s")
        finally:
            if p.poll() is None:
                p.kill()
        self.left -= time.time() - t0
        return p.returncode, out


def summary_counts(out):
    """Counts from pytest's last summary line ('N passed, M failed in 1.2s'), or None."""
    for l in reversed(out.splitlines()):
        if re.search(r"\bin [\d.]+s\b", l) and SUMMARY.search(l):
            c = {}
            for n, word in SUMMARY.findall(l):
                word = {"error": "errors", "warning": "warnings"}.get(word, word)
                c[word] = c.get(word, 0) + int(n)
            return c
    return None


def tail(text, n=15):
    return "\n".join(text.strip().splitlines()[-n:])


# ------------------------------------------------------------------ the check
def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def entry(kind, claim, ok, said, witness, output=None):
    e = {"kind": kind, "claim": claim, "ok": ok, "said": said, "witness": witness}
    if output is not None:
        e["output"] = output
    return e


def check(report, run_record, root=HELIX, per_cmd_timeout=PER_CMD_TIMEOUT,
          total_timeout=TOTAL_TIMEOUT):
    lines = Path(report).read_text(encoding="utf-8", errors="replace").splitlines()
    block, problems = claims_block(lines)
    res = {"report": str(report), "checked_at": iso(time.time()), "run_record": run_record,
           "claims": []}
    if block is None:
        res["verdict"] = "UNCHECKED"
        return res
    res["claim_lines"] = len(block)
    out = res["claims"]
    for p in problems:
        out.append(entry("malformed", "## CLAIMS", False, p, "a report has one CLAIMS section"))
    root = Path(root).resolve()
    budget = Budget(per_cmd_timeout, total_timeout)

    # the run record: the window, and the builder's own outcome
    window = None
    if run_record is None:
        out.append(entry("run", "run record", False, "(a run this report belongs to)",
                         "no run record given — the window and the builder's exit are unwitnessed"))
    else:
        try:
            window = (epoch(run_record["started"]) - SLACK, epoch(run_record["ended"]) + SLACK)
        except Exception as e:
            out.append(entry("run", "run window", False, "(started/ended)",
                             f"run record has no usable window ({type(e).__name__}: {e})"))
        x = run_record.get("exit")
        out.append(entry("run", "run exit", x == 0, "builder exit 0", f"RUNS exit {x}"))
        x = run_record.get("report_exists")
        out.append(entry("run", "run report_exists", x is True, "report_exists true",
                         f"RUNS report_exists {x}"))

    def in_window(ts):
        return window is not None and window[0] <= ts <= window[1]

    def window_text():
        return "no window" if window is None else f"{iso(window[0] + SLACK)} … {iso(window[1] - SLACK)} ±{SLACK}s"

    commits = {}            # repo -> {claimed hash: full hash or None}
    changed = {}            # repo -> {path: claim text}
    bases = []              # (repo, claimed hash, claim text)
    pending = []
    for line in block:
        p = parse(line)
        if p is None:
            out.append(entry("malformed", line, False, line, "not one of the seven claim forms"))
            continue
        kind, g = p
        claim = line[2:]
        if kind == "commit":
            repo = path_of(g[0]).resolve()
            r = git(repo, "rev-parse", "--verify", "--quiet", f"{g[1]}^{{commit}}")
            full = r.stdout.strip() if r.returncode == 0 else None
            commits.setdefault(repo, {})[g[1]] = full
            if not full:
                out.append(entry(kind, claim, False, f"commit {g[1]} in {repo}",
                                 f"does not resolve to a commit in {repo}"))
                continue
            ct = int(git(repo, "log", "-1", "--format=%ct", full).stdout.strip())
            ok = in_window(ct)
            out.append(entry(kind, claim, ok, f"commit {g[1]}, made during this run",
                             f"{full[:12]} committed {iso(ct)}"
                             + ("" if ok else f" — outside the run window ({window_text()})")))
        elif kind == "changed":
            changed.setdefault(path_of(g[0]).resolve(), {})[g[1]] = claim
        elif kind == "base":
            bases.append((path_of(g[0]).resolve(), g[1], claim))
        else:
            pending.append((kind, g, claim))

    # changed: completeness both ways
    for repo in sorted(set(commits) | set(changed), key=str):
        touched = {}
        for h, full in commits.get(repo, {}).items():
            if not full:
                continue
            r = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", full)
            for f in filter(None, r.stdout.split("\0")):
                touched.setdefault(f, []).append(full[:7])
        r = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
        status, items, i = set(), r.stdout.split("\0"), 0
        while i < len(items):
            it = items[i]
            if len(it) > 3:
                status.add(it[3:])
                if it[0] in "RC":           # rename/copy: the next field is the source path
                    i += 1
                    if i < len(items) and items[i]:
                        status.add(items[i])
            i += 1
        listed = changed.get(repo, {})
        for f, claim in sorted(listed.items()):
            if f in touched:
                out.append(entry("changed", claim, True, f, f"touched by {', '.join(touched[f])}"))
            elif f in status:
                out.append(entry("changed", claim, True, f, "uncommitted, in git status"))
            else:
                out.append(entry("changed", claim, False, f,
                                 "neither touched by a claimed commit nor in git status"))
        for f in sorted(set(touched) - set(listed)):
            out.append(entry("changed", f"changed {repo} {f}", False, "(absent)",
                             f"touched by claimed commit {', '.join(touched[f])}, not listed"))

    # base: the commit the run started from (order 53)
    for repo, h, claim in bases:
        r = git(repo, "rev-parse", "--verify", "--quiet", f"{h}^{{commit}}")
        full = r.stdout.strip() if r.returncode == 0 else None
        if not full:
            out.append(entry("base", claim, False, f"base {h} in {repo}",
                             f"does not resolve to a commit in {repo}"))
            continue
        why = []
        ct = int(git(repo, "log", "-1", "--format=%ct", full).stdout.strip())
        if window is None:
            why.append(f"committed {iso(ct)}, not before the run window — no window")
        elif not ct < window[0]:
            why.append(f"committed {iso(ct)}, not before the run window ({window_text()})")
        if repo in commits:
            for c, cfull in commits[repo].items():
                if cfull and git(repo, "merge-base", "--is-ancestor", full, cfull).returncode != 0:
                    why.append(f"not an ancestor of claimed commit {cfull[:12]}")
            relation = "ancestor of every claimed commit"
        else:
            head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").stdout.strip()
            if head != full:
                why.append(f"no commit claimed in {repo}, and HEAD is {head[:12] or '(none)'}, not the base")
            relation = "no commit claimed, and it is HEAD"
        out.append(entry("base", claim, not why, f"base {h}, the commit this run started from",
                         f"{full[:12]} " + ("; ".join(why) if why
                                            else f"committed {iso(ct)}, before the run; {relation}")))

    for kind, g, claim in pending:
        if kind == "sha256":
            p = path_of(g[0])
            try:
                now = sha256_file(p)
            except OSError as e:
                out.append(entry(kind, claim, False, g[1], f"cannot read: {e.strerror}"))
                continue
            out.append(entry(kind, claim, now == g[1], g[1], now))
        elif kind == "created":
            p = path_of(g[0])
            if not p.exists():
                out.append(entry(kind, claim, False, f"{p} created", "does not exist"))
                continue
            mt = p.stat().st_mtime
            ok = in_window(mt)
            out.append(entry(kind, claim, ok, f"{p} created during this run",
                             f"exists, mtime {iso(mt)}"
                             + ("" if ok else f" — outside the run window ({window_text()})")))
        else:                               # tests / output: re-run
            cwd, cmd = path_of(g[0]), g[1]
            said = (f"passed {g[2]} failed {g[3]}" if kind == "tests" else f'contains "{g[2]}"')
            okc, argv = allowed_command(cmd)
            if not okc:
                out.append(entry(kind, claim, False, said, f"not re-runnable: {argv} — not run"))
                continue
            rcwd = cwd.resolve()
            if not (rcwd == root or root in rcwd.parents):
                out.append(entry(kind, claim, False, said,
                                 f"not re-runnable: cwd {rcwd} is outside {root} — not run"))
                continue
            if not rcwd.is_dir():
                out.append(entry(kind, claim, False, said, f"cwd {rcwd} does not exist"))
                continue
            try:
                code, text = budget.run(rcwd, argv)
            except TimeoutError as e:
                out.append(entry(kind, claim, False, said, f"UNVERIFIED: timeout — {e}"))
                continue
            except OSError as e:
                out.append(entry(kind, claim, False, said, f"could not run: {e}"))
                continue
            if kind == "output":
                ok = g[2] in text
                out.append(entry(kind, claim, ok, said,
                                 f"exit {code}, text {'found' if ok else 'NOT found'} in output",
                                 tail(text)))
                continue
            c = summary_counts(text)
            if c is None:
                out.append(entry(kind, claim, False, said,
                                 f"exit {code}, no pytest summary line in the output", tail(text)))
                continue
            got = f"passed {c.get('passed', 0)} failed {c.get('failed', 0)}"
            why = []
            if got != said:
                why.append(got)
            if c.get("errors"):
                why.append(f"{c['errors']} error(s)")
            if int(g[3]) == 0 and code != 0:
                why.append(f"exit {code}")
            out.append(entry(kind, claim, not why, said,
                             f"{got}, exit {code}" + (" — " + "; ".join(why) if why else ""),
                             tail(text)))

    # the prose scan: every hash in the "Commit hash" section is covered by a commit or base claim
    known = [(h, full) for per in commits.values() for h, full in per.items()]
    for repo, h, _ in bases:
        r = git(repo, "rev-parse", "--verify", "--quiet", f"{h}^{{commit}}")
        known.append((h, r.stdout.strip() if r.returncode == 0 else None))
    for head, body in sections(lines):
        if "commit hash" not in head.lower():
            continue
        for tok in dict.fromkeys(HEX_TOKEN.findall("\n".join(body))):
            ok = any((full or h).startswith(tok) or tok.startswith(h) for h, full in known)
            out.append(entry("prose", f"prose {tok}", ok, f"{tok} in '{head.strip()}'",
                             "covered by a commit or base claim" if ok
                             else "no commit or base claim covers it"))

    if not any(e["kind"] in ("commit", "sha256", "base") for e in out):
        out.append(entry("anchor", "anchor", False, "(claims about the disk)",
                         "no commit or sha256 claim, and no base claim — nothing ties the report to the disk"))
    res["verdict"] = "VERIFIED" if all(e["ok"] for e in out) else "FLAGGED"
    return res


def describe(e):
    return f"{e['claim']} — report said: {e['said']}; witness: {e['witness']}"


# ------------------------------------------------------------------ CLI
def usage(msg):
    print("usage: reportcheck.py check <report> [--run-record <json or file>] [--out <path>]\n"
          f"       reportcheck.py claims --since <ISO UTC> <repo dir>... [--sha256 <abs path>]...\n{msg}",
          file=sys.stderr)
    return 2


def generate(since, repos, files):
    """The commit / changed / base / sha256 CLAIMS lines for a run that began at `since` (epoch).
    Read-only: git log, git diff-tree and file reads; nothing is written."""
    lines = []
    for repo in repos:
        made = []
        for l in git(repo, "log", "--date-order", "--reverse", "--format=%H %ct", "HEAD").stdout.splitlines():
            full, ct = l.split()
            if int(ct) >= since:
                made.append(full)
        touched = set()
        for full in made:
            lines.append(f"- commit {repo} {full[:12]}")
            r = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", full)
            touched.update(filter(None, r.stdout.split("\0")))
        lines += [f"- changed {repo} {f}" for f in sorted(touched)]
        r = git(repo, "rev-parse", "--verify", "--quiet", f"{made[0]}^1" if made else "HEAD")
        if r.returncode == 0:               # an oldest commit that is a root has no base
            lines.append(f"- base {repo} {r.stdout.strip()[:12]}")
    lines += [f"- sha256 {p} {sha256_file(p)}" for p in files]
    return lines


def claims_main(argv):
    since, repos, files, i = None, [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--since", "--sha256"):
            if i + 1 >= len(argv) or (a == "--since" and since is not None):
                return usage("bad arguments")
            if a == "--since":
                since = argv[i + 1]
            else:
                files.append(argv[i + 1])
            i += 2
        elif a.startswith("--"):
            return usage(f"unknown option {a}")
        else:
            repos.append(a)
            i += 1
    if since is None or not repos:
        return usage("claims needs --since and at least one repo dir")
    try:
        d = datetime.datetime.fromisoformat(since)
        if d.tzinfo is None:
            raise ValueError("no Z or offset — a run's start is written in UTC")
    except ValueError as e:
        return usage(f"--since is not an ISO time in UTC: {since} ({e})")
    roots = []
    for text in repos:
        p = path_of(text).resolve()
        top = git(p, "rev-parse", "--show-toplevel") if p.is_dir() else None
        if top is None or top.returncode != 0 or Path(top.stdout.strip()).resolve() != p:
            return usage(f"not the top of a git repo: {text}")
        if git(p, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").returncode != 0:
            return usage(f"repo has no HEAD commit: {text}")
        roots.append(p)
    paths = []
    for text in files:
        p = path_of(text)
        if not p.is_absolute() or not p.is_file() or not os.access(p, os.R_OK):
            return usage(f"--sha256 needs a readable file by absolute path: {text}")
        paths.append(p)
    print("\n".join(generate(d.timestamp(), roots, paths)))
    return 0


def main(argv):
    if argv[:1] == ["claims"]:
        return claims_main(argv[1:])
    if len(argv) < 2 or argv[0] != "check" or len(argv) % 2:
        return usage("bad arguments")
    opts = {}
    for flag, value in zip(argv[2::2], argv[3::2]):
        if flag not in ("--run-record", "--out") or flag in opts:
            return usage("bad arguments")
        opts[flag] = value
    report = Path(argv[1])
    if not report.is_file():
        return usage(f"no such report: {report}")
    if "--out" in opts:
        dest = Path(opts["--out"])
        if dest.is_dir() or not dest.parent.is_dir():
            return usage(f"--out must be a file path in an existing directory: {dest}")
    else:
        name = report.name[:-3] if report.name.endswith(".md") else report.name
        dest = report.with_name(name + ".check.json")
    rec = None
    if "--run-record" in opts:
        raw = opts["--run-record"]
        try:
            if os.path.isfile(raw):
                raw = Path(raw).read_text(encoding="utf-8").strip().splitlines()[-1]
            rec = json.loads(raw)
            if not isinstance(rec, dict):
                raise ValueError("not a JSON object")
        except Exception as e:
            return usage(f"--run-record is not a JSON object or a file holding one ({e})")
    res = check(report, rec)
    res["report_sha256"] = sha256_file(report)
    res["reportcheck_sha256"] = sha256_file(__file__)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    bad = [e for e in res["claims"] if not e["ok"]]
    v = res["verdict"]
    if v == "VERIFIED":
        print(f"VERIFIED ({res['claim_lines']} claims, {len(res['claims'])} checks)")
    elif v == "UNCHECKED":
        print("UNCHECKED (no CLAIMS section)")
    else:
        print(f"FLAGGED: {describe(bad[0])}")
        print(f"{len(bad)} of {len(res['claims'])} checks failed ({res['claim_lines']} claim lines):")
        for e in bad:
            print(f"  ✗ [{e['kind']}] {describe(e)}")
    print(f"wrote {dest}")
    return EXIT[v]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
