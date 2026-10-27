"""Tests for cycle.py and gpu_sample.py (order 27, item 16). Scratch ledgers only.

Run:  python3 -m pytest -q test_cycle.py
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cycle  # noqa: E402
import gpu_sample  # noqa: E402

CYCLE = HERE / "cycle.py"
SAMPLER = HERE / "gpu_sample.py"


def z(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def jl(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


# ------------------------------------------------------------------ time

def test_parse_time_reads_z_offset_and_naive_local(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    try:
        assert cycle.parse_time("2026-10-25T01:30:00").timestamp() == \
            cycle.parse_time("2026-10-25T00:30:00Z").timestamp()
        assert cycle.parse_time("2026-10-25T03:00:00").timestamp() == \
            cycle.parse_time("2026-10-25T03:00:00Z").timestamp()
        assert cycle.parse_time("2026-10-01T21:08:43+01:00").timestamp() == \
            cycle.parse_time("2026-10-01T20:08:43Z").timestamp()
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_utc_z_and_durations():
    assert cycle.utc_z(1792888200) == "2026-10-25T00:30:00Z"
    assert [cycle.fmt_dur(s) for s in (0, 38, 458, 3720, 90061)] == \
        ["0s", "38s", "7m38s", "1h02m", "25h01m"]


# ------------------------------------------------------------------ Part B: one build's stream log

RESULT = {"type": "result", "subtype": "success", "num_turns": 62, "total_cost_usd": 4.347333,
          "usage": {"input_tokens": 120, "output_tokens": 89069,
                    "cache_read_input_tokens": 6469325, "cache_creation_input_tokens": 158951}}


def test_run_stats_takes_the_last_result_line(tmp_path):
    s = tmp_path / "s.jsonl"
    s.write_text('{"type":"system"}\nnot json at all\n'
                 + json.dumps(dict(RESULT, num_turns=1, total_cost_usd=0.01)) + "\n"
                 + '{"type":"assistant"}\n' + json.dumps(RESULT) + "\nbuilder_run: tail text\n")
    assert cycle.run_stats(s) == {"cost_usd": 4.347333, "turns": 62,
                                  "tokens": {"input": 120, "output": 89069,
                                             "cache_read": 6469325, "cache_write": 158951}}


def test_run_stats_missing_fields_are_absent_never_zero(tmp_path):
    s = tmp_path / "s.jsonl"
    jl(s, [{"type": "result", "num_turns": 3, "usage": {"output_tokens": 7}}])
    assert cycle.run_stats(s) == {"turns": 3, "tokens": {"output": 7}}
    jl(s, [{"type": "assistant"}])
    assert cycle.run_stats(s) == {}
    assert cycle.run_stats(tmp_path / "nope.jsonl") == {}


def test_runstats_cli_with_the_run_window(tmp_path):
    s = tmp_path / "s.jsonl"
    jl(s, [RESULT])
    r = subprocess.run([sys.executable, str(CYCLE), "runstats", str(s), "--started",
                        "2026-10-01T21:08:43+01:00", "--ended", "2026-10-01T21:41:03"],
                       capture_output=True, text=True, env={**os.environ, "TZ": "Europe/London"})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["wall_s"] == 1940 and out["cost_usd"] == 4.347333 and out["turns"] == 62


# ------------------------------------------------------------------ the window

@pytest.fixture
def ledgers(tmp_path):
    t0 = 1_790_000_000
    jl(tmp_path / "GPU.jsonl", [
        {"ts": z(t0 - 600), "idle": True},
        {"ts": z(t0 + 60), "pid": 11, "cmd": "python3 train.py", "cwd": "/x", "mem_mib": 900},
        {"ts": z(t0 + 120), "pid": 11, "cmd": "python3 train.py", "cwd": "/x", "mem_mib": 900},
        {"ts": z(t0 + 120), "pid": 12, "cmd": "python3 eval.py", "cwd": "/x", "mem_mib": 100},
        {"ts": z(t0 + 180), "error": "nvidia-smi exit 9"},
        {"ts": z(t0 + 900), "pid": 11, "cmd": "python3 train.py", "cwd": "/x", "mem_mib": 900},
    ])
    jl(tmp_path / "TURNS.jsonl", [
        {"ts": z(t0 - 60), "lane": "chat", "cost_usd": 9.0, "output_tokens": 1},
        {"ts": z(t0 + 30), "lane": "chat", "cost_usd": 1.25, "input_tokens": 10,
         "output_tokens": 100, "cache_read": 1000, "cache_write": 50},
        {"ts": z(t0 + 400), "lane": "work", "cost_usd": 1.85, "output_tokens": 200},
        {"ts": z(t0 + 401), "lane": "chat"},
    ])
    return tmp_path, t0


def test_window_counts_gpu_minutes_by_process_and_turns(ledgers):
    d, t0 = ledgers
    w = cycle.window(z(t0), z(t0 + 458), d)
    assert w["wall_s"] == 458
    assert w["gpu"]["min"] == 3 and w["gpu"]["errors"] == 1 and not w["gpu"]["partial"]
    assert w["gpu"]["by_process"] == [{"pid": 11, "cmd": "python3 train.py", "min": 2},
                                      {"pid": 12, "cmd": "python3 eval.py", "min": 1}]
    assert w["turns"]["n"] == 3 and w["turns"]["cost_usd"] == 3.1
    assert w["turns"]["tokens"] == {"input": 10, "output": 300, "cache_read": 1000,
                                    "cache_write": 50}
    assert cycle.line("seal→score", w) == \
        "seal→score 7m38s · GPU 3 min · 3 turns · $3.10 (token spend in window)"


def test_window_with_no_ledgers_says_so_never_zero(tmp_path):
    w = cycle.window("2026-10-02T00:00:00Z", "2026-10-02T00:10:00Z", tmp_path)
    assert w["gpu"] == {"ledger": False} and w["turns"] == {"ledger": False}
    assert cycle.line("register→score", w) == \
        "register→score 10m00s · GPU not sampled · turns not logged"


def test_window_older_than_the_ledger_is_marked_partial(ledgers):
    d, t0 = ledgers
    w = cycle.window(z(t0 - 3600), z(t0 + 458), d)
    assert w["gpu"]["partial"] and w["turns"]["partial"]
    assert "GPU 3 min (sampled from " in cycle.line("seal→score", w)
    assert "turns logged from " in cycle.line("seal→score", w)


def test_window_cli_prints_the_line_and_json(ledgers):
    d, t0 = ledgers
    r = subprocess.run([sys.executable, str(CYCLE), "window", z(t0), z(t0 + 458), "--dir", str(d)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "GPU 3 min" in r.stdout and "pid 11" in r.stdout and "$3.10" in r.stdout
    r = subprocess.run([sys.executable, str(CYCLE), "window", z(t0), z(t0 + 458), "--json"],
                       capture_output=True, text=True, env={**os.environ, "HELIX_CYCLE_DIR": str(d)})
    assert json.loads(r.stdout)["gpu"]["min"] == 3
    assert json.loads(r.stdout)["line"].startswith("window 7m38s · GPU 3 min")


def test_window_refuses_end_before_start():
    r = subprocess.run([sys.executable, str(CYCLE), "window", "2026-10-02T01:00:00Z",
                        "2026-10-02T00:00:00Z"], capture_output=True, text=True)
    assert r.returncode == 2 and "before" in r.stderr


# ------------------------------------------------------------------ Part D: the sampler

def fake_smi(tmp_path, body):
    p = tmp_path / "nvidia-smi"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def lines(p):
    return [json.loads(l) for l in Path(p).read_text().splitlines()] if Path(p).exists() else []


def test_sampler_records_each_gpu_process_with_cmd_and_cwd(tmp_path):
    proc = subprocess.Popen(["sleep", "30"], cwd=str(tmp_path))
    try:
        smi = fake_smi(tmp_path, f'echo "{proc.pid}, 312 MiB"\necho "{os.getpid()}, 5 MiB"\n')
        led = tmp_path / "GPU.jsonl"
        assert gpu_sample.sample(led, smi=smi) == 0
        a, b = lines(led)
        assert a["pid"] == proc.pid and a["cmd"] == "sleep 30" and a["cwd"] == str(tmp_path)
        assert a["mem_mib"] == 312 and a["ts"].endswith("Z")
        assert b["pid"] == os.getpid() and len(b["cmd"]) <= 300
    finally:
        proc.kill()
        proc.wait()


def test_sampler_idle_line_at_most_every_ten_minutes(tmp_path):
    smi = fake_smi(tmp_path, "exit 0\n")
    led = tmp_path / "GPU.jsonl"
    t = 1_790_000_000
    gpu_sample.sample(led, smi=smi, now=t)
    gpu_sample.sample(led, smi=smi, now=t + 60)
    gpu_sample.sample(led, smi=smi, now=t + 599)
    assert lines(led) == [{"ts": z(t), "idle": True}]
    gpu_sample.sample(led, smi=smi, now=t + 600)
    assert len(lines(led)) == 2


def test_sampler_idle_after_a_process_line_is_written_at_once(tmp_path):
    led = tmp_path / "GPU.jsonl"
    t = 1_790_000_000
    jl(led, [{"ts": z(t - 30), "idle": True}, {"ts": z(t), "pid": 1, "cmd": "x", "mem_mib": 1}])
    gpu_sample.sample(led, smi=fake_smi(tmp_path, "exit 0\n"), now=t + 60)
    assert lines(led)[-1] == {"ts": z(t + 60), "idle": True}


@pytest.mark.parametrize("smi", ["missing", "fails", "garbage"])
def test_sampler_nvidia_smi_failing_writes_one_error_line(tmp_path, smi):
    led = tmp_path / "GPU.jsonl"
    path = {"missing": str(tmp_path / "no-such-smi"),
            "fails": fake_smi(tmp_path, "echo boom >&2; exit 9\n"),
            "garbage": fake_smi(tmp_path, "echo 'No devices were found'\n")}[smi]
    gpu_sample.sample(led, smi=path)
    [rec] = lines(led)
    assert set(rec) == {"ts", "error"} and rec["error"], rec


def test_sampler_cli_into_a_given_ledger(tmp_path):
    led = tmp_path / "G.jsonl"
    r = subprocess.run([sys.executable, str(SAMPLER), "--ledger", str(led), "--smi",
                        fake_smi(tmp_path, "exit 0\n")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert lines(led)[0]["idle"] is True


# ------------------------------------------------------------------ order 28 (item 24): K, counted and witnessed

import hashlib  # noqa: E402


def chain(recs):
    """ATTEMPTS.jsonl lines as the server writes them: seq = line number, prev = sha256 of the
    previous line's text (`genesis` first)."""
    out, prev = [], "genesis"
    for i, r in enumerate(recs, 1):
        raw = json.dumps(dict(r, seq=i, prev=prev), ensure_ascii=False)
        out.append(raw)
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return "".join(l + "\n" for l in out)


