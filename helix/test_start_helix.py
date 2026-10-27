"""ORDER 40/40b Part A: start_helix.sh starts the render daemon when nothing listens on :7900.
ORDER 69: the window opens in Google Chrome (own profile) and falls back to Brave.

The launcher is never run for real here (it kills the live app). These tests read its text, and run
only the render-daemon block, or the browser block, cut out of the text, in bash with fake
programs on PATH and a fake HOME.
"""
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "start_helix.sh"
# the live daemon's cmdline, measured in order 40's pre-flight (pid 880610)
DAEMON_CMD = "__HOME__/render_env/bin/python __HOME__/helix/render_daemon.py"
APP_CMD = f"{HERE}/.venv/bin/python app.py"


def text():
    return LAUNCHER.read_text()


def render_block():
    lines = text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("if ") and ":7900" in l)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(lines[start:end + 1])


def test_the_launcher_has_the_guarded_start():
    block = render_block()
    assert '[ -x "$HOME/render_env/bin/python" ]' in block
    assert re.search(r"! ss -ltn 2>/dev/null \| grep -qE ':7900\[\[:space:\]\]'", block), block
    assert ('setsid nohup "$HOME/render_env/bin/python" "$BRAIN/render_daemon.py" '
            '>> "$BRAIN/render_daemon.log" 2>&1 < /dev/null & disown') in block


def test_the_daemon_starts_before_the_app():
    t = text()
    assert t.index(":7900") < t.index('nohup "$BRAIN/.venv/bin/python" app.py')


def test_the_launcher_never_kills_the_daemon():
    for line in text().splitlines():
        if re.search(r"\b(pkill|kill|killall)\b", line):
            assert "render" not in line and "7900" not in line, line


def pkill_patterns():
    pats = []
    for line in text().splitlines():
        m = re.match(r"""\s*pkill -f (?:-- )?(["'])(.*?)\1""", line)
        if m:
            pats.append(m.group(2).replace("$BRAIN", str(HERE)))
    return pats


def test_the_app_pkills_do_not_match_the_daemon():
    pats = [p for p in pkill_patterns() if "app.py" in p]
    assert len(pats) == 2, pats
    for p in pats:
        assert re.search(p, APP_CMD), p                      # control: they still find the app
        assert not re.search(p, DAEMON_CMD), p


def test_no_pkill_in_the_launcher_matches_the_daemon():
    pats = pkill_patterns()
    assert pats
    for p in pats:
        p = p.replace("$HELIX_CHROME_PROFILE", str(Path.home() / ".config/helix-chrome-profile"))
        p = p.replace("$HELIX_PROFILE", str(Path.home() / "snap/brave/common/helix-profile"))
        assert not re.search(p, DAEMON_CMD), p


def fake_env(tmp_path, listening):
    """A fake HOME whose render python records each start, and a fake `ss`. listening: always,
    never, or 'after' (listening once the stub has run)."""
    home, bindir, brain = tmp_path / "home", tmp_path / "bin", tmp_path / "brain"
    py = home / "render_env" / "bin" / "python"
    for d in (py.parent, bindir, brain):
        d.mkdir(parents=True)
    starts = tmp_path / "starts"
    py.write_text(f'#!/bin/bash\necho "$*" >> "{starts}"\n')
    py.chmod(0o755)
    line = "LISTEN 0      128                      127.0.0.1:7900       0.0.0.0:*"
    other = "LISTEN 0      128                      127.0.0.1:79001      0.0.0.0:*"
    cond = {"always": "true", "never": "false", "after": f'[ -s "{starts}" ]'}[listening]
    ss = bindir / "ss"
    ss.write_text(f'#!/bin/bash\necho "State Recv-Q Send-Q Local Peer"\necho "{other}"\n'
                  f'if {cond}; then echo "{line}"; fi\n')
    ss.chmod(0o755)
    env = dict(os.environ, HOME=str(home), BRAIN=str(brain), PATH=f"{bindir}:{os.environ['PATH']}")
    return env, starts, brain


def run_block(env):
    subprocess.run(["bash", "-c", render_block()], env=env, check=True, timeout=10)


def wait_for(path, secs=5.0):
    end = time.monotonic() + secs
    while time.monotonic() < end and not (path.exists() and path.read_text()):
        time.sleep(0.05)


def test_nothing_listening_starts_the_daemon_once(tmp_path):
    env, starts, brain = fake_env(tmp_path, "never")
    run_block(env)
    wait_for(starts)
    assert starts.read_text().splitlines() == [f"{brain}/render_daemon.py"]
    assert (brain / "render_daemon.log").exists()


def test_a_daemon_already_listening_is_not_started(tmp_path):
    env, starts, _ = fake_env(tmp_path, "always")
    run_block(env)
    time.sleep(0.5)
    assert not starts.exists()


