#!/usr/bin/env python3
"""ORDER 26 — reportcheck: a build report's checkable claims against witnesses it did not write.

Every test builds its own scratch git repo and files under tmp_path; nothing here reads or
writes the live build_orders, regate or memory. The allowed-cwd root for re-runs is passed in
as `root=` (the CLI always uses ~/helix), so no environment variable can widen it.

Run:  python3 -m pytest -q test_reportcheck.py
"""
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reportcheck as rc  # noqa: E402

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, env={**GIT_ENV, **(env or {})}).stdout.strip()


def commit(repo, files, msg="c", env=None):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg, env=env)
    return git(repo, "rev-parse", "HEAD")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def record(**kv):
    now = time.time()
    rec = {"order": "99", "started": rc.iso(now - 120), "ended": rc.iso(now + 120),
           "exit": 0, "report_exists": True}
    rec.update(kv)
    return rec


class World:
    """A repo with two commits, a file outside git, a passing test file, and a report."""

    def __init__(self, root):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.c1 = commit(self.repo, {"a.py": "A = 1\n", "test_a.py":
                                     "def test_one():\n    assert 1\n\ndef test_two():\n    assert 2\n"})
        self.c2 = commit(self.repo, {"b.py": "B = 2\n", "a.py": "A = 2\n"})
        self.loose = root / "loose.sh"
        self.loose.write_text("echo hi\n")
        self.report = root / "reports" / "ORDER_99_report.md"
        self.report.parent.mkdir()

    def claims(self):
        r = self.repo
        return [f"- commit {r} {self.c1[:7]}",
                f"- commit {r} {self.c2[:7]}",
                f"- changed {r} a.py",
                f"- changed {r} test_a.py",
                f"- changed {r} b.py",
                f"- sha256 {self.loose} {sha(self.loose)}",
                f"- created {self.loose}",
                f"- tests {r} :: python3 -m pytest -q -p no:cacheprovider test_a.py :: passed 2 failed 0",
                f'- output {r} :: python3 -m pytest -q -p no:cacheprovider test_a.py :: contains "2 passed"']

    def write(self, claims=None, last="DONE", commit_section=None, claims_section=True):
        claims = self.claims() if claims is None else claims
        hashes = commit_section if commit_section is not None else [self.c1[:7], self.c2[:7]]
        text = ["# ORDER 99 — report", "", "## 1. What was built", "stuff, 20261001 and abcdef0 here", "",
                "## 6. Commit hash"] + [f"- `{h}` — a commit" for h in hashes] + [""]
        if claims_section:
            text += ["## CLAIMS"] + claims + [""]
        text += [last, ""]
        self.report.write_text("\n".join(text))
        return self.report

    def check(self, rec="default", **kw):
        rec = record() if rec == "default" else rec
        kw.setdefault("root", self.root)
        return rc.check(str(self.report), rec, **kw)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def failing(res):
    return [f for f in res["claims"] if not f["ok"]]


# ---------------------------------------------------------------- acquittal: a true report passes

def test_true_report_is_verified_with_zero_flags(w):
    w.write()
    res = w.check()
    assert failing(res) == [], failing(res)
    assert res["verdict"] == "VERIFIED"
    assert len([c for c in res["claims"] if c["kind"] not in ("run", "prose")]) == 9
    assert [c["claim"] for c in res["claims"] if c["kind"] == "prose"] == [
        f"prose {w.c1[:7]}", f"prose {w.c2[:7]}"]


def test_short_and_full_hashes_both_resolve(w):
    claims = w.claims()
    claims[0] = f"- commit {w.repo} {w.c1}"
    w.write(claims, commit_section=[w.c1, w.c2[:10]])
    assert w.check()["verdict"] == "VERIFIED"


# ---------------------------------------------------------------- one false claim, one flag, named

def only_flag(res):
    f = failing(res)
    assert res["verdict"] == "FLAGGED"
    assert len(f) == 1, [x["claim"] for x in f]
    return f[0]


def test_wrong_test_count_is_flagged(w):
    claims = w.claims()
    claims[7] = claims[7].replace("passed 2", "passed 3")
    w.write(claims)
    f = only_flag(w.check())
    assert f["claim"] == claims[7][2:]
    assert "passed 3 failed 0" in f["said"] and "passed 2 failed 0" in f["witness"]