def att(t, scope="proj/a", n=1, cwd="/work/exp", pid=None, kind="run", cmd="python3 train.py"):
    r = {"ts": z(t), "scope": scope, "kind": kind, "n": n, "cmd": cmd, "cwd": cwd}
    if pid is not None:
        r["pid"] = pid
    return r


T0 = 1_790_000_000


def k_of(d, scope="proj/a", until=None):
    return cycle.count_k(scope, z(until if until is not None else T0 + 3600), d)


def test_k_is_the_sum_of_n_before_until_for_that_scope_only(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([
        att(T0, pid=101), att(T0 + 60, n=4, pid=102), att(T0 + 120, scope="proj/b", n=9),
        att(T0 + 180, kind="prompt"), att(T0 + 7200, n=5)]))       # the last is after `until`
    r = k_of(tmp_path)
    assert r["k"] == 6 and r["chain"] == "ok" and r["since"] == z(T0)
    assert r["line"].startswith(f"K=6 (since {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(T0))})")
    assert k_of(tmp_path, scope="proj/b")["k"] == 9
    assert k_of(tmp_path, until=T0 + 61)["k"] == 5                  # ts < until, strictly
    assert k_of(tmp_path, until=T0 + 60)["k"] == 1


def test_k_zero_attempts_is_printed_never_refused(tmp_path):
    r = k_of(tmp_path)                                              # no ledger at all
    assert r["k"] == 0 and r["line"] == "K=0 · no attempts logged"
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, scope="proj/b")]))
    r = k_of(tmp_path)
    assert r["k"] == 0 and r["line"] == "K=0 · no attempts logged" and r["since"] is None


