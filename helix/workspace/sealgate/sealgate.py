#!/usr/bin/env python3
"""sealgate.py — a run cannot be blessed until its seal has survived a reviewer
who is not its author, and a seal cannot be quietly edited afterwards to match
what the data did.

Four verbs:

    sealgate.py check  <PREREG.md>                       exit 0 ok / 3 refused
    sealgate.py stamp  <PREREG.md> --reviewer N --findings P
    sealgate.py token  <PREREG.md>                       exit 0 issued / 3 refused
    sealgate.py verify <PREREG.md>                       exit 0 intact / 4 drifted

`token` issues RUN_TOKEN.json only if ALL TEN preconditions hold:

    P1_REVIEW_STAMP            a dated stamp naming a reviewer and a findings file
                               that exists and still hashes to what was stamped
    P2_REVIEWER_NOT_AUTHOR     the reviewer is not an author of the seal
    P3_FINDINGS_DISPOSITIONED  every finding is ADDRESSED / ACCEPTED / DECLINED: <reason>
    P4_CONTROLS_CHECKLIST      a controls seal block, every control ran or skipped,
                               and skipped carries a written reason
    P5_BANDS_AND_STOP_CONDITIONS   both declared as named sections
    P6_CONTENT_HASH            the hashable content is well defined (exactly one
                               stamp block at most, and a non-empty body)
    P7_STAMP_BLOCK_CANONICAL   the stamp block holds nothing but the fields this tool
                               writes: no unrecognised line, no duplicated field
    P8_PREFLIGHT               a heading naming the pre-flight, holding `Brief: <path>` and
                               `Brief-sha256: <hex over raw bytes>`; the brief exists, still
                               hashes to that, passes `preflight.py check`, is not NO-GO,
                               a RESHAPE is quoted under a heading naming the reshape, and
                               its Searcher is not an author of the seal
    P9_ASSIGNED_REVIEWER       review.py ASSIGNED this seal a reviewer (REVIEWS.jsonl), the
                               stamp names exactly that reviewer, that reviewer's raw review is
                               on file and still hashes to the ledger, every finding id in it is
                               in the stamped findings file, and the chain is intact up to there
    P10_ALICE_VETO             veto.py ran Alice against THIS body (VETOES.jsonl, keyed by the
                               seal and its body_sha256): a NO_BREAK, BREAK_NOT_REPRODUCED or
                               BREAK_INVALID run whose recorded files still hash as recorded, no
                               BREAK_CONFIRMED or BREAK_DISPUTED (or order 48's legacy
                               BREAK_REPRODUCED) for this body (the union rule: one such run is a
                               veto; a disputed one waits for the user), and the whole chain intact.
                               FAILED or no run refuses

P10 reads the veto ledger, not the stamp, so `verify` is unchanged and tokens issued before P10
still verify. Editing the seal changes its body hash, so it needs a new veto run. There is no
overrule path. P10 imports ../veto/veto.py; if that cannot be loaded, P10 fails.

P9 reads the ledger, not the body, so like P8 it leaves the stamp block and `verify` unchanged:
tokens issued before P9 still verify. It imports review.py from this directory; if that cannot
be loaded, P9 fails.

P8's path and hash are in the BODY, so the body hash already covers them: the stamp block
is unchanged and `verify` is unchanged, so tokens issued before P8 stay valid. P8 imports
`preflight.py` from ../preflight/ (it does not shell out); if that cannot be loaded, P8
fails. A brief that gains `## Odin's additions` after the seal changes its bytes: re-pin it.

Hashes are over RAW BYTES, never over decoded or normalised text. Three of them:

    file_sha256       the whole pre-registration, stamp block included
    body_sha256       the pre-registration with the stamp block region removed
    findings-sha256   the findings file, as recorded by `stamp` and checked by P1 and `verify`

The findings hash is over bytes for two concrete reasons, both of which a decoded view hides:
`errors="replace"` maps different invalid bytes onto the same U+FFFD, and `read_text()`
translates newlines, so LF -> CRLF leaves the decoded text identical. Decoded text is used to
PARSE the findings (P3's dispositions); it is never hashed.

The body hash cannot see inside the stamp block, which is plain hand-editable markdown.
Order 02 recorded only that hash, so a post-hoc band placed inside the stamp block was
invisible to `verify`, which reported "byte-for-byte ... INTACT", exit 0. `verify` therefore
distinguishes three states, and **the words "byte-for-byte" appear only when file_sha256
matches**:

    INTACT                 file hash matches                         exit 0
    STAMP REGION CHANGED   body matches, file does not               exit 4
    DRIFT                  body hash differs                         exit 4

A schema-1 token records no file hash. It is refused loudly; there is no fall back to
body-only verification, because that is the same defect wearing a version number.

There is no way to waive a precondition. A gate that can be waived is a warning.
If a seal is wrong, the seal gets fixed and re-stamped; the gate is not argued with.
Refusals are exit codes and stderr, and every one of them appends a line to
refusals.log next to this file.

Exit codes:  0 blessed · 2 usage / unreadable input · 3 refused · 4 drift
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

VERSION = "1.1"
TOKEN_SCHEMA = "sealgate/run-token/2"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_DRIFT = 4

TOKEN_NAME = "RUN_TOKEN.json"
REFUSALS_LOG = Path(__file__).resolve().parent / "refusals.log"

# The seven controls, from order 01's artifact:
#   ~/helix/workspace/controls/SEAL_CHECKLIST.md   (read there, never edited here)
# test_sealgate.py asserts this list still matches that file.
REQUIRED_CONTROLS = [
    "answer_free_floor",
    "random_init_null",
    "decoy_label",
    "scrambled_arm",
    "random_ranking_arm",
    "planted_positive",
    "influence_test",
]

# Order 75 (item 14): a rig that cannot find a plant you put there yourself reports nulls that
# mean nothing. These controls may not be skipped, and must have run and PASSed.
MUST_RUN_PASS = ("planted_positive",)
PASS_RE = re.compile(r"\bPASS\b")

PRECONDITIONS = [
    "P1_REVIEW_STAMP",
    "P2_REVIEWER_NOT_AUTHOR",
    "P3_FINDINGS_DISPOSITIONED",
    "P4_CONTROLS_CHECKLIST",
    "P5_BANDS_AND_STOP_CONDITIONS",
    "P6_CONTENT_HASH",
    "P7_STAMP_BLOCK_CANONICAL",
    "P8_PREFLIGHT",
    "P9_ASSIGNED_REVIEWER",
    "P10_ALICE_VETO",
]

# Order 21, item 15a: the pre-flight brief, written by preflight.py from Alice's search.
PREFLIGHT_PY = Path(__file__).resolve().parent.parent / "preflight" / "preflight.py"
PREFLIGHT_HEADING_RE = re.compile(r"pre-?flight", re.I)
RESHAPE_HEADING_RE = re.compile(r"\breshape", re.I)
BRIEF_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?Brief\s*:\s*`?(\S.*?)`?\s*$", re.M)
BRIEF_SHA_RE = re.compile(r"^\s*(?:[-*]\s*)?Brief-sha256\s*:\s*`?(\S*?)`?\s*$", re.M)

# Order 47, item 9: reviewer rotation. review.py assigns and runs; P9 reads its ledger.
REVIEW_PY = Path(__file__).resolve().parent / "review.py"
REVIEWS_LEDGER = Path(__file__).resolve().parent / "REVIEWS.jsonl"

# Order 48, amendment 9a: Alice's veto. veto.py runs her; P10 reads its ledger.
VETO_PY = Path(__file__).resolve().parent.parent / "veto" / "veto.py"
VETOES_LEDGER = Path(__file__).resolve().parent.parent / "veto" / "VETOES.jsonl"

STAMP_BEGIN = "<!-- SEALGATE-STAMP BEGIN -->"
STAMP_END = "<!-- SEALGATE-STAMP END -->"
STAMP_HEADING = "## REVIEW STAMP — written by sealgate.py, not by hand"
STAMP_BLOCK_RE = re.compile(re.escape(STAMP_BEGIN) + r".*?" + re.escape(STAMP_END), re.S)
# The same region, matched on bytes, because both hashes are over raw bytes.
STAMP_BLOCK_BYTES_RE = re.compile(re.escape(STAMP_BEGIN.encode()) + rb".*?"
                                  + re.escape(STAMP_END.encode()), re.S)

# Everything the stamp block is allowed to contain, and the shape of each value.
# P7 refuses anything else, so the region the body hash cannot see stays too small
# to hide a band in.
STAMP_FIELD_PATTERNS = {
    "reviewer": re.compile(r"^\S.*$"),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "findings": re.compile(r"^\S.*$"),
    "findings-sha256": re.compile(r"^[0-9a-f]{64}$"),
    "sealgate": re.compile(r"^\d+(?:\.\d+)*$"),
}
STAMP_FIELD_LINE_RE = re.compile(r"^- ([a-z0-9-]+): ?(.*)$")

# `**Author:** Odin` is the convention in this programme's real pre-registrations,
# so the closing asterisks land after the colon and must not end up in the record.
AUTHOR_RE = re.compile(r"^\s*[*_]{0,2}(?:Authors?|Written by|Sealed by)[*_]{0,2}\s*:"
                       r"[*_\s]*(.+?)\s*$", re.I | re.M)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
BANDS_RE = re.compile(r"\bbands?\b", re.I)
STOPS_RE = re.compile(r"\b(?:stop|kill)\s+conditions?\b", re.I)
FINDING_RE = re.compile(r"^(?:[-*]\s*)?\[?(F\d+)\]?[\s.:)—-]", re.M)
DISPOSITION_RE = re.compile(r"^\s*(?:[-*]\s*)?DISPOSITION\s*:\s*(.*?)\s*$", re.I | re.M)
CONTROLS_HEADER_RE = re.compile(r"CONTROLS SEAL BLOCK", re.I)
ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$", re.M)


# --------------------------------------------------------------------- helpers

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(raw):
    """The whole file, stamp block included. Raw bytes, no normalising."""
    return sha256_bytes(raw)


def body_hash(raw):
    """The file with the stamp block region cut out. Raw bytes, no normalising.

    Nothing is normalised away: a trailing space is content, because a gate that
    forgives whitespace has told you it does not read the file you sealed.
    """
    return sha256_bytes(STAMP_BLOCK_BYTES_RE.sub(b"", raw))


def strip_stamp(text):
    """The decoded body, for parsing only. Hashes never go through this."""
    return STAMP_BLOCK_RE.sub("", text)


def sections_of(body):
    """{heading: sha256 of that section} so drift can be named, not just detected.

    These hashes are over DECODED text and are advisory only: they localise a change
    to a section for the reader. Detection is `body_sha256` over raw bytes, so nothing
    here can produce a false INTACT.
    """
    out, key, buf = {}, "(preamble)", []
    for line in body.split("\n"):
        m = HEADING_RE.match(line)
        if m:
            if buf or key == "(preamble)":
                out[key] = sha256("\n".join(buf))
            key, buf = m.group(2)[:80], []
        else:
            buf.append(line)
    out[key] = sha256("\n".join(buf))
    return out


def normalise_name(name):
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"[*_`]", "", name)
    name = re.sub(r"[.,;:]+$", "", name.strip())
    return re.sub(r"\s+", " ", name).strip().lower()


def split_authors(author_line):
    parts = re.split(r",| and | & |/|;", author_line)
    return [normalise_name(p) for p in parts if normalise_name(p)]


def preamble_of(body):
    """Everything above the first heading below level 1 — where the author line lives."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) >= 2:
            return "\n".join(lines[:i])
    return body


