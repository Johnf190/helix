"""Order 48, Part A: both sandboxes, with a plain `sh -c` inside (no agy, no spend)."""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import sandbox

HERE = Path(__file__).resolve().parent

CONNECT = ("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3)\" "
           ">/dev/null 2>&1 && echo NET_OPEN || echo NET_CLOSED")


def alice_sh(scratch, script, timeout=30):
    return sandbox.run_alice_sandbox(scratch, ["sh", "-c", script], timeout)


def repro_sh(scratch, script, timeout=30):
    return sandbox.run_reproduction(scratch, ["sh", "-c", script], timeout)


@pytest.fixture(params=["alice", "reproduction"])
def shell(request):
    return alice_sh if request.param == "alice" else repro_sh


def test_each_sandbox_can_write_its_scratch(tmp_path, shell):
    rc, out, err, _ = shell(tmp_path, "echo hello > made.txt && cat made.txt && pwd")
    assert rc == 0, err
    assert out.splitlines() == ["hello", str(tmp_path)]
    assert (tmp_path / "made.txt").read_text() == "hello\n"


@pytest.mark.parametrize("path", ["~/helix", "/mnt", "/media", "~/.ssh",
                                  str(Path.home() / "helix" / "workspace")])
def test_each_sandbox_cannot_see_helix_mnt_media_or_ssh(tmp_path, shell, path):
    rc, out, err, _ = shell(tmp_path, f"ls {path} && echo VISIBLE || echo HIDDEN")
    assert out.strip().splitlines()[-1] == "HIDDEN", out + err
    assert "No such file or directory" in err


def test_each_sandbox_cannot_reach_the_user_bus(tmp_path, shell):
    rc, out, err, _ = shell(tmp_path, "systemctl --user status >/dev/null 2>&1; echo rc=$?")
    assert out.strip() != "rc=0", out + err


def test_systemctl_user_fails_in_alices_sandbox_with_the_probed_message(tmp_path):
    rc, out, err, _ = alice_sh(tmp_path, "systemctl --user status 2>&1; busctl --user list 2>&1")
    assert "Transport endpoint is not connected" in out, out + err


def test_the_reproduction_sandbox_has_no_network(tmp_path):
    rc, out, err, _ = repro_sh(tmp_path, CONNECT)
    assert out.strip() == "NET_CLOSED", out + err


def test_the_reproduction_sandbox_has_no_gemini_and_no_agy(tmp_path):
    rc, out, err, _ = repro_sh(tmp_path, f"ls {sandbox.GEMINI}; ls {sandbox.AGY}; echo done")
    assert out.strip() == "done"
    assert err.count("No such file or directory") == 2, err


def test_alices_standing_instructions_are_read_only(tmp_path):
    for p in sandbox.GEMINI_READ_ONLY:
        target = p if p.is_file() else p / "probe_write"
        rc, out, err, _ = alice_sh(tmp_path, f"touch '{target}' && echo WROTE || echo REFUSED")
        assert out.strip() == "REFUSED", (p, out, err)
        assert "Read-only file system" in err
    assert not (sandbox.GEMINI / "config" / "probe_write").exists()


def test_the_agy_binary_is_read_only_inside(tmp_path):
    rc, out, err, _ = alice_sh(tmp_path, f"touch '{sandbox.AGY}' && echo WROTE || echo REFUSED")
    assert out.strip() == "REFUSED", out + err


def test_the_callers_environment_does_not_reach_the_sandbox(tmp_path, monkeypatch, shell):
    monkeypatch.setenv("VETO_TEST_SECRET", "must-not-leak")
    rc, out, err, _ = shell(tmp_path, "env")
    assert "must-not-leak" not in out
    names = {l.split("=", 1)[0] for l in out.splitlines() if "=" in l}
    assert "HOME" in names and "PATH" in names


def test_the_reproduction_argv_carries_no_agy_proxy_gemini_and_unshares_net(tmp_path):
    argv = sandbox.reproduction_argv(tmp_path, ["sh", "x.sh"])
    joined = " ".join(argv)
    assert "--unshare-net" in argv
    for absent in (str(sandbox.AGY), str(sandbox.GEMINI), str(sandbox.USER_BUS),
                   "DBUS_SESSION_BUS_ADDRESS", str(sandbox.RESOLVE)):
        assert absent not in joined


