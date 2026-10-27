# PRE-REGISTRATION — SYNTHETIC FIXTURE 01 (not a real experiment)
**Author:** Odin
Sealed 2026-09-23, before any point of the sweep was run.

## The question, stated so it can be answered wrong
Does the synthetic probe read the planted signal above its own answer-free floor?
This fixture exists only to exercise the seal gate. No real data is involved.

## BANDS — each names the state of the world that makes it fail
- **B1 — the claim.** Probe accuracy on the held-out split exceeds 0.72.
  Fails if the answer-free floor reaches 0.62 (the bar less a 0.10 margin), in which case B1 means nothing.
- **B2 — the null.** Random-init null stays at or below 0.55 (chance 0.50 plus a 0.05 margin).
  Fails if an untrained network reads above 0.55, its own bar here, not B1's 0.72.
- **B3 — the decoy.** No decoy label reads above its own chance threshold.

## STOP CONDITIONS
- **S1.** If the answer-free floor lands above 0.62 (the bar less its margin), the run stops and the bar is wrong.
- **S2.** If `influence_test` returns FAIL on the gate metric, the run stops; there is no result
  to seal if the metric cannot be moved by its own subject.
- **S3.** If wallclock exceeds 2 h, the partial grid is reported as partial and not rescored.

## Scope, recorded before the result is known
One substrate, one seed set, synthetic data only. Nothing here generalises off this fixture.

## CONTROLS

```
CONTROLS SEAL BLOCK        (controls.py @ 121656b)
pre-registration:          fixtures/complete/PREREG.md
sealed by:                 Odin            date: 2026-09-23

| control             | ran / skipped | result (value vs reference, verdict) | if skipped: reason in writing |
|---------------------|---------------|--------------------------------------|--------------------------------|
| answer_free_floor   | ran           | 0.4185 vs 0.6200 PASS                |                                |
| random_init_null    | ran           | 0.5040 vs 0.5500 PASS                |                                |
| decoy_label         | ran           | 0.4407 vs 0.6054 PASS                |                                |
| scrambled_arm       | ran           | 0.4930 vs 0.5500 PASS                |                                |
| random_ranking_arm  | skipped       |                                      | nothing is ranked in this run  |
| planted_positive    | ran           | 2.0080 vs 0.0080 PASS                |                                |
| influence_test      | ran           | 5.0127 moved, baseline 0.0037 PASS   |                                |

Bar / threshold this result will be judged against:   0.72
Highest control value that is NOT the claim:          0.5040
```
