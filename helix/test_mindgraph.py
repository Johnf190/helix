#!/usr/bin/env python3
"""ORDER 58 Part A — mindgraph.py against synthetic facts whose answer is true by construction,
plus one positive control on the real tree (read-only).

Run:  .venv/bin/python -m pytest -q test_mindgraph.py
"""
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest

import mindgraph

HERE = Path(__file__).resolve().parent


def test_not_a_stale_module():
    # promotion rule 4 (fact 101): refuse to test some other copy of the module
    assert Path(mindgraph.__file__).resolve() == HERE / "mindgraph.py"


# ── the synthetic tree ──────────────────────────────────────────────────────────────────────
# 10_a cites 11_* and 12_b                      → edges 10–11, 10–12
# 11_order_x cites 10_a and itself (11_*)        → 10–11 again (undirected, one edge), no self-edge
# 12_b cites box2 by its prefix 13_ only via box2_13_x? no: it cites 14_ (nothing) → no edge
# box2_10_x cites 12_b                            → edge box2–12 (box2 may cite; it is never a target)
# 12_c collides with 12_b on prefix 12; 12_b wins (first by sort order)
# 15_cites_box2 cites 10_x, which must resolve to 10_a, never to box2_10_x
FACTS = {
    "10_a.md": "# Alpha fact\nsee 11_* and 12_b for detail.\n",
    "11_order_x.md": "## Beta order\nfollows 10_a. also 11_order_x itself.\n",
    "12_b.md": "# Gamma budget\nmentions 14_missing and v1.12_x (decimal, not a ref)\n",
    "12_c.md": "# Delta collides\nno refs here\n",
    "box2_10_x.md": "# Recovered\ncites 12_b\n",
    "15_cites_box2.md": "# Epsilon\ncites 10_x only\n",
}
EXPECTED_EDGES = {("10_a", "11_order_x"), ("10_a", "12_b"), ("12_b", "box2_10_x"),
                  ("10_a", "15_cites_box2")}


def make_tree(d, files=FACTS):
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    (d / "README.txt").write_text("not a fact: 10_a 11_x")   # not *.md → not a node
    return d


@pytest.fixture
def tree(tmp_path):
    mindgraph.clear_cache()
    return make_tree(tmp_path / "facts")


def edge_keys(g):
    return {tuple(sorted((g.keys[a], g.keys[b]))) for a, b in g.edges}


