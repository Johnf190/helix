# cycle — the cycle-time ledger (order 27, item 16) and the attempt ledger (order 28, item 24)

> *"Log wallclock, GPU-minutes and token spend per result automatically. … Make it a number that
> prints itself next to every scored claim."*

The 55-minute seal-to-result cycle only surfaced because a reviewer read git timestamps. This
folder makes that number print itself. It is also the **independent witness** item 24's attempt
count K will be counted against, so its ledgers are written by processes **outside Odin's shell**:
`workspace/cycle` is in app.py's `WALL_PATHS`, so a write from Odin's Bash fails with
`Read-only file system`. Helix's own process and the systemd sampler are outside the wall.

## The ledgers (append-only JSONL, gitignored, UTC `Z`)

| file | written by | one line per |
|---|---|---|
| `GPU.jsonl` | `gpu_sample.py`, run every 60 s by the user timer `helix-gpu-sampler.timer` | GPU compute process per sample: `ts`, `pid`, `cmd` (`/proc` cmdline, ≤ 300 chars), `cwd`, `mem_mib`. No process → `{"ts", "idle": true}` at most every 10 min. nvidia-smi missing/failing → `{"ts", "error"}` |
| `TURNS.jsonl` | `app.py` (`_log_turn_usage`), when a chat/work turn's `ResultMessage` arrives | Helix turn: `ts`, `lane`, `model`, and the `_usage_payload` fields (`input_tokens`, `output_tokens`, `cache_read`, `cache_write`, `cost_usd`, `duration_ms`, `num_turns`) — a field the SDK did not give is absent |
| `ATTEMPTS.jsonl` | `app.py`'s `POST /attempt` (order 28), loopback only, **before the attempt's result exists** | attempt: `ts` (server clock), `seq` (line number), `prev` (sha256 of the previous line's text, `genesis` first), `scope`, `kind` (`run\|seed\|variant\|prompt`), `n`, `cmd` (≤ 300), `cwd`, optional `entry`, `pid` |
| `RESTARTS.jsonl` | `app.py` (order 31: `trigger`, `skipped`, `announced`) and `~/helix/nightly_restart.sh` (`start`, `ok`, `rolled_back`, `down`), both under `flock` on the file | event of the nightly restart: `ts`, `local` (local time: the window and "once a day" are local), `seq`, `prev` (chained like ATTEMPTS), `event`, and per event `reason`, `running_sha`/`app_sha`, `pid`/`old_pid`, `seconds`, `lkg_sha`, `failed_copy`, `why`, `of` |

Builds are measured separately: `build_orders/builder_run.sh` puts `wall_s`, `cost_usd`, `turns`
and `tokens` into each RUNS.jsonl line, read from the builder's own stream log with
`cycle.run_stats()`.

## `cycle.py`

```
cycle.py window <start> <end> [--json] [--dir D] [--label L]
    seal→score 7m38s · GPU 0 min · 14 turns · $3.10 (token spend in window)
cycle.py runstats <stream.jsonl> [--started T --ended T]
    {"wall_s": 1940, "cost_usd": 4.347333, "turns": 62, "tokens": {...}}
```

Times are read as written (`Z` or an offset); a **naive time is local time** (old RUNS lines,
old regate ledger lines, old sealgate tokens). The ledger folder is this folder, or
`HELIX_CYCLE_DIR` (tests).

`regate.py score` calls `cycle.py window <seal or register time> <now> --json` and stores the
result as the score field `cycle` (see regate's README).

## Honesty — read before quoting a number

- **An upper bound.** A window counts *all* GPU samples and *all* Helix turns in that period, not
  only the experiment's. Two things done at once are both counted. Attributing GPU and tokens to
  one experiment needs item 24's attempt log; this does not do it.
- **GPU resolution is one minute.** GPU-minutes = the number of samples of a process in the
  window, each ≈ 1 minute. **A job shorter than a minute can be missed entirely**; a job of 90 s
  counts 1 or 2. Two processes in the same minute count 2 GPU-minutes (process-minutes).
- **The timer runs only while the user's user manager runs.** `loginctl` shows `Linger=no` (2026-10-02):
  with no session open, user timers stop, and the gap shows as missing samples (no idle lines),
  not as zero.
- **Missing is not zero.** No ledger → `GPU not sampled` / `turns not logged`. A window that starts
  before a ledger's first line is labelled `(sampled from …)` / `(turns logged from …)`.
- **Turns counted:** the chat-path `_run_turn` (CHAT, WORK and any lane routed through it).
  Unsolicited proactive turns (`_NoteSink`) and turns before the restart that loads order 27 are
  not in TURNS.jsonl.
- Builds (RUNS.jsonl) are **not** in a window's token spend.

## Attempts and K (order 28, item 24)

> *"Every attempt (run, seed, variant, prompt) is logged as an event before its result is known
> (append-only). A verified claim carries K = attempts that preceded it, printed next to its score.
> K must not be self-declared — it is counted from the attempt log."*

Odin's shell cannot write `ATTEMPTS.jsonl` (the wall: `Read-only file system`). It can only ask
Helix to append, over loopback, with `attempt.py`; no route edits or deletes a line.

```
attempt.py run  --scope S [--entry 0005] [--kind run|seed|variant|prompt] [--n N] -- <command…>
    POSTs first; with a seq back, execs the command in the same pid with HELIX_ATTEMPT_SEQ=<seq>.
    Helix down or refusing → exit 5 and the command is NOT run. A sweep of 10 seeds in one
    command is --n 10.
attempt.py log  --scope S --kind prompt [--n N] --cmd "prompt variant B" [--cwd D]
attempt.py list [--scope S]           reads the ledger and checks the chain (exit 4 if broken)

cycle.py k --scope S --until <ts|now> [--json]
    K=12 (since 2026-10-02T09:14Z) · K witnessed (GPU)
    K=12 (since …) · K disputed: +1 unlogged GPU job(s)     (+ pid, first seen, cwd, cmd)
    K=12 (since …) · K not witnessed (GPU not sampled)      (no GPU.jsonl)
    K=0 · no attempts logged
    K unverifiable (chain broken at seq N)                  (never a number; exit 4)
```

**K** = the sum of `n` over this scope's lines with `ts < until` (strictly; one-second
resolution, so an attempt logged in the same second as `until` is not counted). The whole chain
is verified first: a wrong `seq`, a wrong `prev` or an unreadable line breaks it.

**The witness** (GPU.jsonl, item 16): GPU samples from the scope's first attempt to `until`, whose
process `cwd` — or an absolute path in its `cmd` — lies under a `cwd` this scope's attempts used,
and that no attempt line accounts for: not by `pid` (logged at or before the job was first seen,
any scope), and not by a `HELIX_ATTEMPT_SEQ` in the process's environment (a child of a logged
job; read from `/proc` when K is counted, so only for a job still running then). Each distinct
pid counts once. Disclosure first: K stays what the log says, and the dispute prints beside it.

