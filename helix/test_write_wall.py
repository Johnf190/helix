"""Tests for ORDER 23c — the OS-level write wall on Odin's shell (item 25).

app.py writes a wrapper (wall_exec.sh) and hands its path to the CLI as CLAUDE_CODE_SHELL_PREFIX
on the CHAT and WORK lanes. The CLI calls it with ONE argument, the whole command string, and the
wrapper runs that string under bwrap with the guarded paths bound read-only.

Every test that builds options or writes a wrapper points WALL_EXEC at tmp_path first (autouse
fixture), and every write test uses a temp copy of the guarded tree. **Nothing here writes to
~/helix/wall_exec.sh or to any real guarded path.** The real-config tests only read, and check
the mount table inside the wall.

Run with the venv python (app.py needs flask):  .venv/bin/python -m pytest -q test_write_wall.py
The live CLI probe is opt-in and costs a little:  HELIX_WALL_LIVE=1 .venv/bin/python -m pytest ...
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent
sys.path.insert(0, str(HELIX))
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
import app  # noqa: E402
import factgate_hook  # noqa: E402

REAL_WALL_EXEC = app.WALL_EXEC
REAL_WALL_PATHS = app.WALL_PATHS
REAL_WALL_ANCESTORS = app.WALL_ANCESTORS
BLOCKED = ("Read-only file system", "Operation not permitted", "Device or resource busy")


@pytest.fixture(autouse=True)
def _wrapper_in_tmp(tmp_path, monkeypatch):
    """No test may write the real wrapper."""
    monkeypatch.setattr(app, "WALL_EXEC", str(tmp_path / "wx" / "wall.sh"))
    (tmp_path / "wx").mkdir()


def _snapshot(paths):
    """Every file under the guarded paths, with its bytes' hash. Dirs listed by name."""
    out = {}
    for p in paths:
        p = Path(p)
        if p.is_file():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        elif p.is_dir():
            out[str(p)] = "dir"
            for q in sorted(p.rglob("*")):
                out[str(q)] = (hashlib.sha256(q.read_bytes()).hexdigest() if q.is_file()
                               else "dir")
        else:
            out[str(p)] = None
    return out


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A temp helix-shaped tree. Names avoid the real guarded folder names on purpose."""
    h = tmp_path / "hx"
    for d in ("mem/fx", "ws/gt", "ws/sm/rg", "ws/ct", "ws/scratch", "bo/reports", "ws/cy"):
        (h / d).mkdir(parents=True)
    files = {"mem/fx/001_a.md": "# a\n", "ws/gt/g.py": "print(1)\n", "ws/sm/rg/r.json": "{}\n",
             "ws/ct/c.jsonl": "{}\n", "hook.py": "x\n", "hooktest.py": "x\n", "app.py": "x\n",
             "launch.sh": "x\n", "bo/o.md": "x\n", "bo/reports/r.md": "x\n",
             "mem/other.md": "free\n", "ws/cy/TURNS.jsonl": "{}\n"}
    for k, v in files.items():
        (h / k).write_text(v)
    wall_exec = h / "wall.sh"
    guarded = [h / "mem/fx", h / "ws/gt", h / "ws/sm/rg", h / "ws/ct", h / "hook.py",
               h / "hooktest.py", h / "app.py", h / "launch.sh", wall_exec, h / "bo", h / "ws/cy"]
    ancestors = [h, h / "mem", h / "ws", h / "ws/sm"]
    monkeypatch.setattr(app, "WALL_EXEC", str(wall_exec))
    monkeypatch.setattr(app, "WALL_PATHS", tuple(map(str, guarded)))
    monkeypatch.setattr(app, "WALL_ANCESTORS", tuple(map(str, ancestors)))
    src = tmp_path / "src.txt"
    src.write_text("incoming\n")
    prefix = app._write_wall()
    assert prefix == str(wall_exec)
    return {"h": h, "guarded": guarded, "prefix": prefix, "src": src, "tmp": tmp_path}


def run(prefix, cmd, timeout=30):
    """Exactly how the CLI calls the prefix: one argument, the whole command."""
    return subprocess.run([prefix, cmd], capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------ property 1: guarded = read-only

GUARDED_DIRS = ["mem/fx", "ws/gt", "ws/sm/rg", "ws/ct", "bo", "bo/reports", "ws/cy"]  # ws/cy: order 27
DIR_OPS = {
    "python open": "python3 -c \"open('{d}/new.md','w').write('x')\"",
    "echo >>": "echo x >> {d}/{f}",
    "cp in": "cp {src} {d}/",
    "mv in": "mv {src} {d}/moved.txt",
    "mv out": "mv {d}/{f} {tmp}/out.txt",
    "rm": "rm -f {d}/{f}",
    "rm -rf dir": "rm -rf {d}",
    "sed -i": "sed -i s/a/b/ {d}/{f}",
    "touch existing": "touch {d}/{f}",
    "touch new": "touch {d}/new2.md",
    "mkdir": "mkdir {d}/sub",
    "rename dir": "mv {d} {d}_x",
}


@pytest.mark.parametrize("op", sorted(DIR_OPS))
@pytest.mark.parametrize("rel", GUARDED_DIRS)
def test_p1_every_shell_write_into_a_guarded_dir_fails(tree, rel, op):
    d = tree["h"] / rel
    f = next(p.name for p in sorted(d.iterdir()) if p.is_file())
    before = _snapshot(tree["guarded"])
    cmd = DIR_OPS[op].format(d=d, f=f, src=tree["src"], tmp=tree["tmp"])
    r = run(tree["prefix"], cmd)
    assert r.returncode != 0, f"{op} into {rel} SUCCEEDED: {r.stdout}{r.stderr}"
    assert any(b in r.stderr for b in BLOCKED), r.stderr
    assert _snapshot(tree["guarded"]) == before, f"{op} changed {rel}"


GUARDED_FILES = ["hook.py", "hooktest.py", "app.py", "launch.sh", "wall.sh"]
FILE_OPS = {
    "python open": "python3 -c \"open('{p}','a').write('x')\"",
    "echo >>": "echo x >> {p}",
    "echo >": "echo x > {p}",
    "cp over": "cp {src} {p}",
    "mv over": "mv {src} {p}",
    "mv out": "mv {p} {tmp}/out.txt",
    "rm": "rm -f {p}",
    "sed -i": "sed -i s/x/y/ {p}",
    "touch": "touch {p}",
    "chmod": "chmod 777 {p}",
}


@pytest.mark.parametrize("op", sorted(FILE_OPS))
@pytest.mark.parametrize("rel", GUARDED_FILES)
def test_p1_every_shell_write_to_a_guarded_file_fails(tree, rel, op):
    p = tree["h"] / rel
    before = _snapshot(tree["guarded"])
    r = run(tree["prefix"], FILE_OPS[op].format(p=p, src=tree["src"], tmp=tree["tmp"]))
    assert r.returncode != 0, f"{op} on {rel} SUCCEEDED: {r.stdout}{r.stderr}"
    assert any(b in r.stderr for b in BLOCKED), r.stderr
    assert _snapshot(tree["guarded"]) == before, f"{op} changed {rel}"


@pytest.mark.parametrize("rel", ["", "mem", "ws", "ws/sm"])
def test_p1_renaming_an_ancestor_fails(tree, rel):
    a = tree["h"] / rel if rel else tree["h"]
    r = run(tree["prefix"], f"mv {a} {a}_moved")
    assert r.returncode != 0, f"renaming {a} SUCCEEDED"
    assert "Device or resource busy" in r.stderr, r.stderr
    assert a.is_dir() and not Path(f"{a}_moved").exists()


def test_p1_reading_guarded_paths_still_works(tree):
    r = run(tree["prefix"], f"cat {tree['h']}/mem/fx/001_a.md {tree['h']}/app.py; "
                            f"ls {tree['h']}/bo/reports")
    assert r.returncode == 0 and "# a" in r.stdout and "r.md" in r.stdout, r.stderr


# ------------------------------------------------------------------ property 3: normal work is unaffected

def test_p3_writes_outside_the_guarded_paths_work(tree):
    h = tree["h"]
    r = run(tree["prefix"],
            f"set -e; echo x > {h}/ws/scratch/f.txt; mkdir {h}/ws/newproj; "
            f"echo y > {h}/ws/newproj/g.txt; echo z > {h}/new_top.txt; "
            f"echo w > {h}/mem/other.md; mkdir {h}/ws/sm/newdir; "
            f"T=$(mktemp /tmp/wallprobe.XXXX); echo t > $T; rm $T; "
            f"H=$(mktemp ~/.wallprobe.XXXX); echo t > $H; rm $H; echo DONE")
    assert r.returncode == 0 and "DONE" in r.stdout, r.stderr
    assert (h / "ws/newproj/g.txt").read_text() == "y\n"
    assert (h / "mem/other.md").read_text() == "w\n"


def test_p3_dev_null_redirects_work(tree):
    r = run(tree["prefix"], "echo a >/dev/null; ls /nonexistent 2>/dev/null; "
                            "echo b &>/dev/null; echo REDIR_OK")
    assert r.returncode == 0 and "REDIR_OK" in r.stdout, r.stderr
    assert "Permission denied" not in r.stderr


@pytest.mark.skipif(shutil.which("nvidia-smi") is None, reason="no nvidia-smi on this box")
def test_p3_nvidia_smi_sees_the_gpu(tree):
    r = run(tree["prefix"], "nvidia-smi -L")
    assert r.returncode == 0 and "GPU" in r.stdout, r.stderr


def _reachable(url):
    try:
        urllib.request.urlopen(url, timeout=5)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _reachable("http://127.0.0.1:7777/stat"), reason="Helix not up")
def test_p3_curl_to_helix_works(tree):
    r = run(tree["prefix"], "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7777/stat")
    assert r.returncode == 0 and r.stdout == "200", (r.stdout, r.stderr)


@pytest.mark.skipif(not _reachable("https://api.anthropic.com"), reason="no outbound network")
def test_p3_outbound_https_works(tree):
    r = run(tree["prefix"], "curl -s -o /dev/null -w '%{http_code}' https://api.anthropic.com")
    assert r.returncode == 0 and r.stdout.isdigit() and r.stdout != "000", (r.stdout, r.stderr)


def test_p3_git_in_a_workspace_repo_works(tree):
    repo = tree["h"] / "ws/scratch/repo"
    r = run(tree["prefix"],
            f"set -e; git init -q {repo}; cd {repo}; echo a > a; git add a; "
            f"git -c user.email=t@t -c user.name=t commit -qm one; git log --oneline | wc -l")
    assert r.returncode == 0 and r.stdout.strip() == "1", r.stderr


def test_p3_the_venv_python_runs(tree):
    r = run(tree["prefix"], f"{HELIX}/.venv/bin/python -c 'import flask; print(\"VENV_OK\")'")
    assert r.returncode == 0 and "VENV_OK" in r.stdout, r.stderr


def test_p3_a_background_command_outlives_the_call(tree):
    out = tree["h"] / "ws/scratch/bg.txt"
    r = run(tree["prefix"], f"nohup sh -c 'sleep 0.5; echo bg > {out}' >/dev/null 2>&1 &")
    assert r.returncode == 0, r.stderr
    for _ in range(50):
        if out.exists():
            break
        time.sleep(0.1)
    assert out.read_text() == "bg\n"


def test_p3_the_backup_drive_is_not_walled(tree):
    """Checked without writing: the backup drive is read-only for builders (BUILD_PROTOCOL §1).
    Inside the wall its mount must still be rw, and access(W_OK) must say writable."""
    m = "__VAULT_DRIVE__"
    if not os.path.ismount(m):
        pytest.skip("backup drive not mounted")
    r = run(tree["prefix"], f"{_TOP_MOUNT.format(m)}; test -w {m} && echo W_OK")   # order 32
    assert r.returncode == 0 and r.stdout.split() == ["rw", "W_OK"], r.stdout


# ------------------------------------------------------------------ the real configuration (read-only checks)

@pytest.fixture
def real_wall(monkeypatch):
    monkeypatch.setattr(app, "WALL_PATHS", REAL_WALL_PATHS)
    monkeypatch.setattr(app, "WALL_ANCESTORS", REAL_WALL_ANCESTORS)
    return app._write_wall()


# Order 29: run from an already-walled shell (Odin's), the nested bwrap stacks the same mount
# 2-4 times and findmnt lists every layer. The LAST one listed is the top-most mount -- the one
# a process actually sees -- so each check reads that one, on any nesting depth.
_TOP_MOUNT = "findmnt -n -o OPTIONS --target {} | tail -n 1 | cut -d, -f1"


def test_real_config_mounts_every_guarded_path_read_only(real_wall):
    guarded = [p for p in REAL_WALL_PATHS if os.path.exists(p)]
    checks = "; ".join(f"echo \"{p} $({_TOP_MOUNT.format(p)})\"; "
                       f"test -w {p} && echo \"{p} WRITABLE\"" for p in guarded)
    r = run(real_wall, checks)
    assert "WRITABLE" not in r.stdout, r.stdout
    lines = [l for l in r.stdout.splitlines() if l]
    assert lines == [f"{p} ro" for p in guarded], r.stdout      # one line per path, each ro


def test_real_config_leaves_workspace_scratch_and_reads_working(real_wall):
    r = run(real_wall, f"set -e; D=$(mktemp -d {HELIX}/workspace/.wallprobe.XXXX); "
                       f"echo x > $D/f; mkdir $D/sub; rm -r $D; head -c 20 {HELIX}/app.py >/dev/null; "
                       f"ls {HELIX}/memory/facts >/dev/null; "
                       f"{_TOP_MOUNT.format(f'{HELIX}/workspace')}")
    assert r.returncode == 0 and r.stdout.strip() == "rw", (r.stdout, r.stderr)


# ------------------------------------------------------------------ property 4: lanes

def _prefix_of(opts):
    return (opts.env or {}).get("CLAUDE_CODE_SHELL_PREFIX")


def test_p4_chat_and_work_are_walled_sched_and_front_are_not():
    assert _prefix_of(app._lane_options(app.CHAT)) == app.WALL_EXEC
    assert _prefix_of(app._lane_options(app.WORK)) == app.WALL_EXEC
    assert _prefix_of(app._lane_options(app.SCHED)) is None
    assert _prefix_of(app._lane_options(app.FRONT)) is None
    assert app.SCHED.name == "sched" and not app.SCHED.no_tools


def test_p4_sched_options_are_otherwise_the_same_as_work():
    w, s = app._lane_options(app.WORK), app._lane_options(app.SCHED)
    assert s.hooks == w.hooks and s.permission_mode == w.permission_mode
    assert s.disallowed_tools == w.disallowed_tools and s.cwd == w.cwd


def test_p4_unwalled_shell_can_write_a_guarded_path(tree):
    """SCHED's shell has no prefix: the same command that the wall refuses runs plainly."""
    p = tree["h"] / "mem/fx/001_a.md"
    assert run(tree["prefix"], f"echo x >> {p}").returncode != 0
    r = subprocess.run(["/bin/bash", "-c", f"echo x >> {p}"], capture_output=True, text=True)
    assert r.returncode == 0 and p.read_text() == "# a\nx\n"


