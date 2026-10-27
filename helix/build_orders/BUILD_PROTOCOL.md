# BUILD PROTOCOL — standing rules for any Claude Code session executing a work order
# Written by Odin for the user, 2026-09-23. Read this fully before touching anything.

You are a **separate Claude Code session**, run by the user in a terminal. You are executing **one**
numbered work order from `~/helix/build_orders/`. You are not Odin, you do not run inside Helix,
and you must not behave as though you have Odin's standing permissions.

## 0. THE PRIME DIRECTIVE
**One order. Then stop and report.** Do not start the next one. Do not "also fix" something you
noticed. Do not refactor. If the order is finished early, stop early.

## 1. HARD SCOPE FENCES — never touch these unless the order names the exact file
| path | rule |
|---|---|
| `~/helix/memory/facts/**` | 🔴 **READ ONLY. NEVER WRITE.** The fact base is Odin's record; edits here are how a memory system gets quietly rewritten. |
| `~/helix/app.py` and anything imported at start-up | only if the order names it, and only with §3's backup + rollback |
| `~/helix/workspace/crosscheck/**` | 🔴 **READ ONLY.** Sealed pre-registrations and results. Never amend a seal or a result. |
| `~/helix/workspace/box2_recovered/**` | 🔴 **READ ONLY ARCHIVE.** Recovered originals. Never modify. |
| anything containing credentials (`.env`, `*credentials*`, `*accounts*`) | 🔴 **DO NOT OPEN, DO NOT PRINT, DO NOT COPY.** |
| `git` history | **never** rewrite, force-push, rebase or amend past commits |
| `__VAULT_DRIVE__/**` (backup drive) | read only |

## 2. THINGS YOU MAY NEVER DO
- **No subagents, no fan-out, no parallel agent swarms.** (A past incident burned ~267M tokens and
  hundreds of dollars this way. Not negotiable.)
- **No network installs** beyond what the order names. `pip` is PEP-668 blocked here; use
  `~/.local/bin/virtualenv` if a venv is genuinely required and the order allows it.
- **No restarting Helix** unless the order explicitly says to.
- **No deleting anything.** Move to `~/helix/build_orders/quarantine/` instead.
- **No editing the governance code** (gates, write path, review stamp, metric) unless the order is
  specifically about it and the user is watching.

## 3. IF THE ORDER TOUCHES THE LIVE PROCESS (`app.py` or start-up imports)
1. `cp app.py app.py.bak.<timestamp>` **before any edit.**
2. Make the edit.
3. `python3 -c "import ast; ast.parse(open('app.py').read())"` — syntax must pass.
4. Restart only if the order says to, then **health-check**.
5. 🔴 **If it does not come up healthy within the timeout, restore the `.bak` and restart.** Then
   stop and report the failure. **Do not attempt a second fix.**

## 4. WORK METHOD
- Work on a branch: `git checkout -b order-<NN>-<slug>`.
- **Write the test first** where the order specifies acceptance criteria.
- **A test's reference point is a COMMIT, never a branch name or a live path** (orders 07–09 each
  broke on a test comparing against something a cutover had moved).
- Run the acceptance criteria and **paste the real output** into your report. Never assert a check
  passed without showing it.
- Commit with the numbers in the message. 🔴 **MEASURE FIRST, THEN WRITE THE MESSAGE** (added
  2026-09-23 after orders 03 and 06 both shipped stale numbers): run `git diff --cached --stat`,
  `wc -l` and the test count **immediately before `git commit`**, and paste those figures in. A
  message drafted earlier is a draft, not a record.
  **Two commands, never one:** measure in the first, read its output, write the message in the
  second. A heredoc composed in the same command as the measurement cannot contain it (order 07). Attribution line:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## 5. STOP CONDITIONS — halt and report, do not improvise
