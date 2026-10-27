"""ORDER 81 (item 33) — the per-turn fact index, grouped by category.

Run with the venv (app imports flask):  .venv/bin/python -m pytest -q test_index_by_category.py
Every test points app.FACTS / PINNED_TXT / REGISTRY_JSON / MEMDIGEST_OUT / the category files at tmp_path.

FACT_MODE_BEFORE is app._facts_digest(FIXTURE_BUDGET) on the FIXTURE fact base, captured from the
pre-order code (app.py sha256 63cf70dce1718e6f1b2dc21a83bf2a25ba1c02c3e380516b7f7b3578d822197f, kept as
app.py.bak.20261026_212635Z) by running this file as a script:  .venv/bin/python test_index_by_category.py
"""
import os
import sys
import time

import pytest

os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
os.environ.setdefault("HELIX_NO_SCHEDULER", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

DAY = 86400
FIXTURE_BUDGET = 400
CATEGORIES = ("helix | helix | #a98bff | Helix builds, upgrades, orders, infra, machines\n"
              "rules | helix | #c9b8ff | the user's standing rules and preferences for Odin\n"
              "research: attest | research | #4be3a0 | ATTEST network-security arc\n"
              "project: boats | making | #e07a5f | boat, dinghy\n")
ASSIGNED = "file,primary,secondary,source\n35_assigned.md,research: attest,,override\n"
CLOSED = "research: attest | CLOSED 2026-07-29, the user stepped away — 65_*\n"
# (name, age in days, text). Ages are relative to now, so the fact-mode output depends only on their order.
FIXTURE = [
    ("10_old.md", 60, "# 🔴 SUPERSEDED by `20_new.md` — was wrong\nCategory: helix\nold body.\n"),
    ("20_new.md", 40, "# the current one\nCategory: helix\nnew body.\n"),
    ("30_corrected.md", 50, "# a claim that held\nCategory: research: attest\n" + "c" * 150 + "\n⚠️ UPDATE (2026-07-15): it did not.\n"),
    ("35_assigned.md", 55, "# assigned, not declared\nno category line here.\n"),
    ("40_pin_big.md", 70, "# a big standing rule\nCategory: rules\n" + "p" * 520 + "\n"),
    ("41_pin_small.md", 80, "# a small standing rule\nCategory: rules\nshort.\n"),
    ("50_interim.md", 30, "# an interim account\nCategory: project: boats\ncalm.\n"),
    ("51_verdict.md", 31, "# decisive and proven\nCategory: project: boats\n" + "w" * 250 + "\n"),
    ("60_uncat.md", 45, "# nobody filed this\n" + "u" * 200 + "\n"),
    ("70_recent.md", 1, "# changed yesterday\nCategory: helix\n" + "r" * 300 + "\n"),
    ("80_newest.md", 0.01, "# the newest of all\nCategory: helix\nfresh.\n"),
]
PINS = ("40_pin_big.md", "41_pin_small.md")
INTERIM = {"50_interim.md": [], "51_verdict.md": ["decisive", "proven"]}


def make_world(root, monkeypatch=None, now=None):
    """The FIXTURE fact base and its sources under `root`; app pointed at them."""
    now = time.time() if now is None else now
    facts = os.path.join(root, "facts")
    os.makedirs(facts, exist_ok=True)
    for name, age, text in FIXTURE:
        p = os.path.join(facts, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.utime(p, (now - age * DAY, now - age * DAY))
    paths = {k: os.path.join(root, k) for k in ("PINNED.txt", "registry.json", "CATEGORIES.txt",
                                                  "ASSIGNED.csv", "CLOSED.txt", "out")}
    for k, body in (("PINNED.txt", "\n".join(PINS) + "\n"), ("registry.json", "{}"),
                    ("CATEGORIES.txt", CATEGORIES), ("ASSIGNED.csv", ASSIGNED), ("CLOSED.txt", CLOSED)):
        with open(paths[k], "w", encoding="utf-8") as fh:
            fh.write(body)
    sets = {"FACTS": facts, "PINNED_TXT": paths["PINNED.txt"], "REGISTRY_JSON": paths["registry.json"],
            "_interim_names": lambda *a, **k: dict(INTERIM)}
    if hasattr(app, "MEMDIGEST_OUT"):                     # absent in the pre-order code (the capture)
        sets.update(MEMDIGEST_OUT=paths["out"], MEMDIGEST_CATEGORIES=paths["CATEGORIES.txt"],
                    MEMDIGEST_ASSIGNED=paths["ASSIGNED.csv"], MEMDIGEST_CLOSED=paths["CLOSED.txt"])
    for k, v in sets.items():
        if monkeypatch is None:
            setattr(app, k, v)
        else:
            monkeypatch.setattr(app, k, v)
    return paths


# the capture, with the tmp facts dir written as {FACTS}
FACT_MODE_BEFORE = "# 20_new.md\n# the current one\nCategory: helix\nnew body.\n\n# 35_assigned.md\n# assigned, not declared\nno category line here.\n\n# 41_pin_small.md\n# a small standing rule\nCategory: rules\nshort.\n\n# 50_interim.md  🟡 INTERIM — Odin's account, no witness yet\n# an interim account\nCategory: project: boats\ncalm.\n\n# 80_newest.md\n# the newest of all\nCategory: helix\nfresh.\n\n=== OLDER FACTS (index — open any of these in full with your file tools; they live in {FACTS}) ===\n- memory/facts/10_old.md — 🔴 SUPERSEDED by `20_new.md` — was wrong  → SUPERSEDED by 20_new.md, read that\n- memory/facts/30_corrected.md — a claim that held  ⚠ LATER CORRECTION INSIDE — open it, do not cite this line\n- memory/facts/40_pin_big.md — a big standing rule  📌 PINNED — over the pin cap, open it\n- memory/facts/51_verdict.md — decisive and proven  🟡 INTERIM — verdict words unwitnessed\n- memory/facts/60_uncat.md — nobody filed this\n- memory/facts/70_recent.md — changed yesterday"



# ---------------------------------------------------------------------- helpers
@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    paths = make_world(str(tmp_path), monkeypatch)
    paths["facts"] = app.FACTS
    capsys.readouterr()
    return paths


def digest(mode, monkeypatch, budget=FIXTURE_BUDGET):
    monkeypatch.setattr(app, "INDEX_MODE", mode)
    return app._facts_digest(budget)


def own_lines(d):
    return [ln for ln in d.splitlines() if ln.startswith("- memory/facts/")]


def own(d, fn):
    return [ln for ln in own_lines(d) if ln.startswith(f"- memory/facts/{fn} — ")]


# ---------------------------------------------------------------------- fact mode
def test_fact_mode_is_byte_identical_to_the_pre_order_capture(world, monkeypatch):
    assert digest("fact", monkeypatch) == FACT_MODE_BEFORE.replace("{FACTS}", world["facts"])
    assert not os.path.exists(world["out"])                       # fact mode writes no digest


def test_the_mode_comes_from_the_environment_and_is_one_of_two():
    assert app.INDEX_MODE == os.environ.get("HELIX_INDEX_MODE", app.INDEX_MODE)
    assert app.INDEX_MODE in ("fact", "category") and app.RECENT_INDEX_DAYS == 7


def test_memdigest_first_line_is_apps(world):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_t81_md", app.MEMDIGEST_PY)
    md = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(md)
    for t in ("# a\nb", "\n\n  ## x y ##  \nz", "", "   \n#\n", "#" * 3 + " " + "q" * 300, "plain"):
        assert md.first_line(t) == app._first_line(t)


# ---------------------------------------------------------------------- category mode
def test_category_mode_block_layout(world, monkeypatch):
    d = digest("category", monkeypatch)
    fact = digest("fact", monkeypatch)
    inline = fact.split("\n\n=== OLDER FACTS")[0]
    assert d.startswith(inline + "\n\n=== FACT INDEX BY CATEGORY (each line = one category; open its digest for "
                        'the per-fact list, or run python3 ~/helix/workspace/memretrieve/memretrieve.py "<words>") ===\n')
    assert "=== OLDER FACTS" not in d
    cats = d.split("=== FACT INDEX BY CATEGORY")[1].split("\n\n=== RECENT AND FLAGGED FACTS (own line) ===\n")[0]
    lines = cats.splitlines()[1:]
    out = world["out"]
    date = lambda age: time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(f"{world['facts']}/{age}")))
    assert lines == [
        f"- helix — 4 facts, newest {date('80_newest.md')} — Helix builds, upgrades, orders, infra, machines → {out}/helix.md",
        f"- project: boats — 2 facts, newest {date('50_interim.md')} — boat, dinghy → {out}/project__boats.md",
        f"- CLOSED — CLOSED 2026-07-29, the user stepped away — 65_* · research: attest — 2 facts, newest "
        f"{date('30_corrected.md')} — ATTEST network-security arc → {out}/research__attest.md",
        f"- rules — 2 facts, newest {date('40_pin_big.md')} — the user's standing rules and preferences for Odin → {out}/rules.md",
        f"- uncategorised — 1 facts, newest {date('60_uncat.md')} — no category declared or assigned; each one also "
        f"has its own line → {out}/uncategorised.md"]


