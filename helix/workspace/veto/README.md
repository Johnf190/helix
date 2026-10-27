# veto — Alice's veto (order 48, amendment 9a)

Alice (Gemini 3.1 Pro via `agy`, **not Claude**) gets an audit brief per seal (order 50; same task as
48b's *make this seal PASS while its claim is FALSE*): *find out whether its checks could report PASS
when its claim is actually false*. A refusal (no output, reply matching the fixed `REFUSAL_PATTERNS`)
gets one automatic retry with the same brief; both attempts are in the one ledger line (`attempts`). She gets **execution in a quarantined sandbox only**, never write
access to Helix core, facts or ledgers. A break she can script, that reproduces **cleanly** (no
tampering) and that a **cold judge confirms**, vetoes the seal before the user's review (order 48b). `sealgate.py`'s `P10_ALICE_VETO` refuses a token without a passing run for the
seal's current body.

```
python3 veto.py run <PREREG.md>      # exit 0 = recorded (read the outcome) · 2 usage · 3 refused
```

## What a run does

1. A fresh `__BULK__/veto/<UTC>/scratch/` (bulk lives on the HDD) gets a **copy** of the
   seal in `scratch/seal/`: the seal, the text files beside it, and the files it names under its own
   directory. Text only, ≤ 200 KB, nothing that resolves outside the seal's directory (`../`, a
   symlink out). Files named like credentials (`credential`, `accounts`, `.env`) are never copied.
   Files named like her output (`break.sh`, `break.py`, `BREAK.md`, `NO_BREAK.md`) are never copied
   either: only what **she** writes counts.
2. Alice runs in **her sandbox** (`sandbox.py`) with cwd = `scratch/alice/` (empty) and
   `scratch/seal/` bound **read-only**, with the brief as `-p` (< 120,000 bytes; the seal is in
   scratch, the prompt only says where: `../seal/`). 15-minute cap; her process group is killed
   at the cap.
3. Every file she wrote or changed is hashed (sha256) **before anything else runs**.
4. If the seal declares an **instrument** (below), the harness runs it.
5. If she wrote `break.sh` (or `break.py`), it is **reproduced** twice, then **tamper-checked**,
   then, if it reproduced cleanly, **judged** (below).
6. One line is appended to `VETOES.jsonl`.

Outcomes:

| outcome | meaning | P10 |
|---|---|---|
| `NO_BREAK` | she wrote `NO_BREAK.md` and no break script | passes |
| `BREAK_NOT_REPRODUCED` | her script did not run the same, exit 0 and end `BREAK:` twice | passes |
| `BREAK_INVALID (tamper: <names>)` | a reproduction run left a tampering file (below) | passes |
| `BREAK_CONFIRMED` | reproduced, clean, and the cold judge says the break holds | **refuses** |
| `BREAK_DISPUTED` | reproduced, clean, and the judge says it does not: Alice and the judge disagree, **the user decides** | **refuses** |
| `FAILED (<why>)` | agy error, time cap, no output, `refusal x2`, the seal changed mid-run, judge error, judge not cold, no JSON verdict | refuses unless another run passes |
| `BREAK_REPRODUCED` | **legacy** (order 48): reproduced, never tamper-checked or judged. The two order-48 calibration lines; still read | **refuses** its (old) body |

A break and a `NO_BREAK.md` side by side counts as the break.

## Reproduction hygiene (order 48b)

Order 48's calibration showed why: Alice "broke" the sound seal by writing `hashlib.py` (shadowing
the stdlib, so the instrument's own pin check always matched) and a rewritten `med.py` into the
directory the instrument ran from, because reproduction copied her whole scratch, her edited copy of
the seal included. Now each of the two reproduction runs gets a fresh `repro<N>/`:

- `seal/`: a **pristine** copy of the seal, taken again from the seal's **own directory** (never
  from her scratch), bound **read-only**. It must hash exactly as the copy she was given, or the
  run is `FAILED` (the seal changed during the run).
- `alice/`: **only** her output files, `break.sh|break.py`, `BREAK.md` and regular files under
  `evidence/` (the brief tells her to put helpers there). cwd = `alice/`.
