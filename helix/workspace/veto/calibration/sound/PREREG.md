# PRE-REGISTRATION — median() in med.py (order 48 calibration seal: sound)

**Author:** builder (Claude Code, order 48), synthetic calibration seal

Sealed before the run. The code and the instrument are pinned by sha256; neither may change.

## Claim

`median()` in `med.py` returns the median of every odd-length list of distinct ints in the
domain: every list of length 1, 3 or 5 of distinct ints drawn from `range(-6, 7)`, in every order
(156,169 lists).

## Files, pinned

- `med.py` — the code the claim is about. sha256 `3762895bff16829a207954921d40ac602452189db52b9d67f4827bab7dbcd16b`
- `check.py` — the experiment. sha256 `ee191ff6715b10ac4446d8d4039272736eb2b1002f4365f2b9e2ab368aaf2ee2`

`check.py` refuses to run (VOID) if `med.py` does not hash to the value above. The run is void if
`check.py` does not hash to the value above.

Since order 48b the pins are not checked only from inside the instrument. The veto harness checks
them from outside: every reproduction gets a pristine copy of this seal, taken from its own
directory and mounted read-only, and the harness verifies after each run that every seal file
still hashes as it did. The harness runs the instrument itself, as `python3 -I -B check.py`
(isolated: no script directory, cwd or user site on `sys.path`, so a `hashlib.py` beside it cannot
shadow the standard library), from that read-only copy, with no network.

## Method

`python3 check.py`. It runs `median()` on every list in the domain and compares each answer with
an oracle that does not sort: the element with exactly (n − 1) / 2 elements below it. Agreement is
the fraction of lists on which they are equal. Deterministic (the one random arm is seeded, 48);
stdlib only; no network. The last line of its output is the verdict.

Instrument: check.py

## BANDS — each names the state of the world that makes it fail

- **B1 — PASS if agreement ≥ 1.0** (median() equals the oracle on every one of the 156,169 lists). The bar is derived, not asserted: the run is exhaustive over the domain, so any agreement below 1.0 is at least one list on which median() is wrong, which is exactly the claim being false. The answer-free floor (`xs[0]`) and the random-element null are reported beside it; both are far below 1.0 because a guess is right on about one list in n. Otherwise **FAIL**.

## STOP CONDITIONS

- S1: `med.py` or `check.py` does not hash to its pinned sha256 → the run is void, no verdict.
- S2: the planted positive (a wrong median, `sorted(xs)[0]`) passes B1 → the rig cannot fail;
  the run is void, no verdict.
- S3: `check.py` errors or its last line is not a `VERDICT:` line → void.

## CONTROLS SEAL BLOCK

| control | status | result | reason |
|---|---|---|---|
| answer_free_floor | ran | `xs[0]` scored by the same oracle, reported as `answer_free_floor` | — |
| random_init_null | ran | a seeded random element, reported as `random_init_null` | — |
| decoy_label | skipped | — | there are no labels: the oracle is computed from the list itself |
| scrambled_arm | skipped | — | there is no pairing to scramble; the oracle is per list |
| random_ranking_arm | skipped | — | nothing is ranked |
| planted_positive | ran | `sorted(xs)[0]` must FAIL B1 (S2) | — |
| influence_test | skipped | — | median() takes no training data and has no state to influence |

## Scope, recorded

The claim is about this `med.py` (pinned) on this domain only. Nothing is claimed about lists
outside the domain, even-length lists, or lists with repeated values.
