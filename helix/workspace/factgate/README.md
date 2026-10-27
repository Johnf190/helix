# factgate — detection for the fact base

`~/helix/memory/facts/` has **no git** and **no single write path**. `/remember`
(`app.py:709`) is a minor one; nearly every file there is written by Odin through the
SDK's Write/Edit/Bash tools. So a fact can be reworded, back-dated or deleted and
**nothing notices**.

This records what is there, in an append-only hash-chained ledger, and reports what
moved. It is **detection only** — prevention (a PreToolUse hook) is a separate piece,
and always will be partial, because a `sed -i` or a shell redirection can never be fully
gated. The nightly is the backstop.

## Seven verbs

```
python3 factgate.py baseline <facts_dir>                       record every file, once
python3 factgate.py check    <facts_dir>                        exit 0 clean / 4 findings
python3 factgate.py record   <facts_dir> <file> --reason "…"    acknowledge ONE new or changed file
python3 factgate.py record-delete <facts_dir> <file> --reason "…"
# order 22b — INTERIM by default
python3 factgate.py baseline-legacy <facts_dir> --cutoff-line N --cutoff-sha256 H --vault-seq S
python3 factgate.py interim  <facts_dir> [--json]               report; always exit 0
python3 factgate.py release  <facts_dir> <file> --witness KIND:REF --reason "…"
```

| exit | meaning |
|---|---|
| 0 | clean — and nothing else means clean |
| 2 | usage, or the directory cannot be read |
| 3 | refused: a `record` precondition failed |
| 4 | findings, or no ledger |

## 🔴 It never writes to the fact base — except `archive` and `restore`, which the user runs

Not a file, not a rename, not an mtime. Every other verb, including every refusal, is
read-only on `<facts_dir>`, and a test proves it by fingerprinting the directory's
names, bytes **and mtimes** before and after running all four verbs. `archive` and
`restore` (ORDER 35b, below) move one recorded file between `memory/facts/` and
`memory/archive/`, same bytes, one ledger line each; Odin's shell is refused both (hook P5).

The ledger lives in **`factgate/state/FACTS_LEDGER.jsonl`**, beside this program, never
inside `memory/`: that tree is read-only, and anything under `memory/facts/` is loaded
into Odin's context — a ledger there would be both a fence violation and noise in the
model's window. `--state-dir` points at a different one (the tests use it); the default
is what the nightly calls.

## 🔴 There is no bulk record

One file, one reason, one line. A single command that blesses every change is a rubber
stamp, and a rubber stamp is what this replaces. If 40 facts changed in a day, that is
40 deliberate `record` calls or one honest conversation about why.

## What `check` reports

| message | meaning |
|---|---|
| `NEW OFF-LEDGER: <file>` | it appeared without going through `record` |
| `CHANGED OFF-LEDGER: <file> <old12> -> <new12>` | its bytes differ from the record |
| `DELETED OFF-LEDGER: <file>` | it is in the record and not on disk |
| `UNTRACKED DIR: <name>` | a subdirectory; not recursed into, and nothing in it is watched |
| `LEDGER TAMPERED: line <n>` | the chain is broken — **and then nothing else is compared** |
| `NO LEDGER` | not baselined; exit 4, and **nothing is created** |

Hashes are over **raw bytes**. A `touch` is not a change. An LF→CRLF conversion is.
Every regular file is tracked, not only `.md`.

## The ledger

One JSON line per event: `file`, `sha256`, `size`, `kind` (`baseline` / `record` /
`delete`), `reason`, `ts`, `line`, `prev`, `self`. `prev` is the sha256 of the previous
line's exact bytes (genesis is the sha256 of the empty string) and `self` is the hash of
the line's own contents, so editing a line breaks its own hash *and* the next line's
chain.

## `record`'s preconditions

| id | requires |
|---|---|
| `F1_LEDGER_INTACT` | a ledger exists and its chain verifies |
| `F2_FILE_PRESENT` | the file is there (for `record-delete`: it is gone, and was recorded) |
| `F3_SOMETHING_CHANGED` | its bytes differ from its last ledger line — recording a no-op is refused |
| `F4_REASON_IN_WRITING` | a reason of at least three characters |

## What this does not claim

The ledger is a file on the same disk as the thing it watches. Anyone who can rewrite
the whole ledger can start over, and this cannot stop them — the chain makes a *partial*
edit detectable, not a wholesale replacement. It also cannot tell you whether a change
was *right*: only that it happened, when, and whether anyone wrote down why. An
off-ledger change is not an accusation; it is a question.

## INTERIM by default (order 22b)

Every new result enters the fact base as **INTERIM** — Odin's account — until a witness its
author did not write releases it. It is a **label, not a block**: writing a fact never gets
harder; the loader in `app.py` shows `🟡 INTERIM` and the nightly prints `- interim: …`.

