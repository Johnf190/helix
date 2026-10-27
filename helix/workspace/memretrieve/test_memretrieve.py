"""ORDER 34 — the retriever on its own. Every test but the `real model` ones uses a stub embedder and a
fact base + vector cache in tmp_path, so nothing live is read or written.

    .venv/bin/python -m pytest -q workspace/memretrieve/test_memretrieve.py
"""
import ast
import hashlib
import math
import os
import re
import shutil
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HELIX = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import memretrieve as mr  # noqa: E402

OPEN = {"bm25": -math.inf, "embed": -math.inf}


class Stub:
    """A deterministic bag-of-words 'embedding': one dimension per hashed token. Counts calls."""
    tag = "stub"

    def __init__(self):
        self.texts = 0

    def chunks(self, text, head):
        return [text]

    def embed(self, texts, query=False):
        self.texts += len(texts)
        out = np.zeros((len(texts), 64), np.float32)
        for i, t in enumerate(texts):
            for w in mr.toks(t):
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            out[i] /= (np.linalg.norm(out[i]) or 1)
        return out


@pytest.fixture
def base(tmp_path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "111_image_gen.md").write_text("# Helix image generation\nlocal picture generator, stable diffusion, start it with render.\n")
    (facts / "96_repo.md").write_text("# Helix published\npublic github repository, MIT licence.\n")
    (facts / "76_boat.md").write_text("# The boat\nboat weight and draft, foam core planing hull.\n")
    (facts / "50_long.md").write_text("# A long one\n" + "attest attestation verifier " * 400)
    stub = Stub()
    ix = mr.Index(facts_dir=str(facts), cache_dir=str(tmp_path / "cache"), embedder=stub)
    return facts, ix, stub, tmp_path


# ------------------------------------------------------------------ copied from memeval/score.py

def _score_py():
    return ast.parse(open(f"{HELIX}/workspace/memeval/score.py").read())


def test_tokenizer_stop_list_and_bm25_params_equal_score_py():
    """score.py runs its whole eval at import, so memretrieve copies its parts. Prove the copy."""
    tree = _score_py()
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names == ["STOP"]:
                consts["STOP"] = set(node.value.args[0].func.value.value.split())
            if isinstance(node.targets[0], ast.Tuple) and [e.id for e in node.targets[0].elts] == ["K1", "B"]:
                consts["K1"], consts["B"] = [e.value for e in node.value.elts]
        if isinstance(node, ast.FunctionDef) and node.name == "toks":
            consts["toks"] = ast.unparse(node)
    assert consts["STOP"] == mr.STOP
    assert (consts["K1"], consts["B"]) == (mr.K1, mr.B)
    import inspect
    assert consts["toks"] == ast.unparse(ast.parse(inspect.getsource(mr.toks)).body[0])


def test_bm25_scores_equal_score_py_formula(base):
    facts, ix, _stub, _tmp = base
    ix.refresh()
    s = ix.bm25("local picture generator")
    assert max(s, key=s.get) == "111_image_gen.md"
    assert s["76_boat.md"] == 0.0


# ------------------------------------------------------------------ floor and gate

def test_floor_gives_an_empty_patch_not_a_guess(base):
    _f, ix, _s, _t = base
    r = ix.retrieve("ok go ahead", arm="bm25", floors={"bm25": 0.5})
    assert r["hits"] == [] and r["reason"] == "below floor"
    r = ix.retrieve("local picture generator", arm="bm25", floors={"bm25": 0.5})
    assert r["hits"][0][0] == "111_image_gen.md" and r["reason"] == "ok"
    assert mr.render_patch(ix, []) == ""


def test_hybrid_gate_opens_if_either_arm_opens_and_shuts_if_neither(base):
    _f, ix, _s, _t = base
    q = "local picture generator"
    assert ix.retrieve(q, arm="hybrid", floors={"bm25": 1e9, "embed": 1e9})["hits"] == []
    assert ix.retrieve(q, arm="hybrid", floors={"bm25": 1e9, "embed": -1})["hits"]
    assert ix.retrieve(q, arm="hybrid", floors={"bm25": -1, "embed": 1e9})["hits"]


def test_the_gate_is_the_newest_turn_but_older_turns_shape_the_ranking(base):
    _f, ix, _s, _t = base
    fl = {"bm25": 0.5}
    # newest turn contentless -> nothing, even though an older turn has a topic
    assert ix.retrieve(["ok", "local picture generator"], arm="bm25", floors=fl)["hits"] == []
    # newest turn has a topic of its own -> the older turn's topic adds weight (half)
    r = ix.retrieve(["the boat draft", "public github repository"], arm="bm25", floors=fl, k=2)
    assert [h[0] for h in r["hits"]] == ["76_boat.md", "96_repo.md"]
    assert len(mr.TURN_WEIGHTS) == 3 and mr.TURN_WEIGHTS[0] > mr.TURN_WEIGHTS[1] > mr.TURN_WEIGHTS[2]


def test_live_floors_are_the_calibrated_ones():
    txt = open(f"{HELIX}/workspace/memeval/RESULT_ORDER34.txt").read()
    m = re.search(r"FLOORS = (\{[^}]*\})\s+\(max over", txt)
    assert m and ast.literal_eval(m.group(1)) == mr.FLOORS


# ------------------------------------------------------------------ skip, k, budget

def test_already_inlined_facts_are_skipped(base):
    _f, ix, _s, _t = base
    q = "local picture generator github"
    full = [h[0] for h in ix.retrieve(q, arm="bm25", floors=OPEN)["hits"]]
    assert "111_image_gen.md" in full
    r = ix.retrieve(q, arm="bm25", floors=OPEN, skip={"111_image_gen.md"})
    assert "111_image_gen.md" not in [h[0] for h in r["hits"]] and r["hits"]
    r = ix.retrieve(q, arm="bm25", floors=OPEN, skip=set(full), k=len(full))
    assert r["hits"] == [] and r["reason"] == "all skipped"


def test_k_and_the_char_budget(base):
    _f, ix, _s, _t = base
    hits = ix.retrieve("attest local picture github boat", arm="bm25", floors=OPEN, k=2, char_budget=200)["hits"]
    assert len(hits) == 2
    modes = {h[0]: h[2].rsplit("; ", 1)[1] for h in hits}
    patch = mr.render_patch(ix, hits)
    assert patch.startswith(mr.PATCH_HEADER)
    for fn, mode in modes.items():
        if mode == "full":
            assert f"# memory/facts/{fn}\n" in patch
        else:
            assert f"- memory/facts/{fn} — " in patch
    assert modes.get("50_long.md", "head") == "head"     # 12k chars never fits 200