@pytest.fixture
def sched_store(tmp_path, monkeypatch):
    import scheduler
    store = tmp_path / "schedules.json"
    scheduler.save(str(store), [{"id": "s1", "name": "n", "prompt": "nightly", "every_minutes": 60,
                                 "enabled": True, "last_run": None}])
    monkeypatch.setattr(app, "SCHEDULES", str(store))
    calls = []
    monkeypatch.setattr(app, "start_job", lambda text, **k: calls.append((text, k)) or type(
        "B", (), {"id": "j"})())
    return calls


def test_p4_the_scheduler_runs_on_sched(sched_store):
    app.scheduler_tick()
    assert sched_store == [("nightly", {"lane": app.SCHED})]


@pytest.mark.parametrize("busy", ["WORK", "SCHED"])
def test_p4_the_scheduler_skips_while_work_or_sched_is_busy(sched_store, busy):
    lane = getattr(app, busy)
    lane.lock.acquire()
    try:
        app.scheduler_tick()
    finally:
        lane.lock.release()
    assert sched_store == []


def test_p4_run_now_runs_on_sched(sched_store):
    with app.app.test_client() as tc:
        r = tc.post("/schedules/s1/run").get_json()
    assert r["ok"] is True
    assert sched_store == [("nightly", {"lane": app.SCHED})]