- The order requires touching something in §1 that it did not name.
- An acceptance criterion fails twice.
- You find a defect outside the order's scope → **write it in the report, do not fix it.**
- Anything is ambiguous. **Ambiguity is a stop condition, not an invitation.**
- 🔴 **A stop condition that fires means STOP — even when the cause looks trivial** (added after
  order 09, where a builder moved a stray `.pyc` and carried on). Judging that the condition's
  *purpose* is still met is exactly the judgement the condition exists to take away from you.
  Report it; Odin and the user make the call. It costs one round trip.

## 6. REPORT FORMAT (write to `~/helix/build_orders/reports/ORDER_<NN>_report.md`)
1. **What was built** — files created/modified, with line counts.
2. **Acceptance criteria** — each one, with the **actual command output pasted**.
3. **Numbers** — before/after measurements the order asked for.
4. **Out-of-scope defects noticed** — listed, not fixed.
5. **Anything you were unsure about.**
6. **Commit hash.**
7. **`## CLAIMS`** (from order 26) — the report's last section, *before* the final DONE / READY /
   STOP line. One claim per line, exactly these forms; anything else in the section is a malformed
   line, and a malformed line flags the report (it is never skipped):
   ```
   - commit <repo dir> <hash>
   - changed <repo dir> <path relative to repo>
   - sha256 <absolute path> <hex>
   - created <absolute path>
   - tests <cwd> :: <command> :: passed <N> failed <M>
   - output <cwd> :: <command> :: contains "<text>"
   - base <repo dir> <hash>
   ```
   `base` (order 53): the commit you started from; or HEAD, if you committed nothing in that repo.
   `builder_run.sh` runs `workspace/reportcheck/reportcheck.py` on it before writing the RUNS
   line, against witnesses the report did not write: git (a `commit` must resolve and be made
   during the run), the disk (`sha256` now; `created` exists with an mtime inside the run), and a
   re-run (`tests` / `output`). So:
   - **List every file your claimed commits touch** as a `changed` line. An omitted file is a flag.
   - Files outside a git repo (anything in `~/helix` itself) get a `sha256` line.
   - `tests` / `output` commands must be `python3 -m pytest …`, `python3 <path>/test_*.py`, or the
     same two through `.venv/bin/python`, with the cwd under `~/helix`. Nothing else is re-run.
   - Every hash you write in the "Commit hash" section needs a `commit` claim.
   - In the Commit hash section write only commits made in this run. A branch base goes in a
     `base` claim, not in prose.
   - A STOP report that changed nothing claims `- base <repo> <HEAD>` for each repo the order names.
   - Generate the `commit` / `changed` / `base` / `sha256` lines with
     `python3 ~/helix/workspace/reportcheck/reportcheck.py claims --since <your session start, UTC> <every repo you touched> [--sha256 …]`
     and paste its output. Do not hand-type hashes.
   A report with no CLAIMS section is UNCHECKED; anything but VERIFIED halts a `/build` queue.

## 7. WHY THE FENCES ARE THIS TIGHT
On 2026-09-22 a result was built, scored a pass, and killed 12 hours later by a cold review that
found 14 flaws — several of which were visible in the code before any data was collected. The
lesson was not "try harder". It was that **the thing being changed must not be the thing verifying
the change.** You are the hands. Odin writes the orders, the user watches, and a reviewer checks the
result. Keep those roles separate and this works.

## 7. RUN FROM HELIX (from order 16, 2026-09-25)
You may be launched headless by `build_orders/run_order.sh` on the user's `/build NN` in Helix's chat,
instead of in a terminal. Everything above still applies. In addition:
- There is no one to ask. Every "ask the user" in this protocol means: STOP and write it in the report.
- Never restart Helix, even if the order says to. End the report with "READY FOR /restart" if the
  order needs one; the user types /restart.
- You are capped at 90 minutes and $25. If you are near either, stop and report where you are.
- (order 64) `builder_run.sh`, `run_order.sh` and `run_queue.sh` may be running while you build.
  Change them only by writing a complete new file beside them and `mv`-ing it over the old one.
  Keep every executable line inside `main()` with `main "$@"; exit $?` as the last line.
