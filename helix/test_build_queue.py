#!/usr/bin/env python3
"""ORDER 24 — the build queue. No real builder, no systemd, no spend.

Each test copies the live `build_orders/run_queue.sh` into a scratch brain whose
`build_orders/run_order.sh` is a FAKE (it writes a report and a RUNS line itself), whose
`app.py` and `test_factgate_hook.py` are tiny stand-ins the gate can compile and run, and whose
`frontdesk.py` records the inbox note to a file. The rest is env overrides, as the runner's own
tests use BUILD_FAKE_HHMM: QUEUE_FAKE_INLINE (run the loop in the foreground instead of under
systemd-run), QUEUE_FAKE_SYSTEMCTL, QUEUE_FAKE_CLOCK, QUEUE_FAKE_NIGHTLY_RE, QUEUE_POLL_SECONDS.

Run:  python3 -m pytest -q test_build_queue.py
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIVE_QUEUE = HERE / "build_orders" / "run_queue.sh"

FAKE_RUNNER = r"""#!/usr/bin/env bash
# fake run_order.sh: no systemd, no claude. Files in $T steer it.
NN="$1"; T="$(cd "$(dirname "$0")/.." && pwd)"
echo "$NN|$BUILD_TRIGGER" >> "$T/launched.txt"
if [ -f "$T/refuse_$NN" ]; then echo "refused: fake refusal for $NN" >&2; exit 1; fi
UNIT="helix-builder-$NN-fake"
finish() {
  if [ ! -f "$T/noreport_$NN" ]; then
    printf '# report %s\n\nbody\n%s\n\n' "$NN" "$(cat "$T/last_$NN" 2>/dev/null || echo DONE)" \
      > "$T/build_orders/reports/ORDER_${NN}_report.md"
  fi
  if [ ! -f "$T/norunsline_$NN" ]; then
    CHECK="$(cat "$T/check_$NN" 2>/dev/null || echo VERIFIED)"   # ABSENT = a pre-order-26 line
    if [ "$CHECK" = ABSENT ]; then FIELD=""; else FIELD=", \"report_check\": \"$CHECK\""; fi
    printf '{"order": "%s", "exit": %s, "unit": "%s", "trigger": "%s", "report_exists": true%s}\n' \
      "$NN" "$(cat "$T/exit_$NN" 2>/dev/null || echo 0)" "$UNIT" "$BUILD_TRIGGER" "$FIELD" \
      >> "$T/build_orders/runs/RUNS.jsonl"
  fi
}
if [ -n "${FAKE_HOLD:-}" ]; then
  ( while [ ! -f "$T/release_$NN" ]; do sleep 0.05; done; finish ) >/dev/null 2>&1 &
else
  finish
fi
echo "started order $NN (unit $UNIT)"
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
T="__T__"
case "$*" in
  *is-active*) if [ -f "$T/unit_gone" ]; then echo inactive; exit 3; fi; echo active; exit 0 ;;
  *list-units*) [ -f "$T/builder_active" ] && echo "helix-builder-99-x.service loaded active running x"; exit 0 ;;
