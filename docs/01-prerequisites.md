# 1 · Prerequisites

Helix is a local Flask app that orchestrates **CLI tools you already have signed in**. It does not
proxy your credentials anywhere — every model call is made by a subprocess on your own machine.

> **Honest note.** These instructions are written from reading the code, not from a verified
> install on a clean machine. Steps marked ⚠️ are the ones most likely to differ on your box.

---

## Hard requirements

| | |
|---|---|
| **OS** | Linux. Developed on Ubuntu 24.04 / Xubuntu. macOS is likely fine with small changes; Windows is not supported (the code shells out to POSIX tools and reads `/proc`). |
| **Python** | 3.12+ with `venv`. On Debian/Ubuntu: `sudo apt install python3.12-venv`. |
| **A browser** | Anything modern. The launcher prefers Brave in `--app` mode for a clean window, and falls back to `xdg-open`. |
| **Disk** | ~200 MB for the app and its Python deps. Voice adds ~560 MB more (optional, see [06-voice](06-voice.md)). |

## The three minds, and what each one needs

Helix has three tiers. **Only the first is required** — the app runs with the other two missing,
it just tells you they failed instead of pretending.

### ① ODIN — Claude Code CLI · **required**

ODIN is driven by the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview), which
shells out to the `claude` binary. You need:

- The **Claude Code CLI** installed and on `PATH` (the app looks for `~/.local/bin/claude`).
- A signed-in session: run `claude` once interactively and complete the login.
  A **Claude subscription** (Pro or Max) means no per-token charge for ODIN's turns.

Verify:

```bash
claude --version
claude -p "say ok"
```

⚠️ If your Claude config lives somewhere non-default, export `CLAUDE_CONFIG_DIR` before starting
Helix. `app.py` reads it and passes it to every child process; without it the CLI can silently fall
back to a stale credential file and die with *"OAuth session expired"*.

### ② ALICE — Gemini via `agy` · optional

ALICE is the design/audit twin. She is invoked as:

```bash
agy -p "<prompt>" --model "Gemini 3.1 Pro (High)" --dangerously-skip-permissions --output-format json
```

`agy` is the **Antigravity** CLI. She needs a working Gemini sign-in through it. She is given file
access on purpose — her job is auditing *your actual files*, and an auditor who cannot open the
files is a worse auditor.

Don't have `agy`? Either point `gemini()` / `alice_brain()` in `app.py` at whatever Gemini CLI you
do have (both are ~2 lines near the top), or just never switch to ALICE — ODIN works alone.

To change her model, edit `ALICE_MODEL` in `app.py`.

### ③ The council bench — OpenRouter · optional

Six specialist seats, called in parallel, only when you press **COUNCIL**. You need an
[OpenRouter](https://openrouter.ai) account with credit, and the key in your `~/.bashrc`:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

⚠️ Helix reads this by **grepping `~/.bashrc` for the `export` line**, not only from the
environment — so the desktop launcher works even though it runs a non-interactive shell. If you
keep your keys elsewhere, either add the line to `~/.bashrc` or set the variable in the environment
Helix is started from; it checks both.

This is the only tier that costs money per call. See [05-the-council](05-the-council.md) for the
seats and their real prices.

---

## What is NOT required

- **No GPU.** Voice runs on CPU (ONNX Runtime + faster-whisper int8).
- **No database.** Memory is markdown files on disk.
- **No API key for Claude.** The SDK uses the CLI's OAuth session. `app.py` deliberately *deletes*
  `ANTHROPIC_API_KEY` from the environment it hands to children — a leftover junk key breaks the
  CLI, and a real one would bill you per token for something your subscription already covers.
- **No account anywhere else.** Nothing phones home.

Next: [2 · First run →](02-first-run.md)