def test_nodes_exact(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    assert g.keys == sorted(k[:-3] for k in FACTS)
    assert len(g.keys) == 6
    i = g.keys.index("11_order_x")
    assert g.n[i] == "11"
    assert g.t[i] == "Beta order"                 # leading # and spaces stripped
    assert g.n[g.keys.index("box2_10_x")] == "10"
    assert all(cs == "filename-guess" for cs in g.cat_source)


def test_edges_exact(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    assert edge_keys(g) == {tuple(sorted(e)) for e in EXPECTED_EDGES}
    assert len(g.edges) == len(EXPECTED_EDGES)


def test_box2_is_never_an_edge_target(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    w = g.keys.index("box2_10_x")
    # its only edge is the one it cites itself (to 12_b); 15_cites_box2's "10_x" went to 10_a
    assert [e for e in g.edges if w in e] == [tuple(sorted((w, g.keys.index("12_b"))))]


def test_collision_reported_and_first_wins(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    col = g.diagnostics["prefix_collisions"]
    assert col == [{"prefix": "12", "files": ["12_b", "12_c"], "used": "12_b"}]
    assert ("10_a", "12_c") not in edge_keys(g)


def test_no_self_edge(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    assert all(a != b for a, b in g.edges)


def test_diagnostics_shape(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    d = g.diagnostics
    assert {"nodes", "edges", "cats", "layout_ms", "prefix_collisions", "unreferenced_count",
            "facts_hash"} <= set(d)
    assert d["nodes"] == 6 and d["edges"] == 4
    # cited by another fact: 10_a, 11_order_x, 12_b. Never cited: 12_c, box2_10_x, 15_cites_box2
    assert d["unreferenced_count"] == 3
    assert sum(d["cats"].values()) == 6


def test_categories_first_match_default_helix(tmp_path):
    mindgraph.clear_cache()
    d = make_tree(tmp_path / "f", {
        "01_loan_order.md": "# x\n",     # 'order' (helix) is tested before 'loan'
        "02_budget.md": "# x\n",              # finance
        "03_chord_book.md": "# x\n",           # music
        "04_boat.md": "# x\n",                # making
        "05_plain.md": "# x\n",                # no match → helix
        "box2_06_thing.md": "# x\n",            # research (box2_)
        "07_home.md": "# x\n",                # lowercased → mapapp
    })
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    got = dict(zip(g.keys, (g.cats[c] for c in g.c)))
    assert got == {"01_loan_order": "helix", "02_budget": "finance", "03_chord_book": "music",
                   "04_boat": "making", "05_plain": "helix", "box2_06_thing": "research",
                   "07_home": "mapapp"}


def test_title_capped_at_90(tmp_path):
    mindgraph.clear_cache()
    d = make_tree(tmp_path / "f", {"01_long.md": "# " + "x" * 200 + "\n"})
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    assert g.t[0] == "x" * 90


# ── layout ──────────────────────────────────────────────────────────────────────────────────
def test_layout_deterministic(tree, tmp_path):
    a = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    mindgraph.clear_cache()
    b = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    assert a is not b
    assert np.array_equal(np.array(a.p), np.array(b.p))
    # and a byte-identical copy of the tree elsewhere lays out identically too (mtimes differ,
    # so the hash differs, but the positions depend only on the facts and the seed)
    other = make_tree(tmp_path / "copy")
    c = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=other))
    assert np.array_equal(np.array(a.p), np.array(c.p))


def test_cache_hit_and_miss(tree, monkeypatch):
    calls = []
    real = mindgraph._layout
    monkeypatch.setattr(mindgraph, "_layout", lambda *a, **k: calls.append(1) or real(*a, **k))
    opts = mindgraph.MindOptions(facts_dir=tree)
    g1 = mindgraph.build_graph(opts)
    g2 = mindgraph.build_graph(opts)
    assert g2 is g1 and len(calls) == 1                        # unchanged → hit, no new layout
    assert g2.diagnostics["layout_ms"] == g1.diagnostics["layout_ms"]
    (tree / "16_new.md").write_text("# New\ncites 10_a\n")
    g3 = mindgraph.build_graph(opts)
    assert len(calls) == 2                                      # new file → miss
    assert g3.diagnostics["facts_hash"] != g1.diagnostics["facts_hash"]
    assert len(g3.keys) == 7
    t = time.time() + 5                                         # touched file → miss
    os.utime(tree / "12_c.md", (t, t))
    g4 = mindgraph.build_graph(opts)
    assert len(calls) == 3 and g4.diagnostics["facts_hash"] != g3.diagnostics["facts_hash"]


def test_positions_inside_and_centred(tree):
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree))
    P = np.array(g.p)
    assert P.shape == (6, 3)
    assert (np.linalg.norm(P, axis=1) <= 0.9 + 1e-9).all()
    assert np.linalg.norm(P.mean(0)) < 0.05


def test_positions_inside_and_centred_bigger(tmp_path):
    # 60 facts in a chain across categories: enough points for the percentile scale to bite
    mindgraph.clear_cache()
    names = ["order", "attest", "budget", "music", "home", "boat", "user"]
    files = {f"{i:02d}_{names[i % 7]}.md": f"# n{i}\nsee {(i + 1) % 60:02d}_x\n" for i in range(60)}
    d = make_tree(tmp_path / "f", files)
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    P = np.array(g.p)
    r = np.linalg.norm(P, axis=1)
    assert (r <= 0.9 + 1e-9).all()
    assert np.linalg.norm(P.mean(0)) < 0.05
    assert len(g.edges) == 60


def test_empty_dir(tmp_path):
    mindgraph.clear_cache()
    d = tmp_path / "empty"
    d.mkdir()
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    assert g.keys == [] and g.edges == [] and g.diagnostics["nodes"] == 0


def test_json_shape(tree):
    j = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=tree)).to_json()
    json.dumps(j)
    for k in ("p", "c", "n", "t", "e", "d", "cats", "k", "cat_source", "diagnostics"):
        assert k in j, k
    assert len(j["p"]) == len(j["k"]) == len(j["d"]) == 6
    deg = [0] * 6
    for a, b in j["e"]:
        deg[a] += 1
        deg[b] += 1
    assert j["d"] == deg


# ── recalls ─────────────────────────────────────────────────────────────────────────────────
def patch_line(ts, files):
    return json.dumps({"ts": ts, "lane": "chat", "files": [{"file": f, "score": 0.1} for f in files]})