esac
exit 0
"""

FAKE_FRONTDESK = '''
import os
def notepad_path(brain):
    return os.path.join(brain, "notes.txt")
def add_note(path, text):
    with open(path, "a") as fh:
        fh.write(text + "\\n")
'''


class Q:
    """A scratch brain with the live run_queue.sh and fake everything else."""

    def __init__(self, root):
        self.T = root
        self.B = root / "build_orders"
        (self.B / "reports").mkdir(parents=True)
        (self.B / "runs").mkdir()
        shutil.copy2(LIVE_QUEUE, self.B / "run_queue.sh")
        (self.B / "run_order.sh").write_text(FAKE_RUNNER)
        (self.B / "run_order.sh").chmod(0o755)
        (root / "systemctl").write_text(FAKE_SYSTEMCTL.replace("__T__", str(root)))
        (root / "systemctl").chmod(0o755)
        (root / "frontdesk.py").write_text(FAKE_FRONTDESK)
        (root / "app.py").write_text("x = 1\n")
        (root / "test_factgate_hook.py").write_text("def test_ok():\n    assert True\n")
        for nn in ("25", "26", "27", "28", "29", "30", "31", "27b"):
            (self.B / f"ORDER_{nn}_thing.md").write_text(f"# order {nn}\n")
        self.env = {**os.environ,
                    "QUEUE_FAKE_INLINE": "1",
                    "QUEUE_FAKE_SYSTEMCTL": str(root / "systemctl"),
                    "QUEUE_FAKE_NIGHTLY_RE": "[n]o-such-process-order24",
                    "QUEUE_POLL_SECONDS": "0.05",
                    "BUILD_FAKE_HHMM": "1200"}
        self.env.pop("QUEUE_FAKE_CLOCK", None)

    def cmd(self, *args):
        return ["bash", str(self.B / "run_queue.sh"), *args]

    def run(self, *args, **env):
        return subprocess.run(self.cmd(*args), capture_output=True, text=True, timeout=120,
                              env={**self.env, **env})

    def popen(self, *args, **env):
        return subprocess.Popen(self.cmd(*args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env={**self.env, **env})

    def launched(self):
        p = self.T / "launched.txt"
        return [l.split("|")[0] for l in p.read_text().splitlines()] if p.exists() else []

    def queue(self):
        p = self.B / "runs" / "QUEUE.json"
        return json.loads(p.read_text()) if p.exists() else None

    def events(self):
        p = self.B / "runs" / "RUNS.jsonl"
        recs = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
        return [r for r in recs if "queue" in r]

    def notes(self):
        p = self.T / "notes.txt"
        return p.read_text().splitlines() if p.exists() else []

    def touch(self, name, text=""):
        (self.T / name).write_text(text)

    def wait_for(self, pred, what, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; queue={self.queue()}")


@pytest.fixture
def q(tmp_path):
    return Q(tmp_path)


# ---------------------------------------------------------------- the run, end to end

def test_all_pass_runs_all(q):
    r = q.run("--start", "25", "26", "27")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "started queue q-" in r.stdout
    assert q.launched() == ["25", "26", "27"]
    st = q.queue()
    assert st["status"] == "done" and st["halt_reason"] is None
    assert [x["order"] for x in st["results"]] == ["25", "26", "27"]
    assert all(x["outcome"] == "ok" for x in st["results"])
    # each builder was started by the queue, through run_order.sh, with the queue's trigger
    trig = (q.T / "launched.txt").read_text().splitlines()
    assert all(l.endswith(f"|chat:/build queue {st['id']}") for l in trig), trig
    kinds = [e["event"] for e in q.events()]
    assert kinds[0] == "queued" and kinds[-1] == "done"
    assert kinds.count("order_start") == 3 and kinds.count("order_end") == 3
    assert all(e["queue"] == st["id"] for e in q.events())
    notes = q.notes()
    assert len(notes) == 1, notes
    assert st["id"] in notes[0] and "all done" in notes[0]
    assert "25 ✓ 26 ✓ 27 ✓" in notes[0]


def test_ready_for_restart_is_named_in_the_note(q):
    q.touch("last_26", "READY FOR /restart")
    r = q.run("--start", "25", "26")
    assert r.returncode == 0, r.stdout + r.stderr
    assert q.queue()["status"] == "done"
    assert "ready for /restart" in q.notes()[0]


def test_order_2_stops_and_orders_3_on_never_start(q):
    q.touch("last_26", "STOP: ambiguity in section B")
    q.run("--start", "25", "26", "27", "28")
    assert q.launched() == ["25", "26"]
    st = q.queue()
    assert st["status"] == "halted"
    assert "26" in st["halt_reason"] and "STOP: ambiguity in section B" in st["halt_reason"]
    assert [x["outcome"] for x in st["results"]] == ["ok", "halt"]
    note = q.notes()
    assert len(note) == 1
    assert "25 ✓" in note[0] and "26 ✗" in note[0] and "27, 28 not started" in note[0]


def test_ready_for_users_install_halts(q):
    q.touch("last_25", "READY FOR USER'S INSTALL")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "READY FOR USER'S INSTALL" in q.queue()["halt_reason"]


def test_missing_report_halts(q):
    q.touch("noreport_25")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    st = q.queue()
    assert st["status"] == "halted" and "report missing" in st["halt_reason"]


def test_failed_pytest_halts(q):
    (q.T / "test_factgate_hook.py").write_text("def test_bad():\n    assert False\n")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "pytest" in q.queue()["halt_reason"]


def test_broken_app_py_halts(q):
    (q.T / "app.py").write_text("def broken(:\n")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "py_compile" in q.queue()["halt_reason"]


def test_unclean_builder_exit_halts(q):
    q.touch("exit_25", "1")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "builder exit 1" in q.queue()["halt_reason"]


def test_unit_gone_without_a_runs_line_halts(q):
    q.touch("norunsline_25")
    q.touch("unit_gone")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "no RUNS.jsonl line" in q.queue()["halt_reason"]


def test_every_failed_check_is_named(q):
    q.touch("last_25", "STOP")
    q.touch("exit_25", "2")
    (q.T / "test_factgate_hook.py").write_text("def test_bad():\n    assert False\n")
    q.run("--start", "25")
    why = q.queue()["halt_reason"]
    assert "'STOP'" in why and "builder exit 2" in why and "pytest" in why


@pytest.mark.parametrize("verdict", ["FLAGGED", "UNCHECKED", "ERROR: reportcheck exit 1: boom"])
def test_report_check_not_verified_halts_before_the_next_order(q, verdict):
    # order 26: the next order builds on this one, so a flagged report is read before it runs
    q.touch("check_25", verdict)
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    st = q.queue()
    assert st["status"] == "halted"
    assert f"report check {verdict!r}, not VERIFIED" in st["halt_reason"]
    assert "ORDER_25_report.check.json" in st["halt_reason"]
    assert "26 not started" in q.notes()[0]


def test_runs_line_without_report_check_halts(q):
    # a RUNS line from a builder_run.sh that predates order 26 carries no verdict: not VERIFIED
    q.touch("check_25", "ABSENT")
    q.run("--start", "25", "26")
    assert q.launched() == ["25"]
    assert "report check None, not VERIFIED" in q.queue()["halt_reason"]


def test_run_order_refusal_halts_with_its_reason(q):
    q.touch("refuse_26")
    q.run("--start", "25", "26", "27")
    assert q.launched() == ["25", "26"]
    st = q.queue()
    assert "fake refusal for 26" in st["halt_reason"]
    assert [x["outcome"] for x in st["results"]] == ["ok", "refused"]


def test_wall_clock_cap_halts_before_the_next_order(q):
    q.run("--start", "25", "26", QUEUE_FAKE_MAX_SECONDS="0")
    assert q.launched() == []
    assert "10-hour cap" in q.queue()["halt_reason"]


# ---------------------------------------------------------------- refused up front

def test_seven_orders_refused_up_front(q):
    r = q.run("--start", "25", "26", "27", "28", "29", "30", "31")
    assert r.returncode != 0
    assert "7 orders" in r.stdout and "at most 6" in r.stdout
    assert q.launched() == [] and q.queue() is None


def test_existing_report_refused_up_front_with_every_reason(q):
    (q.B / "reports" / "ORDER_26_report.md").write_text("old\n")
    (q.B / "ORDER_27_other.md").write_text("dup\n")
    r = q.run("--start", "25", "26", "27", "98")
    assert r.returncode != 0
    out = r.stdout
    assert "ORDER_26_report.md already exists" in out
    assert "2 files match ORDER_27_*.md" in out
    assert "no ORDER_98_*.md" in out
    assert q.launched() == [] and q.queue() is None


def test_active_builder_refused_up_front(q):
    q.touch("builder_active")
    r = q.run("--start", "25")
    assert r.returncode != 0 and "a builder is already active" in r.stdout
    assert q.launched() == []


def test_active_queue_refused_up_front(q):
    p = q.popen("--start", "25", "26", FAKE_HOLD="1")
    try:
        q.wait_for(lambda: q.launched() == ["25"], "order 25 to start")
        r = q.run("--check", "27")
        assert r.returncode != 0 and "a queue is already active" in r.stdout
    finally:
        q.touch("release_25")
        q.touch("release_26")
        p.wait(timeout=30)


def test_bad_id_refused(q):
    r = q.run("--check", "25;rm")
    assert r.returncode != 0 and "order id" in r.stdout


# ---------------------------------------------------------------- the nightly window

def test_nightly_window_waits_then_continues(q):
    clock = q.T / "clock"
    clock.write_text("0400")
    p = q.popen("--start", "25", "26", QUEUE_FAKE_CLOCK=str(clock))
    try:
        q.wait_for(lambda: (q.queue() or {}).get("status") == "waiting", "the queue to wait")
        time.sleep(0.3)
        assert q.launched() == [], "an order started inside 03:30-05:30"
        clock.write_text("0531")
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert q.launched() == ["25", "26"]
    assert q.queue()["status"] == "done"
    assert "wait" in [e["event"] for e in q.events()]


def test_running_nightly_waits_then_continues(q):
    sleeper = subprocess.Popen(["sleep", "314159"])
    try:
        p = q.popen("--start", "25", QUEUE_FAKE_NIGHTLY_RE="[s]leep 314159")
        q.wait_for(lambda: (q.queue() or {}).get("status") == "waiting", "the queue to wait")
        time.sleep(0.3)
        assert q.launched() == []
    finally:
        sleeper.kill()
        sleeper.wait()
    p.wait(timeout=30)
    assert q.launched() == ["25"]


# ---------------------------------------------------------------- app.py's chat commands

@pytest.fixture
def app_mod(q, monkeypatch):
    os.environ.setdefault("HELIX_NO_WARMUP", "1")
    os.environ.setdefault("HELIX_NO_PRECONNECT", "1")
    os.environ.setdefault("HELIX_NO_SCHEDULER", "1")   # order 32: no real ticker on memory/schedules.json
    sys.path.insert(0, str(HERE))
    import app
    monkeypatch.setattr(app, "BUILD_RUNS", str(q.B / "runs"))
    monkeypatch.setattr(app, "BUILD_QUEUE_RUNNER", str(q.B / "run_queue.sh"))
    for k, v in q.env.items():
        monkeypatch.setenv(k, v)
    return app


def test_build_stop_finishes_current_and_starts_none(q, app_mod):
    p = q.popen("--start", "25", "26", "27", FAKE_HOLD="1")
    try:
        q.wait_for(lambda: q.launched() == ["25"], "order 25 to start")
        handled, reply = app_mod._build_command("/build stop")
        assert handled and "25" in reply and "no further orders" in reply
        assert q.queue()["stop_requested"] is True
        time.sleep(0.3)
        assert p.poll() is None, "the queue must wait for the running order, not kill it"
        q.touch("release_25")
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert q.launched() == ["25"]
    st = q.queue()
    assert st["status"] == "halted" and "/build stop" in st["halt_reason"]
    assert st["results"][0]["outcome"] == "ok"
    assert "26, 27 not started" in q.notes()[0]


def test_build_stop_with_no_queue(q, app_mod):
    assert app_mod._build_command("/build stop") == (True, "no queue is running")


def test_build_status_shows_no_queue_then_the_queue(q, app_mod):
    _, reply = app_mod._build_command("/build status")
    assert "no queue" in reply
    q.touch("last_26", "READY FOR /restart")
    q.run("--start", "25", "26")
    _, reply = app_mod._build_command("/build status")
    assert q.queue()["id"] in reply
    assert "25: DONE" in reply and "26: READY FOR /restart" in reply


def test_build_status_while_running_shows_position(q, app_mod):
    p = q.popen("--start", "25", "26", FAKE_HOLD="1")
    try:
        q.wait_for(lambda: q.launched() == ["25"], "order 25 to start")
        q.wait_for(lambda: (q.queue() or {}).get("current") == "25", "current=25")
        _, reply = app_mod._build_command("/build status")
        assert "order 1 of 2" in reply and "current: 25" in reply
    finally:
        q.touch("release_25")
        q.touch("release_26")
        p.wait(timeout=30)


def test_chat_range_and_list_reach_the_queue(q, app_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(app_mod, "_build_queue_start", lambda orders: seen.append(orders) or "ok")
    assert app_mod._build_command("/build 25-30") == (True, "ok")
    assert app_mod._build_command("/build 25,26,27b") == (True, "ok")
    assert app_mod._build_command("/build 25, 26") == (True, "ok")
    assert seen == [["25", "26", "27", "28", "29", "30"], ["25", "26", "27b"], ["25", "26"]]
    # only exact commands are intercepted; letters are not allowed in a range
    assert app_mod._build_command("remind me to /build 25-30") == (False, None)
    assert app_mod._build_command("/build 25b-27")[1] != "ok"
    assert app_mod._build_command("/build 30-25")[1].startswith("refused")


def test_chat_seven_orders_refused_and_nothing_launched(q, app_mod):
    handled, reply = app_mod._build_command("/build 25-31")
    assert handled and "7 orders" in reply
    assert q.launched() == [] and q.queue() is None


def test_chat_queue_lists_every_reason(q, app_mod):
    handled, reply = app_mod._build_command("/build 98-99")
    assert handled
    assert "no ORDER_98_*.md" in reply and "no ORDER_99_*.md" in reply
    assert q.launched() == [] and q.queue() is None


def test_chat_queue_starts(q, app_mod):
    handled, reply = app_mod._build_command("/build 25,26")
    assert handled and "started queue q-" in reply, reply
    assert q.launched() == ["25", "26"]


def test_single_build_unchanged(q, app_mod, monkeypatch):
    fake = q.T / "single.sh"
    fake.write_text('#!/usr/bin/env bash\necho "noise"\necho "started order $1 via $BUILD_TRIGGER"\n')
    monkeypatch.setattr(app_mod, "BUILD_RUNNER", str(fake))
    called = []
    monkeypatch.setattr(app_mod, "_build_queue_start", lambda orders: called.append(orders))
    assert app_mod._build_command("/build 23") == (True, "started order 23 via chat:/build")
    assert app_mod._build_command("/build 23b") == (True, "started order 23b via chat:/build")
    assert called == []
    assert app_mod._build_command("/build")[1].startswith("usage")


# ---------------------------------------------------------------- builder_run.sh (order 26)
# The live builder_run.sh in a scratch HOME, so its $HOME/.local/bin/claude is a fake that
# commits a file and writes a report; the live reportcheck.py checks that report. No real CLI,
# no systemd, no spend. HOME is the only thing redirected; no override was added to the script.

LIVE_BUILDER_RUN = HERE / "build_orders" / "builder_run.sh"
LIVE_REPORTCHECK = HERE / "workspace" / "reportcheck" / "reportcheck.py"
LIVE_CYCLE = HERE / "workspace" / "cycle" / "cycle.py"

FAKE_CLI = r"""#!/usr/bin/env bash
# fake claude: one commit in a scratch repo, then a report whose CLAIMS are true unless told.
set -e
R="$FAKE_REPO"
cd "$R"
echo "work $RANDOM" > made.py
git add made.py
git -c user.name=t -c user.email=t@t commit -q -m "fake builder work"
H="$(git rev-parse --short HEAD)"
SHA="$(sha256sum made.py | cut -d' ' -f1)"
[ -n "${FAKE_LIE:-}" ] && SHA="0000000000000000000000000000000000000000000000000000000000000000"
if [ -z "${FAKE_NO_REPORT:-}" ]; then
  {
    echo "# ORDER 77 report"; echo; echo "## 6. Commit hash"; echo "- \`$H\`"; echo
    if [ -z "${FAKE_NO_CLAIMS:-}" ]; then
      echo "## CLAIMS"
      echo "- commit $R $H"
      echo "- changed $R made.py"
      echo "- sha256 $R/made.py $SHA"
      echo
    fi
    echo "DONE"
  } > "$FAKE_REPORT"
