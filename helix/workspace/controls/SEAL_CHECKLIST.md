# CONTROLS SEAL BLOCK — paste into the pre-registration

Skipping a control is allowed. Skipping it **silently** is not. Every row gets a
`ran` / `skipped` and, if skipped, the reason **in writing**, before data is collected.

```
CONTROLS SEAL BLOCK        (controls.py @ <commit hash>)
pre-registration:          <name / file>
sealed by:                 <who>            date: <YYYY-MM-DD>

| control             | ran / skipped | result (value vs reference, verdict) | if skipped: reason in writing |
|---------------------|---------------|-------------------------------------|-------------------------------|
| answer_free_floor   |               |                                     |                               |
| random_init_null    |               |                                     |                               |
| decoy_label         |               |                                     |                               |
| scrambled_arm       |               |                                     |                               |
| random_ranking_arm  |               |                                     |                               |
| planted_positive    |               |                                     |                               |
| influence_test      |               |                                     |                               |

Bar / threshold this result will be judged against:   <number>
Highest control value that is NOT the claim:          <number>   <- if this is above the bar, the bar is wrong
```

## Rules for filling it in

1. **Fill it in before collecting data.** A control chosen after seeing the number is not a
   control.
2. **`influence_test` is not optional.** If the gate metric cannot be moved by the thing the
   gate is about, there is no result to seal. Skipping it needs the reason a reviewer would
   accept, and "the metric obviously depends on it" is not that reason — `add_nonref`
   obviously depended on it too, 24 times.
3. **Paste the returned dict, not the verdict.** `value` and `reference` both, always.
4. **The bar goes above the highest non-claim control**, not above zero. If the answer-free
   floor is 0.739, a bar of 0.161 is not a bar.
5. A control that returns `INDETERMINATE` counts as **not run**. Say why it was
   indeterminate.
6. "Not applicable" is a legitimate reason (e.g. `random_ranking_arm` with nothing ranked) —
   write it down anyway, so the reader knows it was considered and not forgotten.
7. `planted_positive` must be `ran` and `PASS`. It is the one control that cannot be skipped.
