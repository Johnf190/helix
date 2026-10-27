"""Tests for attempt.py (order 28, item 24): the client Odin uses to log an attempt BEFORE it runs.

A small stdlib HTTP server stands in for Helix's POST /attempt here (it writes a scratch
ATTEMPTS.jsonl the way app.py does). The real route is tested against the real Flask app in
~/helix/test_attempt_route.py. Nothing here reads or writes the live ledger.

Run:  python3 -m pytest -q test_attempt.py
"""
import hashlib, http.server, json, os, socket, subprocess, sys, threading, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ATTEMPT = HERE / "attempt.py"
sys.path.insert(0, str(HERE))
import cycle  # noqa: E402


class FakeHelix(http.server.ThreadingHTTPServer):
    def __init__(self, ledger, status=200):
        self.ledger, self.status, self.bodies = Path(ledger), status, []
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.bodies.append(body)
        if self.path != "/attempt" or srv.status != 200:
            self.send_response(srv.status if srv.status != 200 else 404)
            self.end_headers()
            self.wfile.write(b'{"error": "no"}')
            return
        lines = srv.ledger.read_text().splitlines() if srv.ledger.exists() else []
        rec = dict(body, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), seq=len(lines) + 1,
                   prev=hashlib.sha256(lines[-1].encode()).hexdigest() if lines else "genesis")
        rec.setdefault("kind", "run")
        rec.setdefault("n", 1)
        with open(srv.ledger, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        out = json.dumps({"seq": rec["seq"], "ts": rec["ts"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def helix(tmp_path):
    srv = FakeHelix(tmp_path / "ATTEMPTS.jsonl")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def attempt(*args, cwd=None, env=None):
    return subprocess.run([sys.executable, str(ATTEMPT), *map(str, args)], capture_output=True,
                          text=True, cwd=cwd, env={**os.environ, **(env or {})}, timeout=60)


def test_run_with_helix_down_exits_5_and_never_runs_the_command(tmp_path):
    marker = tmp_path / "ran.txt"
    r = attempt("run", "--scope", "proj/a", "--url", f"http://127.0.0.1:{free_port()}", "--",
                "touch", marker)
    assert r.returncode == 5, (r.stdout, r.stderr)
    assert not marker.exists(), "the command ran without an attempt line"
    assert "not run" in r.stderr


def test_run_refused_by_helix_exits_5_and_never_runs_the_command(tmp_path):
    srv = FakeHelix(tmp_path / "A.jsonl", status=400)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        marker = tmp_path / "ran.txt"
        r = attempt("run", "--scope", "proj/a", "--url",
                    f"http://127.0.0.1:{srv.server_address[1]}", "--", "touch", marker)
        assert r.returncode == 5 and not marker.exists(), (r.stdout, r.stderr)
    finally:
        srv.shutdown()


def test_run_posts_first_then_execs_in_the_same_pid(helix, tmp_path):
    srv, url = helix
    out = tmp_path / "child.json"
    code = ("import json,os,sys; json.dump({'pid': os.getpid(), "
            "'seq': os.environ.get('HELIX_ATTEMPT_SEQ'), 'lines': "
            "len(open(sys.argv[2]).read().splitlines())}, open(sys.argv[1], 'w'))")
    r = attempt("run", "--scope", "proj/a", "--entry", "0005", "--kind", "seed", "--n", "10",
                "--url", url, "--", sys.executable, "-c", code, out, srv.ledger, cwd=tmp_path)
    assert r.returncode == 0, (r.stdout, r.stderr)
    child = json.loads(out.read_text())
    [line] = [json.loads(l) for l in srv.ledger.read_text().splitlines()]
    assert child["lines"] == 1                     # the line existed before the command started
    assert line["pid"] == child["pid"]             # logged pid == the job's pid
    assert child["seq"] == "1" and line["seq"] == 1 and line["prev"] == "genesis"
    assert line["scope"] == "proj/a" and line["entry"] == "0005" and line["kind"] == "seed"
    assert line["n"] == 10 and line["cwd"] == str(tmp_path)
    assert line["cmd"].startswith(sys.executable + " -c ")


def test_run_passes_the_commands_exit_code_through(helix):
    _, url = helix
    r = attempt("run", "--scope", "proj/a", "--url", url, "--", "sh", "-c", "exit 7")
    assert r.returncode == 7


def test_run_truncates_a_long_cmd_to_300(helix):
    srv, url = helix
    r = attempt("run", "--scope", "proj/a", "--url", url, "--", "echo", "x" * 1000)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(srv.ledger.read_text())["cmd"]) == 300


def test_run_needs_a_command():
    r = attempt("run", "--scope", "proj/a", "--url", "http://127.0.0.1:1")
    assert r.returncode == 2


def test_log_records_an_attempt_that_is_not_a_shell_command(helix, tmp_path):
    srv, url = helix
    r = attempt("log", "--scope", "proj/a", "--kind", "prompt", "--n", "3", "--cmd",
                "prompt variant B, temperature 0.7", "--url", url, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "seq 1" in r.stdout
    [line] = [json.loads(l) for l in srv.ledger.read_text().splitlines()]
    assert line["kind"] == "prompt" and line["n"] == 3 and "pid" not in line
    assert line["cwd"] == str(tmp_path)


def test_log_with_helix_down_exits_5():
    r = attempt("log", "--scope", "proj/a", "--kind", "run", "--cmd", "x", "--url",
                f"http://127.0.0.1:{free_port()}")
    assert r.returncode == 5


def test_list_reads_the_ledger_and_checks_the_chain(helix, tmp_path):
    srv, url = helix
    for scope in ("proj/a", "proj/b", "proj/a"):
        assert attempt("log", "--scope", scope, "--kind", "variant", "--cmd", "v",
                       "--url", url).returncode == 0
    env = {"HELIX_CYCLE_DIR": str(tmp_path)}
    r = attempt("list", env=env)
    assert r.returncode == 0, r.stderr
    rows = [l for l in r.stdout.splitlines() if l.startswith("seq ")]
    assert len(rows) == 3 and "chain OK (3 line(s))" in r.stdout
    r = attempt("list", "--scope", "proj/a", env=env)
    assert len([l for l in r.stdout.splitlines() if l.startswith("seq ")]) == 2
    lines = srv.ledger.read_text().splitlines()
    lines[0] = lines[0].replace("variant", "prompt")
    srv.ledger.write_text("\n".join(lines) + "\n")
    r = attempt("list", env=env)
    assert r.returncode == 4 and "chain BROKEN at seq 2" in r.stdout
