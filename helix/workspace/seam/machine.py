#!/usr/bin/env python3
"""ORDER 67 (item 32): Helix looks after the machine it runs on. One `- machine:` line for the nightly.

Runs as __USER__ from the nightly. Two halves:
  1. user-level reads, each with a timeout of 10 s, none of which can stop the rest: disks, CPU
     temperature, the GPU, failed units, reboot-required, upgradable / autoremovable counts,
     uptime, RAM. Autoremovable packages are reported, never acted on.
  2. root, only through the allowlist (`sudo -n /usr/local/sbin/helix-machine <verb>`): health
     (SMART), then update, then clean. If sudo refuses, nothing else is tried as root.
Appends one JSON line per run to MACHINE.jsonl: every reading + reboot_due + reboot_reason. app.py
reads the last line to decide a night reboot; this file never reboots anything.

`--dry` (for Odin and tests): read only, no sudo, nothing appended; prints the line.
Fakes are passed in as arguments (`run=`, paths); there is no environment switch.
"""
import datetime
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
MACHINE = HERE / "MACHINE.jsonl"
HELIX_MACHINE = "/usr/local/sbin/helix-machine"
SUDO = ["sudo", "-n", HELIX_MACHINE]
LAST_REBOOT = pathlib.Path("/var/lib/helix-machine/last_reboot.json")
REBOOT_REQUIRED = pathlib.Path("/var/run/reboot-required")
PROC = pathlib.Path("/proc")
MOUNTS = (("/", "SSD"), ("__BULK__", "HDD"))
READ_TIMEOUT = 10
# update dominates: helix-machine caps itself at 2700 s; the whole step stays under 50 min
ROOT_TIMEOUTS = {"health": 60, "update": 2730, "clean": 60}
GPU_MISMATCH = "Driver/library version mismatch"
COUNTS = (("realloc", "reallocated"), ("pending", "pending"), ("uncorrectable", "uncorrectable"))
INSTALL_HINT = "the user: `sudo bash ~/helix/machine/INSTALL_FOR_USER.sh`"
# ORDER 68: a USB bridge usually passes no SMART through, so a USB disk with no result is not a warning.
LSBLK = ["lsblk", "-dno", "NAME,TRAN"]
USB = "usb"


