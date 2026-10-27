# sealgate — the gate that refuses

A run cannot be blessed until its seal has survived **a reviewer who is not its author**,
and a seal **cannot be quietly edited afterwards** to match what the data did.

On 2026-09-22 a seal was committed before its own precondition returned, and the
seal-to-result cycle was 55 minutes. Nothing refused. The discipline was prose, and prose
loses to enthusiasm. This turns the ordering into mechanics.

Item 8 was specified as *"the runner refuses to execute a PREREG without a review stamp."*
**There is no runner** — pre-registrations are hand-written markdown executed by ad-hoc
`drive.sh` scripts. So the gate is a standalone CLI that issues a **run token**, and the
convention is that a result without a valid token beside it is not a result.

Standalone on purpose: no library wiring, nothing imports it, it touches no live process.

## Four verbs

```
python3 sealgate.py check  <PREREG.md>                            # exit 0 / 3
python3 sealgate.py stamp  <PREREG.md> --reviewer NAME --findings PATH
python3 sealgate.py token  <PREREG.md>                            # exit 0 / 3
python3 sealgate.py verify <PREREG.md>                            # exit 0 / 4
```

| exit | meaning |
|---|---|
| 0 | blessed — and **nothing else means blessed** |
| 2 | usage, or the file cannot be read |
| 3 | refused: a precondition failed |
| 4 | drift: what is on disk is not what the token was issued against |

Normal order of use: write the seal → `review.py assign` names the reviewer → `review.py run`
gets that reviewer's raw review → the author writes a findings file answering it → `stamp` →
`../veto/veto.py run` (Alice tries to break the seal **as stamped**) → `token` → run the experiment →
`verify` **before** reporting anything.

## The ten preconditions

`token` issues `RUN_TOKEN.json` beside the pre-registration only if all ten hold. `check`
runs the same ten and issues nothing.

