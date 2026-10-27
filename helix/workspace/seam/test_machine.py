"""Tests for ORDER 67 Part B — machine.py, the nightly's `- machine:` line.

Every command goes through a fake `run=` passed in as an argument; every path (the ledger,
last_reboot.json, reboot-required, /proc) is a temp path passed in. Nothing here calls sudo, apt,
smartctl or nvidia-smi, and the live MACHINE.jsonl is never written.

Run:  python3 -m pytest -q workspace/seam/test_machine.py
"""

import sys

sys.dont_write_bytecode = True

import json
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import machine  # noqa: E402

# The 2026-10-18 SMART text, verbatim from /var/log/helix-maintenance-2026-10-18.log (lines 664-673).
SMART_20261018 = """\
-- /dev/sda
SMART overall-health self-assessment test result: PASSED
  5 Reallocated_Sector_Ct   0x0032   100   100   000    Old_age   Always       -       189
  9 Power_On_Hours_and_Msec 0x0032   100   100   000    Old_age   Always       -       1239h+00m+00.000s
-- /dev/sdb
SMART overall-health self-assessment test result: PASSED
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       8
  9 Power_On_Hours          0x0032   083   083   000    Old_age   Always       -       15721
194 Temperature_Celsius     0x0022   034   040   000    Old_age   Always       -       34 (0 12 0 0 0)
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0
"""

DF = """\
Filesystem     1024-blocks      Used  Available Capacity Mounted on
/dev/sda2        228554124  93774160  123097200      44% /
/dev/sdb2       1921595592 223479184 1600430904      13% __BULK__
"""
SENSORS = """\
coretemp-isa-0000
Adapter: ISA adapter
Package id 0:  +57.0°C  (high = +85.0°C, crit = +95.0°C)
Core 0:        +55.0°C  (high = +85.0°C, crit = +95.0°C)
"""
SMI_OK = "| NVIDIA-SMI 580.178.04             Driver Version: 580.178.04     CUDA Version: 13.0     |\n"
SMI_MISMATCH = "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 580.95\n"
APT_LIST = "Listing...\nsnapd/noble-updates 2.77.1+ubuntu24.04 amd64 [upgradable from: 2.76+ubuntu24.04.1]\n"
FAILED_UNIT = "● sidekick.service loaded failed failed sidekick\n"
# `lsblk -dno NAME,TRAN` on this machine 2026-10-20 (loop devices trimmed): sdc, sdd are the USB drives.
LSBLK = "loop0  \nsda    sata\nsdb    sata\nsdc    usb\nsdd    usb\n"


def health_out(realloc_sda=189):
    """helix-machine health's own format around the fixture's SMART lines."""
    return (SMART_20261018.replace("-- /dev/sda", "=== disk sda ===")
            .replace("-- /dev/sdb", "=== exit sda 0 ===\n=== disk sdb ===")
            .replace("-       189", f"-       {realloc_sda}") + "=== exit sdb 0 ===\n")


def health_with_usb(sdc="", sdd=""):
    """health_out() plus sdc and sdd; an empty body is a USB bridge that passes no SMART through."""
    return (health_out() + f"=== disk sdc ===\n{sdc}=== exit sdc 1 ===\n"
            f"=== disk sdd ===\n{sdd}=== exit sdd 1 ===\n")


class Fake:
    """A recording `run`. Defaults describe a healthy machine with the root program installed."""

    def __init__(self, **over):
        self.calls = []
        self.r = {
            "df": (0, DF, ""), "sensors": (0, SENSORS, ""), "nvidia-smi": (0, SMI_OK, ""),
            "systemctl": (0, "", ""), "systemctl --user": (0, "", ""),
            "apt list": (0, "Listing...\n", ""), "apt-get -s": (0, "Reading package lists...\n", ""),
            "sudo health": (0, health_out(), ""), "sudo update": (0, "", ""), "sudo clean": (0, "", ""),
            "lsblk": (0, LSBLK, ""),
        }
        self.r.update(over)

    def key(self, argv):
        if argv[0] == "sudo":
            assert argv[:3] == ["sudo", "-n", "/usr/local/sbin/helix-machine"] and len(argv) == 4
            return f"sudo {argv[3]}"
        if argv[:2] == ["systemctl", "--user"]:
            return "systemctl --user"
        if argv[0] in ("apt", "apt-get"):
            return f"{argv[0]} {argv[1]}"
        return argv[0]

    def __call__(self, argv, timeout):
        assert timeout <= (2730 if argv[0] == "sudo" else 10), (argv, timeout)
        self.calls.append(list(argv))
        v = self.r[self.key(argv)]
        if isinstance(v, Exception):
            raise v
        return v

    def sudo_calls(self):
        return [c for c in self.calls if c[0] == "sudo"]


