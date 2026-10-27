"""controls.py — the seven controls, on the shelf instead of reinvented.

Each control answers one question that has already killed a real result in this
programme. Each returns a small dict:

    {"name", "value", "reference", "verdict", "detail", "stats"}

`verdict` is one of "PASS", "FAIL", "INDETERMINATE". **No control returns a bare
number.** A number without its reference is what caused the failure these
controls exist to prevent. `detail` is always a human-readable string; the
numbers behind the verdict are in `stats`.

Substrate-agnostic by design: arrays in, dicts out. **No torch import anywhere**,
at module level or inside a function — the library must run without a GPU.

Callable contracts (all of them boring on purpose):

    fit_fn(X_train, y_train)      -> predict_fn(X_test) -> predicted labels
    make_untrained_fn(seed)       -> representation array for that seed
    score_fn(data)                -> float, higher = more signal
    rank_fn(items)                -> list of indices into `items`, best first
    detect_fn(data)               -> float, higher = more detection
    plant_fn(data, strength)      -> data with a signal planted at that strength
    metric_fn(data)               -> float
    perturb_fn(data, seed)        -> perturbed data  (perturb_fn(data) also accepted)

`n_repeats` / `test_frac` are keyword-only with defaults, so every call shown in
the work order works unchanged.
"""

from __future__ import annotations

import inspect

import numpy as np

PASS = "PASS"
FAIL = "FAIL"
INDETERMINATE = "INDETERMINATE"

_ABS_MARGIN = 0.05  # a probe must clear chance by at least this much to count


# --------------------------------------------------------------- internals

def _result(name, value, reference, verdict, detail, **stats):
    return {
        "name": name,
        "value": value,
        "reference": reference,
        "verdict": verdict,
        "detail": detail,
        "stats": stats,
    }


def _chance_level(y):
    """Majority-class rate: what you score by guessing without looking."""
    y = np.asarray(y)
    _, counts = np.unique(y, return_counts=True)
    return float(counts.max() / len(y))


def _margin(chance, n_test):
    """How far above chance a score must be before it means anything."""
    se = np.sqrt(max(chance * (1.0 - chance), 0.0) / max(n_test, 1))
    return float(max(_ABS_MARGIN, 2.0 * se))


def _stratified_split(y, test_frac, rng):
    y = np.asarray(y)
    test_idx = []
    for c in np.unique(y):
        idx = np.flatnonzero(y == c)
        rng.shuffle(idx)
        k = max(1, int(round(len(idx) * test_frac)))
        k = min(k, len(idx) - 1)  # never leave a class unrepresented in train
        test_idx.append(idx[:k])
    test_idx = np.concatenate(test_idx)
    mask = np.zeros(len(y), dtype=bool)
    mask[test_idx] = True
    return np.flatnonzero(~mask), test_idx