def test_query_signature_returns_file_score_why(base, monkeypatch):
    _f, ix, _s, _t = base
    monkeypatch.setattr(mr, "FLOORS", dict(OPEN))
    out = ix.query("local picture generator", k=3, char_budget=6000, arm="bm25")
    assert len(out) == 3 and out[0][0] == "111_image_gen.md"
    assert all(isinstance(f, str) and isinstance(s, float) and isinstance(w, str) for f, s, w in out)


# ------------------------------------------------------------------ the vector cache

def test_cache_reembeds_only_changed_files(base):
    facts, ix, stub, tmp = base
    ix.refresh(need_vectors=True)
    assert ix.embedded == 4 and stub.texts == 4
    files = sorted(os.listdir(tmp / "cache" / "stub"))
    assert len(files) == 4 and all(f.endswith(".npy") for f in files)
    # a fresh process with the same cache embeds nothing
    stub2 = Stub()
    ix2 = mr.Index(facts_dir=str(facts), cache_dir=str(tmp / "cache"), embedder=stub2)
    ix2.refresh(need_vectors=True)
    assert ix2.embedded == 0 and stub2.texts == 0
    # touch without change: nothing; change one: exactly one; add one: exactly one
    os.utime(facts / "96_repo.md")
    ix2.refresh(need_vectors=True)
    assert ix2.embedded == 0
    (facts / "96_repo.md").write_text("# Helix published\npublic github repository, MIT licence. Now private.\n")
    ix2.refresh(need_vectors=True)
    assert ix2.embedded == 1 and stub2.texts == 1
    (facts / "200_new.md").write_text("# New\nremembered mid-session\n")
    r = ix2.retrieve("remembered mid-session", arm="hybrid", floors=OPEN)
    assert ix2.embedded == 2 and r["hits"][0][0] == "200_new.md"
    # a removed fact leaves the index
    (facts / "200_new.md").unlink()
    ix2.refresh(need_vectors=True)
    assert "200_new.md" not in ix2.docs and "200_new.md" not in ix2.vecs


def test_bm25_arm_never_loads_the_embedder(base):
    facts, _ix, _s, tmp = base

    def boom():
        raise AssertionError("embedder loaded for a bm25 query")
    ix = mr.Index(facts_dir=str(facts), cache_dir=str(tmp / "c2"), embedder_factory=boom)
    assert ix.retrieve("github", arm="bm25", floors=OPEN)["hits"]
    assert not (tmp / "c2").exists()


def test_writes_nothing_outside_the_cache_dir(base):
    facts, ix, _s, tmp = base
    before = {p: os.path.getmtime(facts / p) for p in os.listdir(facts)}
    ix.retrieve("local picture generator", arm="hybrid", floors=OPEN)
    assert {p: os.path.getmtime(facts / p) for p in os.listdir(facts)} == before
    assert sorted(os.listdir(tmp)) == ["cache", "facts"]


def test_model_and_cache_live_on_the_internal_hdd():
    assert mr.MODEL_DIR.startswith("__BULK__/") and mr.CACHE_DIR.startswith("__BULK__/")


# ------------------------------------------------------------------ the real model (no live writes)

needs_model = pytest.mark.skipif(not os.path.exists(f"{mr.MODEL_DIR}/onnx/model.onnx"), reason="model not downloaded")


@needs_model
def test_real_model_file_is_the_pinned_one():
    h = hashlib.sha256()
    with open(f"{mr.MODEL_DIR}/onnx/model.onnx", "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    assert h.hexdigest() == mr.MODEL_ONNX_SHA256


@needs_model
def test_real_model_finds_the_picture_generator_by_meaning(tmp_path):
    """'picture' never appears in the image-generation fact (124_*'s vocabulary miss). The embed arm
    should still rank it first among three real facts. Facts copied to tmp; cache in tmp."""
    facts = tmp_path / "facts"
    facts.mkdir()
    for fn in ("111_example_afe9.md", "96_example_75ad.md",
               "76_example_8c8f.md"):
        shutil.copy(f"{mr.FACTS}/{fn}", facts / fn)
    ix = mr.Index(facts_dir=str(facts), cache_dir=str(tmp_path / "cache"))
    r = ix.retrieve("how do I start the local picture generator", arm="embed", floors=OPEN)
    assert r["hits"][0][0] == "111_example_afe9.md", r
    v = ix.embedder.embed(["a", "b"])
    assert v.shape == (2, 384) and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)


# ------------------------------------------------------------------ ORDER 35b: memory/archive/

def _arch(facts):
    a = facts.parent / "archive"
    a.mkdir(exist_ok=True)
    return a


def test_the_archive_is_the_facts_sibling_by_default(base):
    facts, ix, _s, _t = base
    assert ix.archive_dir == str(facts.parent / "archive")
    assert mr.ARCHIVE == os.path.join(os.path.dirname(mr.FACTS), "archive")
    assert 0 < mr.ARCHIVE_WEIGHT < 1


def test_an_archived_fact_is_indexed_labelled_and_rendered_from_the_archive(base):
    facts, ix, _s, _t = base
    os.replace(facts / "76_boat.md", _arch(facts) / "76_boat.md")
    r = ix.retrieve("boat weight and draft planing hull", arm="bm25", floors=OPEN, k=1)
    (fn, _score, why), = r["hits"]
    # ORDER 42c: an archived fact is never inlined, whatever the budget: one pointer line
    assert fn == "76_boat.md" and "(archived)" in why and why.endswith("; pointer")
    patch = mr.render_patch(ix, r["hits"])
    assert patch == f"{mr.PATCH_HEADER}\n\n- memory/archive/76_boat.md (archived) — The boat"
    assert "foam core" not in patch and "memory/facts/76_boat.md" not in patch
    r = ix.retrieve("boat", arm="bm25", floors=OPEN, k=1, char_budget=1)
    assert "- memory/archive/76_boat.md (archived) — The boat" in mr.render_patch(ix, r["hits"])


@pytest.mark.parametrize("arm", ["bm25", "embed", "hybrid"])
def test_an_archived_fact_ranks_below_the_same_text_live(base, arm):
    facts, ix, _s, _t = base
    text = "# twin\nthe orchard irrigation schedule for the walnut trees\n"
    (facts / "x1.md").write_text(text)
    (_arch(facts) / "x2.md").write_text(text)
    # ORDER 74: arms=() — this test is about ranking; the shipped adapt arm adds close heads after k
    r = ix.retrieve("orchard irrigation walnut", arm=arm, floors=OPEN, k=2, arms=())
    assert [h[0] for h in r["hits"]] == ["x1.md", "x2.md"], r
    if arm != "hybrid":
        live, arch = r["hits"][0][1], r["hits"][1][1]
        assert arch == pytest.approx(live * mr.ARCHIVE_WEIGHT, rel=1e-3)


