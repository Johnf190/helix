"""Tests for helix-vault-add / helix-vault-verify (ORDER 19). Run as the normal user, no root.

helix-vault-add has no path options, so each test runs a COPY of it with the log path, the three
ledger paths, the expected owner uid (and, in one test, the size cap) rewritten by string replace.
helix-vault-verify is run directly with --log / --root.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ADD = HERE / "helix-vault-add"
VERIFY = HERE / "helix-vault-verify"
PY = "/usr/bin/python3"

LOG_CONST = "/var/lib/helix-vault/vault.log"
LEDGERS = {
    "facts_ledger": "__HOME__/helix/workspace/factgate/state/FACTS_LEDGER.jsonl",
    "regate_ledger": "__HOME__/helix/workspace/seam/registry/REGATE_LEDGER.jsonl",
    "catches": "__HOME__/helix/workspace/catches/CATCHES.jsonl",
}
UID_CONST = "OWNER_UID = 1000"
CAP_CONST = "MAX_BYTES = 256 * 1024 * 1024"
DRIVE_CONST = "__VAULT_DRIVE__/helix-vault/vault.log"
DRIVE_DIR_CONST = "DRIVE_DIR = os.path.dirname(DRIVE_LOG)"
DRIVE_UID_CONST = "DRIVE_UID = 0"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def chained_lines(texts):
    """Ledger lines with their own internal hash chain, like the real ledgers."""
    out, prev = [], "genesis"
    for i, t in enumerate(texts, 1):
        line = json.dumps({"i": i, "prev": prev, "text": t}, sort_keys=True) + "\n"
        out.append(line.encode())
        prev = sha(line.encode())
    return out


class Vault:
    def __init__(self, tmp, uid=None, cap=None, drive_uid=None):
        self.tmp = Path(os.path.realpath(tmp))
        self.root = self.tmp / "root"
        self.log = self.tmp / "var" / "vault.log"
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(b"")
        # the drive copy, as the user's install leaves it: its own folder, the file present and empty
        self.drive_dir = self.tmp / "odin" / "helix-vault"
        self.drive_dir.mkdir(parents=True)
        self.drive_dir.chmod(0o755)
        self.drive = self.drive_dir / "vault.log"
        self.drive.write_bytes(b"")
        self.drive.chmod(0o644)
        self.paths = {}
        for name, p in LEDGERS.items():
            lp = Path(str(self.root) + p)
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_bytes(b"".join(chained_lines([f"{name} line {k}" for k in range(1, 6)])))
            self.paths[name] = lp
        src = ADD.read_text()
        for const in [LOG_CONST, UID_CONST, CAP_CONST, DRIVE_CONST, DRIVE_DIR_CONST, DRIVE_UID_CONST] \
                + list(LEDGERS.values()):
            assert src.count(const) == 1, f"constant {const!r} must appear exactly once"
        src = src.replace(LOG_CONST, str(self.log))
        src = src.replace(DRIVE_CONST, str(self.drive))
        drive_uid = os.getuid() if drive_uid is None else drive_uid
        src = src.replace(DRIVE_UID_CONST, f"DRIVE_UID = {drive_uid}")
        for p in LEDGERS.values():
            src = src.replace(p, str(self.root) + p)
        src = src.replace(UID_CONST, f"OWNER_UID = {os.getuid() if uid is None else uid}")
        if cap is not None:
            src = src.replace(CAP_CONST, f"MAX_BYTES = {cap}")
        self.add_copy = self.tmp / "helix-vault-add"
        self.add_copy.write_text(src)
        vsrc = VERIFY.read_text()
        for const in [DRIVE_CONST, DRIVE_UID_CONST]:
            assert vsrc.count(const) == 1, f"verify: constant {const!r} must appear exactly once"
        self.verify_copy = self.tmp / "helix-vault-verify"
        self.verify_copy.write_text(vsrc.replace(DRIVE_UID_CONST, f"DRIVE_UID = {drive_uid}"))

    def add(self, *names):
        return subprocess.run([PY, "-I", str(self.add_copy), *names], capture_output=True, text=True)

    def verify(self, *extra, drive=True):
        args = ["--drive-log", str(self.drive)] if drive else ["--no-drive"]
        return subprocess.run([PY, "-I", str(self.verify_copy), "--log", str(self.log), "--root", str(self.root),
                               *args, *extra], capture_output=True, text=True, timeout=30)

    def lines(self):
        return [json.loads(l) for l in self.log.read_bytes().splitlines()]


@pytest.fixture
def v(tmp_path):
    return Vault(tmp_path)


def assert_rejected(v, res, code):
    assert res.returncode == code, (res.returncode, res.stdout, res.stderr)
    assert res.stderr.strip(), "a one-line reason on stderr"
    assert res.stdout == ""


# ---------- valid adds ----------

def test_add_one_name_genesis(v):
    r = v.add("catches")
    assert r.returncode == 0, r.stderr
    raw = v.log.read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    rec = v.lines()[0]
    data = v.paths["catches"].read_bytes()
    assert rec["seq"] == 1 and rec["prev"] == "genesis"
    assert rec["entries"] == {"catches": {"n": 5, "sha256": sha(data)}}
    assert rec["utc"].endswith("Z")
    assert r.stdout.splitlines()[0] == raw.decode().strip()
    # sorted keys, no spaces
    assert raw.decode().strip() == json.dumps(rec, sort_keys=True, separators=(",", ":"))


def test_add_several_and_none_means_all(v):
    assert v.add("catches", "facts_ledger").returncode == 0
    assert v.add().returncode == 0
    a, b = v.lines()
    assert set(a["entries"]) == {"catches", "facts_ledger"}
    assert set(b["entries"]) == set(LEDGERS)
    assert b["seq"] == 2
    assert b["prev"] == sha(v.log.read_bytes().splitlines(keepends=True)[0])


def test_honest_growth_accepted_and_partial_line_ignored(v):
    p = v.paths["catches"]
    assert v.add("catches").returncode == 0
    with open(p, "ab") as f:
        f.write(b'{"i":6}\n{"i":7}\n{"partial')
    r = v.add("catches")
    assert r.returncode == 0, r.stderr
    e = v.lines()[1]["entries"]["catches"]
    full = p.read_bytes()
    assert e["n"] == 7
    assert e["sha256"] == sha(full[: full.rfind(b"\n") + 1])
    assert v.verify().returncode == 0


# ---------- rejects, log byte-identical ----------

@pytest.mark.parametrize("args", [
    ["nosuch"], ["catches", "catches"], ["catches:5:" + "ab" * 32], ["--log", "/tmp/x"],
    ["catches/x"], ["../catches"], [".."], ["catches=1"], ["cat ches"], ["catches "], ["catches\n"],
    ["-catches"], ["-h"], [""], ["catches", "nosuch"], ["__HOME__/helix/workspace/catches/CATCHES.jsonl"],
])
def test_bad_args_rejected_nothing_written(v, args):
    assert v.add("catches").returncode == 0
    before = v.log.read_bytes()
    assert_rejected(v, v.add(*args), 2)
    assert v.log.read_bytes() == before


# ---------- the laundering attack ----------

def rewrite_early_line_and_rebuild(p, extra=0):
    texts = [json.loads(l)["text"] for l in p.read_bytes().splitlines()]
    texts[1] = "FORGED — this line said something else"
    texts += [f"new {k}" for k in range(extra)]
    p.write_bytes(b"".join(chained_lines(texts)))


def test_laundering_attack_refused_and_verify_says_rewritten(v):
    p = v.paths["catches"]
    assert v.add("catches").returncode == 0
    rewrite_early_line_and_rebuild(p, extra=2)
    # the ledger's own chain is self-consistent after the rebuild
    prev = "genesis"
    for l in p.read_bytes().splitlines(keepends=True):
        assert json.loads(l)["prev"] == prev
        prev = sha(l)
    before = v.log.read_bytes()
    r = v.add("catches")
    assert_rejected(v, r, 4)
    assert "REWRITTEN since seq 1" in r.stderr
    assert v.log.read_bytes() == before
    # all or nothing: an honest name in the same call is not anchored either
    assert_rejected(v, v.add("facts_ledger", "catches"), 4)
    assert v.log.read_bytes() == before
    vr = v.verify()
    assert vr.returncode != 0
    assert "catches: REWRITTEN (anchor seq 1, 5 lines: live prefix differs)" in vr.stdout
    assert "chain OK (seq 1..1)" in vr.stdout


def test_laundering_after_several_anchors_verify_reports_first(v):
    p = v.paths["catches"]
    assert v.add("catches").returncode == 0
    texts = [json.loads(l)["text"] for l in p.read_bytes().splitlines()]
    p.write_bytes(b"".join(chained_lines(texts + ["honest 6"])))  # honest growth: old lines unchanged
    assert v.add("catches").returncode == 0
    rewrite_early_line_and_rebuild(p, extra=0)
    r = v.add("catches")
    assert_rejected(v, r, 4)
    assert "REWRITTEN since seq 2" in r.stderr
    assert "catches: REWRITTEN (anchor seq 1, 5 lines" in v.verify().stdout


def test_forged_log_verify_walks_every_anchor(v):
    p = v.paths["catches"]
    orig = p.read_bytes()
    rewrite_early_line_and_rebuild(p, extra=1)
    new = p.read_bytes()
    recs, prev = [], "genesis"
    for seq, (n, h) in enumerate([(5, sha(orig)), (6, sha(new))], 1):
        line = json.dumps({"entries": {"catches": {"n": n, "sha256": h}}, "prev": prev, "seq": seq,
                           "utc": "2026-09-30T00:00:00Z"}, sort_keys=True, separators=(",", ":")) + "\n"
        recs.append(line.encode())
        prev = sha(line.encode())
    v.log.write_bytes(b"".join(recs))
    vr = v.verify()
    assert "chain OK (seq 1..2)" in vr.stdout
    assert "catches: REWRITTEN (anchor seq 1, 5 lines: live prefix differs)" in vr.stdout
    assert vr.returncode != 0


# ---------- shrunk ----------

def test_truncated_ledger_shrunk(v):
    p = v.paths["catches"]
    assert v.add("catches").returncode == 0
    p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:3]))
    before = v.log.read_bytes()
    r = v.add("catches")
    assert_rejected(v, r, 4)
    assert "SHRUNK since seq 1" in r.stderr
    assert v.log.read_bytes() == before
    vr = v.verify()
    assert "catches: SHRUNK (anchor seq 1 wants 5 lines, live has 3)" in vr.stdout
    assert vr.returncode != 0


# ---------- unsafe ledger files ----------

def test_ledger_symlink_refused(v):
    p = v.paths["catches"]
    real = v.tmp / "elsewhere.jsonl"
    real.write_bytes(p.read_bytes())
    p.unlink()
    p.symlink_to(real)
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_parent_dir_symlink_refused(v):
    p = v.paths["catches"]
    d = p.parent
    moved = v.tmp / "moved_dir"
    d.rename(moved)
    d.symlink_to(moved)
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_directory_in_place_of_file_refused(v):
    p = v.paths["catches"]
    p.unlink()
    p.mkdir()
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_fifo_in_place_of_file_refused_without_hanging(v):
    p = v.paths["catches"]
    p.unlink()
    os.mkfifo(p)
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_missing_ledger_refused(v):
    v.paths["catches"].unlink()
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_wrong_owner_refused(tmp_path):
    v = Vault(tmp_path, uid=os.getuid() + 1)
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_size_cap_refused(tmp_path):
    v = Vault(tmp_path, cap=50)
    assert_rejected(v, v.add("catches"), 5)
    assert v.log.read_bytes() == b""


def test_unsafe_one_name_writes_nothing_for_others(v):
    v.paths["catches"].unlink()
    assert_rejected(v, v.add(), 5)
    assert v.log.read_bytes() == b""


# ---------- corrupted log ----------

@pytest.mark.parametrize("which", ["last", "middle"])
def test_corrupted_log_refused(v, which):
    for _ in range(3):
        assert v.add("catches").returncode == 0
    lines = v.log.read_bytes().splitlines(keepends=True)
    if which == "last":
        lines[2] = lines[2][:30] + b"\n"  # cut short: no longer valid JSON
    else:
        lines[1] = lines[1].replace(b'"n":5', b'"n":4')  # valid JSON, but line 3's prev no longer matches
    v.log.write_bytes(b"".join(lines))
    before = v.log.read_bytes()
    assert_rejected(v, v.add("catches"), 3)
    assert v.log.read_bytes() == before


@pytest.mark.parametrize("bad", [b"not json\n", b'{"seq":2}', b"[1,2]\n"])
def test_garbage_log_line_refused(v, bad):
    assert v.add("catches").returncode == 0
    with open(v.log, "ab") as f:
        f.write(bad)
    before = v.log.read_bytes()
    assert_rejected(v, v.add("catches"), 3)
    assert v.log.read_bytes() == before


def test_missing_log_refused(v):
    v.log.unlink()
    assert_rejected(v, v.add("catches"), 3)
    assert not v.log.exists()


# ---------- concurrency ----------

def test_twenty_concurrent_adds(v):
    procs = [subprocess.Popen([PY, "-I", str(v.add_copy)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(20)]
    codes = [p.wait(timeout=60) for p in procs]
    assert codes == [0] * 20
    recs = v.lines()
    assert [r["seq"] for r in recs] == list(range(1, 21))
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert "chain OK (seq 1..20)" in vr.stdout


# ---------- verify ----------

def test_verify_clean(v):
    assert v.add().returncode == 0
    assert v.add("catches").returncode == 0
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert "chain OK (seq 1..2)" in vr.stdout
    assert "catches: OK (2 anchors, latest seq 2, 5 lines)" in vr.stdout
    assert "facts_ledger: OK (1 anchors, latest seq 1, 5 lines)" in vr.stdout


def test_verify_not_anchored_does_not_fail(v):
    assert v.add("catches").returncode == 0
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert "facts_ledger: NOT ANCHORED" in vr.stdout
    assert "regate_ledger: NOT ANCHORED" in vr.stdout


def test_verify_empty_log(v):
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert vr.stdout.count("NOT ANCHORED") == 3


def test_verify_missing_ledger(v):
    assert v.add("catches").returncode == 0
    v.paths["catches"].unlink()
    vr = v.verify()
    assert "catches: MISSING" in vr.stdout
    assert vr.returncode != 0


def test_verify_earlier_log_line_edited(v):
    for _ in range(3):
        assert v.add("catches").returncode == 0
    lines = v.log.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b'"utc":"2', b'"utc":"1')
    v.log.write_bytes(b"".join(lines))
    vr = v.verify()
    assert vr.returncode != 0
    assert "chain BROKEN at seq 2" in vr.stdout
    # the ledger comparison is still run
    assert "catches: OK (3 anchors" in vr.stdout


def test_verify_broken_chain_and_rewritten_both_reported(v):
    assert v.add("catches").returncode == 0
    assert v.add("catches").returncode == 0
    lines = v.log.read_bytes().splitlines(keepends=True)
    lines[1] = lines[1].replace(b'"seq":2', b'"seq":7')
    v.log.write_bytes(b"".join(lines))
    rewrite_early_line_and_rebuild(v.paths["catches"])
    vr = v.verify()
    assert "chain BROKEN at seq 2" in vr.stdout
    assert "catches: REWRITTEN (anchor seq 1" in vr.stdout


def test_verify_missing_log_fails(v):
    v.log.unlink()
    assert v.verify().returncode != 0


# ---------- source hygiene ----------

FORBIDDEN = ["sys.stdin", "os.environ", "getenv", "/proc/self/environ", "argparse", "truncate",
             "os.remove", "os.unlink", "os.rename", "os.replace", "shutil", "subprocess"]


@pytest.mark.parametrize("word", FORBIDDEN)
def test_add_source_has_no_forbidden(word):
    assert word not in ADD.read_text()


def test_add_shebang_and_length():
    src = ADD.read_text()
    assert src.startswith("#!/usr/bin/python3 -I\n")
    assert len(src.splitlines()) <= 210
    assert "O_APPEND" in src and "O_NOFOLLOW" in src and "fsync" in src


# ---------- ORDER 19b ----------

INSTALL = HERE / "INSTALL_FOR_USER.sh"


def test_install_uses_staged_copies_only():
    lines = INSTALL.read_text().splitlines()
    mk = [i for i, l in enumerate(lines) if l.strip().startswith('STAGE="$(mktemp -d)"')]
    assert len(mk) == 1, "exactly one STAGE=\"$(mktemp -d)\" line"
    body = lines[mk[0] + 1:]
    assert any(l.strip() == """trap 'rm -rf "$STAGE"' EXIT""" for l in body)
    step1 = [i for i, l in enumerate(lines) if "Step 1 of " in l]
    assert step1 and mk[0] < step1[0], "staging happens before step 1"
    for prog in ("helix-vault-add", "helix-vault-verify"):
        refs = [l.strip() for l in body if f"$HERE/{prog}" in l]
        assert refs, f"$HERE/{prog} is copied into $STAGE"
        assert all(l.startswith("cp ") and '"$STAGE' in l for l in refs), refs
    sums = [l for l in body if "sha256sum" in l]
    assert sums and all('"$STAGE/' in l and "$HERE" not in l for l in sums), sums
    inst = [l for l in body if l.strip().startswith("install ") and ("$ADD_DST" in l or "$VERIFY_DST" in l)]
    assert len(inst) == 2 and all('"$STAGE/' in l and "$HERE" not in l for l in inst), inst


def _anchor_then_replace(v, make):
    assert v.add("catches").returncode == 0
    p = v.paths["catches"]
    p.unlink()
    make(p)
    return subprocess.run([PY, "-I", str(VERIFY), "--log", str(v.log), "--root", str(v.root)],
                          capture_output=True, text=True, timeout=5)


def test_verify_fifo_ledger_unsafe_without_hanging(v):
    vr = _anchor_then_replace(v, os.mkfifo)
    assert "catches: UNSAFE (not a regular file)" in vr.stdout, vr.stdout
    assert vr.returncode == 1


def test_verify_symlink_ledger_not_ok(v, tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(v.paths["catches"].read_bytes())
    vr = _anchor_then_replace(v, lambda p: p.symlink_to(target))
    assert "catches: OK" not in vr.stdout, vr.stdout
    assert vr.returncode == 1


def test_verify_never_sees_half_written_line_during_concurrent_adds(v):
    procs = [subprocess.Popen([PY, "-I", str(v.add_copy)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(20)]
    runs, bad = 0, []
    while runs < 50 or any(p.poll() is None for p in procs):
        vr = v.verify()
        runs += 1
        if "chain BROKEN" in vr.stdout:
            bad.append(vr.stdout)
    assert [p.wait(timeout=60) for p in procs] == [0] * 20
    assert runs >= 50
    assert bad == [], bad[:3]


# ---------- ORDER 20: the drive copy ----------

def stdout_lines(r):
    return r.stdout.splitlines()


def assert_drive(r, code, status):
    assert r.returncode == code, (r.returncode, r.stdout, r.stderr)
    out = stdout_lines(r)
    assert len(out) == 2, out
    json.loads(out[0])  # the anchor line comes first, as before
    assert out[1] == f"drive: {status}", out


def test_drive_ok_identical_after_each_add(v):
    r = v.add()
    assert_drive(r, 0, "OK (1 lines)")
    assert r.stderr == ""
    assert v.drive.read_bytes() == v.log.read_bytes()
    assert_drive(v.add("catches"), 0, "OK (2 lines)")
    assert v.drive.read_bytes() == v.log.read_bytes()


def test_drive_catch_up_appends_the_tail(v):
    for _ in range(3):
        assert v.add().returncode == 0
    local = v.log.read_bytes()
    v.drive.write_bytes(local.splitlines(keepends=True)[0])  # the drive missed seq 2 and 3
    r = v.add()
    assert_drive(r, 0, "CAUGHT UP (+2 lines, 4)")
    assert v.drive.read_bytes() == v.log.read_bytes()
    assert v.log.read_bytes().startswith(local)


def test_drive_catch_up_from_a_half_written_line(v):
    for _ in range(2):
        assert v.add().returncode == 0
    local = v.log.read_bytes()
    v.drive.write_bytes(local[:-20])  # a crash mid-copy: still a byte prefix
    assert v.add().returncode == 0
    assert v.drive.read_bytes() == v.log.read_bytes()


def assert_local_only(v, r, status, before_local):
    assert_drive(r, 6, status)
    assert r.stderr.count("\n") == 1 and "drive copy not updated" in r.stderr, r.stderr
    after = v.log.read_bytes()
    assert after.startswith(before_local) and after.count(b"\n") == before_local.count(b"\n") + 1
    assert v.lines()[-1]["seq"] == after.count(b"\n")


def test_drive_missing_dir(v):
    assert v.add().returncode == 0
    before = v.log.read_bytes()
    v.drive.unlink()
    v.drive_dir.rmdir()
    assert_local_only(v, v.add(), "MISSING", before)
    assert not v.drive_dir.exists(), "the drive step creates nothing"


def test_drive_missing_file(v):
    assert v.add().returncode == 0
    before = v.log.read_bytes()
    v.drive.unlink()
    assert_local_only(v, v.add(), "MISSING", before)
    assert os.listdir(v.drive_dir) == [], "the drive step creates nothing"


def test_drive_renamed_then_back_catches_up(v):
    """Odin's live check (d): rename the folder, add, rename back, add."""
    assert v.add().returncode == 0
    moved = v.drive_dir.parent / "hv_x"
    v.drive_dir.rename(moved)
    before = v.log.read_bytes()
    assert_local_only(v, v.add(), "MISSING", before)
    moved.rename(v.drive_dir)
    assert_drive(v.add(), 0, "CAUGHT UP (+1 lines, 3)")
    assert v.drive.read_bytes() == v.log.read_bytes()


