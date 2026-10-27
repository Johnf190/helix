#!/usr/bin/env python3
"""The infrastructure heartbeat (order 30). Every host and path the record depends on, probed
once a night, one line in NIGHTLY.md.

The BOX2 address changed and nothing noticed for 18 days. So a target that is down is printed
EVERY night with its count, not just the night it changes; silence is how the last gap hid.

Read-only: TCP connects and real commands, never ICMP (no raw sockets in Odin's shell), no
mounting, no ssh beyond `true`, no fixing. Each probe has a hard limit of PROBE_LIMIT seconds and
all of them run side by side, so a run takes about PROBE_LIMIT, never more than RUN_LIMIT.
Nothing here raises — same contract as nightly.vault_line(): a dead heartbeat is a line, not a
stopped nightly.

  heartbeat.py --state HEARTBEAT.jsonl   probe, append one JSON line, print the line
  heartbeat.py --dry                     probe and print; reads the state, writes nothing
"""
import argparse, datetime, json, os, pathlib, socket, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
TARGETS = HERE / "heartbeat_targets.json"
STATE = HERE / "HEARTBEAT.jsonl"
PROBE_LIMIT = 5
RUN_LIMIT = 60


def short(text, n=80):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _reason(err):
    if isinstance(err, ConnectionRefusedError):
        return "connection refused"
    if isinstance(err, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(err, socket.gaierror):
        return f"name not resolved ({err.strerror})"
    return (err.strerror or type(err).__name__).lower() if isinstance(err, OSError) else repr(err)


def _tail(text):
    """The useful end of a command's complaint: 'ssh: connect … port 22: Connection refused'
    → 'connection refused'."""
    last = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), "")
    return short(last.rsplit(": ", 1)[-1].lower()) if last else ""


def _run(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g} s"
    except OSError as e:
        return None, "", f"could not run {argv[0]}: {_reason(e)}"


# ---- probes: target dict in, (ok, short reason) out ----------------------------------------

def probe_tcp(t):
    timeout = t.get("timeout", 3)
    try:
        with socket.create_connection((t["host"], t["port"]), timeout=timeout):
            return True, f"{t['host']}:{t['port']} connects"
    except OSError as e:
        why = _reason(e)
        return False, f"timed out after {timeout:g} s" if why == "timed out" else why


def probe_command(t):
    code, out, err = _run(t["argv"], t.get("timeout", 4))
    if code is None:
        return False, err
    want = t.get("expect")
    if want is not None:
        got = out.strip()
        return (True, got) if got == want else (False, f"{got or 'no output'} (exit {code})")
    if code == 0:
        return True, "exit 0"
    return False, f"exit {code}: {_tail(err) or _tail(out) or 'no output'}"


def probe_smb_share(t):
    ok, why = probe_tcp({"host": t["host"], "port": t.get("port", 445), "timeout": t.get("tcp_timeout", 2)})
    if not ok:
        return False, f"port {t.get('port', 445)}: {why}"
    code, out, err = _run(["smbclient", "-N", "-L", f"//{t['host']}"], t.get("cmd_timeout", 3))
    if code is None:
        return False, err
    listed = any(l.split()[:2] == [t["share"], "Disk"] for l in out.splitlines())
    if listed:
        return True, f"{t['share']} listed"
    status = next((w for w in (out + " " + err).split() if w.startswith("NT_STATUS_")), None)
    return False, status or f"{t['share']} not listed (exit {code})"


def probe_tailnet(t):
    code, out, err = _run(["tailscale", "status", "--json"], t.get("timeout", 4))
    if code is None:
        return False, err
    try:
        s = json.loads(out)
    except ValueError:
        return False, f"exit {code}: {_tail(err) or 'no JSON'}"
    state = s.get("BackendState")
    online = (s.get("Self") or {}).get("Online")
    if state == "Running" and online:
        return True, "running, self online"
    return False, f"BackendState {state}, self {'online' if online else 'offline'}"


def probe_mount_file(t):
    mp = pathlib.Path(t["mountpoint"])
    if not os.path.ismount(mp):
        return False, f"{mp} is not a mountpoint"
    if not (mp / t["file"]).exists():
        return False, f"{t['file']} missing"
    return True, "mounted, file present"


def probe_mount_link(t):
    mp = pathlib.Path(t["mountpoint"])
    if not os.path.ismount(mp):
        return False, f"{mp} is not a mountpoint"
    link = pathlib.Path(t["link"]).expanduser()
    real = link.resolve()
    if real != mp and mp not in real.parents:
        return False, f"{t['link']} resolves to {real}, not into {mp}"
    return True, f"mounted, {t['link']} → {real}"


PROBES = {"tcp": probe_tcp, "command": probe_command, "smb_share": probe_smb_share,
          "tailnet": probe_tailnet, "mount_file": probe_mount_file, "mount_link": probe_mount_link}


