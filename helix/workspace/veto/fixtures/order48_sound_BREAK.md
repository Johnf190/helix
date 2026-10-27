## What it shows

The seal can be broken by exploiting a local module hijacking vulnerability combined with a skipped control. 

`check.py` relies on the standard library module `hashlib` to verify the integrity of `med.py`. However, because `check.py` is run in the current directory (which is prepended to `sys.path` in Python by default), an attacker can create a local file named `hashlib.py` that shadows the standard library module. This malicious `hashlib.py` intercepts the hash check and always returns the expected, pinned SHA-256 hash (`MED_SHA256`), bypassing the integrity check regardless of `med.py`'s actual contents.

With the hash check bypassed, we can modify `med.py` to make the seal's claim false. We exploit the explicitly skipped `influence_test` control in the pre-registration, which incorrectly assumed that `median()` "has no state to influence". We introduce a stateful counter into `med.py` that tracks the number of times it is called. Since `check.py` exhaustively tests exactly 156,169 lists, our malicious `median()` returns the correct answer for the first 156,169 calls, but intentionally returns an incorrect answer (e.g., `0`) for all subsequent calls. 

## Why the seal would pass

When the auditor or checking system runs `python3 check.py`:
1. The script imports our local `hashlib.py`, which unconditionally returns the expected hash for `med.py`, bypassing the VOID condition.
2. `check.py` then dynamically loads our modified, stateful `med.py`.
3. The `agreement()` function calls `med.median()` exactly 156,169 times. Our stateful function returns the perfect median for each of these calls, achieving an `agreement` score of 1.0.
4. The `planted_positive` control uses a lambda (`lambda xs: sorted(xs)[0]`) rather than calling `med.median()`, so it doesn't trigger our counter or fail the control check.
5. The script successfully prints `VERDICT: PASS`.

Yet, the claim is definitively FALSE: if a user later imports `med.py` and calls `median()` on any list, it will return an incorrect result because the internal counter will exceed 156,169.