fi
[ -n "${FAKE_STREAM_LINE:-}" ] && echo "$FAKE_STREAM_LINE" >> "$FAKE_STREAM"
exit "${FAKE_EXIT:-0}"
"""

HANG_REPORTCHECK = r"""import os, sys, time
open(os.environ["FAKE_MARK"], "w").write("checking\n")
time.sleep(60)
"""


class BR:
    """A scratch HOME holding helix/{build_orders,workspace/reportcheck,frontdesk.py}."""

    def __init__(self, root):
        self.home = root
        self.brain = root / "helix"
        self.B = self.brain / "build_orders"
        (self.B / "reports").mkdir(parents=True)
        (self.B / "runs").mkdir()
        shutil.copy2(LIVE_BUILDER_RUN, self.B / "builder_run.sh")
        (self.B / "BUILD_PROTOCOL.md").write_text("protocol\n")
        rcdir = self.brain / "workspace" / "reportcheck"
        rcdir.mkdir(parents=True)
        shutil.copy2(LIVE_REPORTCHECK, rcdir / "reportcheck.py")
        cydir = self.brain / "workspace" / "cycle"            # order 27: run_stats()
        cydir.mkdir(parents=True)
        shutil.copy2(LIVE_CYCLE, cydir / "cycle.py")
        (self.brain / "frontdesk.py").write_text(FAKE_FRONTDESK)
        cli = root / ".local" / "bin" / "claude"
        cli.parent.mkdir(parents=True)
        cli.write_text(FAKE_CLI)
        cli.chmod(0o755)
        self.repo = self.brain / "workspace" / "thing"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.report = self.B / "reports" / "ORDER_77_report.md"
        self.env = {**os.environ, "HOME": str(root), "FAKE_REPO": str(self.repo),
                    "FAKE_REPORT": str(self.report),
                    "FAKE_STREAM": str(self.B / "runs" / "s.jsonl")}

    def args(self):
        return ["bash", str(self.B / "builder_run.sh"), "77", str(self.B / "ORDER_77_x.md"),
                "helix-builder-77-test", str(self.B / "runs" / "s.jsonl"), str(self.report),
                "abc123", "test", "1"]

    def run(self, **env):
        return subprocess.run(self.args(), capture_output=True, text=True, timeout=120,
                              env={**self.env, **env})

    def runs(self):
        p = self.B / "runs" / "RUNS.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def notes(self):
        p = self.brain / "notes.txt"
        return p.read_text().splitlines() if p.exists() else []


@pytest.fixture
def br(tmp_path):
    return BR(tmp_path)


def test_builder_run_true_report_is_verified(br):
    br.run()
    [rec] = br.runs()
    assert rec["report_check"] == "VERIFIED", rec
    assert rec["exit"] == 0 and rec["report_exists"] is True
    [note] = br.notes()
    assert "ended exit 0, wrote its report" in note
    assert "· report check: VERIFIED (3 claims)" in note
    out = json.loads((br.B / "reports" / "ORDER_77_report.check.json").read_text())
    assert out["verdict"] == "VERIFIED"


def test_builder_run_false_report_is_flagged_in_runs_and_note(br):
    br.run(FAKE_LIE="1")
    [rec] = br.runs()
    assert rec["report_check"] == "FLAGGED", rec
    [note] = br.notes()
    assert "· ⚠ REPORT FLAGGED: sha256 " in note and "made.py" in note
    out = json.loads((br.B / "reports" / "ORDER_77_report.check.json").read_text())
    assert out["verdict"] == "FLAGGED"


def test_builder_run_report_without_claims_is_unchecked(br):
    br.run(FAKE_NO_CLAIMS="1")
    [rec] = br.runs()
    assert rec["report_check"] == "UNCHECKED"
    assert "· report check: UNCHECKED" in br.notes()[0]


def test_builder_run_checks_against_its_own_run_record(br):
    # the builder's exit is a witness: a report from a failed run is not VERIFIED
    br.run(FAKE_EXIT="3")
    [rec] = br.runs()
    assert rec["exit"] == 3 and rec["report_check"] == "FLAGGED"
    assert "run exit" in br.notes()[0]


def test_builder_run_no_report_still_writes_the_line(br):
    br.run(FAKE_NO_REPORT="1")
    [rec] = br.runs()
    assert rec["report_exists"] is False
    assert rec["report_check"].startswith("ERROR: reportcheck exit 2"), rec
    assert "did NOT write a report" in br.notes()[0]


def test_builder_run_reportcheck_crash_is_recorded_and_line_still_written(br):
    (br.brain / "workspace" / "reportcheck" / "reportcheck.py").write_text("raise SystemExit('boom')\n")
    br.run()
    [rec] = br.runs()
    assert rec["report_check"] == "ERROR: reportcheck exit 1: boom", rec
    assert "report check ERROR" in br.notes()[0]


def test_builder_run_missing_reportcheck_is_recorded(br):
    (br.brain / "workspace" / "reportcheck" / "reportcheck.py").unlink()
    br.run()
    [rec] = br.runs()
    assert rec["report_check"].startswith("ERROR: reportcheck exit 2"), rec


def test_builder_run_sigterm_during_the_check_still_writes_the_line(br):
    # systemd stops a unit by SIGTERM to the whole cgroup: here, the whole process group
    import signal
    mark = br.home / "mark"
    (br.brain / "workspace" / "reportcheck" / "reportcheck.py").write_text(HANG_REPORTCHECK)
    p = subprocess.Popen(br.args(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env={**br.env, "FAKE_MARK": str(mark)}, start_new_session=True)
    end = time.time() + 30
    while not mark.exists() and time.time() < end:
        time.sleep(0.05)
    assert mark.exists(), "the check never started"
    t0 = time.time()
    os.killpg(p.pid, signal.SIGTERM)
    p.wait(timeout=30)
    assert time.time() - t0 < 15, "the line waited for the hung check"
    [rec] = br.runs()
    assert rec["report_check"].startswith("ERROR: Stopped"), rec
    assert len(br.notes()) == 1


CHAIN_RUNNER = r"""#!/usr/bin/env bash
# fake run_order.sh that runs the REAL builder_run.sh in the foreground (no systemd)
NN="$1"; B="$(cd "$(dirname "$0")" && pwd)"
echo "$NN" >> "$B/../launched.txt"
UNIT="helix-builder-$NN-chain"
export FAKE_REPORT="$B/reports/ORDER_${NN}_report.md"
[ "$NN" = "${CHAIN_LIE:-}" ] && export FAKE_LIE=1
bash "$B/builder_run.sh" "$NN" "$B/ORDER_${NN}_x.md" "$UNIT" "$B/runs/s.jsonl" "$FAKE_REPORT" \
  abc chain 1 >/dev/null 2>&1
