"""ORDER 38 (item 22): housekeeping — check, plan (dry run), and an apply that refuses for a week.

Every test runs on a temp tree through `hk.Conf(...)`. None reads or writes the live plans dir,
workspace/cycle, /tmp's real entries or the HDD quarantine. One test imports app.py, in a
subprocess, to prove the idle check gives app's answer on the same fixtures (pre-flight 2).
"""
import gzip
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIVE = Path.home() / "helix"
sys.path.insert(0, str(HERE))
import housekeeping as hk  # noqa: E402

DAY = 86400
TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 4, 30).timestamp()


def touch(p, age_days=0, size=0, now=NOW):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        if size:
            fh.truncate(size)                    # sparse: apparent size without the disk
    t = now - age_days * DAY
    os.utime(p, (t, t))
    return p


def age(p, days, now=NOW):
    t = now - days * DAY
    os.utime(p, (t, t), follow_symlinks=False)


def make_conf(tmp_path, **kw):
    """A whole fake machine under tmp_path: helix, /tmp, the odin drive, the HDD quarantine."""
    helix = tmp_path / "helix"
    for d in ("memory/facts", "workspace/crosscheck", "workspace/box2_recovered", "workspace/cycle",
              "workspace/housekeeping/plans", "build_orders/runs", "vault", ".lkg", "static"):
        (helix / d).mkdir(parents=True, exist_ok=True)
    bulk = tmp_path / "hdd" / "helix_bulk"
    (bulk / "static" / "render").mkdir(parents=True)
    (helix / "static" / "render").symlink_to(bulk / "static" / "render")   # as live: on the HDD
    (tmp_path / "tmp").mkdir()
    (tmp_path / "odin").mkdir()
    args = dict(helix=helix, tmp=tmp_path / "tmp", odin_drive=tmp_path / "odin",
                quarantine=bulk / "quarantine", hdd=tmp_path / "hdd",
                plans=helix / "workspace" / "housekeeping" / "plans", uid=os.getuid())
    args.update(kw)
    return hk.Conf(**args)


def paths_of(plan, kind=None):
    return {i["path"] for i in plan["items"] if kind is None or i["kind"] == kind}


# ============================================================================================
# Rule 2: never touch — by resolved real path, so a symlink cannot smuggle one in
# ============================================================================================

NEVER = ["memory/facts/01_x.md", "memory/sessions/log.md", "workspace/crosscheck/seal.json",
         "workspace/box2_recovered/a.py", "vault/v.log", ".lkg/app.py", "build_orders/ORDER_1.md",
         "build_orders/runs/RUNS.jsonl"]


@pytest.mark.parametrize("rel", NEVER)
def test_never_touch_paths_are_refused(tmp_path, rel):
    c = make_conf(tmp_path)
    p = touch(c.helix / rel)
    assert hk.refuse_reason(p, c), rel


@pytest.mark.parametrize("rel", NEVER)
def test_never_touch_paths_are_refused_through_a_symlink(tmp_path, rel):
    c = make_conf(tmp_path)
    p = touch(c.helix / rel)
    link = c.tmp / "innocent_name"
    link.symlink_to(p)
    assert hk.refuse_reason(link, c), rel
    # and through a symlinked PARENT directory
    dlink = c.tmp / "innocent_dir"
    dlink.symlink_to(p.parent)
    assert hk.refuse_reason(dlink / p.name, c), rel


def test_the_odin_drive_is_refused_directly_and_via_symlink(tmp_path):
    c = make_conf(tmp_path)
    p = touch(c.odin_drive / "helix-vault" / "vault.log")
    (c.tmp / "x").symlink_to(c.odin_drive)
    assert hk.refuse_reason(p, c) and hk.refuse_reason(c.tmp / "x", c)


@pytest.mark.parametrize("name", ["credentials.json", "my_CREDENTIAL_store", "gcp-credentials",
                                  ".env", ".env.local", ".envrc"])
def test_secret_and_env_names_are_refused(tmp_path, name):
    c = make_conf(tmp_path)
    p = touch(c.tmp / name)
    assert hk.refuse_reason(p, c)
    q = touch(c.tmp / "dir" / name)              # a directory that holds one is refused too
    assert hk.refuse_reason(q.parent, c)
    (c.tmp / "link").symlink_to(p)               # a harmless link name pointing at one
    assert hk.refuse_reason(c.tmp / "link", c)