def test_the_gate_reads_unweighted_scores_so_a_direct_question_still_finds_it(base):
    """The floor asks 'does this turn have a topic at all' (calibrated on contentless turns); a
    direct question about an archived fact has one. The weight orders hits, it does not gate."""
    facts, ix, _s, _t = base
    os.replace(facts / "76_boat.md", _arch(facts) / "76_boat.md")
    q = "foam core planing hull"
    raw = ix.retrieve(q, arm="bm25", floors=OPEN)["gate"]["bm25"]
    r = ix.retrieve(q, arm="bm25", floors={"bm25": raw * (1 + mr.ARCHIVE_WEIGHT) / 2})
    assert [h[0] for h in r["hits"]][:1] == ["76_boat.md"], r


def test_a_move_between_facts_and_archive_flips_the_label_on_a_warm_index(base):
    """os.replace keeps size and mtime, so the stat key alone would miss the move."""
    facts, ix, _s, _t = base
    q = "boat weight and draft planing hull"
    assert "(archived)" not in ix.retrieve(q, arm="bm25", floors=OPEN, k=1)["hits"][0][2]
    os.replace(facts / "76_boat.md", _arch(facts) / "76_boat.md")
    assert "(archived)" in ix.retrieve(q, arm="bm25", floors=OPEN, k=1)["hits"][0][2]
    os.replace(_arch(facts) / "76_boat.md", facts / "76_boat.md")
    assert "(archived)" not in ix.retrieve(q, arm="bm25", floors=OPEN, k=1)["hits"][0][2]


def test_a_name_in_both_places_is_the_live_one(base):
    facts, ix, _s, _t = base
    (_arch(facts) / "76_boat.md").write_text("# old boat\nboat\n")
    hits = ix.retrieve("boat draft", arm="bm25", floors=OPEN, k=5)["hits"]
    assert [h for h in hits if h[0] == "76_boat.md"][0][2].count("(archived)") == 0
    assert ix.text_of("76_boat.md").startswith("# The boat")


def test_the_archive_is_read_never_written(base):
    facts, ix, _s, tmp = base
    a = _arch(facts)
    os.replace(facts / "76_boat.md", a / "76_boat.md")
    before = {p: (os.path.getmtime(a / p), (a / p).read_bytes()) for p in os.listdir(a)}
    ix.retrieve("boat draft", arm="hybrid", floors=OPEN)
    assert {p: (os.path.getmtime(a / p), (a / p).read_bytes()) for p in os.listdir(a)} == before
    assert sorted(os.listdir(tmp)) == ["archive", "cache", "facts"]


# ------------------------------------------------------------------ ORDER 42c: superseded facts never inline

def _world(tmp_path, facts=(), archive=()):
    """A fact base from (name, text) pairs, with an archive beside it."""
    f = tmp_path / "facts"
    f.mkdir()
    for name, text in facts:
        (f / name).write_text(text)
    if archive:
        a = tmp_path / "archive"
        a.mkdir()
        for name, text in archive:
            (a / name).write_text(text)
    return mr.Index(facts_dir=str(f), cache_dir=str(tmp_path / "cache"), embedder=Stub())


OLD = "# 🔴 SUPERSEDED by `20_new.md` (2026-09-23)\n# What it was\nzebra stripes orchard ladder secretbody\n"
NEW = "# Supersedes `10_old.md`\nzebra stripes orchard ladder, current.\n"


def test_42c_a_whole_superseded_hit_is_a_pointer_and_its_replacement_joins(tmp_path):
    ix = _world(tmp_path, [("10_old.md", OLD), ("20_new.md", NEW), ("30_x.md", "# other\nzebra\n")])
    r = ix.retrieve("secretbody zebra", arm="bm25", floors=OPEN, k=1)
    assert [h[0] for h in r["hits"]] == ["10_old.md", "20_new.md"]
    assert all(type(h) is tuple and len(h) == 3 for h in r["hits"])          # hits stay plain 3-tuples
    assert r["hits"][0][2].endswith("; pointer") and r["hits"][1][2].endswith("; full")
    assert "replacement for 10_old.md" in r["hits"][1][2]
    assert r["replaced_by"] == {"10_old.md": "20_new.md"}
    patch = mr.render_patch(ix, r["hits"])
    assert "- memory/facts/10_old.md — SUPERSEDED by memory/facts/20_new.md (newer, used instead)" in patch
    assert "# memory/facts/20_new.md\n" in patch
    for line in OLD.splitlines():                                             # no line of A's body
        assert line not in patch
    assert "secretbody" not in patch


def test_42c_no_replaced_by_on_any_return_path_is_still_a_dict(tmp_path):
    ix = _world(tmp_path, [("10_old.md", OLD), ("20_new.md", NEW)])
    assert ix.retrieve("ok", arm="bm25", floors={"bm25": 1e9})["replaced_by"] == {}
    assert ix.retrieve("stripes", arm="bm25", floors=OPEN, skip={"10_old.md", "20_new.md"})["replaced_by"] == {}
    (tmp_path / "empty").mkdir()
    assert _world(tmp_path / "empty").retrieve("x", arm="bm25")["replaced_by"] == {}


def test_42c_the_ranked_hits_are_unchanged_only_replacements_are_added(tmp_path):
    """Ranking, the gate and FLOORS are untouched: drop the added replacements and the hit list is
    exactly the first k of the full ranking, as before the order."""
    ix = _world(tmp_path, [("10_old.md", OLD), ("20_new.md", NEW), ("30_x.md", "# other\nzebra ladder\n"),
                           ("40_y.md", "# y\norchard\n")])
    full = [h[0] for h in ix.retrieve("secretbody zebra orchard", arm="bm25", floors=OPEN, k=99)["hits"]]
    for k in (1, 2, 3):
        hits = ix.retrieve("secretbody zebra orchard", arm="bm25", floors=OPEN, k=k)["hits"]
        assert [h[0] for h in hits if "; replacement for " not in h[2]] == full[:k]