echo "started order $NN (unit $UNIT)"
"""


@pytest.mark.parametrize("lie,launched,status", [("", ["25", "26"], "done"),
                                                 ("25", ["25"], "halted")])
def test_queue_through_the_real_builder_run_and_reportcheck(br, lie, launched, status):
    """AC4 end to end: real run_queue.sh → fake run_order.sh → real builder_run.sh → fake CLI →
    real reportcheck.py. A false claim in order 25's report halts the queue before 26."""
    shutil.copy2(LIVE_QUEUE, br.B / "run_queue.sh")
    (br.B / "run_order.sh").write_text(CHAIN_RUNNER)
    (br.brain / "app.py").write_text("x = 1\n")
    (br.brain / "test_factgate_hook.py").write_text("def test_ok():\n    assert True\n")
    systemctl = br.home / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL.replace("__T__", str(br.home)))
    systemctl.chmod(0o755)
    for nn in ("25", "26"):
        (br.B / f"ORDER_{nn}_x.md").write_text(f"# order {nn}\n")
    env = {**br.env, "QUEUE_FAKE_INLINE": "1", "QUEUE_FAKE_SYSTEMCTL": str(systemctl),
           "QUEUE_FAKE_NIGHTLY_RE": "[n]o-such-process-order26", "QUEUE_POLL_SECONDS": "0.05",
           "BUILD_FAKE_HHMM": "1200", "CHAIN_LIE": lie,
           # the gate runs pytest; with HOME redirected, python3 must still find it
           "PYTHONUSERBASE": str(Path.home() / ".local")}
    env.pop("QUEUE_FAKE_CLOCK", None)
    r = subprocess.run(["bash", str(br.B / "run_queue.sh"), "--start", "25", "26"],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (br.brain / "launched.txt").read_text().split() == launched
    st = json.loads((br.B / "runs" / "QUEUE.json").read_text())
    assert st["status"] == status, st
    builder_lines = [x for x in br.runs() if "queue" not in x]
    assert [x["report_check"] for x in builder_lines] == (["FLAGGED"] if lie else ["VERIFIED"] * 2)
    notes = br.notes()
    if lie:
        assert "report check 'FLAGGED', not VERIFIED" in st["halt_reason"]
        assert "⚠ REPORT FLAGGED: sha256" in notes[0]       # the builder's own note
        assert "26 not started" in notes[-1]                 # the queue's note
    else:
        assert all("report check: VERIFIED (3 claims)" in n for n in notes[:2])


# ---------------------------------------------------------------- order 27: every build timestamp in UTC

import re  # noqa: E402

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LIVE_RUN_ORDER = HERE / "build_orders" / "run_order.sh"


def _age(text):
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(text)).total_seconds()


