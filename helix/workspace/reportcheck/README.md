# reportcheck — a build report's claims, checked against witnesses it did not write

Order 26, item 3 of `134_*`: *"Before a job report is logged, its checkable claims are verified
against witnesses the text did not write. Mismatch ⇒ the report lands flagged, never silently."*

The principle comes from crosscheck: the witness must not be the reporter. The statistics do not
carry over, and nothing here imports crosscheck.

```
python3 reportcheck.py check <report.md> [--run-record <json or file>] [--out <path>]
python3 reportcheck.py claims --since <ISO UTC> <repo dir>... [--sha256 <abs path>]...
```

`--run-record` is the builder's RUNS line, given either as JSON text or as a file (the file's
last line is read). It supplies the run window (`started` … `ended`) and the builder's `exit` and
`report_exists`. `builder_run.sh` passes it on every run, before it writes that line.

Exit codes: **0 VERIFIED**, **5 FLAGGED**, **6 UNCHECKED** (no `## CLAIMS` section),
**2 usage** (bad arguments, a missing report, or a run record that is not JSON). The result is
written to `<report minus .md>.check.json` beside the report, or only to `--out <path>` when
given (order 41: a shell that cannot write beside the report writes it to `/tmp`). That file holds the verdict, every
check with what the report said and what the witness said, the re-run output tails, the report's
sha256, and reportcheck's own sha256. **The report itself is never edited.**

## The claims (BUILD_PROTOCOL.md §6 item 7)

| claim | witness |
|---|---|
| `- commit <repo> <hash>` | `git rev-parse --verify <hash>^{commit}` resolves, and its **committer** time lies in the run window ± 60 s |
| `- changed <repo> <path>` | **both ways**: every file touched by a claimed commit (`git diff-tree --root -r`) must have a `changed` line; every `changed` line must be touched by a claimed commit or appear in `git status --porcelain --untracked-files=all`. An omitted file is flagged under its own name, with `said: (absent)`. |
| `- sha256 <abs path> <hex>` | the file's sha256 now |
| `- created <abs path>` | exists, mtime in the run window ± 60 s |
| `- tests <cwd> :: <cmd> :: passed N failed M` | **re-run**. Counts come from pytest's last summary line. Any `error`, or a non-zero exit when M = 0, also fails it. |
| `- output <cwd> :: <cmd> :: contains "<text>"` | **re-run**, and the text must appear in stdout+stderr |
| `- base <repo> <hash>` | (order 53) resolves; its **committer** time is before the run window's start − 60 s; it is an ancestor (`git merge-base --is-ancestor`) of every `commit` claimed for that repo, or, if none is claimed, it **is** the repo's HEAD now. No run record ⇒ flagged, "no window". |

Rules:
- **Re-runs.** Only `python3 -m pytest …`, `python3 <path>/test_*.py`, and the same two through
  `.venv/bin/python` (relative to the cwd). The argv is executed as split, with no shell, so
  `;`, `&&` and `$(…)` are just characters. The cwd must resolve to a path under `~/helix`; the
  CLI has no option or environment variable that widens this. Each command gets 300 s and all
  re-runs together get 600 s. A timeout is `UNVERIFIED: timeout`, which is a flag, not a pass.
  On a timeout the command's whole process group is killed.
- **Run record.** `exit` must be 0 and `report_exists` true. If no record is given, that is a
  flag. Every time-bound claim (`commit`, `created`) then flags too, because there is no window.
  Since order 27 builder_run writes `started` and `ended` in UTC with a `Z`; a naive time (a
  RUNS line from before order 27) is still read as local time. A naive time inside the repeated
  hour when DST ends (01:00–02:00 local on 2026-10-25) is read as the first, BST, occurrence —
  the ambiguity the `Z` removes. Times this tool writes (`checked_at`, mtimes in witnesses) are
  UTC `Z`.
- **The prose scan.** Every 7–40-character lowercase-hex token in a section whose heading
  contains "Commit hash" must be covered by a `commit` or `base` claim, meaning one is a prefix
  of the other's resolved hash. A hash in the prose with no claim is a flag.
