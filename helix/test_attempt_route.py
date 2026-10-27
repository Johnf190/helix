"""Tests for ORDER 28 — POST /attempt, the only way to write workspace/cycle/ATTEMPTS.jsonl (item 24).

Every test points app.ATTEMPTS_LEDGER at tmp_path. Importing app leaves it None (only the
server process writes the live ledger), and one test asserts that. **Nothing here writes the
live ATTEMPTS.jsonl.**

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_attempt_route.py
"""
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402

CYCLE = HELIX / "workspace" / "cycle" / "cycle.py"
ATTEMPT = HELIX / "workspace" / "cycle" / "attempt.py"
REGATE = HELIX / "workspace" / "regate" / "regate.py"
LIVE = HELIX / "workspace" / "cycle" / "ATTEMPTS.jsonl"
LIVE_AT_IMPORT = LIVE.read_text().splitlines() if LIVE.exists() else []
GOOD = {"scope": "proj/a", "kind": "seed", "n": 10, "cmd": "python3 train.py --seeds 0-9",
        "cwd": "/work/exp", "pid": 4242}


@pytest.fixture
def led(tmp_path, monkeypatch):
    p = tmp_path / "ATTEMPTS.jsonl"
    monkeypatch.setattr(app, "ATTEMPTS_LEDGER", str(p))
    return p


@pytest.fixture
def client():
    return app.app.test_client()


def post(client, body, addr="127.0.0.1", headers=None, raw=None):
    kw = {"data": raw, "content_type": "application/json"} if raw is not None else {"json": body}
    return client.post("/attempt", environ_base={"REMOTE_ADDR": addr}, headers=headers or {}, **kw)


def lines(p):
    return Path(p).read_text().splitlines() if Path(p).exists() else []


def test_the_ledger_is_off_when_app_is_imported():
    assert app.ATTEMPTS_LEDGER is None


def test_a_valid_post_is_seq_1_with_prev_genesis(led, client):
    r = post(client, GOOD)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert set(out) == {"seq", "ts"} and out["seq"] == 1 and out["ts"].endswith("Z")
    [raw] = lines(led)
    rec = json.loads(raw)
    assert rec == dict(GOOD, ts=out["ts"], seq=1, prev="genesis")


def test_a_second_post_chains_to_the_first(led, client):
    post(client, GOOD)
    r = post(client, {"scope": "proj/a", "cmd": "x", "cwd": "/w"})
    assert r.get_json()["seq"] == 2
    first, second = lines(led)
    rec = json.loads(second)
    assert rec["prev"] == hashlib.sha256(first.encode("utf-8")).hexdigest()
    assert rec["kind"] == "run" and rec["n"] == 1 and "pid" not in rec and "entry" not in rec


@pytest.mark.parametrize("body", [
    {k: v for k, v in GOOD.items() if k != "scope"},             # missing scope
    dict(GOOD, scope="Proj/A"), dict(GOOD, scope=""), dict(GOOD, scope="a//b"), dict(GOOD, scope=7),
    dict(GOOD, kind="retry"), dict(GOOD, n=0), dict(GOOD, n=-1), dict(GOOD, n=True),
    dict(GOOD, n="2"), dict(GOOD, n=1.5), dict(GOOD, cmd="x" * 301), dict(GOOD, cmd=""),
    {k: v for k, v in GOOD.items() if k != "cmd"}, dict(GOOD, cwd="relative/dir"),
    {k: v for k, v in GOOD.items() if k != "cwd"}, dict(GOOD, pid=0), dict(GOOD, pid="12"),
    dict(GOOD, entry="5"), dict(GOOD, entry="0005-p1"),
    dict(GOOD, ts="2020-01-01T00:00:00Z"), dict(GOOD, seq=1), dict(GOOD, prev="genesis"),
    dict(GOOD, extra=1), [GOOD], "scope"])
def test_a_bad_body_is_400_and_nothing_is_written(led, client, body):
    r = post(client, body)
    assert r.status_code == 400, (body, r.get_data(as_text=True))
    assert "error" in r.get_json()
    assert not led.exists()


def test_not_json_is_400_and_nothing_is_written(led, client):
    assert post(client, None, raw="scope=proj/a").status_code == 400
    assert not led.exists()


def test_cmd_of_exactly_300_and_an_entry_id_are_accepted(led, client):
    r = post(client, dict(GOOD, cmd="x" * 300, entry="0005"))
    assert r.status_code == 200
    assert json.loads(lines(led)[0])["entry"] == "0005"


@pytest.mark.parametrize("addr", ["192.168.77.19", "100.101.102.103", "10.0.0.2", "::ffff:127.0.0.1"])
def test_a_non_loopback_client_is_403_and_nothing_is_written(led, client, addr):
    r = post(client, GOOD, addr=addr)
    assert r.status_code == 403
    assert not led.exists()


def test_ipv6_loopback_is_allowed(led, client):
    assert post(client, GOOD, addr="::1").status_code == 200


def test_the_phone_face_through_tailscale_is_403(led, client):
    """`tailscale serve` proxies from 127.0.0.1, so remote_addr is loopback there too. It adds
    X-Forwarded-For and Tailscale-User-Login, which pass the mobile gate for the user's device."""
    hdrs = {"X-Forwarded-For": "100.101.102.103",
            "Tailscale-User-Login": "you@example.com"}
    r = post(client, GOOD, headers=hdrs)
    assert r.status_code == 403, r.get_data(as_text=True)
    hdrs = {"X-Forwarded-For": "100.101.102.103", "Cookie": f"hxm={app.mobile_token()}"}
    assert post(client, GOOD, headers=hdrs).status_code == 403
    assert post(client, GOOD, headers={"Tailscale-User-Login": "x@y"}).status_code == 403
    assert not led.exists()


