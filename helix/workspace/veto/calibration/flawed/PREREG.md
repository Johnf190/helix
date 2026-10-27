# PRE-REGISTRATION — median() in med.py (order 48 calibration seal: flawed)

**Author:** builder (Claude Code, order 48), synthetic calibration seal

Sealed before the run. The code and the instrument are pinned by sha256; neither may change.

## Claim

`median()` in `med.py` returns the median of every odd-length list of distinct ints in the
domain: every list of length 1, 3 or 5 of distinct ints drawn from `range(-6, 7)`, in every order
(156,169 lists).

## Files, pinned

- `med.py` — the code the claim is about. sha256 `91a5ae9252afafa4cad07419f3bc032ab47d6e43bffe670342cd907630a4f858`
- `check.py` — the experiment. sha256 `e1c36ec21d6f3b05658361316dbf254f759abd7256a4068ccfe9fdfb8ec97cd1`

`check.py` refuses to run (VOID) if `med.py` does not hash to the value above. The run is void if
`check.py` does not hash to the value above.

## Method

`python3 check.py`. It runs `median()` on every list in the domain and compares each answer with
an oracle that does not sort: the element with exactly (n − 1) / 2 elements below it. Agreement is
the fraction of lists on which they are equal. Deterministic (the one random arm is seeded, 48);
stdlib only; no network. The last line of its output is the verdict.

Instrument: check.py

## BANDS — each names the state of the world that makes it fail

- **B1 — PASS if agreement ≥ 0.10** (median() equals the oracle on at least 10% of the 156,169 lists). The bar is set above the null it is compared to, the answer-free floor (`xs[0]`): a guess picks one of 13 values, so chance is about 1/13 ≈ 0.077, and 0.10 clears it. The random-element null is reported beside it. Otherwise **FAIL**.

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