@pytest.mark.parametrize("damage", ["edit middle", "delete middle", "reorder", "bad json"])
def test_a_broken_chain_gives_no_number(tmp_path, damage):
    lines = chain([att(T0), att(T0 + 60, n=4), att(T0 + 120)]).splitlines()
    if damage == "edit middle":
        lines[1] = lines[1].replace('"n": 4', '"n": 1')
    elif damage == "delete middle":
        del lines[1]
    elif damage == "reorder":
        lines[1], lines[2] = lines[2], lines[1]
    else:
        lines[1] = lines[1][:-5]
    (tmp_path / "ATTEMPTS.jsonl").write_text("\n".join(lines) + "\n")
    r = k_of(tmp_path)
    assert r["k"] is None and r["chain"] == "broken"
    assert re.fullmatch(r"K unverifiable \(chain broken at seq \d\)", r["line"]), r["line"]
    assert "K=" not in r["line"]


def test_the_first_line_must_say_genesis(tmp_path):
    raw = chain([att(T0)]).replace('"prev": "genesis"', '"prev": "' + "0" * 64 + '"')
    (tmp_path / "ATTEMPTS.jsonl").write_text(raw)
    assert k_of(tmp_path)["line"] == "K unverifiable (chain broken at seq 1)"


def gpu(d, recs):
    jl(d / "GPU.jsonl", [{"ts": z(T0 - 600), "idle": True}] + recs)


def g(t, pid, cwd="/work/exp", cmd="python3 train.py --seed 1"):
    return {"ts": z(t), "pid": pid, "cmd": cmd, "cwd": cwd, "mem_mib": 900}


