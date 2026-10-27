# seatswap — seat-swap replication (order 49, item 10)

*"A surviving result's analysis scripts get re-run on a different seat before final scoring: same
code, different mind reading it. Catches analysis-reasoning artifacts nothing else can."*

Every analysis script in Helix is Claude-written, so the different seat is always **Alice**
(Gemini 3.1 Pro via `agy`): the only non-Claude mind, and the only seat that can execute
(Claude-cold has no tools). Fable is unavailable (`134_*`), so item 10 routes to her.

```
python3 seatswap.py run <pack-name>     # exit 0 = recorded (read the outcome) · 2 usage · 3 refused
python3 seatswap.py check <pack-name>   # regate's S7: one JSON line; exit 0 AGREE · 4 anything else
```

## What a run does

The input is a **replay pack** (order 45, `../replay`): a flat dir on `__BULK__/replay/
<name>/` with the inputs, the script, and `manifest.json` (`cmd`, `numbers` {key: {value,
pattern}}, `tolerance` {key: tol}, `note`), listed in `../replay/PACKS.jsonl`.

1. **Pack check.** The pack is verified with `replay.py`'s own `verify` (imported, not copied): a
   changed, added or missing file, a moved mtime, or a manifest whose sha256 is not the one in
   `PACKS.jsonl` → `FAILED (pack tampered)`, and Alice is never asked.
2. **Scratch.** `__BULK__/seatswap/<UTC>/scratch/` gets `pack/` (every pack file, mtimes
   kept, and a **stripped** `manifest.json`: keys + patterns, tolerances, command, inputs, note;
   **no values, no hashes**) and an empty `alice/`. Files named like credentials (`credential`,
   `accounts`, `.env`) are never copied (packs should not hold any; checked anyway).
3. **Alice** runs in `../veto/sandbox.py`'s sandbox (imported, not copied: the order-48/48b recipe)
   with the whole scratch bound, **`pack/` bound read-only**, cwd `alice/`, 20-minute cap. Her brief
   is the order's text (keys listed, values WITHHELD; write your own `swap.py`; read the original but
   do not import, call, exec or copy it; last stdout line one JSON object; `NOTES.md` on anything
   wrong in the original), plus how her script is checked. The prompt is < 120,000 bytes and only
   points at `../pack/`.
4. **Independence check (harness), grep-level, and only that.** Over every file in `alice/`:
   - a file with the sha256 of a pack code file (a byte copy, under any name);
   - a line that names a pack code file's module or file name (`score_33b`, `score_33b.py`) beside
     `import`, `from`, `exec`, `eval`, `compile`, `runpy`, `subprocess`, `Popen`, `system`,
     `popen`, `spawn*`, `execv*`, `__import__`, `importlib`, `open`, `read_text`, `read_bytes`,
     `source`;
   - any use of dynamic execution or path games, whatever the target: `exec(`, `eval(`,
     `compile(`, `subprocess`, `importlib`, `runpy`, `__import__`, `ctypes`, `os.system`,
     `os.popen`, `os.exec*`, `os.spawn*`, `sys.path`, `pty`.

   `NOTES.md` is exempt from the line rules (her prose may name the script it criticises) but not
   from the sha check. Any hit → `COPIED`, which **does not count** either way. This is a grep, not
   a proof: a hand-retyped copy of the original with the names changed passes it. It catches the
   cheap failure (running the original and printing its numbers), not a determined forger.
5. **Reproduction.** Her `swap.py` (only that file) runs twice in the reproduction sandbox, each in
   a fresh `repro<N>/` with a pristine read-only `pack/` taken again from the bulk pack (it must
   hash exactly as the copy she was given, or `FAILED`), cwd `alice/`, no network, cleared
   environment, `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` (order 48b's
   hygiene), 120 s each. Both must exit 0 and print the **same last line**, a JSON object. Not →
   `NOT_REPRODUCED`.
6. **Compare** each manifest key with her number, within the pack's declared tolerance (absolute,
   as `replay.py`). All within → `AGREE`; else `DISAGREE (<key>: alice X, packed Y, tol Z; …)`. A
   missing key, or a value that is not a finite number, is a disagreement. Extra keys are ignored.
7. One line is appended to `SWAPS.jsonl`.

| outcome | meaning | S7 |
|---|---|---|
| `AGREE` | every key within tolerance | passes, for that manifest sha256 |
| `DISAGREE (…)` | at least one key off, or missing | **refuses** (even beside an AGREE) |
| `COPIED (…)` | the independence check hit | refuses |
| `NOT_REPRODUCED (…)` | not exit 0 twice with the same JSON last line | refuses |
| `FAILED (…)` | pack tampered, agy error / refusal / time cap, no `swap.py`, pack changed mid-run | refuses |

A refusal by Alice (order 48b's first run: "Sorry, I cannot fulfill your request") leaves no
`swap.py`: `FAILED (no output: no swap.py)`. Order 50 adds the automatic retry; until then, re-run by
hand.

## Personal data goes to Google

The real pack holds a **snapshot of the fact base** (personal facts). A seat swap sends it to Alice
(Google), as `alice_brain()` already does with `memory/`. Credential-named facts are not in the pack
(order 45b's FENCED_INPUT) and seatswap's own copy step skips such names again.

## The ledger

`SWAPS.jsonl` beside this file, **append-only and hash-chained** with `veto.py`'s chain (imported):
each line carries `seq` and `prev` = sha256 of the previous line's raw bytes (genesis = sha256 of
nothing), and `seatswap` = the version. A `swap` line: `pack`, `manifest_sha256`, `outcome`, `why`,
`changes` (tampered only), `files` (every file she wrote → sha256), `swap_sha256`, `notes_sha256`,
`independence` (hits), `repro` (per run: dir, rc, last line, stdout sha256, seconds), `alice_result`,
`comparison` (per key: key, alice, packed, tol, ok), `run_dir`, `scratch`, `copied`, `left_out`,
`prompt_bytes`, `alice` (model, rc, seconds, reply tail), `started`.

**Known limit:** this directory is **not behind the write wall**, so the chain makes an edit
**visible, not impossible**, and the last line has no successor to witness it. Her files live on the
HDD, not behind the wall either.

## S7 in regate (`check`)

`regate.py score` on a `kind: experiment` prediction runs `seatswap.py check <its pack>` as a
subprocess (regate never imports this file) and passes S7 only on `ok: true`:

- the pack is in `PACKS.jsonl` and verifies **now** (replay's `verify`);
- the `SWAPS.jsonl` chain is intact;
- there is an `AGREE` for the pack's **current** manifest sha256, and **no `DISAGREE`** for it
  (union rule, as sealgate's P10). An AGREE for an older manifest does not count.

`attach-pack <prediction-id> <pack-name>` in regate sets the pack once, and refuses unless `check`
says `pack_ok`.

## The nightly

`- seatswap: <n> runs · <a> agree · <d> disagree · chain OK`, `- seatswap: none yet`, or
`- seatswap: 🔴 chain BROKEN at line <n>`, right after the veto line. Reads only.

## No flag stands in for Alice

There is no flag that fakes, skips or substitutes Alice (`run` and `check` take one argument each).
Tests inject a fake Alice (`seatswap.ALICE`) with monkeypatch; her `swap.py` runs in the real
reproduction sandbox.

```
python3 -m pytest -q        # test_seatswap.py
```
