"""Tests for the infrastructure heartbeat (order 30).

No network: every probe here is a fake, registered under kind "fake" and driven by the
target's own "fake" field. State is always a temp file. The real probes are exercised
once, by hand, with `heartbeat.py --dry` (AC3), never from this suite.
"""

import sys

sys.dont_write_bytecode = True

import datetime
import json
import subprocess
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import heartbeat as hb  # noqa: E402

IDS = ["box2-tcp", "box2-ssh", "share", "tailnet", "odin-drive", "hdd", "gpu-sampler"]


def fake(target):
    how = target["fake"]
    if how == "ok":
        return True, "ok"
    if how == "down":
        return False, "connection refused"
    if how == "hang":
        time.sleep(30)
        return True, "never"
    if how == "raise":
        raise ValueError("boom")
    raise AssertionError(how)


PROBES = {"fake": fake}


def targets(**down):
    """The seven ids, all OK unless named: targets(box2_ssh="down")."""
    return [{"id": i, "kind": "fake", "fake": down.get(i.replace("-", "_"), "ok")} for i in IDS]


def night(n):
    return datetime.datetime(2026, 10, 2, 4, 30) + datetime.timedelta(days=n)


def line(state, n, **down):
    return hb.heartbeat_line(state, targets=targets(**down), probes=PROBES, now=night(n))


# ---- the line ------------------------------------------------------------------------