def test_any_git_dir_is_refused_and_so_is_a_dir_holding_one(tmp_path):
    c = make_conf(tmp_path)
    g = touch(c.tmp / "repo" / ".git" / "HEAD")
    assert hk.refuse_reason(g, c)
    assert hk.refuse_reason(g.parent, c)
    assert hk.refuse_reason(c.tmp / "repo", c)


def test_a_dir_that_contains_a_never_touch_root_is_refused(tmp_path):
    c = make_conf(tmp_path)
    assert hk.refuse_reason(c.helix / "workspace", c)       # holds crosscheck and box2_recovered
    assert hk.refuse_reason(c.helix, c)


def test_a_dir_holding_a_symlink_into_a_never_touch_root_is_refused(tmp_path):
    c = make_conf(tmp_path)
    d = c.tmp / "agent_out"
    d.mkdir()
    (d / "facts").symlink_to(c.helix / "memory" / "facts")
    assert hk.refuse_reason(d, c)


def test_the_quarantine_itself_is_never_a_source(tmp_path):
    c = make_conf(tmp_path)
    p = touch(c.quarantine / "2026-01-01" / "tmp" / "x")
    assert hk.refuse_reason(p, c)


def test_ordinary_paths_are_allowed(tmp_path):
    c = make_conf(tmp_path)
    assert hk.refuse_reason(touch(c.tmp / "agent-output.txt"), c) is None
    assert hk.refuse_reason(touch(c.helix / "workspace" / "seam" / "__pycache__" / "a.pyc"), c) is None
    assert hk.refuse_reason(touch(c.helix / "static" / "render" / "img.png"), c) is None


def test_the_plan_never_lists_a_never_touch_path(tmp_path):
    """__pycache__ dirs and big logs inside every fenced place: none reach the plan."""
    c = make_conf(tmp_path)
    for d in ("memory", "memory/facts", "workspace/crosscheck", "workspace/box2_recovered", "vault",
              ".lkg", "build_orders", "workspace/seam/.git", "workspace/credentials_store"):
        touch(c.helix / d / "__pycache__" / "x.pyc")
        touch(c.helix / d / "big.log", size=60 * 2**20)
    ok = touch(c.helix / "workspace" / "seam" / "__pycache__" / "x.pyc")
    (c.helix / "workspace" / "smuggle").symlink_to(c.helix / "memory")
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan, "pycache") == {str(ok.parent)}
    assert paths_of(plan, "log") == set()
    for i in plan["items"]:
        assert hk.refuse_reason(i["path"], c) is None


def test_the_plan_lists_refusals_it_found_with_their_reason(tmp_path):
    c = make_conf(tmp_path)
    touch(c.tmp / ".env.backup", age_days=30)
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan) == set()
    assert [r["path"] for r in plan["refused"]] == [str(c.tmp / ".env.backup")]
    assert ".env" in plan["refused"][0]["reason"]


def test_venv_pycache_is_left_alone(tmp_path):
    c = make_conf(tmp_path)
    touch(c.helix / ".venv" / "lib" / "site" / "__pycache__" / "x.pyc")
    touch(c.helix / "tools" / "env2" / "pyvenv.cfg")
    touch(c.helix / "tools" / "env2" / "lib" / "__pycache__" / "y.pyc")
    assert paths_of(hk.make_plan(c, now=NOW), "pycache") == set()


# ============================================================================================
# /tmp: older than 7 days, owned by the user
# ============================================================================================

def test_tmp_age_threshold(tmp_path):
    c = make_conf(tmp_path)
    old = touch(c.tmp / "old.txt", age_days=8)
    touch(c.tmp / "young.txt", age_days=6)
    edge = touch(c.tmp / "edge.txt", age_days=7)          # exactly 7 days is not OLDER than 7
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan, "tmp") == {str(old)}
    assert str(edge) not in paths_of(plan)


def test_a_tmp_dir_is_as_young_as_its_newest_entry(tmp_path):
    c = make_conf(tmp_path)
    d = c.tmp / "task-out"
    touch(d / "a", age_days=30)
    touch(d / "sub" / "b", age_days=2)                     # still being written
    age(d / "sub", 30)
    age(d, 30)
    stale = c.tmp / "stale"
    touch(stale / "a", age_days=20, size=1000)
    touch(stale / "b", age_days=10, size=24)
    age(stale, 20)
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan, "tmp") == {str(stale)}
    item = next(i for i in plan["items"] if i["path"] == str(stale))
    assert item["size"] == 1024 and item["files"] == 2


