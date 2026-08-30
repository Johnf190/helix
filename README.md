# HELIX

**A local AI workbench with a memory that survives the session, and a second mind whose job is to
prove the first one wrong.**

Helix is a single Flask app you run on your own machine. It orchestrates CLI tools you're already
signed into — it is not a wrapper around an API key, and nothing you say leaves your machine except
the model call itself.

---

## The problem it was built for

A chat assistant forgets when the window closes. You re-explain your project every session. It
re-derives conclusions you already falsified last week. The record of what you actually know lives
nowhere but your head and a scroll of dead transcripts.

Helix's answer is deliberately unclever: **one fact per file, on disk, in markdown.** Every turn
loads your operating charter in full, plus the newest facts verbatim, plus a name-and-first-line
index of everything else. No embeddings, no similarity search deciding for you what matters,
nothing that can silently drop what it scored low. You can read it, grep it, diff it, `git` it, and
delete one wrong belief without touching the other two hundred.

## The second idea: two minds, not one

**ODIN** (Claude) executes — writes and runs code, edits files, drives the shell.
**ALICE** (Gemini) audits — her explicit brief is to break what ODIN built.

They share the same conversation and the same `memory/facts/`. They are not two assistants; they
are opposite poles of one system. An assistant that agrees with its own last answer is worth less
than one with a colleague paid to falsify it.

Behind them, a **council** of six specialist models on OpenRouter, convened on a keypress, each
blind to the others. Their disagreements are the information. And a standing rule:
**council output is opinion, not evidence** — nothing becomes a fact until a real test says so.
New memories are stamped `status: unverified` for exactly that reason.

## The third idea: show the work

The right-hand panel streams **every command, file write and tool call of the live turn, in order,
with its real output**. You never have to take the summary's word for what it did.

The design rule the whole interface obeys: *if something is lit, something is genuinely running.*
Nothing loops, breathes or sweeps for decoration.

---

## Quickstart

```bash
git clone https://github.com/Johnf190/helix.git
cd helix
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start_helix.sh
```

Opens on `http://127.0.0.1:7777` — **localhost only**.

You need the **Claude Code CLI installed and signed in**; that's the one hard requirement. Gemini
(via `agy`) and the OpenRouter bench are optional and degrade gracefully — a missing tier reports
itself instead of pretending.

Full details: **[docs/01-prerequisites](docs/01-prerequisites.md)**.

## Documentation

| | |
|---|---|
| [1 · Prerequisites](docs/01-prerequisites.md) | what you need installed and signed in, and what you don't |
| [2 · First run](docs/02-first-run.md) | install, boot, what healthy looks like, headless verification |
| [3 · Memory](docs/03-memory.md) | one fact per file, the recency budget, writing good facts |
| [4 · The interface](docs/04-the-interface.md) | the WORK and MAP lenses, the rail, keyboard |
| [5 · The council](docs/05-the-council.md) | ODIN vs ALICE, the six seats, what a convene costs |
| [6 · Voice](docs/06-voice.md) | Kokoro TTS + faster-whisper STT, both local, both optional |

## Making it yours

`memory/BRAIN.md` ships as a working charter with three sections marked `<!-- FILL IN -->`: who you
are, what you're working on, and your machine. That file is the whole product. The code is just
what loads it.

## What this is not

- **Not a product.** It's one person's workbench, published because the memory architecture might
  be useful to someone else. There is no roadmap and no support.
- **Not multi-user.** It binds localhost, has no auth, and assumes one human.
- **Not verified from a clean install.** The install docs are written from reading the code, not
  from a fresh-machine run. Anything unverified is marked ⚠️ rather than implied to be tested.
- **Not free at every tier.** ODIN runs on your Claude subscription; the council bills OpenRouter
  per call. Prices are in [docs/05](docs/05-the-council.md), and they move.

## Licence

MIT. See [LICENSE](LICENSE). Use it, fork it, take the parts you want.
