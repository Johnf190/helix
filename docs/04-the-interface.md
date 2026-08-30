# 4 · The interface

One screen, three columns, one footer.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ HELIX  │ Session · Uptime · Sign-in · State │ Tokens │ Turn │ CPU │ TTS  │  ← header
├────────────┬───────────────────────────┬─────────────────────────────────┤
│            │                           │  WORK  |  MAP                   │
│ INSTRUMENT │      CONVERSATION         │                                 │
│   (rail)   │                           │   what is actually running      │
│            │                           │                                 │
├────────────┴───────────────────────────┴─────────────────────────────────┤
│ ◉ ODIN │ type here… │ Voice │ Turn │ View │ Memory                       │  ← transport
└──────────────────────────────────────────────────────────────────────────┘
```

**The design rule the whole surface obeys:** *if something is lit, something is genuinely running.*
Nothing loops, nothing breathes, nothing sweeps for decoration. Every cell, number and row is
driven by a real event off `/ask_stream`, `/council_stream` or `/metrics`. If the screen is still,
the machine is idle — and you can trust that.

---

## The header

| | |
|---|---|
| **Session / Uptime** | how long this server process has been alive. |
| **Sign-in** | the live credential state. If ODIN can't reach Claude, you see it here, not three sentences into an answer. |
| **State** | IDLE / thinking / speaking. |
| **Tokens · session** | real per-mind token counts for ODIN and ALICE, with a split bar showing the share. |
| **Turn / CPU / TTS queue** | current turn number, a 40-second CPU sparkline, and how many audio clips are waiting to play. |

## The left rail — the instrument

Press **`Tab`** to slide it away, or **FOCUS** to stand it down entirely and just read.

- **Core** — ODIN and ALICE, and the visualiser. Click either name to switch minds.
- **Routing matrix** — a grid of `YOU · ODIN · ALICE · COUNCIL` against
  `SDK · BASH · FILE · WEB · MEM · TTS · STT`. A cell lights when that actor actually uses that
  capability. It's the honest answer to *"what is it doing right now"*.
- **Council** — the seats, and which have answered.
- **Machine** — uptime, resident memory, TTS cache size, facts on disk, and whether the turn lock
  is held.

## The middle — the conversation

Token-streamed. A few properties worth knowing because they were bugs first:

- **Repaints are coalesced to one per animation frame,** not one per token. Rendering markdown per
  token is O(n²) and it flickers.
- **You can select text mid-stream.** The repaint defers while a selection is live and catches up
  when you let go.
- **Scrolling is sticky-bottom, not forced.** It follows the live edge only while you're already
  near it. Scroll up to re-read and it leaves you alone; a **↓ JUMP TO LATEST** button appears.
- A **blinking caret** in the speaking mind's colour shows tokens are still landing.
- Each turn carries a 2px left border in the speaker's colour — ODIN cyan, ALICE violet, you grey.

## The right — the stage

Two lenses, toggled with **`Ctrl` + `` ` ``**:

### WORK
Every command, file write, web lookup and tool call of the current turn, **in order, with its real
output**. Filter to ALL / EDITS / COMMANDS. A "turn profile" strip shows the shape of the turn.

This is the antidote to a black-box agent. You do not have to take the summary's word for what it
did — the transcript of what it did is right there.

### MAP
Your body of work as a **survival timeline**.

- **x** = the date the fact was written · **lane** = project · **one dot** = one fact.
- **Filled dot** = the file stated a verdict. **Hollow ring** = it never did.
- Hover or click a dot: its citation arcs light up — **cyan out** (what it cites),
  **white in** (what cites it) — and everything else dims.
- Click a lane label to isolate a project. The chips filter by verdict. **⟳** re-reads from disk.
  **READ THE FILE ▸** pulls the real text.

Colour is state, never decoration: green HOLDS · red KILLED · yellow CORRECTED · grey UNSTATED.

> **The honesty rule.** The backend guesses a verdict from body words only as a last resort, and
> those guesses are wrong often enough to matter — a fact saying *"paused until next summer"* gets
> read as KILLED. **The map refuses low-confidence verdicts** and shows them as UNSTATED instead of
> inventing a story. A hollow ring means *nobody said*, and that is a truthful thing to draw.

### BLUEPRINT
A full-screen overlay (**`Ctrl`+`B`**) of the same corpus with search and per-project filters — the
long-form read where MAP is the glance.

## The transport bar

| group | |
|---|---|
| **◉ ODIN** | click to toggle mind. ODIN executes; ALICE audits and designs. |
| **Voice** | **TALK** = press to record, press to send. **AUTO** = always listening; say *"ODIN …"* and the rest of the sentence is sent. See [06-voice](06-voice.md). |
| **Turn** | **STOP** interrupts the turn in flight (`Esc`). **COUNCIL** convenes the bench. |
| **View** | **FOCUS** hides the instrument. **FULL** fills the screen. |
| **Memory** | **REMEMBER** writes the box (or the last answer) to a fact file. **BLUEPRINT** opens the overlay. |

## Keyboard

| key | |
|---|---|
| `Tab` | show/hide the instrument rail |
| `Ctrl`+`` ` `` | switch stage lens (WORK ↔ MAP) |
| `Ctrl`+`B` | BLUEPRINT overlay |
| `Esc` | interrupt the turn / close the overlay |
| *any character* | jumps focus to the message box |

That last one is deliberate. **A bare letter is never a shortcut.** Anything you type with nothing
focused belongs in the message box — the previous behaviour ate whole messages after a button
click, which is a genuinely infuriating bug.

Next: [5 · The council →](05-the-council.md)
