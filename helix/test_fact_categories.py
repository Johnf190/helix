#!/usr/bin/env python3
"""ORDER 65 Part C — /memory and /blueprint prefer a fact's declared category.

FACTS is pointed at a temp dir (2 facts declare a category, 2 do not). Nothing here reads or
writes the live memory/facts. GOLDEN is the output of the two routes on this exact FIXTURE,
captured from app.py BEFORE this order's edit (sha256 006bc7f2f311804d49b034dcd02d39192501e441fc9b1e9a93d256d488c40ea8,
kept as app.py.bak.20261006_000229). With the categories module broken, both routes must
return exactly that.

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_fact_categories.py
"""
import os
import sys
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402

FIXTURE = {
    "02_example_9ecf.md": "# Ratio memory lemma (2026-07-02)\nCategory: research: math · rules\n\n**Status:** proven, holds.\n",
    "05_example_fb2b.md": "# probe-floor result 2026-07-05\n\n**Status:** floor holds. cites 02_example_9ecf.md\n",
    "110_example_68cd.md": "# Boat printed skeleton frames 2026-09-30\nCategory: project: boats\n<!-- blueprint: project=attest status=pass -->\nframes printed.\n",
    "20_misc_note.md": "# A misc note\nnothing special. see 05_example_fb2b.md\n",
}

GOLDEN = {'blueprint': {'counts': {'correction': 0,
                          'edges': 2,
                          'kill': 0,
                          'open': 1,
                          'pass': 3,
                          'prior_art': 0,
                          'total': 4,
                          'unsure': 0},
               'days': ['2026-07-02', '2026-07-05', '2026-09-30'],
               'edges': [{'from': '02_example_9ecf.md', 'to': '05_example_fb2b.md'},
                         {'from': '05_example_fb2b.md', 'to': '20_misc_note.md'}],
               'nodes': [{'cited_by': ['05_example_fb2b.md'],
                          'cites': [],
                          'color': '#eab308',
                          'confident': True,
                          'date': '2026-07-02',
                          'file': '02_example_9ecf.md',
                          'origin': 'box1',
                          'project': 'MATH',
                          'seq': 2,
                          'snip': 'Ratio memory lemma (2026-07-02) Category: research: math · '
                                  'rules **Status:** proven, holds.',
                          'status_line': 'Status: proven, holds.',
                          'title': 'Ratio memory lemma (2026-07-02)',
                          'verdict': 'pass',
                          'vlabel': 'HOLDS',
                          'vsrc': 'status',
                          'words': 12},
                         {'cited_by': ['20_misc_note.md'],
                          'cites': ['02_example_9ecf.md'],
                          'color': '#ff6b81',
                          'confident': True,
                          'date': '2026-07-05',
                          'file': '05_example_fb2b.md',
                          'origin': 'box1',
                          'project': 'probe-study',
                          'seq': 5,
                          'snip': 'probe-floor result 2026-07-05 **Status:** floor holds. cites '
                                  '02_example_9ecf.md',
                          'status_line': 'Status: floor holds. cites 02_example_9ecf.md',
                          'title': 'probe-floor result 2026-07-05',
                          'verdict': 'pass',
                          'vlabel': 'HOLDS',
                          'vsrc': 'status',
                          'words': 8},
                         {'cited_by': [],
                          'cites': [],
                          'color': '#4be3a0',
                          'confident': True,
                          'date': '2026-09-30',
                          'file': '110_example_68cd.md',
                          'origin': 'box1',
                          'project': 'ATTEST',
                          'seq': 110,
                          'snip': 'Boat printed skeleton frames 2026-09-30 Category: project: '
                                  'boats <!-- blueprint: project=attest status=pass --> frames '
                                  'printed.',
                          'status_line': '',
                          'title': 'Boat printed skeleton frames 2026-09-30',
                          'verdict': 'pass',
                          'vlabel': 'HOLDS',
                          'vsrc': 'tag',
                          'words': 15},
                         {'cited_by': [],
                          'cites': ['05_example_fb2b.md'],
                          'color': '#9aa4b2',
                          'confident': True,
                          'date': '2026-07-02',
                          'file': '20_misc_note.md',
                          'origin': 'box1',
                          'project': 'GENERAL',
                          'seq': 20,
                          'snip': 'A misc note nothing special. see 05_example_fb2b.md',
                          'status_line': '',
                          'title': 'A misc note',
                          'verdict': 'open',
                          'vlabel': 'OPEN',
                          'vsrc': 'default',
                          'words': 7}],
               'projects': [{'color': '#eab308', 'count': 1, 'name': 'MATH'},
                            {'color': '#ff6b81', 'count': 1, 'name': 'probe-study'},
                            {'color': '#4be3a0', 'count': 1, 'name': 'ATTEST'},
                            {'color': '#9aa4b2', 'count': 1, 'name': 'GENERAL'}]},
 'memory': {'cats': [{'color': '#22d3ee',
                      'files': [{'file': '05_example_fb2b.md', 'title': 'probe-floor result 2026-07-05'}],
                      'name': 'AI SAFETY'},
                     {'color': '#eab308',
                      'files': [{'file': '02_example_9ecf.md',
                                 'title': 'Ratio memory lemma (2026-07-02)'}],
                      'name': 'PROVEN MATH'},
                     {'color': '#9aa4b2',
                      'files': [{'file': '110_example_68cd.md',
                                 'title': 'Boat printed skeleton frames 2026-09-30'},
                                {'file': '20_misc_note.md', 'title': 'A misc note'}],
                      'name': 'MISC'}]}}