def read_raw(path, what):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        sys.stderr.write(f"cannot read {what} {path}: {exc}\n")
        raise SystemExit(EXIT_USAGE)


def utc_now():
    """Order 27: a new timestamp is UTC with a Z. issued_at is not hashed anywhere, so a
    token written before this (naive local time) still verifies; a reader treats a naive
    value as local time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path, what):
    return read_raw(path, what).decode("utf-8", errors="replace")


def log_refusal(verb, prereg, exit_code, reasons):
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    line = (f"{stamp} exit={exit_code} verb={verb} "
            f"prereg={os.path.abspath(prereg)} failed={','.join(reasons) or 'UNSPECIFIED'}\n")
    try:
        with open(REFUSALS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:  # the gate still refuses, it just could not write it down
        sys.stderr.write(f"WARNING: could not append to {REFUSALS_LOG}: {exc}\n")


# ------------------------------------------------------------- stamp handling

def find_stamp_blocks(text):
    return STAMP_BLOCK_RE.findall(text)


def parse_stamp(text):
    """Fields of the FIRST stamp block, or None. P6 is what refuses a second one."""
    blocks = find_stamp_blocks(text)
    if not blocks:
        return None
    fields = {}
    for line in blocks[0].split("\n"):
        m = re.match(r"^\s*-\s*([a-z0-9-]+)\s*:\s*(.*?)\s*$", line, re.I)
        if m:
            fields.setdefault(m.group(1).lower(), m.group(2))
    return fields


def build_stamp(reviewer, findings_rel, findings_hash, date):
    return "\n".join([
        STAMP_BEGIN,
        STAMP_HEADING,
        f"- reviewer: {reviewer}",
        f"- date: {date}",
        f"- findings: {findings_rel}",
        f"- findings-sha256: {findings_hash}",
        f"- sealgate: {VERSION}",
        STAMP_END,
    ])


# ------------------------------------------------------------- the ten checks

class Check:
    __slots__ = ("status", "detail")

    def __init__(self, status, detail):
        self.status = status          # "ok" | "failed" | "not-evaluated"
        self.detail = detail


def check_p1(text, prereg_dir):
    blocks = find_stamp_blocks(text)
    if not blocks:
        return Check("failed", "no review stamp: this seal has not been reviewed by anyone"), None
    f = parse_stamp(text)
    reviewer = f.get("reviewer", "").strip()
    date = f.get("date", "").strip()
    findings_rel = f.get("findings", "").strip()
    stamped_hash = f.get("findings-sha256", "").strip()
    if not reviewer:
        return Check("failed", "review stamp names no reviewer"), None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return Check("failed", f"review stamp date {date!r} is not an ISO date"), None
    if not findings_rel:
        return Check("failed", "review stamp points at no findings file"), None
    findings_path = (prereg_dir / findings_rel)
    if not findings_path.is_file():
        return Check("failed", f"findings file {findings_rel} does not exist"), None
    findings_raw = findings_path.read_bytes()
    findings_text = findings_raw.decode("utf-8", errors="replace")   # parsing only
    now_hash = sha256_bytes(findings_raw)
    if stamped_hash and now_hash != stamped_hash:
        return Check("failed",
                     f"findings file {findings_rel} changed after it was stamped "
                     f"(stamped {stamped_hash[:12]}, now {now_hash[:12]})"), None
    return (Check("ok", f"stamped by {reviewer} on {date} -> {findings_rel}"),
            {"reviewer": reviewer, "date": date, "findings_rel": findings_rel,
             "findings_path": findings_path, "findings_text": findings_text,
             "findings_sha256": now_hash})


def check_p2(body, stamp):
    if stamp is None:
        return Check("not-evaluated", "depends on P1_REVIEW_STAMP"), None
    m = AUTHOR_RE.search(preamble_of(body))
    if not m:
        return Check("failed",
                     "the seal declares no author, so 'the reviewer is not the author' "
                     "cannot be established"), None
    author_line = m.group(1).strip()
    authors = split_authors(author_line)
    reviewer = normalise_name(stamp["reviewer"])
    if reviewer in authors:
        return Check("failed",
                     f"reviewer {stamp['reviewer']!r} is also an author of this seal "
                     f"({author_line!r}); a seal reviewed by the person who wrote it "
                     f"is an unreviewed seal"), None
    return Check("ok", f"reviewer {stamp['reviewer']!r} is not among {authors}"), \
        {"author_line": author_line, "authors": authors}


def check_p3(stamp):
    if stamp is None:
        return Check("not-evaluated", "depends on P1_REVIEW_STAMP"), None
    text = stamp["findings_text"]
    starts = [(m.start(), m.group(1)) for m in FINDING_RE.finditer(text)]
    if not starts:
        return Check("ok", "findings file lists no findings (0 findings)"), {}
    bounds = [s[0] for s in starts] + [len(text)]
    dispositions, undispositioned = {}, []
    for i, (_, fid) in enumerate(starts):
        block = text[bounds[i]:bounds[i + 1]]
        m = DISPOSITION_RE.search(block)
        value = m.group(1).strip() if m else ""
        if not value:
            undispositioned.append(f"{fid} (no disposition)")
            continue
        if re.match(r"^(ADDRESSED|ACCEPTED)\b", value, re.I):
            dispositions[fid] = value.split()[0].upper()
        elif re.match(r"^DECLINED\s*:", value, re.I):
            reason = re.sub(r"^DECLINED\s*:", "", value, flags=re.I).strip()
            if len(reason) < 3:
                undispositioned.append(f"{fid} (DECLINED with no reason in writing)")
            else:
                dispositions[fid] = "DECLINED"
        else:
            undispositioned.append(f"{fid} (disposition {value!r} is not "
                                   f"ADDRESSED / ACCEPTED / DECLINED: <reason>)")
    if undispositioned:
        return Check("failed",
                     f"{len(undispositioned)} of {len(starts)} findings not dispositioned: "
                     + "; ".join(undispositioned)), None
    return Check("ok", f"all {len(starts)} findings dispositioned"), dispositions


def check_p4(body):
    if not CONTROLS_HEADER_RE.search(body):
        return Check("failed",
                     "no controls seal block (see workspace/controls/SEAL_CHECKLIST.md)"), None
    start = CONTROLS_HEADER_RE.search(body).start()
    rows = {}
    for m in ROW_RE.finditer(body, start):
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) < 4 or set(cells[0]) <= set("-: "):
            continue
        if cells[0] in REQUIRED_CONTROLS:
            rows[cells[0]] = {"status": cells[1].lower(), "result": cells[2],
                              "reason": cells[3]}
    problems = []
    for name in REQUIRED_CONTROLS:
        row = rows.get(name)
        if row is None:
            problems.append(f"{name}: no row in the controls block")
        elif row["status"] not in ("ran", "skipped"):
            problems.append(f"{name}: not marked ran or skipped (found {row['status']!r})")
        elif name in MUST_RUN_PASS and row["status"] == "skipped":
            problems.append(f"{name}: may not be skipped — a rig that has never caught a "
                            f"plant cannot report a null (item 14)")
        elif name in MUST_RUN_PASS and (not PASS_RE.search(row["result"])
                                        or "FAIL" in row["result"]
                                        or "INDETERMINATE" in row["result"]):
            problems.append(f"{name}: ran but the result is not a clean PASS "
                            f"(found {row['result']!r})")
        elif row["status"] == "skipped" and len(row["reason"]) < 3:
            problems.append(f"{name}: skipped with no reason in writing")
    if problems:
        return Check("failed", "; ".join(problems)), None
    ran = sum(1 for r in rows.values() if r["status"] == "ran")
    return Check("ok", f"{len(rows)} controls declared, {ran} ran, "
                       f"{len(rows) - ran} skipped with reasons, positive control PASS"), rows


def check_p5(body):
    headings = [h for _, h in HEADING_RE.findall(body)]
    has_bands = [h for h in headings if BANDS_RE.search(h)]
    has_stops = [h for h in headings if STOPS_RE.search(h)]
    missing = []
    if not has_bands:
        missing.append("no section heading names the bands")
    if not has_stops:
        missing.append("no section heading names the stop conditions "
                       "(or kill conditions)")
    if missing:
        return Check("failed", "; ".join(missing)), None
    return Check("ok", f"bands in {has_bands[0]!r}; stop conditions in {has_stops[0]!r}"), \
        {"bands": has_bands[0], "stop_conditions": has_stops[0]}


def check_p6(text, raw):
    begins = text.count(STAMP_BEGIN)
    ends = text.count(STAMP_END)
    blocks = len(find_stamp_blocks(text))
    if begins != ends or blocks != begins:
        return Check("failed",
                     f"stamp markers do not pair up ({begins} begin, {ends} end, "
                     f"{blocks} well-formed); the content to hash is not well defined"), None
    if blocks > 1:
        return Check("failed",
                     f"{blocks} stamp blocks found; with more than one, 'the content "
                     f"outside the stamp' is ambiguous and an edit can be hidden inside "
                     f"the second block"), None
    body = STAMP_BLOCK_BYTES_RE.sub(b"", raw)
    if not body.strip():
        return Check("failed", "nothing to hash outside the stamp block"), None
    return (Check("ok", f"body {len(body)} bytes, sha256 {body_hash(raw)[:12]}; "
                        f"whole file sha256 {file_hash(raw)[:12]}"),
            body_hash(raw))


def check_p7(text, p6_ok):
    """The stamp block must hold nothing but the fields this tool writes.

    The body hash cannot see inside the stamp block, so the block is kept small
    enough that nothing can hide in it. Detection (the file hash) and prevention
    (this check) are both here on purpose: each one alone has already failed once.
    """
    blocks = find_stamp_blocks(text)
    if not blocks:
        return Check("not-evaluated", "depends on P1_REVIEW_STAMP"), None
    if not p6_ok:
        return Check("not-evaluated", "depends on P6_CONTENT_HASH"), None
    lines = blocks[0].split("\n")
    problems, seen, headings = [], {}, 0
    for line in lines[1:-1]:                      # markers are lines[0] and lines[-1]
        if not line.strip():
            continue
        if line == STAMP_HEADING:
            headings += 1
            continue
        m = STAMP_FIELD_LINE_RE.match(line)
        if not m:
            problems.append(f"unrecognised line in the stamp block: {line[:120]!r}")
            continue
        key, value = m.group(1), m.group(2)
        if key not in STAMP_FIELD_PATTERNS:
            problems.append(f"unrecognised field {key!r} in the stamp block "
                            f"(value {value[:60]!r})")
            continue
        if key in seen:
            problems.append(f"field {key!r} appears more than once in the stamp block "
                            f"({seen[key][:40]!r} and {value[:40]!r}); a reader and a "
                            f"parser would not agree on which is the stamp")
            continue
        seen[key] = value
        if not STAMP_FIELD_PATTERNS[key].match(value):
            problems.append(f"field {key!r} has a value this tool would never write: "
                            f"{value[:60]!r}")
    missing = [k for k in STAMP_FIELD_PATTERNS if k not in seen]
    if missing:
        problems.append(f"stamp block is missing field(s): {sorted(missing)}")
    if headings != 1:
        problems.append(f"stamp block has {headings} canonical heading lines, expected 1")
    if problems:
        return Check("failed", "; ".join(problems)), None
    return Check("ok", f"stamp block holds only its {len(seen)} fields and its heading, "
                       f"{len(lines)} lines total"), seen


def sections_matching(body, heading_re):
    """[(heading, text under it up to the next heading of any level)] for matching headings."""
    heads = list(HEADING_RE.finditer(body))
    out = []
    for i, m in enumerate(heads):
        if heading_re.search(m.group(2)):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            out.append((m.group(2), body[m.end():end]))
    return out


_preflight_module = None


def load_preflight():
    """preflight.py, imported from its file. Any failure to load it fails P8, never passes it."""
    global _preflight_module
    if _preflight_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("preflight", PREFLIGHT_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _preflight_module = module
    return _preflight_module


def check_p8(body, prereg_dir):
    sections = [(h, t) for h, t in sections_matching(body, PREFLIGHT_HEADING_RE)
                if not RESHAPE_HEADING_RE.search(h)]
    if not sections:
        return Check("failed", "no pre-flight section: no run token without Alice's pre-flight "
                               "brief (run workspace/preflight/preflight.py, then name the brief "
                               "under a heading naming the pre-flight)"), None
    if len(sections) > 1:
        return Check("failed", f"{len(sections)} headings name the pre-flight "
                               f"({[h for h, _ in sections]}); which brief is sealed is ambiguous"), None
    heading, text = sections[0]
    paths, hashes = BRIEF_LINE_RE.findall(text), BRIEF_SHA_RE.findall(text)
    if len(paths) != 1 or len(hashes) != 1:
        return Check("failed", f"the pre-flight section {heading!r} must hold exactly one "
                               f"`Brief: <relative path>` and one `Brief-sha256: <hex>` line "
                               f"(found {len(paths)} and {len(hashes)})"), None
    brief_rel, pinned = paths[0], hashes[0]
    if not re.match(r"^[0-9a-f]{64}$", pinned):
        return Check("failed", f"Brief-sha256 {pinned[:70]!r} is not a sha256 hex digest"), None
    brief_path = prereg_dir / brief_rel
    if not brief_path.is_file():
        return Check("failed", f"pre-flight brief {brief_rel} does not exist"), None
    brief_raw = brief_path.read_bytes()
    now = sha256_bytes(brief_raw)
    if now != pinned:
        return Check("failed", f"pre-flight brief {brief_rel} does not hash to the Brief-sha256 "
                               f"the seal pins (pinned {pinned[:12]}, now {now[:12]})"), None
    try:
        preflight = load_preflight()
        problems, info = preflight.check_brief(brief_raw)
    except Exception as exc:                      # fail closed: no module, no pass
        return Check("failed", f"cannot run preflight.py check from {PREFLIGHT_PY}: "
                               f"{type(exc).__name__}: {exc}"), None
    if problems:
        return Check("failed", f"pre-flight brief {brief_rel} fails `preflight.py check`: "
                               + "; ".join(problems[:5])), None
    verdict, searcher = info["verdict"], info["searcher"]
    if verdict == "NO-GO":
        return Check("failed", f"pre-flight brief {brief_rel} says NO-GO; a seal does not "
                               f"outrank its pre-flight"), None
    if verdict == "RESHAPE":
        line = info["reshape"]
        quoted = [h for h, t in sections_matching(body, RESHAPE_HEADING_RE) if line in t]
        if not quoted:
            return Check("failed", f"pre-flight brief {brief_rel} says RESHAPE: {line!r}; the "
                                   f"seal needs a heading naming the reshape that quotes that "
                                   f"line"), None
    m = AUTHOR_RE.search(preamble_of(body))
    if not m:
        return Check("failed", "the seal declares no author, so 'the pre-flight searcher is "
                               "not an author' cannot be established"), None
    authors = split_authors(m.group(1).strip())
    if normalise_name(searcher) in authors:
        return Check("failed", f"pre-flight searcher {searcher!r} is an author of this seal "
                               f"({authors}); the author is the worst searcher for reasons "
                               f"the idea is dead"), None
    return (Check("ok", f"brief {brief_rel} ({now[:12]}) searched by {searcher}, verdict "
                        f"{verdict}"),
            {"brief": brief_rel, "brief_sha256": now, "searcher": searcher,
             "verdict": verdict, "reshape": info.get("reshape")})


_review_module = None


def load_review():
    """review.py, imported from its file. Any failure to load it fails P9, never passes it."""
    global _review_module
    if _review_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sealgate_review", REVIEW_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _review_module = module
    return _review_module


def check_p9(prereg_path, stamp):
    if stamp is None:
        return Check("not-evaluated", "depends on P1_REVIEW_STAMP"), None
    try:
        rv = load_review()
        entries = rv.read_ledger(REVIEWS_LEDGER)
        key = rv.seal_key(prereg_path)
    except Exception as exc:                      # fail closed: no module, no pass
        return Check("failed", f"cannot read the review ledger with {REVIEW_PY}: "
                               f"{type(exc).__name__}: {exc}"), None
    assigns = [(i, rec) for i, (_, _, rec) in enumerate(entries)
               if rec and rec.get("kind") == "assign" and rec.get("seal") == key]
    if not assigns:
        return Check("failed", f"no reviewer was assigned to {key} in {REVIEWS_LEDGER.name} "
                               f"(run `review.py assign`, then `review.py run`)"), None
    named = sorted({str(rec.get("reviewer")) for _, rec in assigns})
    if len(named) != 1:
        return Check("failed", f"{REVIEWS_LEDGER.name} assigns {key} to more than one "
                               f"reviewer ({named}); an assignment is never re-rolled"), None
    assigned = named[0]
    if stamp["reviewer"] != assigned:
        return Check("failed", f"the stamp names {stamp['reviewer']!r}, but this seal was "
                               f"assigned {assigned!r}; the author does not pick the "
                               f"reviewer"), None
    reviews = [(i, rec) for i, (_, _, rec) in enumerate(entries)
               if rec and rec.get("kind") == "review" and rec.get("seal") == key
               and rec.get("reviewer") == assigned]
    if not reviews:
        return Check("failed", f"no review of {key} by {assigned} is in "
                               f"{REVIEWS_LEDGER.name} (run `review.py run`)"), None
    last = max(i for i, _ in assigns + reviews)
    problems = rv.verify_chain(entries[:last + 1])
    if problems:
        return Check("failed", f"{REVIEWS_LEDGER.name} chain is broken at line "
                               f"{problems[0][0]}, before this seal's lines: "
                               f"{problems[0][1]}"), None
    raised, raws = set(), []
    for _, rec in reviews:
        raw_key = str(rec.get("raw", ""))
        raw_path = rv.path_of(raw_key)
        if not raw_key or not raw_path.is_file():
            return Check("failed", f"raw review {raw_key or '(none named)'} does not "
                                   f"exist"), None
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != rec.get("raw_sha256"):
            return Check("failed", f"raw review {raw_path.name} changed after it was "
                                   f"recorded (ledger {str(rec.get('raw_sha256'))[:12]}, "
                                   f"now {sha256_bytes(raw)[:12]})"), None
        raised |= set(rv.finding_ids(raw.decode("utf-8", errors="replace")))
        raws.append(raw_key)
    stamped = {m.group(1) for m in FINDING_RE.finditer(stamp["findings_text"])}
    missing = sorted(raised - stamped, key=lambda f: int(f[1:]))
    if missing:
        return Check("failed", f"finding(s) {', '.join(missing)} raised by {assigned} are not "
                               f"in the stamped findings file {stamp['findings_rel']}"), None
    ids = sorted(raised, key=lambda f: int(f[1:]))
    return (Check("ok", f"assigned {assigned}, stamped {assigned}; {len(reviews)} raw "
                        f"review(s) intact; {len(ids)} finding id(s) all in "
                        f"{stamp['findings_rel']}"),
            {"assigned": assigned, "raw": raws, "findings": ids})


_veto_module = None


def load_veto():
    """veto.py, imported from its file. Any failure to load it fails P10, never passes it."""
    global _veto_module
    if _veto_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sealgate_veto", VETO_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _veto_module = module
    return _veto_module


def check_p10(prereg_path, raw):
    """Alice's veto for the seal's CURRENT body. Union rule: one confirmed or disputed break (or
    an order-48 legacy reproduced break) refuses. Order 48b: a tampered break (BREAK_INVALID)
    passes like a break that did not reproduce."""
    try:
        vt = load_veto()
        entries = vt.read_ledger(VETOES_LEDGER)
        key = vt.seal_key(prereg_path)
    except Exception as exc:                      # fail closed: no module, no pass
        return Check("failed", f"cannot read the veto ledger with {VETO_PY}: "
                               f"{type(exc).__name__}: {exc}"), None
    problems = vt.verify_chain(entries)
    if problems:
        return Check("failed", f"{VETOES_LEDGER.name} chain is broken at line "
                               f"{problems[0][0]}: {problems[0][1]}"), None
    body = body_hash(raw)
    runs = [(n, rec) for n, _, rec in entries
            if rec and rec.get("kind") == "veto" and rec.get("seal") == key
            and rec.get("body_sha256") == body]
    if not runs:
        return Check("failed", f"Alice has not been run against this seal's current body "
                               f"({body[:12]}) in {VETOES_LEDGER.name} (run `veto.py run`; "
                               f"an edited seal needs a new run)"), None
    broken = [(n, rec) for n, rec in runs
              if rec.get("outcome") in ("BREAK_CONFIRMED", "BREAK_DISPUTED", "BREAK_REPRODUCED")]
    if broken:
        n, rec = broken[0]
        last = (rec.get("repro") or [{}])[0].get("last_line", "")
        where = f"{VETOES_LEDGER.name} line {n}: {last[:120]!r}"
        if rec.get("outcome") == "BREAK_DISPUTED":
            why = (rec.get("judge") or {}).get("why", "")
            return Check("failed", f"disputed: Alice and the judge disagree, the user decides "
                                   f"({where}; the judge: {str(why)[:160]!r})"), None
        if rec.get("outcome") == "BREAK_CONFIRMED":
            return Check("failed", f"Alice broke this seal, the break reproduced cleanly and the "
                                   f"cold judge confirmed it ({where}); one confirmed break "
                                   f"vetoes the seal"), None
        return Check("failed", f"Alice broke this seal and the break reproduced (order 48, "
                               f"legacy: {where}); one reproduced break vetoes the seal"), None
    passing = [(n, rec) for n, rec in runs
               if rec.get("outcome") in ("NO_BREAK", "BREAK_NOT_REPRODUCED", "BREAK_INVALID")]
    if not passing:
        whys = "; ".join(f"line {n}: {rec.get('outcome')} ({rec.get('why')})" for n, rec in runs)
        return Check("failed", f"no veto run for this body finished ({whys}); a run that did "
                               f"not finish is not a pass"), None
    bad = []
    for n, rec in reversed(passing):
        scratch = Path(str(rec.get("scratch", "")))
        files = rec.get("files") or {}
        moved = []
        for rel, digest in sorted(files.items()):
            f = scratch / rel
            if not f.is_file():
                moved.append(f"{rel} does not exist")
            elif sha256_bytes(f.read_bytes()) != digest:
                moved.append(f"{rel} changed after it was recorded")
        if not files:
            moved.append("the run recorded no files")
        if not moved:
            return (Check("ok", f"{rec.get('outcome')} at {VETOES_LEDGER.name} line {n} for body "
                                f"{body[:12]}; {len(files)} recorded file(s) intact; no reproduced "
                                f"break; chain intact"),
                    {"line": n, "outcome": rec.get("outcome"), "scratch": str(scratch),
                     "files": files})
        bad.append(f"line {n}: " + "; ".join(moved))
    return Check("failed", f"Alice's recorded files no longer hash as recorded ("
                           + " | ".join(bad) + ")"), None


def run_checks(prereg_path):
    path = Path(prereg_path)
    raw = read_raw(path, "pre-registration")
    text = raw.decode("utf-8", errors="replace")
    body = strip_stamp(text)                      # decoded, for parsing only
    results, data = {}, {}
    results["P1_REVIEW_STAMP"], stamp = check_p1(text, path.parent)
    results["P2_REVIEWER_NOT_AUTHOR"], data["author"] = check_p2(body, stamp)
    results["P3_FINDINGS_DISPOSITIONED"], data["dispositions"] = check_p3(stamp)
    results["P4_CONTROLS_CHECKLIST"], data["controls"] = check_p4(body)
    results["P5_BANDS_AND_STOP_CONDITIONS"], data["sections"] = check_p5(body)
    results["P6_CONTENT_HASH"], data["body_sha256"] = check_p6(text, raw)
    results["P7_STAMP_BLOCK_CANONICAL"], data["stamp_block"] = check_p7(
        text, results["P6_CONTENT_HASH"].status == "ok")
    results["P8_PREFLIGHT"], data["preflight"] = check_p8(body, path.parent)
    results["P9_ASSIGNED_REVIEWER"], data["review"] = check_p9(path, stamp)
    results["P10_ALICE_VETO"], data["veto"] = check_p10(path, raw)
    data["stamp"] = stamp
    data["body"] = body
    data["file_sha256"] = file_hash(raw)
    return results, data


def report(results, stream):
    for name in PRECONDITIONS:
        c = results[name]
        label = {"ok": "ok      ", "failed": "FAILED  ",
                 "not-evaluated": "not-evaluated "}[c.status]
        stream.write(f"{label} {name}: {c.detail}\n")


# ------------------------------------------------------------------- the verbs

def verb_check(args):
    results, _ = run_checks(args.prereg)
    failed = [n for n in PRECONDITIONS if results[n].status == "failed"]
    if failed:
        sys.stderr.write(f"REFUSED: {os.path.abspath(args.prereg)}\n")
        report(results, sys.stderr)
        sys.stderr.write(f"refused on {len(failed)} precondition(s): {', '.join(failed)}\n")
        log_refusal("check", args.prereg, EXIT_REFUSED, failed)
        return EXIT_REFUSED
    sys.stdout.write(f"OK: {os.path.abspath(args.prereg)} satisfies all ten preconditions\n")
    report(results, sys.stdout)
    sys.stdout.write("no token issued (this was `check`); run `token` to issue one\n")
    return EXIT_OK


def verb_stamp(args):
    path = Path(args.prereg)
    text = read_text(path, "pre-registration")
    if find_stamp_blocks(text):
        sys.stderr.write(
            f"REFUSED: {os.path.abspath(args.prereg)} already carries a review stamp.\n"
            "A stamp is a record, so this does not replace it. To re-stamp a corrected\n"
            "seal, remove the existing stamp block deliberately (leaving a git trace)\n"
            "and stamp again.\n")
        log_refusal("stamp", args.prereg, EXIT_REFUSED, ["STAMP_ALREADY_PRESENT"])
        return EXIT_REFUSED
    findings_path = path.parent / args.findings
    if not findings_path.is_file():
        sys.stderr.write(f"REFUSED: findings file {args.findings} does not exist "
                         f"(looked at {findings_path}). A stamp that points at nothing "
                         f"is not a review.\n")
        log_refusal("stamp", args.prereg, EXIT_REFUSED, ["FINDINGS_MISSING"])
        return EXIT_REFUSED
    findings_hash = sha256_bytes(findings_path.read_bytes())
    date = args.date or datetime.date.today().isoformat()
    block = build_stamp(args.reviewer, args.findings, findings_hash, date)
    joiner = "" if text.endswith("\n") else "\n"
    path.write_text(text + joiner + "\n" + block + "\n", encoding="utf-8")
    sys.stdout.write(f"stamped {os.path.abspath(args.prereg)}: reviewer {args.reviewer}, "
                     f"date {date}, findings {args.findings} ({findings_hash[:12]})\n")
    sys.stdout.write("the stamp is not a blessing; run `token` to see if it holds\n")
    return EXIT_OK


def verb_token(args):
    path = Path(args.prereg)
    token_path = path.parent / TOKEN_NAME
    if token_path.exists():
        sys.stderr.write(
            f"REFUSED: {token_path} already exists.\n"
            "Re-issuing would erase the commitment the first token made, which is the\n"
            "one thing this gate exists to keep. If the seal was legitimately corrected\n"
            "and re-stamped, remove the stale token deliberately, leaving a git trace.\n")
        log_refusal("token", args.prereg, EXIT_REFUSED, ["TOKEN_ALREADY_ISSUED"])
        return EXIT_REFUSED
    results, data = run_checks(args.prereg)
    failed = [n for n in PRECONDITIONS if results[n].status == "failed"]
    if failed:
        sys.stderr.write(f"REFUSED: no run token for {os.path.abspath(args.prereg)}\n")
        report(results, sys.stderr)
        sys.stderr.write(f"refused on {len(failed)} precondition(s): {', '.join(failed)}\n")
        log_refusal("token", args.prereg, EXIT_REFUSED, failed)
        return EXIT_REFUSED
    stamp = data["stamp"]
    token = {
        "schema": TOKEN_SCHEMA,
        "sealgate_version": VERSION,
        "issued_at": utc_now(),
        "prereg": os.path.abspath(args.prereg),
        "author_line": data["author"]["author_line"],
        "authors": data["author"]["authors"],
        "reviewer": stamp["reviewer"],
        "review_date": stamp["date"],
        "findings_path": stamp["findings_rel"],
        "findings_sha256": stamp["findings_sha256"],
        "n_findings": len(data["dispositions"]),
        "dispositions": data["dispositions"],
        "controls": data["controls"],
        "sections": data["sections"],
        "body_sha256": data["body_sha256"],
        "file_sha256": data["file_sha256"],
        "section_sha256": sections_of(data["body"]),
        "preconditions": {n: results[n].status for n in PRECONDITIONS},
        "preflight": data["preflight"],
        "review": data["review"],
        "veto": data["veto"],
    }
    token_path.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"RUN TOKEN ISSUED: {token_path}\n")
    report(results, sys.stdout)
    sys.stdout.write(f"body sha256 {token['body_sha256']}\n"
                     f"file sha256 {token['file_sha256']}\n"
                     f"run `verify` before reporting any result; if the seal moved, it fails\n")
    return EXIT_OK


def verb_verify(args):
    path = Path(args.prereg)
    token_path = path.parent / TOKEN_NAME
    if not token_path.exists():
        sys.stderr.write(f"DRIFT: no {TOKEN_NAME} beside {os.path.abspath(args.prereg)}; "
                         f"nothing was ever committed to, so nothing can be verified\n")
        log_refusal("verify", args.prereg, EXIT_DRIFT, ["NO_TOKEN"])
        return EXIT_DRIFT
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"cannot read {token_path}: {exc}\n")
        return EXIT_USAGE

    # An older token records no whole-file hash, so an edit inside the stamp block
    # would be invisible to this check. Refuse it. Falling back to body-only
    # verification would be the same defect wearing a version number.
    schema = token.get("schema")
    if schema != TOKEN_SCHEMA or not token.get("file_sha256"):
        sys.stderr.write(
            f"CANNOT VERIFY: {token_path}\n"
            f"the token's schema is {schema!r}, and this gate verifies {TOKEN_SCHEMA!r}.\n"
            f"An older token carries no whole-file hash, so an edit inside the stamp block\n"
            f"would be invisible here — the defect this schema exists to close. There is no\n"
            f"fall back to body-only verification. Re-issue the token against the current\n"
            f"seal, and treat any result already reported under the old token as unverified.\n")
        log_refusal("verify", args.prereg, EXIT_DRIFT, [f"TOKEN_SCHEMA:{schema}"])
        return EXIT_DRIFT

    raw = read_raw(path, "pre-registration")
    text = raw.decode("utf-8", errors="replace")
    body_now, file_now = body_hash(raw), file_hash(raw)
    body_moved = body_now != token.get("body_sha256")
    file_moved = file_now != token.get("file_sha256")

    drift = []
    if body_moved:
        header = "DRIFT"
        drift.append(f"the sealed body changed after the token was issued "
                     f"(body sha256 {token.get('body_sha256', '')[:12]} -> {body_now[:12]})")
        now_sections = sections_of(strip_stamp(text))
        was = token.get("section_sha256", {})
        for k, v in now_sections.items():
            if k not in was:
                drift.append(f"  section added:   {k}")
            elif was[k] != v:
                drift.append(f"  section edited:  {k}")
        for k in was:
            if k not in now_sections:
                drift.append(f"  section removed: {k}")
    elif file_moved:
        header = "STAMP REGION CHANGED"
        drift.append(f"the sealed body is intact, but the stamp region was edited after the "
                     f"token was issued (file sha256 {token.get('file_sha256', '')[:12]} -> "
                     f"{file_now[:12]})")
        drift.append("content inside the stamp block is NOT covered by the body hash, so it "
                     "can be a post-hoc addition that reads as part of the seal")
        drift.append("the file on disk is NOT the file that was sealed, and must not be "
                     "reported as unchanged")
    else:
        header = "DRIFT"

    stamp = parse_stamp(text)
    if stamp is None:
        drift.append("the review stamp has been removed from the pre-registration")
    else:
        if stamp.get("reviewer", "") != token.get("reviewer"):
            drift.append(f"the stamp's reviewer changed: {token.get('reviewer')!r} -> "
                         f"{stamp.get('reviewer')!r}")
        if stamp.get("date", "") != token.get("review_date"):
            drift.append(f"the stamp's date changed: {token.get('review_date')!r} -> "
                         f"{stamp.get('date')!r}")
        if stamp.get("findings", "") != token.get("findings_path"):
            drift.append(f"the stamp's findings file changed: {token.get('findings_path')!r} "
                         f"-> {stamp.get('findings')!r}")
    findings_path = path.parent / token.get("findings_path", "")
    if not findings_path.is_file():
        drift.append(f"the findings file {token.get('findings_path')} no longer exists")
    else:
        now = sha256_bytes(findings_path.read_bytes())
        if now != token.get("findings_sha256"):
            drift.append(f"the findings file {token.get('findings_path')} changed after the "
                         f"token was issued ({token.get('findings_sha256', '')[:12]} -> "
                         f"{now[:12]})")

    if drift:
        sys.stderr.write(f"{header}: {os.path.abspath(args.prereg)} is not what was sealed\n")
        for d in drift:
            sys.stderr.write(d + "\n")
        sys.stderr.write("a seal that can be revised to match the result is not a seal\n")
        log_refusal("verify", args.prereg, EXIT_DRIFT,
                    ["STAMP_REGION" if header == "STAMP REGION CHANGED" else "DRIFT"])
        return EXIT_DRIFT

    # Only reachable when the WHOLE FILE hashes to what was sealed.
    sys.stdout.write(f"INTACT: {os.path.abspath(args.prereg)} is byte-for-byte what the token "
                     f"was issued against\n"
                     f"  file sha256     {token['file_sha256']}\n"
                     f"  body sha256     {token['body_sha256']}\n"
                     f"  reviewer        {token['reviewer']} ({token['review_date']})\n"
                     f"  findings        {token['findings_path']} "
                     f"({token['findings_sha256'][:12]})\n"
                     f"  token issued at {token['issued_at']}\n")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sealgate.py",
        description="A run cannot be blessed until its seal has survived a reviewer who is "
                    "not its author. There is no way to waive a precondition.")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("check", help="are the ten preconditions satisfied? (issues nothing)")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_check)

    p = sub.add_parser("stamp", help="record a review stamp on a pre-registration")
    p.add_argument("prereg")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--findings", required=True,
                   help="path to the findings file, relative to the pre-registration")
    p.add_argument("--date", default=None, help="ISO date (default: today)")
    p.set_defaults(fn=verb_stamp)

    p = sub.add_parser("token", help="issue RUN_TOKEN.json, or refuse")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_token)

    p = sub.add_parser("verify", help="has the seal drifted since the token was issued?")
    p.add_argument("prereg")
    p.set_defaults(fn=verb_verify)

    args = parser.parse_args(argv)
    if not Path(args.prereg).is_file():
        sys.stderr.write(f"no such pre-registration: {args.prereg}\n")
        return EXIT_USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