def test_p4_start_job_defaults_to_work_and_passes_the_lane(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "_produce", lambda buf, lane=None: seen.append(lane))
    monkeypatch.setattr(app, "_report_job", lambda buf: None)
    monkeypatch.setattr(app, "_announce", lambda buf: None)
    app.start_job("a")
    app.start_job("b", lane=app.SCHED)
    for _ in range(50):
        if len(seen) == 2:
            break
        time.sleep(0.05)
    assert sorted(l.name for l in seen) == ["sched", "work"]


def test_p4_a_busy_sched_blocks_restart_and_counts_as_busy(monkeypatch):
    def no_restart(*a, **k):
        raise AssertionError("_do_restart tried to launch start_helix.sh")
    monkeypatch.setattr(subprocess, "Popen", no_restart)
    monkeypatch.setattr(app, "_builder_active", lambda: (None, None))
    app.SCHED.lock.acquire()
    try:
        assert app._lane_busy() is True
        msg = app._do_restart()
    finally:
        app.SCHED.lock.release()
    assert msg.startswith("not restarting") and "sched" in msg, msg


# ------------------------------------------------------------------ property 5 / R6: fail closed

def test_p5_bwrap_missing_refuses_every_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert shutil.which("bwrap") is None
    prefix = app._write_wall()
    assert prefix == app.WALL_EXEC
    marker = tmp_path / "ran"
    r = run(prefix, f"echo ran > {marker}")
    assert r.returncode == 126 and "WALL: bwrap missing, command refused" in r.stderr
    assert not marker.exists()
    assert _prefix_of(app._lane_options(app.WORK)) == app.WALL_EXEC   # never unwalled