LIST = {line.split("|")[0].strip(): line.split("|")[2].strip()
        for line in (HELIX / "workspace" / "categories" / "CATEGORIES.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")}


@pytest.fixture
def client(tmp_path, monkeypatch):
    d = tmp_path / "facts"
    d.mkdir()
    for k, v in FIXTURE.items():
        (d / k).write_text(v)
    (d / "README.txt").write_text("not a fact")
    monkeypatch.setattr(app, "FACTS", str(d))
    return app.app.test_client()


@pytest.fixture
def broken(tmp_path, monkeypatch):
    p = tmp_path / "categories.py"
    p.write_text("def load(:\n")                         # a syntax error: the module cannot load
    monkeypatch.setattr(app, "FACT_CATEGORIES_PY", str(p))


ORIGINAL_16 = ["personal", "household finance", "rules", "helix", "research: math",
               "research: probe-study", "research: attest", "research: framework",
               "research: finance", "research: neural", "project: boats", "project: mapapp",
               "project: friend", "project: music", "project: musicapp", "project: sidekick"]


def test_the_real_module_loads():
    mod, cats = app._fact_categories()
    # the list grows without a build order (180_*); a name is never deleted (order 76c ruling 2)
    names = [c.name for c in cats]
    assert mod is not None and len(cats) >= 16
    assert set(ORIGINAL_16) <= set(names)
    assert len(names) == len(set(names))


def test_memory_declared_grouped_in_list_order(client):
    r = client.get("/memory")
    assert r.status_code == 200
    cats = r.get_json()["cats"]
    assert [c["name"] for c in cats] == ["research: math", "project: boats", "AI SAFETY", "MISC"]
    by = {c["name"]: c for c in cats}
    assert by["research: math"]["color"] == LIST["research: math"]
    assert by["project: boats"]["color"] == LIST["project: boats"]
    assert [n["file"] for n in by["research: math"]["files"]] == ["02_example_9ecf.md"]
    assert [n["file"] for n in by["project: boats"]["files"]] == ["110_example_68cd.md"]
    # undeclared exactly as before: the hand list, then MISC
    assert [n["file"] for n in by["AI SAFETY"]["files"]] == ["05_example_fb2b.md"]
    assert [n["file"] for n in by["MISC"]["files"]] == ["20_misc_note.md"]
    assert "PROVEN MATH" not in by                       # its one fact declared itself elsewhere
    src = {n["file"]: n["cat_source"] for c in cats for n in c["files"]}
    assert src == {"02_example_9ecf.md": "declared",
                   "110_example_68cd.md": "declared",
                   "05_example_fb2b.md": "filename-guess", "20_misc_note.md": "filename-guess"}