def test_builder_run_started_and_ended_are_utc_z(br):
    br.run()
    [rec] = br.runs()
    assert UTC_Z.match(rec["started"]) and UTC_Z.match(rec["ended"]), rec
    assert -5 < _age(rec["ended"]) < 120 and rec["started"] <= rec["ended"], rec
    assert rec["report_check"] == "VERIFIED", rec      # reportcheck reads the Z window


def test_queue_events_are_utc_z(q):
    q.run("--start", "25")
    ev = q.events()
    assert ev and all(UTC_Z.match(e["at"]) for e in ev), ev
    st = q.queue()
    assert UTC_Z.match(st["started"]) and UTC_Z.match(st["ended"]), st


@pytest.mark.parametrize("form,age,active", [("naive", 30, True), ("z", 30, True),
                                             ("naive", 300, False), ("z", 300, False)])
def test_a_starting_queue_is_aged_from_either_form(q, form, age, active):
    """An old QUEUE.json says `started` in naive local time; a new one in Z. Both age right."""
    t = time.time() - age
    started = (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if form == "naive"
               else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)))
    (q.B / "runs" / "QUEUE.json").write_text(json.dumps(
        {"id": "q-old", "orders": ["25"], "status": "starting", "started": started, "pid": None}))
    r = q.run("--check", "27")
    assert ("a queue is already active" in r.stdout) is active, r.stdout