def test_42c_the_replacement_is_not_added_twice_or_when_already_sent(tmp_path):
    ix = _world(tmp_path, [("10_old.md", OLD), ("20_new.md", NEW)])
    hits = ix.retrieve("zebra stripes orchard ladder", arm="bm25", floors=OPEN, k=5)["hits"]
    assert sorted(h[0] for h in hits) == ["10_old.md", "20_new.md"]
    r = ix.retrieve("secretbody", arm="bm25", floors=OPEN, k=5, skip={"20_new.md"})
    assert [h[0] for h in r["hits"]] == ["10_old.md"] and r["replaced_by"] == {"10_old.md": "20_new.md"}
    assert "SUPERSEDED by memory/facts/20_new.md (newer, used instead)" in mr.render_patch(ix, r["hits"])


def test_42c_a_pointer_frees_its_budget_for_the_replacement(tmp_path):
    big = OLD + "filler " * 300
    ix = _world(tmp_path, [("10_old.md", big), ("20_new.md", NEW)])
    hits = ix.retrieve("secretbody", arm="bm25", floors=OPEN, k=1, char_budget=len(NEW) + 5)["hits"]
    assert [(h[0], h[2].rsplit("; ", 1)[1]) for h in hits] == [("10_old.md", "pointer"), ("20_new.md", "full")]


def test_42c_a_chain_ends_at_the_newest_live_fact(tmp_path):
    a = "# SUPERSEDED by `B_2.md`\nalpha secretbody\n"
    b = "# SUPERSEDED by `C_3.md`\nalpha beta\n"
    ix = _world(tmp_path, [("A_1.md", a), ("B_2.md", b), ("C_3.md", "# the newest\nalpha gamma\n")])
    r = ix.retrieve("secretbody", arm="bm25", floors=OPEN, k=1)
    assert [h[0] for h in r["hits"]] == ["A_1.md", "C_3.md"] and r["replaced_by"] == {"A_1.md": "C_3.md"}
    assert ix.replacement_of("B_2.md") == "C_3.md"
    assert "SUPERSEDED by memory/facts/C_3.md" in mr.render_patch(ix, r["hits"])


def test_42c_a_chain_stops_at_depth_five(tmp_path):
    names = [f"{i}_c.md" for i in range(1, 9)]
    ix = _world(tmp_path, [(n, f"# SUPERSEDED by `{names[i + 1]}`\nchain\n" if i + 1 < len(names) else "# end\n")
                           for i, n in enumerate(names)])
    ix.refresh()
    assert ix.replacement_of("1_c.md") == names[mr.MAX_CHAIN] == "6_c.md"


def test_42c_a_cycle_is_reported_and_both_are_treated_as_partial(tmp_path):
    ix = _world(tmp_path, [("1_p.md", "# SUPERSEDED by `2_q.md`\nkiwi\n"), ("2_q.md", "# SUPERSEDED by `1_p.md`\nkiwi\n")])
    ix.refresh()
    assert ix.sup["cycles"] == [["1_p.md", "2_q.md"]]
    assert ix.replacement_of("1_p.md") is None and ix.replacement_of("2_q.md") is None
    assert ix.partial_of("1_p.md") == ["2_q.md"] and ix.partial_of("2_q.md") == ["1_p.md"]
    r = ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=2)
    assert all(h[2].endswith("; full") for h in r["hits"]) and r["replaced_by"] == {}


def test_42c_an_archived_hit_never_inlines_even_when_it_ranks_first(tmp_path):
    ix = _world(tmp_path, [("1_live.md", "# live\nnothing here\n")],
                archive=[("9_gone.md", "# gone heading\nquokka secretbody\n")])
    r = ix.retrieve("quokka", arm="bm25", floors=OPEN, k=5, char_budget=10 ** 6)
    assert r["hits"][0][0] == "9_gone.md" and r["hits"][0][2].endswith("(archived); pointer")
    patch = mr.render_patch(ix, r["hits"])
    assert "- memory/archive/9_gone.md (archived) — gone heading" in patch and "secretbody" not in patch


def test_42c_an_archived_whole_superseded_fact_points_at_its_live_replacement(tmp_path):
    ix = _world(tmp_path, [("135_disp.md", "# disposition\nwombat current\n")],
                archive=[("133_front.md", "# 🔴 SUPERSEDED BY `135_disp.md` — READ THAT FIRST.\nwombat old\n")])
    r = ix.retrieve("wombat old", arm="bm25", floors=OPEN, k=1)
    assert [h[0] for h in r["hits"]] == ["133_front.md", "135_disp.md"]
    assert ("- memory/archive/133_front.md (archived) — SUPERSEDED by memory/facts/135_disp.md (newer, used instead)"
            in mr.render_patch(ix, r["hits"]))


def test_42c_partial_supersession_inlines_with_a_warning_line(tmp_path):
    ix = _world(tmp_path, [("77_dance.md", "# dance plan\nthe back alley dance\n"),
                           ("78_roof.md", "# pivot\nTHIS SUPERSEDES THE DANCE PLAN IN `77_*`.\nrooftop\n")])
    r = ix.retrieve("back alley dance", arm="bm25", floors=OPEN, k=1)
    assert [h[0] for h in r["hits"]] == ["77_dance.md"] and r["hits"][0][2].endswith("; full")
    assert r["replaced_by"] == {}
    patch = mr.render_patch(ix, r["hits"])
    assert ("# memory/facts/77_dance.md\n⚠ newer: `78_roof.md` supersedes part of this — where they disagree, "
            "78_roof.md wins.\n# dance plan") in patch


def _sup(tmp_path, facts=(), archive=()):
    ix = _world(tmp_path, facts, archive)
    ix.refresh()
    return ix.sup


def _edges(sup):
    return sorted((e["kind"], e["a"], e["b"]) for e in sup["edges"])


def test_42c_passive_superseded_alone_makes_no_edge(tmp_path):
    sup = _sup(tmp_path, [("1_a.md", "# a\nThis was superseded by `2_b.md` later.\n"), ("2_b.md", "# b\nx\n")])
    assert sup["edges"] == [] and sup["replaced"] == {}


def test_42c_names_match_by_full_file_name_never_by_number_prefix(tmp_path):
    sup = _sup(tmp_path, [("77_music.md", "# g\nx\n"), ("5_x.md", "# x\n")],
               archive=[("box2_79.md", "# r\n**Supersedes and retracts `77_probe-study_rounds12_13_unrecorded.md`, wrong.\n")])
    assert sup["edges"] == []
    assert [(u["src"], u["token"]) for u in sup["unresolved"]] == [("box2_79.md", "77_probe-study_rounds12_13_unrecorded.md")]