def _probe_accuracy(fit_fn, X, y, n_repeats, test_frac):
    """Mean held-out accuracy of fit_fn over n_repeats fresh splits."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    if len(X) != len(y):
        raise ValueError(f"representation has {len(X)} rows, labels have {len(y)}")
    accs = []
    n_test = 0
    for r in range(n_repeats):
        rng = np.random.default_rng(1000 + r)
        tr, te = _stratified_split(y, test_frac, rng)
        n_test = len(te)
        predict = fit_fn(X[tr], y[tr])
        accs.append(float((np.asarray(predict(X[te])) == y[te]).mean()))
    return float(np.mean(accs)), accs, n_test


def _empirical_p(real, null_scores):
    """P(null >= real), with the standard +1 so p is never 0."""
    null_scores = np.asarray(null_scores, dtype=float)
    return float((1 + int((null_scores >= real).sum())) / (len(null_scores) + 1))


def _alpha(n):
    """Achievable significance floor for n null draws, never tighter than 0.05."""
    return max(0.05, 1.0 / (n + 1))


def _ndcg(order, relevance):
    relevance = np.asarray(relevance, dtype=float)
    gains = relevance[np.asarray(order, dtype=int)]
    discount = 1.0 / np.log2(np.arange(len(gains)) + 2)
    dcg = float((gains * discount).sum())
    ideal = float((np.sort(relevance)[::-1] * discount).sum())
    return dcg / ideal if ideal > 0 else 0.0


def _call_with_optional_seed(fn, arg, seed):
    try:
        params = inspect.signature(fn).parameters
        takes_seed = len(params) >= 2
    except (TypeError, ValueError):
        takes_seed = True
    if takes_seed:
        return fn(arg, seed)
    return fn(arg)


# ------------------------------------------------------- 1. answer-free floor

def answer_free_floor(fit_fn, representations: dict, labels, *, n_repeats=3, test_frac=0.3):
    """What does this probe score on a representation that CANNOT contain the answer?

    Killed: probe-study read 0.739 off the embedding table with 0 layers, against a
    "0.90 readable answer". PASS means the floor sits at chance, so a score above
    it means something. FAIL means the answer-free representation is not
    answer-free (or the probe is reading the task, not the network).
    """
    if not representations:
        return _result("answer_free_floor", None, None, INDETERMINATE,
                       "no answer-free representations supplied")
    chance = _chance_level(labels)
    means, n_test = {}, 0
    for name, X in representations.items():
        m, _, n_test = _probe_accuracy(fit_fn, X, labels, n_repeats, test_frac)
        means[name] = m
    margin = _margin(chance, n_test)
    threshold = chance + margin
    worst = max(means, key=means.get)
    value = means[worst]
    above = {k: v for k, v in means.items() if v > threshold}
    if above:
        verdict, detail = FAIL, (
            f"answer-free representation(s) {sorted(above)} score above chance "
            f"({value:.3f} > {threshold:.3f}); the floor is not at chance, so any "
            f"result above it is not evidence of a readable answer")
    else:
        verdict, detail = PASS, (
            f"floor sits at chance (max {value:.3f} <= {threshold:.3f}); a probe "
            f"score above this floor is interpretable")
    return _result("answer_free_floor", value, chance, verdict, detail,
                   per_representation=means, chance=chance, margin=margin,
                   threshold=threshold, highest=worst, n_test=n_test,
                   n_repeats=n_repeats)


# --------------------------------------------------------- 2. random-init null

def random_init_null(fit_fn, make_untrained_fn, labels, *, n_seeds=3, test_frac=0.3):
    """What does the probe score on an UNTRAINED network?

    Killed: random-init site 0 scored 0.180-0.325, all of it above a 0.161
    "detection" bar. PASS means untrained representations are at chance. FAIL
    means the bar is below the null and measures the probe, not the network.
    """
    chance = _chance_level(labels)
    per_seed, n_test = {}, 0
    for s in range(n_seeds):
        X = make_untrained_fn(s)
        m, _, n_test = _probe_accuracy(fit_fn, X, labels, 1, test_frac)
        per_seed[s] = m
    margin = _margin(chance, n_test)
    threshold = chance + margin
    value = float(max(per_seed.values()))
    if value > threshold:
        verdict, detail = FAIL, (
            f"untrained network already reads at {value:.3f} > {threshold:.3f}; "
            f"any detection bar must sit above this null, not below it")
    else:
        verdict, detail = PASS, (
            f"untrained null at chance (max {value:.3f} <= {threshold:.3f})")
    return _result("random_init_null", value, chance, verdict, detail,
                   per_seed=per_seed, chance=chance, margin=margin,
                   threshold=threshold, n_test=n_test, n_seeds=n_seeds)


# -------------------------------------------------------------- 3. decoy label

def decoy_label(fit_fn, representation, decoy_labels: dict, *, n_repeats=3, test_frac=0.3):
    """Can the probe "detect" a capability the system DOES NOT HAVE?

    Killed: `(x*y)%31` — a function the model was never trained on — read at
    0.403, 2.5x the bar. PASS means the decoys are unreadable. FAIL means the
    pipeline manufactures readability and proves nothing about the real label.
    """
    if not decoy_labels:
        return _result("decoy_label", None, None, INDETERMINATE,
                       "no decoy labels supplied")
    per_decoy, refs, flagged = {}, {}, {}
    for name, dy in decoy_labels.items():
        chance = _chance_level(dy)
        m, _, n_test = _probe_accuracy(fit_fn, representation, dy, n_repeats, test_frac)
        threshold = chance + _margin(chance, n_test)
        per_decoy[name] = m
        refs[name] = threshold
        if m > threshold:
            flagged[name] = (m, threshold)
    worst = max(per_decoy, key=lambda k: per_decoy[k] - refs[k])
    value = per_decoy[worst]
    if flagged:
        verdict, detail = FAIL, (
            f"decoy capabilit(ies) {sorted(flagged)} read out anyway "
            f"(best {value:.3f} vs threshold {refs[worst]:.3f}); the probe detects "
            f"capabilities the system does not have")
    else:
        verdict, detail = PASS, (
            f"no decoy readable above chance (best {value:.3f} <= {refs[worst]:.3f})")
    return _result("decoy_label", value, refs[worst], verdict, detail,
                   per_decoy=per_decoy, thresholds=refs, flagged=sorted(flagged),
                   worst_decoy=worst, n_repeats=n_repeats)


# ----------------------------------------------------------- 4. scrambled arm

def _scramble(data, kind, rng):
    """"time" permutes feature rows; "label" permutes labels only.

    `data` is either a dict with "x" (and optionally "y"), or a bare array
    (feature rows only — then the "label" arm cannot be run).
    """
    if isinstance(data, dict):
        if kind == "time":
            x = np.asarray(data["x"])
            return {**data, "x": x[rng.permutation(len(x))]}
        if kind == "label":
            if "y" not in data:
                return None
            y = np.asarray(data["y"])
            return {**data, "y": y[rng.permutation(len(y))]}
        raise ValueError(f"unknown shuffle kind: {kind!r}")
    if kind == "time":
        x = np.asarray(data)
        return x[rng.permutation(len(x))]
    return None


def scrambled_arm(score_fn, data, *, shuffles=("time", "label"), n_repeats=10):
    """Does the signal survive time-shuffling / label-permutation?

    Killed: ATTEST's raw detection was base-rate blind — 32x lift on real data,
    but 3.5x on scrambled data, so most of the "detection" was structure the
    shuffle could not remove. PASS means the real score beats every scrambled
    draw. FAIL means the scrambled arm reaches the same place.
    """
    real = float(score_fn(data))
    if not np.isfinite(real):
        return _result("scrambled_arm", real, None, INDETERMINATE,
                       "score_fn returned a non-finite value on the real data")
    arms, p_values, skipped, pooled = {}, {}, [], []
    for kind in shuffles:
        draws = []
        for r in range(n_repeats):
            rng = np.random.default_rng(2000 + 97 * r + hash(kind) % 97)
            scrambled = _scramble(data, kind, rng)
            if scrambled is None:
                break
            draws.append(float(score_fn(scrambled)))
        if not draws:
            skipped.append(kind)
            continue
        arms[kind] = {"mean": float(np.mean(draws)), "max": float(np.max(draws)),
                      "draws": draws}
        p_values[kind] = _empirical_p(real, draws)
        pooled.extend(draws)
    if not arms:
        return _result("scrambled_arm", real, None, INDETERMINATE,
                       f"no shuffle arm could be run (skipped: {skipped})")
    reference = float(np.mean(pooled))
    alpha = _alpha(n_repeats)
    failing = {k: p for k, p in p_values.items() if p > alpha + 1e-12}
    lift = real / reference if reference not in (0.0,) else float("inf")
    if skipped:
        verdict, detail = INDETERMINATE, (
            f"arm(s) {skipped} could not be run on this data shape; "
            f"ran {sorted(arms)} with p={ {k: round(v, 4) for k, v in p_values.items()} }")
    elif failing:
        verdict, detail = FAIL, (
            f"scrambled arm(s) {sorted(failing)} reach the real score "
            f"(real {real:.4f} vs scrambled mean {reference:.4f}, "
            f"p={ {k: round(v, 4) for k, v in failing.items()} }); the signal does not "
            f"depend on the structure the shuffle destroys")
    else:
        verdict, detail = PASS, (
            f"real {real:.4f} beats every scrambled draw in {sorted(arms)} "
            f"(scrambled mean {reference:.4f}, lift {lift:.2f}x)")
    return _result("scrambled_arm", real, reference, verdict, detail,
                   arms={k: {"mean": v["mean"], "max": v["max"]} for k, v in arms.items()},
                   p_values=p_values, alpha=alpha, lift=lift, skipped=skipped,
                   n_repeats=n_repeats)


# ------------------------------------------------------ 5. random ranking arm

def random_ranking_arm(rank_fn, items, relevance, *, n_repeats=100):
    """Does the ranker beat RANDOM ORDERING of the same items?

    The baseline every ranking claim needs. Scored with nDCG over the full list.
    """
    items = list(items)
    relevance = np.asarray(relevance, dtype=float)
    if len(items) != len(relevance):
        raise ValueError(f"{len(items)} items but {len(relevance)} relevance values")
    order = list(rank_fn(items))
    if sorted(int(i) for i in order) != list(range(len(items))):
        raise ValueError("rank_fn must return a permutation of indices into items")
    real = _ndcg(order, relevance)
    draws = []
    for r in range(n_repeats):
        rng = np.random.default_rng(3000 + r)
        draws.append(_ndcg(rng.permutation(len(items)), relevance))
    reference = float(np.mean(draws))
    p = _empirical_p(real, draws)
    alpha = _alpha(n_repeats)
    if p > alpha + 1e-12:
        verdict, detail = FAIL, (
            f"ranker nDCG {real:.4f} is inside the random-ordering distribution "
            f"(mean {reference:.4f}, p={p:.4f}); the ordering carries no information")
    else:
        verdict, detail = PASS, (
            f"ranker nDCG {real:.4f} beats random ordering "
            f"(mean {reference:.4f}, p={p:.4f})")
    return _result("random_ranking_arm", real, reference, verdict, detail,
                   p_value=p, alpha=alpha, random_max=float(np.max(draws)),
                   random_std=float(np.std(draws)), n_items=len(items),
                   n_repeats=n_repeats)


# ------------------------------------------------------- 6. planted positive

def planted_positive(detect_fn, plant_fn, data, *, strengths=(1.0, 0.5, 0.25)):
    """Can the rig detect a signal YOU PUT THERE YOURSELF?

    Without this, "instrument or rig?" is unanswerable: a null result from a rig
    that has never caught anything is not a null result. PASS means the rig
    responds to a plant at full strength. FAIL means the rig is blind and its
    nulls mean nothing.
    """
    strengths = tuple(float(s) for s in strengths)
    if not strengths:
        return _result("planted_positive", None, None, INDETERMINATE,
                       "no plant strengths supplied")
    clean = float(detect_fn(data))
    if not np.isfinite(clean):
        return _result("planted_positive", None, clean, INDETERMINATE,
                       "detect_fn returned a non-finite value on clean data")
    tol = max(1e-12, 1e-9 * abs(clean))
    scores = {s: float(detect_fn(plant_fn(data, s))) for s in strengths}
    detected = {s: (np.isfinite(v) and v > clean + tol) for s, v in scores.items()}
    full = max(strengths)
    faintest = min((s for s in strengths if detected[s]), default=None)
    value = scores[full]
    if detected[full]:
        verdict, detail = PASS, (
            f"rig responds to its own plant at strength {full} "
            f"({value:.4f} vs clean {clean:.4f}); faintest plant still seen: {faintest}")
    else:
        verdict, detail = FAIL, (
            f"rig is blind to a plant at full strength {full} "
            f"({value:.4f} vs clean {clean:.4f}); a null from this rig is not evidence")
    return _result("planted_positive", value, clean, verdict, detail,
                   clean_score=clean, per_strength=scores,
                   detected={str(k): bool(v) for k, v in detected.items()},
                   faintest_detected=faintest, strengths=strengths)


# --------------------------------------------------------- 7. influence test

def influence_test(metric_fn, perturb_fn, data, *, n_perturbations=5):
    """Can the thing being gated ACTUALLY MOVE the gate's metric?

    Killed: `add_nonref` — accuracy measured on the non-refused subset while the
    hiding intervention only ever touched the refused subset. The two subsets are
    disjoint by construction, so the number could not move. It was an algebraic
    identity, and it was reported as a result 24 times.

    FAIL means a maximal perturbation of the metric's own subject left the metric
    unchanged.
    """
    baseline = float(metric_fn(data))
    if not np.isfinite(baseline):
        return _result("influence_test", None, baseline, INDETERMINATE,
                       "metric_fn returned a non-finite value on the unperturbed data")
    moves, values = [], []
    for r in range(n_perturbations):
        perturbed = _call_with_optional_seed(perturb_fn, data, 4000 + r)
        v = float(metric_fn(perturbed))
        if not np.isfinite(v):
            return _result("influence_test", None, baseline, INDETERMINATE,
                           f"metric_fn returned a non-finite value on perturbation {r}")
        values.append(v)
        moves.append(abs(v - baseline))
    value = float(max(moves))
    tol = max(1e-12, 1e-9 * abs(baseline))
    if value <= tol:
        verdict = FAIL
        detail = "metric is an algebraic identity / insensitive to its own subject"
    else:
        verdict = PASS
        detail = (f"metric moves under perturbation (max |delta| {value:.6g} vs "
                  f"baseline {baseline:.6g}, tolerance {tol:.3g})")
    return _result("influence_test", value, baseline, verdict, detail,
                   baseline=baseline, perturbed_values=values, moves=moves,
                   tolerance=tol, n_perturbations=n_perturbations)
