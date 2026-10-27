# HELIX BLUEPRINT — for the Claude that is setting it up

You are helping a person install and adapt **Helix**, a persistent personal assistant built on Claude Code.
This kit is a cleaned copy of a working system, taken from a single-user machine where it has run daily
for months. Your job: get it running on THIS machine, for THIS person, and verify every step. Report
honestly: if a check fails, say so and fix it or say what is blocking. Do not claim a step worked without
running its check.

---------------------------------------------------------------------------------------------------------
## 1. What Helix is

A Flask app (`app.py`, port 7777) with a browser interface (`static/index.html`). The person talks to:

- **Odin**: Claude, run through the Claude Agent SDK on the person's own Claude subscription (no API key;
  `app.py` deliberately removes `ANTHROPIC_API_KEY` from the environment so the CLI uses the login).
- **Alice**: Gemini, through the Gemini CLI installed as `agy`. Optional. She reviews and challenges Odin.
- **The front desk**: a small, fast model with no tools that answers trivial messages ("hi", "thanks")
  and hands everything real to Odin (`frontdesk.py`).
- **A council** (`/council`): open models through OpenRouter, for second opinions. Optional; needs
  `OPENROUTER_API_KEY` in `~/.bashrc`.

Work runs in **lanes** so a long job does not block conversation: `chat` (talking), `work` (long
background jobs), `sched` (scheduled jobs, `scheduler.py`), `front` (the front desk). When a background
job finishes, Helix reports back on its own.

## 2. Memory — the whole point

- `memory/BRAIN.md`: the charter. Who the person is, how Odin should behave, the standing rules.
  Loaded on every turn. Written by the installer from the person's answers; grows with use.
- `memory/facts/NNN_name.md`: one durable fact per file. Title line first. A **new** fact must carry a
  `Category:` line in its first 15 lines (names come from `workspace/categories/CATEGORIES.txt`); the fact
  gate refuses one without it. Facts saved from the chat with `/remember` get a guessed category written
  for them. Older facts can be placed through `workspace/categories/OVERRIDES.csv` instead.
- Aliases: a fact may say `Also called: a; b` in its first 15 lines (or list them in
  `workspace/memretrieve/ALIASES.csv`), so the person's own words for a thing find it.
- `memory/PINNED.txt`: facts that are always loaded in full. Keep it short.
- Per-turn retrieval (`workspace/memretrieve/`): for each message, the most relevant facts are found
  (BM25 + a small local embedding model, bge-small, CPU only) and added to the turn.
- **Corrections**: a fact whose first heading says `SUPERSEDED by \`NEW.md\`` is never loaded; the new
  one is loaded instead. A fact that says it `supersedes \`OLD.md\`` makes the old one load with a warning.
  See the two example facts 003/004.
- New facts start **INTERIM**: they are the assistant's own account until something outside confirms them.
- **Corrections queue** (`workspace/corrections/`): findings from a cold read wait in a queue until a newer
  fact retires the passage they quote, or the person rejects them in their own words.
- `memory/sessions/`: the conversation log, so a restart does not lose the thread.

## 3. The guard rails (why Helix can be trusted with its own memory)

- **Fact gate** (`factgate_hook.py`, a PreToolUse hook passed to the Agent SDK in `app.py`): refuses
  Odin's tool calls that would write to the fact base, the ledgers, or the guard files themselves,
  except through the sanctioned path (`/remember`). Every refusal is logged to
  `workspace/catches/CATCHES.jsonl`. A refusal that turns out to be wrong is retracted by appending a
  retraction line, never by editing the log.
- **Fact ledger** (`workspace/factgate/`): a hash ledger of every fact file. `check` reports anything
  that changed without being recorded. Recording is a human act; the installer does the first one.
- **Write wall** (`wall_exec.sh`, bubblewrap): Odin's shell commands run with the guarded paths mounted
  read-only at the OS level. The hook is a tripwire; the wall is the wall.
- **Prediction ledger** (`workspace/regate/`): claims and predictions are registered with a due date and
  a kill condition before results exist. Frozen fields cannot be edited after the fact ("goalpost moved").
- **Seal gate** (`workspace/sealgate/`): a pre-registration must be reviewed and sealed before the
  experiment it describes may run.
- **Nightly audit** (`workspace/seam/nightly.py`): broken references, unrecorded fact changes, due
  predictions, infrastructure heartbeat, canaries (deliberately planted faults that must be caught).
  It runs at 04:30 as a scheduled Helix job (`memory/schedules.json`, run by `scheduler.py`); a nightly
  restart follows. Scheduled jobs can be added and edited from the interface.
- **Retired passages**: facts are never edited. A newer fact can carry one line
  `RETIRES: <older fact> "<exact quote>"`; wherever the older fact is shown to Odin, that sentence is
  replaced by a marker naming the fact that retired it. The quote must match exactly once or nothing
  changes (reported as unresolved). The nightly counts retirements.