def test_blueprint_declared_wins_over_tag_and_filename(client):
    r = client.get("/blueprint")
    assert r.status_code == 200
    nodes = {n["file"]: n for n in r.get_json()["nodes"]}
    gold = {n["file"]: n for n in GOLDEN["blueprint"]["nodes"]}
    boat = nodes["110_example_68cd.md"]
    assert gold["110_example_68cd.md"]["project"] == "ATTEST"   # the tag, before
    assert (boat["project"], boat["color"], boat["cat_source"]) == \
        ("PROJECT: BOATS", LIST["project: boats"], "declared")
    ratio = nodes["02_example_9ecf.md"]
    assert (ratio["project"], ratio["color"], ratio["cat_source"]) == \
        ("RESEARCH: MATH", LIST["research: math"], "declared")
    for f in ("05_example_fb2b.md", "20_misc_note.md"):       # undeclared: as before, plus cat_source
        assert nodes[f]["cat_source"] == "filename-guess"
        assert {k: v for k, v in nodes[f].items() if k != "cat_source"} == gold[f]
    # everything but the project fields is untouched for the declared ones too
    for f in ("110_example_68cd.md", "02_example_9ecf.md"):
        keep = lambda n: {k: v for k, v in n.items() if k not in ("project", "color", "cat_source")}
        assert keep(nodes[f]) == keep(gold[f])


def test_blueprint_tag_source_recorded(client, tmp_path):
    (tmp_path / "facts" / "30_tagged.md").write_text("# t\n<!-- blueprint: project=neural -->\n")
    nodes = {n["file"]: n for n in client.get("/blueprint").get_json()["nodes"]}
    assert (nodes["30_tagged.md"]["project"], nodes["30_tagged.md"]["cat_source"]) == ("NEURAL", "tag")


def test_broken_module_memory_identical_to_before(client, broken):
    assert app._fact_categories() == (None, None)
    r = client.get("/memory")
    assert r.status_code == 200
    assert r.get_json() == GOLDEN["memory"]


def test_broken_module_blueprint_identical_to_before(client, broken):
    r = client.get("/blueprint")
    assert r.status_code == 200
    assert r.get_json() == GOLDEN["blueprint"]


def test_missing_module_identical_to_before(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "FACT_CATEGORIES_PY", str(tmp_path / "no_such.py"))
    assert client.get("/memory").get_json() == GOLDEN["memory"]
    assert client.get("/blueprint").get_json() == GOLDEN["blueprint"]


# ── ORDER 73b Part B: facts with no declared line take their category from ASSIGNED.csv ────
# Every test here points app.FACT_ASSIGNED at tmp_path (absent unless a test writes it), so the
# results do not change after Odin runs `categories.py assign` for real.
LIVE_ASSIGNED = HELIX / "workspace" / "categories" / "ASSIGNED.csv"


@pytest.fixture(autouse=True)
def _no_live_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "FACT_ASSIGNED", str(tmp_path / "ASSIGNED.csv"))


def write_assigned(tmp_path, rows):
    lines = ["file,primary,secondary,source"] + [",".join(r) for r in rows]
    (tmp_path / "ASSIGNED.csv").write_text("\n".join(lines) + "\n")


def test_assigned_path_is_the_sidecar_beside_the_module():
    assert Path(app.FACT_CATEGORIES_PY).parent / "ASSIGNED.csv" == LIVE_ASSIGNED