def test_first_run_says_there_was_no_state_and_starts_one(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    assert line(state, 0) == "- heartbeat: no state yet, starting fresh · 7/7 OK"
    assert len(state.read_text().splitlines()) == 1


def test_all_ok_and_unchanged_is_one_short_line(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    assert line(state, 1) == "- heartbeat: 7/7 OK"


def test_one_down_is_named_in_red(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    assert line(state, 1, box2_ssh="down") == \
        "- heartbeat: 🔴 box2-ssh DOWN (since 2026-10-03, 1 night: connection refused) · 6/7 OK"


def test_still_down_is_printed_again_with_its_count(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    line(state, 1, box2_ssh="down")
    assert line(state, 2, box2_ssh="down") == \
        "- heartbeat: 🔴 box2-ssh DOWN (since 2026-10-03, 2 nights: connection refused) · 6/7 OK"


def test_two_down_are_both_named(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    assert line(state, 1, box2_ssh="down", share="down") == (
        "- heartbeat: 🔴 box2-ssh DOWN (since 2026-10-03, 1 night: connection refused)"
        " · share DOWN (since 2026-10-03, 1 night: connection refused) · 5/7 OK")


def test_back_is_said_for_one_night_then_quiet(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    line(state, 1, box2_ssh="down")
    line(state, 2, box2_ssh="down")
    assert line(state, 3) == "- heartbeat: box2-ssh back (was down 2 nights) · 7/7 OK"
    assert line(state, 4) == "- heartbeat: 7/7 OK"


def test_a_new_target_is_called_new(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    hb.heartbeat_line(state, targets=targets()[:6], probes=PROBES, now=night(0))
    hb.heartbeat_line(state, targets=targets()[:6], probes=PROBES, now=night(1))
    assert line(state, 2) == "- heartbeat: gpu-sampler new · 7/7 OK"
    assert line(state, 3) == "- heartbeat: 7/7 OK"


def test_a_new_target_that_is_down_is_new_and_down(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    hb.heartbeat_line(state, targets=targets()[:6], probes=PROBES, now=night(0))
    assert line(state, 1, gpu_sampler="down") == (
        "- heartbeat: 🔴 gpu-sampler DOWN (new, since 2026-10-03, 1 night: connection refused)"
        " · 6/7 OK")


# ---- never raises, never waits past its limit ---------------------------------------

def test_a_hanging_probe_is_reported_as_timed_out_within_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "PROBE_LIMIT", 1)
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    t0 = time.monotonic()
    out = line(state, 1, share="hang")
    took = time.monotonic() - t0
    assert out == "- heartbeat: 🔴 share DOWN (since 2026-10-03, 1 night: timed out after 1 s) · 6/7 OK"
    assert took < 2.5, took


def test_hanging_probes_run_side_by_side_not_one_after_another(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "PROBE_LIMIT", 1)
    t0 = time.monotonic()
    hb.run_probes([{"id": i, "kind": "fake", "fake": "hang"} for i in IDS], PROBES)
    assert time.monotonic() - t0 < 2.5


def test_a_probe_that_raises_is_reported_and_the_line_still_comes(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    assert line(state, 1, hdd="raise") == (
        "- heartbeat: 🔴 hdd DOWN (since 2026-10-03, 1 night: probe error: ValueError: boom) · 6/7 OK")


def test_an_unknown_probe_kind_is_reported_not_raised(tmp_path):
    out = hb.heartbeat_line(tmp_path / "s.jsonl", targets=[{"id": "x", "kind": "nope"}],
                            probes=PROBES, now=night(0))
    assert out == ("- heartbeat: 🔴 no state yet, starting fresh"
                   " · x DOWN (since 2026-10-02, 1 night: unknown probe kind 'nope') · 0/1 OK")


def test_a_missing_targets_file_is_a_line_not_a_crash(tmp_path):
    out = hb.heartbeat_line(tmp_path / "s.jsonl", targets_path=tmp_path / "nope.json",
                            probes=PROBES, now=night(0))
    assert out.startswith("- heartbeat: 🔴 unavailable (no targets: "), out
    assert not (tmp_path / "s.jsonl").exists()


def test_an_unwritable_state_is_said_and_the_line_still_comes(tmp_path):
    state = tmp_path / "no_such_dir" / "HEARTBEAT.jsonl"
    out = line(state, 0)
    assert out.startswith("- heartbeat: 🔴 state not written ("), out
    assert out.endswith(" · no state yet, starting fresh · 7/7 OK"), out


# ---- the state file ------------------------------------------------------------------

def test_each_run_appends_one_json_line_of_the_promised_shape(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    line(state, 1, share="down")
    rows = [json.loads(l) for l in state.read_text().splitlines()]
    assert len(rows) == 2
    assert set(rows[1]) == {"ts", "results"}
    assert rows[1]["ts"].startswith("2026-10-03T04:30")
    assert set(rows[1]["results"]) == set(IDS)
    assert rows[1]["results"]["share"] == {"ok": False, "detail": "connection refused"}
    assert rows[1]["results"]["hdd"] == {"ok": True, "detail": "ok"}


def test_a_corrupt_state_is_said_set_aside_and_started_fresh(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    state.write_text(state.read_text() + "{not json\n")
    out = line(state, 1)
    assert out.startswith("- heartbeat: 🔴 state was corrupt (line 2), set aside as HEARTBEAT.jsonl.corrupt-"), out
    assert out.endswith(", starting fresh · 7/7 OK"), out
    aside = list(tmp_path.glob("HEARTBEAT.jsonl.corrupt-*"))
    assert len(aside) == 1 and "{not json" in aside[0].read_text()
    assert len(state.read_text().splitlines()) == 1
    assert line(state, 2) == "- heartbeat: 7/7 OK"


def test_a_state_line_of_the_wrong_shape_counts_as_corrupt(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    state.write_text('{"ts": "2026-10-01T04:30:00", "results": [1, 2]}\n')
    assert line(state, 0).startswith("- heartbeat: 🔴 state was corrupt (line 1)")


def test_dry_reads_the_state_and_writes_nothing(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    line(state, 0)
    line(state, 1, box2_ssh="down")
    before = state.read_bytes()
    out = hb.heartbeat_line(state, targets=targets(box2_ssh="down"), probes=PROBES,
                            now=night(2), write=False)
    assert out == "- heartbeat: 🔴 box2-ssh DOWN (since 2026-10-03, 2 nights: connection refused) · 6/7 OK"
    assert state.read_bytes() == before


def test_dry_on_a_corrupt_state_neither_moves_nor_writes_it(tmp_path):
    state = tmp_path / "HEARTBEAT.jsonl"
    state.write_text("{not json\n")
    out = hb.heartbeat_line(state, targets=targets(), probes=PROBES, now=night(0), write=False)
    assert "state is corrupt (line 1)" in out, out
    assert state.read_text() == "{not json\n"
    assert list(tmp_path.iterdir()) == [state]


# ---- the targets file and the real probes, without the network ------------------------

REAL_IDS = ["tailnet", "odin-drive", "hdd", "gpu-sampler", "render"]   # Helix is independent of other machines, 2026-10-02 (fact 163); render: order 40b


def test_the_targets_file_has_the_five_and_cites_a_source_for_each():
    data = json.loads((HERE / "heartbeat_targets.json").read_text())
    assert [t["id"] for t in data["targets"]] == REAL_IDS
    for t in data["targets"]:
        assert t["kind"] in hb.PROBES, t
        assert t.get("source"), t


def test_the_real_probes_ask_for_short_timeouts():
    data = json.loads((HERE / "heartbeat_targets.json").read_text())
    for t in data["targets"]:
        for key in ("timeout", "tcp_timeout", "cmd_timeout"):
            if key in t:
                assert t[key] <= hb.PROBE_LIMIT, t
    assert hb.PROBE_LIMIT <= 5


def test_no_target_points_at_box2():
    """BOX2 is no longer part of the Helix network (the user, 2026-10-02, fact 163)."""
    raw = (HERE / "heartbeat_targets.json").read_text()
    targets = json.loads(raw)["targets"]
    assert not any(t["id"].startswith("box2") for t in targets)
    assert "192.168.77.5\"" not in json.dumps(targets) and "otheruser" not in json.dumps(targets)


LOCAL_HOSTS = ("127.0.0.1", "localhost")      # exactly these; order 40b


def only_this_machine(t):
    return t.get("host", LOCAL_HOSTS[0]) in LOCAL_HOSTS and "192.168." not in json.dumps(t)


def test_no_target_points_at_another_machine():
    """Helix is independent from other machines (the user, 2026-10-02, fact 163): no LAN host in the real config.
    A `host` is allowed only when it is exactly 127.0.0.1 or localhost (order 40b)."""
    for t in json.loads((HERE / "heartbeat_targets.json").read_text())["targets"]:
        assert only_this_machine(t), t


@pytest.mark.parametrize("host", ["192.168.77.5", "10.0.0.2", "127.0.0.2", "0.0.0.0", "::1", "otheruser",
                                  "localhost.lan", " 127.0.0.1", "127.0.0.1 ", "LOCALHOST", ""])
def test_the_local_host_rule_refuses_every_other_host(host):
    assert not only_this_machine({"id": "x", "kind": "tcp", "host": host, "port": 1})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_the_local_host_rule_allows_exactly_these(host):
    assert only_this_machine({"id": "x", "kind": "tcp", "host": host, "port": 1})


def test_the_local_host_rule_still_refuses_a_lan_address_anywhere():
    assert not only_this_machine({"id": "x", "kind": "tcp", "host": "127.0.0.1", "port": 1,
                                  "note": "192.168.77.5"})


def test_the_render_target_is_a_tcp_connect_to_the_local_daemon():
    data = json.loads((HERE / "heartbeat_targets.json").read_text())
    t = next(t for t in data["targets"] if t["id"] == "render")
    assert (t["kind"], t["host"], t["port"]) == ("tcp", "127.0.0.1", 7900)
    assert "111_" in t["source"] and "ORDER_40" in t["source"]
    assert "port listens" in data["_note"] and "not that the daemon answers" in data["_note"]


def test_no_real_probe_runs_ssh():
    for t in json.loads((HERE / "heartbeat_targets.json").read_text())["targets"]:
        assert "ssh" not in t.get("argv", []), t


def test_tcp_probe_against_a_closed_local_port_says_refused():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert hb.probe_tcp({"host": "127.0.0.1", "port": port, "timeout": 1}) == \
        (False, "connection refused")


def test_tcp_probe_against_an_open_local_port_says_ok():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        assert hb.probe_tcp({"host": "127.0.0.1", "port": s.getsockname()[1], "timeout": 1})[0]
    finally:
        s.close()


def test_command_probe_reports_the_tail_of_stderr(tmp_path):
    t = {"argv": [sys.executable, "-c",
                  "import sys; sys.stderr.write('ssh: connect to host h port 22: Connection refused\\n');"
                  " sys.exit(255)"], "timeout": 2}
    assert hb.probe_command(t) == (False, "exit 255: connection refused")


def test_mount_probe_on_a_plain_directory_is_down(tmp_path):
    assert hb.probe_mount_file({"mountpoint": str(tmp_path), "file": "x"}) == \
        (False, f"{tmp_path} is not a mountpoint")


def test_cli_dry_writes_no_state(tmp_path):
    targets_path = tmp_path / "t.json"
    targets_path.write_text(json.dumps({"targets": [
        {"id": "here", "kind": "mount_file", "mountpoint": "/", "file": "etc/hostname"}]}))
    state = tmp_path / "HEARTBEAT.jsonl"
    r = subprocess.run([sys.executable, str(HERE / "heartbeat.py"), "--dry", "--state", str(state),
                        "--targets", str(targets_path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "here" in r.stdout and "- heartbeat: " in r.stdout
    assert not state.exists()


def test_cli_state_appends_one_line(tmp_path):
    targets_path = tmp_path / "t.json"
    targets_path.write_text(json.dumps({"targets": [
        {"id": "here", "kind": "mount_file", "mountpoint": "/", "file": "etc/hostname"}]}))
    state = tmp_path / "HEARTBEAT.jsonl"
    for _ in range(2):
        r = subprocess.run([sys.executable, str(HERE / "heartbeat.py"), "--state", str(state),
                            "--targets", str(targets_path)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "- heartbeat: 1/1 OK"
    assert len(state.read_text().splitlines()) == 2
