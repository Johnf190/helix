"""ORDER 81 — memdigest: mechanical per-category digests of the facts.

    python3 -m pytest -q test_memdigest.py
Every test builds under tmp_path; the category list, ASSIGNED.csv and CLOSED.txt are tmp copies.
"""
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memdigest as md  # noqa: E402

CATS = ("helix | helix | #a98bff | Helix builds\n"
        "rules | helix | #c9b8ff | standing rules\n"
        "research: attest | research | #4be3a0 | ATTEST arc\n")
NOW = 1_800_000_000


@pytest.fixture
def world(tmp_path, monkeypatch):
    facts, out = tmp_path / "facts", tmp_path / "out"
    facts.mkdir()
    (tmp_path / "CATEGORIES.txt").write_text(CATS)
    (tmp_path / "ASSIGNED.csv").write_text("file,primary,secondary,source\n03_assigned.md,research: attest,,override\n")
    (tmp_path / "CLOSED.txt").write_text("research: attest | CLOSED 2026-07-29 — 65_*\n")
    monkeypatch.setattr(md, "CATEGORIES_TXT", tmp_path / "CATEGORIES.txt")
    monkeypatch.setattr(md, "ASSIGNED_CSV", tmp_path / "ASSIGNED.csv")
    monkeypatch.setattr(md, "CLOSED_TXT", tmp_path / "CLOSED.txt")

    def add(name, text, age):
        p = facts / name
        p.write_text(text)
        os.utime(p, (NOW - age, NOW - age))
    add("01_helix_old.md", "# helix old\nCategory: helix\nbody\n", 300)
    add("02_helix_new.md", "# helix new\nCategory: helix\nbody\n", 100)
    add("03_assigned.md", "# attest, by the sidecar\nno line\n", 200)
    add("04_rule.md", "# a rule\nCategory: rules\n", 400)
    add("05_none.md", "# nobody filed this\n", 500)

    class W:
        pass
    w = W()
    w.facts, w.out, w.root, w.add = facts, out, tmp_path, add
    return w


def flags(fn, text):
    return f"  [{fn}:{len(text or '')}]"


def digest_lines(path):
    return [ln for ln in Path(path).read_text().splitlines() if ln.startswith("- memory/facts/")]


def snapshot(out):
    return {p.name: p.read_bytes() for p in sorted(Path(out).glob("*.md"))}


def test_one_file_per_category(world):
    res = md.build(world.facts, world.out, flags)
    assert sorted(res) == ["helix", "research: attest", "rules", "uncategorised"]
    assert sorted(snapshot(world.out)) == ["helix.md", "research__attest.md", "rules.md", "uncategorised.md"]
    assert res["helix"]["count"] == 2 and res["helix"]["files"] == ["02_helix_new.md", "01_helix_old.md"]
    assert res["helix"]["path"] == str(world.out / "helix.md")
    assert res["helix"]["description"] == "Helix builds"
    assert res["uncategorised"]["description"] == md.UNCATEGORISED_DESC
    assert res["helix"]["newest_date"] == __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(NOW - 100))


def test_every_fact_in_exactly_one_digest(world):
    md.build(world.facts, world.out, flags)
    seen = [re.match(r"- memory/facts/(\S+) — ", ln).group(1)
            for p in world.out.glob("*.md") for ln in digest_lines(p)]
    assert sorted(seen) == sorted(os.listdir(world.facts))


def test_the_line_is_first_line_plus_the_flags_callable_newest_first(world):
    md.build(world.facts, world.out, flags)
    text = (world.facts / "02_helix_new.md").read_text().strip()
    assert digest_lines(world.out / "helix.md") == [
        "- memory/facts/02_helix_new.md — helix new" + flags("02_helix_new.md", text),
        "- memory/facts/01_helix_old.md — helix old"
        + flags("01_helix_old.md", (world.facts / "01_helix_old.md").read_text().strip())]


def test_header_and_closed_line(world):
    res = md.build(world.facts, world.out, flags)
    lines = (world.out / "research__attest.md").read_text().splitlines()
    assert lines[:4] == ["# DIGEST — research: attest", md.GENERATED, f"source: {res['research: attest']['source']}",
                         "closed: CLOSED 2026-07-29 — 65_*"]
    assert res["research: attest"]["closed"] == "CLOSED 2026-07-29 — 65_*"
    assert res["helix"]["closed"] is None
    assert not any(ln.startswith("closed:") for ln in (world.out / "helix.md").read_text().splitlines())