@pytest.fixture
def paths(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("12345.67 99999.00\n")
    (proc / "meminfo").write_text("MemTotal:       32000000 kB\nMemFree: 1 kB\nMemAvailable:   24000000 kB\n")
    hm = tmp_path / "helix-machine"
    hm.write_text("#!/bin/bash\n")
    return dict(ledger=tmp_path / "MACHINE.jsonl", last_reboot=tmp_path / "last_reboot.json",
                reboot_required=tmp_path / "reboot-required", proc=proc, helix_machine=hm)


def go(paths, fake=None, **kw):
    fake = fake or Fake()
    rec, line = machine.check(run=fake, **paths, **kw)
    return rec, line, fake


def ledger(paths):
    return [json.loads(l) for l in paths["ledger"].read_text().splitlines()]


# ------------------------------------------------------------------ parsers on real output

def test_parse_smart_on_the_2026_10_18_maintenance_log():
    d = machine.parse_smart(SMART_20261018)
    assert d["sda"] == dict(overall="PASSED", realloc=189, pending=None, uncorrectable=None,
                            wear=None, power_on_hours=1239)
    assert d["sdb"] == dict(overall="PASSED", realloc=8, pending=0, uncorrectable=None,
                            wear=None, power_on_hours=15721)


def test_parse_smart_on_helix_machine_output_with_wear_and_failure():
    out = ("=== disk sda ===\nSMART overall-health self-assessment test result: FAILED!\n"
           "177 Wear_Leveling_Count     0x0013   093   093   000    Pre-fail  Always       -       71\n"
           "198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Offline      -       3\n"
           "=== exit sda 8 ===\n=== disk nvme0n1 ===\nSMART overall-health self-assessment test result: PASSED\n"
           "Percentage Used:                    4%\n")
    d = machine.parse_smart(out)
    assert d["sda"]["overall"] == "FAILED!" and d["sda"]["uncorrectable"] == 3
    assert d["sda"]["wear"] == "93 left (raw 71)" and d["nvme0n1"]["wear"] == "4% used"


def test_parse_the_user_level_readings():
    assert machine.parse_df(DF) == {"/": 44, "__BULK__": 13}
    assert machine.parse_sensors(SENSORS) == 57.0
    assert machine.parse_sensors("nothing here") is None
    assert machine.parse_nvidia(0, SMI_OK, "") == ("OK", False)
    assert machine.parse_nvidia(18, SMI_MISMATCH, "") == (
        "Failed to initialize NVML: Driver/library version mismatch", True)
    assert machine.parse_nvidia(None, "", "timed out after 10 s") == ("timed out after 10 s", False)
    assert machine.parse_failed_units(FAILED_UNIT + "x.socket loaded failed failed X\n") == [
        "sidekick.service", "x.socket"]
    assert machine.parse_upgradable(APT_LIST) == 1
    assert machine.parse_autoremovable("Remv linux-a [1]\nRemv linux-b [2]\nInst x\n") == 2
    assert machine.parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\n") == 75.0


# ------------------------------------------------------------------ the line

def test_all_fine_after_a_baseline(paths):
    go(paths)                                         # night 1: the baseline
    rec, line, fake = go(paths)                       # night 2: nothing moved
    assert line == "- machine: OK · SSD 44% · HDD 13% · CPU 57°C · GPU OK · 0 updates left · disks PASSED"
    assert [c[3] for c in fake.sudo_calls()] == ["health", "update", "clean"]
    assert rec["reboot_due"] is False and rec["reboot_reason"] is None


def test_the_first_run_records_a_baseline_and_flags_no_counts(paths):
    rec, line, _ = go(paths)
    assert line.startswith("- machine: OK · ") and line.endswith("disks PASSED · baseline recorded")
    [stored] = ledger(paths)
    assert stored["smart"]["sda"]["realloc"] == 189 and stored["smart"]["sdb"]["pending"] == 0
    for key in ("disk_pct", "cpu_c", "gpu", "failed_units", "reboot_required", "reboot_pkgs",
                "upgradable", "uptime_s", "ram_pct", "autoremovable", "reboot_due", "reboot_reason"):
        assert key in stored, key
    assert stored["uptime_s"] == 12345 and stored["ram_pct"] == 25.0


def test_a_reallocated_count_going_up_is_red_with_old_and_new(paths):
    go(paths)
    _, line, _ = go(paths, Fake(**{"sudo health": (0, health_out(190), "")}))
    assert "  - 🔴 disk sda: reallocated sectors went up, 189 → 190" in line.splitlines()
    assert line.startswith("- machine: 🔴 1 · ")


def test_a_count_staying_or_falling_is_not_flagged(paths):
    go(paths, Fake(**{"sudo health": (0, health_out(190), "")}))
    _, line, _ = go(paths, Fake(**{"sudo health": (0, health_out(189), "")}))
    assert "🔴" not in line


def test_counts_compare_with_the_last_run_that_read_smart(paths):
    go(paths)
    go(paths, Fake(**{"sudo health": (1, "", "sudo: a password is required\n")}))
    _, line, _ = go(paths, Fake(**{"sudo health": (0, health_out(191), "")}))
    assert "189 → 191" in line and "baseline" not in line


def test_sudo_refused_says_not_installed_and_tries_no_other_root_verb(paths):
    fake = Fake(**{"sudo health": (1, "", "sudo: a password is required\n")})
    rec, line, fake = go(paths, fake)
    assert [c[3] for c in fake.sudo_calls()] == ["health"]
    assert any(l.startswith("  - 🟡 root checks not installed") for l in line.splitlines())
    assert rec["root"] == "not installed" and "disks not checked" in line


def test_dry_never_calls_sudo_and_writes_nothing(paths):
    rec, line, fake = go(paths, dry=True)
    assert fake.sudo_calls() == []
    assert not paths["ledger"].exists()
    assert line.startswith("- machine: ") and "disks not checked" in line
    assert rec["root"] == "skipped (--dry)"


def test_dry_says_not_installed_when_the_program_is_absent(paths):
    paths["helix_machine"].unlink()
    rec, line, fake = go(paths, dry=True)
    assert fake.sudo_calls() == [] and rec["root"] == "not installed"
    assert "🟡 root checks not installed" in line


def test_a_reader_that_raises_gives_the_did_not_run_line(paths):
    line = machine.machine_line(run=Fake(sensors=RuntimeError("boom")), **paths)
    assert line == "- machine: 🔴 machine check did not run (RuntimeError: boom)"


def test_the_nightly_still_completes_when_machine_raises(monkeypatch):
    import nightly
    boom = types.SimpleNamespace(machine_line=lambda: (_ for _ in ()).throw(OSError("disk gone")))
    monkeypatch.setitem(sys.modules, "machine", boom)
    assert nightly.machine_line() == "- machine: 🔴 machine check did not run (OSError: disk gone)"


def test_positive_control_gpu_mismatch_makes_a_reboot_due(paths):
    rec, line, _ = go(paths, Fake(**{"nvidia-smi": (18, SMI_MISMATCH, "")}))
    assert rec["reboot_due"] is True and rec["reboot_reason"] == "GPU driver mismatch"
    lines = line.splitlines()
    assert "GPU FAILED" in lines[0]
    assert ("  - 🔴 GPU not working: driver/library version mismatch (a reboot fixes it)") in lines
    assert "  - 🟡 reboot due: GPU driver mismatch" in lines
    assert ledger(paths)[-1]["reboot_due"] is True


def test_reboot_required_with_packages(paths):
    paths["reboot_required"].write_text("*** System restart required ***\n")
    Path(f"{paths['reboot_required']}.pkgs").write_text("linux-image-6.8\nlinux-base\n")
    rec, line, _ = go(paths)
    assert rec["reboot_due"] and rec["reboot_reason"] == "reboot-required (linux-image-6.8, linux-base)"
    assert "  - 🟡 reboot due: reboot-required (linux-image-6.8, linux-base)" in line.splitlines()


def test_each_problem_in_plain_words(paths):
    df = DF.replace("44% /", "91% /").replace("13% __BULK__", "85% __BULK__")
    fake = Fake(df=(0, df, ""), sensors=(0, SENSORS.replace("+57.0", "+83.0"), ""),
                systemctl=(1, FAILED_UNIT, ""), **{"sudo update": (100, "", "E: broken"),
                                                   "apt-get -s": (0, "Remv a [1]\nRemv b [2]\n", ""),
                                                   "apt list": (0, APT_LIST, "")})
    _, line, _ = go(paths, fake)
    lines = line.splitlines()
    assert lines[0] == ("- machine: 🔴 2 · 🟡 4 · SSD 91% · HDD 85% · CPU 83°C · GPU OK · 1 update left"
                        " · disks PASSED · baseline recorded")
    assert lines[1:] == ["  - 🔴 SSD (/) is 91% full",
                         "  - 🔴 failed unit: sidekick.service (system)",
                         "  - 🟡 HDD (__BULK__) is 85% full",
                         "  - 🟡 CPU is hot: 83°C",
                         "  - 🟡 update failed (exit 100)",
                         "  - 🟡 2 packages no longer needed — the user: `sudo apt autoremove`"]


def test_update_busy_and_smart_failed(paths):
    out = health_out().replace("test result: PASSED", "test result: FAILED", 1)
    _, line, _ = go(paths, Fake(**{"sudo health": (0, out, ""), "sudo update": (3, "", "busy")}))
    assert "  - 🔴 disk sda: SMART says FAILED" in line.splitlines()
    assert "  - 🟡 update not run: another package install held the dpkg lock (exit 3)" in line.splitlines()
    assert "disks: sda FAILED" in line.splitlines()[0]


def test_rebooted_at_shows_once(paths):
    go(paths)
    paths["last_reboot"].write_text(json.dumps({"ts": "2099-01-01T04:41:00Z", "reason": "GPU driver mismatch"}))
    _, line, _ = go(paths)
    hhmm = machine._hhmm("2099-01-01T04:41:00Z")
    assert f"rebooted at {hhmm} (GPU driver mismatch)" in line.splitlines()[0]
    paths["last_reboot"].write_text(json.dumps({"ts": "2000-01-01T04:41:00Z", "reason": "old"}))
    _, line, _ = go(paths)
    assert "rebooted" not in line


def test_timeouts_stay_inside_fifty_minutes():
    reads = 11                                   # the user-level reads plus the re-count
    assert max(machine.READ_TIMEOUT, 0) <= 10
    assert sum(machine.ROOT_TIMEOUTS.values()) + reads * machine.READ_TIMEOUT <= 50 * 60


def test_machine_py_has_no_environment_switch():
    src = (HERE / "machine.py").read_text()
    for w in ("os.environ", "getenv", "HELIX_MACHINE_TEST"):
        assert w not in src, w


# ------------------------------------------------------------------ ORDER 68 Part B: USB disks

def test_usb_disks_without_smart_are_counted_in_the_head_not_warned(paths):
    go(paths)
    rec, line, fake = go(paths, Fake(**{"sudo health": (0, health_with_usb(), "")}))
    assert line == ("- machine: OK · SSD 44% · HDD 13% · CPU 57°C · GPU OK · 0 updates left"
                    " · disks PASSED · 2 USB not checked · baseline recorded")
    assert ["lsblk", "-dno", "NAME,TRAN"] in fake.calls
    stored = ledger(paths)[-1]
    assert {k: d["tran"] for k, d in stored["smart"].items()} == {
        "sda": "sata", "sdb": "sata", "sdc": "usb", "sdd": "usb"}
    assert stored["problems"] == []


def test_a_sata_disk_without_smart_stays_yellow(paths):
    lsblk = (0, LSBLK.replace("sdc    usb", "sdc    sata"), "")
    _, line, _ = go(paths, Fake(lsblk=lsblk, **{"sudo health": (0, health_with_usb(), "")}))
    lines = line.splitlines()
    assert lines[1:] == ["  - 🟡 disk sdc: no SMART health result"]
    assert "disks: sdc ? · 1 USB not checked" in lines[0]


def test_a_usb_disk_that_reports_smart_is_judged_like_sata(paths):
    sdc_failed = "SMART overall-health self-assessment test result: FAILED\n"
    rec, line, _ = go(paths, Fake(**{"sudo health": (0, health_with_usb(sdc=sdc_failed), "")}))
    assert "  - 🔴 disk sdc: SMART says FAILED" in line.splitlines()
    assert "disks: sdc FAILED · 1 USB not checked" in line.splitlines()[0]
    assert rec["smart"]["sdc"]["tran"] == "usb"


@pytest.mark.parametrize("lsblk", [(1, "", "lsblk: failed"), (None, "", "timed out after 10 s"),
                                   RuntimeError("lsblk exploded")])
def test_lsblk_failing_keeps_todays_behaviour(paths, lsblk):
    rec, line, _ = go(paths, Fake(lsblk=lsblk, **{"sudo health": (0, health_with_usb(), "")}))
    lines = line.splitlines()
    assert lines[1:] == ["  - 🟡 disk sdc: no SMART health result", "  - 🟡 disk sdd: no SMART health result"]
    assert "USB" not in lines[0] and "disks: sdc ?, sdd ?" in lines[0]
    assert rec["smart"]["sdc"]["tran"] is None


def test_dry_reads_no_transport(paths):
    _, _, fake = go(paths, dry=True)
    assert not [c for c in fake.calls if c[0] == "lsblk"]