def test_r6_an_unwritable_wrapper_fails_closed(tmp_path, monkeypatch):
    d = tmp_path / "ro"
    d.mkdir()
    monkeypatch.setattr(app, "WALL_EXEC", str(d / "wall.sh"))
    d.chmod(0o555)
    try:
        prefix = app._write_wall()
        assert prefix != app.WALL_EXEC
        marker = tmp_path / "ran"
        r = run(prefix, f"echo ran > {marker}")
        assert r.returncode == 126 and r.stderr.startswith("WALL:"), r.stderr
        assert not marker.exists()
    finally:
        d.chmod(0o755)


def test_r6_a_tampered_wrapper_is_rewritten(tree):
    """Outside the wall (e.g. app.py's own process) someone edits it: the next build restores it."""
    w = Path(tree["prefix"])
    good = w.read_bytes()
    w.write_text("#!/bin/bash\nexec /bin/bash -c \"$1\"\n")
    assert app._write_wall() == str(w) and w.read_bytes() == good


def test_the_wrapper_refuses_anything_but_one_argument(tree):
    r = subprocess.run([tree["prefix"], "echo a", "echo b"], capture_output=True, text=True)
    assert r.returncode == 126 and r.stderr.startswith("WALL:"), r.stderr


def test_the_wrapper_shape(tree):
    w = Path(tree["prefix"])
    text = w.read_text()
    assert stat.S_IMODE(w.stat().st_mode) == 0o755
    assert app._wall_script(shutil.which("bwrap")) == text
    args = text.split("exec ", 1)[1]
    assert [t for t in args.split() if t != "\\"][:7] == [shutil.which("bwrap"), "--bind", "/", "/",
                                "--dev-bind", "/dev", "/dev"]
    assert "--dev /dev" not in text and "--proc" not in text and "--unshare" not in text
    assert args.rstrip().endswith('-- /bin/bash -c "$1"')
    # ancestors (rw self-binds) come before every ro-bind
    assert args.rfind("--bind ") < args.find("--ro-bind-try ")