def test_nonexistent_commit_is_flagged(w):
    claims = w.claims()
    claims[1] = f"- commit {w.repo} 1234567"
    w.write(claims, commit_section=[w.c1[:7], "1234567"])
    res = w.check()
    # the omission check would also see b.py as unclaimed; that is the same lie, so it is one
    # commit flag plus one "changed but no claimed commit" for b.py — both name c2's content
    f = [x for x in failing(res) if x["kind"] == "commit"]
    assert len(f) == 1 and "1234567" in f[0]["claim"]
    assert "does not resolve" in f[0]["witness"]


def test_wrong_sha256_is_flagged(w):
    claims = w.claims()
    claims[5] = f"- sha256 {w.loose} {'0' * 64}"
    w.write(claims)
    f = only_flag(w.check())
    assert f["kind"] == "sha256" and sha(w.loose) in f["witness"]


def test_omitted_changed_line_is_flagged_by_name(w):
    claims = w.claims()
    del claims[4]                                  # b.py, touched by c2, no longer listed
    w.write(claims)
    f = only_flag(w.check())
    assert f["kind"] == "changed" and "b.py" in f["claim"]
    assert "(absent)" in f["said"] and w.c2[:7] in f["witness"]


def test_created_file_that_does_not_exist_is_flagged(w):
    claims = w.claims()
    claims[6] = f"- created {w.root / 'ghost.py'}"
    w.write(claims)
    f = only_flag(w.check())
    assert f["kind"] == "created" and "ghost.py" in f["claim"] and "does not exist" in f["witness"]


def test_no_claims_section_is_unchecked(w):
    w.write(claims_section=False)
    res = w.check()
    assert res["verdict"] == "UNCHECKED" and res["claims"] == []


def test_disallowed_command_is_flagged_and_not_run(w):
    victim = w.repo / "x"
    victim.write_text("still here\n")
    claims = w.claims()
    claims.append(f"- tests {w.repo} :: rm -rf x :: passed 1 failed 0")
    w.write(claims)
    f = only_flag(w.check())
    assert "not re-runnable" in f["witness"]
    assert victim.read_text() == "still here\n"


@pytest.mark.parametrize("cmd", [
    "bash -c 'python3 -m pytest'",
    "python3 -c 'print(1)'",
    "python3 evil.py",
    "python3 -m pip install x",
    "/usr/bin/python3 -m pytest",
    ".venv/bin/python -c 1",
    "python3 sub/test_a.py extra",
])
def test_only_the_three_command_shapes_are_allowed(cmd):
    assert rc.allowed_command(cmd)[0] is False, cmd


@pytest.mark.parametrize("cmd", [
    "python3 -m pytest -q test_a.py",
    "python3 sub/test_a.py",
    ".venv/bin/python -m pytest -q x.py",
    ".venv/bin/python /abs/test_x.py",
])
def test_the_three_command_shapes(cmd):
    assert rc.allowed_command(cmd)[0] is True, cmd


# ORDER 63 (P-10, Part D): the brain's own venv python by its absolute path is re-runnable (order 55's
# `.venv/bin/python` claims were written that way). Compared after realpath of its directory, so the
# venv's own symlink to /usr/bin/python3.12 does not let every interpreter through.
BRAIN_VENV_PY = "__HOME__/helix/.venv/bin/python"


@pytest.mark.parametrize("cmd", [
    f"{BRAIN_VENV_PY} -m pytest -q x",
    f"{BRAIN_VENV_PY} /abs/test_x.py",
    f"{BRAIN_VENV_PY.replace('/helix/', '/helix/./')} -m pytest -q x",
])
def test_63_the_brain_venv_by_absolute_path_is_allowed(cmd):
    assert rc.allowed_command(cmd)[0] is True, cmd


@pytest.mark.parametrize("cmd", [
    "/usr/bin/python3 -m pytest",
    "/usr/bin/python3.12 -m pytest",
    "/tmp/v/bin/python -m pytest",
    f"{BRAIN_VENV_PY}3 -m pytest",
    f"{BRAIN_VENV_PY} -c 1",
    f"{BRAIN_VENV_PY} evil.py",
    "__HOME__/helix/workspace/.venv/bin/python -m pytest",
])
def test_63_no_other_absolute_interpreter_or_venv(cmd):
    assert rc.allowed_command(cmd)[0] is False, cmd


