#!/usr/bin/env python3
"""Housekeeping (order 38, item 22): Helix keeps the machine it lives in. Report first, clean later.

    housekeeping.py check            one line each: disks, RAM, temps, GPU, drive, Helix, daemon, restart
    housekeeping.py plan             what it WOULD move, with sizes → plans/<date>.json. Moves nothing.
    housekeeping.py apply            refuses unless the user's flag + 7 dated plans + the flag is newer
                                     than the 7th plan + nothing is running

The five cleaning rules (134_* item 22), each held in code here and tested:
  1. Never rm -rf. Move to __BULK__/quarantine/<date>/ (the HDD); a quarantine day is
     deleted only once it is more than 30 days old.
  2. Never touch memory/**, workspace/crosscheck/**, workspace/box2_recovered/**, the odin drive, the
     vault, *credential*, .env*, .lkg/, build_orders/, any .git dir — judged on the path as given AND
     its resolved real path, so a symlink cannot carry one in. A directory holding one is refused.
  3. Dry run first: a week of dated plans before apply can run.
  4. Never while a job runs: the out-of-process half of app.py's `_nightly_restart_busy` (order 31),
     re-implemented here on the same files and commands (app.py is not imported; a test runs both
     on the same fixtures).
  5. Every move is one line in workspace/cycle/MOVES.jsonl (walled; the flag lives beside it).
Report only, never fix: anomalies are printed, nothing is repaired.
"""
import argparse, datetime as dt, gzip, hashlib, json, os, pwd, re, shutil, stat, subprocess, sys, time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELIX = Path.home() / "helix"
DAY = 86400
TMP_DAYS = 7
RENDER_KEEP_N = 200
RENDER_KEEP_DAYS = 60
LOG_MAX = 50 * 2**20                     # logs over 50 MB are compressed
QUARANTINE_DAYS = 30
PLANS_NEEDED = 7
SHA_MAX = 100 * 2**20                    # MOVES carries a sha256 for files up to 100 MB
DISK_ALARM = 85
OWNER = "__USER__"
HELIX_STAT = "http://127.0.0.1:7777/stat"
RENDER_HEALTH = "http://127.0.0.1:7900/health"
_PLAN_NAME = re.compile(r"^(\d{4}-\d\d-\d\d)\.json$")
_LOG_NAME = re.compile(r"\.log(\.\d+)?$")


def _uid():
    try:
        return pwd.getpwnam(OWNER).pw_uid
    except KeyError:
        return os.getuid()


class Conf:
    """Every place this file reads or writes. Tests build one on a temp tree."""

    def __init__(self, helix=HELIX, tmp="/tmp", odin_drive="__VAULT_DRIVE__",
                 hdd="__BULK__", quarantine="__BULK__/quarantine", plans=None, uid=None,
                 build_runs=None, attempts=None, resident_gpu=None):
        self.helix = Path(helix)
        self.tmp = Path(tmp)
        self.odin_drive = Path(odin_drive)
        self.hdd = Path(hdd)
        self.quarantine = Path(quarantine)
        self.plans = Path(plans) if plans else self.helix / "workspace" / "housekeeping" / "plans"
        self.uid = _uid() if uid is None else uid
        cycle = self.helix / "workspace" / "cycle"          # walled (order 27): Odin's shell can't write
        self.flag = cycle / "housekeeping_enabled.flag"
        self.moves = cycle / "MOVES.jsonl"
        self.render = self.helix / "static" / "render"
        # the idle check reads what app.py's reads (BUILD_RUNS, NIGHTLY_ATTEMPTS, NIGHTLY_RESIDENT_GPU)
        self.build_runs = Path(build_runs) if build_runs else self.helix / "build_orders" / "runs"
        self.attempts = Path(attempts) if attempts else cycle / "ATTEMPTS.jsonl"
        self.resident_gpu = (tuple(resident_gpu) if resident_gpu is not None
                             else (os.path.realpath(self.helix / "render_daemon.py"),))

    def never_roots(self):
        """(real path, label) of every place rule 2 fences off, plus the quarantine itself."""
        h = self.helix
        named = [(h / "memory", "memory/"), (h / "workspace" / "crosscheck", "workspace/crosscheck/"),
                 (h / "workspace" / "box2_recovered", "workspace/box2_recovered/"),
                 (self.odin_drive, "the odin drive"), (h / "vault", "the vault"),
                 (h / ".lkg", ".lkg/"), (h / "build_orders", "build_orders/"),
                 (self.quarantine, "the quarantine")]
        return [(os.path.realpath(p), label) for p, label in named]


