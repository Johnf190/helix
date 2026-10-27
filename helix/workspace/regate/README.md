# regate — the registry write gate

A prediction's goalposts are frozen the moment it is registered. This makes that
mechanical instead of a promise.

`workspace/seam/registry/registry.json` has **no write path**. It is changed by hand, and by
`seam/nightly.py score()`, which rewrites the whole file. Nothing prevents changing a
prediction's `p`, `due`, `metric`, `statement`, `kill` or `min_n` after it was registered;
registering one with no kill condition; scoring before the due date; overwriting a score; or
deleting an entry. Those are the moves the protocol's part eight forbids in prose —
*"do not move the goalpost"* — and prose loses to enthusiasm.

Standalone: **nothing imports this and nothing is wired through it.** Wiring the nightly
through `regate` is a separate order.

## Ten verbs

```
python3 regate.py baseline <registry.json>       # record the frozen fields of every prediction
python3 regate.py check    <registry.json>       # has anything frozen changed?      exit 0 / 4
python3 regate.py register <registry.json> <entry.json>                              exit 0 / 3
python3 regate.py score    <registry.json> <id> --outcome 0|1|none --note "..." [--value <n>]
python3 regate.py attach-prereg <registry.json> <pred-id> <prereg.md>                exit 0 / 3
python3 regate.py attach-pack <registry.json> <pred-id> <pack-name>                  exit 0 / 3
python3 regate.py attach-deps <registry.json> <entry> (--on <id|pack:name> ...|--none) exit 0 / 3
python3 regate.py attach-narrative <registry.json> <entry> <prose.md>                exit 0 / 3
python3 regate.py revise-narrative <registry.json> <entry> "<reason>"                exit 0 / 3
python3 regate.py status   <registry.json> <entry> "<text>"                          exit 0 / 3
```

| exit | meaning |
|---|---|
| 0 | done — and **nothing else means done** |
| 2 | usage, or the file cannot be read |
| 3 | refused: a `register`/`score` precondition failed |
| 4 | `check` failed: the registry no longer says what the ledger says it said |

## What is frozen, exactly

**Frozen, per prediction** — changing any of these after registration is moving the goalpost:

```
id   kind   p   due   metric   statement   kill   kill_rule   min_n   prereg   pack
```

`pack` (order 49) is absent on every prediction before it, so those hash as they always did.

**Frozen, per entry:** `claim`, `scope`, `supersedes`, `structural_change`. A claim that can be
reworded after registration is not a claim; nor is one whose scope, or the dead claim it says it
supersedes, can be swapped afterwards.

An **absent** field is recorded as absent, never as `null`, so adding a `min_n` that was never
there is a change and is reported as `30 -> <absent>`'s mirror image.

**Not frozen, but logged:** `status`. Cutting scope hard after a cold review and saying so in
`status` is legitimate — #0002 and #0003 both did it — and must stay possible. It goes through
`regate.py status`, which records the old and new text in the ledger; a hand edit is reported as
`STATUS CHANGED OFF-LEDGER`.

**Not frozen and not tracked:** entry-level notes, `sealed_code`.

## `kind`: a time prediction and an experiment are not the same thing

Order 05 found that `0002-p1` and `0003-p1` were scored three months before their `due` date.
That was legitimate — they are experiments, where `due` means *run by* — but the registry could
not tell it apart from cheating on a prediction about a date. So every prediction declares which
it is:

| kind | when it can be scored |
|---|---|
| `time` | not before `due`. The date is the claim. |
| `experiment` | before `due` **only while** its `prereg` passes `sealgate.py verify` — precondition `S5_EXPERIMENT_SEALED`, which names sealgate's first line when it refuses |

**An absent `kind` is read as `time`**, the strict reading, never the lenient one. `register`
requires `kind` on every new prediction, and requires a `prereg` when the kind is `experiment`.

`prereg` is **set once**. Absent → a path is allowed, because an experiment can be registered
before it is sealed (`0004-p1` is exactly that), and that move goes through
`regate.py attach-prereg <registry> <pred-id> <path>`, which refuses unless sealgate verifies the
seal. A path → a different path, or a path → absent, is `GOALPOST MOVED: <id> field prereg`.

