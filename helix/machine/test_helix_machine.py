"""Tests for ORDER 67 Part A — helix-machine, the root program.

The installed program has no test hook (asserted below). So every test copies it into a temp dir
and rewrites the absolute command paths and the /var paths in THE COPY (the `sed` of the order,
done here with str.replace) to point at fakes that record their argv. Nothing here runs as root,
calls sudo, or touches /var, apt, the disks or the GPU.

Run:  python3 -m pytest -q machine/test_helix_machine.py
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE / "helix-machine"
FORBIDDEN = ("remove", "purge", "autoremove", "dist-upgrade", "full-upgrade")
FAKED = ("smartctl", "lsblk", "apt-get", "journalctl", "systemctl", "nvidia-smi", "shutdown",
         "fuser", "timeout", "date", "mv")
PASSTHROUGH = {"date", "mv"}          # real behaviour, not recorded

FAKE = """#!/bin/bash
name=$(basename "$0")
d=$(dirname "$0")
[ -n "{passthrough}" ] && exec /usr/bin/$name "$@"
printf '%s' "$name" >> "$d/calls"; for a in "$@"; do printf '\\t%s' "$a" >> "$d/calls"; done
echo >> "$d/calls"
case "$name" in
  timeout) shift; exec "$@" ;;
  lsblk) printf 'loop0 loop\\nsda   disk\\nsdb   disk\\nsr0   rom\\n' ;;
  smartctl) echo "SMART overall-health self-assessment test result: PASSED" ;;
  nvidia-smi) [ -f "$d/smi_out" ] && cat "$d/smi_out"; exit "$(cat "$d/smi_rc" 2>/dev/null || echo 0)" ;;