def test_63_a_symlink_to_the_brain_venv_python_from_elsewhere_is_not_allowed(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").symlink_to(BRAIN_VENV_PY)
    assert rc.allowed_command(f"{tmp_path}/bin/python -m pytest -q")[0] is False


def test_63_an_absolute_venv_claim_is_re_run(w):
    claims = w.claims()
    claims[7] = f"- tests {w.repo} :: {BRAIN_VENV_PY} -m pytest -q -p no:cacheprovider test_a.py :: passed 2 failed 0"
    w.write(claims)
    res = w.check()
    assert res["verdict"] == "VERIFIED", res


def test_cwd_outside_root_is_not_run(w, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    claims = w.claims()
    claims[7] = f"- tests {other} :: python3 -m pytest -q test_a.py :: passed 2 failed 0"
    w.write(claims)
    f = only_flag(w.check())
    assert "outside" in f["witness"]


def test_timeout_is_a_flag_not_a_pass(w):
    (w.repo / "test_slow.py").write_text("import time\ndef test_s():\n    time.sleep(30)\n")
    claims = w.claims()
    claims[7] = f"- tests {w.repo} :: python3 -m pytest -q -p no:cacheprovider test_slow.py :: passed 1 failed 0"
    git(w.repo, "add", "test_slow.py")            # shows in git status, so it may be listed
    claims.append(f"- changed {w.repo} test_slow.py")
    w.write(claims)
    t0 = time.time()
    f = only_flag(w.check(per_cmd_timeout=1))
    assert time.time() - t0 < 20
    assert f["witness"].startswith("UNVERIFIED: timeout")


def test_total_budget_spent_is_unverified(w):
    w.write()
    res = w.check(total_timeout=0)
    bad = failing(res)
    assert {x["kind"] for x in bad} == {"tests", "output"}
    assert all(x["witness"].startswith("UNVERIFIED: timeout") for x in bad)


def test_errors_are_not_green(w):
    (w.repo / "test_err.py").write_text("import no_such_module_xyz\n")
    claims = w.claims()
    claims[7] = (f"- tests {w.repo} :: python3 -m pytest -q -p no:cacheprovider test_a.py test_err.py "
                 ":: passed 2 failed 0")
    claims.append(f"- changed {w.repo} test_err.py")
    w.write(claims)
    f = only_flag(w.check())
    assert "error" in f["witness"]


def test_output_text_missing_is_flagged(w):
    claims = w.claims()
    claims[8] = claims[8].replace('"2 passed"', '"7 passed"')
    w.write(claims)
    f = only_flag(w.check())
    assert f["kind"] == "output"


# ---------------------------------------------------------------- the window

def test_commit_outside_the_window_is_flagged(w):
    old = commit(w.repo, {"c.py": "C\n"}, env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00"})
    claims = w.claims() + [f"- commit {w.repo} {old[:7]}", f"- changed {w.repo} c.py"]
    w.write(claims, commit_section=[w.c1[:7], w.c2[:7], old[:7]])
    f = only_flag(w.check())
    assert f["kind"] == "commit" and "outside" in f["witness"]


def test_created_file_older_than_the_run_is_flagged(w):
    os.utime(w.loose, (time.time() - 3600, time.time() - 3600))
    claims = w.claims()
    claims[5] = f"- sha256 {w.loose} {sha(w.loose)}"
    w.write(claims)
    f = only_flag(w.check())
    assert f["kind"] == "created" and "outside" in f["witness"]


def test_slack_is_sixty_seconds(w):
    now = time.time()
    os.utime(w.loose, (now - 290, now - 290))         # 50 s before a start 240 s ago
    w.write()
    rec = record(started=rc.iso(now - 240), ended=rc.iso(now + 60))
    assert w.check(rec)["verdict"] == "VERIFIED"
    os.utime(w.loose, (now - 310, now - 310))         # 70 s before
    assert only_flag(w.check(rec))["kind"] == "created"


def test_naive_ended_is_local_time(w):
    w.write()
    now = time.time()
    rec = record(ended=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now + 120)))
    assert w.check(rec)["verdict"] == "VERIFIED"


# ---------------------------------------------------------------- changed: completeness both ways

def test_changed_line_touched_by_nothing_is_flagged(w):
    (w.repo / "untouched.py").write_text("x\n")
    git(w.repo, "add", "untouched.py")
    git(w.repo, "commit", "-q", "-m", "not claimed")
    claims = w.claims() + [f"- changed {w.repo} untouched.py"]
    w.write(claims)
    f = only_flag(w.check())
    assert "untouched.py" in f["claim"] and "neither" in f["witness"]


def test_uncommitted_change_listed_is_fine(w):
    (w.repo / "a.py").write_text("A = 3\n")       # dirty, shows in git status
    (w.repo / "new_dir").mkdir()
    (w.repo / "new_dir" / "n.py").write_text("n\n")
    claims = w.claims() + [f"- changed {w.repo} new_dir/n.py"]
    w.write(claims)
    assert w.check()["verdict"] == "VERIFIED"


# ---------------------------------------------------------------- the run record

def test_builder_exit_nonzero_is_flagged(w):
    w.write()
    f = only_flag(w.check(record(exit=1)))
    assert f["kind"] == "run" and "exit" in f["claim"]


def test_report_exists_false_is_flagged(w):
    w.write()
    f = only_flag(w.check(record(report_exists=False)))
    assert f["kind"] == "run" and "report_exists" in f["claim"]


def test_no_run_record_is_flagged(w):
    w.write()
    res = w.check(None)
    assert res["verdict"] == "FLAGGED"
    bad = failing(res)
    assert bad[0]["kind"] == "run" and "no run record" in bad[0]["witness"]
    # with no window, nothing time-bound can be witnessed either: those flag too, saying why
    assert {x["kind"] for x in bad[1:]} == {"commit", "created"}
    assert all("no window" in x["witness"] for x in bad[1:])


# ---------------------------------------------------------------- the prose scan

def test_hash_in_prose_not_claimed_is_flagged(w):
    w.write(commit_section=[w.c1[:7], w.c2[:7], "deadbee"])
    f = only_flag(w.check())
    assert f["kind"] == "prose" and "deadbee" in f["claim"]


def test_prose_scan_only_reads_the_commit_hash_section(w):
    w.write()     # "abcdef0" and "20261001" sit in section 1
    assert w.check()["verdict"] == "VERIFIED"


# ---------------------------------------------------------------- malformed lines

@pytest.mark.parametrize("line", [
    "- commit only-one-arg",
    "- comitted /x abc1234",
    "- sha256 relative/path " + "0" * 64,
    "- sha256 /abs/path nothex",
    "- created relative.py",
    "- tests /x :: python3 -m pytest :: passed two failed 0",
    "- output /x :: python3 -m pytest :: contains unquoted",
    "a stray sentence",
    "* commit /x abc1234",
])
def test_malformed_line_is_flagged_never_skipped(w, line):
    w.write(w.claims() + [line])
    f = only_flag(w.check())
    assert f["kind"] == "malformed" and f["claim"] == line


@pytest.mark.parametrize("last", ["DONE", "READY FOR /restart", "STOP: ambiguity in AC3"])
def test_final_line_is_not_a_claim(w, last):
    w.write(last=last)
    assert w.check()["verdict"] == "VERIFIED"


def test_claims_section_ends_at_next_heading(w):
    w.write()
    w.report.write_text(w.report.read_text().replace("\nDONE\n", "\n## Appendix\nanything\nDONE\n"))
    assert w.check()["verdict"] == "VERIFIED"


def test_a_hash_behind_a_fenced_comment_is_still_scanned(w):
    w.write(commit_section=[w.c1[:7], w.c2[:7]])
    t = w.report.read_text().replace("## CLAIMS", "```\n# not a heading\nfeedfee\n```\n\n## CLAIMS", 1)
    w.report.write_text(t)
    f = only_flag(w.check())
    assert f["kind"] == "prose" and "feedfee" in f["claim"]


def test_a_fenced_claims_heading_is_not_the_section(w):
    w.write(claims_section=False)
    w.report.write_text(w.report.read_text().replace("\nDONE", "\n```\n## CLAIMS\n- created /x\n```\nDONE"))
    assert w.check()["verdict"] == "UNCHECKED"


def test_two_claims_sections_are_flagged(w):
    w.write()
    t = w.report.read_text().replace("## 6. Commit hash", "## CLAIMS\n- created /nowhere\n\n## 6. Commit hash", 1)
    w.report.write_text(t)
    f = only_flag(w.check())
    assert f["kind"] == "malformed" and "2 '## CLAIMS' headings" in f["said"]


def test_all_true_but_nothing_anchoring_is_not_verified(w):
    claims = [c for c in w.claims() if c.startswith(("- created", "- tests", "- output"))]
    w.write(claims, commit_section=[])
    res = w.check()
    assert res["verdict"] == "FLAGGED"
    assert any("commit or sha256" in x["witness"] for x in failing(res))


# ---------------------------------------------------------------- the CLI

def cli(*args):
    return subprocess.run([sys.executable, str(HERE / "reportcheck.py"), *args],
                          capture_output=True, text=True, timeout=120)


def test_cli_cannot_widen_the_root(w):
    # the CLI's cwd root is ~/helix; a tmp repo is outside it, so its tests claim is not re-run
    w.write()
    rec = w.root / "rec.json"
    rec.write_text(json.dumps(record()))
    r = cli("check", str(w.report), "--run-record", str(rec))
    assert r.returncode == 5, r.stdout + r.stderr
    out = json.loads((w.root / "reports" / "ORDER_99_report.check.json").read_text())
    assert {c["kind"] for c in out["claims"] if not c["ok"]} == {"tests", "output"}


def test_cli_writes_check_json_and_never_edits_the_report(w):
    claims = [c for c in w.claims() if not c.startswith(("- tests", "- output"))]
    w.write(claims)
    before = sha(w.report)
    r = cli("check", str(w.report), "--run-record", json.dumps(record()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("VERIFIED")
    assert sha(w.report) == before
    out = json.loads((w.root / "reports" / "ORDER_99_report.check.json").read_text())
    assert out["verdict"] == "VERIFIED"
    assert out["reportcheck_sha256"] == sha(HERE / "reportcheck.py")
    assert out["report_sha256"] == before
    assert len(out["claims"]) == len(claims) + 2 + 2  # + two run-record checks, two prose hashes


def test_cli_exit_codes(w):
    w.write(claims_section=False)
    assert cli("check", str(w.report), "--run-record", json.dumps(record())).returncode == 6
    w.write([f"- sha256 {w.loose} {'1' * 64}"])
    r = cli("check", str(w.report), "--run-record", json.dumps(record()))
    assert r.returncode == 5 and r.stdout.startswith("FLAGGED")
    assert cli().returncode == 2
    assert cli("check").returncode == 2
    assert cli("check", str(w.root / "nope.md")).returncode == 2
    assert cli("check", str(w.report), "--run-record", "{not json").returncode == 2


def test_cli_first_line_names_the_first_failure(w):
    w.write([f"- sha256 {w.loose} {'1' * 64}"])
    r = cli("check", str(w.report), "--run-record", json.dumps(record()))
    first = r.stdout.splitlines()[0]
    assert first.startswith("FLAGGED") and "sha256" in first and str(w.loose) in first


# ---------------------------------------------------------------- order 41: --out

def test_order41_default_path_is_unchanged_beside_the_report(w):
    claims = [c for c in w.claims() if not c.startswith(("- tests", "- output"))]
    w.write(claims)
    r = cli("check", str(w.report), "--run-record", json.dumps(record()))
    assert r.returncode == 0, r.stdout + r.stderr
    dest = w.root / "reports" / "ORDER_99_report.check.json"
    assert json.loads(dest.read_text())["verdict"] == "VERIFIED"
    assert r.stdout.splitlines()[-1] == f"wrote {dest}"


@pytest.mark.parametrize("out_first", [False, True])
def test_order41_out_writes_only_there(w, tmp_path, out_first):
    claims = [c for c in w.claims() if not c.startswith(("- tests", "- output"))]
    w.write(claims)
    before = sha(w.report)
    reports = sorted(p.name for p in (w.root / "reports").iterdir())
    out_dir = tmp_path / "elsewhere"
    out_dir.mkdir()
    out = out_dir / "r41.json"
    rr = ["--run-record", json.dumps(record())]
    opts = ["--out", str(out)] + rr if out_first else rr + ["--out", str(out)]
    r = cli("check", str(w.report), *opts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(out.read_text())["verdict"] == "VERIFIED"
    assert json.loads(out.read_text())["report_sha256"] == before
    assert sorted(p.name for p in out_dir.iterdir()) == ["r41.json"]        # no .tmp left
    assert sorted(p.name for p in (w.root / "reports").iterdir()) == reports  # nothing beside
    assert sha(w.report) == before
    assert r.stdout.splitlines()[-1] == f"wrote {out}"


def test_order41_out_without_a_run_record(w, tmp_path):
    w.write(claims_section=False)
    out = tmp_path / "x.json"
    r = cli("check", str(w.report), "--out", str(out))
    assert r.returncode == 6, r.stdout + r.stderr
    assert json.loads(out.read_text())["verdict"] == "UNCHECKED"
    assert not (w.root / "reports" / "ORDER_99_report.check.json").exists()


def test_order41_bad_out_is_a_usage_error_and_writes_nothing(w, tmp_path):
    w.write(claims_section=False)
    beside = w.root / "reports" / "ORDER_99_report.check.json"
    outs = tmp_path / "outs"
    outs.mkdir()
    assert cli("check", str(w.report), "--out").returncode == 2
    assert cli("check", str(w.report), "--out", str(outs / "a.json"),
               "--out", str(outs / "b.json")).returncode == 2
    assert cli("check", str(w.report), "--out", str(outs / "no_dir" / "a.json")).returncode == 2
    assert cli("check", str(w.report), "--out", str(outs)).returncode == 2           # a directory
    assert cli("check", str(w.report), "--bogus", "x").returncode == 2
    assert not beside.exists()
    assert list(outs.iterdir()) == []


# ---------------------------------------------------------------- order 27: UTC, and the DST boundary

@pytest.fixture
def london():
    """Europe/London for the test's duration; DST ends there 2026-10-25 02:00 BST -> 01:00 GMT."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_iso_writes_utc_z():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", rc.iso(time.time()))
    assert rc.iso(1792888200) == "2026-10-25T00:30:00Z"


def test_naive_local_and_z_are_the_same_instant_across_the_dst_boundary(london):
    # the day before: BST, local = UTC + 1
    assert rc.epoch("2026-10-24T12:00:00") == rc.epoch("2026-10-24T11:00:00Z")
    # 01:30 BST on the boundary night is 00:30 UTC. 01:30 local happens twice that night;
    # a naive time is read as the first (BST) — the ambiguity the Z removes.
    assert rc.epoch("2026-10-25T01:30:00") == rc.epoch("2026-10-25T00:30:00Z")
    assert rc.epoch("2026-10-25T01:30:00+01:00") == rc.epoch("2026-10-25T00:30:00Z")
    # after the change: GMT, local = UTC
    assert rc.epoch("2026-10-25T03:00:00") == rc.epoch("2026-10-25T03:00:00Z")
    # a run window with a naive (old) start and a Z (new) end spans the change correctly
    assert rc.epoch("2026-10-25T02:30:00Z") - rc.epoch("2026-10-25T00:30:00") == 3 * 3600


def test_a_utc_run_window_is_verified(w):
    w.write()
    now = time.time()
    z = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    rec = record(started=z(now - 120), ended=z(now + 120))
    assert rec["started"].endswith("Z") and rec["ended"].endswith("Z")
    assert w.check(rec)["verdict"] == "VERIFIED"


# ---------------------------------------------------------------- order 53: base claims

OLD = {"GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00"}


def base_world(root):
    """A repo whose base commit is old and whose one claimed commit was made now."""
    repo = root / "brepo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, {"x.py": "X = 0\n"}, env=OLD)
    new = commit(repo, {"x.py": "X = 1\n"})
    return repo, base, new


def write_report(path, claims, hashes, last="DONE"):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ["# ORDER 98 — report", "", "## 6. Commit hash"] + [f"- `{h}`" for h in hashes]
    path.write_text("\n".join(text + ["", "## CLAIMS"] + claims + ["", last, ""]))
    return path


def bcheck(tmp_path, claims, hashes=(), rec="default", last="DONE"):
    report = write_report(tmp_path / "reports" / "ORDER_98_report.md", claims, hashes, last)
    return rc.check(str(report), record() if rec == "default" else rec, root=tmp_path)


def test_order53_base_before_the_window_and_ancestor_is_ok(tmp_path):
    repo, base, new = base_world(tmp_path)
    claims = [f"- commit {repo} {new[:12]}", f"- changed {repo} x.py", f"- base {repo} {base[:12]}"]
    res = bcheck(tmp_path, claims, [new[:7]])
    assert failing(res) == [], failing(res)
    assert res["verdict"] == "VERIFIED"
    assert [c["kind"] for c in res["claims"]].count("base") == 1


def test_order53_base_made_during_the_window_is_flagged(tmp_path):
    repo, base, new = base_world(tmp_path)
    newer = commit(repo, {"y.py": "Y\n"})
    claims = [f"- commit {repo} {newer[:12]}", f"- changed {repo} y.py", f"- base {repo} {new[:12]}"]
    f = only_flag(bcheck(tmp_path, claims))
    assert f["kind"] == "base" and "not before" in f["witness"]


def test_order53_base_not_an_ancestor_is_flagged(tmp_path):
    repo, base, new = base_world(tmp_path)
    git(repo, "checkout", "-q", "-b", "side", base)
    side = commit(repo, {"s.py": "S\n"}, env=OLD)            # old, but on another branch
    git(repo, "checkout", "-q", "main")
    claims = [f"- commit {repo} {new[:12]}", f"- changed {repo} x.py", f"- base {repo} {side[:12]}"]
    f = only_flag(bcheck(tmp_path, claims))
    assert f["kind"] == "base" and "not an ancestor" in f["witness"] and new[:12] in f["witness"]


def test_order53_stop_report_with_only_base_claims_is_verified(tmp_path):
    repos = []
    for name in ("one", "two"):                  # two repos, each with HEAD made before the run
        r = tmp_path / name
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        repos.append((r, commit(r, {"o.py": name}, env=OLD)))
    claims = [f"- base {repos[0][0]} {repos[0][1][:12]}", f"- base {repos[1][0]} {repos[1][1]}"]
    res = bcheck(tmp_path, claims, last="STOP")
    assert failing(res) == [], failing(res)
    assert res["verdict"] == "VERIFIED"                          # base satisfies the anchor
    assert not any(c["kind"] == "anchor" for c in res["claims"])


def test_order53_base_with_no_commit_claim_and_head_moved_is_flagged(tmp_path):
    repo, base, new = base_world(tmp_path)
    f = only_flag(bcheck(tmp_path, [f"- base {repo} {base[:12]}"], last="STOP"))
    assert f["kind"] == "base" and "HEAD" in f["witness"] and new[:12] in f["witness"]


def test_order53_base_that_does_not_resolve_is_flagged(tmp_path):
    repo, base, new = base_world(tmp_path)
    claims = [f"- commit {repo} {new[:12]}", f"- changed {repo} x.py", f"- base {repo} 1234567"]
    f = only_flag(bcheck(tmp_path, claims))
    assert f["kind"] == "base" and "does not resolve" in f["witness"]


def test_order53_base_without_a_run_record_is_unwitnessed(tmp_path):
    repo, base, new = base_world(tmp_path)
    res = bcheck(tmp_path, [f"- base {repo} {new[:12]}"], rec=None, last="STOP")
    bad = failing(res)
    assert [x["kind"] for x in bad] == ["run", "base"]
    assert "no window" in bad[1]["witness"]


def test_order53_prose_parent_hash_covered_by_base_is_ok(tmp_path):
    repo, base, new = base_world(tmp_path)
    claims = [f"- commit {repo} {new[:12]}", f"- changed {repo} x.py", f"- base {repo} {base[:12]}"]
    res = bcheck(tmp_path, claims, [new[:7], base[:7]])
    assert res["verdict"] == "VERIFIED", failing(res)
    prose = [c for c in res["claims"] if c["kind"] == "prose"]
    assert len(prose) == 2 and all(c["ok"] for c in prose)


def test_order53_prose_unrelated_hash_still_flagged_with_base(tmp_path):
    repo, base, new = base_world(tmp_path)
    claims = [f"- commit {repo} {new[:12]}", f"- changed {repo} x.py", f"- base {repo} {base[:12]}"]
    f = only_flag(bcheck(tmp_path, claims, [new[:7], base[:7], "deadbee"]))
    assert f["kind"] == "prose" and "deadbee" in f["claim"]


# ---------------------------------------------------------------- order 53: the claims generator

def tree_state(root):
    """Every path under root (including .git) with its mtime and size."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def test_order53_generated_claims_verify_and_a_broken_hash_flags(tmp_path):
    repo = tmp_path / "grepo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, {"a.py": "A = 0\n", "keep.txt": "k\n"}, env=OLD)
    since = rc.iso(time.time() - 5)
    c1 = commit(repo, {"a.py": "A = 1\n", "sub/b.py": "B\n"})
    c2 = commit(repo, {"c.py": "C\n"})
    loose = tmp_path / "loose.md"
    loose.write_text("outside any repo\n")
    r = cli("claims", "--since", since, str(repo), "--sha256", str(loose))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines == [f"- commit {repo} {c1[:12]}", f"- commit {repo} {c2[:12]}",
                     f"- changed {repo} a.py", f"- changed {repo} c.py", f"- changed {repo} sub/b.py",
                     f"- base {repo} {base[:12]}", f"- sha256 {loose} {sha(loose)}"]
    report = write_report(tmp_path / "reports" / "ORDER_98_report.md", lines, [c1[:7], c2[:7]])
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps(record()))
    v = cli("check", str(report), "--run-record", str(rec))
    assert v.returncode == 0 and v.stdout.startswith("VERIFIED"), v.stdout + v.stderr
    broken = [l.replace(c2[:12], c2[:11] + ("0" if c2[11] != "0" else "1")) for l in lines]
    write_report(report, broken, [c1[:7]])
    v = cli("check", str(report), "--run-record", str(rec))
    assert v.returncode == 5 and v.stdout.startswith("FLAGGED"), v.stdout + v.stderr


def test_order53_generator_with_nothing_since_gives_head_as_base(tmp_path):
    repo, base, new = base_world(tmp_path)
    r = cli("claims", "--since", rc.iso(time.time() + 3600), str(repo))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [f"- base {repo} {new[:12]}"]


def test_order53_generator_is_read_only(tmp_path):
    repo, base, new = base_world(tmp_path)
    loose = tmp_path / "loose.md"
    loose.write_text("x\n")
    time.sleep(0.05)
    before = tree_state(tmp_path)
    r = cli("claims", "--since", rc.iso(time.time() - 600), str(repo), "--sha256", str(loose))
    assert r.returncode == 0, r.stderr
    assert tree_state(tmp_path) == before


def test_order53_generator_usage(tmp_path):
    repo, base, new = base_world(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    now = rc.iso(time.time())
    assert cli("claims", "--since", now, str(plain)).returncode == 2                 # not a repo
    assert cli("claims", "--since", now, str(repo / "nope")).returncode == 2
    assert cli("claims", "--since", now, str(repo), str(plain)).stdout == ""         # nothing printed
    assert cli("claims", str(repo)).returncode == 2                                  # no --since
    assert cli("claims", "--since", now).returncode == 2                             # no repo
    assert cli("claims", "--since", "yesterday", str(repo)).returncode == 2
    assert cli("claims", "--since", "2026-10-04T07:53:43", str(repo)).returncode == 2  # not UTC
    assert cli("claims", "--since", now, "--since", now, str(repo)).returncode == 2
    assert cli("claims", "--since", now, str(repo), "--sha256", "relative.md").returncode == 2
    assert cli("claims", "--since", now, str(repo), "--sha256", str(tmp_path / "gone")).returncode == 2
    assert cli("claims", "--since", now, str(repo), "--bogus").returncode == 2