def test_only_post_is_routed(led, client):
    assert client.get("/attempt", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 405
    for m in (client.put, client.delete, client.patch):
        assert m("/attempt", json=GOOD, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 405
    assert not led.exists()


def test_no_other_route_touches_the_ledger():
    names = ("ATTEMPTS_LEDGER", "_append_attempt")
    touching = sorted(rule.rule for rule in app.app.url_map.iter_rules()
                      if any(n in app.app.view_functions[rule.endpoint].__code__.co_names
                             for n in names))
    assert touching == ["/attempt"]
    src = (HELIX / "app.py").read_text()
    assert src.count("_append_attempt(") == 2          # its def and the route's one call


def test_a_write_error_is_500_logged_and_writes_nothing(tmp_path, monkeypatch, client, capsys):
    monkeypatch.setattr(app, "ATTEMPTS_LEDGER", str(tmp_path / "no" / "such" / "A.jsonl"))
    r = post(client, GOOD)
    assert r.status_code == 500 and "error" in r.get_json()
    assert "HELIX: [attempt] could not append" in capsys.readouterr().out
    assert not (tmp_path / "no").exists()


def test_with_the_ledger_off_nothing_is_written(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "ATTEMPTS_LEDGER", None)
    assert post(client, GOOD).status_code == 503


def test_concurrent_posts_make_one_unbroken_chain(led, client):
    def go():
        c = app.app.test_client()
        for _ in range(5):
            assert post(c, GOOD).status_code == 200
    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    recs = [json.loads(l) for l in lines(led)]
    assert [r["seq"] for r in recs] == list(range(1, 41))
    sys.path.insert(0, str(CYCLE.parent))
    import cycle
    got, broken = cycle.read_attempts(led)
    assert broken is None and len(got) == 40


def k(led, scope="proj/a"):
    import time
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2))   # ts < until, strictly
    return subprocess.run([sys.executable, str(CYCLE), "k", "--scope", scope, "--until", until],
                          capture_output=True, text=True,
                          env={**os.environ, "HELIX_CYCLE_DIR": str(led.parent)})


def test_cycle_k_counts_what_the_route_wrote_and_catches_an_edited_middle_line(led, client):
    post(client, GOOD)
    post(client, dict(GOOD, n=1, pid=4343))
    post(client, dict(GOOD, n=2, pid=4444))
    r = k(led)
    assert r.returncode == 0 and r.stdout.startswith("K=13 (since "), r.stdout + r.stderr
    raw = lines(led)
    raw[1] = raw[1].replace('"n": 1', '"n": 0')                # quietly un-count one attempt
    led.write_text("\n".join(raw) + "\n")
    r = k(led)
    assert r.returncode == 4 and r.stdout.strip() == "K unverifiable (chain broken at seq 3)"


def test_the_scope_rule_is_regates(tmp_path):
    spec = importlib.util.spec_from_file_location("regate_for_scope", REGATE)
    rg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rg)
    assert app._ATTEMPT_SCOPE.pattern == rg.SCOPE.pattern
    for s in ("proj/a", "crosscheck/attest", "helix/selftest", "a", "Proj", "", "a/", "/a", 3, None,
              "a b", "a/b.c-d_e"):
        assert (app._attempt_scope_problem(s) is None) == (rg.scope_problem(s) is None), s


# ------------------------------------------------------------------ end to end: attempt.py -> served app

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def served(led):
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app.app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_attempt_run_with_the_app_down_exits_5_and_runs_nothing(tmp_path):
    marker = tmp_path / "ran.txt"
    r = subprocess.run([sys.executable, str(ATTEMPT), "run", "--scope", "proj/a", "--url",
                        f"http://127.0.0.1:{_free_port()}", "--", "touch", str(marker)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 5 and not marker.exists(), (r.stdout, r.stderr)


def test_attempt_run_through_the_real_route_logs_the_childs_pid(served, led, tmp_path):
    out = tmp_path / "child.txt"
    r = subprocess.run([sys.executable, str(ATTEMPT), "run", "--scope", "proj/a", "--url", served,
                        "--", sys.executable, "-c",
                        "import os,sys; open(sys.argv[1],'w').write(str(os.getpid()) + ' ' + "
                        "os.environ['HELIX_ATTEMPT_SEQ'])", str(out)],
                       capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    pid, seq = out.read_text().split()
    [rec] = [json.loads(l) for l in lines(led)]
    assert rec["pid"] == int(pid) and rec["seq"] == int(seq) == 1 and rec["prev"] == "genesis"
    assert rec["cwd"] == str(tmp_path)


def test_the_live_ledger_was_not_touched_by_this_suite():
    """Every write above went to tmp_path. The live ledger is compared with its state when this
    module was imported: anything appended since (by the live Helix, for Odin) must not be ours."""
    assert app.ATTEMPTS_LEDGER is None
    now = LIVE.read_text().splitlines() if LIVE.exists() else []
    assert now[:len(LIVE_AT_IMPORT)] == LIVE_AT_IMPORT
    assert not [l for l in now[len(LIVE_AT_IMPORT):] if '"proj/a"' in l]
