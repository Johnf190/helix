# controls — the seven controls, on the shelf

One file, `controls.py`. Pure numpy. **No torch import anywhere**, at module level or
inside a function, so it runs on any box without a GPU.

Every control here has already killed a real result in this programme, and every one of
them was reinvented from scratch by a reviewer instead of being on a shelf. This is the
shelf.

## Use

```python
from controls import answer_free_floor, influence_test  # etc.

res = influence_test(metric_fn, perturb_fn, data)
if res["verdict"] == "FAIL":
    print(res["detail"])   # -> "metric is an algebraic identity / insensitive to its own subject"
```

Every control returns
`{"name", "value", "reference", "verdict", "detail", "stats"}`.

- `verdict` — `"PASS"`, `"FAIL"` or `"INDETERMINATE"`.
- `value` — the number the control measured.
- `reference` — the number it must be compared against. **No control returns a bare
  number**: a number without its reference is what caused the 2026-09-22 failure.
- `detail` — a sentence a reviewer can read.
- `stats` — the per-arm / per-seed / per-strength numbers behind the verdict.

## The seven

| control | question | what it killed |
|---|---|---|
| `answer_free_floor` | what does the probe score on a representation that **cannot** contain the answer? | probe-study: embedding table, 0 layers = **0.739** vs the "0.90 readable answer" |
| `random_init_null` | what does it score on an **untrained** network? | random-init site 0 = **0.180–0.325**, all above a 0.161 "detection" bar |
| `decoy_label` | can it "detect" a capability the system **does not have**? | `(x*y)%31` read at **0.403**, 2.5× the bar |
| `scrambled_arm` | does the signal survive **time-shuffling / label-permutation**? | ATTEST: 32× lift real vs 3.5× scrambled — base-rate blind |
| `random_ranking_arm` | does the ranker beat **random ordering** of the same items? | the baseline every ranking claim needs |
| `planted_positive` | can the rig detect a signal **you put there yourself**? | the missing control that made "instrument or rig?" unanswerable |
| `influence_test` | can the thing being gated **actually move the gate's metric**? | `add_nonref` — an algebraic identity reported as a result 24 times |

## Callable contracts

All deliberately boring:

```
fit_fn(X_train, y_train)  -> predict_fn(X_test) -> predicted labels
make_untrained_fn(seed)   -> representation array for that seed
score_fn(data)            -> float, higher = more signal
rank_fn(items)            -> list of INDICES into items, best first
detect_fn(data)           -> float, higher = more detection
plant_fn(data, strength)  -> data with a signal planted at that strength
metric_fn(data)           -> float
perturb_fn(data, seed)    -> perturbed data   (a 1-arg perturb_fn(data) is also accepted)
```

`scrambled_arm` takes either a dict with `"x"` (rows of features) and optionally `"y"`
(labels), or a bare array of feature rows. `"time"` permutes the feature rows; `"label"`
permutes the labels only. On a bare array the `"label"` arm cannot be run and the control
returns `INDETERMINATE` naming the skipped arm — it never silently drops it.

## Decision rules, stated so they can be argued with

- **Probe controls** (`answer_free_floor`, `random_init_null`, `decoy_label`): chance is the
  majority-class rate. A score counts as above chance only if it clears
  `chance + max(0.05, 2·SE)`, `SE = sqrt(p(1−p)/n_test)`. Accuracy is held-out, over a
  stratified split, averaged over repeats.
- **Null-distribution controls** (`scrambled_arm`, `random_ranking_arm`): empirical
  `p = (1 + #{null ≥ real}) / (n + 1)`, and PASS requires `p ≤ max(0.05, 1/(n+1))` — so with
  the default 10 shuffles the real score must beat **every** scrambled draw.
- **`planted_positive`**: PASS requires the detector to respond to the plant at full
  strength. `stats["faintest_detected"]` is the sensitivity floor of the rig.
- **`influence_test`**: FAIL when the largest movement over `n_perturbations` maximal
  perturbations is `≤ max(1e-12, 1e-9·|baseline|)`.

`n_repeats` / `n_seeds` / `test_frac` are keyword-only with defaults, so every call form in
work order 01 works unchanged.

## Tests

```
python3 -m pytest test_controls.py -q
```

Each control is exercised **twice**: once on synthetic data where it must PASS and once
where it must FAIL. A control that has never been seen to fail is not trusted. The suite
also reproduces the `add_nonref` shape (a metric computed on a subset the perturbation
provably never touches) and asserts `influence_test` returns FAIL on it, and it imports the
library in a subprocess with `torch` import blocked.

## Status

Standalone. **Nothing imports it.** Wiring it into Helix, the nightly, or any experiment is
a separate, sealed decision — not this library's business.