# ------------------------------------------------------------------ R3: drift with the hook

def test_r3_every_hook_guarded_path_lies_inside_the_wall():
    walled = [os.path.realpath(p) for p in REAL_WALL_PATHS]

    def inside(p):
        p = os.path.realpath(p)
        return any(p == w or p.startswith(w + os.sep) for w in walled)

    hook = ([factgate_hook.FACTS, factgate_hook.CATCHES, factgate_hook.RUNNER]
            + factgate_hook._factgate_paths() + list(factgate_hook._P7_FILES)
            + list(factgate_hook._P7_DIRS))
    missing = [p for p in hook if not inside(p)]
    assert missing == [], f"hook guards these but the wall does not: {missing}"


def test_r3_the_wrapper_is_guarded_by_both():
    assert os.path.realpath(REAL_WALL_EXEC) in [os.path.realpath(p) for p in REAL_WALL_PATHS]
    assert factgate_hook._build_path_owns(REAL_WALL_EXEC)


def test_r3_the_wall_set_is_the_order_23c_list():
    want = ["memory/facts", "workspace/factgate", "workspace/seam/registry", "workspace/catches",
            "factgate_hook.py", "test_factgate_hook.py", "app.py", "start_helix.sh",
            "wall_exec.sh", "build_orders", "workspace/cycle",           # + order 27
            "nightly_restart.sh", ".lkg",                                # + order 31
            "memory/PINNED.txt",                                         # + order 33b
            "workspace/cycle/PATCHES.jsonl",                             # + order 34
            "memory/archive"]                                            # + order 35b
    assert list(REAL_WALL_PATHS) == [os.path.realpath(HELIX / w) for w in want]
    assert list(REAL_WALL_ANCESTORS) == [os.path.realpath(HELIX / a) for a in
                                         ("", "memory", "workspace", "workspace/seam")]


# ------------------------------------------------------------------ R5: hooks are in-process callbacks

def test_r5_the_fact_hooks_are_python_callbacks_not_shell_commands():
    h = app._lane_options(app.WORK).hooks
    assert [f for m in h["PreToolUse"] for f in m.hooks] == [factgate_hook.pre_tool_use]
    assert [f for m in h["PostToolUse"] for f in m.hooks] == [factgate_hook.post_tool_use]


# ------------------------------------------------------------------ live CLI probe (opt-in, R9)