| id | what it requires |
|---|---|
| `P1_REVIEW_STAMP` | a stamp block naming a reviewer, an ISO date, and a findings file that exists **and still hashes to what was stamped** |
| `P2_REVIEWER_NOT_AUTHOR` | the reviewer is not an author of the seal. *A seal reviewed by the person who wrote it is an unreviewed seal.* No author line declared → refused, because the claim cannot be established |
| `P3_FINDINGS_DISPOSITIONED` | every finding is `ADDRESSED`, `ACCEPTED`, or `DECLINED: <reason in writing>` |
| `P4_CONTROLS_CHECKLIST` | a controls seal block in the format of `../controls/SEAL_CHECKLIST.md`, all seven controls marked `ran` or `skipped`, and **skipped carries a written reason** |
| `P5_BANDS_AND_STOP_CONDITIONS` | both declared as named sections |
| `P6_CONTENT_HASH` | the content to hash is well defined: **at most one** well-formed stamp block, and a non-empty body |
| `P7_STAMP_BLOCK_CANONICAL` | the stamp block holds **nothing** but the fields this tool writes — no unrecognised line, no duplicated field, no value it would never write |
| `P8_PREFLIGHT` | a heading naming the pre-flight, holding `Brief: <relative path>` and `Brief-sha256: <hex over raw bytes>`. The brief exists, **still hashes to that**, passes `preflight.py check`, and is not `NO-GO`. A `RESHAPE` needs a heading naming the reshape that quotes its line. The brief's `Searcher` is not an author of the seal (P2's author parsing) |
| `P9_ASSIGNED_REVIEWER` | `review.py` **assigned** this seal a reviewer in `REVIEWS.jsonl`, and the stamp's `reviewer` is **exactly** that name; a `review` line by that reviewer exists for this seal, its raw output file exists and **still hashes to the ledger**; every finding id it raised (`[F1]` …) is in the stamped findings file (P3 then makes sure each is dispositioned); the ledger chain is intact up to those lines |
| `P10_ALICE_VETO` | `../veto/VETOES.jsonl` holds a veto run **for this seal and its current `body_sha256`** whose outcome is `NO_BREAK`, `BREAK_NOT_REPRODUCED` or `BREAK_INVALID` (order 48b), and whose recorded files **still hash as recorded**; **no** `BREAK_CONFIRMED` or `BREAK_DISPUTED` (nor order 48's legacy `BREAK_REPRODUCED`) for this body (union rule); the whole chain intact. `FAILED` or no run → refused |

All ten are evaluated on every call, so a refusal lists everything that is wrong at once.
A precondition whose input is missing reports `not-evaluated` (P2, P3 and P9 depend on P1,
P7 on P1 and P6) — never `ok`.

### Why P7 exists

Order 02 hashed the pre-registration **excluding the stamp block**, exactly as specified. The
stamp block is plain markdown in the same file, hand-editable, and was covered by no hash at
all. Odin put this line inside it, after the token was issued:

```
- **B4 — added AFTER the token.** Any accuracy above 0.30 also counts as a pass.
```

`verify` reported `INTACT ... byte-for-byte`, exit 0. A post-hoc band had been added to a
sealed pre-registration and the gate certified the file as unchanged — an affirmative false
statement from the component whose whole job is to be trustworthy about this. The failure
class is the one ATTEST calls D30-M1: **the monitor computed its testimony from a region the
attacker controlled.**

The fix is both mechanisms, because either alone has already failed once:
**detection** (a whole-file hash, below) and **prevention** (P7 keeps the unguarded region
too small to hide anything in).

### P8 — the pre-flight (order 21, item 15a)

Every big kill in this programme was findable in an afternoon and was found months late.
P8 makes that afternoon mandatory and first: no run token without a pre-flight brief from
`../preflight/preflight.py`, which asks Alice (Gemini, web only, no access to our memory) to
find reasons the idea is already done, already dead, or bounded by a known impossibility.
The brief is written by the program from her reply, not by the seal's author.

The seal names the brief in its **body**:

```
## PRE-FLIGHT
Brief: PREFLIGHT_myidea_20261001T120000Z.md
Brief-sha256: <sha256 of the brief file's raw bytes>
```

Because both lines are in the body, the body hash already covers them: **no stamp-block
change, and `verify` is unchanged, so tokens issued before P8 still verify.** P8 imports
`preflight.py` (it does not shell out); if that module cannot be loaded, P8 fails. If Odin
appends `## Odin's additions` to a brief after sealing, its bytes change and P8 refuses
until the seal pins the new hash. There is no waiver, no date-based exemption and no flag.

### P9 — reviewer rotation (order 47, item 9)

Every cold review so far was Claude-family reviewing Claude-family: shared blind spots are a
same-generator risk one level up. `review.py` routes reviews between two reviewers, `Alice`
(Gemini 3.1 Pro via `agy`) and `Claude-cold` (a cold `claude -p` session, coldread's flag set,
≤ $2.00), and makes the choice mechanical so the author cannot pick a friendly one:

```
review.py assign <PREREG.md>    # prints Alice or Claude-cold
review.py run    <PREREG.md>    # calls the ASSIGNED reviewer → REVIEW_<reviewer>_<UTC>.md beside the seal
```

- **Strict alternation** over the ledger: a new seal gets the other reviewer than the last
  *new* assignment; the first ever is `Alice`. Not a coin, and not a hash of the seal (a hash
  can be re-rolled by editing a comma). Asking again for an assigned seal (same path relative
  to `~/helix`) returns its assignment and never re-rolls; every call, new or repeat, appends a
  line. A seal outside `~/helix` is keyed by its absolute path and labelled `throwaway: true`
  (it still counts for the alternation).
- `run` sends the cold review brief (*find what would make this seal pass when the claim is
  false: missing controls, bands that can't fail, circular checks, a bar not derived*), the
  seal, and every file the seal names by a relative path under its own directory: text only,
  ≤ 200 KB in total, anything left out listed; a path that leaves the directory (`../`, or a
  symlink out) is refused. Alice's prompt is one argv string, so it is capped at 120,000 bytes
  (seal first, then files until the cap). The raw review is written once and never
  overwritten; a reviewer failure or a session that is not cold writes nothing. Its header
  also lists `named but not found`: tokens that look like paths (a `/` in them, or ending in
  `.py .md .json .csv .txt .sh`) that name nothing on disk (order 47b).
- `REVIEWS.jsonl` (beside this file) is **append-only and hash-chained**: each line carries
  `seq` and `prev` = sha256 of the previous line (genesis = sha256 of nothing), as regate's
  ledger does. `assign` lines: seal, reviewer, repeat, throwaway. `review` lines: seal,
  reviewer, `body_sha256` of the seal at review time, raw output path + sha256, finding ids,
  files inlined / left out.
- **Known limit:** this directory is not behind the write wall (`WALL_PATHS` in app.py), so the
  chain makes an edit **visible, not impossible** — the same as regate before the vault — and
  the last line has no successor to witness it. The nightly prints `- reviews: … · chain OK`
  or `🔴 chain BROKEN at line N`. That line checks the chain only: a throwaway seal's raw review
  in `/tmp` vanishing at reboot leaves it OK. P9 still needs a seal's raw file (order 47b).
- There is no flag that fakes, substitutes or passes over a reviewer. Tests inject a fake
  reviewer function with monkeypatch.

P9 reads the ledger, not the seal body, so the stamp block is unchanged and **`verify` is
unchanged: tokens issued before P9 still verify** (tested with a token issued by the sealgate
at commit `dc17419`). P9 imports `review.py` from this directory; if that cannot be loaded, P9
fails. Disagreement between reviewers is item 9a (order 48), not this.

### P10 — Alice's veto (order 48, amendment 9a)

Alice (Gemini via `agy`, not Claude) gets **execution in a quarantined sandbox only**
(`../veto/sandbox.py`: bwrap, one writable scratch dir under `__BULK__/veto/<UTC>/`,
no `~/helix`, no `/mnt`, no `/media`, the user bus filtered to the keyring). `veto.py run
<PREREG.md>` copies the seal into that scratch and briefs her: *make this seal PASS while its claim
is FALSE*. A `break.sh`/`break.py` she writes is run twice in a reproduction sandbox (no network,
no agy), 120 s each. Order 48b: each run gets a **pristine read-only copy of the seal** (from the
seal's own dir) and only her output files; a run that plants a seal-named file, a stdlib-named
`.py` or a renamed copy of a seal file is thrown out as tampering; the seal's declared
`Instrument:` is run by the harness as the instrument of record; and a **cold Claude judge** must
agree that the break holds for the seal as written. Outcomes: `BREAK_CONFIRMED` ·
`BREAK_DISPUTED` · `BREAK_INVALID (tamper: …)` · `BREAK_NOT_REPRODUCED` · `NO_BREAK` ·
`FAILED (<why>)`; order 48's `BREAK_REPRODUCED` lines are legacy. See `../veto/README.md`.

- **Union rule:** a single confirmed break for the current body refuses, whatever other runs
  say. A **disputed** one (Alice and the judge disagree) refuses too, with "disputed: Alice and
  the judge disagree, the user decides". A legacy `BREAK_REPRODUCED` still refuses its (old) body.
  `BREAK_INVALID` passes, like a break that did not reproduce. The acquittal control beside it is
  the calibration in `../veto/calibration/`: a known-true seal must survive her.
- **The body hash is the key.** Editing the seal changes `body_sha256` and needs a new run.
  `stamp` itself appends a blank line *outside* the stamp block, so **a run made before the
  stamp does not count** (tested): run Alice on the seal as stamped.
- `FAILED` (agy error, the 15-minute cap, no output) and "no run" both refuse: `not evaluated`
  is never `ok`.
- **No overrule path.** If one is wanted it is a later order and the user's decision.

P10 reads the veto ledger, not the stamp, so **`verify` is unchanged: tokens issued before P10
still verify** (tested with a token issued by the sealgate at commit `2963f58`). P10 imports
`../veto/veto.py`; if that cannot be loaded, P10 fails.

## There is no escape hatch

**No `--force`, no `--override`, no environment-variable waiver. None will be added.** A
gate that can be waived is a warning. If a seal is wrong, the seal gets fixed and
re-stamped; the gate is not argued with. `test_sealgate.py` asserts the absence, and so
does acceptance criterion 7:

```
$ grep -in "force\|override\|bypass\|skip_check" sealgate.py
$ echo $?
1
```

Two more things the gate will not do quietly, for the same reason:

- **`stamp` does not replace an existing stamp.** A stamp is a record. To re-stamp a
  corrected seal, remove the old block deliberately, leaving a git trace.
- **`token` does not overwrite an existing `RUN_TOKEN.json`.** Re-issuing would erase the
  commitment the first token made, which is the one thing this gate exists to keep.

## `verify` — the part that matters most

The token records **two** hashes of the pre-registration, both over **raw bytes**:

| field | covers |
|---|---|
| `file_sha256` | the whole file, stamp block included |
| `body_sha256` | the whole file with the stamp block region cut out |

plus `findings-sha256`, **also over raw bytes**, recorded by `stamp`, re-checked by P1, and
re-checked again by `verify`. One hash is *not* over bytes: the per-`##`-section hash, which
exists only so drift can be **named** by section. Detection never depends on it — `body_sha256`
decides that — so it cannot produce a false `INTACT`. `verify` recomputes all of it and distinguishes **three** states:

| state | condition | exit |
|---|---|---|
| `INTACT` | the whole file hashes to what was sealed | 0 |
| `STAMP REGION CHANGED` | the body matches, the file does not — something was edited inside the stamp block, where the body hash cannot see it | 4 |
| `DRIFT` | the body hash differs; the section that moved is named | 4 |

🔴 **The words "byte-for-byte" appear only when `file_sha256` matches.** In the stamp-region
case the message says plainly that the sealed body is intact **but** the stamp region was
edited after the token, and that content there is not covered by the body hash.

**Token schema.** `token` writes `sealgate/run-token/2`. A schema-1 token (order 02) carries
no `file_sha256`, so `verify` **refuses it loudly** — non-zero exit, and it says to re-issue.
There is deliberately **no fall back to body-only verification**: silently degrading to the
broken behaviour would be the same defect wearing a version number.

A body edit is named by section:

```
DRIFT: /tmp/.../PREREG.md is not what was sealed
the pre-registration changed after the token was issued (body sha256 1c87d10add75 -> 2cffbd9debca)
  section edited: BANDS — each names the state of the world that makes it fail
a seal that can be revised to match the result is not a seal
```

It catches an edit to the seal body, a section added or removed, the reviewer or date
swapped in the stamp, the stamp deleted, and the findings file edited after the fact.

**Hashing is over raw bytes, never over decoded or normalised text.** A trailing space is
content. A changed line ending is content. This applies to the findings file as much as to the
pre-registration: `read_text()` translates newlines and `errors="replace"` maps different
invalid bytes onto the same U+FFFD, so a findings file converted LF → CRLF, or with one invalid
byte swapped for another, decodes identically and would hash identically. Both are byte changes
to sealed evidence, and both are now reported. Order 02 normalised trailing whitespace and blank lines away,
which meant a whitespace-only edit inside a sealed band also passed as `INTACT` — a second
false claim from the same root cause. A gate that forgives whitespace has told you it does not
read the file you sealed.

## `refusals.log`

Every refusal appends exactly one line beside `sealgate.py` — timestamp, verb, prereg
path, and which precondition failed — so the gate can say how often it fired:

```
2026-09-23T21:29:20 exit=3 verb=token prereg=/tmp/.../p2/PREREG.md failed=P2_REVIEWER_NOT_AUTHOR
2026-09-23T21:29:20 exit=4 verb=verify prereg=/tmp/.../happy/PREREG.md failed=DRIFT
```

There is no way to point that log somewhere else; it is derived from this file's location.
It is git-ignored because the test suite writes to it on every run.

**This log is the gate's own record. The catches log / correction metric (item 18) is a
later order and must not be built on top of it.**

## Formats the gate expects

No convention existed to adopt — there is no review stamp anywhere in this programme's
pre-registrations — so these are defined here and shown in `fixtures/complete/`.

**Stamp block** — written by `stamp`, not by hand:

```
<!-- SEALGATE-STAMP BEGIN -->
## REVIEW STAMP — written by sealgate.py, not by hand
- reviewer: Alice
- date: 2026-09-23
- findings: FINDINGS.md
- findings-sha256: 4740f719dc84...
- sealgate: 1.0
<!-- SEALGATE-STAMP END -->
```

The stamp block must stay **exactly** this shape: those five fields, each once, that heading
line, and blank lines. Anything else is refused by P7 and named. The heading and the field
values are checked too (`date` must be an ISO date, `findings-sha256` 64 hex characters,
`sealgate` a version number).

**Author line** — in the header, above the first `##`. `**Author:** Odin`,
`**Authors:** A, B (role)`, `Written by:` and `Sealed by:` are all read; parenthetical
roles are ignored when matching, and matching is case-insensitive.

**Findings file** — one block per finding, `F<n>` first, each with a disposition:

```
- [F1] The bar of 0.72 is asserted, not derived.
  DISPOSITION: ADDRESSED — the bar is now stated as "above the highest non-claim control".
- [F2] Band B2 could be satisfied by an artefact of the split.
  DISPOSITION: DECLINED: the split is fixed before the run and its seed is in the token.
```

**Bands and stop conditions** — section headings matching `/bands?/i` and
`/(stop|kill) conditions?/i`. `kill conditions` is accepted because that is what
`workspace/seam/PREREG_v2.md` calls them.

**Controls block** — the table from `../controls/SEAL_CHECKLIST.md`, pasted into the seal.
The seven control names are order 01's, and `test_sealgate.py` reads that checklist
(read-only) to assert this gate's list has not drifted from it.

## Fixtures

- `fixtures/complete/` — a synthetic, complete pre-registration and findings file. Every
  test copies these into a tmp directory and breaks exactly one precondition, so a refusal
  can be checked to name that precondition **and no other**.
- `fixtures/real_copy/PREREG.md` — a byte-identical, mode-444 copy of a **real**
  pre-registration (`workspace/crosscheck/probe-study_hook/PREREG.md`). It is **supposed to be
  refused**: nothing in this programme has ever carried a review stamp. If that test ever
  passes, the gate has stopped checking. The real file is never touched.

## What this gate does not claim

It is files on a disk. Anyone who can delete `RUN_TOKEN.json` can start over, and this
cannot stop them. What it gives you is that **nothing is erased silently by the tool**,
every refusal is written down, and a seal that moved after its token cannot be presented as
one that did not.

## Tests

```
python3 -m pytest test_sealgate.py test_review.py -q
```

129 tests (94 in `test_sealgate.py`, 35 in `test_review.py`); the gate's all drive the real CLI
in a subprocess so the exit codes are the real thing.
Each of the ten preconditions has a test that breaks it alone; the drift check, the
author-equals-reviewer refusal, the refusals log, and the real-prereg canary each have
their own. Order 03 added 14, including Odin's exact injection caught at `verify` **and**
refused at `token`, a whitespace-only body edit, and a schema-1 token refused rather than
downgraded. Order 21 added 14 for P8: every refusal alone, RESHAPE with and without its quoted
line, the searcher-is-author refusal, and a token issued by the pre-P8 sealgate (commit
`8cb65ad`) still verifying under the current one. The `sealed`/`unsealed` test fixtures append
a pre-flight section and a program-built brief in the tmp copy; `fixtures/complete/PREREG.md`
itself is unchanged. Order 27 added 2: a new token's `issued_at` is UTC with a `Z`, and a token whose
`issued_at` is an old naive local time still verifies (`issued_at` is not hashed anywhere).
Order 41 added 1 (65). Order 47 added 12 for P9 (77) — the wrong reviewer stamped, the raw
review edited after, a finding id missing from the findings file, no assignment, no review, a
broken chain, a forged second assignment, not-evaluated without a stamp, fail-closed without
`review.py`, all-good → token, a token from the pre-P9 sealgate (`dc17419`) still verifying, and
no waiver in P9 — and `test_review.py` (32): alternation, no re-roll, the chain catching an
edited / deleted / inserted line, `run` refusing an unassigned seal, the fake reviewer's raw file
and ledger line, the Claude-cold init check, the 200 KB and 120,000-byte caps, `../` refused.
Every test has its own temp `REVIEWS.jsonl` and fake reviewers; none calls a real one.
Order 47b added 4 (78 + 35): P9 refusing when the raw review is gone, the nightly line staying
OK when a throwaway raw review is gone, and the `named but not found` header (two).
Order 48 added 16 for P10 (94): no run, NO_BREAK → token recording the veto, a break that did not
reproduce passing, a reproduced break refusing, the union rule (a later NO_BREAK does not wash it
out), FAILED never ok, an edited seal needing a new run, an older body's break not vetoing the
current one, a run before the stamp not counting, another seal's run not counting, her files
edited or deleted, a broken chain, fail-closed without `veto.py`, no waiver, and a token from the
pre-P10 sealgate (`2963f58`) still verifying; `check` now says "all ten". Order 48b (133): a
confirmed break refusing (it replaces the reproduced-break test), a disputed break refusing with
"the user decides", a tampered break (`hashlib.py`) passing as `BREAK_INVALID`, a legacy
`BREAK_REPRODUCED` line still refusing, and the union rule for both confirmed and disputed. Every
test has its own temp `VETOES.jsonl`, a fake Alice and a fake judge; none calls the real ones. The tests that break exactly one
*other* precondition edit the body, so the test helper gives the current body a fake-Alice
`NO_BREAK` run before `check`/`token` unless the test opts out (`no_auto_veto`).

**Timestamps (order 27):** a token's `issued_at` is written as UTC, `2026-10-02T00:12:00Z`.
Tokens issued before order 27 carry a naive local time; a reader (regate's cycle line) treats a
naive value as local time. `refusals.log` lines are unchanged (local).
