# The Claim Protocol — v1, 2026-09-22

A claim is not a sentence. It is a **record with five attachments**, and it does not exist until
all five are present. One page. If this needs a second page it has become ritual — cut it back.

## The unit

**#NNNN — one sentence, stating what is true, with its numbers.**

| # | attachment | what it must contain | refused if |
|---|---|---|---|
| 1 | **Extractor** | the procedure that produces the number from data, runnable by someone else, with error bars and the n it rests on | the number cannot be recomputed, or has no error bar |
| 2 | **Proxy admissibility** | for every hand-chosen mapping (which column is "load", which reference is "near"): the criterion that admitted it, and the check that it **does not consult the outcome test** | any classifier can see the label it is predicting |
| 3 | **Jurisdiction** | the conditions under which the claim is meaningless, stated as a rule the code can apply, plus a refusal output | it has no stated place where it does not apply |
| 4 | **Cold review** | the verdict of a session with no history, no vocabulary and no framing, that tried to kill it — attached in full, including a survived kill | not attempted, or the reviewer was shown the author's framing |
| 5 | **Residual licence** | what the standard baseline already explains, and what is left for this claim to own | no baseline was fitted, or the claim's share is inside the noise floor |

## Reviewer independence

- The cold session sees **the claim, the data and the code. Never the author's framing** — no
  history, no motivation, no name for the idea, no indication of what the author hopes is true.
- Cold reviews are **seeded**: on a schedule the author does not control, a reviewer is handed a
  claim already known to be sound. A reviewer that kills those is a **killer, not a
  discriminator**, and its verdicts are discounted.
- Both rates are tracked per reviewer: **recall** on known-broken claims, **false-alarm rate** on
  known-sound ones. A reviewer with no measured discrimination cannot retire an entry.
- The author may rebut a cold review only with new measurement, never with context.

## Standing laws (each one was paid for)

1. **No classifier may consult the outcome test.** (SEAM v1: "near" meant "the file exists", and
   "broken" meant "the file does not exist". The result was a definition in costume.)
2. **No ranking claim without a random arm.** (Distance beat recency 4×, and lost to random.)
3. **Errors cluster by document.** 992 claims from 157 files is 157 independent things, not 992.
4. **A failed measurement is reported as failed, never tuned until it speaks.** (The churn proxy
   returned 1.00 for 96.5 % of claims; it was reported, not rescaled.)
5. **Every entry carries a kill condition, written before the run**, and fires it itself.
6. **A band must name the state of the world that makes it fail.** If the answer is "a change
   that also moves the threshold", it is not a band — it is self-normalising. Two sealed bands
   in this project were vacuous: one an algebraic identity (a common shift cancels in a
   leave-one-out residual), one a quantile of its own calibration distribution.

## Lifecycle

`registered → predicted (a dated forward number) → scored (Brier, by the nightly audit) → kept or killed`

An entry that has not been scored is **not evidence**, whatever it says. An entry whose kill
condition fires is rewritten as a kill, dated, above the original — never quietly deleted.