def test_recalls(tmp_path):
    p = tmp_path / "PATCHES.jsonl"
    p.write_text("\n".join([
        patch_line("2026-10-04T20:00:00Z", ["10_a.md", "11_order_x.md"]),
        patch_line("2026-10-04T21:00:00Z", ["12_b.md"]),
        "{this is not json",
        patch_line("2026-10-04T22:00:00Z", []),
    ]) + "\n")
    r = mindgraph.recalls(p)
    assert r["skipped"] == 1
    assert r["recalls"] == [
        {"ts": "2026-10-04T20:00:00Z", "keys": ["10_a", "11_order_x"]},
        {"ts": "2026-10-04T21:00:00Z", "keys": ["12_b"]},
        {"ts": "2026-10-04T22:00:00Z", "keys": []},
    ]
    r2 = mindgraph.recalls(p, "2026-10-04T20:00:00Z")          # strictly after
    assert [e["ts"] for e in r2["recalls"]] == ["2026-10-04T21:00:00Z", "2026-10-04T22:00:00Z"]
    r3 = mindgraph.recalls(p, "2026-10-04T21:30:00+00:00")     # offset form parses too
    assert [e["ts"] for e in r3["recalls"]] == ["2026-10-04T22:00:00Z"]
    assert mindgraph.recalls(p, "2026-10-05T00:00:00Z")["recalls"] == []


def test_recalls_missing_file(tmp_path):
    r = mindgraph.recalls(tmp_path / "nope.jsonl")
    assert r == {"recalls": [], "skipped": 0}


def test_recalls_bad_since(tmp_path):
    with pytest.raises(ValueError):
        mindgraph.recalls(tmp_path / "x.jsonl", "yesterday")


# ── positive control on the real tree (read-only) ───────────────────────────────────────────
def test_real_tree_node_count():
    mindgraph.clear_cache()
    facts = HERE / "memory" / "facts"
    on_disk = len(list(facts.glob("*.md")))      # == ls memory/facts/*.md | wc -l
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=facts))
    assert len(g.keys) == on_disk > 0
    assert g.diagnostics["nodes"] == on_disk


# ── ORDER 65: a fact may declare its category (workspace/categories) ───────────────────────
def test_declared_category_sets_lobe(tmp_path):
    mindgraph.clear_cache()
    d = make_tree(tmp_path / "f", {
        "01_plain.md": "# Plain\nno category\n",                       # → helix (default guess)
        "02_budget.md": "# Budget\n",                                   # → finance (guess)
        "03_declares.md": "# Declares\nCategory: project: boats\n",     # → making, declared
        "04_unknown.md": "# Unknown\nCategory: wizardry\n",             # unknown → guess (helix)
    })
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    lobe = dict(zip(g.keys, (g.cats[c] for c in g.c)))
    src = dict(zip(g.keys, g.cat_source))
    assert lobe["03_declares"] == "making" and src["03_declares"] == "declared"
    assert src == {"01_plain": "filename-guess", "02_budget": "filename-guess",
                   "03_declares": "declared", "04_unknown": "filename-guess"}
    assert lobe["01_plain"] == "helix" and lobe["02_budget"] == "finance"
    assert lobe["04_unknown"] == "helix"
    assert g.to_json()["cat_source"] == g.cat_source


def test_categories_lobes_are_the_seven():
    # every lobe the list may name is one of the orb's seven, so it keeps seven lobes
    import importlib.util
    spec = importlib.util.spec_from_file_location("cats_under_test", mindgraph.CATEGORIES_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.LOBES) == set(mindgraph.CAT_NAMES) and len(mindgraph.CAT_NAMES) == 7
    assert {c.lobe for c in mod.load(mindgraph.CATEGORIES_FILE)} <= set(mindgraph.CAT_NAMES)


def test_broken_categories_module_falls_back(tmp_path, monkeypatch):
    mindgraph.clear_cache()
    bad = tmp_path / "categories.py"
    bad.write_text("def load(:\n")
    monkeypatch.setattr(mindgraph, "CATEGORIES_PY", bad)
    d = make_tree(tmp_path / "f", {"03_declares.md": "# D\nCategory: project: boats\n"})
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    assert g.cat_source == ["filename-guess"] and g.cats[g.c[0]] == "helix"