def _unsafe_case(v, how, tmp_path):
    """Returns the path whose bytes must not change."""
    if how == "group-writable dir":
        v.drive_dir.chmod(0o775)
        return v.drive
    if how == "world-writable dir":
        v.drive_dir.chmod(0o757)
        return v.drive
    if how == "file is a symlink":
        real = tmp_path / "planted.log"
        real.write_bytes(v.drive.read_bytes())
        v.drive.unlink()
        v.drive.symlink_to(real)
        return real
    if how == "dir is a symlink":
        moved = v.drive_dir.parent / "real_dir"
        v.drive_dir.rename(moved)
        v.drive_dir.symlink_to(moved)
        return moved / "vault.log"
    if how == "file is a directory":
        v.drive.unlink()
        v.drive.mkdir()
        return None
    if how == "file is a fifo":
        v.drive.unlink()
        os.mkfifo(v.drive)
        return None
    if how == "group-writable file":
        v.drive.chmod(0o664)
        return v.drive
    raise AssertionError(how)


UNSAFE_CASES = ["group-writable dir", "world-writable dir", "file is a symlink", "dir is a symlink",
                "file is a directory", "file is a fifo", "group-writable file"]


@pytest.mark.parametrize("how", UNSAFE_CASES)
def test_drive_unsafe(v, tmp_path, how):
    assert v.add().returncode == 0
    before = v.log.read_bytes()
    watched = _unsafe_case(v, how, tmp_path)
    watched_before = watched.read_bytes() if watched else None
    r = v.add()
    assert r.returncode == 6, (r.stdout, r.stderr)
    assert stdout_lines(r)[1].startswith("drive: UNSAFE ("), r.stdout
    assert_local_only(v, r, stdout_lines(r)[1][len("drive: "):], before)
    if watched:
        assert watched.read_bytes() == watched_before


