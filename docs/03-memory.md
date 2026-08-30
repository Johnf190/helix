# 3 · Memory

This is the part Helix exists for. Everything else is a shell around it.

## The problem

A chat assistant forgets when the window closes. You re-explain your project every session, it
re-derives conclusions you already falsified last week, and the record of what you actually know
lives nowhere except in your head and a scroll of old transcripts.

## The shape of the fix: **one fact per file**

```
memory/
  BRAIN.md          ← the operating charter. Always loaded, in full.
  ALICE.md          ← the same, for the audit twin.
  facts/            ← the durable record. One file = one fact.
    20260830-the-thing-i-learned-a1b2c3.md
    ...
  alice/            ← ALICE's own drawer: her audits and design specs.
  sessions/log.md   ← raw scrollback. NOT the record.
```

Every turn, Helix builds the context as `BRAIN.md` **in full** + a digest of `facts/`.

Why files rather than one big memory blob, or a vector store:

- **No single edit can wipe the record.** A blob that gets rewritten badly loses everything at once.
- **You can read it, grep it, diff it, and `git`-track it.** It's markdown.
- **You can delete one wrong belief** without touching the other two hundred.
- **Nothing is retrieved by similarity.** A vector store decides for you what's relevant, silently,
  and quietly drops what it scored low. Here, recent facts are present *verbatim* and the rest are
  listed by name — so the model always knows the fact exists even when the text isn't inlined.

## How the digest stays bounded

`FACTS_BUDGET` (default **45,000 characters**, top of `app.py`) is the amount of full fact text
inlined per turn.

- Facts are sorted **newest-first by mtime**. As many as fit inside the budget go in **verbatim**.
- Everything that doesn't fit becomes a one-line **index entry**: filename + first line, plus the
  real path on disk.
- Both lists are then re-sorted into filename order so they read coherently.

That index line is the load-bearing detail. ODIN has file tools. An indexed fact isn't forgotten —
it's a thing he knows exists and can open on demand. Memory degrades from *"in context"* to
*"one read away"*, never to *"gone"*.

Raise the budget if your model has room; the tradeoff is cost and latency per turn, not correctness.

## Saving a fact

Press **REMEMBER** in the transport bar, or `POST /remember {"text": "..."}`.

It writes one new file:

```
memory/facts/20260830-<slug-of-your-text>-<hash>.md
---
saved: 2026-08-30
status: unverified
---
<your text>
```

Note `status: unverified`. Something you typed is not yet something you tested. Promote it by
editing the file once a real test has passed.

## Writing good facts

The system is only as honest as what you put in it. What works:

- **A verdict word in the filename.** `..._KILLED.md`, `..._confirmed.md`, `..._prior_art.md`.
  The BLUEPRINT and MAP views read it directly (see [04-the-interface](04-the-interface.md)).
- **A date in the first few lines.** That drives the timeline.
- **Cite other facts by filename.** `see 12_probe_result.md` becomes a real edge in the map.
  It's literal filename matching — no fuzziness, no inference.
- **Record failures with the same weight as successes.** A killed hypothesis is the most valuable
  thing in the folder, because it stops you re-running it in November.
- **Correct old facts explicitly, in a new file, naming the one you're correcting.** Don't quietly
  edit history. The correction is itself a fact worth keeping.

## `sessions/log.md` is scrollback, not memory

The transcript is kept so the UI can repaint your last conversation on reload and so a restart
doesn't lose the thread mid-sentence. It is **not** the durable record and it is not treated as
truth. The record is `facts/` — the things you *chose* to keep.

## Grouping facts into projects — `memory/projects.json`

Optional. Helix derives a fact's date, its stated verdict, and its citations from the file itself.
The one thing it can't infer is which *project* a file belongs to, so that's the one thing you
declare:

```bash
cp memory/projects.example.json memory/projects.json
```

Then edit the keyword lists. Without the file, everything lands in one `GENERAL` bucket and
nothing breaks.

Next: [4 · The interface →](04-the-interface.md)
