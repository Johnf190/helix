# corrections — the correction queue (order 72, item 30)

A monthly cold read (`workspace/coldread/READS/<date>.md`) finds passages in the fact base that are
wrong or stale. Before this queue, nothing turned a finding into a correction: twenty days after
the first read, none of its findings had been fixed. `corrections.py` closes the loop:

    finding → queued → CLOSED by a newer fact that retires the passage (order 70)
                     → or REJECTED in the user's own words

It never edits a fact and never decides one. Odin checks each open finding against the fact and
its sources. If the finding is true, Odin writes a correcting fact (normal path; factgate applies)
with the right statement and a `RETIRES:` line, and the user approves it. If it is false, Odin puts it
to the user, and the user's words reject it.

## Verbs

```
corrections.py harvest [--dry]      parse every READS/*.md not yet harvested → kind "finding" lines
corrections.py status [--json]      open / closed / rejected, with ages
corrections.py reject <C-id> --quote "<the user's words>"
corrections.py verify               the hash chain
```

- **harvest** reads coldread's own rendered markdown (`coldread.py render_md`): the Verdict, each
  runner-up and every stage-1 candidate. The plant is skipped (its `← THE PLANT` mark, and its file
  from the `- plant:` line). Each distinct (file, whitespace-collapsed quote) is queued once, at its
  highest rank: `verdict` > `runner-up` > `candidate`. Ids are `C-<read date YYYYMMDD>-<k>`, with k
  counted from 1 in rank order. A verdict or runner-up gets the confidence of its stage-1 twin, if
  it has one. A read is harvested once (a `kind:"harvest"` line names it). An item that does not
  parse refuses the whole read, so nothing is silently dropped.
- **closed** is derived on every run and never written. A finding is closed when an APPLIED
  retirement (`memretrieve.retired_in()`, order 70's one parser) has A equal to the finding's file
  and a passage that overlaps the finding's quote. Both are whitespace-collapsed, and they overlap
  when either contains the other or they share ≥ 20 consecutive chars. The closing B is shown. If
  the retirement later stops resolving, the finding re-opens.
- **reject** appends `{kind:"rejection", id, quote, log_heading}`, but only when
  `propose.find_quote` finds the user's words in a `**You:**` line of the session log. This is the same
  witness rule and the same `DECISION_DAYS` as proposals. Each finding takes one decision.
- **status**: open means neither closed nor rejected. Age is the number of days since the read.

## Ledger

`QUEUE.jsonl` sits beside this file. It is append-only, and each line carries `seq` and the sha256
of the line before it (the chain is copied from `proposals/propose.py`). If the chain is broken,
`harvest` and `reject` refuse, and the nightly reports the break.

## The nightly

`nightly.py`'s `corrections_line()` comes right after `- coldread:`. It runs `harvest` first, so a
new monthly read is queued the same night, and then prints:

```
- corrections: O open (oldest D days) · C closed · R rejected
```

When the oldest open finding is more than 30 days old, the line gets `🟡` and the first 3 open ids
and files. With nothing queued, it reads `- corrections: none queued (run corrections.py harvest)`.
It never raises: an exception gives `- corrections: unavailable — <Type>: <msg>`.

## Tests

`python3 -m pytest -q -p no:cacheprovider test_corrections.py`. The real `READS/2026-10-03.md` is
read only. Every queue, fact base and log the tests use is a temp fixture.
