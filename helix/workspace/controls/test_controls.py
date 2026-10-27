"""Tests for controls.py — the seven controls.

Every control is exercised TWICE: once on synthetic data where it must PASS and
once on synthetic data where it must FAIL. A control that has never been seen to
fail is not trusted.

All synthetic data is defined here, is pure numpy, and is seeded.
"""

import subprocess
import sys

import numpy as np
import pytest

from controls import (
    answer_free_floor,
    decoy_label,
    influence_test,
    planted_positive,
    random_init_null,
    random_ranking_arm,
    scrambled_arm,
)

REQUIRED_KEYS = {"name", "value", "reference", "verdict", "detail"}


def assert_control_shape(res, expected_name):
    """No control may return a bare number."""
    assert isinstance(res, dict), f"expected dict, got {type(res)}"
    assert REQUIRED_KEYS <= set(res), f"missing keys: {REQUIRED_KEYS - set(res)}"
    assert res["name"] == expected_name
    assert res["verdict"] in ("PASS", "FAIL", "INDETERMINATE")
    assert isinstance(res["detail"], str) and res["detail"], "detail must be a non-empty string"
    assert res["reference"] is not None, "a value without a reference is the failure mode"


# ---------------------------------------------------------------- substrate

def nearest_centroid_fit(X_train, y_train):
    """A boring, deterministic fit_fn: fit_fn(X, y) -> predict_fn."""
    X_train = np.asarray(X_train, dtype=float)
    y_train = np.asarray(y_train)
    classes = np.unique(y_train)
    centroids = np.stack([X_train[y_train == c].mean(axis=0) for c in classes])

    def predict(X_test):
        X_test = np.asarray(X_test, dtype=float)
        d = ((X_test[:, None, :] - centroids[None, :, :]) ** 2).sum(-1)
        return classes[d.argmin(axis=1)]

    return predict


N = 300