def test_category_mode_own_lines_only_for_recent_pins_over_the_cap_and_uncategorised(world, monkeypatch):
    fact = digest("fact", monkeypatch)
    d = digest("category", monkeypatch)
    assert not own(d, "30_corrected.md")                          # old, categorised: in its digest only
    assert not own(d, "10_old.md") and not own(d, "51_verdict.md")
    assert own(d, "70_recent.md") == own(fact, "70_recent.md")    # 1 day old
    assert own(d, "40_pin_big.md") == own(fact, "40_pin_big.md")  # a pin over the cap, 📌
    assert app._PINNED_FLAG in own(d, "40_pin_big.md")[0]
    assert own(d, "60_uncat.md") == own(fact, "60_uncat.md")      # no category
    assert len(own_lines(d)) == 3
    assert d.split("\n\n=== RECENT AND FLAGGED FACTS (own line) ===\n")[1].splitlines() == \
        own(fact, "40_pin_big.md") + own(fact, "60_uncat.md") + own(fact, "70_recent.md")


def test_every_indexed_fact_is_in_exactly_one_digest_on_its_fact_mode_line(world, monkeypatch):
    fact = digest("fact", monkeypatch)
    digest("category", monkeypatch)
    lines = [ln for p in sorted(os.listdir(world["out"])) if p.endswith(".md")
             for ln in open(os.path.join(world["out"], p), encoding="utf-8").read().splitlines()
             if ln.startswith("- memory/facts/")]
    names = [ln.split(" — ")[0][len("- memory/facts/"):] for ln in lines]
    assert sorted(names) == sorted(n for n, _a, _t in FIXTURE)
    for ln in own_lines(fact.split("=== OLDER FACTS")[1]):
        assert lines.count(ln) == 1, ln