def test_42c_a_name_resolves_with_or_without_md_and_a_glob_only_when_unique(tmp_path):
    sup = _sup(tmp_path, [("1_a.md", "# a\n"), ("2_b.md", "# b\n"), ("3_c1.md", "# c\n"), ("3_c2.md", "# c\n"),
                          ("9_new.md", "# n\nThis supersedes `1_a`, `2_*` and `3_*`.\n")])
    assert _edges(sup) == [("partial", "1_a.md", "9_new.md"), ("partial", "2_b.md", "9_new.md")]
    assert [u["token"] for u in sup["unresolved"]] == ["3_*"]


def test_42c_a_glob_in_an_archived_sentence_is_never_resolved(tmp_path):
    sup = _sup(tmp_path, [("2_b.md", "# b\n")], archive=[("8_old.md", "# o\nThis supersedes `2_*`.\n")])
    assert sup["edges"] == [] and [u["token"] for u in sup["unresolved"]] == ["2_*"]


def test_42c_the_sentence_ends_at_its_full_stop_and_may_wrap_one_line(tmp_path):
    sup = _sup(tmp_path, [("131_e.md", "# e\n"), ("133_r.md", "# r\n"), ("120_u.md", "# u\n"), ("122_m.md", "# m\n"),
                          ("135_d.md", "# Supersedes the language in `133_r.md`. Reads with `131_e.md`.\n"),
                          ("123_s.md", "# s\nVerified. Supersedes the\n\"restart\" half of `120_*` and `122_*`.\n")])
    assert _edges(sup) == [("partial", "120_u.md", "123_s.md"), ("partial", "122_m.md", "123_s.md"),
                           ("partial", "133_r.md", "135_d.md")]


def test_42c_a_backticked_word_that_is_no_file_name_is_ignored(tmp_path):
    sup = _sup(tmp_path, [("1_a.md", "# a\n- entry in `supersedes` skips the check (report).\n"
                                     "- unless it supersedes it with a new mechanism.\n")])
    assert sup["edges"] == [] and sup["unresolved"] == []


def test_42c_bullets_under_what_this_supersedes(tmp_path):
    body = ("# box2 removed\n\n## What this supersedes\n- `118_w.md`: not a worker any more.\n"
            "- `45_*` / `box1_audit.md`: closed.\n- BOX2 research (`box2_*`, `44_*`) stays as **history**.\n\n"
            "## Done\n- `34_b.md`: unrelated bullet after the section.\n")
    sup = _sup(tmp_path, [("163_n.md", body), ("118_w.md", "# w\n"), ("45_h.md", "# h\n"), ("44_p.md", "# p\n"),
                          ("34_b.md", "# b\n")], archive=[("box1_audit.md", "# a\n")])
    assert _edges(sup) == [("partial", "118_w.md", "163_n.md"), ("partial", "45_h.md", "163_n.md"),
                           ("partial", "box1_audit.md", "163_n.md")]


def test_42c_whole_and_partial_naming_the_same_pair_is_one_whole_edge_not_a_cycle(tmp_path):
    ix = _world(tmp_path, [("10_old.md", OLD), ("20_new.md", NEW)])
    ix.refresh()
    assert ix.sup["cycles"] == [] and ix.replacement_of("10_old.md") == "20_new.md"
    assert ix.partial_of("10_old.md") == []


def test_42c_the_map_follows_the_files_on_refresh(tmp_path):
    ix = _world(tmp_path, [("10_old.md", "# plain\nsecretbody\n"), ("20_new.md", NEW)])
    assert ix.retrieve("secretbody", arm="bm25", floors=OPEN, k=1)["replaced_by"] == {}
    (tmp_path / "facts" / "10_old.md").write_text(OLD)
    assert ix.retrieve("secretbody", arm="bm25", floors=OPEN, k=1)["replaced_by"] == {"10_old.md": "20_new.md"}


def test_42c_superseded_by_reads_the_dirs_and_never_raises(tmp_path, capsys):
    f = tmp_path / "facts"
    f.mkdir()
    (f / "10_old.md").write_text(OLD)
    (f / "20_new.md").write_text(NEW)
    assert mr.superseded_by(str(f)) == {"10_old.md": "20_new.md"}
    assert mr.superseded_by(str(tmp_path / "nope")) == {}
    assert capsys.readouterr().out.count("no supersession map") == 1


# ------------------------------------------------------------------ ORDER 70: retired passages

SHARE = ("# finance folder\nThe folder lives on the internal disk. `backup_memory.sh` mirrors it to\n"
         "`//192.168.77.17/odin/box1`, but **only when the user says to back up.** Nothing else copies it.\n")
WRAPPED = "mirrors it to `//192.168.77.17/odin/box1`, but **only when the user says to back up.**"


def _ret(tmp_path, facts=(), archive=()):
    ix = _world(tmp_path, facts, archive)
    ix.refresh()
    return ix, ix.ret


def _why(ret):
    return [(u["b"], u["line"], u["why"]) for u in ret["unresolved"]]


def test_70_resolved_by_name_by_glob_and_across_a_wrapped_line(tmp_path):
    for i, tok in enumerate(("103_fin.md", "103_fin", "103_*")):
        (tmp_path / str(i)).mkdir()
        ix, ret = _ret(tmp_path / str(i), [("103_fin.md", SHARE),
                                           ("164_ind.md", f"# independent\nRETIRES: `{tok}` \"{WRAPPED}\"\n")])
        assert _why(ret) == [], tok
        [r] = ret["applied"]
        assert (r["a"], r["b"], r["passage"]) == ("103_fin.md", "164_ind.md", WRAPPED)
        assert ix.text_of("103_fin.md")[r["start"]:r["end"]] == WRAPPED.replace("to `", "to\n`")
        shown = ix.apply_retirements("103_fin.md")
        assert shown == SHARE.strip().replace(
            WRAPPED.replace("to `", "to\n`"), f'⟦RETIRED by 164_ind.md: "{WRAPPED[:60]}…"⟧')
        assert "only when the user says" not in shown and "Nothing else copies it." in shown


def test_70_unresolved_lines_carry_the_right_why(tmp_path):
    b = ("# newer\n"
         "RETIRES: `103_fin.md` \"this sentence is not in the old fact\"\n"
         "RETIRES: `2_twice.md` \"the same twenty-char line\"\n"
         "RETIRES: `103_fin.md` \"short one\"\n"
         "RETIRES: `5_new.md` \"text of the newer fact itself\"\n"
         "RETIRES: `77_nowhere.md` \"a passage in a fact that is not there\"\n")
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE), ("5_new.md", b + "text of the newer fact itself\n"),
                              ("2_twice.md", "# t\nthe same twenty-char line\nthe same  twenty-char\tline\n")])
    assert ret["applied"] == []
    assert _why(ret) == [("5_new.md", 2, "0 matches in 103_fin.md"),
                         ("5_new.md", 3, "2 matches in 2_twice.md"),
                         ("5_new.md", 4, "passage < 20 chars"),
                         ("5_new.md", 5, "retires itself"),
                         ("5_new.md", 6, "no such fact `77_nowhere.md`")]


