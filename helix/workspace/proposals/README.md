# proposals — the proposal engine (order 52, item 21; `134_*` Part Six)

*"Helix gathers information over time and proposes its own upgrades … but does not act on its own;
the user reviews."* This module makes **observation → proposal** automatic. Nothing else: it never
acts, never judges its own proposals, never approves them, never writes or starts a work order.

```
propose.py harvest [--json]                 the evidence record: files only, no model
propose.py draft [--dry]                    one batch, one cold session, ≤ $3.00
propose.py decide <P-id> approve|reject|park --quote "<the user's words>"
propose.py status [--json]
propose.py verify                           the three hash chains
```

## The laws, and where they live in code

- **Evidence.** Every proposal cites the ids of logged events it would have prevented. Every cited
  id must exist in this run's harvest snapshot. An item with **zero** valid ids goes to
  `LEADS.jsonl`, not the queue. An item with some unknown ids is **rejected**, never repaired.
- **No new evidence.** An item whose ids were all cited by earlier proposals (any status, and earlier
  items in the same batch) is rejected `repeat`. A run with fewer than 3 never-cited events records
  `outcome: SKIPPED (no new evidence)` and spends nothing.
- **Law A — Helix may propose, may not judge its proposals.** There is no field or verb in
  `propose.py` that says whether an approved proposal helped (no score, no "worked", no "effect";
  the test suite greps the source for them). Whether it helped is read later from the
  correction-rate metric (order 37), never written here.
- **Law B — friction.** Every proposal is `ADDS-FRICTION` or `REMOVES-FRICTION`. Each batch line
  records `adds`/`removes`. A batch with ≥ 1 proposal and 0 `ADDS-FRICTION` is recorded with
  `flag: "friction-only"` and shows 🔴 on the nightly. It is **not refused**: refusing would teach
  the engine to invent tightenings.
- **Law C — Alice's lane.** UI / visual / generation needs are `lane: alice`; their `change` is the
  need, never a design. The batch line counts them as `for_alice`.
- **the user decides, witnessed.** `decide` records a decision only if `--quote` appears verbatim in a
  `**You:**` line of `memory/sessions/log.md` whose nearest `## YYYY-MM-DD HH:MM` heading above is
  at most 7 days old. The log is read, never written. A proposal is decided once.
- **Approved → order.** Odin writes the work order by hand and cites the P-id in it. `status` shows
  `approved → order NN` once a file `build_orders/ORDER_NN_*.md` names the P-id.

## Part A — harvest (deterministic, read-only, no model)

| id | source | which |
|---|---|---|
| `catch:<n>` | `workspace/catches/CATCHES.jsonl` | every catch line minus those any retraction line names (`142_*`) |
| `report:<NN>:STOP` | `build_orders/reports/ORDER_*_report.md` | last line exactly `STOP`; text = first `## 🔴 STOP…` heading, else the report's first line |
| `check:<NN>:FLAGGED` / `check:<NN>:UNCHECKED` | `ORDER_*_report.check.json` | text = the first failing claim |
| `coldread:<date>:<k>` | `coldread/READS.jsonl` + `READS/<report>.md` | `k = 0` the top, `k = 1..` the runners-up in their rank (the plant is already filtered out by coldread) |
| `veto:<seq>` | `veto/VETOES.jsonl` | outcome `BREAK_CONFIRMED` |
| `swap:<seq>` | `seatswap/SWAPS.jsonl` | outcome `DISAGREE` |
| `replay:<line>` | `replay/REPLAYS.jsonl` | result `MISMATCH` or `TAMPERED` |
| `canary:<date>:<case>` | `canary/CANARY.jsonl` | a canary `missed`, or an acquittal `refused` (wrongly convicted) |
| `refusal:<gate>:<line>` | `regate/refusals.log`, `sealgate/refusals.log` | live targets only: `metric/correction_rate.py`'s own `REFUSAL` and `TEST_TARGET`, imported |

Each event is one line of text (≤ 300 chars). An id seen twice keeps its first event. A missing
source is listed under `unavailable`, never raised. The snapshot has no clock in it: the same files
give the same bytes, so its sha256 can be re-derived.

**Not harvested:** questions Odin could not answer from memory. No log of them exists yet. That gap
is itself a LEAD.

## Part B — draft

- The harvest is snapshotted to `__BULK__/proposals/<UTC>/harvest.json` and sha256'd;
  the raw answer is kept beside it as `answer.txt`.