def test_run_order_unit_name_is_utc(tmp_path):
    B = tmp_path / "helix" / "build_orders"
    B.mkdir(parents=True)
    shutil.copy2(LIVE_RUN_ORDER, B / "run_order.sh")
    shutil.copy2(LIVE_BUILDER_RUN, B / "builder_run.sh")
    (B / "BUILD_PROTOCOL.md").write_text("protocol line\n" * 300)
    (B / "tools").mkdir()                                  # ORDER 54c: refusal 6 lints the order
    shutil.copy2(HERE / "build_orders" / "tools" / "orderlint.py", B / "tools" / "orderlint.py")
    (B.parent / "app.py").write_text("import json\n")      # ORDER 54d: rule 5 reads app.py under RESTART: no
    (B / "ORDER_88_x.md").write_text(
        "# order 88\n\n## ORDER HEADER\nEDITS: x.py (new)\nDOES NOT TOUCH: memory\nRESTART: no\n\n## Report\n"
        "The report's very LAST line must be exactly `STOP` (the protocol also accepts `DONE`).\n")
    cli = tmp_path / ".local" / "bin" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    no_units = tmp_path / "systemctl"                      # ORDER 77: never ask the real systemd
    no_units.write_text("#!/bin/sh\nexit 0\n")
    no_units.chmod(0o755)
    before = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    r = subprocess.run(["bash", str(B / "run_order.sh"), "--dry-run", "88"], capture_output=True,
                       text=True, timeout=60,
                       env={**os.environ, "HOME": str(tmp_path), "BUILD_FAKE_HHMM": "1200",
                            "BUILD_FAKE_SYSTEMCTL": str(no_units)})
    after = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    assert r.returncode == 0, r.stdout + r.stderr
    m = re.search(r"^unit:\s+helix-builder-88-(\d{8}_\d{6})Z$", r.stdout, re.M)
    assert m and before <= m.group(1) <= after, r.stdout
    assert f"ORDER_88_{m.group(1)}Z.jsonl" in r.stdout