def test_70_an_archived_b_and_a_malformed_line_are_unresolved(tmp_path):
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE), ("7_bad.md", "# bad\nRETIRES: 103_fin.md no quotes\n")],
                   archive=[("9_old.md", f"# old\nRETIRES: `103_fin.md` \"{WRAPPED}\"\n")])
    assert ret["applied"] == []
    assert [(b, ln) for b, ln, _w in _why(ret)] == [("7_bad.md", 2), ("9_old.md", 2)]
    assert _why(ret)[0][2].startswith("malformed") and _why(ret)[1][2] == "B archived"
    assert ix.apply_retirements("103_fin.md") == SHARE.strip()


def test_70_two_retirements_in_one_fact_both_apply(tmp_path):
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE),
                              ("164_a.md", f"# a\nRETIRES: `103_*` \"{WRAPPED}\"\n"),
                              ("170_b.md", "# b\nRETIRES: `103_fin` \"The folder lives on the internal disk.\"\n")])
    assert _why(ret) == [] and sorted(r["b"] for r in ret["applied"]) == ["164_a.md", "170_b.md"]
    shown = ix.apply_retirements("103_fin.md")
    assert shown == ('# finance folder\n⟦RETIRED by 170_b.md: "The folder lives on the internal disk.…"⟧ '
                     f'`backup_memory.sh` ⟦RETIRED by 164_ind.md: "{WRAPPED[:60]}…"⟧ Nothing else copies it.'
                     ).replace("164_ind.md", "164_a.md")


def test_70_an_overlapping_retirement_by_the_later_name_is_unresolved(tmp_path):
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE),
                              ("164_a.md", f"# a\nRETIRES: `103_*` \"{WRAPPED}\"\n"),
                              ("170_b.md", "# b\nRETIRES: `103_*` \"but **only when the user says to back up.** Nothing\"\n")])
    assert [r["b"] for r in ret["applied"]] == ["164_a.md"]
    assert _why(ret) == [("170_b.md", 2, "overlaps 164_a.md's retirement in 103_fin.md")]
    assert "⟦RETIRED by 164_a.md" in ix.apply_retirements("103_fin.md")


def test_70_render_patch_shows_the_marker_and_not_the_passage(tmp_path):
    ix = _world(tmp_path, [("103_fin.md", SHARE), ("164_ind.md", f"# independent\nRETIRES: `103_*` \"{WRAPPED}\"\n")])
    r = ix.retrieve("finance folder internal disk", arm="bm25", floors=OPEN, k=1, char_budget=10 ** 6)
    assert [h[0] for h in r["hits"]] == ["103_fin.md"] and r["hits"][0][2].endswith("; full")
    patch = mr.render_patch(ix, r["hits"])
    assert f'⟦RETIRED by 164_ind.md: "{WRAPPED[:60]}…"⟧' in patch
    assert "only when the user says to back up" not in patch and "Nothing else copies it." in patch
    assert (tmp_path / "facts" / "103_fin.md").read_text() == SHARE            # on disk A is unchanged


def test_70_a_base_with_no_retires_lines_renders_byte_identical(tmp_path):
    """Positive control: no RETIRES line anywhere -> the patch is exactly order 42c's patch."""
    facts = [("103_fin.md", SHARE), ("77_dance.md", "# dance plan\nthe back alley dance\n"),
             ("78_roof.md", "# pivot\nTHIS SUPERSEDES THE DANCE PLAN IN `77_*`.\nrooftop\n")]
    ix = _world(tmp_path, facts)
    r = ix.retrieve("finance folder dance plan rooftop", arm="bm25", floors=OPEN, k=3, char_budget=10 ** 6)
    assert ix.ret == {"applied": [], "unresolved": []}
    expect = [mr.PATCH_HEADER]
    for fn, _s, _w in r["hits"]:
        warn = "".join(f"⚠ newer: `{b}` supersedes part of this — where they disagree, {b} wins.\n"
                       for b in ix.partial_of(fn))
        expect.append(f"# memory/facts/{fn}\n{warn}{ix.text_of(fn)}")
    assert mr.render_patch(ix, r["hits"]) == "\n\n".join(expect)
    for fn, text in facts:
        assert ix.apply_retirements(fn) == text.strip() == ix.text_of(fn)


def test_70_a_fact_other_than_a_is_untouched_when_b_retires_a(tmp_path):
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE), ("164_ind.md", f"# i\nRETIRES: `103_*` \"{WRAPPED}\"\n"),
                              ("50_other.md", "# other\n" + WRAPPED + "\n")])
    assert [r["a"] for r in ret["applied"]] == ["103_fin.md"]
    assert ix.apply_retirements("50_other.md") == ix.text_of("50_other.md")
    assert ix.apply_retirements("164_ind.md") == ix.text_of("164_ind.md")


def test_70_the_map_follows_the_files_on_refresh(tmp_path):
    ix, ret = _ret(tmp_path, [("103_fin.md", SHARE), ("164_ind.md", "# i\nnothing yet\n")])
    assert ret["applied"] == []
    (tmp_path / "facts" / "164_ind.md").write_text(f"# i\nRETIRES: `103_*` \"{WRAPPED}\"\n")
    ix.refresh()
    assert [r["b"] for r in ix.ret["applied"]] == ["164_ind.md"]


def test_70_retired_in_reads_the_dirs_and_never_raises(tmp_path, capsys):
    f = tmp_path / "facts"
    f.mkdir()
    (f / "103_fin.md").write_text(SHARE)
    (f / "164_ind.md").write_text(f"# i\nRETIRES: `103_*` \"{WRAPPED}\"\n")
    ret = mr.retired_in(str(f))
    assert [(r["a"], r["b"]) for r in ret["applied"]] == [("103_fin.md", "164_ind.md")]
    assert mr.apply_retirements("103_fin.md", SHARE, ret).startswith("# finance folder\nThe folder lives")
    assert "⟦RETIRED by 164_ind.md" in mr.apply_retirements("103_fin.md", SHARE, ret)
    assert mr.retired_in(str(tmp_path / "nope")) == {"applied": [], "unresolved": []}
    assert capsys.readouterr().out.count("no retirements map") == 1


# ------------------------------------------------------------------ ORDER 74: aliases, adaptive k, category boost