def test_a_category_whose_every_fact_is_inline_is_omitted(world, monkeypatch):
    d = digest("category", monkeypatch, budget=10 ** 6)           # all inline but the superseded 10_old
    cats = d.split("=== FACT INDEX BY CATEGORY")[1].splitlines()[1:]
    assert len(cats) == 1 and cats[0].startswith("- helix — 4 facts")
    assert "=== RECENT AND FLAGGED" not in d                       # 10_old is 60 days old and categorised


def test_two_category_builds_are_byte_identical(world, monkeypatch):
    a = digest("category", monkeypatch)
    snap = lambda: {p: open(os.path.join(world["out"], p), "rb").read() for p in sorted(os.listdir(world["out"]))}
    s1 = snap()
    assert digest("category", monkeypatch) == a and snap() == s1


@pytest.mark.parametrize("how", ["missing module", "build raises", "a fact in no digest"])
def test_a_memdigest_failure_falls_back_to_fact_mode_with_one_log_line(world, monkeypatch, capsys, how, tmp_path):
    fact = digest("fact", monkeypatch)
    capsys.readouterr()
    if how == "missing module":
        monkeypatch.setattr(app, "MEMDIGEST_PY", str(tmp_path / "nope.py"))
    else:
        src = open(app.MEMDIGEST_PY, encoding="utf-8").read()
        bad = tmp_path / "memdigest_bad.py"
        tail = ("\ndef build(*a, **k):\n    raise OSError('disk full')\n" if how == "build raises" else
                "\n_real_build = build\ndef build(*a, **k):\n    r = _real_build(*a, **k)\n"
                "    r['helix']['files'] = [f for f in r['helix']['files'] if f != '70_recent.md']\n    return r\n")
        real = os.path.join(app.WORKSPACE, "categories", "categories.py")      # the copy's HERE is tmp
        bad.write_text(src + f"\nCATEGORIES_PY = Path({real!r})\n" + tail)
        monkeypatch.setattr(app, "MEMDIGEST_PY", str(bad))
    assert digest("category", monkeypatch) == fact
    log = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(log) == 1 and log[0].startswith("HELIX: [facts] no category index this time"), log


def test_the_patch_is_unchanged_between_modes(world, monkeypatch, tmp_path):
    sys.path.insert(0, os.path.join(app.WORKSPACE, "memretrieve"))
    from test_memretrieve import Stub
    mr = app._memretrieve()
    ix = mr.Index(facts_dir=world["facts"], cache_dir=str(tmp_path / "cache"), embedder=Stub())
    monkeypatch.setattr(mr, "_DEFAULT", ix)
    monkeypatch.setattr(mr, "FLOORS", {"bm25": 0.5, "embed": 0.5})
    monkeypatch.setenv("HELIX_MEMORY_PATCH", "1")
    monkeypatch.setattr(app, "PATCHES_LEDGER", str(tmp_path / "PATCHES.jsonl"))
    monkeypatch.setattr(app, "HISTORY", [])
    patches = {}
    for mode in ("fact", "category", "fact"):
        digest(mode, monkeypatch)
        patches.setdefault(mode, []).append(app._memory_patch(app.Lane(f"t81-{mode}"), "a big standing rule pins"))
    assert patches["fact"][0] and "40_pin_big.md" in patches["fact"][0]
    assert patches["fact"][0] == patches["category"][0] == patches["fact"][1]


if __name__ == "__main__":                                # the capture, run once against the pre-order code
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        make_world(d)
        print(repr(app._facts_digest(FIXTURE_BUDGET)))