- **Parsing.** The CLAIMS section runs from the last `## CLAIMS` heading to the next heading or
  to the end of the file. The report's last non-blank line is not a claim if it is `DONE`,
  `READY FOR /restart` or `STOP…`. Headings inside fenced code blocks are ignored, so a
  `# comment` in a ``` block can neither end a section nor hide a hash. A second `## CLAIMS`
  heading is a flag.
- **VERIFIED** needs every check to hold **and** at least one `commit`, `sha256` or `base`
  claim. Without one, nothing ties the report to the disk, so the result is FLAGGED (`anchor`).
  A STOP report that changed nothing anchors with `- base <repo> <HEAD>` for each repo.

## The claims generator (order 53)

`claims --since <ISO UTC> <repo dir>... [--sha256 <abs path>]...` prints CLAIMS lines to stdout
and writes nothing. For each repo, in the order given: every commit reachable from HEAD with
committer time ≥ `--since`, oldest first (`- commit <repo> <12 chars>`); every path those commits
touch, deduplicated and sorted (`- changed`); and the first parent of the oldest of them, or HEAD
if there are none (`- base`). If the oldest is a root commit there is no base line. Then a
`- sha256` line per `--sha256` file, hashed now. Repo paths are printed resolved and absolute.
It does not write `tests`, `output` or `created` lines. Exit 0, or 2 on usage: `--since` without
a `Z` or offset, a path that is not the top of a git repo (or has no HEAD), a `--sha256` path
that is not an absolute readable file. On exit 2 nothing is printed to stdout.

## Wiring

- `build_orders/builder_run.sh` `finish()` runs reportcheck after the builder exits and **before**
  it writes the RUNS line. The line gains `"report_check": "VERIFIED" | "FLAGGED" | "UNCHECKED"
  | "ERROR: …"`. The inbox note gains `· report check: VERIFIED (k claims)` or
  `· ⚠ REPORT FLAGGED: <first failure>`. A crash, a missing reportcheck, a missing report (exit 2)
  or a SIGTERM during the check is recorded as `ERROR: …`, and the line and note are still
  written. When the run was itself stopped (exit 143/99), the check gets 60 s instead of 900 s,
  because systemd sends SIGKILL 90 s after SIGTERM.
- **Flag, not block**, for a single `/build`: the verdict is recorded and nothing is undone.
- `build_orders/run_queue.sh` `gate()`: a queue halts unless `report_check` is exactly
  `VERIFIED`. A RUNS line from a pre-order-26 `builder_run.sh` has no verdict, so it halts too.

## What this does NOT witness (known residues — written down, not solved)

- **The builder controls the clocks.** It could set `GIT_COMMITTER_DATE` to put a commit inside
  the window, or `touch` a file's mtime. The window checks catch honest mistakes (claiming an old
  commit or a file that was already there). They do not stop a builder that intends to deceive.
- **A test can be written to pass.** `tests` re-runs prove that the suite says what the report
  says *now*. They do not prove that the suite tests anything. The same goes for `output`.
- **reportcheck witnesses that the claims match the disk, not that the work is good.** Whether
  the work is good stays with Odin's review and with item 9's reviewer.
- It only checks what is claimed. A report with no `changed` lines and no commits, but a
  `sha256` on one file, can be VERIFIED. The omission check works per claimed commit, so an
  unclaimed commit is invisible to it. The prose scan exists to catch the commonest form of this:
  a hash the report names but does not claim.
- Re-runs execute the builder's own test code under the reportcheck process's user. The cwd
  fence and the command shapes limit *what* is run, not what that code does.
- Running a re-run can have side effects that are normal for that suite. For example, regate's
  suite appends to its gitignored `refusals.log`.

## Tests

`python3 -m pytest -q test_reportcheck.py`. Every test builds a scratch git repo under tmp_path,
and nothing reads the live build_orders, regate or memory. A test that injects one false claim
asserts that **exactly one** check fails and that it is the injected one. The exception is a
replaced commit hash: the `changed` lines that commit covered then fail too, and the test says so. The builder_run and queue wiring is
tested in `~/helix/test_build_queue.py`.