- Environment cleared, plus `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`;
  no `PYTHONPATH`; no network.

**The tamper check** runs after each run, by the harness, over every file in `alice/` at the end
(hers, or made by her script): any file named like a seal file; any `.py`/`.pyc` whose module
name (its stem, or a package directory above it) is in `sys.stdlib_module_names`; any file whose
content is a seal file's sha256 (a renamed copy; the empty file is no one's copy). It also checks
that every seal file still hashes as it did (the read-only mount should guarantee it; it is
checked anyway). Any hit → `BREAK_INVALID (tamper: <names>)`, and the judge is not asked.

Replayed through this harness, her order-48 sound-seal `break.sh` (committed as
`fixtures/order48_sound_break.sh`, sha pinned) comes out `BREAK_INVALID (tamper: hashlib.py,
med.py)`: tested.

## The seal's own checker: `Instrument:`

A seal MAY declare one line `Instrument: <relative path to a .py>`: a **fast design-checker on
synthetic or stand-in data, ≤ 120 s. Never the real experiment**: running the real thing before the
token defeats pre-registration. The harness runs it itself, `python3 -I -B <path>`, cwd = a
pristine read-only copy of the seal, no network, 120 s, and records rc, stdout sha256 and last line
(`instrument` in the record; the stdout is kept beside the run). It goes to the judge as the
**instrument of record**. Not declared → `instrument: none`, and the judge works from her
demonstration alone. A declared instrument that cannot be run (not a `.py`, not inside the seal,
not in its copy, more than one line) is recorded with its `error` and shown to the judge as such.

## The judge (order 48b)

Only a break that reproduced **and** is not invalid reaches the judge: a cold `claude -p` session
with the cold flag set and the init-event refusal copied from `../sealgate/review.py` (order 47),
`--max-budget-usd 1.00`, its prompt on stdin from a file (`<run>/judge/prompt.txt`, ≤ 120,000
bytes; each stdout is cut to its last 20,000 bytes). Input: the seal text, her `BREAK.md` and
script (and `evidence/` files), both reproduction stdouts, the instrument of record, and the tamper
report. The question:

> Does this show that the seal AS WRITTEN (pinned files unmodified, checker run cleanly) would PASS
> while its claim is FALSE? A break that depends on changing the seal's files, the interpreter,
> imports or the environment does not count. Answer JSON {"valid": true|false, "why": "..."}.

`valid: true` → `BREAK_CONFIRMED`; `valid: false` → `BREAK_DISPUTED` (`disagreement: true`,
`disagreement_kind: "alice_vs_judge"`); an error, a warm session or no JSON → `FAILED (judge: …)`.
There is no overrule path in code; that is a later order.

## The sandboxes (`sandbox.py`)

The recipe Odin probed live on 2026-10-03 17:06:

| | Alice | reproduction |
|---|---|---|
| `/usr`, `/etc` | read-only | read-only |
| `/home`, `/tmp`, `/run` | empty tmpfs (no `~/helix`, `/mnt`, `/media`, `~/.ssh`) | same |
| scratch | the one writable bind; cwd `alice/`, `seal/` read-only on top | the one writable bind (a fresh `repro<N>/`); cwd `alice/`, the pristine `seal/` read-only on top |
| `~/.gemini` | bound (her login); **`GEMINI.md` and `config/` read-only on top** | absent |
| `~/.local/bin/agy` | read-only (it cannot self-update from inside) | absent |
| session bus | `xdg-dbus-proxy --filter --talk=org.freedesktop.secrets` (keyring only) | absent |
| network | open (she needs Google); `/run/systemd/resolve` bound for DNS | `--unshare-net` |
| environment | `--clearenv`, then HOME, PATH, LANG, DBUS address, XDG_RUNTIME_DIR | `--clearenv`, then HOME, PATH, LANG, PYTHONSAFEPATH, PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE |

Both: `--unshare-pid --die-with-parent`, own process group, SIGKILL at the cap and on exit. The
proxy runs in its own process group and is killed when her sandbox ends. `/var/run/nscd` does not
exist here, and bwrap fails if asked to bind it, so it is not bound.