BAK = os.path.join(HERE, "memretrieve.py.bak.20261023_220109Z")    # memretrieve.py before order 74
BAK_SHA256 = "b026b88e83eacc2c6e1debad76567031f308be66e92c5bfdfebabf81610957d5"
NO_ALIASES = "/nonexistent/ALIASES.csv"


def _ix(tmp_path, facts, aliases=None, cats_txt=None, cats_py=None):
    """A fact base from (name, text) pairs; a sidecar from (file, aliases) rows; a category list."""
    f = tmp_path / "facts"
    f.mkdir(exist_ok=True)
    for name, text in facts:
        (f / name).write_text(text)
    side = NO_ALIASES
    if aliases is not None:
        side = tmp_path / "ALIASES.csv"
        side.write_text("file,aliases\n" + "".join(f"{a},{b}\n" for a, b in aliases))
    kw = {}
    if cats_txt is not None:
        p = tmp_path / "CATEGORIES.txt"
        p.write_text(cats_txt)
        kw = {"categories_txt": str(p), "assigned_path": str(tmp_path / "no_assigned.csv")}
    if cats_py is not None:
        kw["categories_py"] = cats_py
    stub = Stub()
    return mr.Index(facts_dir=str(f), cache_dir=str(tmp_path / "cache"), embedder=stub,
                    aliases_path=str(side), **kw), stub


def test_74_alias_line_is_parsed_from_the_first_15_lines_only():
    text = "# vault\n" + "x\n" * 5 + "Also called: volt; The Safe ;  ; anchor box\nbody\n"
    assert mr.aliases_in(text) == ["volt", "The Safe", "anchor box"]
    late = "# vault\n" + "x\n" * 15 + "Also called: volt\n"
    assert mr.aliases_in(late) == []
    assert mr.aliases_in("# v\nalso called volt\n") == []
    assert mr.aliases_in("") == []


def test_74_sidecar_merges_with_the_line_dedups_casefolded_and_lists_unknown_files(tmp_path):
    ix, _ = _ix(tmp_path, [("149_vault.md", "# vault\nAlso called: Volt; safe\nthe anchor vault\n"),
                           ("50_other.md", "# other\nnothing\n")],
                aliases=[("149_vault.md", "volt;strongbox"), ("999_gone.md", "ghost"), ("50_other.md", "")])
    ix.refresh()
    rep = ix.aliases_report()
    assert rep == {"applied": {"149_vault.md": ["Volt", "safe", "strongbox"]}, "unknown_files": ["999_gone.md"]}
    assert mr.aliases_report(str(tmp_path / "facts"), path=str(tmp_path / "ALIASES.csv")) == rep
    assert mr.aliases_report(str(tmp_path / "facts"), path=NO_ALIASES) == {
        "applied": {"149_vault.md": ["Volt", "safe"]}, "unknown_files": []}


def test_74_an_alias_raises_its_fact_for_an_alias_only_query_and_text_is_unchanged(tmp_path):
    facts = [("149_vault.md", "# The anchor vault\nroot owned anchor store, sealed hashes.\n"),
             ("50_volt.md", "# Electricity\nthe volt is a unit; a volt meter.\n"),
             ("60_x.md", "# x\nunrelated text here\n")]
    ix, _ = _ix(tmp_path, facts, aliases=[("149_vault.md", "volt")])
    q = "the volt"
    off = [h[0] for h in ix.retrieve(q, arm="bm25", floors=OPEN, k=1, arms=())["hits"]]
    assert off == ["50_volt.md"]
    on = ix.retrieve(q, arm="bm25", floors=OPEN, k=2, arms=("aliases",))["hits"]
    assert [h[0] for h in on] == ["149_vault.md", "50_volt.md"] or [h[0] for h in on] == ["50_volt.md", "149_vault.md"]
    assert on[[h[0] for h in on].index("149_vault.md")][1] > 0
    s_off, s_on = ix.bm25(q), ix.bm25(q, aliases=True)
    assert s_off["149_vault.md"] == 0.0 < s_on["149_vault.md"]
    assert ix.text_of("149_vault.md") == facts[0][1].strip()
    # +2 per alias token occurrence
    assert ix.atf["149_vault.md"] == {"volt": 2}
    assert "hybrid" in ix.retrieve(q, arm="hybrid", floors=OPEN, arms=("aliases",))["hits"][0][2]


def test_74_alias_vectors_reembed_on_alias_change_only(tmp_path):
    facts = [("149_vault.md", "# The anchor vault\nroot owned anchor store.\n"), ("60_x.md", "# x\nother\n")]
    ix, stub = _ix(tmp_path, facts, aliases=[("149_vault.md", "volt")])
    ix.refresh(need_vectors=True)
    assert stub.texts == 3                       # two facts + one alias chunk
    key1 = ix.avecs["149_vault.md"][0]
    assert key1 == hashlib.sha256("The anchor vault\nAlso called: volt".encode()).hexdigest()
    ix.refresh(need_vectors=True)
    assert stub.texts == 3                       # nothing changed, nothing embedded
    side = tmp_path / "ALIASES.csv"
    side.write_text("file,aliases\n149_vault.md,volt;strongbox\n")
    os.utime(side, ns=(1, 1))
    ix.refresh(need_vectors=True)
    assert stub.texts == 4 and ix.avecs["149_vault.md"][0] != key1
    assert "149_vault.md" in ix.vecs and ix.vecs["149_vault.md"].shape[0] == 1   # base vectors untouched
    side.write_text("file,aliases\n")
    os.utime(side, ns=(2, 2))
    ix.refresh(need_vectors=True)
    assert ix.avecs == {} and stub.texts == 4


def _adapt_world(tmp_path, n=8):
    # bm25 scores fall off in small steps: fact i has i copies of "kiwi" in a fixed-length body
    facts = [(f"{10 + i}_f.md", f"# f{i}\n" + "kiwi " * (n - i) + "pad " * i + "\n") for i in range(n)]
    ix, _ = _ix(tmp_path, facts)
    ix.refresh()
    return ix


def test_74_adapt_adds_heads_only_above_the_ratio_and_at_most_3(tmp_path, monkeypatch):
    ix = _adapt_world(tmp_path)
    base = ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=())
    s = ix.bm25("kiwi")
    order = sorted(s, key=lambda f: (-s[f], f))
    kth = s[order[2]]
    for ratio in (0.0, 0.97, 1.01):
        monkeypatch.setattr(mr, "ADAPT_RATIO", ratio)
        r = ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=("adapt",))
        assert r["hits"][:3] == base["hits"]
        extra = r["hits"][3:]
        want = [f for f in order[3:6] if s[f] >= ratio * kth]
        assert [h[0] for h in extra] == want
        assert len(extra) <= 3
        assert all(h[2].endswith("; head (close to #3)") for h in extra)
    monkeypatch.setattr(mr, "ADAPT_RATIO", 0.0)
    assert len(ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=("adapt",))["hits"]) == 6
    patch = mr.render_patch(ix, ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=("adapt",))["hits"])
    for f in order[3:6]:
        assert f"- memory/facts/{f} — " in patch and f"# memory/facts/{f}\n" not in patch


