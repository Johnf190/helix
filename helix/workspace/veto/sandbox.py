#!/usr/bin/env python3
"""sandbox.py — the two bwrap sandboxes of Alice's veto (order 48, amendment 9a).

    ALICE         Alice (`agy`, Gemini) runs here. She sees /usr and /etc read-only, her own
                  ~/.gemini (her login; GEMINI.md and config/ bound READ-ONLY on top, so a hostile
                  seal cannot rewrite her standing instructions), the agy binary read-only, and ONE
                  writable directory: the run's scratch. /home, /tmp and /run are empty tmpfs, so
                  ~/helix, /mnt and /media do not exist in there. Network is open (she needs
                  Google). Her session bus is an xdg-dbus-proxy that lets through nothing but
                  org.freedesktop.secrets (her login lives in the keyring), so `systemctl --user`
                  and `busctl --user` cannot reach the user's bus.
    REPRODUCTION  The same recipe WITHOUT agy, the proxy, ~/.gemini or network (--unshare-net).
                  Her break script runs here (order 48b): cwd = a writable `alice/` holding only
                  her output files, beside a PRISTINE copy of the seal bound READ-ONLY at `seal/`.
                  Python is isolated by the environment: PYTHONSAFEPATH=1 (no script dir or cwd
                  on sys.path), PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, no PYTHONPATH.
                  The seal's declared instrument runs here too, cwd = the read-only seal.

The recipe is the shape Odin probed live on 2026-10-03 17:06. /var/run/nscd does not exist here and
bwrap fails if asked to bind it; /run/systemd/resolve must be bound or name lookup fails (DNS is
systemd-resolved at 127.0.0.53). Both sandboxes start from an EMPTY environment (--clearenv) plus
the few variables set below, so nothing in the caller's environment reaches Alice or her script.

Every sandbox runs in its own process group, which is killed (SIGKILL) at the time cap and again
when the sandbox exits; bwrap's --die-with-parent takes its child down with it. The proxy is in
its own process group too, and is killed as soon as Alice's sandbox ends.

There is no switch here that widens either sandbox.
"""

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

BWRAP = "/usr/bin/bwrap"
DBUS_PROXY = "/usr/bin/xdg-dbus-proxy"

HOME = Path.home()
GEMINI = HOME / ".gemini"
AGY = HOME / ".local" / "bin" / "agy"
# Bound read-only on top of the writable ~/.gemini (probed live: agy still runs with these).
GEMINI_READ_ONLY = (GEMINI / "GEMINI.md", GEMINI / "config")
USER_BUS = Path(f"/run/user/{os.getuid()}/bus")
RESOLVE = Path("/run/systemd/resolve")
SECRETS = "org.freedesktop.secrets"

MODEL = "Gemini 3.1 Pro (High)"
PROXY_WAIT = 10                               # seconds for the proxy's socket to appear
PATH_VAR = "/usr/local/bin:/usr/bin:/bin"


def base(scratch, cwd=None, read_only=()):
    """The part both sandboxes share: system read-only, everything personal hidden, one
    writable scratch directory, with `read_only` paths under it bound read-only on top
    (order 48b: the seal). cwd defaults to the scratch directory."""
    scratch = str(scratch)
    ro = []
    for p in read_only:
        ro += ["--ro-bind", str(p), str(p)]
    return [BWRAP,
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/sbin", "/sbin",
            "--ro-bind", "/etc", "/etc",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", "/home",
            "--tmpfs", "/run",
            "--bind", scratch, scratch,
            *ro,
            "--clearenv",
            "--setenv", "HOME", str(HOME),
            "--setenv", "PATH", PATH_VAR,
            "--setenv", "LANG", "C.UTF-8",
            "--unshare-pid",
            "--die-with-parent",
            "--chdir", str(cwd or scratch)]