# ---------------------------------------------------------------- order 77: one start per live build
# The lock only lives until systemd-run returns, so refusal 3 also asks systemd for a live
# helix-builder-* unit. BUILD_FAKE_SYSTEMCTL stands in for systemctl; fake systemd-run records a launch.

LIVE_76C_UNIT = "helix-builder-76c-20261023_223927Z"


@pytest.fixture
def ro(tmp_path):
    B = tmp_path / "helix" / "build_orders"
    B.mkdir(parents=True)
    shutil.copy2(LIVE_RUN_ORDER, B / "run_order.sh")
    shutil.copy2(LIVE_BUILDER_RUN, B / "builder_run.sh")
    (B / "BUILD_PROTOCOL.md").write_text("protocol line\n" * 300)
    (B / "tools").mkdir()
    shutil.copy2(HERE / "build_orders" / "tools" / "orderlint.py", B / "tools" / "orderlint.py")
    (B.parent / "app.py").write_text("import json\n")
    (B / "ORDER_88_x.md").write_text(
        "# order 88\n\n## ORDER HEADER\nEDITS: x.py (new)\nDOES NOT TOUCH: memory\nRESTART: no\n\n## Report\n"
        "The report's very LAST line must be exactly `STOP` (the protocol also accepts `DONE`).\n")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, body in [(tmp_path / ".local" / "bin" / "claude", "exit 0\n"),
                       (fakebin / "systemd-run", f'echo "$*" >> {tmp_path / "launched.txt"}\nexit 0\n')]:
        name.parent.mkdir(parents=True, exist_ok=True)
        name.write_text("#!/bin/sh\n" + body)
        name.chmod(0o755)

    def run(units, *args):
        fake = tmp_path / "systemctl"
        fake.write_text("#!/bin/sh\n" + "".join(f"echo '{u}'\n" for u in units))
        fake.chmod(0o755)
        return subprocess.run(["bash", str(B / "run_order.sh"), *args, "88"], capture_output=True,
                              text=True, timeout=60,
                              env={**os.environ, "HOME": str(tmp_path), "BUILD_FAKE_HHMM": "1200",
                                   "BUILD_FAKE_SYSTEMCTL": str(fake),
                                   "PATH": f"{fakebin}:{os.environ['PATH']}"})
    run.launched = lambda: ((tmp_path / "launched.txt").read_text().splitlines()
                            if (tmp_path / "launched.txt").exists() else [])
    run.lock = B / "runs" / ".lock"
    return run


def test_active_builder_unit_refuses_a_second_start(ro):
    r = ro([f"{LIVE_76C_UNIT} loaded active running /home/x/helix/build_orders/builder_run.sh 76c"])
    assert r.returncode != 0, r.stdout + r.stderr
    assert "refused" in r.stderr and LIVE_76C_UNIT in r.stderr, r.stderr
    assert "started order" not in r.stdout, r.stdout
    assert ro.launched() == []
    assert not ro.lock.exists()


def test_an_activating_builder_unit_also_refuses(ro):
    r = ro([f"{LIVE_76C_UNIT} loaded activating start /home/x/helix/build_orders/builder_run.sh 76c"])
    assert r.returncode != 0 and LIVE_76C_UNIT in r.stderr, r.stderr
    assert ro.launched() == []


def test_no_builder_unit_is_not_refused_by_refusal_3(ro):
    r = ro([])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already active" not in r.stderr, r.stderr
    assert "started order 88 (unit helix-builder-88-" in r.stdout, r.stdout
    [line] = ro.launched()
    assert "--unit=helix-builder-88-" in line, line