def test_74_adapt_respects_skip_and_is_off_when_fewer_than_k_picks(tmp_path, monkeypatch):
    ix = _adapt_world(tmp_path)
    monkeypatch.setattr(mr, "ADAPT_RATIO", 0.0)
    s = ix.bm25("kiwi")
    order = sorted(s, key=lambda f: (-s[f], f))
    r = ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=("adapt",), skip={order[3]})
    assert [h[0] for h in r["hits"][3:]] == order[4:7]
    r = ix.retrieve("kiwi", arm="bm25", floors=OPEN, k=3, arms=("adapt",), skip=set(order[2:]))
    assert [h[0] for h in r["hits"]] == order[:2]
    assert mr.ADAPT_MAX == 3


def test_74_adapt_ratio_constant_is_named():
    assert mr.ADAPT_RATIO == 0.97 and mr.CATEGORY_BOOST == 1.15
    assert mr.ALL_ARMS == ("aliases", "adapt", "catboost")
    assert set(mr.DEFAULT_ARMS) <= set(mr.ALL_ARMS)


CATS = """# fixture list
household finance | finance | #7bd8ff | budget, loan, car loans
research: finance | research | #5fb3d9 | crypto tests, terra
project: boats    | making   | #e07a5f | boat, dinghy
"""
CAT_FACTS = [("10_loan.md", "# loans\nCategory: household finance\nthe plan for the money.\n"),
             ("11_terra.md", "# terra\nCategory: research: finance\nthe plan for the money.\n"),
             ("12_none.md", "# none\nthe plan for the money.\n"),
             ("13_boat.md", "# hull\nCategory: project: boats\nthe plan for the money.\n")]


def test_74_catboost_distinctive_word_boosts_only_that_category(tmp_path):
    ix, _ = _ix(tmp_path, CAT_FACTS, cats_txt=CATS)
    cats = mr.distinctive_words(ix._categories()[0])
    assert "loan" in cats["household finance"] and "finance" not in cats["household finance"]
    s0 = ix.retrieve("loan money plan", arm="bm25", floors=OPEN, k=4, arms=())["hits"]
    s1 = ix.retrieve("loan money plan", arm="bm25", floors=OPEN, k=4, arms=("catboost",))["hits"]
    d0, d1 = {h[0]: h[1] for h in s0}, {h[0]: h[1] for h in s1}
    assert d1["10_loan.md"] == pytest.approx(d0["10_loan.md"] * mr.CATEGORY_BOOST, abs=1e-3)
    assert all(d1[f] == d0[f] for f in d0 if f != "10_loan.md")


def test_74_catboost_shared_word_boosts_nothing(tmp_path):
    ix, _ = _ix(tmp_path, CAT_FACTS, cats_txt=CATS)
    q = "finance money plan"            # "finance" is in two categories' names: not distinctive
    assert ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=("catboost",)) == \
        ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=())
    q = "money plan"                     # no category word at all
    assert ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=("catboost",)) == \
        ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=())


def test_74_catboost_loader_failure_is_no_boost_and_never_raises(tmp_path, capsys):
    ix, _ = _ix(tmp_path, CAT_FACTS, cats_txt=CATS, cats_py=str(tmp_path / "nope.py"))
    q = "loan money plan"
    assert ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=("catboost",)) == \
        ix.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=())
    assert "no category boost" in capsys.readouterr().out
    bad = tmp_path / "bad.py"
    bad.write_text("raise RuntimeError('boom')\n")
    (tmp_path / "b").mkdir()
    ix2, _ = _ix(tmp_path / "b", CAT_FACTS, cats_txt=CATS, cats_py=str(bad))
    assert ix2.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=("catboost",)) == \
        ix2.retrieve(q, arm="bm25", floors=OPEN, k=4, arms=())


def test_74_unknown_arm_is_refused(base):
    _f, ix, _s, _t = base
    with pytest.raises(ValueError):
        ix.retrieve("github", arm="bm25", floors=OPEN, arms=("nope",))


def _load_bak():
    import importlib.machinery, importlib.util
    assert hashlib.sha256(open(BAK, "rb").read()).hexdigest() == BAK_SHA256
    spec = importlib.util.spec_from_loader("_mr_before_74", importlib.machinery.SourceFileLoader("_mr_before_74", BAK))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("arm", ["bm25", "embed", "hybrid"])
def test_74_all_arms_off_is_byte_identical_to_before_order_74(tmp_path, arm):
    """Positive control: the pre-74 file (pinned by sha256) and arms=() give the same results."""
    old = _load_bak()
    facts = [("111_image_gen.md", "# Helix image generation\nlocal picture generator, stable diffusion.\n"),
             ("96_repo.md", "# Helix published\npublic github repository, MIT licence.\n"),
             ("76_boat.md", "# The boat\nboat weight and draft, foam core planing hull.\n"),
             ("50_long.md", "# A long one\n" + "attest attestation verifier " * 400),
             ("10_old.md", OLD), ("20_new.md", NEW),
             ("149_vault.md", "# vault\nAlso called: volt\nanchor vault\n")]
    ix_new, _ = _ix(tmp_path, facts, aliases=[("96_repo.md", "repo; the github")], cats_txt=CATS)
    ix_old = old.Index(facts_dir=str(tmp_path / "facts"), cache_dir=str(tmp_path / "cache_old"), embedder=Stub())
    for q in ["local picture generator", "the volt", "github repository boat", "zebra orchard",
              ["attest", "the boat draft"], "ok go ahead", "loan boat"]:
        for k, skip in ((5, ()), (2, {"96_repo.md"}), (3, ())):
            for fl in (OPEN, None, {"bm25": 0.5, "embed": 0.2}):
                a = ix_old.retrieve(q, k=k, arm=arm, floors=fl, skip=skip, char_budget=300)
                b = ix_new.retrieve(q, k=k, arm=arm, floors=fl, skip=skip, char_budget=300, arms=())
                assert repr(a) == repr(b), (q, k, skip, fl)
                assert old.render_patch(ix_old, a["hits"]) == mr.render_patch(ix_new, b["hits"])