def test_cache_key_includes_list_mtime(tmp_path, monkeypatch):
    mindgraph.clear_cache()
    lst = tmp_path / "CATEGORIES.txt"
    lst.write_text(mindgraph.CATEGORIES_FILE.read_text())
    monkeypatch.setattr(mindgraph, "CATEGORIES_FILE", lst)
    d = make_tree(tmp_path / "f", {"03_declares.md": "# D\nCategory: project: boats\n"})
    opts = mindgraph.MindOptions(facts_dir=d)
    g1 = mindgraph.build_graph(opts)
    assert mindgraph.build_graph(opts) is g1                    # unchanged → hit
    assert g1.cats[g1.c[0]] == "making"
    lst.write_text(lst.read_text().replace("project: boats    | making", "project: boats    | music"))
    t = time.time() + 5
    os.utime(lst, (t, t))
    g2 = mindgraph.build_graph(opts)                            # list edited → miss, new lobe
    assert g2 is not g1 and g2.cats[g2.c[0]] == "music"


# ── ORDER 73b: a fact with no valid declared line takes its category from ASSIGNED.csv ─────
# Every test points mindgraph.ASSIGNED_FILE at tmp_path (absent unless a test writes it), so the
# real tree's results do not change after Odin runs `categories.py assign` for real.
@pytest.fixture(autouse=True)
def _no_live_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(mindgraph, "ASSIGNED_FILE", tmp_path / "ASSIGNED.csv")


def write_assigned(path, rows):
    path.write_text("file,primary,secondary,source\n" + "".join(",".join(r) + "\n" for r in rows))


def test_assigned_path_is_the_sidecar_beside_the_list():
    assert mindgraph.ASSIGNED_DEFAULT == mindgraph.CATEGORIES_FILE.parent / "ASSIGNED.csv"


def test_assigned_category_sets_lobe(tmp_path):
    mindgraph.clear_cache()
    d = make_tree(tmp_path / "f", {
        "01_plain.md": "# Plain\nno category\n",                       # assigned → making
        "02_budget.md": "# Budget\n",                                   # nothing → finance (guess)
        "03_declares.md": "# Declares\nCategory: project: boats\n",     # declared beats assigned
        "04_unknown.md": "# Unknown\nCategory: wizardry\n",             # bad line → assigned
    })
    write_assigned(mindgraph.ASSIGNED_FILE, [["01_plain.md", "project: boats", "", "proposal"],
                                             ["03_declares.md", "project: music", "", "proposal"],
                                             ["04_unknown.md", "research: math", "", "override"]])
    g = mindgraph.build_graph(mindgraph.MindOptions(facts_dir=d))
    lobe = dict(zip(g.keys, (g.cats[c] for c in g.c)))
    src = dict(zip(g.keys, g.cat_source))
    assert src == {"01_plain": "assigned", "02_budget": "filename-guess",
                   "03_declares": "declared", "04_unknown": "assigned"}
    assert lobe == {"01_plain": "making", "02_budget": "finance", "03_declares": "making",
                    "04_unknown": "research"}


def test_cache_key_includes_assigned_mtime(tmp_path):
    mindgraph.clear_cache()
    d = make_tree(tmp_path / "f", {"01_plain.md": "# Plain\n"})
    opts = mindgraph.MindOptions(facts_dir=d)
    g1 = mindgraph.build_graph(opts)
    assert not mindgraph.ASSIGNED_FILE.exists()
    assert mindgraph.build_graph(opts) is g1                    # absent twice → the same token → hit
    assert g1.cat_source == ["filename-guess"] and g1.cats[g1.c[0]] == "helix"
    write_assigned(mindgraph.ASSIGNED_FILE, [["01_plain.md", "project: boats", "", "proposal"]])
    g2 = mindgraph.build_graph(opts)                            # a new assign → miss, no fact changed
    assert g2 is not g1 and g2.cat_source == ["assigned"] and g2.cats[g2.c[0]] == "making"
    assert mindgraph.build_graph(opts) is g2
    write_assigned(mindgraph.ASSIGNED_FILE, [["01_plain.md", "project: music", "", "override"]])
    t = time.time() + 5
    os.utime(mindgraph.ASSIGNED_FILE, (t, t))
    g3 = mindgraph.build_graph(opts)                            # assign re-run → miss again
    assert g3 is not g2 and g3.cats[g3.c[0]] == "music"
