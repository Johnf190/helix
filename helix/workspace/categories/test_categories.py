#!/usr/bin/env python3
"""ORDER 65 Part B — categories.py: the list parser, declared() and primary().

Run:  python3 -m pytest -q test_categories.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import categories as C  # noqa: E402


def test_not_a_stale_module():
    assert Path(C.__file__).resolve() == HERE / "categories.py"


def test_load_good_comment_malformed(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("# a comment\n"
                 "\n"
                 "alpha one | life | #112233 | first\n"
                 "  # indented comment\n"
                 "beta | nowhere | #112233 | bad lobe\n"
                 "gamma | life | blue | bad colour\n"
                 "delta | life | #112233\n"
                 "Alpha  One | helix | #445566 | duplicate, ignoring case and spaces\n"
                 "epsilon: two | making | #aabbcc | second\n")
    cats = C.load(p)
    assert [c.name for c in cats] == ["alpha one", "epsilon: two"]
    assert cats[0] == C.Category("alpha one", "life", "#112233", "first")
    assert [e[0] for e in C.load_errors] == [5, 6, 7, 8]
    assert "lobe" in C.load_errors[0][2] and "colour" in C.load_errors[1][2]
    assert "4 fields" in C.load_errors[2][2] and "duplicate" in C.load_errors[3][2]


def test_load_missing_file(tmp_path):
    assert C.load(tmp_path / "nope.txt") == []
    assert len(C.load_errors) == 1 and "unreadable" in C.load_errors[0][2]


def test_real_list_loads_clean():
    cats = C.load()
    assert C.load_errors == []
    # the list grows without a build order (180_*); a name is never deleted
    names = [c.name for c in cats]
    assert len(cats) >= 16
    assert set(ORIGINAL_16) <= set(names)
    assert len(names) == len(set(names))
    assert {c.lobe for c in cats} <= set(C.LOBES)


ORIGINAL_16 = ["personal", "household finance", "rules", "helix", "research: math",
               "research: probe-study", "research: attest", "research: framework",
               "research: finance", "research: neural", "project: boats", "project: mapapp",
               "project: friend", "project: music", "project: musicapp", "project: sidekick"]


CATS = [C.Category("project: boats", "making", "#e07a5f", ""),
        C.Category("rules", "helix", "#c9b8ff", ""),
        C.Category("research: math", "research", "#eab308", "")]


def test_declared_none():
    assert C.declared("# title\nno category here\n", CATS) == (None, None)
    assert C.declared("", CATS) == (None, None)
    assert C.declared("category: rules\n", CATS) == (None, None)       # key is case-sensitive


def test_declared_one():
    assert C.declared("# t\nCategory: project: boats\n", CATS) == ("project: boats", None)
    assert C.declared("Category:   Project:  Boats  \n", CATS) == ("project: boats", None)


def test_declared_two():
    for line in ("Category: project: boats · rules", "Category: project: boats, rules"):
        assert C.declared(line, CATS) == ("project: boats", None)
        assert C.declared_all(line, CATS) == (["project: boats", "rules"], None)


def test_declared_too_many():
    name, problem = C.declared("Category: rules, project: boats, research: math", CATS)
    assert name is None and problem.startswith("too many")


def test_declared_unknown():
    assert C.declared("Category: wizardry\n", CATS) == (None, "unknown: wizardry")
    assert C.declared("Category: rules · wizardry\n", CATS) == (None, "unknown: wizardry")


def test_declared_line_20_ignored():
    text = "".join(f"line {i}\n" for i in range(1, 20)) + "Category: rules\n"
    assert text.splitlines()[19] == "Category: rules"
    assert C.declared(text, CATS) == (None, None)
    text15 = "".join(f"line {i}\n" for i in range(1, 15)) + "Category: rules\n"
    assert C.declared(text15, CATS) == ("rules", None)


def test_primary_never_raises(tmp_path):
    assert C.primary(tmp_path / "missing.md", CATS) is None
    assert C.primary(str(tmp_path / "missing.md"), CATS) is None
    assert C.primary(None, CATS) is None
    assert C.primary(12345, CATS) is None
    f = tmp_path / "10_x.md"
    f.write_text("# x\nCategory: rules · project: boats\n")
    assert C.primary(f, CATS) == "rules"
    assert C.primary(str(f), CATS) == "rules"
    assert C.primary("# x\nCategory: research: math\n", CATS) == "research: math"


# ── Part D: propose_backfill ───────────────────────────────────────────────────────────────
import propose_backfill as PB  # noqa: E402
import pytest  # noqa: E402

CONTROL = {
    "110_example_68cd.md": ("# BOAT — THE PRINTED SKELETON IS BUILT. Reads with `76_*`.\n", "project: boats"),
    "176_example_3bb9.md": ("# THE HOUSEHOLD BUDGET SHEET — filed. Reads with `103_*`, `175_*`.\n", "household finance"),
    "99_example_754b.md": ("# ODIN / COUNCIL OPERATING DISCIPLINE — set by the user. STANDING RULES.\n", "rules"),
}


def facts_tree(d):
    d.mkdir(parents=True)
    for name, (text, _) in CONTROL.items():
        (d / name).write_text(text)
    (d / "76_example_8c8f.md").write_text("# Boat boat project\n")
    (d / "50_unclear.md").write_text("# Something\ncites 76_boat and 99_odin\n")
    (d / "60_declares.md").write_text("# x\nCategory: personal · project: boats\n")
    (d / "SECURITY_credentials.md").write_text("must never be opened")
    (d / "notes.txt").write_text("not a fact")
    return d


def test_backfill_rows_equal_facts(tmp_path):
    d = facts_tree(tmp_path / "facts")
    out = tmp_path / "out"
    out.mkdir()
    assert PB.main(["--facts", str(d), "--out", str(out)]) == 0
    import csv
    rows = list(csv.DictReader((out / "BACKFILL_PROPOSAL.csv").open()))
    assert len(rows) == len(list(d.glob("*.md"))) == 7
    by = {r["file"]: r for r in rows}
    assert (by["60_declares.md"]["primary"], by["60_declares.md"]["secondary"]) == ("personal", "project: boats")
    assert by["SECURITY_credentials.md"]["confidence"] == "low"
    assert "not opened" in by["SECURITY_credentials.md"]["reason"]
    assert {r["confidence"] for r in rows} <= {"high", "low"}
    md = (out / "BACKFILL_PROPOSAL.md").read_text()
    assert md.index("LOW confidence") < md.index("High confidence")


def test_backfill_positive_control(tmp_path):
    rows = {r["file"]: r for r in PB.propose(facts_tree(tmp_path / "facts"), C.load())}
    for name, (_, want) in CONTROL.items():
        assert rows[name]["primary"] == want, (name, rows[name])


def test_backfill_uses_citations(tmp_path):
    rows = {r["file"]: r for r in PB.propose(facts_tree(tmp_path / "facts"), C.load())}
    assert "cites" in rows["50_unclear.md"]["reason"]


def test_guess_alone_matches_propose_for_a_fact_with_no_citations(tmp_path):
    # ORDER 76: /remember calls guess() for one note; propose() is guess() per fact plus citations
    d = facts_tree(tmp_path / "facts")
    rows = {r["file"]: r for r in PB.propose(d, C.load())}
    checked = 0
    for name, row in rows.items():
        text = PB.read_fact(d / name)
        if "cites" in row["reason"]:
            continue
        checked += 1
        why = []
        assert PB.guess(name, text, C.load(), why=why) == (row["primary"], row["secondary"],
                                                           row["confidence"]), name
        assert "; ".join(why) == row["reason"], name
    assert checked >= 3, checked


def test_guess_a_declared_line_wins():
    cats = C.load()
    assert PB.guess("boat_frames.md", "# x\nCategory: rules\n", cats) == ("rules", "", "high")


def test_guess_never_says_high_for_an_unopened_file():
    assert PB.guess("boat_frames.md", None, C.load())[2] == "low"


def test_credentials_file_never_opened(tmp_path, monkeypatch):
    d = facts_tree(tmp_path / "facts")
    opened = []
    real = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: opened.append(str(f)) or real(f, *a, **k))
    PB.propose(d, C.load())
    assert opened and not any("credentials" in Path(f).name for f in opened)


def test_facts_opened_read_only(tmp_path, monkeypatch):
    d = facts_tree(tmp_path / "facts")
    modes = []
    real = open
    monkeypatch.setattr("builtins.open", lambda f, mode="r", *a, **k: modes.append(mode) or real(f, mode, *a, **k))
    PB.propose(d, C.load())
    assert modes and set(modes) == {"r"}


def test_output_under_memory_refused(tmp_path):
    d = facts_tree(tmp_path / "facts")
    bad = tmp_path / "memory" / "facts"
    bad.mkdir(parents=True)
    with pytest.raises(PermissionError):
        PB.main(["--facts", str(d), "--out", str(bad)])
    with pytest.raises(PermissionError):
        PB.write_out([], tmp_path / "memory", C.load())
    assert list(bad.iterdir()) == []


def test_no_category_name_in_python():
    # CATEGORIES.txt is the only source of names: none appears as a string literal in the code.
    # Lobe names are exempt: "helix" is both mindgraph's lobe and a category (and a description word).
    import ast
    names = {c.name for c in C.load()} - set(C.LOBES)
    for f in ("categories.py", "propose_backfill.py"):
        lits = {n.value for n in ast.walk(ast.parse((HERE / f).read_text()))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not (lits & names), (f, lits & names)


# ── ORDER 73b Part A: ASSIGNED.csv (the sidecar), assign, category_of, census ─────────────────
# Every path here is under tmp_path: no test reads or writes the live ASSIGNED / OVERRIDES /
# proposal / facts (test_fact_categories.py checks that no test run opens the live ASSIGNED.csv).
import csv as _csv  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_sidecar(tmp_path, monkeypatch):
    side = tmp_path / "_sidecar"
    monkeypatch.setattr(C, "ASSIGNED", side / "ASSIGNED.csv")
    monkeypatch.setattr(C, "OVERRIDES", side / "OVERRIDES.csv")
    monkeypatch.setattr(C, "PROPOSAL", side / "BACKFILL_PROPOSAL.csv")
    monkeypatch.setattr(C, "FACTS", side / "facts")


def _csv_write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def assign_tree(tmp_path, overrides=True):
    facts = tmp_path / "facts"
    facts.mkdir()
    texts = {
        "10_decl.md": "# d\nCategory: rules\n",                     # valid declared → skipped
        "11_over.md": "# o\n",                                      # low + override → override
        "12_high.md": "# h\n",                                      # high → proposal
        "13_low.md": "# l\n",                                       # low, no override → needs the user
        "14_unknown.md": "# u\n",                                   # high, unknown name → unknown
        "15_bad.md": "# b\nCategory: wizardry\n",                   # bad declared → falls through
        "16_both.md": "# both\n",                                   # high AND override → override
        "17_ovunk.md": "# ou\n",                                    # override with unknown name
    }
    for k, v in texts.items():
        (facts / k).write_text(v)
    prop = _csv_write(tmp_path / "BACKFILL_PROPOSAL.csv",
                      ["file", "primary", "secondary", "reason", "confidence"], [
                          ["10_decl.md", "rules", "", "already declares Category:", "high"],
                          ["11_over.md", "research: math", "", "x", "low"],
                          ["12_high.md", "project: boats", "rules", "x", "high"],
                          ["13_low.md", "research: math", "", "x", "low"],
                          ["14_unknown.md", "wizardry", "", "x", "high"],
                          ["15_bad.md", "Rules", "", "x", "high"],
                          ["16_both.md", "research: math", "", "x", "high"],
                          ["17_ovunk.md", "rules", "", "x", "high"],
                      ])
    over = tmp_path / "OVERRIDES.csv"
    if overrides:
        _csv_write(over, ["file", "primary", "secondary", "why"], [
            ["11_over.md", "project: boats", "", "the user said"],
            ["16_both.md", "rules", "research: math", "the user said"],
            ["17_ovunk.md", "rules", "sorcery", "typo"],
        ])
    return facts, prop, over, tmp_path / "out" / "ASSIGNED.csv"


def _read_assigned(p):
    return {r["file"]: (r["primary"], r["secondary"], r["source"]) for r in _csv.DictReader(p.open())}


def test_assign_override_high_low_unknown(tmp_path, capsys):
    facts, prop, over, out = assign_tree(tmp_path)
    out.parent.mkdir()
    res = C.assign(facts=facts, proposal=prop, overrides=over, out=out, cats=CATS)
    assert _read_assigned(out) == {
        "11_over.md": ("project: boats", "", "override"),
        "12_high.md": ("project: boats", "rules", "proposal"),
        "15_bad.md": ("rules", "", "proposal"),                     # canonical name, bad line fell through
        "16_both.md": ("rules", "research: math", "override"),      # override beats a high proposal
    }
    assert res["declared"] == ["10_decl.md"]
    assert res["needs"] == ["13_low.md"]
    assert res["unknown"] == [("wizardry", "14_unknown.md"), ("sorcery", "17_ovunk.md")]
    assert list(out.parent.iterdir()) == [out]                       # temp file moved, none left over
    assert next(_csv.reader(out.open())) == ["file", "primary", "secondary", "source"]


def test_assign_without_overrides_file(tmp_path):
    facts, prop, over, out = assign_tree(tmp_path, overrides=False)
    out.parent.mkdir()
    assert not over.exists()
    res = C.assign(facts=facts, proposal=prop, overrides=over, out=out, cats=CATS)
    got = _read_assigned(out)
    assert set(got) == {"12_high.md", "15_bad.md", "16_both.md", "17_ovunk.md"}
    assert {v[2] for v in got.values()} == {"proposal"}
    assert res["needs"] == ["11_over.md", "13_low.md"]


def test_assign_cli_prints_lists(tmp_path, capsys):
    facts, prop, over, out = assign_tree(tmp_path)
    out.parent.mkdir()
    assert C.main(["assign", "--facts", str(facts), "--proposal", str(prop), "--overrides", str(over),
                   "--out", str(out)]) == 0
    o = capsys.readouterr().out
    assert "needs the user: 13_low.md" in o
    assert "unknown: wizardry (14_unknown.md)" in o and "unknown: sorcery (17_ovunk.md)" in o
    assert out.exists()


def test_assign_dry_writes_nothing(tmp_path, capsys):
    facts, prop, over, out = assign_tree(tmp_path)
    out.parent.mkdir()
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert C.main(["assign", "--dry", "--facts", str(facts), "--proposal", str(prop),
                   "--overrides", str(over), "--out", str(out)]) == 0
    o = capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.rglob("*")) == before and not out.exists()
    assert "needs the user: 13_low.md" in o and "4 assigned" in o and "nothing written" in o


def test_assign_refuses_output_under_memory(tmp_path):
    facts, prop, over, _ = assign_tree(tmp_path)
    bad = tmp_path / "memory" / "ASSIGNED.csv"
    bad.parent.mkdir()
    with pytest.raises(PermissionError):
        C.assign(facts=facts, proposal=prop, overrides=over, out=bad, cats=CATS)
    assert list(bad.parent.iterdir()) == []


def test_assign_never_opens_a_credentials_file(tmp_path, monkeypatch):
    facts, prop, over, out = assign_tree(tmp_path)
    (facts / "SECURITY_credentials.md").write_text("must never be opened")
    out.parent.mkdir()
    opened = []
    real = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: opened.append(str(f)) or real(f, *a, **k))
    res = C.assign(facts=facts, proposal=prop, overrides=over, out=out, cats=CATS)
    assert opened and not any("credentials" in Path(f).name for f in opened)
    assert "SECURITY_credentials.md" in res["needs"]


def _assigned_file(tmp_path, rows):
    return _csv_write(tmp_path / "A.csv", ["file", "primary", "secondary", "source"], rows)


def test_load_assigned(tmp_path):
    p = _assigned_file(tmp_path, [["1_a.md", "Rules", "", "proposal"],
                                  ["2_b.md", "project: boats", "rules", "override"],
                                  ["3_c.md", "wizardry", "", "proposal"]])        # list changed since
    assert C.load_assigned(p, CATS) == {"1_a.md": ["rules"], "2_b.md": ["project: boats", "rules"]}
    assert C.load_assigned(tmp_path / "missing.csv", CATS) == {}
    assert C.load_assigned(None, CATS) == {}                         # the (patched) default is absent


def test_category_of_declared_beats_assigned():
    a = {"1_a.md": ["project: boats"]}
    assert C.category_of("1_a.md", "# a\nCategory: rules\n", CATS, a) == (["rules"], "declared")
    assert C.category_of("1_a.md", "# a\n", CATS, a) == (["project: boats"], "assigned")


def test_category_of_bad_declared_falls_through_to_assigned():
    a = {"1_a.md": ["rules"]}
    assert C.category_of("1_a.md", "# a\nCategory: wizardry\n", CATS, a) == (["rules"], "assigned")
    assert C.category_of("2_b.md", "# b\nCategory: wizardry\n", CATS, a) == ([], None)


def test_category_of_assigned_absent(tmp_path):
    assert not C.ASSIGNED.exists()
    assert C.category_of("1_a.md", "# a\n", CATS) == ([], None)      # default path: absent → nothing
    assert C.category_of("1_a.md", "# a\n", CATS, {}) == ([], None)
    assert C.category_of("1_a.md", None, CATS, {}) == ([], None)


def test_census(tmp_path):
    facts, *_ = assign_tree(tmp_path)
    a = {"12_high.md": ["project: boats"], "15_bad.md": ["rules"]}
    c = C.census(facts, CATS, a)
    assert c["declared"] == ["10_decl.md"]
    assert c["assigned"] == ["12_high.md", "15_bad.md"]
    assert c["none"] == ["11_over.md", "13_low.md", "14_unknown.md", "16_both.md", "17_ovunk.md"]
    assert c["bad"] == [("15_bad.md", "unknown: wizardry")]
