"""Tests for preflight.py — Alice's hostile search before any seal (item 15a).

`check` is tested on briefs the program itself builds, then broken one way at a time.
`run` is tested against a STUB `agy` put first on PATH: it records its argv and cwd,
and answers with a canned JSON reply, a non-zero exit, or a sleep past the timeout.
No test here calls the real Alice.
"""

import hashlib
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import preflight  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 2, 3

IDEA = b"Idea: a throwaway idea for tests.\nIt is described here in two lines.\n"

GOOD_SECTIONS = textwrap.dedent("""\
    ## 1. Academia
    - Doe & Roe 2004, "Some paper", Proc. X — already proves the main claim for the scalar case

    ## 2. GitHub
    - NOTHING FOUND
    - query: throwaway idea implementation github
    - query: throwaway idea library

    ## 3. Known math
    - https://example.org/textbook/ch6 — the standard result the idea restates

    ## 4. Known limits
    - Roe 2009, "An impossibility" — bounds what any such scheme can detect

    ## 5. Cheapest kill
    Read chapter 6 of the textbook above; one afternoon.

    ## 6. Verdict
    RESHAPE: narrow the claim to the vector case the literature leaves open
    """)

GOOD_RESPONSE = ("QUERIES:\n- throwaway idea prior work\n- throwaway idea implementation github\n"
                 "- throwaway idea library\n\n" + GOOD_SECTIONS)


def build(sections=GOOD_SECTIONS, **kw):
    args = dict(searcher="Alice", model="Gemini 3.1 Pro (High)",
                date_utc="2026-09-30T22:00:00Z", n_queries=3)
    args.update(kw)
    return preflight.build_brief(IDEA, sections, **args)


def problems_of(text):
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return preflight.check_brief(raw)[0]