def test_alices_argv_is_the_probed_shape(tmp_path):
    argv = sandbox.alice_argv(tmp_path, tmp_path / "bus", sandbox.agy_command("hi"))
    assert "--unshare-net" not in argv
    assert "/var/run/nscd" not in " ".join(argv)            # pre-flight correction 1
    triples = [argv[i:i + 3] for i in range(len(argv) - 2)]
    assert ["--ro-bind", str(sandbox.RESOLVE), str(sandbox.RESOLVE)] in triples
    assert ["--bind", str(tmp_path), str(tmp_path)] in triples
    assert ["--bind", str(tmp_path / "bus"), str(sandbox.USER_BUS)] in triples
    assert ["--bind", str(sandbox.GEMINI), str(sandbox.GEMINI)] in triples
    for p in sandbox.GEMINI_READ_ONLY + (sandbox.AGY,):
        assert ["--ro-bind", str(p), str(p)] in triples
    for flag in ("--unshare-pid", "--die-with-parent", "--clearenv"):
        assert flag in argv
    tail = argv[argv.index("--") + 1:]
    assert tail == [str(sandbox.AGY), "-p", "hi", "--model", "Gemini 3.1 Pro (High)",
                    "--dangerously-skip-permissions", "--output-format", "json"]


def test_the_time_cap_kills_the_whole_group(tmp_path, shell):
    t = time.monotonic()
    rc, out, err, secs = shell(tmp_path, "sleep 60 & sleep 60; echo never", timeout=2)
    assert rc is None and "killed at the 2 s cap" in err
    assert time.monotonic() - t < 15
    left = subprocess.run(["pgrep", "-f", "sleep 60 & sleep 60"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


def test_the_proxy_is_gone_when_the_sandbox_ends(tmp_path):
    with sandbox.Proxy() as proxy:
        assert proxy.socket.exists()
        assert proxy.proc.poll() is None
    assert proxy.proc.poll() is not None
    assert not Path(proxy.dir).exists()


def test_the_proxy_lets_through_nothing_but_the_secrets_service(tmp_path):
    rc, out, err, _ = alice_sh(
        tmp_path, "busctl --user call org.freedesktop.DBus /org/freedesktop/DBus "
                  "org.freedesktop.DBus ListNames 2>&1; echo rc=$?")
    assert "org.freedesktop.systemd1" not in out, out


def test_no_escape_hatch_in_the_sandbox():
    src = (HERE / "sandbox.py").read_text()
    hits = [l for l in src.splitlines() if re.search(r"force|override|bypass|skip_check", l, re.I)]
    assert hits == []


# ------------------------------------------- order 48b: a read-only seal/, cwd alice/

def layout(tmp_path):
    (tmp_path / "seal").mkdir()
    (tmp_path / "seal" / "check.py").write_text("print('checked')\n")
    (tmp_path / "alice").mkdir()
    return tmp_path / "seal", tmp_path / "alice"


def test_the_seal_is_read_only_and_cwd_is_alice_in_each_sandbox(tmp_path):
    seal, alice = layout(tmp_path)
    script = ("pwd; echo x > ../seal/check.py && echo WROTE_SEAL || echo SEAL_RO; "
              "touch ../seal/hashlib.py && echo ADDED || echo NO_ADD; "
              "echo mine > mine.txt && echo WROTE_ALICE")
    for run in (lambda: sandbox.run_alice_sandbox(tmp_path, ["sh", "-c", script], 30,
                                                  cwd=alice, read_only=[seal]),
                lambda: sandbox.run_reproduction(tmp_path, ["sh", "-c", script], 30,
                                                 cwd=alice, read_only=[seal])):
        rc, out, err, _ = run()
        assert out.splitlines() == [str(alice), "SEAL_RO", "NO_ADD", "WROTE_ALICE"], out + err
        assert "Read-only file system" in err
    assert (seal / "check.py").read_text() == "print('checked')\n"
    assert not (seal / "hashlib.py").exists()


def test_the_reproduction_sandbox_sets_the_python_isolation_variables(tmp_path):
    rc, out, err, _ = repro_sh(tmp_path, "env")
    env = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "PYTHONPATH" not in env


def test_a_hashlib_py_in_cwd_does_not_shadow_the_stdlib_in_reproduction(tmp_path):
    (tmp_path / "hashlib.py").write_text("raise SystemExit('SHADOWED')\n")
    rc, out, err, _ = repro_sh(tmp_path, "python3 -c 'import hashlib; print(hashlib.__file__)'")
    assert rc == 0, err
    assert out.strip().startswith("/usr/lib/python3"), out + err
    assert not list(tmp_path.rglob("*.pyc"))