> 🔴 **Honesty: the witness only sees GPU jobs that last ≥ ~1 minute in a directory the scope has
> used.** CPU-only jobs, jobs that last under a minute, and jobs started from a fresh directory
> never logged are invisible to it. **K is a lower bound that the witness can only push up.**

Also: a child process of a logged job that has **ended** before K is counted cannot be tied by
its environment (`gpu_sample.py` does not record it), so it shows as disputed: an over-count, the
safe direction. The chain catches an edited, deleted or reordered line anywhere but the
**last**; the wall is what protects the last line (Odin's shell cannot write the file at all).

## The nightly restart (order 31, item 20)

Work gets built and verified, then does not run until Helix restarts. So the nightly ends with
one: the **last** step, only when idle, health-checked, with one rollback and a loud alert.

- **Trigger (app.py, code constants only).** `scheduler_tick` arms it only for the schedule whose
  id is `NIGHTLY_RESTART_SCHEDULE_ID = "3810a43ef3c7"`. No HTTP route arms it (the run-now route
  does not). It waits until that job has finished (done or error) **and** its reply was logged
  and pushed, plus 30 s. Then: only inside **04:00–06:59 local**, at most **one sequence per
  local calendar day** (a `trigger` or `start` line dated today), and only when **idle**.
- **Idle** = no CHAT/WORK/SCHED turn lock held, no builder active, no build queue live, no
  ATTEMPTS.jsonl pid still running (a pid that started > 60 s after its attempt line is a reused
  pid and does not count), and no GPU compute process except Helix's own resident ones: this
  app.py process and `render_daemon.py` (its argv[1] resolves to `~/helix/render_daemon.py`).
  nvidia-smi failing counts as busy. Busy → re-check every 5 minutes; still busy at 06:59 →
  `skipped: busy (<what>)`.
- **The restart (`nightly_restart.sh`, own session).** `start` line → `start_helix.sh` with
  `HELIX_NO_BROWSER=1` → health within 90 s (`/stat` 200, `/model` has `odin`, exactly one
  app.py) → `ok` and `~/helix/.lkg/app.py` (+ `.sha256`) becomes last known good.
- **Not healthy → one rollback, never more.** Only if `.lkg/app.py` differs from app.py **and**
  the last `ok` line's `lkg_sha` vouches for it: the bad file is kept as
  `app.py.failed.<UTC>`, `.lkg/app.py` is restored, `start_helix.sh` runs once more, one health
  check → `rolled_back` or `down`. No `.lkg`, the same sha, or an unvouched `.lkg` → `down` with
  no second start. `down` also writes `~/Desktop/HELIX_DID_NOT_COME_BACK.txt`.
- 🔴 **The rollback restores `app.py` only.** Any other file that changed (facts, frontdesk.py,
  scheduler.py, static/, …) is **not** rolled back.
- **Visible.** On boot Helix posts one note for an unannounced outcome (and waits up to 5 min
  if its own start is still being health-checked): `🔁 Nightly restart 04:3x: healthy in N s ·
  app.py changed since last start | unchanged`, or a 🔴 line. A skip is posted when it happens:
  `Nightly restart skipped: <reason>`. Each is followed by an `announced` line.
- **Guarded.** `nightly_restart.sh` and `.lkg` are in app.py's `WALL_PATHS` and in factgate_hook's
  P6 (run) and P7 (edit) lists: Odin's shell can neither run nor edit them.

## Install (done by order 27)

```
systemctl --user daemon-reload && systemctl --user enable --now helix-gpu-sampler.timer
systemctl --user list-timers helix-gpu-sampler.timer
```

Units: `~/.config/systemd/user/helix-gpu-sampler.{service,timer}`.

## Tests

```
python3 -m pytest -q test_cycle.py
python3 -m pytest -q test_attempt.py
```

Scratch ledgers, a fake `nvidia-smi` and a stand-in `/attempt` server only; nothing here reads or
writes the live ledgers. The real route is tested in `~/helix/test_attempt_route.py`, the nightly
restart in `~/helix/test_nightly_restart.py`.