- **Machine caretaker** (`workspace/seam/machine.py`, nightly line `- machine:`): disk use, SMART health,
  temperatures, failed services, pending updates, reboot-required, GPU driver. Report only. The optional
  root half (`machine/`, needs sudo once) is a short allowlist of root commands (apt update/upgrade,
  restarting Helix's own services, health reads), never full sudo.
- **Vault** (`vault/`, optional, needs sudo once): a root-owned, append-only hash chain of the ledgers,
  with an optional copy on a second drive. Without it everything else still works.

## 4. Building and changing Helix: build orders

Changes to Helix itself are written as **build orders** (`build_orders/ORDER_NN_*.md`) and carried out
by a fresh Claude session (the "builder"), never by Odin editing the running app mid-conversation.
The person types `/build NN` in chat; `/restart` restarts Helix. The protocol is in
`build_orders/BUILD_PROTOCOL.md`; the checkers are `build_orders/tools/` (orderlint, namecheck,
ordertests) and `workspace/reportcheck/` (verifies a builder's report against evidence it did not write).
This folder starts empty of orders: the person's own Helix writes its own.

## 5. Install steps — do them in order, and run each check

1. **Prerequisites.** `claude --version` works and the person is logged in (`claude` once interactively).
   `python3 -c "import ensurepip"` works, or `virtualenv` exists (else: `sudo apt install python3-venv`).
   `git config user.name` is set. `bwrap --version` works (else: `sudo apt install bubblewrap`).
2. **Run the installer**: `bash install.sh`. It refuses if `~/helix` exists. It asks name, what to be
   called, a line about the person, the answer language, a folder for big files, and an optional second
   drive. Check: `~/helix/memory/BRAIN.md` contains the person's name; `grep -r __HOME__ ~/helix` finds nothing.
3. **Self-check**: `~/helix/run_tests.sh`. Expected: everything `ok` except the known items in §7.
   Anything else failing is a real problem on this machine: show the person the output (`-v`) and fix it.
4. **Memory model**: the installer downloads bge-small (about 130 MB, pinned revision, checksums
   verified) into `<big-files folder>/memretrieve/model/`. Check: it printed "model downloaded and checked".
5. **First start**: `~/helix/start_helix.sh`, then open http://127.0.0.1:7777. Say hello. Ask Odin
   "what do you know about me?" — he should answer from BRAIN.md, not invent.
6. **Optional — voice out**: put `kokoro-v1.0.onnx` and `voices-v1.0.bin` in `~/helix/models/kokoro/`
   (from the kokoro-onnx releases on GitHub). The Odin and Alice voice blends are already there.
7. **Optional — voice in**: `~/helix/.venv/bin/pip install -r requirements-optional.txt` (faster-whisper).
8. **Optional — Alice**: install the Gemini CLI as `agy`, log in once in a terminal. Then `/council`
   and Alice's reviews work. `workspace/veto/test_sandbox.py` passes once `~/.gemini` exists.
9. **Optional — phone**: Tailscale on the computer and the phone, then `tailscale serve` to port 7777.
   Set `HELIX_TAILSCALE_LOGIN` to the person's Tailscale login so only they get in.
10. **Optional — desktop launcher**: a `.desktop` file that runs `~/helix/start_helix.sh`.
11. **Optional — vault**: read `vault/README.md`; it needs one `sudo` run of `vault/INSTALL_FOR_USER.sh`.
    Never run sudo yourself: give the person the command.

## 6. Adapting it to this person

- Ask what they want Helix for, and save the answers as facts (with their OK). Do not fill BRAIN.md
  with guesses.
- Categories: edit `workspace/categories/CATEGORIES.txt` as projects appear (name | lobe | colour | desc).
- The memory map groups in `app.py` (`MEMORY_CATS`, `BP_PROJECTS`) and `mindgraph.py` (`CATEGORIES`) are
  generic starting points. Change them through a build order, not by hand-editing the running app.
- The names Odin and Alice can be changed; they appear in `app.py`, the interface and the memory files.
- Voice, colours and layout: the interface is one file, `static/index.html`. Show a mock-up before
  changing anything visible (that is one of Helix's own standing rules).

## 7. Known test results on a fresh install (verified on a clean machine before shipping)

`run_tests.sh` skips 68 tests that check the original owner's own history (his old build orders, git
commits, heartbeat targets, calibrated eval set and category list), plus one that needs pytest for the
system python3; the list and the reasons are at the top of the script.
Of the rest, these fail on a fresh install and on the original machine alike, and are known:
- `workspace/controls/test_controls.py` — one check about blocking `torch` imports.
And this one passes only once Alice is set up:
- `workspace/veto/test_sandbox.py` — needs `~/.gemini` (the Gemini CLI's folder).

## 8. Standing rules for you while setting up (the same ones Helix lives by)

- Act on routine, reversible steps; ask before anything destructive, anything with sudo, and anything
  that costs money or reaches the outside world.
- Never start a swarm of agents or a multi-agent workflow unless the person asks for one in their own words.
- Report once, when done, with what you checked. Lead with the result.
- If something is unclear, ask one question rather than guessing.