def write(tmp_path, text, name="BRIEF.md"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


# ------------------------------------------------------------------ check: accepts

def test_check_accepts_a_good_brief(tmp_path):
    brief = build()
    assert problems_of(brief) == []
    assert preflight.main(["check", str(write(tmp_path, brief))]) == EXIT_OK


def test_check_reports_verdict_and_searcher():
    probs, info = preflight.check_brief(build().encode())
    assert probs == []
    assert info["verdict"] == "RESHAPE"
    assert info["reshape"] == "narrow the claim to the vector case the literature leaves open"
    assert info["searcher"] == "Alice"


def test_check_accepts_go_and_no_go():
    for v in ("GO", "NO-GO"):
        s = GOOD_SECTIONS.replace(
            "RESHAPE: narrow the claim to the vector case the literature leaves open", v)
        probs, info = preflight.check_brief(build(s).encode())
        assert probs == [], probs
        assert info["verdict"] == v


def test_check_accepts_an_appended_odins_additions_section(tmp_path):
    brief = build() + "\n## Odin's additions\n- Smith 1999 — found later by Odin\n"
    assert problems_of(brief) == []
    assert preflight.main(["check", str(write(tmp_path, brief))]) == EXIT_OK


# ------------------------------------------------------------------ check: refuses

@pytest.mark.parametrize("heading", preflight.SECTIONS)
def test_check_refuses_each_missing_section(tmp_path, heading):
    brief = build()
    lines = brief.split("\n")
    i = lines.index(heading)
    j = i + 1
    while j < len(lines) and not lines[j].startswith("## "):
        j += 1
    broken = "\n".join(lines[:i] + lines[j:])
    probs = problems_of(broken)
    assert probs, f"a brief without {heading!r} was accepted"
    assert any(heading.lstrip("# ") in p for p in probs), probs
    assert preflight.main(["check", str(write(tmp_path, broken))]) == EXIT_REFUSED


def test_check_refuses_a_bare_nothing_found_without_queries():
    s = GOOD_SECTIONS.replace("- query: throwaway idea implementation github\n"
                              "- query: throwaway idea library\n", "")
    probs = problems_of(build(s))
    assert any("NOTHING FOUND" in p and "quer" in p for p in probs), probs


def test_check_refuses_nothing_found_mixed_with_findings():
    s = GOOD_SECTIONS.replace("- NOTHING FOUND\n", "- NOTHING FOUND\n- https://x.org — a finding\n")
    assert problems_of(build(s))


def test_check_refuses_an_empty_section():
    s = GOOD_SECTIONS.replace("- Roe 2009, \"An impossibility\" — bounds what any such scheme "
                              "can detect\n", "")
    probs = problems_of(build(s))
    assert any("4. Known limits" in p for p in probs), probs


def test_check_refuses_a_finding_without_the_why():
    s = GOOD_SECTIONS.replace(" — the standard result the idea restates", "")
    probs = problems_of(build(s))
    assert any("3. Known math" in p for p in probs), probs


@pytest.mark.parametrize("verdict", ["MAYBE", "go", "GO!", "RESHAPE:", "RESHAPE", "GO\nNO-GO",
                                     "Verdict: GO", "**GO**"])
def test_check_refuses_a_bad_verdict(verdict):
    s = GOOD_SECTIONS.replace(
        "RESHAPE: narrow the claim to the vector case the literature leaves open", verdict)
    probs = problems_of(build(s))
    assert any("6. Verdict" in p for p in probs), probs


def test_check_refuses_a_header_the_program_did_not_write():
    brief = build()
    # a hand-made header: fields reordered
    lines = brief.split("\n")
    lines[3], lines[4] = lines[4], lines[3]
    assert problems_of("\n".join(lines))
    # an extra field
    assert problems_of(brief.replace("Searcher: Alice\n", "Searcher: Alice\nReviewed: yes\n"))
    # a missing field
    assert problems_of(brief.replace("Queries run: 3\n", ""))
    # the wrong title line
    assert problems_of(brief.replace("# PRE-FLIGHT BRIEF — written by preflight.py, not by hand",
                                     "# My brief"))
    # a malformed hash
    assert problems_of(brief.replace(brief.split("Idea-sha256: ")[1][:64], "abc"))
    # zero queries is not a search
    assert problems_of(build(n_queries=0))


def test_check_refuses_an_edited_section_body_hash_mismatch(tmp_path):
    brief = build()
    edited = brief.replace("already proves the main claim", "hints at the main claim")
    probs = problems_of(edited)
    assert any("Brief-body-sha256" in p for p in probs), probs
    assert preflight.main(["check", str(write(tmp_path, edited))]) == EXIT_REFUSED


def test_check_refuses_a_verdict_flipped_after_writing():
    brief = build()
    flipped = brief.replace("RESHAPE: narrow the claim to the vector case the literature leaves open",
                            "GO")
    assert any("Brief-body-sha256" in p for p in problems_of(flipped))


def test_check_refuses_odins_additions_that_restate_a_section():
    brief = build() + "\n## Odin's additions\n## 6. Verdict\nGO\n"
    assert problems_of(brief)


def test_check_refuses_a_whitespace_edit_inside_the_sections():
    brief = build()
    assert problems_of(brief.replace("one afternoon.", "one afternoon. "))


def test_body_hash_is_over_raw_bytes_of_sections_1_to_6():
    brief = build()
    raw = brief.encode()
    start = raw.index(b"## 1. Academia")
    expect = hashlib.sha256(raw[start:].rstrip(b"\r\n")).hexdigest()
    assert f"Brief-body-sha256: {expect}" in brief


def test_check_on_a_missing_file_is_a_usage_error(tmp_path):
    assert preflight.main(["check", str(tmp_path / "nope.md")]) == EXIT_USAGE


# ----------------------------------------------------------------- run: stub agy

STUB = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    log = os.environ["STUB_LOG"]
    with open(log, "w") as fh:
        json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, fh)
    if os.environ.get("STUB_TOUCH"):
        open(os.environ["STUB_TOUCH"], "w").write("x")
    if os.environ.get("STUB_SLEEP"):
        time.sleep(float(os.environ["STUB_SLEEP"]))
    code = int(os.environ.get("STUB_EXIT", "0"))
    if code:
        sys.stderr.write("stub failure\\n")
        sys.exit(code)
    sys.stdout.write(open(os.environ["STUB_REPLY"]).read())
    """)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    agy = bindir / "agy"
    agy.write_text(STUB.format(python=sys.executable))
    agy.chmod(agy.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "stub_log.json"
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps({"status": "SUCCESS", "response": GOOD_RESPONSE,
                                 "usage": {"total_tokens": 1}}))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_REPLY", str(reply))
    idea = tmp_path / "my_idea.md"
    idea.write_bytes(IDEA)
    out = tmp_path / "out"
    return {"log": log, "reply": reply, "idea": idea, "out": out, "tmp": tmp_path}


def briefs_in(out):
    return sorted(p for p in out.glob("PREFLIGHT_*.md")) if out.exists() else []


def test_run_sends_the_idea_text_and_no_add_dir(stub):
    assert preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])]) == EXIT_OK
    call = json.loads(stub["log"].read_text())
    argv = call["argv"]
    assert "--add-dir" not in argv, argv
    assert "--dangerously-skip-permissions" not in argv, argv
    assert argv[0] == "-p"
    prompt = argv[1]
    assert IDEA.decode() in prompt
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (High)"
    assert argv[argv.index("--output-format") + 1] == "json"
    # she runs in an empty scratch dir, not in ours
    assert Path(call["cwd"]).resolve() != HERE
    assert not str(Path(call["cwd"]).resolve()).startswith(str(Path.home() / "helix"))


def test_the_prompt_is_hostile_and_asks_for_queries():
    p = preflight.PROMPT.lower()
    assert "already done" in p and "already dead" in p and "impossib" in p
    assert "not be encouraging" in p or "do not be encouraging" in p
    assert "nothing found" in p and "quer" in p
    for h in preflight.SECTIONS:
        assert h in preflight.PROMPT


def test_run_writes_the_brief_and_raw_json_with_a_program_header(stub):
    assert preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])]) == EXIT_OK
    briefs = briefs_in(stub["out"])
    assert len(briefs) == 1, briefs
    brief = briefs[0]
    assert brief.name.startswith("PREFLIGHT_my_idea_") and brief.name.endswith("Z.md")
    raw_json = brief.with_suffix(".raw.json")
    assert raw_json.exists()
    raw = json.loads(raw_json.read_text())
    assert json.loads(raw["agy_stdout"])["response"] == GOOD_RESPONSE, \
        "the raw artifact is her unframed reply"
    assert raw["alice_cwd_files"] == []
    text = brief.read_text()
    assert f"Idea-sha256: {hashlib.sha256(IDEA).hexdigest()}" in text
    assert "Searcher: Alice\n" in text
    assert "Model: Gemini 3.1 Pro (High)\n" in text
    assert "Queries run: 3\n" in text
    assert "Idea: a throwaway idea for tests.\n" in text
    assert preflight.main(["check", str(brief)]) == EXIT_OK


def test_run_ignores_a_header_alice_writes_herself(stub):
    forged = ("Searcher: Nobody\nIdea-sha256: " + "0" * 64 + "\n" + GOOD_RESPONSE)
    stub["reply"].write_text(json.dumps({"status": "SUCCESS", "response": forged}))
    assert preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])]) == EXIT_OK
    text = briefs_in(stub["out"])[0].read_text()
    assert "Nobody" not in text and "0" * 64 not in text
    assert "Searcher: Alice\n" in text


def test_run_non_zero_exit_writes_no_brief(stub, monkeypatch):
    monkeypatch.setenv("STUB_EXIT", "7")
    rc = preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])])
    assert rc != EXIT_OK
    assert briefs_in(stub["out"]) == []


def test_run_timeout_writes_no_brief(stub, monkeypatch):
    monkeypatch.setattr(preflight, "TIMEOUT", 1)
    monkeypatch.setenv("STUB_SLEEP", "5")
    rc = preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])])
    assert rc != EXIT_OK
    assert briefs_in(stub["out"]) == []


def test_the_real_timeout_is_900_seconds():
    assert preflight.TIMEOUT == 900


def test_run_malformed_reply_writes_no_brief_but_keeps_the_raw(stub):
    stub["reply"].write_text(json.dumps({"status": "SUCCESS", "response": "Looks great, GO!"}))
    rc = preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])])
    assert rc == EXIT_REFUSED
    assert briefs_in(stub["out"]) == []
    assert list(stub["out"].glob("*.raw.json")), "her reply is kept even when refused"


def test_run_unparseable_json_writes_no_brief(stub):
    stub["reply"].write_text("not json at all")
    assert preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])]) != EXIT_OK
    assert briefs_in(stub["out"]) == []


def test_run_refuses_if_a_file_appears_in_alices_cwd(stub, monkeypatch):
    # the stub writes into its own cwd, which is the scratch dir the program made
    monkeypatch.setenv("STUB_TOUCH", "written_by_alice.txt")
    rc = preflight.main(["run", str(stub["idea"]), "--out", str(stub["out"])])
    assert rc != EXIT_OK
    assert briefs_in(stub["out"]) == []
    raws = list(stub["out"].glob("*.raw.json"))
    assert raws
    assert json.loads(raws[0].read_text())["alice_cwd_files"] == ["written_by_alice.txt"]


def test_run_on_a_missing_idea_is_a_usage_error(stub):
    assert preflight.main(["run", str(stub["tmp"] / "none.md"), "--out", str(stub["out"])]) \
        == EXIT_USAGE


def test_calibration_fixtures_name_no_killer():
    """They describe each idea as on day one. The answer key lives only in the report."""
    for f in (HERE / "calibration").glob("*.md"):
        t = f.read_text().lower()
        for word in ("shankar", "castelluccia", "lehmann", "romano", "maximal invariant"):
            assert word not in t, (f.name, word)