def alice_argv(scratch, proxy_socket, command, cwd=None, read_only=()):
    """Alice's sandbox around `command` (agy, or `sh -c` in the tests)."""
    argv = base(scratch, cwd, read_only)
    argv += ["--ro-bind", str(RESOLVE), str(RESOLVE),
             "--bind", str(proxy_socket), str(USER_BUS),
             "--setenv", "DBUS_SESSION_BUS_ADDRESS", f"unix:path={USER_BUS}",
             "--setenv", "XDG_RUNTIME_DIR", str(USER_BUS.parent),
             "--bind", str(GEMINI), str(GEMINI)]
    for p in GEMINI_READ_ONLY:
        argv += ["--ro-bind", str(p), str(p)]
    argv += ["--ro-bind", str(AGY), str(AGY)]
    return argv + ["--"] + [str(c) for c in command]


# Order 48b: what she writes beside her script must not change what Python imports.
PYTHON_ISOLATION = (("PYTHONSAFEPATH", "1"), ("PYTHONNOUSERSITE", "1"),
                    ("PYTHONDONTWRITEBYTECODE", "1"))


def reproduction_argv(scratch, command, cwd=None, read_only=()):
    """The reproduction sandbox: no agy, no proxy, no ~/.gemini, no network."""
    argv = base(scratch, cwd, read_only)
    for name, value in PYTHON_ISOLATION:
        argv += ["--setenv", name, value]
    return argv + ["--unshare-net", "--"] + [str(c) for c in command]


def agy_command(prompt):
    return [str(AGY), "-p", prompt, "--model", MODEL,
            "--dangerously-skip-permissions", "--output-format", "json"]


# --------------------------------------------------------------- processes

def _killpg(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run_group(argv, timeout):
    """→ (returncode or None on timeout, stdout, stderr, seconds). Own process group, killed
    at the cap and after exit."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, errors="replace",
                                start_new_session=True, env={"PATH": PATH_VAR})
    except OSError as exc:
        return -1, "", f"cannot start {argv[0]}: {exc}", 0.0
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _killpg(proc)
        out, err = proc.communicate()
        rc, err = None, (err or "") + f"\n[sandbox: killed at the {timeout} s cap]"
    finally:
        _killpg(proc)
    return rc, out or "", err or "", round(time.monotonic() - started, 2)


class Proxy:
    """xdg-dbus-proxy: the user bus, filtered down to org.freedesktop.secrets. A context
    manager; the proxy's process group is killed on exit."""

    def __init__(self):
        self.dir = None
        self.proc = None
        self.socket = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="veto_bus_")
        os.chmod(self.dir, 0o700)
        self.socket = Path(self.dir) / "bus"
        self.proc = subprocess.Popen(
            [DBUS_PROXY, f"unix:path={USER_BUS}", str(self.socket), "--filter",
             f"--talk={SECRETS}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.monotonic() + PROXY_WAIT
        while not self.socket.exists():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.__exit__(None, None, None)
                raise RuntimeError("xdg-dbus-proxy did not come up")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self.proc is not None:
            _killpg(self.proc)
            self.proc.wait()
        if self.dir:
            try:
                if self.socket and self.socket.exists():
                    self.socket.unlink()
                os.rmdir(self.dir)
            except OSError:
                pass
        return False


def run_alice_sandbox(scratch, command, timeout, cwd=None, read_only=()):
    """`command` inside Alice's sandbox, with the proxy up for exactly as long as it runs."""
    with Proxy() as proxy:
        return run_group(alice_argv(scratch, proxy.socket, command, cwd, read_only), timeout)


def run_alice(scratch, prompt, timeout, cwd=None, read_only=()):
    """Alice herself: agy -p <prompt> in her sandbox, cwd = `cwd` (default scratch)."""
    return run_alice_sandbox(scratch, agy_command(prompt), timeout, cwd, read_only)


def run_reproduction(scratch, command, timeout, cwd=None, read_only=()):
    return run_group(reproduction_argv(scratch, command, cwd, read_only), timeout)
