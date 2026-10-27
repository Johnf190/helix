# HELIX

**A personal AI assistant that runs on your own Linux machine, keeps a memory between sessions,
and has guard rails so it cannot quietly rewrite that memory or the system it runs in.**

Helix is a local Flask app (port 7777, bound to 127.0.0.1). You talk to **Odin** (Claude, through
your own Claude Code subscription) and, if you like, a second seat **Alice** (Gemini) whose job is
to find what Odin got wrong. It is a personal project, built with Claude Code by someone who does
not write code by hand, and it is still under construction.

---

## The problem it was built for

A chat assistant forgets when the window closes. You re-explain your project every session, and it
repeats conclusions you already threw out last week. Helix keeps the record on disk, where you can
read it, correct it and back it up yourself.

## Memory

- **One fact per file**, in markdown, under `memory/facts/`. Saving a new fact writes a new file,
  so no single edit can wipe the record.
- **A fact ledger** (`workspace/factgate/`) records the hash of every fact. Any change to a fact
  that did not go through the recorded path shows up as off-ledger, and the gate refuses.
- **Retrieval**: each turn gets the charter, the newest facts in full, and the facts most relevant
  to the question (hybrid BM25 + a small local embedding model, `workspace/memretrieve/`).
- **A per-turn index grouped by category** (`workspace/memdigest/`): one line per category instead
  of one per fact, with a mechanical digest file for each category. Nothing in the digests is
  written by a model, so nothing can be summarised wrong. This keeps the per-turn context from
  growing without limit as the fact base grows.
- **Retirements**: a newer fact can retire a passage in an older one, so a stale line is marked
  rather than silently repeated.

## Guard rails

An assistant with a shell can also do damage, so:

- **A hook checks every tool call** before it runs (`factgate_hook.py`) and refuses writes to the
  memory, the ledgers and the guards themselves. Every refusal is logged.
- **A write wall**: the assistant's shell runs inside a bubblewrap sandbox where the guarded files
  are mounted read-only (`wall_exec.sh`). The kernel refuses the write even if the hook missed it.
- **An append-only vault** (`vault/`) anchors a hash chain of the record, with an optional copy on
  a separate drive.

## Changing Helix: build orders

The assistant does not edit the running app from inside a conversation. It writes a **build
order** with a hard scope fence (`build_orders/`). A separate, fresh Claude Code session executes
it; tests must pass, and the builder's report is checked against witnesses it did not write
(`workspace/reportcheck/`) before anything goes live.

## Checking itself

- **A nightly audit** (`workspace/seam/nightly.py`): broken references, the fact ledger, the vault,
  disks and services, planted canaries that the gates must catch, and replays of earlier
  measurements. It reports; it never fixes anything on its own.
- **Predictions are written down before a test runs** (`workspace/regate/`), and scored afterwards,
  so a result cannot be explained after the fact.

## Also in the box

- Voice in and out, offline (Vosk speech-to-text, Kokoro text-to-speech; models downloaded
  separately).
- A council of open models through OpenRouter, convened on demand. Council output is treated as
  opinion, not evidence.
- A scheduler, a phone view over Tailscale, and an image-generation studio (optional, needs a GPU).

---

## What you need

- Linux (built on Ubuntu/Xubuntu 24.04). A normal desktop or laptop is enough; no GPU needed.
- **Claude Code**, installed and logged in with your own account.
- `python3`, `git`, and `bubblewrap` (`sudo apt install python3-venv git bubblewrap`).
- Optional: the Gemini CLI (for Alice), an NVIDIA GPU (image generation), Tailscale (phone).

## Install, the easy way

Open a terminal in this folder, start Claude Code (`claude`), and say:

> Read BLUEPRINT.md and set up Helix with me.

## Install, by hand

```
bash install.sh          # asks a few questions, installs to ~/helix
~/helix/run_tests.sh     # self-check
~/helix/start_helix.sh   # opens http://127.0.0.1:7777
```

It starts as a blank slate with a few example facts. Nothing in this repository contains anyone
else's data, logins or keys.

## Honest status

- Built and used daily on one machine. Not tested on other distributions, and not on Windows or macOS.
- On a fresh install, `run_tests.sh` currently reports 3 of 71 test files failing
  (`controls`, `gapaudit`, `veto/test_sandbox`). They are known and fail the same way on the
  machine Helix runs on.

MIT licence.