@pytest.mark.skipif(os.environ.get("HELIX_WALL_LIVE") != "1", reason="set HELIX_WALL_LIVE=1")
def test_live_cli_bash_is_walled_and_write_tool_is_recorded(tree, tmp_path, monkeypatch):
    """One haiku turn on WORK with every guarded path in temp. The Bash tool's write into the
    fact dir must fail with EROFS; the Write tool's write must land and be recorded."""
    h = tree["h"]
    fx, state = h / "mem/fx", tmp_path / "gstate"
    state.mkdir()
    real_fg = HELIX / "workspace" / ("fact" + "gate") / ("fact" + "gate.py")
    subprocess.run([sys.executable, str(real_fg), "--state-dir", str(state), "baseline", str(fx)],
                   check=True, capture_output=True)
    monkeypatch.setattr(factgate_hook, "FACTS", str(fx))
    monkeypatch.setattr(factgate_hook, "FACTGATE_DIR", str(h / "ws/gt"))
    monkeypatch.setattr(factgate_hook, "FACTGATE", str(real_fg))
    monkeypatch.setattr(factgate_hook, "FACTGATE_STATE", str(state))
    monkeypatch.setattr(factgate_hook, "CATCHES", str(h / "ws/ct/c.jsonl"))
    monkeypatch.setattr(factgate_hook, "BUILD_ORDERS", str(h / "bo"))
    monkeypatch.setattr(app, "WORKSPACE", str(h / "ws/scratch"))
    monkeypatch.setattr(app, "ODIN_MODEL", "haiku")
    monkeypatch.setattr(app, "TURN_BUDGET_USD", 0.5)
    shell_target, tool_target = fx / "002_shell.md", fx / "003_tool.md"
    prompt = (f"Do exactly these two steps, nothing else, then reply DONE.\n"
              f"1. Use the Bash tool to run: python3 -c \"open('{shell_target}','w').write('s')\"; "
              f"echo RC=$?\n"
              f"2. Use the Write tool to create {tool_target} with the content: '# tool\\n\\nok.\\n'")

    async def go():
        from claude_agent_sdk import ClaudeSDKClient
        tool_results = []
        async with ClaudeSDKClient(app._lane_options(app.WORK)) as c:
            await c.query(prompt)
            async for m in c.receive_response():
                for b in getattr(m, "content", None) or []:
                    if type(b).__name__ == "ToolResultBlock":
                        tool_results.append(str(b.content))
        return tool_results

    results = asyncio.run(go())
    blob = "\n".join(results)
    print(blob)
    assert "Read-only file system" in blob, blob
    assert not shell_target.exists()
    assert tool_target.exists(), blob
    ledger = "".join(p.read_text() for p in state.rglob("*") if p.is_file())
    assert tool_target.name in ledger, "the Write tool's file was not recorded"


# ------------------------------------------------------------------ order 27: the cycle ledgers

def test_order27_the_cycle_folder_exists_so_the_wall_binds_it():
    """--ro-bind-try skips a missing path silently; the folder must exist before the wall."""
    assert os.path.isdir(HELIX / "workspace" / "cycle")


def test_order27_real_wall_makes_the_cycle_ledgers_read_only(real_wall):
    p = HELIX / "workspace" / "cycle"
    r = run(real_wall, f"{_TOP_MOUNT.format(p)}; "                  # order 32: top-most layer
                       f"test -w {p} && echo WRITABLE; true")
    assert r.stdout.split() == ["ro"], r.stdout + r.stderr


def test_order27_turns_ledger_is_off_when_app_is_imported():
    """Only the server process writes TURNS.jsonl; a test importing app never touches it."""
    assert app.TURNS_LEDGER is None


def test_order27_a_turn_appends_one_utc_line_to_turns_jsonl(tmp_path, monkeypatch):
    led = tmp_path / "TURNS.jsonl"
    monkeypatch.setattr(app, "TURNS_LEDGER", str(led))
    led.write_text('{"ts": "2026-10-01T00:00:00Z", "lane": "old"}\n')
    payload = {"input_tokens": 3, "output_tokens": 40, "cache_read": 900, "cache_write": 12,
               "cost_usd": 0.0123, "duration_ms": 2100, "num_turns": 1}
    app._log_turn_usage(app.CHAT, payload)
    old, new = [json.loads(l) for l in led.read_text().splitlines()]
    assert old["lane"] == "old"                                     # appended, not rewritten
    assert new["lane"] == "chat" and new["model"] and new["ts"].endswith("Z")
    assert {k: new[k] for k in payload} == payload


def test_order27_a_failing_turns_write_logs_one_line_and_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app, "TURNS_LEDGER", str(tmp_path / "no" / "such" / "dir" / "T.jsonl"))
    app._log_turn_usage(app.CHAT, {"cost_usd": 1.0})               # must not raise
    app._log_turn_usage(app.CHAT, None)
    out = capsys.readouterr().out
    assert out.count("TURNS.jsonl") == 0 and out.count("could not append") == 2, out


