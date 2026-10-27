# ANCHOR

**The Example framework, pointed at the one target it was always right about.**

    python3 anchor.py ~/helix/memory/facts --md REPORT.md

```
ANCHOR  __HOME__/helix/memory/facts
   173 documents    1578 claims   7.8s
  anchored        829
  BROKEN          228
  unanchorable    497   (ε = 32.9%)
  stale             2   (true when written, not now)
```

## The rethink

Six months of work tried to make one idea into a law of nature: that fragility has a universal
signature you can read off any system. Every version of that died out of sample — velocity-beats-
level 0 wins in 4 fields, silence came back the wrong direction, R_I scored 0.569 against a 0.720
baseline, the κ law was falsified, the witness principle turned out to be prior art in six fields.
The graveyard is in `AUDITED_INVENTORY.md` and it is long.

What survived is smaller and sharper, and it is worth reading twice:

> A system concealing its true state must still commit somewhere the lie cannot reach.
> Read there, and measure your false-negative rate.

Now look at where that thesis actually won. ATTEST: a **forged telemetry channel**. probe-floor and
probe-study: a **model sandbagging an evaluation**. The single clean finance result: **FTX moving
$167.59M six days before saying "assets are fine."** And look at where it lost: galaxies, quakes,
traffic, climate, markets-as-weather.

The pattern is not subtle once it is said out loud. **Nature does not conceal. Agents do.** The
framework was never a physics. It is an epistemics — an instrument for auditing things that have
an interest in being believed. Every attempt to aim it at a rock failed; every attempt to aim it
at a liar worked.

So point it at the one body of claims that matters most and is audited least: **the record its
own author keeps.** Not as self-flagellation — because this is exactly where the framework's own
history shows the damage. From `body_of_work_status.md`, written by hand in July:

- *"30d AUC 0.81 → 365d 0.55"* — **NO ARTIFACT ANYWHERE. Unsourced.** It existed only in a memory file.
- *"walk-forward Sharpe 0.52 vs 0.49"* — **miscited.** No artifact.
- *"80% backfilled"* — **actually 93.5%.**
- *"live record ~3.3 months"* — **actually 24 days.**
- *"markets κ=0.19 ✓ Measured"* — **false; it is a citation to someone else's paper.**

Every one of those was found by a human reading 887 lines once, in one sitting, and never again.
That is the gap this tool closes.

## What it does

A claim is a surface. The artifact it would have to commit to, if it were true, is the forced
commitment. ANCHOR extracts the claims, names the commitment each one is forced to, checks it,
and sorts the answer into four honest buckets.

| kind of claim | the commitment it is forced to |
|---|---|
| `path` | a path with a location: the file is there, or it is not |
| `cite` | `[[fact_name]]` resolves to a file in `memory/facts/` |
| `git`  | `commit abc1234` is a real object in a known repository |
| `count`| a number about a collection someone can recount — **and a recount rule exists** |
| `quote`| a line attributed to the user appears in the session log |
| `url`  | resolves (only with `--net`) |
| `verdict` | a PASS/KILL line carries a receipt on its own line |

**Verdicts:** `anchored` · `broken` · `stale` (true when written, not now) · `unanchorable`.

## The three design decisions that make it trustworthy

**1. ε is printed, not hidden.** A third of the claims in the fact base cannot be checked by this
instrument at all. That number is on the first line of every report, because an auditor that
reports only what it can see is precisely the failure mode the framework exists to catch. The
report also lists *which* rules are missing — the backlog that lowers ε.

**2. A false alarm is worse than a miss.** The first version had a rule for the bare noun
"aircraft" and cheerfully "corrected" *12 phone registrations* into *160 aircraft*. Counting rules
now match a **sentence shape**, not a word; a bare filename (`SCALING.md says…`) is an
unanchored reference rather than a broken path, because it never claimed a location; a number that
looks like a year is not a count; `BOX2 fact files` is not "2 fact files".

**3. Stale is its own verdict.** A dated record says what was true when it was written. "169 fact
files" was right in September and is 173 now. Calling that *false* teaches the reader to ignore
the tool. Calling it *stale* is the number he actually wants.

## The graveyard check

Operating discipline, rule 6: *a claim once killed needs new evidence and a dated re-listed entry
to return — never a quiet reintroduction.* That rule has never been enforceable, because nothing
read the whole record at once. `resurrections()` carries the ten dead claims and flags any line
that asserts one without saying it is dead, with a two-line context window so the graveyard files
themselves do not trip it.

Current state of the fact base: **two borderline lines, no genuine resurrections.** The discipline
held. Now it is checkable every night instead of every six months.

## Honest limits

- ε ≈ 33 % on the fact base. Ten counting rules exist; the backlog is printed.
- The graveyard list is ten curated claims, not everything that ever died.
- `quote` matching is substring-based against `memory/sessions/log.md` — a paraphrase reads as
  unanchorable, which is the safe direction.
- It proves a file **exists**, not that it **says what the claim says**. That is the next rule
  class, and it is deliberately not faked today.
- Read-only by construction, and there is a test that the audited tree's mtimes do not move.

## Files

`core.py` extraction, resolution, rules, graveyard · `anchor.py` CLI and report ·
`test_anchor.py` 18 tests, no network, including the null test — *it must be able to return
"nothing here"* · `REPORT_facts.md`, `REPORT_workspace.md` generated.