- **Legacy** is a cutoff by ledger line, witnessed by the vault: vault seq 1 anchored
  `FACTS_LEDGER.jsonl` at n = 209, sha256 `d90663ab…554f`. `baseline-legacy` refuses unless the
  live ledger's first N lines hash to that value, then records as legacy every fact whose
  **last** ledger event is at or before line N **and** whose bytes still hash to it. It writes
  `state/LEGACY_BASELINE.json` **once** (with its own `self` hash). `interim` trusts a legacy
  entry only while the JSON's self hash, the ledger prefix hash and the re-derived rule all
  agree; editing a legacy fact (new sha) makes it INTERIM.
- **Release** appends a `release` event (`file`, `sha256` of the fact now, `witness`,
  `witness_sha256`, `reason`) to the same hash-chained ledger. If the fact's bytes change
  afterwards it is INTERIM again. `check` ignores release events (they record no change of
  content). Witnesses are **checked by the tool**, looked up from `helix/` (two levels above
  this program, never from a flag):

| kind | checked |
|---|---|
| `regate:<entry or prediction id>` | `regate.py check` on the registry exits 0, and `REGATE_LEDGER.jsonl` has a `kind: score` line for that id with outcome 0 or 1 |
| `seal:<path to RUN_TOKEN.json>` | the token exists, names a prereg beside it, and `sealgate.py verify` on that prereg exits 0 |
| `review:<path to findings file>` | exists outside `memory/`; its `Reviewer:` line names someone, none of them Odin (sealgate's `split_authors`/`normalise_name`) |
| `report:<build_orders/reports/ORDER_NN_report.md>` | in that folder, named `ORDER_NN_report.md` (not `*_review_odin.md` / `*_stopped_report.md`), last non-empty line exactly `DONE`, `READY FOR /restart` or `READY FOR USER'S INSTALL`, and the fact's text names order NN |

  Anything else is refused (exit 3), nothing written. **There is no `user:` kind**: the user's word
  typed by Odin is Odin's testimony. the user releases by hand in his own terminal; the hook (P5)
  refuses `release` and `baseline-legacy` from Odin's shell.

- **Verdict words** (`VERDICT_WORDS`, case-insensitive, whole word, backticks and quotes do not
  hide them): `proven`, `proves`, `decisive`, `passed`, `pass`, `confirmed`, `settled`,
  `definitive`, `conclusive`, `breakthrough`, `solved`, `novel`, `established`, `validated`,
  plus 🟢 in the first line. An INTERIM fact that carries them is flagged; a legacy or released
  one is not.

## ORDER 35b — archive, never delete

```
factgate.py archive <file>... --why "<one clause>"   memory/facts/ -> memory/archive/
factgate.py restore <file> --why "<one clause>"      memory/archive/ -> memory/facts/
```

An archived fact stops loading into Odin's context (the loader reads `memory/facts/` only),
stays on disk, is still found by the per-turn retriever at a lower weight and labelled
`(archived)`, and keeps its place in the ledger. Paths come from where this program lives,
never from a flag: `memory/facts`, `memory/archive`, `memory/PINNED.txt`, the seam registry.

- `archive` checks every named file first; **one refusal and nothing moves**. Refused:
  a file not in `memory/facts/` (or whose bytes are not the ledger's: an off-ledger edit must
  not go out of sight); a fact **listed in `memory/PINNED.txt`** (heading words pin nothing;
  to archive a pinned fact the user or a build order takes it off the list first — there is no
  flag); an INTERIM fact; a fact an `open` registry entry names (filename or `NN_` prefix,
  the loader's rule); a name already in the archive; a `--why` under 3 characters. A missing
  PINNED.txt or unreadable registry refuses too.
- Each move is `os.replace` and one line: `{"kind": "archive", "file", "sha256", "size",
  "reason", "actor"}`. `restore` lines carry `"restore": true` and the running count is
  printed — if it climbs, archiving is too eager. `restore` refuses a name the ledger has not
  archived, an archived file whose bytes changed, and a name already back in `memory/facts/`.
- `check` treats an archived name as "not here" and a restored one as here at the sha it
  left with. Archive and restore are not legacy events, so a legacy fact that goes and comes
  back unchanged stays legacy; a release is keyed on the sha, so it survives too.
- `actor` is the Unix user (the user and a builder share it; the hook keeps Odin out).

## Tests

```
python3 -m pytest test_factgate.py -q
```

114 tests (25 + 54 for order 22b + 35 for order 35b), nearly all driving the real CLI in a subprocess. Every `check` verdict and every
`record` precondition has a test that triggers it alone, plus the read-only proof and a
test that reaches the real fact base only to copy it and show it did not move.