def test_order27_run_turn_writes_the_turns_line_from_its_result_message(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage
    led = tmp_path / "TURNS.jsonl"
    monkeypatch.setattr(app, "TURNS_LEDGER", str(led))

    class Client:
        async def query(self, *a, **k):
            pass

        def receive_messages(self):
            async def gen():
                yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=5,
                                    is_error=False, num_turns=2, session_id="s",
                                    total_cost_usd=0.25, usage={"input_tokens": 7,
                                                                "output_tokens": 11},
                                    result="hi")
            return gen()

        async def disconnect(self):
            pass

    import queue as _queue
    lane = app.Lane("chat-test27")
    lane.client, lane.primed = Client(), True
    q = _queue.Queue()

    async def drive():
        lane.reader_task = asyncio.get_running_loop().create_task(app._read_stream(lane.client, lane))
        await app._run_turn("hello", q, lane)
    monkeypatch.setattr(app, "_mark_model_state", lambda *a, **k: None)
    asyncio.run(drive())
    [rec] = [json.loads(l) for l in led.read_text().splitlines()]
    assert rec["lane"] == "chat-test27" and rec["cost_usd"] == 0.25 and rec["num_turns"] == 2
    assert rec["input_tokens"] == 7 and rec["output_tokens"] == 11, rec


# ------------------------------------------------------------------ order 28: the attempt ledger

def test_order28_odins_shell_cannot_open_the_attempt_ledger_for_writing(real_wall):
    """Inside the real wall, an append-open of workspace/cycle/ATTEMPTS.jsonl fails. Guarded by
    the mount check in the same shell, so a broken wall can never create the live file."""
    p = HELIX / "workspace" / "cycle" / "ATTEMPTS.jsonl"
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p.parent)})\" = ro ] "     # order 32
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order28_attempt_ledger_is_off_when_app_is_imported():
    assert app.ATTEMPTS_LEDGER is None


# ------------------------------------------------------------------ order 31: the nightly restart

def test_order31_odins_shell_cannot_open_the_restart_script_for_writing(real_wall):
    """Inside the real wall, an append-open of nightly_restart.sh fails (nothing is written even
    if it opened). Guarded by the mount check, so a broken wall never touches the real script."""
    p = HELIX / "nightly_restart.sh"
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order31_the_wall_binds_the_restart_script_and_the_lkg_read_only():
    text = app._wall_script(shutil.which("bwrap"))
    for p in (HELIX / "nightly_restart.sh", HELIX / ".lkg"):
        rp = os.path.realpath(p)
        assert f"--ro-bind-try {rp} {rp}" in text


def test_order31_the_restart_script_and_lkg_are_guarded_by_both():
    walled = [os.path.realpath(p) for p in REAL_WALL_PATHS]
    for p in (HELIX / "nightly_restart.sh", HELIX / ".lkg"):
        assert os.path.realpath(p) in walled
    assert factgate_hook._build_path_owns(str(HELIX / "nightly_restart.sh"))
    assert factgate_hook._build_path_owns(str(HELIX / ".lkg" / "app.py"))


# ------------------------------------------------------------------ order 32: no third round

def test_order32_every_mount_check_reads_the_top_most_layer():
    """Order 29 fixed two tests that read the mount listing's first line; order 32 fixed two more.
    Inside Odin's walled shell it lists one line per stacked layer, so every mount check in this file
    must go through _TOP_MOUNT. A raw call anywhere else fails here, not later inside the wall."""
    tool = "find" + "mnt"
    raw = [l.strip() for l in Path(__file__).read_text().splitlines()
           if tool in l and not l.lstrip().startswith("#")]
    assert raw == ['_TOP_MOUNT = "' + tool + ' -n -o OPTIONS --target {} | tail -n 1 | cut -d, -f1"'], raw


# ------------------------------------------------------------------ order 33b: the loader's pin list

def test_order33b_odins_shell_cannot_open_pinned_txt_for_writing(real_wall):
    """Inside the real wall, an append-open of memory/PINNED.txt fails (nothing is written even if
    it opened). Guarded by the mount check, so a broken wall never touches the real list."""
    p = HELIX / "memory" / "PINNED.txt"
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order33b_pinned_txt_is_walled_and_guarded_by_both():
    p = os.path.realpath(HELIX / "memory" / "PINNED.txt")
    assert p in [os.path.realpath(w) for w in REAL_WALL_PATHS]
    assert f"--ro-bind-try {p} {p}" in app._wall_script(shutil.which("bwrap"))
    assert factgate_hook._build_path_owns(p)


# ------------------------------------------------------------------ order 34: the memory patch ledger

