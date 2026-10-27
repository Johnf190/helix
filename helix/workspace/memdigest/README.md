# memdigest — the fact index, one file per category (ORDER 81, item 33)

Every turn Odin's memory used to end with an OLDER FACTS index: one line per fact not inlined, growing
~10 000 chars a month with no cap. In `category` mode (`app.INDEX_MODE`, env `HELIX_INDEX_MODE`) app.py
prints one line per category instead and points at that category's digest here:

    - <category> — <n> facts, newest <YYYY-MM-DD> — <description> → workspace/memdigest/out/<slug>.md

Facts changed in the last 7 days (`RECENT_INDEX_DAYS`), pins over the cap and facts with no category keep
their own line under `=== RECENT AND FLAGGED FACTS (own line) ===`.

## Drift-free by construction
- A digest is **mechanical**: each member fact's own first line plus the flags app passes in (SUPERSEDED
  pointer, correction flag, INTERIM), newest first, so a digest line is that fact's old index line byte for
  byte. No model writes a word of it.
- It is **rebuilt from memory/facts at every memory load** (`_facts_digest`), and only rewritten when its
  content changed (tmp file + `os.replace`).
- Its `source:` line is a sha256 over the sorted (filename, sha256 of file bytes) of its members;
  `python3 memdigest.py check` lists any digest that no longer matches the facts (empty = in sync).
- A category that disappears has its digest moved to `out/_old/`, never deleted.
- Categories come from `workspace/categories` (declared `Category:` line, else ASSIGNED.csv), loaded by path,
  read-only. If those cannot be read, every fact is `uncategorised` (and so keeps its own line).
- A fact whose name matches the never-open rule (credentials, accounts, .env) is never opened: its digest
  line says so, and its hash is over its size and mtime.

## Files
| file | what |
|---|---|
| `memdigest.py` | `build`, `check`, CLI `python3 memdigest.py build\|check` |
| `CLOSED.txt` | `category \| one-line decision with its fact`; such a category's index line starts `CLOSED — <decision>` |
| `out/*.md` | the digests (generated; do not edit) |
| `test_memdigest.py` | `python3 -m pytest -q test_memdigest.py` |

`memdigest.py build` from the command line has no app flags; app's next memory load rebuilds the digests
with them. The measurement and ship verdict are in `workspace/memeval/RESULT_ORDER81.txt`.