def test_drive_unsafe_wrong_owner(tmp_path):
    v = Vault(tmp_path, drive_uid=os.getuid() + 1)
    r = v.add()
    assert r.returncode == 6
    assert stdout_lines(r)[1].startswith("drive: UNSAFE (") and "uid" in r.stdout, r.stdout
    assert v.drive.read_bytes() == b""
    assert v.log.read_bytes().count(b"\n") == 1


def test_drive_diverged_never_repaired(v):
    for _ in range(2):
        assert v.add().returncode == 0
    d = bytearray(v.drive.read_bytes())
    i = d.index(b'"seq":1')
    d[i + 6] = ord("7")  # one byte changed in line 1
    v.drive.write_bytes(bytes(d))
    drive_before = v.drive.read_bytes()
    before = v.log.read_bytes()
    assert_local_only(v, v.add(), "DIVERGED at line 1", before)
    assert v.drive.read_bytes() == drive_before


def test_drive_longer_than_local_is_diverged(v):
    assert v.add().returncode == 0
    v.drive.write_bytes(v.log.read_bytes() + b'{"extra":1}\n{"extra":2}\n{"extra":3}\n')
    drive_before = v.drive.read_bytes()
    r = v.add()
    assert_drive(r, 6, "DIVERGED at line 2")
    assert v.drive.read_bytes() == drive_before


