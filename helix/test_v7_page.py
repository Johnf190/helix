#!/usr/bin/env python3
"""ORDER 58 Part B — the v7 page and its three read-only routes.

Run:  .venv/bin/python -m pytest -q test_v7_page.py
"""
import os
import re
from pathlib import Path

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"

import pytest  # noqa: E402

import app  # noqa: E402

HERE = Path(__file__).resolve().parent
V6 = HERE / "static" / "index_v6.html"
V7 = HERE / "static" / "index_v7.html"

# Every placeholder string from the order's appendix. "HP workstation" is allowed only when read from a
# live source; no route reports the machine model, so it must not be in the file at all.
PLACEHOLDERS = [
    "1 402 993", "5 / 6", "ORDER 58", "alice desk · lock settings dir", "ORDER 59",
    "I don't like the entity", "Understood — no creature", "Audit: the chat has to stay",
    "Queue is clear", 'turn <b class="num">261</b>', "1 508", "HP workstation", "WORK=[", "FACTS=[",
    'Sign-in</span><span class="v num">0.8 h',
]

# v6 endpoints v7 deliberately does not call, each with its reason.
MATCH_EXCEPTIONS = {
    # v6's Live Workspace module was removed from its rail (the user, 2026-08-30): pollWorkspace()
    # returns before fetching when #ws-tree is absent, so v6 never calls this either.
    "/api/workspace": "dead in v6 — no #ws-tree, pollWorkspace() returns before fetching",
}

FETCH = re.compile(r"""fetch\(\s*(['"`])([^'"`]*)\1""")


def endpoints(text):
    """The path literal at the start of every fetch( call: '/fact/'+name → '/fact/'."""
    return {m.group(2) for m in FETCH.finditer(text)}


@pytest.fixture(scope="module")
def client():
    return app.app.test_client()


@pytest.fixture(scope="module")
def v7():
    return V7.read_text()


def test_v7_route(client, v7):
    r = client.get("/v7")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == v7


def test_api_mind(client):
    r = client.get("/api/mind")
    assert r.status_code == 200
    j = r.get_json()
    on_disk = len(list((HERE / "memory" / "facts").glob("*.md")))
    assert len(j["p"]) == len(j["k"]) == j["diagnostics"]["nodes"] == on_disk
    assert all(a < b for a, b in j["e"])


def test_api_mind_recalls(client):
    r = client.get("/api/mind/recalls")
    assert r.status_code == 200
    j = r.get_json()
    assert isinstance(j["recalls"], list) and len(j["recalls"]) <= app.MIND_RECALLS_TAIL
    assert isinstance(j["skipped"], int)
    r = client.get("/api/mind/recalls?since=2026-10-04T00:00:00Z")
    assert r.status_code == 200
    assert client.get("/api/mind/recalls?since=not-a-time").status_code == 400


def test_api_queue(client):
    r = client.get("/api/queue")
    assert r.status_code == 200
    j = r.get_json()
    assert {"queue", "live", "running"} <= set(j)
    assert isinstance(j["running"], list)


def test_new_routes_are_get_only(client):
    for u in ("/api/mind", "/api/mind/recalls", "/api/queue", "/v7"):
        assert client.post(u).status_code == 405, u


def test_v6_untouched_and_still_served(client):
    assert client.get("/v6").get_data(as_text=True) == V6.read_text()


def test_wiring_attest(v7):
    v6_eps = endpoints(V6.read_text())
    assert len(v6_eps) >= 15, v6_eps                     # the regex really reads v6's calls
    missing = v6_eps - endpoints(v7) - set(MATCH_EXCEPTIONS)
    assert not missing, f"v6 endpoints absent from v7: {sorted(missing)}"
    assert set(MATCH_EXCEPTIONS) <= v6_eps                # every exception names a real v6 call


def test_v7_reads_the_new_routes(v7):
    eps = endpoints(v7)
    for ep in ("/api/mind", "/api/mind/recalls", "/api/queue", "/stat", "/metrics"):
        assert ep in eps or any(e.startswith(ep) for e in eps), ep


def test_no_external_urls(v7):
    # strict: not even the SVG namespace (the page reads it off the inline <svg>'s namespaceURI)
    for bad in ("http://", "https://", "//cdn"):
        hits = [ln[:120] for ln in v7.splitlines() if bad in ln]
        assert not hits, (bad, hits[:3])


def test_no_mock_leftovers(v7):
    found = [s for s in PLACEHOLDERS if s in v7]
    assert not found, found


def test_no_random_firing(v7):
    assert "pickSeed" not in v7


def test_snapshot_kept_as_fallback(v7):
    assert "const MIND_SNAPSHOT=" in v7
    assert 'id="snaptag"' in v7


def test_exit_controls(v7):
    assert "window.close()" in v7
    assert "Press Alt+F4 to close" in v7
    assert "Helix keeps running, the desktop icon reopens it." in v7
    assert "F11" in v7