def balanced_labels(n=N):
    y = np.zeros(n, dtype=int)
    y[n // 2:] = 1
    return y


def label_in_disguise(y, rng):
    """A 'representation' that is the label wearing a hat."""
    X = np.zeros((len(y), 4))
    X[np.arange(len(y)), y] = 5.0
    return X + rng.normal(scale=0.1, size=X.shape)


def informative_features(y, rng):
    X = rng.normal(size=(len(y), 8))
    X[:, 0] += 3.0 * y
    return X


# ------------------------------------------------- 1. answer_free_floor

def test_answer_free_floor_passes_when_the_answer_free_representation_is_noise():
    rng = np.random.default_rng(0)
    y = balanced_labels()
    reps = {"pure_noise": rng.normal(size=(N, 8))}
    res = answer_free_floor(nearest_centroid_fit, reps, y)
    assert_control_shape(res, "answer_free_floor")
    assert res["verdict"] == "PASS", res
    assert abs(res["value"] - res["reference"]) < 0.1, res


def test_answer_free_floor_fails_when_the_representation_is_the_label_in_disguise():
    rng = np.random.default_rng(1)
    y = balanced_labels()
    reps = {"label_in_disguise": label_in_disguise(y, rng)}
    res = answer_free_floor(nearest_centroid_fit, reps, y)
    assert_control_shape(res, "answer_free_floor")
    assert res["verdict"] == "FAIL", res
    assert res["value"] > 0.9, res


# -------------------------------------------------- 2. random_init_null

def test_random_init_null_passes_when_untrained_features_are_uninformative():
    y = balanced_labels()

    def make_untrained(seed):
        return np.random.default_rng(100 + seed).normal(size=(N, 8))

    res = random_init_null(nearest_centroid_fit, make_untrained, y)
    assert_control_shape(res, "random_init_null")
    assert res["verdict"] == "PASS", res


def test_random_init_null_fails_when_untrained_features_already_carry_the_label():
    y = balanced_labels()

    def make_untrained(seed):
        return label_in_disguise(y, np.random.default_rng(200 + seed))

    res = random_init_null(nearest_centroid_fit, make_untrained, y)
    assert_control_shape(res, "random_init_null")
    assert res["verdict"] == "FAIL", res
    assert res["value"] > 0.9, res


# ------------------------------------------------------ 3. decoy_label

def test_decoy_label_passes_when_the_decoy_capability_is_unreadable():
    rng = np.random.default_rng(2)
    y = balanced_labels()
    X = informative_features(y, rng)
    decoys = {"absent_capability": rng.permutation(y)}
    res = decoy_label(nearest_centroid_fit, X, decoys)
    assert_control_shape(res, "decoy_label")
    assert res["verdict"] == "PASS", res


def test_decoy_label_fails_when_a_capability_the_system_lacks_reads_out_anyway():
    rng = np.random.default_rng(3)
    y = balanced_labels()
    X = informative_features(y, rng)
    X[:, 1] *= 3.0
    decoys = {"absent_capability": (X[:, 1] > 0).astype(int)}
    res = decoy_label(nearest_centroid_fit, X, decoys)
    assert_control_shape(res, "decoy_label")
    assert res["verdict"] == "FAIL", res


# ---------------------------------------------------- 4. scrambled_arm

def paired_data(seed=4):
    rng = np.random.default_rng(seed)
    y = balanced_labels()
    return {"x": informative_features(y, rng), "y": y}


def corr_with_label(data):
    x = np.asarray(data["x"], dtype=float)[:, 0]
    y = np.asarray(data["y"], dtype=float)
    return float(abs(np.corrcoef(x, y)[0, 1]))


def test_scrambled_arm_passes_when_the_signal_dies_under_time_and_label_shuffles():
    res = scrambled_arm(corr_with_label, paired_data())
    assert_control_shape(res, "scrambled_arm")
    assert res["verdict"] == "PASS", res
    assert res["value"] > res["reference"], res


def test_scrambled_arm_fails_when_the_score_is_blind_to_shuffling():
    def shuffle_invariant_score(data):
        return float(np.asarray(data["x"], dtype=float).std())

    res = scrambled_arm(shuffle_invariant_score, paired_data())
    assert_control_shape(res, "scrambled_arm")
    assert res["verdict"] == "FAIL", res


def test_scrambled_arm_time_arm_works_on_a_bare_series():
    t = np.linspace(0, 12 * np.pi, 600)
    series = np.sin(t)[:, None]

    def lag1_autocorr(x):
        x = np.asarray(x, dtype=float).ravel()
        return float(abs(np.corrcoef(x[:-1], x[1:])[0, 1]))

    res = scrambled_arm(lag1_autocorr, series, shuffles=("time",))
    assert_control_shape(res, "scrambled_arm")
    assert res["verdict"] == "PASS", res


# ----------------------------------------------- 5. random_ranking_arm

def ranking_fixture(seed=5):
    rng = np.random.default_rng(seed)
    items = list(range(40))
    relevance = rng.integers(0, 4, size=40).astype(float)
    return items, relevance


def test_random_ranking_arm_passes_when_the_ranker_beats_random_ordering():
    items, relevance = ranking_fixture()

    def good_rank(its):
        return list(np.argsort(-relevance))

    res = random_ranking_arm(good_rank, items, relevance)
    assert_control_shape(res, "random_ranking_arm")
    assert res["verdict"] == "PASS", res
    assert res["value"] > res["reference"], res


def test_random_ranking_arm_fails_when_the_ranker_is_no_better_than_random():
    items, relevance = ranking_fixture()

    def useless_rank(its):
        return list(range(len(its)))

    res = random_ranking_arm(useless_rank, items, relevance)
    assert_control_shape(res, "random_ranking_arm")
    assert res["verdict"] == "FAIL", res


# --------------------------------------------------- 6. planted_positive

def test_planted_positive_passes_when_the_rig_finds_the_flaw_it_was_given():
    rng = np.random.default_rng(6)
    data = rng.normal(size=400)

    def detect(d):
        return float(np.asarray(d).mean())

    def plant(d, strength):
        return np.asarray(d) + strength * 2.0

    res = planted_positive(detect, plant, data)
    assert_control_shape(res, "planted_positive")
    assert res["verdict"] == "PASS", res


def test_planted_positive_fails_when_the_rig_is_blind_to_its_own_plant():
    rng = np.random.default_rng(7)
    data = rng.normal(size=400)

    def blind_detect(d):
        return float(np.asarray(d).size)

    def plant(d, strength):
        return np.asarray(d) + strength * 2.0

    res = planted_positive(blind_detect, plant, data)
    assert_control_shape(res, "planted_positive")
    assert res["verdict"] == "FAIL", res


# ---------------------------------------------------- 7. influence_test

def test_influence_test_passes_when_the_metric_moves_under_perturbation():
    rng = np.random.default_rng(8)
    data = {"x": rng.normal(size=(200, 3))}

    def metric(d):
        return float(np.asarray(d["x"]).mean())

    def perturb(d, seed):
        r = np.random.default_rng(seed)
        return {"x": np.asarray(d["x"]) + r.normal(loc=5.0, size=np.asarray(d["x"]).shape)}

    res = influence_test(metric, perturb, data)
    assert_control_shape(res, "influence_test")
    assert res["verdict"] == "PASS", res
    assert res["value"] > 0.0, res


def test_influence_test_fails_on_the_add_nonref_shape():
    """Reproduction of the add_nonref defect.

    add_nonref was accuracy measured on the NON-refused subset, while the hiding
    intervention was applied only where the refusal mask was True. The metric's
    subset and the perturbation's subset are disjoint by construction, so the
    number could never move: an algebraic identity reported as a result.
    """
    rng = np.random.default_rng(9)
    n = 400
    refused = np.zeros(n, dtype=bool)
    refused[: n // 2] = True
    logits = rng.normal(size=(n, 5))
    answers = logits.argmax(axis=1)
    data = {"logits": logits, "refused": refused, "answers": answers}

    def add_nonref(d):
        keep = ~d["refused"]
        return float((d["logits"][keep].argmax(axis=1) == d["answers"][keep]).mean())

    def hide(d, seed):
        """The hiding head: touches ONLY the rows where refused is True."""
        r = np.random.default_rng(seed)
        logits = np.array(d["logits"], dtype=float)
        logits[d["refused"]] = r.normal(loc=50.0, size=logits[d["refused"]].shape)
        return {**d, "logits": logits}

    res = influence_test(add_nonref, hide, data)
    assert_control_shape(res, "influence_test")
    assert res["verdict"] == "FAIL", res
    assert res["detail"] == "metric is an algebraic identity / insensitive to its own subject", res
    assert res["value"] == 0.0, res


def test_influence_test_is_indeterminate_when_the_metric_is_not_finite():
    def nan_metric(d):
        return float("nan")

    def perturb(d, seed):
        return d

    res = influence_test(nan_metric, perturb, {"x": np.zeros(3)})
    assert_control_shape(res, "influence_test")
    assert res["verdict"] == "INDETERMINATE", res


# ------------------------------------------------------ 5. no-torch rule

def test_library_imports_with_torch_import_blocked():
    """Acceptance criterion 5: usable without a GPU / without torch."""
    blocker = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'torch' or name.startswith('torch.'):\n"
        "            raise ImportError('torch import blocked by test')\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'torch' or name.startswith('torch.'):\n"
        "            raise ImportError('torch import blocked by test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "try:\n"
        "    import torch\n"
        "    raise SystemExit('BLOCKER FAILED: torch was importable')\n"
        "except ImportError:\n"
        "    pass\n"
        "import controls\n"
        "assert 'torch' not in sys.modules, 'controls pulled in torch'\n"
        "print('imported controls with torch blocked; controls has',\n"
        "      len([n for n in dir(controls) if not n.startswith('_')]), 'public names')\n"
    )
    proc = subprocess.run([sys.executable, "-c", blocker], capture_output=True, text=True, cwd=".")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "imported controls with torch blocked" in proc.stdout, proc.stdout


# ------------------------------------------------------ 6. the seal checklist (order 75)

def test_seal_checklist_says_planted_positive_cannot_be_skipped():
    """Item 14: the positive control is the one control a seal may not skip."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent / "SEAL_CHECKLIST.md").read_text()
    assert ("7. `planted_positive` must be `ran` and `PASS`. It is the one control that "
            "cannot be skipped.") in text
