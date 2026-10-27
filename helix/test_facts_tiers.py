"""ORDER 33b — the facts loader's tiers: PINNED.txt first, open-registry facts second, recency last.

Run with the venv (app imports flask):  .venv/bin/python -m pytest -q test_facts_tiers.py
Every test but the `live` ones points app.FACTS / PINNED_TXT / REGISTRY_JSON at tmp_path.
"""
import json
import os
import sys

import pytest

os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
os.environ.setdefault("HELIX_NO_SCHEDULER", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import factgate_hook  # noqa: E402

LIVE_PINS = [
    "001_how_helix_remembers.md", "002_show_before_you_build.md",
]


@pytest.fixture(autouse=True)
def _fact_index_mode(monkeypatch):
    """ORDER 81: these tests read the OLDER FACTS index, which is `fact` mode's."""
    monkeypatch.setattr(app, "INDEX_MODE", "fact")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An empty fact base, PINNED.txt and registry in tmp_path. `add(name, size, age)` writes a fact
    of `size` chars whose mtime is `age` seconds in the past (bigger age = older)."""
    facts = tmp_path / "facts"
    facts.mkdir()
    pinned = tmp_path / "PINNED.txt"
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(app, "FACTS", str(facts))
    monkeypatch.setattr(app, "PINNED_TXT", str(pinned))
    monkeypatch.setattr(app, "REGISTRY_JSON", str(registry))
    monkeypatch.setattr(app, "_interim_names", lambda *a, **k: {})
    now = 1_900_000_000

    class W:
        pass
    w = W()
    w.facts, w.pinned, w.registry = facts, pinned, registry
    pinned.write_text("# no pins\n")                    # both sources present by default:
    registry.write_text("{}")                            # a missing one logs a line

    def add(name, size, age, head="# fact"):
        body = (head + "\n" + "x" * size)[:size] if size > len(head) + 1 else "y" * size
        p = facts / name
        p.write_text(body)
        os.utime(p, (now - age, now - age))
        return name

    def pins(*names, header=True):
        pinned.write_text(("# header comment\n\n" if header else "") + "\n".join(names) + "\n")

    def reg(entries):
        registry.write_text(json.dumps(entries))

    w.add, w.pins, w.reg = add, pins, reg
    return w


def names(pairs):
    return sorted(p[0] for p in pairs)


def headed(digest, all_names):
    """The facts the digest inlines: those with a `# name` header line (INTERIM header allowed)."""
    lines = set(digest.split("=== OLDER FACTS")[0].splitlines())
    return sorted(fn for fn in all_names if fn.endswith(".md") and (
        f"# {fn}" in lines or f"# {fn}{app._INTERIM_HEADER}" in lines))


# ---------------------------------------------------------------------- tier order

def test_tier_order_pins_then_open_registry_then_recency(world):
    world.add("01_old_rule.md", 1000, age=9000)       # oldest, pinned (cap 1500)
    world.add("02_open_ref.md", 500, age=8000)        # named by an open entry (cap 600)
    world.add("03_new.md", 1000, age=10)
    world.add("04_newer.md", 1000, age=5)
    world.pins("01_old_rule.md")
    world.reg({"0000": {"status": "open", "claim": "see 02_open_ref.md"}})
    inline, index, tiers = app._select_inline(3000)
    assert names(inline) == ["01_old_rule.md", "02_open_ref.md", "04_newer.md"]
    assert names(index) == ["03_new.md"]
    assert tiers == {"pinned": 1000, "open": 500, "recent": 1000}


def test_inside_a_tier_newest_first(world):
    world.add("10_a.md", 1000, age=300)
    world.add("11_b.md", 1000, age=100)
    world.add("12_c.md", 1000, age=200)
    world.pins("10_a.md", "11_b.md", "12_c.md")
    inline, index, tiers = app._select_inline(4000)    # tier-1 cap 2000: room for two pins
    assert names(inline) == ["11_b.md", "12_c.md"]
    assert [(fn, pinned) for fn, _t, pinned in index] == [("10_a.md", True)]


# ---------------------------------------------------------------------- caps

def test_pin_cap_is_half_the_budget_and_overflow_is_flagged_not_recency(world):
    for k, age in ((1, 50), (2, 40), (3, 30)):
        world.add(f"2{k}_rule.md", 2000, age=age)
    world.add("30_recent.md", 500, age=1)
    world.pins("21_rule.md", "22_rule.md", "23_rule.md")
    inline, index, tiers = app._select_inline(10000)   # cap 5000 -> two pins of 2000
    assert app.PIN_SHARE == 0.5
    assert tiers["pinned"] == 4000
    assert "21_rule.md" not in names(inline)            # the oldest pin overflowed …
    assert ("21_rule.md", True) in [(fn, p) for fn, _t, p in index]   # … to the flagged index
    assert "30_recent.md" in names(inline)
    # overflow pins do NOT compete in tier 3, even though 6,000 chars of budget are left
    assert tiers["recent"] == 500


def test_open_tier_capped_at_twenty_percent_overflow_falls_to_recency(world):
    world.add("40_a.md", 1500, age=500)
    world.add("41_b.md", 1500, age=400)
    world.reg({"0000": {"status": "open", "x": "40_ and 41_b.md"}})
    inline, index, tiers = app._select_inline(10000)   # tier-2 cap 2000 -> one of them
    assert app.OPEN_SHARE == 0.2
    assert tiers["open"] == 1500
    assert names(inline) == ["40_a.md", "41_b.md"]       # the other one made it on recency
    assert tiers["recent"] == 1500


# ---------------------------------------------------------------------- superseded pins

def test_superseded_pin_drops_to_tier_three(world):
    world.add("50_rule.md", 1000, age=9000, head="# 🔴 SUPERSEDED by 51_rule.md — was wrong")
    world.add("52_new.md", 1000, age=1)
    world.pins("50_rule.md")
    inline, index, tiers = app._select_inline(1000)
    assert tiers["pinned"] == 0
    assert names(inline) == ["52_new.md"]
    assert [(fn, p) for fn, _t, p in index] == [("50_rule.md", False)]   # not flagged: no longer a pin


def test_pin_with_a_correction_but_no_superseded_heading_stays_pinned(world):
    world.add("53_rule.md", 1000, age=9000, head="# STANDING RULE\n⚠️ UPDATE (2026-07-15) was wrong")
    world.pins("53_rule.md")
    inline, _index, tiers = app._select_inline(4000)
    assert tiers["pinned"] == 1000 and names(inline) == ["53_rule.md"]


# ---------------------------------------------------------------------- PINNED.txt and registry reads

def test_pinned_txt_comments_blanks_and_unknown_names(world, capsys):
    world.add("60_real.md", 100, age=1)
    world.pinned.write_text("# who and when\n\n  60_real.md  \n# 61_commented.md\nno_such_fact.md\n")
    assert app._pinned_names() == {"60_real.md", "no_such_fact.md"}
    inline, _index, tiers = app._select_inline(1000)
    assert tiers["pinned"] == 100
    assert capsys.readouterr().out == ""                 # an unknown name is skipped quietly


def test_registry_open_reference_by_name_and_by_prefix(world):
    world.add("70_named.md", 100, age=900)
    world.add("71_prefixed.md", 100, age=900)
    world.add("72_closed.md", 100, age=900)
    world.add("box2_71_not_prefix.md", 100, age=900)
    world.reg({"0000": {"status": "open", "a": "70_named.md", "b": ["see 71_*"]},
               "0001": {"status": "PASSED its band", "c": "72_closed.md"},
               "0002": {"status": "NOT SEALED", "c": "72_"}})
    got = app._open_registry_names(["70_named.md", "71_prefixed.md", "72_closed.md",
                                    "box2_71_not_prefix.md"])
    assert got == {"70_named.md", "71_prefixed.md"}


@pytest.mark.parametrize("what", ["pinned", "registry", "both"])
def test_missing_sources_are_an_empty_tier_one_log_line_and_no_crash(world, capsys, what):
    for k in range(5):
        world.add(f"8{k}_f.md", 1000, age=k)
    world.pins("84_f.md")
    world.reg({})
    if what in ("pinned", "both"):
        os.unlink(world.pinned)
    if what == "registry":
        world.registry.write_text("{ not json")
    if what == "both":
        os.unlink(world.registry)
    inline, index, tiers = app._select_inline(3000)
    lines = [l for l in capsys.readouterr().out.splitlines() if l]
    assert len(lines) == (2 if what == "both" else 1)
    assert tiers["open"] == 0
    if what != "registry":
        assert "PINNED" in lines[0] and tiers["pinned"] == 0
        # no pins at all -> exactly the pre-33b behaviour: newest by mtime
        assert names(inline) == ["80_f.md", "81_f.md", "82_f.md"]
    else:
        assert "registry" in lines[0] and names(inline) == ["80_f.md", "81_f.md", "84_f.md"]


def test_unreadable_pinned_txt_logs_once(world, capsys):
    world.add("90_f.md", 100, age=1)
    world.pinned.unlink()
    world.pinned.mkdir()                                 # a directory: open() raises
    inline, _i, tiers = app._select_inline(1000)
    assert names(inline) == ["90_f.md"] and tiers["pinned"] == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


# ---------------------------------------------------------------------- one selection, two readers

def test_digest_and_mem_state_agree_on_a_tiered_world(world):
    for k in range(8):
        world.add(f"1{k}_f.md", 700 + 50 * k, age=100 * k)
    world.pins("17_f.md", "16_f.md", "15_f.md")
    world.reg({"0000": {"status": "open", "x": "14_"}})
    digest = app._facts_digest(3000)
    state = app._mem_state(3000)
    assert headed(digest, os.listdir(app.FACTS)) == state["names"]
    inline, _index, tiers = app._select_inline(3000)
    assert state["names"] == names(inline)
    assert state["budget_used"] == sum(tiers.values())
    assert state["tiers"] == tiers


def test_digest_flags_overflow_pins_in_the_index(world):
    world.add("20_big.md", 3000, age=5)
    world.add("21_small.md", 100, age=1)
    world.pins("20_big.md")
    digest = app._facts_digest(4000)                    # cap 2000: the pin cannot fit
    line = next(l for l in digest.splitlines() if "20_big.md" in l)
    assert line.startswith("- memory/facts/20_big.md") and "📌 PINNED" in line
    assert "📌" not in next(l for l in digest.splitlines() if "21_small.md" in l)


def test_digest_and_mem_state_agree_on_the_live_fact_base():
    digest = app._facts_digest()
    state = app._mem_state()
    assert headed(digest, os.listdir(app.FACTS)) == state["names"]


# ---------------------------------------------------------------------- the live settings

def test_live_budget_and_shares():
    assert app.FACTS_BUDGET == 60000
    assert (app.PIN_SHARE, app.OPEN_SHARE) == (0.5, 0.2)


def test_live_pinned_txt_has_exactly_the_twelve():
    assert app._pinned_names() == set(LIVE_PINS)
    lines = [l.strip() for l in open(app.PINNED_TXT) if l.strip() and not l.lstrip().startswith("#")]
    assert lines == LIVE_PINS


def test_live_all_twelve_pins_are_inline():
    inline, index, tiers = app._select_inline()
    assert set(LIVE_PINS) <= set(names(inline))
    assert tiers["pinned"] <= app.FACTS_BUDGET * app.PIN_SHARE


def test_pinned_txt_is_walled_and_guarded():
    real = os.path.realpath(app.PINNED_TXT)
    assert real in app.WALL_PATHS
    assert real.startswith(os.path.realpath(app.BRAIN + "/memory") + os.sep)
    assert not real.startswith(os.path.realpath(app.BRAIN + "/memory/facts") + os.sep)
    assert factgate_hook._build_path_owns(real)


# ---------------------------------------------------------------------- ORDER 35b: the archive

def _factgate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_t35b_factgate", app.FACTGATE_PY)
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    return fg


def test_35b_the_loader_ignores_memory_archive(world, monkeypatch):
    """An archived fact sits beside memory/facts/, in memory/archive/: neither inlined nor
    indexed, even when PINNED.txt or an open registry entry still names it."""
    world.add("10_live.md", 100, age=10)
    arch = world.facts.parent / "archive"
    arch.mkdir()
    (arch / "20_gone.md").write_text("# archived one\nattest detail\n")
    world.pins("20_gone.md")
    world.reg({"0001": {"status": "open", "facts": ["20_gone.md"]}})
    inline, index, _ = app._select_inline()
    assert names(inline) == ["10_live.md"] and index == []
    monkeypatch.setattr(app, "MEMORY", str(world.facts.parent / "BRAIN.md"))
    assert "20_gone" not in app.load_memory() and "archived one" not in app.load_memory()


@pytest.mark.parametrize("text", [
    "# header\n\n15_a.md\n91_b.md\n",
    "15_a.md\n  # indented comment\n\n\n  91_b.md  \n#99_c.md\n",
    "﻿# BOM first line\n15_a.md\r\n91_b.md\r\n",
    "",
    "# only comments\n# 15_a.md\n",
    "15_a.md\n15_a.md\n with space.md\n",
])
def test_35b_factgate_reads_pinned_txt_exactly_as_the_loader_does(tmp_path, text):
    """factgate must not import app (studio db, scheduler), so it has its own reader. Same files,
    same sets — this is the test that keeps the two rules one rule."""
    p = tmp_path / "PINNED.txt"
    p.write_text(text, encoding="utf-8")
    assert _factgate().pinned_names(p) == app._pinned_names(str(p))


def test_35b_factgate_reads_the_live_pinned_txt_exactly_as_the_loader_does():
    assert _factgate().pinned_names(app.PINNED_TXT) == app._pinned_names() == set(LIVE_PINS)


@pytest.mark.parametrize("reg", [
    {},
    {"1": {"status": "open", "facts": ["10_a.md"]}, "2": {"status": "closed", "note": "20_"}},
    {"1": {"status": "open", "note": "see 20_ and 30_b.md"}, "2": {"status": "OPEN", "x": "10_"}},
    {"1": "not a dict", "2": {"status": "open", "text": "a100_ is not a prefix, 100_ is"}},
])
def test_35b_factgate_reads_open_registry_entries_exactly_as_the_loader_does(tmp_path, reg):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(reg))
    have = ["10_a.md", "20_b.md", "30_b.md", "100_c.md", "110_d.md"]
    assert _factgate().open_registry_names(have, p) == app._open_registry_names(have, str(p))


# ---------------------------------------------------------------------- ORDER 42c: superseded facts

def test_42c_a_whole_superseded_fact_names_its_replacement_in_the_index(world):
    world.add("10_old.md", 300, age=50, head="# 🔴 SUPERSEDED by `20_new.md` — was wrong")
    world.add("20_new.md", 300, age=10, head="# the current one")
    world.add("30_part.md", 300, age=60, head="# part\nits framing was superseded in part by 20_new.md")
    world.add("40_plain.md", 300, age=70, head="# plain")
    index = app._facts_digest(0).split("=== OLDER FACTS")[1]
    line = lambda fn: next(l for l in index.splitlines() if fn in l.split(" — ")[0])
    assert line("10_old.md") == ("- memory/facts/10_old.md — 🔴 SUPERSEDED by `20_new.md` — was wrong"
                                 "  → SUPERSEDED by 20_new.md, read that")
    assert app._CORRECTION_FLAG not in line("10_old.md")
    assert line("30_part.md").endswith(app._CORRECTION_FLAG)              # no heading edge: flag as before
    assert line("20_new.md") == "- memory/facts/20_new.md — the current one"
    assert line("40_plain.md") == "- memory/facts/40_plain.md — plain"


def test_42c_a_chain_in_the_index_names_the_newest(world):
    world.add("1_a.md", 200, age=30, head="# SUPERSEDED by `2_b.md`")
    world.add("2_b.md", 200, age=20, head="# SUPERSEDED by `3_c.md`")
    world.add("3_c.md", 200, age=10, head="# newest")
    index = app._facts_digest(0).split("=== OLDER FACTS")[1]
    assert "- memory/facts/1_a.md — SUPERSEDED by `2_b.md`  → SUPERSEDED by 3_c.md, read that" in index
    assert "- memory/facts/2_b.md — SUPERSEDED by `3_c.md`  → SUPERSEDED by 3_c.md, read that" in index


def test_42c_the_index_is_built_as_before_when_the_map_cannot_be_read(world, monkeypatch):
    world.add("10_old.md", 300, age=50, head="# 🔴 SUPERSEDED by `20_new.md` — was wrong")
    world.add("20_new.md", 300, age=10)
    def broken():
        raise RuntimeError("no retriever")
    monkeypatch.setattr(app, "_memretrieve", broken)
    index = app._facts_digest(0).split("=== OLDER FACTS")[1]
    assert next(l for l in index.splitlines() if "10_old.md" in l).endswith(app._CORRECTION_FLAG)


# ---------------------------------------------------------------------- ORDER 43: superseded facts never inline

def test_43_a_newest_small_superseded_fact_is_not_inline_and_its_index_line_points(world):
    world.add("10_old.md", 200, age=1, head="# 🔴 SUPERSEDED by `20_new.md` — was wrong")   # newest, small
    world.add("20_new.md", 300, age=50, head="# the current one")
    inline, index, _tiers = app._select_inline(3000)
    assert names(inline) == ["20_new.md"]                                 # the replacement is still eligible
    assert [fn for fn, _t, _p in index] == ["10_old.md"]
    digest = app._facts_digest(3000)
    assert headed(digest, os.listdir(app.FACTS)) == ["20_new.md"]
    line = next(l for l in digest.split("=== OLDER FACTS")[1].splitlines() if "10_old.md" in l)
    assert line.endswith("  → SUPERSEDED by 20_new.md, read that")


def test_43_a_pinned_superseded_fact_is_not_inline_either(world):
    world.add("10_old.md", 200, age=1, head="# 🔴 SUPERSEDED by `20_new.md` — was wrong")
    world.add("20_new.md", 300, age=50, head="# the current one")
    world.pins("10_old.md", "20_new.md")
    world.reg({"0000": {"status": "open", "x": "10_"}})                  # and named by an open entry
    inline, index, tiers = app._select_inline(3000)
    assert names(inline) == ["20_new.md"]
    assert [(fn, p) for fn, _t, p in index] == [("10_old.md", False)]     # not a 📌 over-cap pin
    assert tiers == {"pinned": 300, "open": 0, "recent": 0}


def test_43_selection_is_unchanged_when_the_map_cannot_be_read(world, monkeypatch):
    world.add("10_old.md", 200, age=1, head="# 🔴 SUPERSEDED by `20_new.md` — was wrong")
    world.add("20_new.md", 300, age=50, head="# the current one")
    def broken():
        raise RuntimeError("no retriever")
    monkeypatch.setattr(app, "_memretrieve", broken)
    inline, _index, _tiers = app._select_inline(3000)
    assert names(inline) == ["10_old.md", "20_new.md"]                    # fails safe to {}: as before 43


def test_43_mem_state_shows_the_superseded_index_facts(world):
    world.add("1_a.md", 200, age=1, head="# SUPERSEDED by `2_b.md`")
    world.add("2_b.md", 200, age=2, head="# SUPERSEDED by `3_c.md`")
    world.add("3_c.md", 200, age=3, head="# newest")
    world.add("4_d.md", 200, age=4, head="# plain")
    state = app._mem_state(3000)
    whole = app._memretrieve().superseded_by(app.FACTS)
    _inline, index, _tiers = app._select_inline(3000)
    assert state["superseded"] == {fn: whole[fn] for fn, _t, _p in index if fn in whole}
    assert state["superseded"] == {"1_a.md": "3_c.md", "2_b.md": "3_c.md"}
    assert state["names"] == ["3_c.md", "4_d.md"]
    for k in ("count", "loaded", "budget", "budget_used", "indexed", "names", "tiers"):
        assert k in state
    assert (state["count"], state["budget"], state["budget_used"]) == (4, 3000, 400)


# ---------------------------------------------------------------------- ORDER 70: retired passages

_SHARE = ("# finance folder\nThe folder lives on the internal disk. `backup_memory.sh` mirrors it to\n"
          "`//192.168.77.17/odin/box1`, but **only when the user says to back up.** Nothing else copies it.\n")
_PASSAGE = "mirrors it to `//192.168.77.17/odin/box1`, but **only when the user says to back up.**"
_MARK = f'⟦RETIRED by 164_ind.md: "{_PASSAGE[:60]}…"⟧'


def _retire_world(world, age_a=10, age_b=5):
    world.add("103_fin.md", 1, age=age_a)
    (world.facts / "103_fin.md").write_text(_SHARE)
    world.add("164_ind.md", 1, age=age_b)
    (world.facts / "164_ind.md").write_text(f"# independent\nRETIRES: `103_*` \"{_PASSAGE}\"\n")
    now = 1_900_000_000
    for fn, age in (("103_fin.md", age_a), ("164_ind.md", age_b)):
        os.utime(world.facts / fn, (now - age, now - age))


def test_70_the_digest_shows_the_marker_for_an_inlined_a_and_never_edits_it(world):
    _retire_world(world)
    digest = app._facts_digest(10 ** 6)
    a = digest.split("# 103_fin.md\n", 1)[1].split("\n\n# 164_ind.md", 1)[0]
    assert a == _SHARE.strip().replace(_PASSAGE.replace("to `", "to\n`"), _MARK)
    assert "only when the user says to back up.** Nothing" not in digest
    assert (world.facts / "103_fin.md").read_text() == _SHARE


def test_70_the_inline_budget_counts_the_shown_text(world):
    _retire_world(world)
    shown = _SHARE.strip().replace(_PASSAGE.replace("to `", "to\n`"), _MARK)
    b_len = len((world.facts / "164_ind.md").read_text().strip())
    assert len(shown) != len(_SHARE.strip())
    inline, _index, tiers = app._select_inline(10 ** 6)
    assert dict(inline)["103_fin.md"] == shown
    assert tiers["recent"] == len(shown) + b_len
    room = len(shown) + b_len                            # exactly enough for both, as shown
    assert names(app._select_inline(room)[0]) == ["103_fin.md", "164_ind.md"]
    assert len(_SHARE.strip()) + b_len <= room - 1        # the raw text would still fit one char less…
    assert names(app._select_inline(room - 1)[0]) == ["164_ind.md"]        # …the shown text does not


def test_70_with_no_retirer_the_selection_is_byte_identical(world):
    world.add("103_fin.md", 1, age=10)
    (world.facts / "103_fin.md").write_text(_SHARE)
    inline, _index, _tiers = app._select_inline(10 ** 6)
    assert dict(inline)["103_fin.md"] == _SHARE.strip()


def test_70_mem_state_has_the_retired_keys(world):
    _retire_world(world)
    world.add("170_bad.md", 1, age=1)
    (world.facts / "170_bad.md").write_text("# bad\nRETIRES: `103_*` \"this text is not in the old fact\"\n")
    state = app._mem_state(10 ** 6)
    assert state["retired"] == {"103_fin.md": ["164_ind.md"]}
    assert state["retired_unresolved"] == [{"b": "170_bad.md", "line": 2, "why": "0 matches in 103_fin.md"}]


def test_70_mem_state_retired_keys_are_empty_when_the_map_cannot_be_read(world, monkeypatch):
    _retire_world(world)
    def broken():
        raise RuntimeError("no retriever")
    monkeypatch.setattr(app, "_memretrieve", broken)
    state = app._mem_state(10 ** 6)
    assert state["retired"] == {} and state["retired_unresolved"] == []
    assert dict(app._select_inline(10 ** 6)[0])["103_fin.md"] == _SHARE.strip()