def _run(argv, timeout):
    """(exit code, stdout, stderr); exit code None when it timed out or could not start."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout} s"
    except OSError as e:
        return None, "", f"could not run: {e.strerror}"


# ------------------------------------------------------------------ parsers (pure)

def parse_df(out):
    """`df -P` → {mount point: % used}."""
    pct = {}
    for l in out.splitlines()[1:]:
        f = l.split()
        if len(f) >= 6 and f[4].endswith("%") and f[4][:-1].isdigit():
            pct[f[5]] = int(f[4][:-1])
    return pct


def parse_sensors(out):
    """CPU package temperature in °C from `sensors`, or None."""
    m = re.search(r"^Package id \d+:\s*\+?(-?\d+(?:\.\d+)?)\s*°?C", out, flags=re.M)
    if not m:
        m = re.search(r"^Tctl:\s*\+?(-?\d+(?:\.\d+)?)\s*°?C", out, flags=re.M)
    return float(m.group(1)) if m else None


def parse_nvidia(code, out, err):
    """("OK" or the error in plain text, driver/library mismatch?)."""
    text = (out or "") + "\n" + (err or "")
    mismatch = GPU_MISMATCH in text
    if code == 0 and not mismatch:
        return "OK", False
    first = next((l.strip() for l in text.splitlines() if l.strip()), f"exit {code}")
    return first, mismatch


def parse_failed_units(out):
    """Unit names from `systemctl --failed --no-legend` (with or without the ● column)."""
    units = []
    for l in out.splitlines():
        f = [x for x in l.split() if x not in ("●", "*")]
        if f and "." in f[0]:
            units.append(f[0])
    return units


def parse_upgradable(out):
    return sum(1 for l in out.splitlines() if "[upgradable from" in l)


def parse_autoremovable(out):
    return sum(1 for l in out.splitlines() if l.startswith("Remv"))


def parse_meminfo(text):
    kv = {}
    for l in text.splitlines():
        k, _, v = l.partition(":")
        if v.strip().split()[:1] and v.split()[0].isdigit():
            kv[k] = int(v.split()[0])
    if not kv.get("MemTotal") or "MemAvailable" not in kv:
        return None
    return round(100 * (1 - kv["MemAvailable"] / kv["MemTotal"]), 1)


def parse_lsblk(out):
    """`lsblk -dno NAME,TRAN` → {disk: transport}; disks with no transport (loop devices) are left out."""
    tran = {}
    for l in out.splitlines():
        f = l.split()
        if len(f) == 2:
            tran[f[0]] = f[1]
    return tran


_DISK = re.compile(r"^(?:=== disk ([a-z0-9]+) ===|-- /dev/([a-z0-9]+))\s*$")
_ATTR = re.compile(r"^\s*(\d+)\s+(\S+)\s+0x[0-9a-fA-F]+\s+(\d+)\s+(\d+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(.*)$")


def _raw(text):
    m = re.match(r"\s*(\d+)", text)
    return int(m.group(1)) if m else None


def parse_smart(out):
    """helix-machine health output → {disk: {overall, realloc, pending, uncorrectable, wear,
    power_on_hours}}. Missing attributes are None; overall is PASSED, FAILED or None."""
    disks, cur = {}, None
    for l in out.splitlines():
        m = _DISK.match(l)
        if m:
            cur = m.group(1) or m.group(2)
            disks[cur] = dict(overall=None, realloc=None, pending=None, uncorrectable=None,
                              wear=None, power_on_hours=None)
            continue
        if cur is None:
            continue
        d = disks[cur]
        h = re.search(r"(?:overall-health self-assessment test result|SMART Health Status):\s*(\S+)", l)
        if h:
            d["overall"] = "PASSED" if h.group(1) in ("PASSED", "OK") else h.group(1)
            continue
        p = re.match(r"\s*Percentage Used:\s*(\d+)%", l)
        if p:
            d["wear"] = f"{p.group(1)}% used"
            continue
        a = _ATTR.match(l)
        if not a:
            continue
        name, value, raw = a.group(2), int(a.group(3)), _raw(a.group(6))
        if name == "Reallocated_Sector_Ct":
            d["realloc"] = raw
        elif name == "Current_Pending_Sector":
            d["pending"] = raw
        elif name == "Offline_Uncorrectable":
            d["uncorrectable"] = raw
        elif name == "Wear_Leveling_Count":
            d["wear"] = f"{value} left (raw {raw})"
        elif name.startswith("Power_On_Hours"):
            d["power_on_hours"] = raw
    return disks


# ------------------------------------------------------------------ the check

def _last_lines(ledger):
    try:
        return [json.loads(l) for l in pathlib.Path(ledger).read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def _previous_counts(history):
    """{disk: {count: value}} from the latest earlier run that read each disk's SMART."""
    prev = {}
    for rec in reversed(history):
        for disk, d in (rec.get("smart") or {}).items():
            if disk not in prev:
                prev[disk] = {k: d.get(k) for k, _ in COUNTS}
    return prev