def test_order34_odins_shell_cannot_open_the_patch_ledger_for_writing(real_wall):
    """Inside the real wall, an append-open of workspace/cycle/PATCHES.jsonl fails. Guarded by the
    mount check on its directory, so a broken wall can never create the live file."""
    p = HELIX / "workspace" / "cycle" / "PATCHES.jsonl"
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p.parent)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order34_patch_ledger_is_off_when_app_is_imported_and_walled_and_guarded():
    assert app.PATCHES_LEDGER is None
    p = os.path.realpath(HELIX / "workspace" / "cycle" / "PATCHES.jsonl")
    assert p in [os.path.realpath(w) for w in REAL_WALL_PATHS]
    assert f"--ro-bind-try {p} {p}" in app._wall_script(shutil.which("bwrap"))
    assert factgate_hook._build_path_owns(p)


# ------------------------------------------------------------------ order 35b: memory/archive/

def test_order35b_the_archive_folder_exists_so_the_wall_binds_it():
    assert (HELIX / "memory" / "archive").is_dir()


def test_order35b_odins_shell_cannot_write_into_the_archive(real_wall):
    """Inside the real wall, creating a file in memory/archive/ fails. Guarded by the mount
    check, so a broken wall never writes into the real archive."""
    d = HELIX / "memory" / "archive"
    probe = ("import os, sys\ntry:\n    os.open(os.path.join(sys.argv[1], '.wallprobe'), "
             "os.O_WRONLY | os.O_CREAT | os.O_EXCL)\n    print('OPENED')\nexcept OSError as e:\n"
             "    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(d)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {d}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order35b_the_archive_is_walled_and_guarded_by_both():
    p = os.path.realpath(HELIX / "memory" / "archive")
    assert p in [os.path.realpath(w) for w in REAL_WALL_PATHS]
    assert f"--ro-bind-try {p} {p}" in app._wall_script(shutil.which("bwrap"))
    assert factgate_hook._build_path_owns(p) and factgate_hook._build_path_owns(p + "/x.md")


# ------------------------------------------------------------------ order 36: the gap audit's opt-out

def test_order36_odins_shell_cannot_append_to_the_gap_opt_out(real_wall):
    """Inside the real wall, an append-open of workspace/cycle/NO_RECORD_NEEDED.jsonl fails, so Odin
    cannot wave his own gaps away. Guarded by the mount check on its directory, so a broken wall can
    never create the live file. No new walled path: workspace/cycle is walled since order 27."""
    p = HELIX / "workspace" / "cycle" / "NO_RECORD_NEEDED.jsonl"
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p.parent)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order36_the_gap_opt_out_is_walled_and_guarded_by_both():
    p = os.path.realpath(HELIX / "workspace" / "cycle" / "NO_RECORD_NEEDED.jsonl")
    cycle = os.path.realpath(HELIX / "workspace" / "cycle")
    assert cycle in [os.path.realpath(w) for w in REAL_WALL_PATHS]
    assert p not in [os.path.realpath(w) for w in REAL_WALL_PATHS], "no new walled path (app.py fenced)"
    assert f"--ro-bind-try {cycle} {cycle}" in app._wall_script(shutil.which("bwrap"))
    assert factgate_hook._build_path_owns(p)


# ------------------------------------------------------------------ order 38: housekeeping's flag and MOVES ledger

@pytest.mark.parametrize("name", ["housekeeping_enabled.flag", "MOVES.jsonl"])
def test_order38_odins_shell_cannot_create_the_flag_or_append_to_moves(real_wall, name):
    """Inside the real wall, creating the flag or appending to MOVES.jsonl fails: the user creates the
    flag, housekeeping (on the unwalled SCHED lane) writes the ledger. Guarded by the mount check on
    the directory, so a broken wall can never create the live file."""
    p = HELIX / "workspace" / "cycle" / name
    probe = ("import os, sys\ntry:\n    os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
             "    print('OPENED')\nexcept OSError as e:\n    print('ERRNO', e.errno, e.strerror)")
    r = run(real_wall, f"[ \"$({_TOP_MOUNT.format(p.parent)})\" = ro ] "
                       f"|| {{ echo NOT-RO; exit 0; }}; python3 -c \"{probe}\" {p}")
    assert r.stdout.strip() == "ERRNO 30 Read-only file system", r.stdout + r.stderr


def test_order38_the_flag_and_moves_are_walled_and_guarded_by_both():
    cycle = os.path.realpath(HELIX / "workspace" / "cycle")
    assert cycle in [os.path.realpath(w) for w in REAL_WALL_PATHS]
    assert f"--ro-bind-try {cycle} {cycle}" in app._wall_script(shutil.which("bwrap"))
    for name in ("housekeeping_enabled.flag", "MOVES.jsonl"):
        p = os.path.realpath(HELIX / "workspace" / "cycle" / name)
        assert p not in [os.path.realpath(w) for w in REAL_WALL_PATHS], "no new walled path (app.py fenced)"
        assert factgate_hook._build_path_owns(p), name