def test_tmp_entries_of_another_owner_are_left_alone(tmp_path):
    c = make_conf(tmp_path, uid=os.getuid() + 12345)
    touch(c.tmp / "old.txt", age_days=30)
    assert paths_of(hk.make_plan(c, now=NOW), "tmp") == set()


def test_a_tmp_symlink_is_judged_by_its_target(tmp_path):
    c = make_conf(tmp_path)
    (c.tmp / "facts_link").symlink_to(c.helix / "memory" / "facts")
    age(c.tmp / "facts_link", 30)
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan) == set()
    assert str(c.tmp / "facts_link") in {r["path"] for r in plan["refused"]}


# ============================================================================================
# static/render: keep the newest 200 OR the last 60 days, whichever keeps more
# ============================================================================================

def render(c, ages):
    r = c.helix / "static" / "render"
    return [touch(r / f"img_{n:04d}.png", age_days=a, size=10) for n, a in enumerate(ages)]


def test_render_keeps_the_newest_200_when_that_keeps_more(tmp_path):
    c = make_conf(tmp_path)
    files = render(c, [100 + n * 0.01 for n in range(250)])     # all older than 60 days
    plan = hk.make_plan(c, now=NOW)
    moved = paths_of(plan, "render")
    assert len(moved) == 50
    assert moved == {str(os.path.realpath(f)) for f in files[200:]}     # the 50 oldest


def test_render_keeps_60_days_when_that_keeps_more(tmp_path):
    c = make_conf(tmp_path)
    render(c, [n * 0.2 for n in range(260)] + [90] * 40)       # 260 young, 40 old
    moved = paths_of(hk.make_plan(c, now=NOW), "render")
    assert len(moved) == 40


def test_render_keeps_everything_when_both_limits_cover_it(tmp_path):
    c = make_conf(tmp_path)
    render(c, [1] * 150 + [400] * 20)                          # 170 files: under 200
    assert paths_of(hk.make_plan(c, now=NOW), "render") == set()
    c2 = make_conf(tmp_path / "b")
    render(c2, [n * 0.1 for n in range(300)])                  # 300 files, all within 30 days
    assert paths_of(hk.make_plan(c2, now=NOW), "render") == set()


def test_render_retention_is_the_union_of_the_two_limits(tmp_path):
    c = make_conf(tmp_path)
    files = render(c, [70 + n for n in range(230)] + [5] * 10)  # 230 old, 10 young
    moved = paths_of(hk.make_plan(c, now=NOW), "render")
    # the 10 young + the 200 newest overall (10 young + 190 newest old) → keep 200; 40 old move
    assert len(moved) == 40
    assert moved == {str(os.path.realpath(f)) for f in files[190:230]}


# ============================================================================================
# logs over 50 MB → compress
# ============================================================================================

def test_logs_over_50_mb_are_planned_for_compression(tmp_path):
    c = make_conf(tmp_path)
    big = touch(c.helix / "odin.log", size=51 * 2**20)
    touch(c.helix / "small.log", size=49 * 2**20)
    rot = touch(c.helix / "render_daemon.log.1", size=60 * 2**20)
    touch(c.helix / "old.log.gz", size=60 * 2**20)             # already compressed
    plan = hk.make_plan(c, now=NOW)
    assert paths_of(plan, "log") == {str(big), str(rot)}
    assert {i["action"] for i in plan["items"] if i["kind"] == "log"} == {"compress"}


# ============================================================================================
# the plan file; nothing moves
# ============================================================================================