def test_memory_places_assigned_facts(client, tmp_path):
    (tmp_path / "facts" / "30_bad.md").write_text("# Bad line\nCategory: wizardry\n")
    write_assigned(tmp_path, [["05_example_fb2b.md", "research: probe-study", "", "proposal"],
                              ["30_bad.md", "project: boats", "", "override"],
                              ["02_example_9ecf.md", "personal", "", "proposal"]])  # declared wins
    cats = client.get("/memory").get_json()["cats"]
    assert [c["name"] for c in cats] == ["research: math", "research: probe-study", "project: boats", "MISC"]
    by = {c["name"]: c for c in cats}
    assert by["research: probe-study"]["color"] == LIST["research: probe-study"]
    assert [n["file"] for n in by["project: boats"]["files"]] == ["110_example_68cd.md",
                                                                   "30_bad.md"]
    src = {n["file"]: n["cat_source"] for c in cats for n in c["files"]}
    assert src == {"02_example_9ecf.md": "declared", "05_example_fb2b.md": "assigned",
                   "110_example_68cd.md": "declared", "30_bad.md": "assigned",
                   "20_misc_note.md": "filename-guess"}


def test_blueprint_places_assigned_facts(client, tmp_path):
    write_assigned(tmp_path, [["05_example_fb2b.md", "research: probe-study", "rules", "proposal"],
                              ["110_example_68cd.md", "personal", "", "proposal"]])
    nodes = {n["file"]: n for n in client.get("/blueprint").get_json()["nodes"]}
    cap = nodes["05_example_fb2b.md"]
    assert (cap["project"], cap["color"], cap["cat_source"]) == \
        ("RESEARCH: probe-study", LIST["research: probe-study"], "assigned")
    boat = nodes["110_example_68cd.md"]                  # declared beats assigned
    assert (boat["project"], boat["cat_source"]) == ("PROJECT: BOATS", "declared")
    assert nodes["20_misc_note.md"]["cat_source"] == "filename-guess"


def test_assigned_absent_is_as_before(client, tmp_path):
    assert not (tmp_path / "ASSIGNED.csv").exists()
    src = {n["file"]: n["cat_source"] for c in client.get("/memory").get_json()["cats"] for n in c["files"]}
    assert "assigned" not in src.values()


def test_no_test_run_opens_the_live_assigned_csv(tmp_path):
    # Re-runs the app, mindgraph and categories tests under an audit hook that records every open()
    # of the live ASSIGNED.csv path, read or write, existing or not. A positive control first.
    import subprocess
    log = tmp_path / "opened.txt"
    hook = ("import sys\n"
            f"LIVE, LOG = {str(LIVE_ASSIGNED)!r}, {str(log)!r}\n"
            "def _h(ev, args):\n"
            "    if ev == 'open' and args and isinstance(args[0], (str, bytes)) and "
            "__import__('os').path.abspath(__import__('os').fsdecode(args[0])) == LIVE:\n"
            "        open(LOG, 'a').write('opened\\n')\n"
            "sys.addaudithook(_h)\n")
    control = hook + "try:\n    open(LIVE)\nexcept OSError:\n    pass\n"
    subprocess.run([sys.executable, "-c", control], check=True)
    assert log.read_text() == "opened\n", "the hook must see an open of the live path"
    log.unlink()
    files = [str(HELIX / "test_fact_categories.py"), str(HELIX / "test_mindgraph.py"),
             str(HELIX / "workspace" / "categories" / "test_categories.py")]
    run = hook + ("import pytest\n"
                  f"sys.exit(pytest.main({files!r} + ['-q', '-p', 'no:cacheprovider', "
                  "'-k', 'not test_no_test_run_opens_the_live_assigned_csv']))\n")
    r = subprocess.run([sys.executable, "-c", run], cwd=str(HELIX), capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert " passed" in r.stdout
    assert not log.exists(), "a test opened the live ASSIGNED.csv"