def test_dry_run_still_works_with_no_unit_active(ro):
    r = ro([], "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("DRY RUN — nothing launched"), r.stdout
    assert ro.launched() == []


# ---------------------------------------------------------------- order 27 part B: time and money per run

RESULT_LINE = json.dumps({"type": "result", "subtype": "success", "num_turns": 62,
                          "total_cost_usd": 4.347333,
                          "usage": {"input_tokens": 120, "output_tokens": 89069,
                                    "cache_read_input_tokens": 6469325,
                                    "cache_creation_input_tokens": 158951}})


def test_builder_run_records_wall_cost_turns_tokens_and_the_note_shows_them(br):
    (br.B / "runs" / "s.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    br.run(FAKE_STREAM_LINE=RESULT_LINE)
    [rec] = br.runs()
    assert isinstance(rec["wall_s"], int) and 0 <= rec["wall_s"] < 60, rec
    assert rec["cost_usd"] == 4.347333 and rec["turns"] == 62, rec
    assert rec["tokens"] == {"input": 120, "output": 89069, "cache_read": 6469325,
                             "cache_write": 158951}, rec
    assert rec["report_check"] == "VERIFIED", rec
    [note] = br.notes()
    assert "· 0 min · $4.35 · report check: VERIFIED" in note, note


def test_builder_run_without_a_result_line_leaves_the_fields_absent(br):
    br.run()
    [rec] = br.runs()
    assert "wall_s" in rec
    assert not {"cost_usd", "turns", "tokens"} & set(rec), rec
    [note] = br.notes()
    assert "· 0 min ·" in note and "$" not in note, note


def test_builder_run_without_cycle_py_still_writes_the_line(br):
    (br.brain / "workspace" / "cycle" / "cycle.py").unlink()
    br.run(FAKE_STREAM_LINE=RESULT_LINE)
    [rec] = br.runs()
    assert rec["report_check"] == "VERIFIED" and "cost_usd" not in rec, rec
    assert rec["run_stats"].startswith("ERROR: "), rec


# ---------------------------------------------------------------- order 64: a runner script edited mid-run
# Bash reads a script a piece at a time, so an order that rewrote builder_run.sh IN PLACE while it ran
# (order 63) sent bash on at the old byte offset into the new file: a syntax error, RC 99. The three
# runner scripts now wrap everything in main() and end `main "$@"; exit $?`, so the whole file is
# parsed before any line runs. The control is the pre-order-64 builder_run.sh, pinned by sha256.

import hashlib  # noqa: E402

OLD_BUILDER_RUN = HERE / "build_orders" / "builder_run.sh.bak.20261005_225541Z"
OLD_BUILDER_RUN_SHA = "f75d463946b34b137eab16e7181c8d9f0f4d879a2fc7a10654b9e8b8340df503"
RUNNER_SCRIPTS = [HERE / "build_orders" / n for n in ("builder_run.sh", "run_order.sh", "run_queue.sh")]
MAIN_LAST_LINE = 'main "$@"; exit $?'

# Opens the file r+ (same inode, as an editor that writes in place does) and inserts >2 KB of
# comment lines after the shebang.
REWRITE_PY = r"""import os, sys
p = sys.argv[1]
ino = os.stat(p).st_ino
pad = "".join(f"# padding line {i:03d}, inserted in place while this script runs ..........\n"
              for i in range(32))
assert len(pad) >= 2048, len(pad)
with open(p, "r+") as fh:
    lines = fh.read().splitlines(True)
    fh.seek(0)
    fh.write(lines[0] + pad + "".join(lines[1:]))
    fh.truncate()
assert os.stat(p).st_ino == ino
open(sys.argv[2], "a").write(f"rewrote {p}\n")
"""


def _rewrite_cli(br):
    """The fake claude, but first it rewrites the scratch builder_run.sh in place."""
    (br.home / "rewrite.py").write_text(REWRITE_PY)
    cli = br.home / ".local" / "bin" / "claude"
    cli.write_text(FAKE_CLI.replace(
        "set -e\n",
        f'set -e\npython3 "{br.home}/rewrite.py" "{br.B}/builder_run.sh" "{br.home}/rewrote.txt"\n', 1))


def test_old_builder_run_edited_mid_run_breaks_the_run(br):
    """Positive control: the pre-order-64 script hits the order-63 fault."""
    assert hashlib.sha256(OLD_BUILDER_RUN.read_bytes()).hexdigest() == OLD_BUILDER_RUN_SHA
    shutil.copy2(OLD_BUILDER_RUN, br.B / "builder_run.sh")
    _rewrite_cli(br)
    r = br.run(FAKE_EXIT="5")
    assert (br.home / "rewrote.txt").exists()
    print(f"OLD builder_run.sh rc={r.returncode}\n--- stderr ---\n{r.stderr}")
    [rec] = br.runs()
    assert "syntax error" in r.stderr, r.stderr
    assert r.returncode != 5 and rec["exit"] == 99, (r.returncode, rec)


def test_builder_run_edited_mid_run_still_finishes(br):
    _rewrite_cli(br)
    r = br.run(FAKE_EXIT="5")
    assert (br.home / "rewrote.txt").exists()
    print(f"NEW builder_run.sh rc={r.returncode}\n--- stderr ---\n{r.stderr}")
    assert "syntax error" not in r.stderr + r.stdout, r.stderr
    assert r.returncode == 5, (r.returncode, r.stderr)
    [rec] = br.runs()
    assert rec["exit"] == 5 and rec["report_exists"] is True, rec
    assert {"order", "started", "ended", "unit", "trigger", "budget_usd", "protocol_sha256",
            "wall_s", "report_check", "namecheck", "hookcorpus"} <= set(rec), rec
    assert rec["report_check"] == "FLAGGED", rec       # exit 5 is a witness against the report
    [note] = br.notes()
    assert "ended exit 5, wrote its report" in note, note


def test_run_queue_edited_mid_run_finishes_both_orders(q):
    (q.T / "rewrite.py").write_text(REWRITE_PY)
    runner = FAKE_RUNNER.replace(
        'echo "$NN|$BUILD_TRIGGER" >> "$T/launched.txt"\n',
        'echo "$NN|$BUILD_TRIGGER" >> "$T/launched.txt"\n'
        'python3 "$T/rewrite.py" "$T/build_orders/run_queue.sh" "$T/rewrote.txt"\n', 1)
    (q.B / "run_order.sh").write_text(runner)
    r = q.run("--start", "25", "26")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "syntax error" not in r.stdout + r.stderr, r.stderr
    assert len((q.T / "rewrote.txt").read_text().splitlines()) == 2
    assert q.launched() == ["25", "26"]
    assert q.queue()["status"] == "done", q.queue()


@pytest.mark.parametrize("script", RUNNER_SCRIPTS, ids=lambda p: p.name)
def test_runner_script_is_parsed_whole(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = script.read_text().splitlines()
    assert lines[-1] == MAIN_LAST_LINE, lines[-1]
    assert lines.count(MAIN_LAST_LINE) == 1
    start = lines.index("main() {")
    before = [l for l in lines[:start] if l.strip() and not l.lstrip().startswith("#")]
    assert before == [], before
    # between main's closing brace and the last line: nothing but blanks and comments
    end = len(lines) - 1 - lines[::-1].index("}")
    after = [l for l in lines[end + 1:-1] if l.strip() and not l.lstrip().startswith("#")]
    assert after == [], after