def snapshot(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_plan_writes_its_dated_file_and_moves_nothing(tmp_path):
    c = make_conf(tmp_path)
    touch(c.tmp / "old.txt", age_days=30, size=5)
    touch(c.helix / "workspace" / "x" / "__pycache__" / "a.pyc", size=7)
    render(c, [300] * 205)
    before = snapshot(tmp_path)
    plan = hk.write_plan(c, today=TODAY, now=NOW)
    f = c.plans / "2026-10-02.json"
    assert json.loads(f.read_text()) == plan
    assert plan["dry_run"] is True and plan["totals"]["n"] == len(plan["items"]) == 7
    after = snapshot(tmp_path)
    keep = lambda snap: {k: v for k, v in snap.items() if not k.startswith(str(c.plans))}  # noqa: E731
    assert keep(after) == keep(before)
    assert not c.quarantine.exists()
    assert not c.moves.exists()


def test_plan_items_carry_where_they_would_go(tmp_path):
    c = make_conf(tmp_path)
    p = touch(c.tmp / "old.txt", age_days=30, size=5)
    plan = hk.make_plan(c, now=NOW, today=TODAY)
    item = plan["items"][0]
    assert item["dest"] == str(c.quarantine / "2026-10-02" / str(p).lstrip("/"))
    assert item["reason"] == "tmp older than 7 days"


# ============================================================================================
# apply: refuses unless flag + 7 dated plans + flag newer than the 7th plan + idle
# ============================================================================================

def seven_plans(c, n=7, start=date(2026, 9, 1)):
    for k in range(n):
        d = start + timedelta(days=k)
        f = c.plans / f"{d.isoformat()}.json"
        f.write_text("{}")
        t = datetime(d.year, d.month, d.day, 4, 30).timestamp()
        os.utime(f, (t, t))


def flag(c, when):
    c.flag.write_text("")
    os.utime(c.flag, (when, when))


IDLE = lambda c: []                                             # noqa: E731


def test_apply_refuses_with_no_flag(tmp_path):
    c = make_conf(tmp_path)
    seven_plans(c)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert not ok and "housekeeping_enabled.flag" in why


def test_apply_refuses_with_six_plans(tmp_path):
    c = make_conf(tmp_path)
    seven_plans(c, n=6)
    flag(c, NOW)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert not ok and "6 dated plan" in why


def test_undated_plan_files_do_not_count(tmp_path):
    c = make_conf(tmp_path)
    seven_plans(c, n=6)
    (c.plans / "notes.json").write_text("{}")
    (c.plans / "2026-13-45.json").write_text("{}")
    flag(c, NOW)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert not ok and "6 dated plan" in why


@pytest.mark.parametrize("offset", [-3600, 0])
def test_apply_refuses_when_the_flag_is_not_newer_than_the_7th_plan(tmp_path, offset):
    c = make_conf(tmp_path)
    seven_plans(c)
    seventh = (c.plans / "2026-09-07.json").stat().st_mtime
    flag(c, seventh + offset)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert not ok and "7th plan" in why


def test_apply_refuses_while_a_job_runs(tmp_path):
    c = make_conf(tmp_path)
    seven_plans(c)
    flag(c, NOW)
    ok, why = hk.apply(c, busy_fn=lambda c: ["builder pid 4242"], today=TODAY, now=NOW)
    assert not ok and "builder pid 4242" in why


def test_apply_refuses_a_quarantine_on_the_root_disk(tmp_path):
    c = make_conf(tmp_path)                       # tmp_path is on / here: hdd is no mount
    seven_plans(c)
    flag(c, NOW)
    touch(c.tmp / "old.txt", age_days=30)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert not ok and "HDD" in why
    assert (c.tmp / "old.txt").exists() and not c.moves.exists()


def test_every_refusal_moves_nothing(tmp_path):
    c = make_conf(tmp_path)
    touch(c.tmp / "old.txt", age_days=30)
    before = snapshot(tmp_path)
    for setup in (lambda: None, lambda: seven_plans(c), lambda: flag(c, 0)):
        setup()
        ok, _ = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
        assert not ok
    after = snapshot(tmp_path)
    keep = lambda snap: {k: v for k, v in snap.items()  # noqa: E731
                         if not k.startswith((str(c.plans), str(c.flag))) and k != str(c.flag.parent)}
    assert keep(after) == keep(before)                     # (the flag's own dir mtime moves: the test made it)


def enabled(c, monkeypatch):
    seven_plans(c)
    flag(c, NOW)
    monkeypatch.setattr(hk, "_on_the_hdd", lambda conf: (True, ""))


def test_apply_moves_into_the_dated_quarantine_and_logs_each_move(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    f = touch(c.tmp / "old.txt", age_days=30)
    f.write_bytes(b"hello")
    age(f, 30)
    d = c.tmp / "olddir"
    touch(d / "a", age_days=30)
    age(d, 30)
    ok, why = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert ok, why
    q = c.quarantine / "2026-10-02"
    assert not f.exists() and (q / str(f).lstrip("/")).read_bytes() == b"hello"
    assert not d.exists() and (q / str(d).lstrip("/") / "a").exists()
    lines = [json.loads(l) for l in c.moves.read_text().splitlines()]
    assert len(lines) == 2


def test_moves_line_shape(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    f = c.tmp / "old.txt"
    f.write_bytes(b"hello")
    age(f, 30)
    ok, _ = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert ok
    (line,) = c.moves.read_text().splitlines()
    rec = json.loads(line)
    assert set(rec) == {"ts", "action", "kind", "path", "dest", "size", "sha256", "reason", "ok"}
    assert rec["path"] == str(f) and rec["size"] == 5 and rec["ok"] is True
    assert rec["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert rec["reason"] == "tmp older than 7 days" and rec["action"] == "move"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["ts"])


def test_moves_has_no_sha_over_100_mb_and_for_dirs(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    monkeypatch.setattr(hk, "SHA_MAX", 4)
    f = c.tmp / "old.txt"
    f.write_bytes(b"hello")
    age(f, 30)
    d = c.tmp / "olddir"
    touch(d / "a", age_days=30)
    age(d, 30)
    assert hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)[0]
    recs = {json.loads(l)["path"]: json.loads(l) for l in c.moves.read_text().splitlines()}
    assert recs[str(f)]["sha256"] is None
    assert recs[str(d)]["sha256"] is None


def test_the_moves_ledger_and_the_flag_live_in_the_walled_cycle_dir():
    c = hk.Conf()
    cycle = LIVE / "workspace" / "cycle"
    assert c.moves == cycle / "MOVES.jsonl" and c.flag == cycle / "housekeeping_enabled.flag"
    assert c.quarantine == Path("__BULK__/quarantine")


def test_apply_rechecks_never_touch_at_move_time(tmp_path, monkeypatch):
    """A plan item that turned into a symlink into memory/ after planning is not moved."""
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    real_plan = hk.make_plan

    def sneaky(conf, **kw):
        plan = real_plan(conf, **kw)
        for i in plan["items"]:
            p = Path(i["path"])
            p.unlink()
            p.symlink_to(c.helix / "memory" / "facts")
        return plan
    touch(c.tmp / "old.txt", age_days=30)
    monkeypatch.setattr(hk, "make_plan", sneaky)
    ok, _ = hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)
    assert (c.helix / "memory" / "facts").is_dir()
    assert not (c.quarantine / "2026-10-02").exists() or not any((c.quarantine / "2026-10-02").rglob("*"))
    rec = json.loads(c.moves.read_text().splitlines()[0])
    assert rec["ok"] is False and "never touch" in rec["reason"]


def test_apply_compresses_a_big_log_into_the_quarantine(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    monkeypatch.setattr(hk, "LOG_MAX", 10)
    log = c.helix / "odin.log"
    log.write_bytes(b"line\n" * 10)
    assert hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)[0]
    gz = c.quarantine / "2026-10-02" / (str(log).lstrip("/") + ".gz")
    assert gzip.decompress(gz.read_bytes()) == b"line\n" * 10
    assert log.exists() and log.stat().st_size == 0           # copy-truncate: the writer's inode stays
    rec = json.loads(c.moves.read_text().splitlines()[0])
    assert rec["action"] == "compress" and rec["size"] == 50 and rec["ok"] is True


def test_quarantine_is_purged_only_after_30_days(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    enabled(c, monkeypatch)
    old = touch(c.quarantine / "2026-09-01" / "tmp" / "a")    # 31 days before 2026-10-02
    edge = touch(c.quarantine / "2026-09-02" / "tmp" / "b")   # 30 days: not yet
    odd = touch(c.quarantine / "not-a-date" / "c")
    assert hk.apply(c, busy_fn=IDLE, today=TODAY, now=NOW)[0]
    assert not old.parent.parent.exists()
    assert edge.exists() and odd.exists()
    recs = [json.loads(l) for l in c.moves.read_text().splitlines()]
    assert [(r["action"], r["path"]) for r in recs] == [("purge", str(old.parent.parent))]


def test_no_rm_rf_anywhere_in_the_module():
    src = (HERE / "housekeeping.py").read_text().replace(hk.__doc__, "")   # the rules quote "rm -rf"
    for banned in ("rmtree", "rm -rf", "rm -r", "os.system", "shell=True"):
        assert banned not in src, banned


def test_no_way_round_the_refusal():
    src = (HERE / "housekeeping.py").read_text()
    assert not re.search(r"force|override|bypass|skip_check|environ", src, re.I)


# ============================================================================================
# Rule 4: the idle check — app's out-of-process signals, re-implemented, compared on fixtures
# ============================================================================================

def test_the_module_does_not_import_app():
    src = (HERE / "housekeeping.py").read_text()
    assert not re.search(r"^\s*(import app\b|from app\b)|__import__|importlib", src, re.M)


IDLE_PROG = r'''
import json, os, sys, subprocess, time, threading
sys.path.insert(0, os.getcwd()); import app
sys.path.insert(0, sys.argv[1]); import housekeeping as hk
base = sys.argv[2]
live = subprocess.Popen(["sleep", "120"]); dead = subprocess.Popen(["true"]); dead.wait()
now_z = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
gpu = {"pids": [], "fail": False}
def gpu_pids():
    if gpu["fail"]: raise RuntimeError("no nvidia-smi")
    return list(gpu["pids"])
def argv(pid):
    if pid == 111: return ["python3", "render_daemon.py"], base
    if pid == 444: raise PermissionError(13, "Permission denied", f"/proc/{pid}/cwd")
    if pid == 222: return ["python3", "train.py"], base
    raise OSError("gone")
app._nr_gpu_pids = gpu_pids; app._nr_argv = argv
resident = (os.path.realpath(os.path.join(base, "render_daemon.py")),)
app.NIGHTLY_RESIDENT_GPU = resident
cases = {
 "idle": {},
 "builder live": {"lock": live.pid},
 "builder dead": {"lock": dead.pid},
 "builder junk": {"lock": "x"},
 "queue running live": {"queue": {"status": "running", "pid": live.pid}},
 "queue running dead": {"queue": {"status": "running", "pid": dead.pid}},
 "queue starting young": {"queue": {"status": "starting", "started": time.strftime("%Y-%m-%dT%H:%M:%S")}},
 "queue starting old": {"queue": {"status": "starting", "started": "2026-01-01T00:00:00"}},
 "queue done": {"queue": {"status": "done", "pid": live.pid}},
 "attempt live": {"attempts": [{"pid": live.pid, "ts": now_z, "scope": "s"}]},
 "attempt reused pid": {"attempts": [{"pid": live.pid, "ts": "2026-01-01T00:00:00Z", "scope": "s"}]},
 "attempt dead": {"attempts": [{"pid": dead.pid, "ts": now_z, "scope": "s"}]},
 "attempt bad ts": {"attempts": [{"pid": live.pid, "ts": "garbage", "scope": "s"}]},
 "gpu resident only": {"gpu": [111]},
 "gpu other": {"gpu": [111, 222, 333]},
 "gpu fails": {"gpufail": True},
 "gpu cwd unreadable": {"gpu": [111, 444]},
 "everything": {"lock": live.pid, "queue": {"status": "waiting", "pid": live.pid},
                "attempts": [{"pid": live.pid, "ts": now_z, "scope": "s"}], "gpu": [222]},
}
out = {}
for name, cs in cases.items():
    d = os.path.join(base, name.replace(" ", "_")); os.makedirs(d)
    if "lock" in cs: open(os.path.join(d, ".lock"), "w").write(str(cs["lock"]))
    open(os.path.join(d, "RUNS.jsonl"), "w").write(json.dumps({"unit": "u1"}) + "\n")
    if "queue" in cs: json.dump(cs["queue"], open(os.path.join(d, "QUEUE.json"), "w"))
    att = os.path.join(d, "ATTEMPTS.jsonl")
    open(att, "w").write("".join(json.dumps(a) + "\n" for a in cs.get("attempts", [])) + "not json\n")
    gpu["pids"] = cs.get("gpu", []); gpu["fail"] = cs.get("gpufail", False)
    app.BUILD_RUNS = d; app.NIGHTLY_ATTEMPTS = att
    a = app._nightly_restart_busy()
    c = hk.Conf(build_runs=d, attempts=att, resident_gpu=resident)
    h = hk.busy(c, gpu_pids=gpu_pids, argv=argv)
    out[name] = [a, h]
live.kill()
print(json.dumps(out))
'''


def test_the_idle_check_gives_apps_answer_on_the_same_fixtures(tmp_path):
    env = dict(os.environ, HELIX_NO_WARMUP="1", HELIX_NO_PRECONNECT="1", HELIX_NO_SCHEDULER="1",
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([str(LIVE / ".venv/bin/python"), "-c", IDLE_PROG, str(HERE), str(tmp_path)],
                       cwd=LIVE, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(out) == 18
    for name, (app_says, hk_says) in out.items():
        assert app_says == hk_says, f"{name}: app {app_says} vs housekeeping {hk_says}"
    # and the fixtures really exercised both answers
    assert out["idle"][0] == [] and out["gpu resident only"][0] == []
    assert out["everything"][0] == [f"builder pid {out['everything'][0][0].split()[-1]}",
                                    "build queue", out["everything"][0][2], "GPU pid 222"]
    assert out["gpu fails"][0] == ["GPU unknown (nvidia-smi failed: RuntimeError)"]
    # order 40b: a GPU process whose cwd is unreadable is not proven resident: both say busy
    assert out["gpu cwd unreadable"] == [["GPU pid 444"], ["GPU pid 444"]]


def test_the_idle_check_reads_the_same_files_app_does():
    c = hk.Conf()
    assert c.build_runs == LIVE / "build_orders" / "runs"
    assert c.attempts == LIVE / "workspace" / "cycle" / "ATTEMPTS.jsonl"
    assert c.resident_gpu == (os.path.realpath(LIVE / "render_daemon.py"),)


# ============================================================================================
# check, and the nightly's one line
# ============================================================================================

def fake_check(**kw):
    base = dict(root_pct=44.0, hdd_pct=12.0, hdd_mounted=True, mem_total=31.0, mem_free=2.0,
                mem_avail=11.0, cpu_temp=45.0, cpu_src="coretemp", gpu_temp=33, gpu_mem=845,
                gpu_mem_total=24576, gpu_err=None, odin_mounted=True, helix_ok=True,
                helix_why="200", render_ok=True, render_why="200", restart_pending=[])
    base.update(kw)
    return base


def test_check_lines_one_each(tmp_path):
    lines = hk.check_lines(fake_check())
    keys = [l.split(":")[0] for l in lines]
    assert keys == ["/ disk", "__BULK__ disk", "RAM", "CPU temp", "GPU", "odin drive", "Helix /stat",
                    "render daemon", "needs restart"]


@pytest.mark.parametrize("kw,anomaly", [
    (dict(root_pct=86.0), "/ disk 86%"), (dict(hdd_pct=90.0), "__BULK__ disk 90%"),
    (dict(hdd_mounted=False, hdd_pct=None), "__BULK__ not mounted"),
    (dict(odin_mounted=False), "odin drive not mounted"),
    (dict(render_ok=False, render_why="connection refused"), "render daemon down")])
def test_anomalies_are_reported(kw, anomaly):
    assert anomaly in hk.anomalies(fake_check(**kw))


def test_no_anomaly_at_85_percent():
    assert hk.anomalies(fake_check(root_pct=85.0, hdd_pct=85.0)) == []


def test_the_house_line(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    seven_plans(c, n=2)
    touch(c.tmp / "old.txt", age_days=30, size=3 * 2**30)
    monkeypatch.setattr(hk, "measure", lambda conf: fake_check())
    line = hk.house_line(c, today=TODAY, now=NOW)
    assert line.startswith("- house: / 44% · hdd 12% · RAM 11.0G avail of 31G · CPU 45°C · "
                           "GPU 33°C 845MiB · odin drive mounted · "), line
    assert line.endswith("plan: would move 1 items, 3.22 GB (dry run, day 3/7)"), line
    assert (c.plans / "2026-10-02.json").exists()


def test_the_house_line_reports_anomalies_never_fixes(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    monkeypatch.setattr(hk, "measure", lambda conf: fake_check(odin_mounted=False, render_ok=False,
                                                                 render_why="refused"))
    line = hk.house_line(c, today=TODAY, now=NOW)
    assert "🔴 odin drive not mounted" in line and "🔴 render daemon down" in line
    assert "(dry run, day 1/7)" in line


def test_the_house_line_never_raises(tmp_path, monkeypatch):
    c = make_conf(tmp_path)

    def boom(conf):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(hk, "measure", boom)
    assert hk.house_line(c, today=TODAY, now=NOW) == "- house: 🔴 did not run (RuntimeError: kaboom)"


def test_needs_restart_counts_ready_reports_newer_than_the_running_helix(tmp_path):
    c = make_conf(tmp_path)
    rep = c.helix / "build_orders" / "reports"
    touch(rep / "ORDER_1_report.md").write_text("x\n## CLAIMS\nREADY FOR /restart\n")
    touch(rep / "ORDER_2_report.md").write_text("x\nDONE\n")
    old = touch(rep / "ORDER_0_report.md")
    old.write_text("x\nREADY FOR /restart\n")
    age(old, 5)
    got = hk.restart_pending(c, started=NOW - DAY)
    assert got == ["ORDER_1_report.md"]
    assert hk.restart_pending(c, started=None) == ["helix not running"]


# ============================================================================================
# ORDER 40b Part C: inside Odin's wall /proc/<pid>/cwd is refused; that is "unknown", never "not running"
# ============================================================================================

def fake_proc(tmp_path, procs):
    """procs: {pid: (argv, cwd or PermissionError or None)}. None: the process is gone by the readlink."""
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    for pid, (argv, _cwd) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    return root


@pytest.fixture
def procs(tmp_path, monkeypatch):
    table = {}
    real = os.readlink

    def readlink(path):
        path = str(path)
        if path.startswith(str(tmp_path / "proc")) and path.endswith("/cwd"):
            cwd = table[int(Path(path).parent.name)][1]
            if cwd is None:
                raise FileNotFoundError(2, "No such file or directory", path)
            if cwd is PermissionError:
                raise PermissionError(13, "Permission denied", path)
            return cwd
        return real(path)
    monkeypatch.setattr(hk.os, "readlink", readlink)
    monkeypatch.setattr(hk, "proc_started", lambda pid: 1000.0 + pid)

    def make(**kw):
        table.clear()
        table.update(kw.get("table", {}))
        return fake_proc(tmp_path, table)
    return make


def helix_conf(tmp_path):
    c = make_conf(tmp_path)
    touch(c.helix / "app.py")
    return c


def test_a_readable_relative_app_is_found(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["/x/.venv/bin/python", "app.py"], str(c.helix))})
    assert hk.helix_started(c, proc=root) == (1007.0, 0)


def test_an_absolute_app_needs_no_cwd(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["python3", str(c.helix / "app.py")], PermissionError)})
    assert hk.helix_started(c, proc=root) == (1007.0, 0)


def test_a_relative_app_with_an_unreadable_cwd_is_unknown_and_counted(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["/x/.venv/bin/python", "app.py"], PermissionError),
                        8: (["python3", "other.py"], PermissionError),
                        9: (["bash", "app.py"], PermissionError)})       # not python: not a candidate
    assert hk.helix_started(c, proc=root) == (None, 2)


def test_a_found_helix_wins_over_unreadable_others(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["python3", "app.py"], str(c.helix)), 8: (["python3", "x.py"], PermissionError)})
    assert hk.helix_started(c, proc=root) == (1007.0, 1)