def run_probes(targets, probes=None):
    """All probes at once, each on a daemon thread; one that outlives PROBE_LIMIT is reported as
    timed out and abandoned. Returns {id: {"ok", "detail"}} in target order."""
    probes = PROBES if probes is None else probes
    limit = min(PROBE_LIMIT, RUN_LIMIT)
    box = {}

    def one(t):
        try:
            fn = probes.get(t.get("kind"))
            if fn is None:
                box[t["id"]] = (False, f"unknown probe kind {t.get('kind')!r}")
                return
            ok, detail = fn(t)
            box[t["id"]] = (bool(ok), short(detail))
        except BaseException as e:   # noqa: BLE001 — a probe must never take the nightly down
            box[t["id"]] = (False, short(f"probe error: {type(e).__name__}: {e}"))

    threads = [(t, threading.Thread(target=one, args=(t,), daemon=True)) for t in targets]
    for _, th in threads:
        th.start()
    deadline = time.monotonic() + limit
    for _, th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
    out = {}
    for t, _ in threads:
        ok, detail = box.get(t["id"], (False, f"timed out after {limit:g} s"))
        out[t["id"]] = {"ok": ok, "detail": detail}
    return out


# ---- state -----------------------------------------------------------------------------------

def read_state(path):
    """(rows, problem). problem is None, 'missing', or 'corrupt (line N)'."""
    path = pathlib.Path(path)
    if not path.exists():
        return [], "missing"
    rows = []
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return [], f"unreadable ({short(e, 40)})"
    for n, l in enumerate(lines, 1):
        if not l.strip():
            continue
        try:
            row = json.loads(l)
            ok = (isinstance(row, dict) and isinstance(row.get("ts"), str)
                  and isinstance(row.get("results"), dict)
                  and all(isinstance(v, dict) and isinstance(v.get("ok"), bool)
                          for v in row["results"].values()))
        except ValueError:
            ok = False
        if not ok:
            return [], f"corrupt (line {n})"
        rows.append(row)
    return rows, None


def _plural(n):
    return f"{n} night" + ("" if n == 1 else "s")


def compose(rows, results, now):
    """The parts of the line from history (oldest first) plus tonight. No I/O."""
    downs, backs, news = [], [], []
    today = now.date().isoformat()
    for tid, r in results.items():
        past = [row for row in rows if tid in row["results"]]
        if r["ok"]:
            if not past:
                if rows:
                    news.append(f"{tid} new")
                continue
            was = 0
            for row in reversed(past):
                if row["results"][tid]["ok"]:
                    break
                was += 1
            if was:
                backs.append(f"{tid} back (was down {_plural(was)})")
            continue
        streak = [today]
        for row in reversed(past):
            if row["results"][tid]["ok"]:
                break
            streak.append(row["ts"][:10])
        new = "new, " if rows and not past else ""
        downs.append(f"{tid} DOWN ({new}since {streak[-1]}, {_plural(len(streak))}: {r['detail']})")
    n_ok = sum(1 for r in results.values() if r["ok"])
    return downs, backs, news, f"{n_ok}/{len(results)} OK"


def heartbeat_line(state=STATE, targets=None, targets_path=TARGETS, probes=None, now=None, write=True,
                   results=None):
    """One line for NIGHTLY.md. write=False is --dry: the state is read, never written or moved.
    results: tonight's probes if the caller already ran them (--dry prints them first)."""
    try:
        now = now or datetime.datetime.now()
        if targets is None and results is None:
            try:
                targets = json.loads(pathlib.Path(targets_path).read_text())["targets"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                return f"- heartbeat: 🔴 unavailable (no targets: {short(e, 60)})"
        results = run_probes(targets, probes) if results is None else results
        state = pathlib.Path(state)
        rows, problem = read_state(state)
        notes, red = [], False
        if problem == "missing":
            notes.append("no state yet, starting fresh")
        elif problem and not write:
            notes.append(f"state is {problem}, would start fresh")
            red = True
        elif problem:
            aside = state.with_name(f"{state.name}.corrupt-{now:%Y%m%dT%H%M%S}")
            try:
                state.rename(aside)
                notes.append(f"state was {problem}, set aside as {aside.name}, starting fresh")
            except OSError as e:
                notes.append(f"state was {problem}, could not set aside ({_reason(e)}), starting fresh")
            red = True
        if write:
            try:
                with state.open("a") as f:
                    f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "results": results}) + "\n")
            except OSError as e:
                notes.insert(0, f"state not written ({_reason(e)})")
                red = True
        downs, backs, news, count = compose(rows, results, now)
        red = red or bool(downs)
        return "- heartbeat: " + ("🔴 " if red else "") + " · ".join(notes + downs + backs + news + [count])
    except Exception as e:   # noqa: BLE001 — never stop the nightly
        return f"- heartbeat: 🔴 unavailable ({type(e).__name__}: {short(e, 60)})"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=str(STATE), help="the HEARTBEAT.jsonl to read (and append to)")
    ap.add_argument("--targets", default=str(TARGETS))
    ap.add_argument("--dry", action="store_true", help="probe and print; write nothing")
    a = ap.parse_args(argv)
    results = None
    if a.dry:
        try:
            results = run_probes(json.loads(pathlib.Path(a.targets).read_text())["targets"])
            for tid, r in results.items():
                print(f"{'OK  ' if r['ok'] else 'DOWN'}  {tid:<12} {r['detail']}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"no targets: {e}")
    print(heartbeat_line(a.state, targets_path=a.targets, write=not a.dry, results=results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