esac
exit "$(cat "$d/rc_$name" 2>/dev/null || echo 0)"
"""


@pytest.fixture
def hm(tmp_path):
    """A copy of helix-machine wired to fakes. Returns (run(*args) -> CompletedProcess, root)."""
    fake = tmp_path / "fake"
    fake.mkdir()
    for name in FAKED:
        p = fake / name
        p.write_text(FAKE.replace("{passthrough}", "1" if name in PASSTHROUGH else ""))
        p.chmod(0o755)
    (fake / "rc_fuser").write_text("1")            # fuser exits 1 when nobody holds the lock
    var = tmp_path / "var"
    (var / "lib/helix-machine").mkdir(parents=True)
    (var / "run").mkdir()
    (var / "lib/dpkg").mkdir()
    src = SRC.read_text()
    for d in ("/usr/sbin/", "/usr/bin/"):
        src = src.replace(d, f"{fake}/")
    src = (src.replace("/var/log/helix-machine.log", f"{var}/helix-machine.log")
              .replace("/var/lib/helix-machine", f"{var}/lib/helix-machine")
              .replace("/var/run/reboot-required", f"{var}/run/reboot-required")
              .replace("/var/lib/dpkg/lock-frontend", f"{var}/lib/dpkg/lock-frontend"))
    assert "/usr/bin/" not in src.split("PATH=", 1)[1] and "/usr/sbin/" not in src
    copy = tmp_path / "helix-machine"
    copy.write_text(src)
    copy.chmod(0o755)

    def run(*args):
        return subprocess.run([str(copy), *args], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin"})
    return run, {"fake": fake, "var": var}


def calls(root):
    p = root["fake"] / "calls"
    return [l.split("\t") for l in p.read_text().splitlines()] if p.exists() else []


def log_lines(root):
    p = root["var"] / "helix-machine.log"
    return p.read_text().splitlines() if p.exists() else []


# ------------------------------------------------------------------ the installed file itself

def test_the_installed_program_has_no_test_hook():
    src = SRC.read_text()
    assert "HELIX_MACHINE_TEST" not in src
    assert not re.search(r"\$\{?[A-Z_]*(TEST|ROOT|FAKE)", src), "no variable may redirect it"


def test_it_sets_path_and_u_and_calls_everything_by_absolute_path():
    src = SRC.read_text()
    assert "\nset -u\n" in src and "\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n" in src
    for name in FAKED:
        bare = re.findall(rf"(?<![/\w-]){re.escape(name)} ", "\n".join(
            l for l in src.splitlines() if not l.lstrip().startswith("#")
            and "echo" not in l and "printf" not in l))
        assert bare == [], (name, bare)


def test_it_is_at_most_150_lines_and_executable():
    assert len(SRC.read_text().splitlines()) <= 150
    assert SRC.stat().st_mode & stat.S_IXUSR


def test_update_never_mentions_taking_packages_off_in_the_source():
    src = SRC.read_text()
    for w in FORBIDDEN:
        assert w not in src, w


# ------------------------------------------------------------------ 1. each verb's commands

def test_health_runs_smartctl_on_each_disk_only(hm):
    run, root = hm
    r = run("health")
    assert r.returncode == 0, r.stderr
    assert calls(root) == [["lsblk", "-dno", "NAME,TYPE"],
                           ["smartctl", "-H", "-A", "/dev/sda"],
                           ["smartctl", "-H", "-A", "/dev/sdb"]]
    assert "=== disk sda ===" in r.stdout and "=== exit sdb 0 ===" in r.stdout


def test_update_runs_exactly_update_then_upgrade(hm):
    run, root = hm
    r = run("update")
    assert r.returncode == 0, r.stderr
    c = calls(root)
    assert c[0][0] == "fuser" and c[0][1].endswith("/lib/dpkg/lock-frontend")
    assert c[1:3] == [["timeout", "600", f"{root['fake']}/apt-get", "update"], ["apt-get", "update"]]
    assert c[3][0] == "timeout" and 0 < int(c[3][1]) <= 2700
    assert c[4] == ["apt-get", "-y", "--with-new-pkgs", "-o", "Dpkg::Options::=--force-confold",
                    "upgrade"]
    assert len(c) == 5
    flat = " ".join(" ".join(x) for x in c)
    for w in FORBIDDEN:
        assert w not in flat, w


def test_update_stops_when_apt_get_update_fails(hm):
    run, root = hm
    (root["fake"] / "rc_apt-get").write_text("100")
    r = run("update")
    assert r.returncode == 100
    assert [x for x in calls(root) if x[0] == "apt-get"] == [["apt-get", "update"]]


def test_clean_runs_apt_clean_and_journal_vacuum(hm):
    run, root = hm
    assert run("clean").returncode == 0
    assert calls(root) == [["apt-get", "clean"], ["journalctl", "--vacuum-size=500M"]]


def test_restart_sidekick(hm):
    run, root = hm
    assert run("restart-sidekick").returncode == 0
    assert calls(root) == [["systemctl", "restart", "sidekick.service"]]


# ------------------------------------------------------------------ 2. refusals

@pytest.mark.parametrize("args", [(), ("update", "clean"), ("rm",), ("update;reboot",),
                                  ("../health",), ("HEALTH",), ("",), ("health", "")])
def test_refusals_exit_2_and_run_nothing(hm, args):
    run, root = hm
    r = run(*args)
    assert r.returncode == 2 and "refused" in r.stderr
    assert calls(root) == []
    assert len(log_lines(root)) == 1 and log_lines(root)[0].endswith(" refused 2")


def test_stdin_and_environment_are_not_read(hm):
    run, root = hm
    copy = root["var"].parent / "helix-machine"
    r = subprocess.run([str(copy)], input="health\n", capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "VERB": "health", "LOG": "/tmp/x"})
    assert r.returncode == 2 and calls(root) == []


# ------------------------------------------------------------------ 3. reboot

def test_reboot_with_no_reason_does_nothing(hm):
    run, root = hm
    r = run("reboot")
    assert r.returncode == 4 and "no reason" in r.stderr
    assert [x[0] for x in calls(root)] == ["timeout", "nvidia-smi"]
    assert not (root["var"] / "lib/helix-machine/last_reboot.json").exists()


def test_reboot_when_reboot_required(hm):
    run, root = hm
    (root["var"] / "run/reboot-required").write_text("*** System restart required ***\n")
    r = run("reboot")
    assert r.returncode == 0, r.stderr
    assert calls(root)[-1] == ["shutdown", "-r", "+2", "helix: reboot-required"]
    rec = json.loads((root["var"] / "lib/helix-machine/last_reboot.json").read_text())
    assert rec["reason"] == "reboot-required" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["ts"])


def test_reboot_when_the_gpu_driver_mismatches(hm):
    run, root = hm
    (root["fake"] / "smi_out").write_text(
        "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 580.95\n")
    (root["fake"] / "smi_rc").write_text("18")
    r = run("reboot")
    assert r.returncode == 0, r.stderr
    assert calls(root)[-1] == ["shutdown", "-r", "+2", "helix: GPU driver mismatch"]
    rec = json.loads((root["var"] / "lib/helix-machine/last_reboot.json").read_text())
    assert rec["reason"] == "GPU driver mismatch"


def test_a_failing_nvidia_smi_without_the_mismatch_is_no_reason(hm):
    run, root = hm
    (root["fake"] / "smi_out").write_text("NVIDIA-SMI has failed because it couldn't communicate\n")
    (root["fake"] / "smi_rc").write_text("9")
    assert run("reboot").returncode == 4
    assert "shutdown" not in [x[0] for x in calls(root)]


# ------------------------------------------------------------------ 4. dpkg lock

def test_update_with_the_dpkg_lock_held_is_busy(hm):
    run, root = hm
    (root["fake"] / "rc_fuser").write_text("0")
    r = run("update")
    assert r.returncode == 3 and "busy" in r.stderr
    assert [x[0] for x in calls(root)] == ["fuser"]


def test_update_with_the_lock_free_runs(hm):
    run, root = hm
    (root["fake"] / "rc_fuser").write_text("1")
    assert run("update").returncode == 0
    assert ["apt-get", "update"] in calls(root)


# ------------------------------------------------------------------ 5. the log

def test_every_call_appends_one_log_line(hm):
    run, root = hm
    (root["fake"] / "rc_fuser").write_text("1")
    for args, rc in ((("health",), 0), (("clean",), 0), (("reboot",), 4), (("nope",), 2),
                     (("update",), 0), (("restart-sidekick",), 0)):
        assert run(*args).returncode == rc
    lines = log_lines(root)
    assert len(lines) == 6
    assert [l.split(" ", 1)[1] for l in lines] == ["health 0", "clean 0", "reboot 4", "refused 2",
                                                    "update 0", "restart-sidekick 0"]
    assert all(re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", l) for l in lines)
