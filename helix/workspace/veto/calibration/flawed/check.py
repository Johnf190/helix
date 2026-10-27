#!/usr/bin/env python3
"""check.py — the experiment this seal pre-registers. Stdlib only, deterministic, no network.

Runs median() from med.py on EVERY list in the domain and compares it with an oracle that does
not sort: the element with exactly (n - 1) / 2 elements below it. Prints one JSON line of numbers,
then the verdict as its last line: `VERDICT: PASS`, `VERDICT: FAIL` or `VERDICT: VOID (<why>)`.
"""

import hashlib
import importlib.util
import itertools
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MED_SHA256 = "91a5ae9252afafa4cad07419f3bc032ab47d6e43bffe670342cd907630a4f858"
BAR = 0.10
DOMAIN = range(-6, 7)
LENGTHS = (1, 3, 5)


def domain():
    """Every odd-length (1, 3, 5) list of distinct ints from range(-6, 7), in every order."""
    for n in LENGTHS:
        for xs in itertools.permutations(DOMAIN, n):
            yield list(xs)


def oracle(xs):
    for e in xs:
        if sum(y < e for y in xs) == (len(xs) - 1) // 2:
            return e
    raise AssertionError("no median: the list is not odd-length and distinct")


def agreement(fn):
    total = hits = 0
    for xs in domain():
        total += 1
        hits += fn(list(xs)) == oracle(xs)
    return hits / total, total


def main():
    raw = (HERE / "med.py").read_bytes()
    if hashlib.sha256(raw).hexdigest() != MED_SHA256:
        print("VERDICT: VOID (med.py is not the file this seal pins)")
        return 2
    spec = importlib.util.spec_from_file_location("med", HERE / "med.py")
    med = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(med)
    rng = random.Random(48)
    score, n = agreement(med.median)
    floor, _ = agreement(lambda xs: xs[0])                       # answer_free_floor
    null, _ = agreement(lambda xs: rng.choice(xs))               # random_init_null
    planted, _ = agreement(lambda xs: sorted(xs)[0])             # planted_positive
    out = {"lists": n, "bar": BAR, "agreement": round(score, 6),
           "answer_free_floor": round(floor, 6), "random_init_null": round(null, 6),
           "planted_positive": round(planted, 6)}
    print(json.dumps(out, sort_keys=True))
    if planted >= BAR:
        print("VERDICT: VOID (the planted wrong median passes the band: the rig cannot fail)")
        return 2
    print("VERDICT: PASS" if score >= BAR else "VERDICT: FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