- **One** cold session, reusing `coldread.py`'s code by import (`session_command`, `claude_session`,
  `check_cold`, `init_record`, `parse_json`): no tools, no MCP, no settings, an empty temp cwd,
  the prompt on stdin from a file, the init event checked. Warm init → `NOT_COLD`, nothing queued.
- Caps: `RUN_CAP_USD = 3.00`, one session (coldread's `SESSION_CAP_USD` = 2.50 is passed as
  `--max-budget-usd`). A session is not started if its cap would pass the run's cap; a session that
  reports more than the run's cap → `ABORTED (budget)`, nothing queued.
- `--dry`: a fake model that still builds the real argv; the ledgers are copied into a temp dir and
  the run goes there. No `claude` call, no spend, no live ledger write.
- Outcomes: `DONE` · `ABORTED (budget)` · `NOT_COLD` · `FAILED` · `SKIPPED (no new evidence)`.

### The brief (`BRIEF`, the session's system prompt, verbatim)

> You read the failure record of a personal AI assistant (Helix) and its build pipeline: things that were caught, builds that stopped, reports whose claims failed a check, cold reads of its fact base, vetoes, replication runs, canary misses and gate refusals. Each event has a stable id. You have no tools and no other context.
> Propose at most 5 upgrades to the system. Each proposal must cite the ids of the logged events it would have prevented; a proposal without evidence is worthless. Cite only ids you were given. An event marked `cited: true` was already cited by an earlier proposal: a proposal whose ids are all already cited is rejected as a repeat.
> Tag each proposal ADDS-FRICTION (it tightens: a new check, gate, refusal or test) or REMOVES-FRICTION (it loosens or streamlines). At least one proposal should tighten something. An engine that only removes friction is treated as compromised.
> Say which of these numbers it should move: correction-share, caught-before-vs-after, time-to-correction, vintage, or none.
> Lane: backend, or alice for anything visual, UI or generated media. For an alice item, describe it as a NEED for Alice, never as a design: say what is needed and why, not how it should look.
> Answer with JSON only: {"proposals": [{"title": "<short>", "evidence": ["<id>", ...], "change": "<what to change>", "acceptance": "<how we would know it is built right>", "moves": "<one of the five>", "friction": "ADDS-FRICTION|REMOVES-FRICTION", "lane": "backend|alice"}, ...]}

Items past the fifth are rejected `beyond the first 5`.

## Ledgers (beside `propose.py`, git-ignored, append-only, hash-chained)

Same chain shape as `SWAPS.jsonl` / `VETOES.jsonl`: each line carries `seq` and `prev`, the sha256
of the previous line's raw bytes (`sha256("")` for the first). `verify` names the first edited,
deleted or inserted line; `draft` and `decide` refuse to append to a broken chain.

- `PROPOSALS.jsonl` — `kind: proposal` (id `P-<YYYYMMDD>-<k>`, `status: pending`, the harvest sha it
  cites) and `kind: decision` (`approve` / `reject` / `park`, the user's quote, its log heading).
- `BATCHES.jsonl` — one line per run: harvest sha, events / new events, cost, init summary, outcome,
  accepted / leads / `rejected: [{title, why}]`, `adds` / `removes`, `for_alice`, `flag`.
- `LEADS.jsonl` — items whose evidence named nothing in the harvest.

## Part D — the nightly

`seam/nightly.py` calls `proposals_line()` right after `provenance_line()`:

```
- proposals: <p> pending · <a> approved · <r> rejected · friction +<adds>/−<removes> · leads <l> · last batch <date|never> · chain OK
```

with a 🔴 sub-line when a chain breaks, the last batch is `friction-only`, or the last batch is not
`DONE`. It reads the three ledgers only, never runs a batch, never stops the nightly.

## Part E — cadence

`systemd/helix-proposals.{service,timer}`: Sundays 10:00, `Persistent=true`, env as coldread's
service (`CLAUDE_CONFIG_DIR=%h/claude/config`, `UnsetEnvironment=ANTHROPIC_API_KEY`). Committed,
**not installed**: Odin enables it after his live check
(`systemctl --user enable --now helix-proposals.timer`).

## Tests

`python3 -m pytest -q test_propose.py` — temp dirs and a fake model only; never the live ledgers,
never a real `claude` call.