**regate takes the date from the system clock**, never from its caller. There is no `--today`:
a date the gate accepted from whoever called it would be a way straight past `S2`.

**The score fields** (`scored`, `outcome`, `brier`, `note`, and `value` when one was given) are
frozen **once written**. A
written score is never overwritten: not by a flag, not by this tool.

## The ledger

`baseline` writes `REGATE_LEDGER.jsonl` **beside the registry** — one JSON line per prediction:

```
{"entry":"0000","id":"0000-p1","frozen":{...},"frozen_sha256":"c1475076c90a…",
 "score":null,"score_sha256":null,"kind":"baseline","line":1,"prev":"e3b0c44298fc…",
 "ts":"2026-09-23T22:…","regate":"1.0"}
```

- `frozen_sha256` is sha256 over the **raw bytes of a canonical JSON encoding** of the frozen
  fields — sorted keys, no spaces, UTF-8 — so the hash does not move when a file is
  reformatted, and does move when a value does.
- **Append-only.** The tool only ever appends. `prev` is the sha256 of the previous line's exact
  bytes, so removing or editing an earlier line breaks the chain and `check` says
  `LEDGER TAMPERED: line <n>`.
- `baseline` also records a score that **already exists** (#0002 and #0003 are already scored),
  so those cannot be quietly revised either.
- `register` and `score` append their own lines, `kind` `register` / `score`.

## What `check` reports

| message | meaning |
|---|---|
| `GOALPOST MOVED: <id> field <name> <old> -> <new>` | a frozen field changed. For two long strings the window shown is around the first character that differs, so the message actually shows the change |
| `GOALPOST MOVED: <entry> field claim\|scope\|supersedes\|structural_change <old> -> <new>` | an entry-level frozen field changed |
| `GOALPOST MOVED: <entry> status left INVALID <old> -> <new>` | an INVALID status was hand-edited to something that is not INVALID |
| `STATUS CHANGED OFF-LEDGER: <entry>` | the status differs from the last one the ledger recorded |
| `DELETED: <id>` | in the ledger, gone from the registry |
| `UNRECORDED: <id>` | in the registry with no ledger line — added by hand, around the gate |
| `SCORE REWRITTEN: <id> field <name> <old> -> <new>` | a written score was changed |
| `LEDGER TAMPERED: line <n>` | the chain is broken, or a line does not hash to its own contents |

On success the summary line ends `, N legacy prose kills` — predictions with no `kill_rule`
(see below). It goes **last** so that `seam/nightly.py registry_line()`'s pattern
`(\d+) prediction\(s\), (\d+) narrative\(s\)` still matches:

```
OK: …/registry.json still says what the ledger says it said
  5 prediction(s), 3 narrative(s), 10 ledger line(s), chain intact, 5 legacy prose kills
```

When the ledger chain is broken, `check` says so and compares nothing else — a ledger that
cannot be trusted is not evidence about the registry.

## `register` preconditions

`<entry.json>` is a JSON object with **exactly one key**, the new entry id, keyed exactly as the
registry itself is:

```json
{"0005": {"claim": "...", "scope": "proj/a", "registered": "...", "status": "open",
          "predictions": [{"id": "0005-p1", "kind": "time", "due": "2027-06-30", "p": 0.5,
                           "metric": "...", "statement": "...", "kill": "...",
                           "kill_rule": {"on": "value", "op": "<", "value": 0.05}}]}}
```

| id | requires |
|---|---|
| `R1_ID_IS_NEXT` | the entry id is new and is the next number in sequence |
| `R2_PREDICTION_COMPLETE` | every prediction has `id, kind, p, due, metric, statement, kill, kill_rule`; `kind` one of `time`/`experiment`; a `prereg` when `kind` is `experiment`; `p` strictly inside (0,1); `due` an ISO date **after today**; `kill` non-empty; `kill_rule` well-formed; each `id` of the form `<entry>-p<n>`; and the entry has a well-formed `scope` |
| `R3_REGISTRY_UNDRIFTED` | `check` passes **as the registry stands** — you cannot add to a registry whose goalposts have already moved |
| `R4_PREREG_VERIFIES` | any `prereg` path the entry names passes `sealgate.py verify` (run as a subprocess; sealgate is never imported or edited) |
| `R5_NOT_A_RESURRECTION` | the entry does not restate an INVALID claim **in its own scope** without citing it — see below |

On success the registry is written **atomically** — temp file, `fsync`, `os.replace` — in the
same `indent=1, ensure_ascii=False` format `nightly.py` writes, so a file this tool touches does
not come back as a whole-file diff. On refusal **nothing is written at all**: registry and ledger
are byte-identical before and after.

## `score` preconditions

| id | requires |
|---|---|
| `S1_EXISTS_AND_UNDRIFTED` | the prediction exists and `check` passes |
| `S2_DUE_HAS_ARRIVED` | today ≥ `due` — scoring early is scoring a prediction you can still change |
| `S3_NOT_ALREADY_SCORED` | it has no score yet |
| `S4_NONE_NEEDS_UNDERPOWERED` | `--outcome none` needs a note starting `UNDERPOWERED:`, as `nightly.py` writes it |
| `S5_EXPERIMENT_SEALED` | scoring an `experiment` before `due` requires its `prereg` to pass `sealgate.py verify` now, at scoring time |
| `S6_VALUE_MATCHES_RULE` | `--value` (a finite number) is given **exactly when** the prediction's `kill_rule` is on `value`; refused for an outcome rule, a prose rule, or a legacy prediction |
| `S7_SEAT_SWAP` | (order 49) a `kind: experiment` prediction has an attached `pack`, and `seatswap.py check <pack>` (run as a subprocess, never imported) finds an intact `AGREE` for the pack's **current** manifest sha256 and no `DISAGREE` for it. `DISAGREE`, `COPIED`, `NOT_REPRODUCED`, `FAILED`, an AGREE for an older manifest, or no run → refused, and the message names the outcome. A `time` prediction (or one with no kind): `ok`, "time prediction, no analysis to swap" |
| `S8_NOT_QUARANTINED` | (order 51) the prediction's entry is not QUARANTINED (below); the refusal names the path, e.g. `0004 ← 0003 INVALID` |
| `S9_RUN_AFTER_SEAL` | (order 60) an `experiment` scored before `due` has a run of its entry's scope logged in `ATTEMPTS.jsonl` strictly after its seal and not after now. Otherwise `ok`, "not an early experiment score"; `not-evaluated` when S5 has already refused |

S9 (order 60, proposal P-6, catch #7) closes what S5 left open: S5 checks that an early-scored
experiment's seal still verifies, but not that the experiment was **run after** it, so a result
known before sealing could be sealed and scored minutes later (0002-p1 was scored 7m38s after
its seal, 0003-p1 2m11s). The seal time is the prereg token's `issued_at` (the same lookup as the
cycle line's start). regate reads `~/helix/workspace/cycle/ATTEMPTS.jsonl` as a file, read-only
(only Helix's process writes it, via `attempt.py`), and needs at least one record whose `scope` is
the entry's scope and whose `ts` is after the seal and not in the future. It fails closed: a
missing or unreadable attempts log, a line it cannot parse, or a seal time it cannot read is a
refusal that says which. There is no flag or environment variable that turns S9 off; a refusal
goes to `refusals.log` like every other S-rule.

## `attach-pack` preconditions (order 49, item 10)

Same shape as `attach-prereg`: absent to a name is the only move, and `pack` is frozen, so a hand
edit is a `GOALPOST MOVED`. One `attach-pack` ledger line, carrying the pack's `manifest_sha256`.

| id | requires |
|---|---|
| `A1_PREDICTION_EXISTS_AND_UNDRIFTED` | the prediction exists and `check` passes |
| `A4_PACK_SET_ONCE` | it names no pack yet |
| `A5_PACK_IN_PACKS_AND_VERIFIES` | the pack is in `replay/PACKS.jsonl` and verifies now (`seatswap.py check` says `pack_ok`) |
| `A_NOT_QUARANTINED` | (order 51) the entry is not QUARANTINED; `attach-prereg` has the same precondition, same wording |

Attaching does not need an AGREE; scoring does (S7). See `../seatswap/README.md`.

## The provenance DAG (order 51, item 23) — registry entries only

Nothing used to track inference dependency: when an upstream claim died, the claims resting on it
stood until a human noticed. Now every entry declares, **once**, what it rests on:

```
python3 regate.py attach-deps <registry.json> 0004 --on 0003
python3 regate.py attach-deps <registry.json> 0001 --none
```

An edge is another entry id, or `pack:<replay-pack-name>` (item 26) for the code/data the claim
rests on. `--none` is a declaration (an empty list), unlike "never declared". The edges go into the
entry's `depends_on` and into one `attach-deps` ledger line whose `frozen` is
`{"depends_on": [...]}`. `depends_on` is deliberately **not** in `ENTRY_FROZEN_FIELDS` (every legacy
entry would drift); the ledger line is the frozen record, and `check` reports a hand edit, removal or
addition of `depends_on` as `GOALPOST MOVED: <id> field depends_on`.

| id | requires |
|---|---|
| `D1_ENTRY_EXISTS_AND_UNDRIFTED` | the entry exists and `check` passes |
| `D2_DEPS_SET_ONCE` | it has declared nothing yet (`--none` counts as declared) |
| `D3_EDGES_RESOLVE` | each edge is an entry in this registry, or a `pack:` whose name is in `replay/PACKS.jsonl`; none named twice |
| `D4_NO_CYCLE` | no self-dependency, and no path of declared edges leads back to the entry; a cycle is named, e.g. `cycle: 0004 → 0003 → 0004` |

**QUARANTINED is computed on every read, never stored.** No status or ledger line is written when an
upstream dies; `check`, `score`, `attach-prereg`, `attach-pack` and the nightly compute it from the
registry and the ledgers each time, so it is instant and cannot go stale. An entry is quarantined
when any upstream node, transitively, is

- an entry whose status is INVALID (in the registry, or in the ledger's last word on it), or is
  itself quarantined;
- a `pack:` whose **latest** line in `replay/REPLAYS.jsonl` is `MISMATCH` or `TAMPERED`, or whose
  **latest** seat swap in `seatswap/SWAPS.jsonl` is `DISAGREE`. A later `MATCH` / `AGREE` lifts it:
  packs can recover, INVALID cannot.

regate reads those two ledgers **as files**, once per call; it never imports replay or seatswap and
never runs a subprocess per node. **Fail closed:** a line in REPLAYS.jsonl or SWAPS.jsonl that is
not a JSON object, or a broken SWAPS.jsonl hash chain (`prev`/`seq`, seatswap's rule), quarantines
*every* `pack:` edge, with the reason in the path (`0005 ← pack:x SWAPS.jsonl chain broken at line 1`).
REPLAYS.jsonl carries no hash chain of its own, so for it "broken" means unreadable.

Each quarantine names its path: `0004 ← 0003 INVALID`, `0006 ← 0005 ← pack:x MISMATCH`. `check`
prints `QUARANTINED: <path>` for each (it is not drift: exit 0), and, for every prereg on a
quarantined entry, `seal dropped: <prereg> (entry quarantined: <path>)` — the R4 side ("downstream
seals drop"). sealgate itself is untouched; it knows nothing about the registry.

A quarantined entry stays quarantined while its upstream is dead. INVALID is final, so the way out
is a new entry that `supersedes` the dead one (order 25b), never editing the edge. Undeclared
entries are reported by the nightly, not refused (legacy):

```
- provenance: 5 entries · 1 edge · 0 undeclared · 0 quarantined · no cycles
```

with a `  - 🔴 QUARANTINED: <path>` sub-line per quarantined entry (and `  - 🔴 cycle: ...` if one
ever got into the ledger).

**Brier is computed here**, `round((p - outcome) ** 2, 4)`, and there is no way to supply one:
`--brier` is not an argument, and passing it is a usage error.

## The falsifier has a consequence (order 25b, amendment 1a)

Until v1.3 a kill condition was prose: meeting it changed nothing unless someone narrated it.

### `kill_rule` — beside the prose `kill`, never instead of it

```
"kill_rule": {"on": "outcome", "op": "==", "value": 0}       the scored outcome
"kill_rule": {"on": "value",   "op": "<",  "value": 0.05}    a measured number, given as --value
"kill_rule": "prose"                                          allowed, flagged, never auto-evaluated
```

`op` is one of `< <= > >= ==`; `value` a finite number (0 or 1 when `on` is `outcome`); no other
keys. `register` requires it on every new prediction; a prose rule prints
`PROSE KILL: <id> (not auto-evaluable)` and the ledger line carries `kill_prose: true`. It is
**frozen**: tightening, loosening or downgrading it to `"prose"` by hand is `GOALPOST MOVED`.

**Legacy.** 0000–0004 have no `kill_rule` and are **not** given one: it is a frozen field, and
adding it now would itself be a goalpost move. Absent reads as legacy prose; `check` counts them
(`5 legacy prose kills`) and `score` treats them as prose.

### Auto-INVALID

In the same `score` call, after the score is written: if the rule is structured and met, the
entry's status becomes

```
INVALID — killed by <pred-id>: <on> <actual> <op> <threshold> (regate, <date>)
```

and the ledger gets a second line, kind `auto_invalid`, with `old_status`, `new_status`, the
rule and the actual number. Score and status land in **one** registry write, and both ledger
lines in one append; nothing is written if any precondition fails. Not evaluated: a prose or
legacy rule (`PROSE KILL — not evaluated by regate; decide by hand via status`), and an
`--outcome none` score (underpowered is not evidence either way). A kill met on an entry that is
already INVALID scores, and leaves the first verdict standing. The nightly's
`score --outcome` is unchanged for outcome rules.

### INVALID is final

`status` refuses any change to an entry whose status starts with `INVALID`
(`R_INVALID_IS_FINAL`, exit 3) — whether regate set it or a person did, deciding a prose kill by
hand. A hand edit away from INVALID is `GOALPOST MOVED: <entry> status left INVALID` (exit 4).

### INVALID is local to its scope (the user, 2026-10-01)

*"If invalid in one project or framework it might be valid in another."* Every new entry names
its `scope` (`"crosscheck/attest"`, `"seam/record"`; lower-case, `/`-separated). 0000–0004 have
none and read as `legacy`. Auto-INVALID touches **only** the entry whose prediction was scored —
no other entry, in any scope, ever.

### R5 — no resurrection by rewording

- An entry may carry `supersedes: "<entry id>"` and
  `structural_change: {"type": "mechanism"|"instrument", "what": "<at least 20 characters>"}`.
  If it supersedes an INVALID entry, the structural change is **required**; `parameter`, `seed`,
  `prompt`, `wording` or anything else is refused — that is the same claim run again.
  `supersedes` naming an entry that is **not** INVALID is refused (order 41): only a dead claim
  can be superseded. Until order 41 it was accepted with a note, and the resemblance check below
  never ran — a reworded dead claim could cite any open entry and pass.
- With no `supersedes`, the new claim and statements are compared with every INVALID entry's.
  At or above the threshold **in the same scope**: refused, naming the entry to cite. In a
  **different** scope (or `legacy`): allowed, printed as
  `RESEMBLES INVALID <id> (scope <other>): allowed, different scope`, and recorded in the
  register ledger line as `resembles_invalid: [{"id", "scope", "similarity"}]` — moving a dead
  claim to a new scope is visible, never silent.

**Resemblance** = the highest Jaccard similarity of lower-cased content-word sets (a short
stopword list removed) over every pair of (claim or statement of the new entry) × (claim or
statement of the old one).

**The threshold, 0.59, was measured, not guessed** (`fixtures/similarity/groups.json`, and
`test_the_threshold_sits_between_the_measured_groups` re-measures it):

| pair | group | resemblance |
|---|---|---|
| paraphrase 1 vs original | paraphrase | 0.765 |
| paraphrase 2 vs original | paraphrase | 0.706 |
| paraphrase 3 vs original | paraphrase | 0.706 |
| different 1 vs original | different, same topic | 0.167 |
| different 2 vs original | different, same topic | 0.176 |
| different 3 vs original | different, same topic | 0.167 |
| 0000 vs 0001 | live, different | 0.480 |
| 0002 vs 0003 | live, different | 0.256 |
| 0003 vs 0004 | live, different | 0.128 |
| 0002 vs 0004 | live, different | 0.095 |
| the other six live pairs | live, different | 0.000 |

No overlap: highest different 0.480, lowest paraphrase 0.706; 0.59 is the midpoint (margins
0.116 and 0.110). 0000 vs 0001 is the closest because their statements are built on one
template. **This is a tripwire, not a proof** — a determined rewording with new vocabulary gets
under it; item 12's canary measures it on real attempts.

**Known residue, not solved here.** Re-labelling the scope to dodge R5 is possible: register the
dead claim under a new scope and R5 lets it through. What stops it being silent is that `scope`
is frozen once registered and the register ledger line carries `resembles_invalid` as the
witness. R5 compares only when `supersedes` is absent; since order 41 a present `supersedes` must
name an INVALID entry, and is then held to the structural-change rule instead.

## The cycle line (order 27, item 16)

`score` measures what the cycle cost and prints it under the verdict:

```
cycle: seal→score 7m38s · GPU 0 min · 14 turns · $3.10 (token spend in window)
```

- **Start:** for a prediction with a `prereg`, the sealgate token's `issued_at` (`RUN_TOKEN.json`
  beside the prereg) — **seal→score**. Otherwise its `register` ledger line (**register→score**),
  or, for a prediction that only has a `baseline` line, that line (**baseline→score**). A token
  that cannot be read falls back to the ledger line. **End:** the moment of scoring (UTC).
- **Numbers:** `workspace/cycle/cycle.py window <start> <end> --json`, run as a subprocess.
  Wallclock; GPU-minutes from `GPU.jsonl` (the systemd sampler, one sample ≈ 1 min); Helix turns
  and their token spend from `TURNS.jsonl` (app.py). A missing ledger prints `GPU not sampled` /
  `turns not logged`, never 0.
- **Stored** as the score field `cycle` (`SCORE_FIELDS`), written with the score and frozen with
  it: editing it later is `SCORE REWRITTEN: … field cycle`. If cycle.py fails, the score is still
  written, with `cycle.line` = `not measured (…)` — a measurement never blocks a score.
- **`check`** prints `  <id> cycle: <line>` for every scored prediction, between the `OK:` line
  and the summary (the summary stays last; it is the line `nightly.registry_line()` reads).
  Scores written before order 27 (0002-p1, 0003-p1) print `cycle: not recorded (pre-gate)`;
  their score fields are not touched.
- **Honesty:** the window counts *all* GPU samples and *all* Helix turns in that period, not
  only the experiment's. It is an **upper bound**, stored with `upper_bound: true`. Attributing
  GPU and tokens to one experiment needs item 24's attempt log. Builds (RUNS.jsonl) are not
  counted in the window.

**Timestamps (order 27):** new ledger `ts` values are UTC with a `Z`. Older lines carry naive
local time and are never rewritten (the chain is over their raw text); `parse_time()` reads a
naive value as local. `refusals.log` lines are unchanged (local).

## The K line (order 28, item 24)

`score` prints, on the line after the cycle line, **K = the attempts that preceded this score**
in the entry's scope, counted — never declared — from `workspace/cycle/ATTEMPTS.jsonl`:

```
cycle: register→score 2h10m · GPU 34 min · 9 turns · $3.10 (token spend in window)
K=12 (since 2026-10-02T09:14Z) · K witnessed (GPU)
```

- **Counted by** `workspace/cycle/cycle.py k --scope <entry scope> --until <score time> --json`,
  run as a subprocess (never imported). K = sum of `n` over the scope's attempt lines with
  `ts <` the score time. Only Helix's own process writes that ledger (POST `/attempt`, loopback
  only); Odin logs with `workspace/cycle/attempt.py run --scope … -- <command>`. An entry with no
  scope (0000–0004) is counted in scope `legacy`.
- **Witnessed** by `GPU.jsonl`: an unlogged GPU job in the scope's directories prints
  `K disputed: +M unlogged GPU job(s)` with each job's pid, first-seen time, cwd and cmd. No GPU
  ledger → `K not witnessed (GPU not sampled)`. A broken attempt chain →
  `K unverifiable (chain broken at seq N)`, never a number.
- **Zero attempts prints `K=0 · no attempts logged`. It is printed, never refused:** refusing
  would push attempts off the record.
- **Stored** as the score field `k` (`SCORE_FIELDS`): `{"k", "since", "until", "witness",
  "unlogged", "line"}` (+ `unlogged_jobs` when disputed). `witness` is `ok` / `disputed`, or
  `none` (no attempts, or no GPU ledger) / `unverifiable` (broken chain). Frozen with the score:
  editing it is `SCORE REWRITTEN: … field k`. If cycle.py fails, the score is still written with
  `k.line` = `K not measured (…)`. Scores written before order 28 have no `k` and hash as before.
- **`check`** prints `  <id> K: <line>` under each scored prediction's cycle line; scores from
  before order 28 print `K: not recorded (pre-gate)`. The summary stays the last line.
- **Honesty:** the witness only sees GPU jobs that last ≥ ~1 minute in a directory the scope has
  used. CPU-only jobs, jobs under a minute and jobs started from a fresh directory never logged
  are invisible to it. **K is a lower bound that the witness can only push up.**
- **Correction: disclosure only.** K is shown; nothing is corrected for it. No Bonferroni or other
  formula, because the registry's kill rules are thresholds on an outcome or a measured value,
  not p-values: there is nothing to correct yet. Item 24 says "do not invent a universal
  formula".

## There is no escape hatch

**No `--force`, no `--override`, no way to re-score, no environment-variable waiver.** A gate
that can be waived is a warning. If a registered prediction is wrong, it is scored, or retired
in `status`, or a new entry supersedes it — the goalpost does not move. `test_regate.py` asserts
the absence, and so does the acceptance criterion:

```
$ grep -in "force\|override\|bypass\|skip_check" regate.py
$ echo $?
1
```

Every refusal appends one line to `refusals.log` beside `regate.py`: timestamp, verb, target,
and which preconditions failed.

## What this gate does not claim

It is files on a disk, and the ledger sits next to the registry it protects. **Anyone who can
delete or rewrite the whole ledger can start over, and this cannot stop them** — the chain makes
a partial edit detectable, not a wholesale replacement. That is the same honest limit as
sealgate's run token, and the answer to it is the same: an anchor kept off this machine
(upgrade item 13). What this gate does give you: a hand edit to any frozen field is named, a
score cannot be rewritten in place, an entry cannot be added around the gate without leaving
`UNRECORDED` behind, and nothing is erased silently *by the tool*.

One more limit worth stating: `status` is deliberately not frozen, so an entry's status can be
rewritten — but not silently, since `check` reports a status the ledger did not record. And the
gate still cannot tell you whether an `experiment`'s result was honest; it can only tell you that
the seal it was judged against still verifies.

## Tests

```
python3 -m pytest test_regate.py -q
```

256 tests, all driving the real CLI in a subprocess (the threshold test imports
`resemblance()` directly; order 27's `cycle_start`/`measure_cycle` and order 28's `measure_k`/`SCORE_FIELDS`
tests and order 60's `run_after_seal` seal-time test load the module). Order 60's S9 tests point regate's `ATTEMPTS` at a temp file, emptied before every test. Order 28's K tests use scratch `ATTEMPTS.jsonl`/`GPU.jsonl` via `HELIX_CYCLE_DIR`. Every `check` verdict and every `register`
and `score` precondition has a test that triggers it alone. `fixtures/real_copy/registry.json`
is a byte-identical mode-444 copy of the real registry; every test works on a copy of it in a
temp directory, and one test asserts the real registry's hash is unchanged and that **no ledger
is ever created beside it**.