def test_witness_a_logged_pid_is_witnessed(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    gpu(tmp_path, [g(T0 + 60, 101), g(T0 + 120, 101)])
    r = k_of(tmp_path)
    assert r["witness"] == "ok" and r["unlogged"] == 0
    assert r["line"].endswith(" · K witnessed (GPU)"), r["line"]


def test_witness_an_extra_pid_in_the_same_cwd_is_disputed_and_named(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    gpu(tmp_path, [g(T0 + 60, 101), g(T0 + 120, 202, cmd="python3 train.py --seed 2"),
                   g(T0 + 180, 202, cmd="python3 train.py --seed 2"),
                   g(T0 + 240, 303, cwd="/work/exp/sub")])
    r = k_of(tmp_path)
    assert r["witness"] == "disputed" and r["unlogged"] == 2          # each pid once
    assert r["line"].endswith(" · K disputed: +2 unlogged GPU job(s)"), r["line"]
    assert r["unlogged_jobs"][0] == {"pid": 202, "first_seen": z(T0 + 120),
                                     "cmd": "python3 train.py --seed 2", "cwd": "/work/exp"}
    assert r["k"] == 1                    # disclosure: K is what was logged; the dispute sits beside it


def test_witness_ignores_an_unrelated_cwd_and_jobs_outside_the_window(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    gpu(tmp_path, [g(T0 + 60, 404, cwd="/elsewhere"), g(T0 + 60, 405, cwd="/work/expanded"),
                   g(T0 - 60, 406), g(T0 + 7200, 407)])
    r = k_of(tmp_path)
    assert r["witness"] == "ok" and r["unlogged"] == 0, r["unlogged_jobs"]


def test_witness_a_cmd_naming_a_path_under_a_used_cwd_counts(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    gpu(tmp_path, [g(T0 + 60, 505, cwd="/tmp", cmd="python3 /work/exp/train.py")])
    assert k_of(tmp_path)["unlogged"] == 1


def test_witness_a_pid_logged_under_another_scope_is_logged(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101),
                                                    att(T0 + 30, scope="proj/b", pid=202)]))
    gpu(tmp_path, [g(T0 + 60, 202)])
    assert k_of(tmp_path)["witness"] == "ok"


def test_witness_a_pid_logged_only_after_the_job_was_seen_does_not_cover_it(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101), att(T0 + 600, pid=202)]))
    gpu(tmp_path, [g(T0 + 60, 202)])                     # on the GPU before its attempt line
    assert k_of(tmp_path)["unlogged"] == 1


def test_witness_ties_a_child_by_its_environment(tmp_path):
    """A child of a logged job has another pid but inherits HELIX_ATTEMPT_SEQ."""
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    proc = subprocess.Popen(["sleep", "30"], env={**os.environ, "HELIX_ATTEMPT_SEQ": "1"})
    other = subprocess.Popen(["sleep", "30"], env={**os.environ, "HELIX_ATTEMPT_SEQ": "99"})
    try:
        gpu(tmp_path, [g(T0 + 60, proc.pid), g(T0 + 60, other.pid)])
        r = k_of(tmp_path)
        assert [j["pid"] for j in r["unlogged_jobs"]] == [other.pid]      # seq 99 is no attempt
    finally:
        for p in (proc, other):
            p.kill()
            p.wait()


def test_witness_with_no_gpu_ledger_says_so(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101)]))
    r = k_of(tmp_path)
    assert r["witness"] == "none" and r["line"].endswith(" · K not witnessed (GPU not sampled)")


def test_k_cli_text_and_json(tmp_path):
    (tmp_path / "ATTEMPTS.jsonl").write_text(chain([att(T0, pid=101), att(T0 + 5, n=4)]))
    gpu(tmp_path, [g(T0 + 60, 202)])
    env = {**os.environ, "HELIX_CYCLE_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, str(CYCLE), "k", "--scope", "proj/a", "--until",
                        z(T0 + 3600)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith("K=5 (since ") and "disputed: +1" in r.stdout
    assert "pid 202" in r.stdout and "python3 train.py --seed 1" in r.stdout
    r = subprocess.run([sys.executable, str(CYCLE), "k", "--scope", "proj/a", "--until", "now",
                        "--json"], capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert out["k"] == 5 and out["until"].endswith("Z")


def test_k_cli_broken_chain_exits_4(tmp_path):
    lines = chain([att(T0), att(T0 + 60), att(T0 + 120)]).splitlines()
    lines[1] = lines[1].replace("train", "trian")
    (tmp_path / "ATTEMPTS.jsonl").write_text("\n".join(lines) + "\n")
    r = subprocess.run([sys.executable, str(CYCLE), "k", "--scope", "proj/a", "--until", "now"],
                       capture_output=True, text=True,
                       env={**os.environ, "HELIX_CYCLE_DIR": str(tmp_path)})
    assert r.returncode == 4 and r.stdout.strip() == "K unverifiable (chain broken at seq 3)"