def test_refusal_before_local_append_never_touches_drive(v):
    assert v.add().returncode == 0
    snap = (v.drive.read_bytes(), os.stat(v.drive).st_mtime_ns)
    assert_rejected(v, v.add("nosuch"), 2)
    rewrite_early_line_and_rebuild(v.paths["catches"], extra=1)
    assert_rejected(v, v.add("catches"), 4)
    assert (v.drive.read_bytes(), os.stat(v.drive).st_mtime_ns) == snap


def test_twenty_concurrent_adds_with_drive(v):
    procs = [subprocess.Popen([PY, "-I", str(v.add_copy)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(20)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0] * 20, outs
    assert all(o[0].decode().splitlines()[1].startswith("drive: OK (") for o in outs), outs
    assert v.log.read_bytes().count(b"\n") == 20
    assert v.drive.read_bytes() == v.log.read_bytes()
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert "chain OK (seq 1..20)" in vr.stdout
    assert "drive copy: OK (20 lines, identical)" in vr.stdout


# ---------- ORDER 20: verify's drive section ----------

def test_verify_drive_identical(v):
    for _ in range(2):
        assert v.add().returncode == 0
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert vr.stdout.splitlines()[-1] == "drive copy: OK (2 lines, identical)"


def test_verify_drive_behind_is_ok(v):
    for _ in range(3):
        assert v.add().returncode == 0
    v.drive.write_bytes(v.log.read_bytes().splitlines(keepends=True)[0])
    vr = v.verify()
    assert vr.returncode == 0, vr.stdout
    assert "drive copy: BEHIND (2 lines)" in vr.stdout


@pytest.mark.parametrize("how", ["no file", "no dir"])
def test_verify_drive_missing_fails(v, how):
    assert v.add().returncode == 0
    v.drive.unlink()
    if how == "no dir":
        v.drive_dir.rmdir()
    vr = v.verify()
    assert vr.returncode == 1
    assert "drive copy: MISSING" in vr.stdout


def test_verify_drive_diverged_fails(v):
    for _ in range(2):
        assert v.add().returncode == 0
    v.drive.write_bytes(v.drive.read_bytes().replace(b'"seq":2', b'"seq":9'))
    vr = v.verify()
    assert vr.returncode == 1
    assert "drive copy: DIVERGED at line 2" in vr.stdout


@pytest.mark.parametrize("how", UNSAFE_CASES)
def test_verify_drive_unsafe_fails(v, tmp_path, how):
    assert v.add().returncode == 0
    _unsafe_case(v, how, tmp_path)
    vr = v.verify()
    assert vr.returncode == 1
    assert "drive copy: UNSAFE (" in vr.stdout, vr.stdout


def test_verify_drive_wrong_owner_fails(tmp_path):
    v = Vault(tmp_path)
    assert v.add().returncode == 0
    vsrc = VERIFY.read_text().replace(DRIVE_UID_CONST, f"DRIVE_UID = {os.getuid() + 1}")
    v.verify_copy.write_text(vsrc)
    vr = v.verify()
    assert vr.returncode == 1
    assert "drive copy: UNSAFE (" in vr.stdout and "uid" in vr.stdout


def test_verify_no_drive_ignores_it(v):
    assert v.add().returncode == 0
    v.drive.write_bytes(b"garbage\n")
    vr = v.verify(drive=False)
    assert vr.returncode == 0, vr.stdout
    assert "drive" not in vr.stdout


def test_verify_drive_copy_on_its_own(v):
    for _ in range(3):
        assert v.add().returncode == 0
    vr = subprocess.run([PY, "-I", str(v.verify_copy), "--log", str(v.drive), "--root", str(v.root), "--no-drive"],
                        capture_output=True, text=True, timeout=30)
    assert vr.returncode == 0, vr.stdout
    assert "chain OK (seq 1..3)" in vr.stdout
    assert "catches: OK (3 anchors" in vr.stdout


def test_verify_real_default_path_is_the_drive():
    src = VERIFY.read_text()
    assert f'DRIVE_LOG = "{DRIVE_CONST}"' in src
    assert DRIVE_UID_CONST in src


# ---------- ORDER 20: the drive step creates nothing ----------

@pytest.mark.parametrize("prog", [ADD, VERIFY])
def test_drive_code_creates_nothing(prog):
    import re
    src = prog.read_text()
    for word in ["mkdir", "makedirs", "O_CREAT", "O_TRUNC", "os.mknod", "os.symlink", "os.link", "os.chmod", "os.chown"]:
        assert word not in src, word
    assert not re.search(r"""open\([^)]*["'][wx+]""", src), "no open(..., 'w'/'x'/'+')"
    assert f'DRIVE_LOG = "{DRIVE_CONST}"' in src


def test_add_real_constants():
    src = ADD.read_text()
    assert f'DRIVE_LOG = "{DRIVE_CONST}"' in src
    assert DRIVE_DIR_CONST in src and DRIVE_UID_CONST in src
    doc_exit = [l for l in src.split('"""')[1].splitlines() if l.startswith("Exit:")]
    assert doc_exit and "6 anchored locally, drive copy not updated" in src.split('"""')[1]


# ---------- ORDER 20: nightly.py's vault_line() ----------

NIGHTLY = Path("__HOME__/helix/workspace/seam/nightly.py")

STUB = r'''
import sys, time
who, mode = sys.argv[1], sys.argv[2]
ANCHOR = '{"entries":{},"prev":"genesis","seq":7,"utc":"2026-10-01T03:30:00Z"}'
if mode == "sleep":
    time.sleep(10)
elif who == "add" and mode == "ok":
    print(ANCHOR); print("drive: OK (7 lines)")
elif who == "add" and mode == "caughtup":
    print(ANCHOR); print("drive: CAUGHT UP (+2 lines, 7)")
elif who == "add" and mode == "e6":
    print(ANCHOR); print("drive: MISSING")
    print("helix-vault-add: anchored locally as seq 7; drive copy not updated: MISSING", file=sys.stderr)
    sys.exit(6)
elif who == "add" and mode == "e4":
    print("helix-vault-add: catches: REWRITTEN since seq 1 (first 5 lines differ)", file=sys.stderr)
    print("second stderr line", file=sys.stderr)
    sys.exit(4)
elif who == "verify" and mode == "ok":
    print("chain OK (seq 1..7)"); print("facts_ledger: OK (7 anchors, latest seq 7, 9 lines)")
    print("regate_ledger: NOT ANCHORED"); print("drive copy: BEHIND (1 lines)")
elif who == "verify" and mode == "missing":
    print("chain OK (seq 1..7)"); print("facts_ledger: OK (7 anchors, latest seq 7, 9 lines)")
    print("drive copy: MISSING"); sys.exit(1)
elif who == "verify" and mode == "rewritten":
    print("chain OK (seq 1..7)"); print("catches: REWRITTEN (anchor seq 1, 5 lines: live prefix differs)")
    print("drive copy: OK (7 lines, identical)"); sys.exit(1)
'''


def load_vault_line(tmp_path, add_mode, verify_mode, timeout=60):
    """Pull vault_line() and its constants out of nightly.py without importing nightly (no side effects)."""
    import ast
    import re
    src = NIGHTLY.read_text()
    tree = ast.parse(src)
    consts = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id in ("VAULT_ADD", "VAULT_VERIFY", "VAULT_TIMEOUT")}
    assert consts == {"VAULT_ADD": ["sudo", "-n", "/usr/local/sbin/helix-vault-add"],
                      "VAULT_VERIFY": ["/usr/local/bin/helix-vault-verify"], "VAULT_TIMEOUT": 60}
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vault_line"]
    assert len(fn) == 1
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    ns = {"subprocess": subprocess, "json": json, "re": re,
          "VAULT_ADD": [PY, str(stub), "add", add_mode], "VAULT_VERIFY": [PY, str(stub), "verify", verify_mode],
          "VAULT_TIMEOUT": timeout}
    exec(compile(ast.Module(body=fn, type_ignores=[]), str(NIGHTLY), "exec"), ns)
    return ns["vault_line"]


@pytest.mark.parametrize("add_mode,verify_mode,want", [
    ("ok", "ok", "- vault: seq 7 anchored · drive OK (7 lines) · verify OK"),
    ("caughtup", "ok", "- vault: seq 7 anchored · drive CAUGHT UP (+2 lines, 7) · verify OK"),
    ("e6", "missing", "- vault: 🔴 add exit 6: helix-vault-add: anchored locally as seq 7; drive copy not updated: "
                      "MISSING · verify FAIL: drive copy: MISSING"),
    ("e4", "rewritten", "- vault: 🔴 add exit 4: helix-vault-add: catches: REWRITTEN since seq 1 "
                        "(first 5 lines differ) · verify FAIL: catches: REWRITTEN (anchor seq 1, 5 lines: "
                        "live prefix differs)"),
    ("ok", "rewritten", "- vault: seq 7 anchored · drive OK (7 lines) · verify FAIL: catches: REWRITTEN "
                        "(anchor seq 1, 5 lines: live prefix differs)"),
])
def test_nightly_vault_line(tmp_path, add_mode, verify_mode, want):
    assert load_vault_line(tmp_path, add_mode, verify_mode)() == want


def test_nightly_vault_line_add_timeout_verify_still_runs(tmp_path):
    line = load_vault_line(tmp_path, "sleep", "ok", timeout=1)()
    assert line == "- vault: 🔴 add timed out after 1 s · verify OK"


def test_nightly_vault_line_verify_timeout(tmp_path):
    line = load_vault_line(tmp_path, "ok", "sleep", timeout=1)()
    assert line == "- vault: seq 7 anchored · drive OK (7 lines) · verify FAIL: timed out after 1 s"


def test_nightly_vault_line_not_installed_never_raises(tmp_path):
    f = load_vault_line(tmp_path, "ok", "ok")
    f.__globals__["VAULT_ADD"] = [str(tmp_path / "no-such-program")]
    f.__globals__["VAULT_VERIFY"] = [str(tmp_path / "no-such-program")]
    line = f()
    assert line.startswith("- vault: 🔴 add could not run: ") and " · verify FAIL: could not run: " in line


def _vault_order_ok(src):
    # facts_line() once, vault_line() after it and before heartbeat_line(); only other *_line() calls in the gap
    if src.count("facts_line(),") != 1:
        return False
    f = src.index("facts_line(),")
    v = src.find("vault_line(),", f)
    h = src.find("heartbeat_line(", f)
    if v < 0 or h < 0 or v > h:
        return False
    gap = src[f + len("facts_line(),"):v].split("\n")
    if gap[0] != "" or gap[-1].strip() != "":
        return False
    return all(re.fullmatch(r"\s+[a-z_]+_line\(\),", l) for l in gap[1:-1] if l.strip())


def test_nightly_calls_vault_line_once_after_facts_line():
    src = NIGHTLY.read_text()
    assert src.count("def vault_line()") == 1 and src.count("vault_line()") == 2  # defined once, called once
    assert _vault_order_ok(src)
    assert "shell=True" not in src
    # negative controls, in memory only: (a) vault after heartbeat, (b) raw code in the gap
    vault = "             vault_line(),\n"
    hb = "             heartbeat_line(HERE / \"HEARTBEAT.jsonl\"),\n"
    moved = src.replace(vault, "", 1).replace(hb, hb + vault, 1)
    assert moved != src and not _vault_order_ok(moved)
    raw = src.replace(vault, "             x = 1,\n" + vault, 1)
    assert raw != src and not _vault_order_ok(raw)