def test_the_source_hash_is_over_sorted_names_and_byte_hashes(world):
    import hashlib
    res = md.build(world.facts, world.out, flags)
    h = hashlib.sha256()
    for fn in ("01_helix_old.md", "02_helix_new.md"):
        h.update(f"{fn}\0{hashlib.sha256((world.facts / fn).read_bytes()).hexdigest()}\n".encode())
    assert res["helix"]["source"] == h.hexdigest()


def test_a_vanished_category_moves_to_old_never_deleted(world):
    md.build(world.facts, world.out, flags)
    before = (world.out / "rules.md").read_bytes()
    (world.facts / "04_rule.md").rename(world.root / "04_rule.md")
    res = md.build(world.facts, world.out, flags)
    assert "rules" not in res and not (world.out / "rules.md").exists()
    assert (world.out / "_old" / "rules.md").read_bytes() == before
    (world.facts / "06_rule.md").write_text("# again\nCategory: rules\n")
    md.build(world.facts, world.out, flags)
    (world.facts / "06_rule.md").unlink()
    md.build(world.facts, world.out, flags)
    assert len(list((world.out / "_old").glob("rules*.md"))) == 2          # the first copy is kept


def test_two_builds_are_byte_identical_and_leave_no_tmp(world):
    a = md.build(world.facts, world.out, flags)
    s1 = snapshot(world.out)
    b = md.build(world.facts, world.out, flags)
    assert a == b and snapshot(world.out) == s1
    assert not list(world.out.glob("*.tmp"))


def test_check_flags_a_changed_member_and_is_empty_after_rebuild(world):
    assert md.check(world.facts, world.out) != []                          # nothing built yet
    md.build(world.facts, world.out, flags)
    assert md.check(world.facts, world.out) == []
    (world.facts / "03_assigned.md").write_text("# attest, edited\n")
    assert md.check(world.facts, world.out) == [str(world.out / "research__attest.md")]
    md.build(world.facts, world.out, flags)
    assert md.check(world.facts, world.out) == []


def test_check_flags_a_digest_for_a_category_that_is_gone(world):
    md.build(world.facts, world.out, flags)
    (world.out / "stray.md").write_text("# DIGEST — stray\n")
    assert md.check(world.facts, world.out) == [str(world.out / "stray.md")]


@pytest.mark.parametrize("missing", ["CATEGORIES.txt", "ASSIGNED.csv", "both", "module"])
def test_missing_category_sources_make_everything_uncategorised_without_raising(world, monkeypatch, capsys, missing):
    if missing in ("CATEGORIES.txt", "both"):
        (world.root / "CATEGORIES.txt").unlink()
    if missing in ("ASSIGNED.csv", "both"):
        (world.root / "ASSIGNED.csv").unlink()
    if missing == "module":
        monkeypatch.setattr(md, "CATEGORIES_PY", world.root / "nope.py")
    res = md.build(world.facts, world.out, flags)
    if missing == "ASSIGNED.csv":                       # the declared lines still count
        assert res["uncategorised"]["files"] == ["03_assigned.md", "05_none.md"]
        assert "helix" in res
    else:
        assert list(res) == ["uncategorised"] and res["uncategorised"]["count"] == 5
        assert capsys.readouterr().out.strip()          # it said why


def test_a_missing_facts_dir_or_closed_file_does_not_raise(world, capsys):
    (world.root / "CLOSED.txt").unlink()
    res = md.build(world.facts, world.out, flags)
    assert res["research: attest"]["closed"] is None
    assert md.build(world.root / "nope", world.root / "out2", flags) == {}
    assert "HELIX: [memdigest]" in capsys.readouterr().out


def test_a_never_open_name_is_not_opened(world, monkeypatch):
    world.add("07_SECURITY_credentials.md", "# SECRET-HEADLINE\nCategory: helix\n", 50)
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self.name) or real(self))
    md.build(world.facts, world.out, flags)
    assert "07_SECURITY_credentials.md" not in opened
    text = (world.out / "uncategorised.md").read_text()
    assert f"- memory/facts/07_SECURITY_credentials.md — {md.NOT_OPENED}" in text
    assert "SECRET-HEADLINE" not in "".join(p.read_text() for p in world.out.glob("*.md"))


def test_the_seeded_closed_file():
    # the kit ships CLOSED.txt with only a comment line: no category is closed yet
    assert md.load_closed(HERE / "CLOSED.txt") == {}


def test_slug():
    assert md.slug("research: attest") == "research__attest"
    assert md.slug("household finance") == "household_finance"