def read_user(run, reboot_required, proc):
    r = {}
    code, out, _ = run(["df", "-P", *[m for m, _ in MOUNTS]], READ_TIMEOUT)
    r["disk_pct"] = parse_df(out) if code is not None else {}
    code, out, _ = run(["sensors"], READ_TIMEOUT)
    r["cpu_c"] = parse_sensors(out) if code == 0 else None
    r["gpu"], r["gpu_mismatch"] = parse_nvidia(*run(["nvidia-smi"], READ_TIMEOUT))
    units = []
    for argv, tag in ((["systemctl", "--failed", "--no-legend"], "system"),
                      (["systemctl", "--user", "--failed", "--no-legend"], "user")):
        code, out, _ = run(argv, READ_TIMEOUT)
        units += [f"{u} ({tag})" for u in parse_failed_units(out)] if code is not None else []
    r["failed_units"] = units
    rr = pathlib.Path(reboot_required)
    r["reboot_required"] = rr.exists()
    try:
        r["reboot_pkgs"] = pathlib.Path(f"{rr}.pkgs").read_text().split()
    except OSError:
        r["reboot_pkgs"] = []
    code, out, _ = run(["apt", "list", "--upgradable"], READ_TIMEOUT)
    r["upgradable"] = parse_upgradable(out) if code == 0 else None
    code, out, _ = run(["apt-get", "-s", "autoremove"], READ_TIMEOUT)
    r["autoremovable"] = parse_autoremovable(out) if code == 0 else None
    try:
        r["uptime_s"] = int(float((pathlib.Path(proc) / "uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        r["uptime_s"] = None
    try:
        r["ram_pct"] = parse_meminfo((pathlib.Path(proc) / "meminfo").read_text())
    except OSError:
        r["ram_pct"] = None
    return r


def read_transport(run):
    """{disk: transport} at user level. Never raises; on any failure {} (every disk unknown)."""
    try:
        code, out, _ = run(LSBLK, READ_TIMEOUT)
        return parse_lsblk(out) if code == 0 else {}
    except Exception:
        return {}


def _usb_unchecked(d):
    return d["overall"] is None and d.get("tran") == USB


def _sudo_refused(code, err):
    return code not in (0, None) and any(l.startswith("sudo:") for l in (err or "").splitlines())


def read_root(run):
    """SMART, update, clean through the allowlist. Stops at the first sudo refusal."""
    r = {"root": "ok", "smart": {}, "update_rc": None, "clean_rc": None, "smart_error": None}
    code, out, err = run([*SUDO, "health"], ROOT_TIMEOUTS["health"])
    if _sudo_refused(code, err):
        r["root"] = "not installed"
        return r
    r["smart"] = parse_smart(out)
    if code != 0:
        r["smart_error"] = err.strip().splitlines()[0] if (err or "").strip() else f"exit {code}"
    code, _, err = run([*SUDO, "update"], ROOT_TIMEOUTS["update"])
    r["update_rc"] = code if code is not None else "timeout"
    code, _, err = run([*SUDO, "clean"], ROOT_TIMEOUTS["clean"])
    r["clean_rc"] = code if code is not None else "timeout"
    return r


def _hhmm(ts):
    t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t.astimezone().strftime("%H:%M")


def check(run=_run, dry=False, ledger=MACHINE, last_reboot=LAST_REBOOT,
          reboot_required=REBOOT_REQUIRED, proc=PROC, helix_machine=HELIX_MACHINE, now=None):
    """All readings → (record, the `- machine:` line). Appends the record unless dry."""
    t = now or datetime.datetime.now(datetime.timezone.utc)
    history = _last_lines(ledger)
    rec = {"ts": t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "date": t.astimezone().date().isoformat(), "dry": bool(dry)}
    rec.update(read_user(run, reboot_required, proc))
    if dry:
        rec.update(root="not installed" if not pathlib.Path(helix_machine).exists()
                   else "skipped (--dry)", smart={}, update_rc=None, clean_rc=None, smart_error=None)
    else:
        rec.update(read_root(run))
        if rec["smart"]:
            tran = read_transport(run)
            for disk, d in rec["smart"].items():
                d["tran"] = tran.get(disk)
        if rec["update_rc"] is not None:
            code, out, _ = run(["apt", "list", "--upgradable"], READ_TIMEOUT)
            rec["upgradable"] = parse_upgradable(out) if code == 0 else rec["upgradable"]

    reasons = []
    if rec["reboot_required"]:
        pk = rec["reboot_pkgs"]
        reasons.append("reboot-required" + (f" ({', '.join(pk[:3])}{' …' if len(pk) > 3 else ''})" if pk else ""))
    if rec["gpu_mismatch"]:
        reasons.append("GPU driver mismatch")
    rec["reboot_due"] = bool(reasons)
    rec["reboot_reason"] = "; ".join(reasons) or None

    red, yellow = [], []
    for mount, label in MOUNTS:
        p = rec["disk_pct"].get(mount)
        if p is None:
            yellow.append(f"{label} ({mount}) not readable — not mounted?")
        elif p >= 90:
            red.append(f"{label} ({mount}) is {p}% full")
        elif p >= 80:
            yellow.append(f"{label} ({mount}) is {p}% full")
    if rec["cpu_c"] is not None and rec["cpu_c"] >= 80:
        yellow.append(f"CPU is hot: {rec['cpu_c']:.0f}°C")
    if rec["gpu"] != "OK":
        red.append("GPU not working: " + ("driver/library version mismatch (a reboot fixes it)"
                                          if rec["gpu_mismatch"] else rec["gpu"]))
    for u in rec["failed_units"]:
        red.append(f"failed unit: {u}")

    prev = _previous_counts(history)
    baseline = False
    for disk, d in sorted(rec["smart"].items()):
        if d["overall"] == "FAILED":
            red.append(f"disk {disk}: SMART says FAILED")
        elif d["overall"] != "PASSED" and not _usb_unchecked(d):
            yellow.append(f"disk {disk}: no SMART health result")
        if disk not in prev:
            baseline = True
            continue
        for key, words in COUNTS:
            old, new = prev[disk].get(key), d.get(key)
            if old is not None and new is not None and new > old:
                red.append(f"disk {disk}: {words} sectors went up, {old} → {new}")
    if rec["smart_error"]:
        yellow.append(f"SMART check did not finish cleanly ({rec['smart_error']})")
    if rec["reboot_due"]:
        yellow.append(f"reboot due: {rec['reboot_reason']}")
    for verb in ("update", "clean"):
        rc = rec[f"{verb}_rc"]
        if rc == 3 and verb == "update":
            yellow.append("update not run: another package install held the dpkg lock (exit 3)")
        elif rc not in (None, 0):
            yellow.append(f"{verb} failed ({'timed out' if rc == 'timeout' else f'exit {rc}'})")
    if rec["autoremovable"]:
        yellow.append(f"{rec['autoremovable']} packages no longer needed — the user: `sudo apt autoremove`")
    if rec["root"] == "not installed":
        yellow.append(f"root checks not installed (no SMART, no updates) — {INSTALL_HINT}")

    head = []
    for mount, label in MOUNTS:
        p = rec["disk_pct"].get(mount)
        head.append(f"{label} {p}%" if p is not None else f"{label} ?")
    head.append(f"CPU {rec['cpu_c']:.0f}°C" if rec["cpu_c"] is not None else "CPU ?")
    head.append("GPU OK" if rec["gpu"] == "OK" else "GPU FAILED")
    n = rec["upgradable"]
    head.append(f"{n} update{'' if n == 1 else 's'} left" if n is not None else "updates ?")
    if not rec["smart"]:
        head.append("disks not checked")
    else:
        bad = [k for k, d in sorted(rec["smart"].items()) if d["overall"] != "PASSED" and not _usb_unchecked(d)]
        head.append("disks PASSED" if not bad else "disks: " + ", ".join(
            f"{k} {rec['smart'][k]['overall'] or '?'}" for k in bad))
        usb = sum(1 for d in rec["smart"].values() if _usb_unchecked(d))
        if usb:
            head.append(f"{usb} USB not checked")
    if baseline:
        head.append("baseline recorded")
    try:
        lr = json.loads(pathlib.Path(last_reboot).read_text())
        if not history or str(lr["ts"]) > str(history[-1].get("ts", "")):
            head.append(f"rebooted at {_hhmm(lr['ts'])} ({lr.get('reason')})")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    rec["problems"] = [f"🔴 {x}" for x in red] + [f"🟡 {x}" for x in yellow]

    if not dry:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    status = "OK" if not rec["problems"] else " · ".join(
        x for x in (f"🔴 {len(red)}" if red else "", f"🟡 {len(yellow)}" if yellow else "") if x)
    line = f"- machine: {status} · " + " · ".join(head)
    return rec, "\n".join([line] + [f"  - {p}" for p in rec["problems"]])


def machine_line(**kw):
    """The nightly's line. Never raises (same contract as vault_line())."""
    try:
        return check(**kw)[1]
    except Exception as e:
        return f"- machine: 🔴 machine check did not run ({type(e).__name__}: {e})"


def main(argv):
    if argv not in ([], ["--dry"]):
        print("usage: machine.py [--dry]", file=sys.stderr)
        return 2
    print(machine_line(dry=bool(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