def test_a_second_launch_does_not_start_a_second_daemon(tmp_path):
    env, starts, _ = fake_env(tmp_path, "after")
    run_block(env)
    wait_for(starts)
    run_block(env)
    time.sleep(0.5)
    assert len(starts.read_text().splitlines()) == 1


def test_a_port_that_only_starts_with_7900_does_not_count(tmp_path):
    env, starts, _ = fake_env(tmp_path, "never")          # only :79001 listens
    run_block(env)
    wait_for(starts)
    assert len(starts.read_text().splitlines()) == 1


def test_no_render_python_means_no_start(tmp_path):
    env, starts, _ = fake_env(tmp_path, "never")
    (Path(env["HOME"]) / "render_env" / "bin" / "python").unlink()
    run_block(env)
    time.sleep(0.5)
    assert not starts.exists()


# ---- ORDER 69: the browser block -------------------------------------------------------------

V7_URL = "http://127.0.0.1:7777/static/index_v7.html"
# the user's normal Chrome and sidekick's window: no launcher pkill may match either
NORMAL_CHROME = ["/opt/google/chrome/chrome",
                 "/opt/google/chrome/chrome --type=renderer --crashpad-handler-pid=1234",
                 "/opt/google/chrome/chrome --app=http://127.0.0.1:7790/"]


def browser_block():
    """From the URL= line to the end: the part that closes old windows and opens the new one."""
    lines = text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("URL="))
    return "\n".join(lines[start:])


def fake_browsers(tmp_path, names):
    """A fake HOME and a PATH holding ONLY stubs: each of `names`, plus pkill, sleep and xdg-open.
    Every stub writes its name and args, one per line, to tmp_path/<name>.log."""
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    for n in list(names) + ["pkill", "sleep", "xdg-open"]:
        stub = bindir / n
        stub.write_text(f'#!/bin/bash\nfor a in "$@"; do echo "$a"; done >> "{tmp_path}/{n}.log"\n'
                        f'echo "--" >> "{tmp_path}/{n}.log"\n')
        stub.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=str(bindir))
    return env, home


def run_browser_block(env):
    subprocess.run(["/bin/bash", "-c", browser_block()], env=env, check=True, timeout=10)


def log(tmp_path, name):
    f = tmp_path / f"{name}.log"
    return f.read_text().splitlines() if f.exists() else []


def test_chrome_is_preferred_over_brave(tmp_path):
    env, home = fake_browsers(tmp_path, ["google-chrome", "brave", "brave-browser"])
    run_browser_block(env)
    args = log(tmp_path, "google-chrome")
    assert f"--user-data-dir={home}/.config/helix-chrome-profile" in args, args
    assert f"--app={V7_URL}" in args, args
    assert "--start-fullscreen" in args, args
    assert log(tmp_path, "brave") == [] and log(tmp_path, "brave-browser") == []
    assert log(tmp_path, "xdg-open") == []


def test_both_old_helix_windows_are_closed_before_chrome(tmp_path):
    env, home = fake_browsers(tmp_path, ["google-chrome", "brave"])
    run_browser_block(env)
    pk = log(tmp_path, "pkill")
    assert f"--user-data-dir={home}/.config/helix-chrome-profile" in pk, pk
    assert f"--user-data-dir={home}/snap/brave/common/helix-profile" in pk, pk


def test_only_brave_falls_back_to_the_snap_profile(tmp_path):
    env, home = fake_browsers(tmp_path, ["brave"])
    run_browser_block(env)
    args = log(tmp_path, "brave")
    assert f"--user-data-dir={home}/snap/brave/common/helix-profile" in args, args
    assert f"--app={V7_URL}" in args, args
    pk = log(tmp_path, "pkill")
    assert f"--user-data-dir={home}/.config/helix-chrome-profile" in pk, pk
    assert f"--user-data-dir={home}/snap/brave/common/helix-profile" in pk, pk


def test_no_browser_still_ends_in_xdg_open(tmp_path):
    env, _ = fake_browsers(tmp_path, [])
    run_browser_block(env)
    assert log(tmp_path, "xdg-open") == [V7_URL, "--"]


def test_no_pkill_matches_users_normal_chrome_or_sidekick():
    home = str(Path.home())
    helix_chrome = (f"/opt/google/chrome/chrome --user-data-dir={home}/.config/helix-chrome-profile "
                    f"--app={V7_URL} --start-fullscreen")
    pats = [p.replace("$HELIX_CHROME_PROFILE", f"{home}/.config/helix-chrome-profile")
             .replace("$HELIX_PROFILE", f"{home}/snap/brave/common/helix-profile")
            for p in pkill_patterns()]
    assert any(re.search(p, helix_chrome) for p in pats), pats      # control: the Helix window is found
    for p in pats:
        for cmd in NORMAL_CHROME:
            assert not re.search(p, cmd), (p, cmd)