def test_no_python_at_all_is_not_running(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["bash"], PermissionError), 8: (["python3"], PermissionError),
                        9: (["python3", "app.py"], "/elsewhere")})
    assert hk.helix_started(c, proc=root) == (None, 0)


def test_a_process_that_exits_mid_read_is_not_counted(tmp_path, procs):
    c = helix_conf(tmp_path)
    root = procs(table={7: (["python3", "app.py"], None)})
    assert hk.helix_started(c, proc=root) == (None, 0)


def test_unknown_is_never_helix_not_running(tmp_path):
    c = make_conf(tmp_path)
    got = hk.restart_pending(c, started=None, unreadable=3)
    assert got == ["unknown (cannot read /proc from here)", "3 processes unreadable"]
    assert "helix not running" not in got
    assert hk.restart_pending(c, started=None, unreadable=1)[1] == "1 process unreadable"
    assert hk.restart_pending(c, started=None, unreadable=0) == ["helix not running"]


def test_the_check_line_prints_unknown(tmp_path):
    line = hk.check_lines(fake_check(restart_pending=hk.restart_pending(make_conf(tmp_path), None, 2)))[-1]
    assert line == "needs restart: unknown (cannot read /proc from here), 2 processes unreadable"


def test_measure_passes_the_count_through(tmp_path, monkeypatch):
    c = make_conf(tmp_path)
    monkeypatch.setattr(hk, "helix_started", lambda conf: (None, 4))
    monkeypatch.setattr(hk, "_http", lambda url: (True, "200"))
    monkeypatch.setattr(hk, "_gpu", lambda: (None, None, None, "x"))
    m = hk.measure(c)
    assert m["restart_pending"][0] == "unknown (cannot read /proc from here)"


def test_proc_argv_still_raises_on_an_unreadable_cwd_like_app(monkeypatch):
    """GPU attest: app._nr_argv raises when cwd is refused; housekeeping's proc_argv must too."""
    def refuse(path):
        raise PermissionError(13, "Permission denied", path)
    monkeypatch.setattr(hk.os, "readlink", refuse)
    with pytest.raises(PermissionError):
        hk.proc_argv(os.getpid())