# ------------------------------------------------------------------------------ rule 2

def _bad_name(part):
    low = part.lower()
    if part == ".git":
        return "a git dir"
    if part == ".lkg":
        return ".lkg/"
    if "credential" in low:
        return "*credential*"
    if part.startswith(".env"):
        return ".env*"
    return None


def _inside(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def refuse_reason(path, conf):
    """None if rule 2 allows touching `path`; else why not. Checks the path as given and resolved,
    every component of both, any fenced root inside it, and (for a real directory) every entry
    below it, including where its symlinks point."""
    given = os.path.abspath(str(path))
    try:
        real = os.path.realpath(given)
    except (OSError, ValueError) as e:
        return f"never touch: cannot resolve ({type(e).__name__})"
    roots = conf.never_roots()
    for cand in dict.fromkeys((given, real)):
        for part in Path(cand).parts:
            why = _bad_name(part)
            if why:
                return f"never touch: {why} ({cand})"
        for root, label in roots:
            if _inside(cand, root):
                return f"never touch: {label} ({cand})"
            if _inside(root, cand):
                return f"never touch: holds {label} ({cand})"
    if os.path.isdir(given) and not os.path.islink(given):
        for d, dirs, files in os.walk(given, followlinks=False):
            for n in dirs + files:
                p = os.path.join(d, n)
                why = _bad_name(n)
                if why:
                    return f"never touch: holds {why} ({p})"
                if os.path.islink(p):
                    t = os.path.realpath(p)
                    for root, label in roots:
                        if _inside(t, root) or _inside(root, t):
                            return f"never touch: holds a link into {label} ({p})"
                    if any(_bad_name(x) for x in Path(t).parts):
                        return f"never touch: holds a link to {t} ({p})"
    return None


# ------------------------------------------------------------------------------ the plan

def _tree(path):
    """(newest mtime, bytes, files, error) of a path, never following a symlink."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_mtime, st.st_size, 1, None
    newest, size, files, errs = st.st_mtime, 0, 0, []
    for d, dirs, names in os.walk(path, followlinks=False, onerror=errs.append):
        for n in dirs + names:
            try:
                s = os.lstat(os.path.join(d, n))
            except OSError as e:
                errs.append(e)
                continue
            newest = max(newest, s.st_mtime)
            if not stat.S_ISDIR(s.st_mode):
                size += s.st_size
                files += 1
    return newest, size, files, (f"{type(errs[0]).__name__}: {errs[0]}" if errs else None)


def _dest(conf, today, path, action):
    d = conf.quarantine / today.isoformat() / str(path).lstrip("/")
    return str(d) + (".gz" if action == "compress" else "")


def _is_venv(d):
    return os.path.basename(d) == ".venv" or os.path.exists(os.path.join(d, "pyvenv.cfg"))


def make_plan(conf, now=None, today=None):
    """What apply would do today. Reads only."""
    now = time.time() if now is None else now
    today = today or dt.date.fromtimestamp(now)
    items, refused, errors = [], [], []

    def add(kind, path, size, files, reason, action="move"):
        why = refuse_reason(path, conf)
        if why:
            refused.append(dict(kind=kind, path=str(path), reason=why))
            return
        items.append(dict(kind=kind, path=str(path), size=size, files=files, action=action,
                          reason=reason, dest=_dest(conf, today, path, action)))

    # /tmp: entries of ours whose newest part is older than 7 days
    try:
        entries = sorted(os.scandir(conf.tmp), key=lambda e: e.name)
    except OSError as e:
        entries, _ = [], errors.append(f"{conf.tmp}: {e}")
    for e in entries:
        try:
            if os.lstat(e.path).st_uid != conf.uid:
                continue
            newest, size, files, err = _tree(e.path)
        except OSError as x:
            errors.append(f"{e.path}: {x}")
            continue
        if err:
            errors.append(f"{e.path}: partly unreadable, not judged ({err})")
            continue
        if now - newest > TMP_DAYS * DAY:
            add("tmp", e.path, size, files, f"tmp older than {TMP_DAYS} days")

    # ~/helix: __pycache__ dirs and big logs. Fenced places and venvs are not walked into.
    roots = [r for r, _ in conf.never_roots()]
    for d, dirs, files in os.walk(conf.helix, followlinks=False, onerror=lambda x: errors.append(str(x))):
        keep = []
        for n in sorted(dirs):
            p = os.path.join(d, n)
            if n == ".git" or _is_venv(p) or any(_inside(os.path.realpath(p), r) for r in roots):
                continue
            if n == "__pycache__":
                try:
                    _, size, nf, _ = _tree(p)
                except OSError as x:
                    errors.append(f"{p}: {x}")
                    continue
                add("pycache", p, size, nf, "__pycache__ (rebuilt on the next import)")
                continue
            keep.append(n)
        dirs[:] = keep
        for n in sorted(files):
            if not _LOG_NAME.search(n):
                continue
            p = os.path.join(d, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size > LOG_MAX:
                add("log", p, st.st_size, 1, f"log over {LOG_MAX // 2**20} MB", action="compress")

    # static/render (on the HDD): keep the newest 200 and everything from the last 60 days
    rdir = os.path.realpath(conf.render)
    if os.path.isdir(rdir):
        files = []
        for e in os.scandir(rdir):
            st = e.stat(follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                files.append((st.st_mtime, e.name, e.path, st.st_size))
        files.sort(key=lambda f: (-f[0], f[1]))
        for k, (mtime, _, p, size) in enumerate(files):
            if k < RENDER_KEEP_N or now - mtime <= RENDER_KEEP_DAYS * DAY:
                continue
            add("render", p, size, 1, f"render beyond the newest {RENDER_KEEP_N} and older than "
                                      f"{RENDER_KEEP_DAYS} days")

    by_kind = {}
    for i in items:
        n, b = by_kind.get(i["kind"], [0, 0])
        by_kind[i["kind"]] = [n + 1, b + i["size"]]
    return dict(date=today.isoformat(),
                made_at=dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                dry_run=True, items=items, refused=refused, errors=errors,
                totals=dict(n=len(items), bytes=sum(i["size"] for i in items)), by_kind=by_kind,
                rules=dict(tmp_days=TMP_DAYS, render_keep_n=RENDER_KEEP_N,
                           render_keep_days=RENDER_KEEP_DAYS, log_max=LOG_MAX,
                           quarantine=str(conf.quarantine)))


def write_plan(conf, today=None, now=None):
    plan = make_plan(conf, now=now, today=today)
    conf.plans.mkdir(parents=True, exist_ok=True)
    f = conf.plans / f"{plan['date']}.json"
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, f)
    return plan


def dated_plans(conf):
    out = []
    if conf.plans.is_dir():
        for f in conf.plans.iterdir():
            m = _PLAN_NAME.match(f.name)
            if not m:
                continue
            try:
                dt.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            out.append(f)
    return sorted(out, key=lambda f: f.name)


# ------------------------------------------------------------------------------ rule 4: the idle check
# The out-of-process signals of app.py's _nightly_restart_busy (order 31), on the same files and
# commands. The lane locks are the running app's own and cannot be seen from here (pre-flight 2).

def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for l in fh:
                try:
                    out.append(json.loads(l))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def builder_active(conf):
    """(pid, unit) of a live builder, or (None, None). Mirrors app._builder_active."""
    try:
        pid = int(open(conf.build_runs / ".lock").read().strip())
        os.kill(pid, 0)
    except Exception:
        return None, None
    unit = ""
    try:
        last = [l for l in open(conf.build_runs / "RUNS.jsonl") if l.strip()][-1]
        unit = json.loads(last).get("unit", "")
    except Exception:
        pass
    return pid, unit


def queue_read(conf):
    try:
        return json.load(open(conf.build_runs / "QUEUE.json"))
    except Exception:
        return None


def queue_live(q):
    """Mirrors app._queue_live: the queue unit's pid, or a 'starting' queue younger than 2 minutes."""
    if not q or q.get("status") not in ("starting", "running", "waiting"):
        return False
    if q.get("pid"):
        try:
            os.kill(int(q["pid"]), 0)
            return True
        except Exception:
            return False
    try:
        return time.time() - dt.datetime.fromisoformat(q["started"]).timestamp() < 120
    except Exception:
        return False


def proc_started(pid):
    with open(f"/proc/{pid}/stat") as fh:
        rest = fh.read().rsplit(")", 1)[1].split()
    with open("/proc/stat") as fh:
        btime = next(int(l.split()[1]) for l in fh if l.startswith("btime "))
    return btime + int(rest[19]) / os.sysconf("SC_CLK_TCK")


def live_attempts(conf):
    """Mirrors app._nr_live_attempts: logged attempts whose pid still runs (not a reused pid)."""
    out, seen = [], set()
    for r in _read_jsonl(conf.attempts):
        pid = r.get("pid")
        if not isinstance(pid, int) or pid in seen:
            continue
        if not os.path.exists(f"/proc/{pid}"):
            continue
        try:
            logged = dt.datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00")).timestamp()
            if proc_started(pid) > logged + 60:
                continue
        except Exception:
            pass                                 # cannot tell: count it as running
        seen.add(pid)
        out.append(f"attempt pid {pid} ({r.get('scope')})")
    return out


def gpu_pids():
    r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}")
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def proc_argv(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as fh:
        argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    return argv, os.readlink(f"/proc/{pid}/cwd")


def gpu_others(conf, pids_fn=gpu_pids, argv_fn=proc_argv):
    """Mirrors app._nr_gpu_others: GPU users other than the resident render daemon."""
    try:
        pids = pids_fn()
    except Exception as e:
        return [f"GPU unknown (nvidia-smi failed: {type(e).__name__})"]
    out = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            argv, cwd = argv_fn(pid)
            if len(argv) > 1 and os.path.realpath(os.path.join(cwd, argv[1])) in conf.resident_gpu:
                continue
        except Exception:
            pass                                 # unreadable: not proven resident
        out.append(f"GPU pid {pid}")
    return out


def busy(conf, gpu_pids=gpu_pids, argv=proc_argv):
    """What is running, by name; [] when idle."""
    out = []
    pid, _unit = builder_active(conf)
    if pid:
        out.append(f"builder pid {pid}")
    if queue_live(queue_read(conf)):
        out.append("build queue")
    return out + live_attempts(conf) + gpu_others(conf, gpu_pids, argv)


# ------------------------------------------------------------------------------ apply

def _on_the_hdd(conf):
    hdd, q = os.path.realpath(conf.hdd), os.path.realpath(conf.quarantine)
    if not os.path.ismount(hdd):
        return False, f"{hdd} is not mounted"
    if not _inside(q, hdd):
        return False, f"{q} is not under {hdd}"
    if os.stat(hdd).st_dev == os.stat("/").st_dev:
        return False, f"{hdd} is on the same device as /"
    return True, ""


def _now_z():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_move(conf, rec):
    fd = os.open(conf.moves, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _do(conf, item):
    src, dest = item["path"], item["dest"]
    rec = dict(ts=_now_z(), action=item["action"], kind=item["kind"], path=src, dest=dest,
               size=item["size"], sha256=None, reason=item["reason"], ok=False)
    why = refuse_reason(src, conf)                 # again, at move time: the plan may be stale
    if why:
        rec["reason"] = f"{item['reason']}; refused at move time: {why}"
        _log_move(conf, rec)
        return False
    try:
        st = os.lstat(src)
        _, rec["size"], _, _ = _tree(src)
        if stat.S_ISREG(st.st_mode) and st.st_size <= SHA_MAX:
            rec["sha256"] = _sha256(src)
        if os.path.lexists(dest):
            raise FileExistsError(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if item["action"] == "compress":
            # copy-truncate (logrotate's way): the writer keeps its inode; the content is kept,
            # compressed, in the quarantine
            part = dest + ".part"
            with open(src, "rb") as fi, gzip.open(part, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.replace(part, dest)
            os.truncate(src, 0)
        else:
            shutil.move(src, dest)
        rec["ok"] = True
    except Exception as e:
        rec["reason"] = f"{item['reason']}; failed: {type(e).__name__}: {e}"
    _log_move(conf, rec)
    return rec["ok"]


def _unlink_tree(path):
    """Delete a quarantine day, one entry at a time, never following a link."""
    for d, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for n in files:
            os.unlink(os.path.join(d, n))
        for n in dirs:
            p = os.path.join(d, n)
            os.unlink(p) if os.path.islink(p) else os.rmdir(p)
    os.rmdir(path)


def _purge(conf, today):
    """Rule 1's second half: a quarantine day goes once it is more than 30 days old."""
    n = 0
    if not conf.quarantine.is_dir():
        return n
    for e in sorted(os.scandir(conf.quarantine), key=lambda e: e.name):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", e.name) or e.is_symlink() or not e.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(e.name)
        except ValueError:
            continue
        if (today - day).days <= QUARANTINE_DAYS:
            continue
        _, size, files, _ = _tree(e.path)
        rec = dict(ts=_now_z(), action="purge", kind="quarantine", path=e.path, dest=None, size=size,
                   sha256=None, reason=f"quarantine day older than {QUARANTINE_DAYS} days ({files} files)",
                   ok=False)
        try:
            _unlink_tree(e.path)
            rec["ok"] = True
            n += 1
        except OSError as x:
            rec["reason"] += f"; failed: {type(x).__name__}: {x}"
        _log_move(conf, rec)
    return n


def apply(conf, busy_fn=busy, today=None, now=None):
    """(done, message). Refuses unless every gate is open; a refusal touches nothing."""
    now = time.time() if now is None else now
    today = today or dt.date.fromtimestamp(now)
    if not conf.flag.exists():
        return False, f"refused: no {conf.flag} (the user creates it after reading a week of plans)"
    plans = dated_plans(conf)
    if len(plans) < PLANS_NEEDED:
        return False, f"refused: {len(plans)} dated plan(s), {PLANS_NEEDED} needed (dry run first)"
    seventh = plans[PLANS_NEEDED - 1]
    if conf.flag.stat().st_mtime <= seventh.stat().st_mtime:
        return False, (f"refused: the flag is not newer than the 7th plan ({seventh.name}): "
                       "the user reads the week of plans, then creates it")
    running = busy_fn(conf)
    if running:
        return False, "refused: a job is running — " + ", ".join(running)
    ok, why = _on_the_hdd(conf)
    if not ok:
        return False, f"refused: the quarantine is not on the HDD ({why})"
    plan = make_plan(conf, now=now, today=today)
    moved = sum(_do(conf, i) for i in plan["items"])
    purged = _purge(conf, today)
    return True, (f"applied: {moved}/{len(plan['items'])} moved or compressed, "
                  f"{purged} quarantine day(s) purged — see {conf.moves}")


# ------------------------------------------------------------------------------ check

def _http(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200, str(r.status)
    except Exception as e:
        return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"


def _pct(path):
    u = shutil.disk_usage(path)
    return 100.0 * u.used / (u.used + u.free)              # df's Use%


def _cpu_temp():
    if shutil.which("sensors"):
        try:
            out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"Package id \d+:\s*\+?([\d.]+)", out) or re.search(r"Tctl:\s*\+?([\d.]+)", out)
            if m:
                return float(m.group(1)), "sensors"
        except Exception:
            pass
    best = None
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (h / "name").read_text().strip() not in ("coretemp", "k10temp", "zenpower"):
                continue
            for t in h.glob("temp*_input"):
                v = int(t.read_text()) / 1000
                best = v if best is None else max(best, v)
        except Exception:
            continue
    return (best, "hwmon (sensors not installed)") if best is not None else (None, "no sensor")


def _gpu():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15)
        t, used, total = [int(x) for x in r.stdout.strip().splitlines()[0].split(",")]
        return t, used, total, None
    except Exception as e:
        return None, None, None, type(e).__name__


UNKNOWN_START = "unknown (cannot read /proc from here)"


def helix_started(conf, proc=Path("/proc")):
    """Start time of the running Helix (a python process whose script is ~/helix/app.py), and how many
    processes could not be decided. Inside Odin's wall /proc/<pid>/cwd is refused (EACCES, order 40b),
    so a relative argv[1] cannot be resolved there; no other /proc file is tried for the path. An
    absolute argv[1] needs no cwd. Returns (start or None, unreadable)."""
    target = os.path.realpath(conf.helix / "app.py")
    starts, unreadable = [], 0
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            with open(p / "cmdline", "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
            if not (len(argv) > 1 and "python" in os.path.basename(argv[0])):
                continue
            script = argv[1]
            if not os.path.isabs(script):
                script = os.path.join(os.readlink(p / "cwd"), script)
            if os.path.realpath(script) == target:
                starts.append(proc_started(int(p.name)))
        except (FileNotFoundError, ProcessLookupError):
            continue                             # exited while we looked
        except Exception:
            unreadable += 1                      # cannot tell: counted, and said
    return (min(starts) if starts else None), unreadable


def restart_pending(conf, started, unreadable=0):
    """Work that waits for a restart: reports ending READY FOR /restart, or an app.py edited, since
    the running Helix started. No start and unreadable processes: unknown, never "not running"."""
    if started is None:
        if unreadable:
            return [UNKNOWN_START, f"{unreadable} process{'' if unreadable == 1 else 'es'} unreadable"]
        return ["helix not running"]
    out = []
    for f in sorted((conf.helix / "build_orders" / "reports").glob("*.md")):
        try:
            if f.stat().st_mtime <= started:
                continue
            lines = [l.strip() for l in f.read_text(errors="replace").splitlines() if l.strip()]
            if lines and lines[-1] == "READY FOR /restart":
                out.append(f.name)
        except OSError:
            continue
    app = conf.helix / "app.py"
    if app.exists() and app.stat().st_mtime > started:
        out.append("app.py changed since start")
    return out


def _meminfo():
    m = {}
    with open("/proc/meminfo") as fh:
        for l in fh:
            k, v = l.split(":", 1)
            m[k] = int(v.split()[0]) / 2**20                 # kB → GiB
    return m["MemTotal"], m["MemFree"], m["MemAvailable"]


def measure(conf):
    hdd_mounted = os.path.ismount(conf.hdd)
    total, free, avail = _meminfo()
    ct, csrc = _cpu_temp()
    gt, gm, gmt, gerr = _gpu()
    hok, hwhy = _http(HELIX_STAT)
    rok, rwhy = _http(RENDER_HEALTH)
    return dict(root_pct=_pct("/"), hdd_pct=_pct(conf.hdd) if hdd_mounted else None,
                hdd_mounted=hdd_mounted, mem_total=total, mem_free=free, mem_avail=avail,
                cpu_temp=ct, cpu_src=csrc, gpu_temp=gt, gpu_mem=gm, gpu_mem_total=gmt, gpu_err=gerr,
                odin_mounted=os.path.ismount(conf.odin_drive), helix_ok=hok, helix_why=hwhy,
                render_ok=rok, render_why=rwhy, restart_pending=restart_pending(conf, *helix_started(conf)))


def check_lines(m):
    cpu = (f"{m['cpu_temp']:.0f}°C ({m['cpu_src']})" if m["cpu_temp"] is not None
           else f"n/a ({m['cpu_src']})")
    gpu = (f"{m['gpu_temp']}°C, {m['gpu_mem']}/{m['gpu_mem_total']} MiB" if not m["gpu_err"]
           else f"n/a (nvidia-smi: {m['gpu_err']})")
    return [f"/ disk: {m['root_pct']:.0f}%",
            f"__BULK__ disk: {m['hdd_pct']:.0f}%" if m["hdd_mounted"] else "__BULK__ disk: NOT mounted",
            f"RAM: {m['mem_free']:.1f}G free, {m['mem_avail']:.1f}G available, of {m['mem_total']:.0f}G",
            f"CPU temp: {cpu}",
            f"GPU: {gpu}",
            f"odin drive: {'mounted' if m['odin_mounted'] else 'NOT mounted'}",
            f"Helix /stat: {'answering' if m['helix_ok'] else 'NOT answering'} ({m['helix_why']})",
            f"render daemon: {'alive' if m['render_ok'] else 'DOWN'} (/health {m['render_why']})",
            f"needs restart: {', '.join(m['restart_pending']) or 'nothing pending'}"]


def anomalies(m):
    out = []
    if m["root_pct"] > DISK_ALARM:
        out.append(f"/ disk {m['root_pct']:.0f}%")
    if not m["hdd_mounted"]:
        out.append("__BULK__ not mounted")
    elif m["hdd_pct"] is not None and m["hdd_pct"] > DISK_ALARM:
        out.append(f"__BULK__ disk {m['hdd_pct']:.0f}%")
    if not m["odin_mounted"]:
        out.append("odin drive not mounted")
    if not m["render_ok"]:
        out.append("render daemon down")
    return out


def house_line(conf=None, today=None, now=None):
    """The nightly's one line (Part B). Writes today's plan; moves nothing; never raises."""
    try:
        conf = conf or Conf()
        m = measure(conf)
        plan = write_plan(conf, today=today, now=now)
        k = len(dated_plans(conf))
        parts = [f"/ {m['root_pct']:.0f}%",
                 f"hdd {m['hdd_pct']:.0f}%" if m["hdd_mounted"] else "hdd NOT mounted",
                 f"RAM {m['mem_avail']:.1f}G avail of {m['mem_total']:.0f}G",
                 f"CPU {m['cpu_temp']:.0f}°C" if m["cpu_temp"] is not None else "CPU n/a",
                 f"GPU {m['gpu_temp']}°C {m['gpu_mem']}MiB" if not m["gpu_err"] else "GPU n/a",
                 "odin drive mounted" if m["odin_mounted"] else "odin drive NOT mounted",
                 f"plan: would move {plan['totals']['n']} items, {plan['totals']['bytes'] / 1e9:.2f} GB "
                 f"(dry run, day {k}/{PLANS_NEEDED})"]
        parts += [f"🔴 {a}" for a in anomalies(m)]
        return "- house: " + " · ".join(parts)
    except Exception as e:
        return f"- house: 🔴 did not run ({type(e).__name__}: {e})"


# ------------------------------------------------------------------------------ CLI

def _size(b):
    return f"{b / 2**30:.2f} GiB" if b >= 2**30 else f"{b / 2**20:.1f} MiB" if b >= 2**20 else f"{b / 1024:.1f} KiB"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("verb", choices=("check", "plan", "apply"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="plan: list every __pycache__ dir too")
    a = ap.parse_args(argv)
    conf = Conf()
    if a.verb == "check":
        m = measure(conf)
        if a.json:
            print(json.dumps(m, indent=1))
            return 0
        print("\n".join(check_lines(m)))
        bad = anomalies(m)
        print("anomalies: " + ("; ".join(bad) if bad else "none") + " (reported, never fixed)")
        return 0
    if a.verb == "plan":
        plan = write_plan(conf)
        if a.json:
            print(json.dumps(plan, indent=1, ensure_ascii=False))
            return 0
        t = plan["totals"]
        print(f"plan {plan['date']} (DRY RUN — nothing moved): would move {t['n']} items, "
              f"{_size(t['bytes'])} ({t['bytes'] / 1e9:.2f} GB) → {conf.quarantine}/{plan['date']}/")
        for kind in ("tmp", "pycache", "render", "log"):
            n, b = plan["by_kind"].get(kind, [0, 0])
            print(f"  {kind}: {n} items, {_size(b)}")
        for i in plan["items"]:
            if i["kind"] != "pycache" or a.all:
                print(f"    {i['action']:8} {_size(i['size']):>11}  {i['path']}  ({i['reason']})")
        if not a.all and plan["by_kind"].get("pycache"):
            print(f"    (… {plan['by_kind']['pycache'][0]} __pycache__ dirs; --all lists them)")
        print(f"  refused (rule 2): {len(plan['refused'])}")
        for r in plan["refused"]:
            print(f"    {r['path']}  — {r['reason']}")
        print(f"  errors: {len(plan['errors'])}")
        for e in plan["errors"][:20]:
            print(f"    {e}")
        print(f"  dated plans: {len(dated_plans(conf))}/{PLANS_NEEDED} · written {conf.plans / (plan['date'] + '.json')}")
        return 0
    ok, why = apply(conf)
    print(why)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