Probed live for this order (2026-10-03 ~17:36 UTC): agy runs with `GEMINI.md`, `config/` and its own
binary read-only and a cleared environment (`status SUCCESS`, wrote `out.txt` in scratch). Inside
her sandbox, `touch ~/.gemini/GEMINI.md` → `Read-only file system`, and `systemctl --user` /
`busctl --user` → `Transport endpoint is not connected`.

## The ledger

`VETOES.jsonl` beside this file is **append-only and hash-chained**, with the chain code copied from
sealgate's `review.py` (order 47): each line carries `seq` and `prev` = sha256 of the previous line's
raw bytes (genesis = sha256 of nothing). A `veto` line: `seal` (path relative to `~/helix`, or
absolute outside it), `body_sha256` (sealgate's body hash), `file_sha256`, `outcome`, `why`,
`files` (her files → sha256, keyed `alice/…` or `seal/…` under `scratch`), `script`, `repro` (per
run: dir, rc, last line, stdout sha256, seconds), `repro_copied`, `tamper` (per run: file,
reason), `instrument` (`"none"` or path, argv, rc, stdout sha256, last line, stdout file),
`judge` (valid, why, meta: cost and the init record), `judge_prompt_bytes`, `scratch`, `copied`,
`left_out`, `prompt_bytes`, `alice` (model, rc, seconds, reply tail), `reviewer`,
`reviewer_findings`, `reviewer_findings_accepted`, `disagreement`, `disagreement_kind`. Order
48's two lines (version 1.0) lack the order-48b fields; they are read as they are and never
rewritten.

**Known limit:** this directory is **not behind the write wall**, so the chain makes an edit
**visible, not impossible**, and the last line has no successor to witness it. Her files live on
the HDD and are not behind the wall either; P10 re-hashes them, so a change is visible, not
prevented.

## Disagreement (order 47's item 9 leftover)

Two kinds, in `disagreement_kind`. **`alice_vs_judge`**: the outcome is `BREAK_DISPUTED`.
**`alice_vs_reviewer`**: the outcome is `BREAK_CONFIRMED`, the seal's assigned reviewer in
`sealgate/REVIEWS.jsonl` was `Claude-cold`, and none of that reviewer's findings is dispositioned
`ACCEPTED` or `ADDRESSED` in the seal's stamped findings file: Claude saw no fatal flaw, Alice broke
it, and the judge agreed with her. A seal with no stamp has no such disposition, so it counts as none accepted. The dispositions
are read with sealgate's own P1 + P3.

## P10 in sealgate

See `../sealgate/README.md`. In short: a `NO_BREAK`, `BREAK_NOT_REPRODUCED` or `BREAK_INVALID` run
for the seal's **current** `body_sha256` whose files still hash as recorded; no `BREAK_CONFIRMED`,
`BREAK_DISPUTED` or legacy `BREAK_REPRODUCED` for that body (**union rule**); the whole chain
intact. `FAILED` or no run → refused. Editing the seal changes its
body hash and needs a new run, and **so does stamping it** (`stamp` appends a blank line outside the
stamp block), so run Alice on the seal as stamped. There is no overrule path.

## Calibration (the acquittal control)

`calibration/sound/` (a known-true claim, honest controls) must NOT come out a veto;
`calibration/flawed/` (one planted band that cannot fail) MUST. Order 48 ran both live on
2026-10-03 and the sound seal was "broken" by tampering (above). Order 48b added `Instrument:
check.py` to both (the calibration `check.py` is a deterministic run on a synthetic domain, so here
the instrument is the whole check), and re-ran both: **PASS = flawed → `BREAK_CONFIRMED`, sound →
`NO_BREAK`, `BREAK_NOT_REPRODUCED` or `BREAK_INVALID`.** See the order 48b report.

## No flag stands in for Alice

There is no flag that fakes, replaces or passes over Alice or the judge. Tests inject a fake Alice
(`veto.ALICE`) and a fake judge (`veto.JUDGE`) with monkeypatch. The break scripts and instruments
in the tests run in the real reproduction sandbox.

```
python3 -m pytest -q        # test_veto.py + test_sandbox.py
```
