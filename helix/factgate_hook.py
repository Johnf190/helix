#!/usr/bin/env python3
"""factgate_hook.py — prevention for the fact base, on Odin's ordinary write path.

`factgate` (workspace/factgate/) is the detector: the nightly says `- facts: OK (196
recorded)` or names what moved off-ledger. On its own that means every fact Odin writes
shows up red until a human records it. This closes the loop on the path Odin actually
uses — the SDK's tools — so that:

  * a fact write is REFUSED while the fact base is not clean,
  * a fact write that succeeds is RECORDED automatically, with the tool and turn in the
    reason,
  * the ways round it that can be seen are refused: Bash writes into `memory/facts/`,
    and any Odin write to factgate itself (he may not edit his own checks).

🔴 **What this does NOT close.** A Python one-liner (`python3 -c "open(...,'w')…"`), a
script that writes the file, an editor launched from Bash, `xargs`, a heredoc — none of
those match a pattern list, and no pattern list will ever be complete. Prevention here
is a fence with a gate in it, not a wall. **The nightly's `factgate check` is the
backstop, and it is the thing that actually guarantees detection.** Do not read this
file as making the fact base tamper-proof.

🔴 **The lock-out is deliberate.** While `factgate check` is dirty, Odin cannot write
ANY fact — and because P5 refuses him `factgate record`, he cannot clear it himself.
Only a human running `record` can. That is the point: an agent that can bless its own
unexplained edits has no gate at all. Every deny says so in one sentence, so Odin
reports the refusal rather than retrying it.

Fail CLOSED on the fact path (anything unexpected → deny), fail OPEN everywhere else (a
bug here must never stop Odin running `ls`). Neither hook raises into the SDK.

ORDER 15 — P6. `/build NN` and `/restart` are the user's, typed in Helix's chat and handled by
app.py. Odin may not reach the same machinery from his shell: a Bash command that names
`run_order.sh`, `run_queue.sh`, `systemd-run`, `start_helix.sh`, `helix-builder` or
`nightly_restart.sh` (ORDER 31), or that sends `/build` or `/restart` to his own port 7777, is
refused. Reading those files (`cat`, `grep`, …) is still fine. P3 also guards `run_order.sh`
from the editor tools.
ORDER 23b: a command that names one and is not all reads is refused only if it RUNS one — as
the program, as the script a shell is handed, by feeding a shell, or by a systemctl verb that is
not a status read. A read of a name next to an unrelated part (catches #45, #46) is not a run.

🔴 **Honest scope of P6: string rules are a tripwire, not a wall.** An obfuscated command —
a name assembled from variables, an encoded string, a script written to disk and run later,
a different spelling of the host — gets past them. The backstops are: `RUNS.jsonl` records
the trigger of every run, the user sees every `/build` in chat, and item 25 (the kernel write
wall) is the real wall.

ORDER 16 — P7. The build path — app.py (where this hook is wired in), start_helix.sh, the
protocol, run_order.sh, builder_run.sh, and build_orders/reports/, runs/, quarantine/, .git/,
plus (ORDER 23c) wall_exec.sh, (ORDER 31) nightly_restart.sh and ~/helix/.lkg/, (ORDER 33b)
memory/PINNED.txt, (ORDER 34) the memory patch ledger workspace/cycle/PATCHES.jsonl, (ORDER 36) the gap
audit's opt-out workspace/cycle/NO_RECORD_NEEDED.jsonl (else Odin could wave his own gaps away), (ORDER 38)
housekeeping's workspace/cycle/housekeeping_enabled.flag (the user's, not Odin's) and its ledger
workspace/cycle/MOVES.jsonl, (ORDER 62b) the cycle ledgers RESTARTS, TURNS, ATTEMPTS and GPU.jsonl in
workspace/cycle/ (Helix's own processes write them; cycle.py and its tests stay Odin's), (ORDER 54d)
build_orders/tools/, where the order lint lives (it checks
Odin's orders, so Odin's tools cannot change it), and (ORDER 35b)
memory/archive/, where only the user's `factgate archive`/`restore` move facts — changes only through a build order the user starts. One list, `_build_path_owns`, is used by the editor tools AND by the Bash write
test, so the two cannot drift. Reading any of it stays allowed. Odin's own files stay writable:
ORDER_*.md, NEXT.md, reports/*_review_odin.md, and everything in workspace/.

🔴 **Honest scope of P7: like P6, a tripwire against Odin's ordinary tools, not a wall.** A
Python one-liner or a script written elsewhere and run later still gets past it. Item 25 (the
kernel write wall) is the wall.

ORDER 62b (P-20261004-9). Python code the command hands python (`-c`, or a heredoc python reads its
program from) is judged by what it WRITES, resolved, not by what it mentions — see _py_parts.

There is no way round this: no flag, no environment variable, no special mode. The grep
in the work order is meant to come back empty, so the words it looks for are kept out of
this file entirely rather than appearing in a sentence that denies them.
"""

import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

BRAIN = os.path.dirname(os.path.abspath(__file__))
FACTS = f"{BRAIN}/memory/facts"
FACTGATE_DIR = f"{BRAIN}/workspace/factgate"
FACTGATE = f"{FACTGATE_DIR}/factgate.py"
FACTGATE_STATE = f"{FACTGATE_DIR}/state"
CATCHES = f"{BRAIN}/workspace/catches/CATCHES.jsonl"
TIMEOUT = 10                       # a hung check must not hang Odin's turn

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
PATH_KEYS = ("file_path", "notebook_path", "path")

# Bash fragments that write.
#
# ORDER 13. A bare `>` used to count as a write anywhere in the command. It made
# `2>&1` and `2>/dev/null` look like writes, which refused four read-only commands in
# the hook's first 30 minutes live — including Odin reading his own ledger. A redirect
# is now judged by WHERE IT POINTS (see _guarded_redirect), and only these words still
# count on a bare mention of a guarded path: their targets are too varied to parse
# safely, and none of the measured false positives came from them.
_WRITE_WORDS = ("tee", "mv", "cp", "rm", "truncate", "touch", "ln", "chmod", "install", "dd")
_WRITE_WORD_RE = re.compile(r"(\bsed\s+-i)|(\bperl\s+-[^\s]*i)|\b("
                            + "|".join(_WRITE_WORDS) + r")\b")
_TOKEN_RE = re.compile(r"[~$\w./\\-]+")

# Redirects that write nowhere a guard cares about: fd duplications (2>&1, 1>&2, 2>&-)
# and anything aimed at /dev/null. Stripped before the rest is examined.
_FD_DUP_RE = re.compile(r"(?:\d+|&)?>&\s*(?:\d+-?|-)")
_DEVNULL_RE = re.compile(r"(?:\d+|&)?>>?\|?\s*(?:'/dev/null'|\"/dev/null\"|/dev/null)\b")
# A target this program cannot resolve: a variable, a substitution, a glob.
_UNRESOLVABLE_RE = re.compile(r"[$`*?\[]")

# factgate the TOOL, not the word inside a longer identifier: `factgate.py` and
# `factgate/` count; `factgate_hook`, `test_factgate_hook`, `139_example_d22c` do not.
_FACTGATE_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])factgate(?![A-Za-z0-9_])")
# Its recording verbs, as words. ORDER 22b: `baseline-legacy` and `release` added — the
# `-legacy` suffix used to slip past the lookahead. Releasing a real fact is the user's, in his
# own terminal. ORDER 35b: `archive` and `restore` move facts in and out of memory/archive/;
# they are the user's too.
_RECORDING_VERBS = ("record-delete", "record", "baseline-legacy", "baseline", "release",
                    "archive", "restore")
_RECORDING_VERB_RE = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(_RECORDING_VERBS)
                                + r")(?![A-Za-z0-9_-])")
_FACTGATE_PROGS = ("factgate.py", "factgate")
# Interpreter options that take the next word as their value (python -W x, -X y).
_P5_PY_OPT_ARGS = ("-W", "-X", "-Q")

_CLEARED_BY = ("Only a human running `factgate record` can clear this — you cannot, and "
               "retrying will not help. Report the refusal.")

# ORDER 15 — P6. The builder runner and the restart path.
RUNNER = f"{BRAIN}/build_orders/run_order.sh"
_P6_WHY = "Builds and restarts start on the user's word in chat, not from Odin's shell."
_P6_NAMES_RE = re.compile(r"run_order\.sh|run_queue\.sh|systemd-run|start_helix\.sh|helix-builder"
                          r"|nightly_restart\.sh")      # ORDER 31
_P6_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost):7777")
_P6_COMMAND_RE = re.compile(r"/(?:build|restart)(?![\w-])")
# A mention of the runner is fine when every part of the command only READS. Anything else —
# an unknown verb, a pipe into a shell, a substitution — counts as starting it.
#
# ORDER 16. Read-only verbs added after catches #24, #25, #27, #28. Deliberately NOT here, because
# each can run code: awk, sed, xargs, env, bash, sh, python*, find (-exec), watch, nohup,
# timeout, setsid, sudo.
_P6_READERS = ("cd", "cat", "head", "tail", "grep", "wc", "sha256sum", "md5sum", "ls",
               "stat", "file", "diff",
               "echo", "printf", "cut", "sort", "uniq", "tr", "nl", "ps", "pgrep",
               "basename", "dirname", "readlink", "realpath", "true", "date", "zcat")
# ORDER 23b. Reads that need a look at their arguments (catches #45, #46). awk and sed stay off
# _P6_READERS, because their other forms run code.
_P6_CODE_VERBS = ("awk", "sed", "find", "crontab", "at", "batch")
_P6_AWK_RUNS_RE = re.compile(r"system\s*\(|\||getline")
_P6_AWK_OPT_ARGS = ("-F", "-v")
# ORDER 23b (ruling 3). A P6 PROGRAM as an argument of a part that is not a read: a wrapper the
# parser does not know (ssh, strace, flock, tmux new …) would run it.
_P6_PROGRAMS = ("run_order.sh", "run_queue.sh", "start_helix.sh", "systemd-run",
                "nightly_restart.sh")                          # ORDER 31
_P6_SPLIT_RE = re.compile(r"&&|\|\||[;|&\n]")
_P6_NOT_A_READ_RE = re.compile(r"[>`]|\$\(|<\(")
# ORDER 23b (ruling 5). Inside double quotes `$(` and a backtick still run; `>` and `<(` do not.
_P6_DQ_LIVE_RE = re.compile(r"`|\$\(")
# ORDER 16. A quoted string is one argument, so a `|` or `;` inside it does not start a new part
# (catches #24 and #28 were refused for `grep "a\|b"`). It is replaced by a placeholder that is no
# reader, so a quoted VERB (`"bash" run_order.sh`) still fails. Unterminated quotes are left alone.
_P6_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')
# ORDER 18. systemctl is NOT a reader — it starts and stops units. A systemctl part counts as a
# read only when every option is on this list and its verb (the first non-option word) only reads
# (catch #31 was `systemctl --user list-units 'helix-builder-*' --all --no-pager`). Anything else —
# another verb, no verb, an unknown option — is not a read.
_P6_SYSTEMCTL_VERBS = ("status", "list-units", "list-timers", "list-unit-files", "show",
                       "is-active", "is-failed", "is-enabled", "cat")
_P6_SYSTEMCTL_FLAGS = ("--user", "--all", "-a", "--no-pager", "--no-legend", "--plain", "--full",
                       "-l", "--value")
_P6_SYSTEMCTL_TAKES_VALUE = ("-o", "-p", "-n")
_P6_SYSTEMCTL_VALUE_RE = re.compile(r"--(?:type|state|output|property)=.*|--lines=\d+")

# ORDER 16 — P7. The build path.
BUILD_ORDERS = f"{BRAIN}/build_orders"
_P7_WHY = ("The build path (app, launcher, protocol, runner, reports) changes only through a "
           "build order that the user starts.")
_P7_FILES = (f"{BRAIN}/app.py", f"{BRAIN}/start_helix.sh", f"{BUILD_ORDERS}/BUILD_PROTOCOL.md",
             RUNNER, f"{BUILD_ORDERS}/builder_run.sh",
             f"{BRAIN}/wall_exec.sh",         # ORDER 23c: the write wall's wrapper, written by app.py
             f"{BRAIN}/nightly_restart.sh",   # ORDER 31: the nightly restart
             f"{BRAIN}/memory/PINNED.txt",    # ORDER 33b: the loader's pin list (the user's / a build order's)
             f"{BRAIN}/workspace/cycle/PATCHES.jsonl",   # ORDER 34: the memory patch ledger (app.py appends)
             f"{BRAIN}/workspace/cycle/NO_RECORD_NEEDED.jsonl",   # ORDER 36: the gap audit's opt-out
             f"{BRAIN}/workspace/cycle/housekeeping_enabled.flag",   # ORDER 38: the user's go for apply
             f"{BRAIN}/workspace/cycle/MOVES.jsonl",   # ORDER 38: every housekeeping move
             # ORDER 62b: the cycle ledgers Helix's own processes write (app.py, nightly_restart.sh,
             # gpu_sample.py under its systemd timer). Files, not the directory: cycle.py is Odin's.
             f"{BRAIN}/workspace/cycle/RESTARTS.jsonl", f"{BRAIN}/workspace/cycle/TURNS.jsonl",
             f"{BRAIN}/workspace/cycle/ATTEMPTS.jsonl", f"{BRAIN}/workspace/cycle/GPU.jsonl")
_P7_DIRS = (f"{BUILD_ORDERS}/runs", f"{BUILD_ORDERS}/quarantine", f"{BUILD_ORDERS}/.git",
            f"{BUILD_ORDERS}/reports",
            f"{BRAIN}/.lkg",                   # ORDER 31: the rollback's last known good app.py
            f"{BRAIN}/memory/archive",         # ORDER 35b: archived facts (factgate archive/restore)
            f"{BUILD_ORDERS}/tools")           # ORDER 54d: the order lint (it checks Odin's orders)
_P7_REVIEW_SUFFIX = "_review_odin.md"          # Odin's reviews live in reports/
_CATCH_COMMAND_MAX = 4000


# ------------------------------------------------------------------- small parts

# ORDER 18 — B. One pre_tool_use call resolves the same guarded roots once per token (81% of the
# time on a large heredoc). Within ONE call nothing on disk changes, so its answers are memoised
# for that call only; the memo is dropped when the call returns, so a later symlink is still seen.
_real_memo = None


def _real(path):
    if _real_memo is None:
        return os.path.realpath(os.path.expandvars(os.path.expanduser(str(path))))
    key = str(path)
    if key not in _real_memo:
        _real_memo[key] = os.path.realpath(os.path.expandvars(os.path.expanduser(key)))
    return _real_memo[key]


def _inside(path, root):
    root = _real(root)
    path = _real(path)
    return path == root or path.startswith(root + os.sep)


def _targets(tool_input):
    """Every path this tool write is aimed at."""
    if not isinstance(tool_input, dict):
        return []
    return [tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str)]


def _resolve(path, cwd):
    p = os.path.expandvars(os.path.expanduser(str(path)))
    if not os.path.isabs(p):
        p = os.path.join(str(cwd or ""), p)
    return os.path.realpath(p)


def _mentions(command, cwd, root):
    """Does this command name a path inside `root`, however it spells it?"""
    root = _real(root)
    tail = os.sep.join(root.split(os.sep)[-2:])          # e.g. "memory/facts"
    if tail and tail in command:
        return True
    for tok in _TOKEN_RE.findall(command):
        try:
            if _inside(_resolve(tok, cwd), root):
                return True
        except Exception:
            continue
    return False


def _redirect_targets(command):
    """Every redirect target in the command, after the harmless ones are dropped.

    `None` means the target could not be read (missing, or a variable/glob) — the
    caller treats that as a write if the command names a guarded path, so a parsing
    gap can never open a hole.
    """
    s = _DEVNULL_RE.sub(" ", _FD_DUP_RE.sub(" ", command))
    out, i = [], 0
    while i < len(s):
        if s[i] != ">":
            i += 1
            continue
        j = i + 1
        if j < len(s) and s[j] == ">":
            j += 1
        if j < len(s) and s[j] == "|":
            j += 1
        while j < len(s) and s[j] in " \t":
            j += 1
        if j < len(s) and s[j] in "'\"":
            quote = s[j]
            end = s.find(quote, j + 1)
            out.append(s[j + 1:end] if end != -1 else s[j + 1:])
            j = end + 1 if end != -1 else len(s)
        else:
            end = j
            while end < len(s) and s[end] not in " \t;|&<>()":
                end += 1
            out.append(s[j:end] or None)
            j = end
        i = j
    return out


def _guarded_redirect(command, cwd, root):
    """Does any redirect in this command land inside `root`?"""
    for target in _redirect_targets(command):
        if target is None or _UNRESOLVABLE_RE.search(target):
            if _mentions(command, cwd, root):      # cannot tell -> refuse
                return True
            continue
        try:
            if _inside(_resolve(target, cwd), root):
                return True
        except Exception:
            if _mentions(command, cwd, root):
                return True
    return False


def _factgate_files():
    """The hook's own two files. They live in ~/helix, NOT in workspace/factgate/."""
    return [os.path.join(BRAIN, "factgate_hook.py"),
            os.path.join(BRAIN, "test_factgate_hook.py")]


def _factgate_paths():
    """What "touching factgate" means: its directory, and the hook's own two files.

    The hook files live outside workspace/factgate/, so narrowing the mention test to
    the tool alone would have left `rm ~/helix/factgate_hook.py` allowed. Order 13 says
    keep the refusal when narrowing would open a hole.
    """
    return [FACTGATE_DIR] + _factgate_files()


def _factgate_owns(path):
    """Is this path something factgate owns — its folder, or the hook's own files?

    ORDER 14. P3 used to test FACTGATE_DIR alone, so an `Edit` of ~/helix/factgate_hook.py
    was ALLOWED: a more direct way to switch the gate off than any shell command, open
    since order 12. Bash has judged by _factgate_paths() since order 13; the editor tools
    now use the same list, so the two cannot drift apart again. Resolution goes through
    _real, so a symlink, a relative path or a `~`/$HOME spelling all land on the same file.
    """
    real = _real(path)
    if _inside(real, FACTGATE_DIR):
        return True
    return any(real == _real(f) for f in _factgate_files())


def _about_factgate(command, cwd):
    """Is this command about factgate at all?"""
    if _FACTGATE_WORD_RE.search(command):
        return True
    return any(_mentions(command, cwd, p) for p in _factgate_paths())


def _p5_code_runs(code):
    """Code we cannot parse (python -c, a heredoc): the old word rule, factgate + a verb."""
    return bool(_FACTGATE_WORD_RE.search(code) and _RECORDING_VERB_RE.search(code))


def _p5_script(verb, args):
    """(script, its arguments) for `python3 [opts] script args…` / `bash script args…`."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "-m") or (verb.startswith("python") and a == "-"):
            return None, []
        if a in _P5_PY_OPT_ARGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return None, []


def _runs_factgate_recording(command, depth=0, cwd=None):
    """ORDER 22b — P5 (b). Does this command RUN factgate.py with a recording verb?

    factgate.py counts only as the program, or as the script an interpreter is handed; then a
    recording verb anywhere in its arguments refuses. A grep pattern or a path that is only
    read is not running it (catch #42). What cannot be parsed is judged by the old word rule:
    `-c` code, heredoc bodies, and a program or script hidden behind a variable. ORDER 55: a data
    heredoc's body (`cat > f <<EOF`, judged as P7 judges it) is data, not code (catch #51).
    """
    if depth > 4:
        return _p5_code_runs(command)
    shell, docs = _p7_heredocs(command)
    data = {d["pos"] for d in _p7_parse(command, cwd)[1]}
    py = _py_parts(command, cwd) if depth == 0 else _PY_NONE     # ORDER 62b
    if any(_p5_code_runs(d["body"]) for d in docs if d["pos"] not in data | py["docs"]):
        return True
    for k, (start, end, _) in enumerate(_p7_segments(shell)):
        if k in py["segs"]:
            continue                                     # ORDER 62b: cleared python runs nothing
        text = shell[start:end]
        words, _, subs = _p7_words(text)
        if any(_runs_factgate_recording(sub, depth + 1, cwd) for sub in subs):
            return True
        for verb, args, _, word in _p7_commands(words):
            run_args = None
            if os.path.basename(word) in _FACTGATE_PROGS:
                run_args = args
            elif verb in _P7_SHELLS or _P7_OTHER_CODE_RE.match(verb):
                for code in _p7_code_args(verb, args):
                    if (_runs_factgate_recording(code, depth + 1, cwd) if verb in _P7_SHELLS
                            else _p5_code_runs(code)):
                        return True
                script, rest = _p5_script(verb, args)
                if script and os.path.basename(script) in _FACTGATE_PROGS:
                    run_args = rest
                elif script and _P7_DYNAMIC_RE.search(script) and _p5_code_runs(command):
                    return True
            elif _P7_DYNAMIC_RE.search(word) and _p5_code_runs(command):
                return True
            elif verb not in _P6_READERS and not _p6_reads_too([verb] + args) \
                    and any(os.path.basename(a) in _FACTGATE_PROGS for a in args):
                run_args = args                          # ORDER 23b: ssh, strace, flock, tmux …
            if run_args is not None and any(a in _RECORDING_VERBS for a in run_args):
                return True
    return False


def _writes_factgate(command, cwd):
    """Does it write to anything factgate owns? ORDER 55: judged on _p5_effect_text, so a `>` in a
    quoted echo argument (catch #38), a write word in a grep pattern (#65) or a data heredoc's body
    (#51), or `pip install` (#72) is not a write. A target that cannot be read still falls back to
    a mention anywhere in the whole command, as before. ORDER 62b: a cleared python part's resolved
    targets are judged here."""
    if any(_py_lands(t, _factgate_paths(), _factgate_owns) for t in _py_parts(command, cwd)["targets"]):
        return True
    text = _p5_effect_text(command, cwd)
    for p in _factgate_paths():
        for target in _redirect_targets(text):
            if target is None or _UNRESOLVABLE_RE.search(target):
                if _mentions(command, cwd, p):
                    return True
            elif _inside(_resolve(target, cwd), p):
                return True
        if _WRITE_WORD_RE.search(text) and _mentions(command, cwd, p):
            return True
    return False


def _p5_effect_text(command, cwd):
    """ORDER 55: the command as the P5 write test sees it. A part that is inert (_p5_inert) keeps
    only its verb and its redirects, each target resolved from the cwd that part runs in; every
    other part is kept whole; a data heredoc's body is dropped and a code heredoc's body kept."""
    segs, _data, code = _p7_parse(command, cwd)
    py = _py_parts(command, cwd)                         # ORDER 62b: cleared python is kept as inert
    out = []
    for k, s in enumerate(segs):
        if _p5_inert(segs, k) or k in py["segs"]:
            parts = [s["cmds"][0][0]]
            for t in s["targets"]:
                if t is None or _P7_DYNAMIC_RE.search(t) or (
                        s["cwd"] is None and not t.startswith(("/", "~"))):
                    parts.append(">")                    # unreadable: the caller's fallback
                else:
                    parts.append("> " + json.dumps(_resolve(t, s["cwd"])))
            out.append(" ".join(parts))
        else:
            out.append(s["text"])
        out.append(" " + (s["sep"] or ""))
    return "".join(out) + "\n" + "\n".join(b for b, _w, _l, pos in code if pos not in py["docs"])


def _p5_inert(segs, k):
    """ORDER 55: are this part's words only data? Yes for one reader (_P6_READERS) whose
    substitutions only read; and for one plain program — not a write word, a shell, an interpreter,
    sed, awk, find, xargs or anything that runs code — that has no variable, substitution or glob,
    is not fed by a pipe, runs from a known cwd outside factgate, and names nothing factgate owns
    (`pip install pyflakes`). Either way its output may go only into readers. Its redirects are
    never data."""
    s = segs[k]
    if len(s["cmds"]) != 1:
        return False
    verb, args, xargs, word = s["cmds"][0]
    if xargs:
        return False
    j = k
    while segs[j]["sep"] in ("|", "|&"):
        j += 1
        if j >= len(segs) or not segs[j]["cmds"] or any(
                v not in _P6_READERS or x for v, _a, x, _w in segs[j]["cmds"]):
            return False
    if verb in _P6_READERS:
        return all(_only_reads(sub) for sub in s["subs"])
    fed = k > 0 and segs[k - 1]["sep"] in ("|", "|&")
    runs_code = (verb in _WRITE_WORDS or verb in _P7_SHELLS or verb in ("source", ".", "sed", "perl",
                 "find", "systemctl") or verb in _P6_CODE_VERBS or _P7_OTHER_CODE_RE.match(verb))
    return not (runs_code or fed or s["subs"] or s["cwd"] is None
                or any(_P7_DYNAMIC_RE.search(w) for w in s["words"])
                or any(_inside(s["cwd"], p) for p in _factgate_paths())
                or _about_factgate(s["text"], s["cwd"]))


def _systemctl_reads(words):
    """ORDER 18: is this `systemctl …` part a status read? See _P6_SYSTEMCTL_VERBS."""
    verb, i = None, 1
    while i < len(words):
        w = words[i]
        if w in _P6_SYSTEMCTL_TAKES_VALUE:
            if i + 1 >= len(words) or (w == "-n" and not words[i + 1].isdigit()):
                return False
            i += 2
            continue
        if w.startswith("-"):
            if w not in _P6_SYSTEMCTL_FLAGS and not _P6_SYSTEMCTL_VALUE_RE.fullmatch(w):
                return False
        elif verb is None:
            verb = w
        i += 1
    return verb in _P6_SYSTEMCTL_VERBS


def _p6_reads_too(words):
    """ORDER 23b: `crontab -l`, `sed -n` without -i/-f, and awk without -f whose program has no
    system(, pipe or getline. A P6 name inside a sed script is not a read (sed's `e` runs it)."""
    verb, args = words[0], words[1:]
    if verb == "crontab":
        return args == ["-l"]
    shorts = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if verb == "sed":
        if "i" in shorts or "f" in shorts or any(a.startswith(("--in-place", "--file"))
                                                 for a in args):
            return False
        if not ("n" in shorts or "--quiet" in args or "--silent" in args):
            return False
        ops = [a for a in args if not a.startswith("-")]
        scripts = ([args[i + 1] for i, a in enumerate(args[:-1]) if a == "-e"]
                   or ops[:1])
        return not any(_P6_NAMES_RE.search(x) for x in scripts)
    if verb == "awk":
        if "f" in shorts or any(a.startswith("--file") for a in args):
            return False
        if any(_P6_AWK_RUNS_RE.search(a) for a in args):
            return False
        i = 0
        while i < len(args) and args[i].startswith("-") and args[i] != "-":
            i += 2 if args[i] in _P6_AWK_OPT_ARGS else 1
        return i < len(args) and not _P6_NAMES_RE.search(args[i])   # ruling 4: the program
    return False


def _only_reads(command):
    """Is every part of this command a plain read? Redirects, substitutions and any verb
    not on the reader list say no — so `cat run_order.sh | bash` is not a read."""
    s = _DEVNULL_RE.sub(" ", _FD_DUP_RE.sub(" ", command))
    quoted, live = [], []

    def hide(m):
        q = m.group(0)
        if q.startswith('"') and _P6_DQ_LIVE_RE.search(q):
            live.append(q)
        quoted.append(q[1:-1])
        return f" \0{len(quoted) - 1}\0 "

    s = _P6_QUOTED_RE.sub(hide, s)
    if live or _P6_NOT_A_READ_RE.search(s):              # ORDER 23b: quotes hidden first
        return False
    for part in _P6_SPLIT_RE.split(s):
        words = part.split()
        if not words or words[0] in _P6_READERS:
            continue
        if words[0] == "systemctl" and _systemctl_reads(words):
            continue
        if words[0].startswith("\0"):
            return False                                 # a quoted verb is no reader
        full = [quoted[int(w[1:-1])] if w.startswith("\0") and w.endswith("\0") else w
                for w in words]
        if not _p6_reads_too(full):
            return False
    return True


def _p6_unbalanced(shell):
    """ORDER 23b (ruling 4): does a quote in this shell text never close?"""
    i, n = 0, len(shell)
    while i < n:
        c = shell[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and shell[j] != c:
                j += 2 if c == '"' and shell[j] == "\\" else 1
            if j >= n:
                return True
            i = j + 1
            continue
        i += 1
    return False


def _p6_word_rule(code):
    """The old rule, for code that cannot be parsed: a P6 name in it, and it is not all reads."""
    return bool(_P6_NAMES_RE.search(code)) and not _only_reads(code)


def _p6_find_split(args):
    """ORDER 39: (find's own words, [each -exec/-execdir/-ok/-okdir command up to `;` or `+`])."""
    own, bodies, i = [], [], 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            body, i = [], i + 1
            while i < len(args) and args[i] not in (";", "+"):
                body.append(args[i])
                i += 1
            bodies.append(body)
        else:
            own.append(args[i])
        i += 1
    return own, bodies


def _p6_runs(command, depth=0):
    """ORDER 23b. In a command that names a P6 name and is not all reads: does a part RUN one?

    Refused: a P6 name as the program (past sudo/env/nohup/exec/…), or as an argument of a
    shell, `source`, `.` or an interpreter; a shell fed by a pipe, by `-s`, or by stdin; xargs;
    a systemctl part that is not a status read; a P6 name among a prefix's words (timeout's
    duration slot); a part that is not a read with a P6 program as an argument (ssh, strace,
    flock, tmux new …). What cannot be parsed keeps the old word rule: `-c`/`-e` code, eval,
    heredoc bodies, an unbalanced quote, a program behind a variable or a glob, a command
    string handed to a wrapper, and verbs that run code we do not parse (awk and sed outside
    their read forms, crontab other than -l, at, batch). ORDER 39: `find` is judged by ruling 3 on
    its own words, and each command after its -exec/-execdir/-ok/-okdir like any other part
    (catch #65); `-exec {}` or a hidden program there is a run.
    """
    if depth > 4:
        return _p6_word_rule(command)
    shell, docs = _p7_heredocs(command)
    if any(_p6_word_rule(d["body"]) for d in docs):
        return True
    if _p6_unbalanced(shell):
        return _p6_word_rule(command)                    # ruling 4: unparseable
    fed = False
    for start, end, sep in _p7_segments(shell):
        text = shell[start:end]
        words, _, subs = _p7_words(text)
        if any(_p6_runs(sub, depth + 1) for sub in subs):
            return True
        cmds = list(_p7_commands(words))
        lead = words[:len(words) - len(cmds[0][1]) - 1] if cmds else words
        if any(_P6_NAMES_RE.search(w) for w in lead):
            return True                                  # ruling 4: in a prefix's words
        for verb, args, xargs, word in cmds:
            if xargs:
                return True
            if word in ("[", "[[", "test"):
                continue
            if _P7_DYNAMIC_RE.search(word) or any(c.isspace() for c in word):
                return True
            if _P6_NAMES_RE.search(verb):
                return True
            if verb == "systemctl":
                if not _systemctl_reads([verb] + args):
                    return True
                continue
            if verb == "find":                           # ORDER 39: its -exec commands are judged
                own, bodies = _p6_find_split(args)       # as parts of their own (yielded above)
                if any(not b or _P7_DYNAMIC_RE.search(b[0]) for b in bodies) \
                        or any(os.path.basename(a) in _P6_PROGRAMS for a in own):
                    return True                          # `-exec {}`, or ruling 3 on find's own words
                continue
            if verb in _P6_CODE_VERBS and not _p6_reads_too([verb] + args):
                return True
            if verb not in _P6_READERS and not _p6_reads_too([verb] + args) \
                    and any(os.path.basename(a) in _P6_PROGRAMS for a in args):
                return True                              # ruling 3: a wrapper runs it
            shell_verb = verb in _P7_SHELLS and verb != "eval"
            if verb == "eval" or shell_verb or verb in ("source", ".") \
                    or _P7_OTHER_CODE_RE.match(verb):
                codes = _p7_code_args(verb, args)
                if any(_p6_word_rule(c) for c in codes):
                    return True
                rest = [a for a in args if a not in codes]
                if any(_P6_NAMES_RE.search(a) or _P7_DYNAMIC_RE.search(a)
                       or a.startswith("/dev/") for a in rest):
                    return True
                ops = [a for a in rest if not a.startswith("-") or a == "-"]
                if shell_verb and (fed or "s" in "".join(
                        a[1:] for a in rest if a.startswith("-") and not a.startswith("--"))):
                    return True
                if not codes and "-m" not in args and (not ops or ops[0] == "-") \
                        and verb not in ("source", ".", "eval"):
                    return True                          # reads its program from stdin
            elif any(c.isspace() for a in args for c in a) and verb not in _P6_READERS \
                    and any(_P6_NAMES_RE.search(a) for a in args if any(c.isspace() for c in a)):
                return True                              # a command string handed to a wrapper
        fed = sep in ("|", "|&")
    return False


def _starts_build_or_restart(command, cwd=None):
    """P6: does this Bash command reach the runner, the launcher or the user's chat commands? ORDER 62b: a
    name or the port inside cleared python code is text; everything else is judged as before."""
    text = _py_without_code(command, cwd) if cwd and _py_parts(command, cwd)["segs"] else command
    if _P6_PORT_RE.search(text) and _P6_COMMAND_RE.search(text):
        return True
    if not _P6_NAMES_RE.search(text) or _only_reads(command):
        return False
    try:
        return _p6_runs(command)
    except Exception:
        return True                                      # cannot parse: the old word rule


def _build_path_owns(path):
    """P7: is this path part of the build path? The ONE list, used by the editor tools and by
    Bash. The review exception is judged on the resolved file, so a link named *_review_odin.md
    does not unguard what it points at."""
    real = _real(path)
    if any(real == _real(f) for f in _P7_FILES):
        return True
    if any(_inside(real, d) for d in _P7_DIRS):
        return not (_inside(real, f"{BUILD_ORDERS}/reports")
                    and os.path.basename(real).endswith(_P7_REVIEW_SUFFIX))
    return False


def _mentions_build_path(command, cwd):
    for tok in _TOKEN_RE.findall(command):
        try:
            if _build_path_owns(_resolve(tok, cwd)):
                return True
        except Exception:
            continue
    return False


# ORDER 17 — P7 judges what a Bash command WRITES, not what it mentions. Order 16's test (a write
# word anywhere + a guarded path mentioned anywhere, heredoc bodies included) refused Odin writing
# an order or a doc whose TEXT says "app.py". Now:
#   1. a data heredoc (`cat > f <<EOF`, `tee f <<EOF`, not piped on, not run later in the same
#      command) is data: its body is dropped and only its target is judged;
#   2. the rest is split into segments, quote-aware, tracking `cd` and `NAME=value`;
#   3. write words are judged by their operands (cp/install/ln: the destination; mv, ORDER 39:
#      every operand, since a source is removed), redirects by their targets;
#   4. interpreter code (heredoc, -c/-e, `| bash`, `eval`) is still inspected: shell code is judged
#      as shell, other code is refused if it both names the build path and has a write form;
#   5. a target that cannot be resolved is judged by what it could point at; one that could point
#      anywhere (starts with an unknown variable or a substitution) is refused when the build path
#      is named anywhere outside data-heredoc bodies.
# 🔴 Honest scope, unchanged: a script written by a data heredoc and run in a LATER command (e.g.
# historical #31's `cat > /tmp/patch_app.py`, run on its own) is not seen, and a substitution in the
# middle of a file name is trusted not to climb out of its directory. Item 25 is the wall.
_P7_BASH_WHY = (_P7_WHY + " Odin's own files — ORDER_*.md, NEXT.md, reports/*_review_odin.md — "
                "are writable; use Write/Edit for them.")
_P7_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|<>()'\"]+))")
_P7_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
_P7_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}",
                "time", "case", "esac", "select"}
_P7_PREFIXES = {"sudo", "doas", "nice", "nohup", "command", "builtin", "exec", "stdbuf", "ionice",
                "setsid", "env", "timeout", "xargs", "watch", "time", "unbuffer"}
_P7_PREFIX_ARGS = {"sudo": "ugCDhprtU", "xargs": "InLPsadE", "nice": "n", "timeout": "sk",
                   "watch": "nd", "ionice": "cnp", "env": "uCS"}
_P7_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "eval"}
_P7_OTHER_CODE_RE = re.compile(r"^(?:python[\d.]*|perl|ruby|node|nodejs)$")
_P7_CODE_WRITE_RE = re.compile(
    r"""\bopen\s*\((?:[^()]|\([^()]*\))*?['"](?:[rbtU]*[wax+][rbtU+]*|\+?>>?|\+<)['"]"""
    r"|\.write_(?:text|bytes)\s*\(|\bshutil\.(?:copy\w*|move|rmtree)\s*\("
    r"|\bos\.(?:replace|rename|remove|unlink|truncate)\s*\(|\.(?:unlink|rename|touch|chmod)\s*\("
    r"|\bfs\.\w*(?:write|append|unlink|rename|copy|rm|truncate)\w*\s*\("
    r"|\bFile\.(?:write|delete|unlink|rename)\b|\bsyswrite\b|\bunlink\s*\(|\brename\s*\(")
_P7_RUNS_SHELL_RE = re.compile(r"subprocess|os\.system|os\.popen|\bsystem\s*\(|`|\bexec\s*\(")
_P7_BENIGN_VAR_RE = re.compile(r"\$(?:\$|\?|!|#|\{?(?:RANDOM|SECONDS|EPOCHSECONDS|BASHPID)\b\}?)")
_P7_DYNAMIC_RE = re.compile(r"[$`*?\[]|\{\}")
# ORDER 55 — principle 3. A bare `mktemp` / `mktemp -d` makes a NEW file or directory, so nothing
# under it is a guarded path (catch #50: `T=$(mktemp -d); mkdir -p $T/h/memory/facts`). A template,
# -p or --tmpdir could place it anywhere, so those forms are not counted.
_TEMP_ROOT_RE = re.compile(r"\$\(mktemp(?:\s+-[dqu]+)*\s*\)")


def _p7_close(s, i):
    """Index of the `)` closing the `(` at s[i], skipping quotes; len(s) if none."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = s.find(c, i + 1)
            i = n if j == -1 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _p7_skip_quote(s, i):
    """s[i] opens a quote: index just past its close (len(s) if unterminated)."""
    q, n = s[i], len(s)
    j = i + 1
    while j < n:
        if q == '"' and s[j] == "\\":
            j += 2
            continue
        if s[j] == q:
            return j + 1
        j += 1
    return n


def _p7_heredocs(command):
    """(shell text with every heredoc body cut out, [{pos, body}]). `pos` indexes the `<<` in the
    shell text. `<<` inside quotes is not a heredoc."""
    out, docs, pending, size = [], [], [], 0
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\":
            piece, i = command[i:i + 2], i + 2
        elif c in "'\"":
            j = _p7_skip_quote(command, i)
            piece, i = command[i:j], j
        elif (command.startswith("<<", i) and not command.startswith("<<<", i)
              and not (i and command[i - 1] == "<")
              and (m := _P7_HEREDOC_RE.match(command, i))):
            pending.append({"pos": size, "tabs": m.group(1) == "-",
                            "delim": next(g for g in m.groups()[1:] if g is not None)})
            piece, i = m.group(0), m.end()
        elif c == "\n" and pending:
            out.append("\n")
            size += 1
            i += 1
            for d in pending:
                lines = []
                while i < n:
                    j = command.find("\n", i)
                    line, i = (command[i:], n) if j == -1 else (command[i:j], j + 1)
                    if (line.lstrip("\t") if d["tabs"] else line) == d["delim"]:
                        break
                    lines.append(line)
                docs.append({"pos": d["pos"], "body": "\n".join(lines)})
            pending = []
            continue
        else:
            piece, i = c, i + 1
        out.append(piece)
        size += len(piece)
    docs += [{"pos": d["pos"], "body": ""} for d in pending]
    return "".join(out), docs


def _p7_segments(text):
    """[(start, end, separator after)], split on ; && || | & and newlines outside quotes and
    substitutions. `>|`, `>&`, `&>` are redirects, not separators."""
    segs, start, i, n = [], 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            i = _p7_skip_quote(text, i)
            continue
        if text.startswith("$(", i) or text.startswith("<(", i) or text.startswith(">(", i):
            i = _p7_close(text, i + 1) + 1
            continue
        if c == "`":
            j = text.find("`", i + 1)
            i = n if j == -1 else j + 1
            continue
        sep = None
        if text.startswith(("&&", "||"), i):
            sep = text[i:i + 2]
        elif c == "|" and not (i and text[i - 1] == ">"):
            sep = "|&" if text.startswith("|&", i) else "|"
        elif c == "&" and not (i and text[i - 1] in "<>") and not text.startswith("&>", i):
            sep = "&"
        elif c in ";\n":
            sep = c
        if sep:
            segs.append((start, i, sep))
            i += len(sep)
            start = i
            continue
        i += 1
    segs.append((start, n, ""))
    return segs


def _p7_words(seg):
    """(words, redirect targets, substitution bodies) of one segment. Quotes are removed from
    words; `$(…)` is kept whole inside a word, and its body is also returned for judging. A
    redirect with no readable target gives None."""
    words, targets, subs = [], [], []
    cur, has, pending = [], False, None
    i, n = 0, len(seg)

    def flush():
        nonlocal cur, has, pending
        if has:
            w = "".join(cur)
            if pending == "target":
                targets.append(w)
            elif pending != "skip":
                words.append(w)
            pending = None
        cur, has = [], False

    while i < n:
        c = seg[i]
        if c in " \t\n":
            flush()
            i += 1
        elif c == "\\":
            cur.append(seg[i + 1:i + 2])
            has, i = True, i + 2
        elif c == "'":
            j = seg.find("'", i + 1)
            j = n if j == -1 else j
            cur.append(seg[i + 1:j])
            has, i = True, j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and seg[j] != '"':
                if seg[j] == "\\":
                    buf.append(seg[j + 1:j + 2])
                    j += 2
                elif seg.startswith("$(", j):
                    k = _p7_close(seg, j + 1)
                    subs.append(seg[j + 2:k])
                    buf.append(seg[j:k + 1])
                    j = k + 1
                elif seg[j] == "`":
                    k = seg.find("`", j + 1)
                    k = n if k == -1 else k
                    subs.append(seg[j + 1:k])
                    buf.append(seg[j:k + 1])
                    j = k + 1
                else:
                    buf.append(seg[j])
                    j += 1
            cur.append("".join(buf))
            has, i = True, j + 1
        elif seg.startswith("$(", i):
            k = _p7_close(seg, i + 1)
            subs.append(seg[i + 2:k])
            cur.append(seg[i:k + 1])
            has, i = True, k + 1
        elif c == "`":
            k = seg.find("`", i + 1)
            k = n if k == -1 else k
            subs.append(seg[i + 1:k])
            cur.append(seg[i:k + 1])
            has, i = True, k + 1
        elif seg.startswith(("<(", ">("), i):
            k = _p7_close(seg, i + 1)
            subs.append(seg[i + 2:k])
            cur.append("/dev/fd/63")
            has, i = True, k + 1
        elif c in "<>" or seg.startswith("&>", i):
            if has and ("".join(cur).isdigit()):
                cur, has = [], False                     # an fd number, not a word
            else:
                flush()
            m = re.match(r"&>>?|<<<|<<-?|<>|>>|>\||>&|<&|>|<", seg[i:])
            op = m.group(0)
            i += len(op)
            if op in (">&", "<&"):
                rest = seg[i:].lstrip(" \t")
                fd_dup = re.match(r"(?:\d+|-)(?=$|[\s;&|<>])", rest)
                pending = "skip" if fd_dup or op == "<&" else "target"
            elif op.startswith("<"):
                pending = "skip"                         # input, heredoc delimiter, here-string
            else:
                pending = "target"
        else:
            cur.append(c)
            has, i = True, i + 1
    flush()
    if pending == "target":
        targets.append(None)
    return words, targets, subs


def _p7_commands(words):
    """Yield (verb, args, fed_by_xargs, word) for the command in command position — past assignments,
    keywords and prefixes like sudo/env/xargs — and for each `find -exec` command."""
    i, n, xargs = 0, len(words), False
    while i < n:
        w = words[i].lstrip("({")
        if not w or w in _P7_KEYWORDS or _P7_ASSIGN_RE.match(w):
            i += 1
            continue
        base = os.path.basename(w)
        if base in _P7_PREFIXES:
            xargs = xargs or base == "xargs"
            i += 1
            while i < n and words[i].startswith("-") and words[i] != "-":
                opt = words[i]
                i += 1
                if (not opt.startswith("--") and len(opt) == 2
                        and opt[1] in _P7_PREFIX_ARGS.get(base, "")):
                    i += 1
            if base == "timeout" and i < n:
                i += 1                                   # the duration
            continue
        args = words[i + 1:]
        yield base, args, xargs, w
        if base == "find":
            for j, a in enumerate(args):
                if a in ("-exec", "-execdir", "-ok", "-okdir"):
                    inner = []
                    for b in args[j + 1:]:
                        if b in (";", "+"):
                            break
                        inner.append(b)
                    yield from _p7_commands(inner)
        return


def _p7_operands(args, takes_arg):
    """Non-option operands, skipping the argument of any short option in `takes_arg`.
    Returns (operands, {option letter: its argument})."""
    ops, vals, only, i = [], {}, False, 0
    while i < len(args):
        a = args[i]
        if only or a == "-" or not a.startswith("-"):
            ops.append(a)
        elif a == "--":
            only = True
        elif a.startswith("--"):
            if "=" in a:
                vals[a.split("=", 1)[0]] = a.split("=", 1)[1]
            elif a in ("--target-directory", "--suffix", "--mode", "--owner", "--group",
                       "--reference", "--date", "--size", "--expression", "--file"):
                vals[a] = args[i + 1] if i + 1 < len(args) else ""
                i += 1
        else:
            for k, ch in enumerate(a[1:], 1):
                if ch in takes_arg:
                    val = a[k + 1:]
                    if not val:
                        val = args[i + 1] if i + 1 < len(args) else ""
                        i += 1
                    vals.setdefault(ch, val)
                    break
                vals.setdefault(ch, "")
        i += 1
    return ops, vals


def _p7_write_targets(verb, args):
    """What this write word writes to, judged by its operands. None: it writes nothing here."""
    if verb in ("cp", "mv", "install", "ln"):
        ops, vals = _p7_operands(args, "tSmog" if verb == "install" else "tS")
        tdir = vals.get("t", vals.get("--target-directory"))
        if tdir is not None:
            return [tdir] + (ops if verb == "mv" else [])  # ORDER 39: mv's sources go too
        if verb == "install" and "d" in vals:
            return ops
        if verb == "ln" and len(ops) == 1:
            return [os.path.basename(ops[0].rstrip("/")) or ops[0]]
        if len(ops) < 2:
            return []
        return ops if verb == "mv" else ops[-1:]
    if verb in ("rm", "touch", "truncate", "chmod", "tee"):
        return _p7_operands(args, {"touch": "drt", "truncate": "sr"}.get(verb, ""))[0]
    if verb == "sed":
        inplace = False
        for a in args:
            if a == "--in-place" or a.startswith("--in-place="):
                inplace = True
            elif a.startswith("-") and not a.startswith("--"):
                for ch in a[1:]:
                    if ch == "i":
                        inplace = True
                    if ch in "iefl":
                        break
        return _p7_operands(args, "efl")[0] if inplace else None
    if verb == "perl":
        ops, vals = _p7_operands(args, "ieEIMx")
        return ops if "i" in vals else None
    if verb == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    return None


def _p7_code_args(verb, args):
    """The code a -c/-e flag hands an interpreter, and eval's arguments."""
    if verb == "eval":
        return [" ".join(args)]
    flag = ("c" if verb in _P7_SHELLS or verb.startswith("python") else
            "ep" if verb.startswith("node") else "eE")
    code, i = [], 0
    while i < len(args):
        a = args[i]
        if verb.startswith("node") and a in ("--eval", "--print"):
            code += args[i + 1:i + 2]
            i += 1
        elif a.startswith("-") and not a.startswith("--") and any(ch in flag for ch in a[1:]):
            k = next(k for k, ch in enumerate(a[1:], 1) if ch in flag)
            if a[k + 1:] and verb not in _P7_SHELLS:
                code.append(a[k + 1:])                   # perl -e'code'
            else:
                code += args[i + 1:i + 2]
                i += 1
        elif not a.startswith("-") and (verb in _P7_SHELLS or verb.startswith("python")):
            break                                        # a script path: code ends here
        i += 1
    return code


def _p7_code_writes(code, cwd):
    """Non-shell code: refused if it both names the build path and has a write form."""
    if not _mentions_build_path(code, cwd):
        return False
    return bool(_P7_CODE_WRITE_RE.search(code)
                or (_P7_RUNS_SHELL_RE.search(code) and _WRITE_WORD_RE.search(code)))


def _p7_guarded_roots():
    return [_real(p) for p in _P7_FILES + _P7_DIRS]


def _p7_target(t, cwd, home_cwd):
    """'deny', 'unknown' (could point anywhere) or None, for one write target."""
    if t is None:
        return "unknown"
    t = _P7_BENIGN_VAR_RE.sub("0", t)
    if cwd:
        t = re.sub(r"\$\{?PWD\b\}?", lambda _: cwd, t)
    t = os.path.expandvars(t)                           # only variables that exist here
    t = t[:-1] if t.endswith(")") and "(" not in t else t
    m = _P7_DYNAMIC_RE.search(t)
    relative = not t.startswith(("/", "~"))
    if m and _p7_temp(t):
        return None
    if not m:
        if cwd is None and relative:
            return "unknown"
        return "deny" if _build_path_owns(_resolve(t, cwd)) else None
    if _mentions_build_path(t, cwd or home_cwd):
        return "deny"
    comps = t.split("/")
    for k, c in enumerate(comps):
        if not c or _P7_DYNAMIC_RE.search(c):
            continue
        if c in {os.path.basename(f) for f in _P7_FILES}:
            return "deny"
        if k and comps[k - 1] == "build_orders" and c in {os.path.basename(d) for d in _P7_DIRS}:
            return "deny"
    prefix = t[:m.start()]
    if cwd is None and relative:
        return "unknown"
    starts_component = not prefix or prefix.endswith("/")
    if (t[m.start()] in "$`" and starts_component) or m.group() == "{}":
        return "unknown"                                 # a whole path part we cannot see
    head, slash, part = prefix.rpartition("/")
    if not slash and prefix.startswith("~"):
        return "unknown"
    base = _resolve((head or "/") if slash else ".", cwd)
    if _build_path_owns(base):
        return "deny"
    stem = os.path.join(base, part)
    if t[m.start()] in "$`" or "/" in t[m.start():]:
        return "deny" if any(g.startswith(stem) for g in _p7_guarded_roots()) else None
    pattern = os.path.join(base, part + t[m.start():])   # a glob in the last part only
    return "deny" if any(os.path.dirname(g) == base and fnmatch.fnmatch(g, pattern)
                         for g in _p7_guarded_roots()) else None


def _p7_parse(command, cwd):
    """ORDER 55: passes 1 and 2 of _writes_build_path, shared with P5. (segments with their words,
    redirect targets, substitutions, commands, cwd and known variables; data heredocs; [(body, cwd, lang,
    heredoc position)] for code)."""
    shell, docs = _p7_heredocs(command)
    spans = _p7_segments(shell)
    segs, known, here = [], {}, cwd

    def subst(text):
        for name in sorted(known, key=len, reverse=True):
            text = re.sub(r"\$\{" + name + r"\}|\$" + name + r"(?!\w)",
                          lambda _: known[name], text)
        return text

    for start, end, sep in spans:                        # pass 1: words, cwd and variables
        text = subst(shell[start:end])
        words, targets, subs = _p7_words(text)
        cmds = list(_p7_commands(words))
        segs.append({"start": start, "end": end, "sep": sep, "text": text, "words": words,
                     "targets": targets, "subs": subs, "cmds": cmds, "cwd": here,
                     "known": dict(known)})               # ORDER 62b: the variables it was read with
        lead = [w for w in words if w not in ("export", "local", "readonly", "declare")]
        if words and not cmds and all(_P7_ASSIGN_RE.match(w) for w in lead):
            for w in lead:
                name, value = w.split("=", 1)
                known[name.rstrip("+")] = value
        elif cmds and cmds[0][0] in ("cd", "pushd"):
            dest = next((a for a in cmds[0][1] if not a.startswith("-")), "~")
            if [a for a in cmds[0][1] if a == "-"] or _P7_DYNAMIC_RE.search(
                    os.path.expandvars(dest)) or here is None and not dest.startswith(("/", "~")):
                here = None
            else:
                here = _resolve(dest, here)

    def seg_at(pos):
        return next((k for k, s in enumerate(segs) if s["start"] <= pos <= s["end"]), 0)

    def runs(k, path):                                   # is `path` run by a later segment?
        for s in segs[k + 1:]:
            for verb, args, _, word in s["cmds"]:
                cands = [word] + ([next((a for a in args if not a.startswith("-")), "")]
                                  if verb in _P7_SHELLS | {"source", "."}
                                  or _P7_OTHER_CODE_RE.match(verb) else [])
                if any(c and not _P7_DYNAMIC_RE.search(c) and s["cwd"]
                       and _resolve(c, s["cwd"]) == path for c in cands):
                    return True
        return False

    data, code = [], []                                  # pass 2: which heredocs are data
    for d in docs:
        k = seg_at(d["pos"])
        s = segs[k]
        verb, args = s["cmds"][0][:2] if s["cmds"] else ("", [])
        outs = ([t for t in s["targets"] if t] if verb == "cat" else
                _p7_write_targets("tee", args) if verb == "tee" else [])
        written = [_resolve(t, s["cwd"]) for t in outs
                   if s["cwd"] and not _P7_DYNAMIC_RE.search(t)]
        if (verb in ("cat", "tee") and outs and not s["sep"].startswith("|")
                and not any(runs(k, p) for p in written)):
            data.append(d)
            continue
        consumer = verb
        if not (verb in _P7_SHELLS or _P7_OTHER_CODE_RE.match(verb)) and s["sep"].startswith("|") \
                and k + 1 < len(segs) and segs[k + 1]["cmds"]:
            consumer = segs[k + 1]["cmds"][0][0]
        lang = ("code" if _P7_OTHER_CODE_RE.match(consumer) else
                "shell" if consumer in _P7_SHELLS else "either")
        code.append((d["body"], s["cwd"] or cwd, lang, d["pos"]))
    return segs, data, code


def _p7_temp(t):
    """ORDER 55: is `t` a fresh mktemp result, or a path under one with no variable, substitution,
    glob or `..` after it that could climb out?"""
    m = _TEMP_ROOT_RE.match(t)
    if not m:
        return False
    rest = t[m.end():]
    return ((not rest or rest.startswith("/")) and not _P7_DYNAMIC_RE.search(rest)
            and ".." not in rest.split("/"))


def _p7_could_reach(segs, k):
    """ORDER 55 (catch #72): could a write word that segment k does not place land in the build
    path? Only if the segment names the build path itself, runs from an unknown cwd, holds a word
    that cannot be read (variable, substitution, glob) or is fed by a pipe. `pip install pyflakes`
    beside a read of app.py writes nothing there."""
    s = segs[k]
    return (s["cwd"] is None or _mentions_build_path(s["text"], s["cwd"])
            or any(_P7_DYNAMIC_RE.search(w) for w in s["words"])
            or (k > 0 and segs[k - 1]["sep"] in ("|", "|&")))


def _writes_build_path(command, cwd, depth=0, named_outside=False):
    """P7 for Bash: does anything in this command write to the build path? See ORDER 17 above."""
    segs, _data, code = _p7_parse(command, cwd)
    py = _py_parts(command, cwd) if depth == 0 else _PY_NONE     # ORDER 62b
    if any(_py_lands(t, _P7_FILES + _P7_DIRS, _build_path_owns) for t in py["targets"]):
        return True
    named = named_outside or any(_mentions_build_path(s["text"], s["cwd"] or cwd)
                                 for k, s in enumerate(segs) if k not in py["segs"]) \
        or any(_mentions_build_path(b, c) for b, c, _, pos in code if pos not in py["docs"])

    def judge_code(body, where, lang):
        if lang in ("code", "either") and _p7_code_writes(body, where):
            return True
        if lang in ("shell", "either"):
            if depth >= 4:
                return _p7_code_writes(body, where) or bool(_WRITE_WORD_RE.search(body)
                                                            and _mentions_build_path(body, where))
            return _writes_build_path(body, where, depth + 1, named)
        return False

    for body, where, lang, pos in code:                  # pass 3: judge
        if pos not in py["docs"] and judge_code(body, where, lang):
            return True
    for k, s in enumerate(segs):
        where = s["cwd"]
        for sub in s["subs"]:
            if judge_code(sub, where or cwd, "shell"):
                return True
        found = list(s["targets"])
        verb_seen = False
        for verb, args, xargs, _ in s["cmds"]:
            tg = _p7_write_targets(verb, args)
            if tg is not None:
                verb_seen = True
                found += tg + ([None] if xargs else [])
            if verb in _P7_SHELLS or _P7_OTHER_CODE_RE.match(verb):
                verb_seen = True
                lang = "shell" if verb in _P7_SHELLS else "code"
                if k not in py["segs"] and any(judge_code(c, where or cwd, lang) for c in _p7_code_args(verb, args)):
                    return True
        for t in found:
            r = _p7_target(t, where, cwd)
            if r == "deny" or (r == "unknown" and named):
                return True
        if (not verb_seen and _WRITE_WORD_RE.search(s["text"]) and named
                and not (s["cmds"] and s["cmds"][0][0] in _P6_READERS)
                and _p7_could_reach(segs, k)):
            return True                                  # a write word we could not place
    return False


# ORDER 33b — P4 judges a `tee` or a redirect by WHERE IT LANDS. Catch #55: `cd ~/helix/memory/facts;
# … | tee /tmp/pins.txt` was refused because the command named the fact base (the `cd`) and held the
# word `tee`. Each tee operand and redirect target is now resolved against the cwd of the segment it
# runs in (`cd`/`pushd` tracked, as P7 does) and refused if it lands in the fact base. One that cannot
# be resolved, or a `tee` the parser cannot place (inside `$(…)`, `bash -c`, …), is still refused when
# the fact base is named. Every other write word keeps the mention rule.
# ORDER 39 — catch #60: `cd …/facts; ls | tee /tmp/x; echo "tee exit"` was refused because the word
# in echo's quoted text counted as a tee the parser could not place. Text in a quoted argument of
# echo/printf is not a tee — unless that output is piped on, the quote holds `$(…)`/a backtick, or
# the command also runs a shell, eval, source, an interpreter, xargs or any substitution. A tee
# fed by xargs gets an unknown target, as P7 already gives it.
# ORDER 43 — catch #67: `… | tee /tmp/x; echo "tee exit $?" | tail -1` was refused because echo's
# output was piped on (the `$?` played no part). Piped on into nothing but _P6_READERS, which never
# run their input, the quoted text is still text.
_TEE_WORD_RE = re.compile(r"\btee\b")
_P4_TEXT_VERBS = ("echo", "printf")


def _p4_text_tees(command):
    """ORDER 39: how many tee words are only text in a quoted echo/printf argument (see above)."""
    shell, _docs = _p7_heredocs(command)
    if _P6_DQ_LIVE_RE.search(shell) or "<(" in shell or ">(" in shell:
        return 0
    count, segs = 0, _p7_segments(shell)
    verbs = [list(_p7_commands(_p7_words(shell[start:end])[0])) for start, end, _sep in segs]
    for k, (start, end, sep) in enumerate(segs):
        seg = shell[start:end]
        cmds = verbs[k]
        for verb, _args, xargs, word in cmds:
            if (xargs or verb in _P7_SHELLS or verb in ("source", ".")
                    or _P7_OTHER_CODE_RE.match(verb) or _P7_DYNAMIC_RE.search(word)):
                return 0
        if not cmds or cmds[0][0] not in _P4_TEXT_VERBS or not _p4_feeds_readers(segs, verbs, k):
            continue
        i = 0
        while i < len(seg):
            if seg[i] == "\\":
                i += 2
            elif seg[i] in "'\"":
                j = _p7_skip_quote(seg, i)
                count += len(_TEE_WORD_RE.findall(seg[i:j]))
                i = j
            else:
                i += 1
    return count


def _p4_feeds_readers(segs, verbs, k):
    """ORDER 43: segment k's output goes nowhere, or only through a pipeline of _P6_READERS."""
    while segs[k][2] in ("|", "|&"):
        k += 1
        if not verbs[k] or any(v not in _P6_READERS or x for v, _a, x, _w in verbs[k]):
            return False
    return True


def _p4_target(t, here):
    """'in', 'unknown' or None: where one tee/redirect target lands, from cwd `here`."""
    if t is None or _P7_DYNAMIC_RE.search(t):
        return "unknown"
    if here is None and not t.startswith(("/", "~")):
        return "unknown"
    return "in" if _inside(_resolve(t, here), FACTS) else None


def _p4_tee_redirects(command, cwd):
    """(verdict, tees placed, a segment ran inside the fact base) for every tee operand and redirect
    target. verdict: 'in' if one lands in the fact base, else 'unknown' if one cannot be resolved,
    else None."""
    shell, _docs = _p7_heredocs(command)
    here, verdict, placed, inside = cwd, None, 0, False
    for start, end, _sep in _p7_segments(shell):
        words, targets, _subs = _p7_words(shell[start:end])
        cmds = list(_p7_commands(words))
        inside = inside or (here is not None and _inside(here, FACTS))
        for verb, args, xargs, _word in cmds:
            if verb == "tee":
                placed += 1
                targets = targets + _p7_write_targets("tee", args) + ([None] if xargs else [])
        for t in targets:
            r = _p4_target(t, here)
            if r == "in":
                return "in", placed, inside
            verdict = verdict or r
        if cmds and cmds[0][0] in ("cd", "pushd"):
            args = cmds[0][1]
            dest = next((a for a in args if not a.startswith("-")), "~")
            if "-" in args or _P7_DYNAMIC_RE.search(os.path.expandvars(dest)) \
                    or here is None and not dest.startswith(("/", "~")):
                here = None
            else:
                here = _resolve(dest, here)
    return verdict, placed, inside


def _writes_facts(command, cwd):
    """P4 for Bash: does this command write into the fact base? ORDER 55: a data heredoc's body is
    data (catch #51), and a path under a fresh mktemp directory or /tmp does not name the fact base
    (catch #50); see _p4_without_temp. ORDER 62b: a cleared python part's resolved targets are judged
    here, and its code is not text (_p4_without_data)."""
    if any(_py_lands(t, [FACTS], lambda p: _inside(p, FACTS)) for t in _py_parts(command, cwd)["targets"]):
        return True
    lands, tees, inside = _p4_tee_redirects(command, cwd)
    if lands == "in":
        return True
    text = _p4_without_data(command, cwd)
    scratch = _p4_without_temp(text, cwd)
    if not (inside or _mentions(scratch, cwd, FACTS)):
        return False
    other_word = any(m.group(3) != "tee" for m in _WRITE_WORD_RE.finditer(text))
    return (other_word or lands == "unknown"
            or tees < len(_TEE_WORD_RE.findall(text)) - _p4_text_tees(command)
            or _guarded_redirect(scratch, cwd, FACTS))


def _p4_without_data(command, cwd):
    """ORDER 55: the command with each data heredoc's body cut out (P7 decides which are data:
    `cat > f <<EOF` / `tee f <<EOF`, not piped on, not run later). Code bodies are kept. ORDER 62b: a
    cleared python part keeps only its verb and its redirects, and the heredoc it reads is dropped."""
    return _py_without_code(command, cwd, {d["pos"] for d in _p7_parse(command, cwd)[1]})


def _p4_without_temp(command, cwd):
    """ORDER 55 — principle 3: the command with every scratch path blanked. A scratch path is one
    under a variable whose ONLY appearance besides its `$NAME` uses is one `NAME=$(mktemp …)` of the
    _TEMP_ROOT_RE form, or a literal /tmp or /var/tmp path that does not resolve into the fact
    base. A path with `..`, or one followed straight on by a variable or substitution, is kept."""
    chunk = r"(/[^\s'\";|&<>()$`\\]*)?"
    names = []
    for m in re.finditer(r"(?<![\w$])([A-Za-z_]\w*)=" + _TEMP_ROOT_RE.pattern, command):
        name = m.group(1)
        uses = len(re.findall(r"\$\{?" + name + r"\b\}?", command))
        if len(re.findall(r"(?<![\w])" + name + r"\b", command)) == uses + 1:
            names.append(name)

    def blank(m):
        rest = m.group(1) or ""
        after = m.string[m.end():m.end() + 1]
        if ".." in rest.split("/") or after in ("$", "`"):
            return m.group(0)
        if m.group(0).startswith("/") and _inside(_resolve(m.group(0), cwd), FACTS):
            return m.group(0)
        return "/dev/null"                               # still a target, and harmless

    for name in names:
        command = re.sub(r"\$\{?" + name + r"\b\}?" + chunk, blank, command)
    return re.sub(r"(?<![\w.~/$-])/(?:var/)?tmp(?![\w.-])" + chunk, blank, command)


# ORDER 62b — P-20261004-9. Python code is judged by what it WRITES, resolved, not by what it mentions.
# A python part (`python -c CODE`, or `python [-]` reading its program from a heredoc) is CLEARED when bash
# hands python exactly the text read here, the code parses, and every effect is known: imports from a fixed
# module list, assignments, control flow, print, pure calls (_PY_PURE_CALLS, the hook's own _PY_HOOK_PURE,
# _PY_METHODS on values), and writes — open() for writing, Path(x).write_text/write_bytes, _PY_WRITE_CALLS —
# whose target is a literal, a name bound once to one, an f-string / os.path.join / os.path.expanduser of
# those, or a sys.argv[i] the command supplies. A cleared part's code is not a mention for P4, P5 or P7, nor
# a name for P6's gate (P6's run test is unchanged, so catch #40 stays refused);
# its resolved targets are judged instead. Anything else (a loop variable, input(), a computed path, a call
# not on a list) leaves the part judged exactly as before this order: fail closed, as today.
# 🔴 Honest scope: a module python imports is code this file does not read, like a script; an import is not
# cleared if the cwd holds a file or directory of that name, and factgate_hook only from ~/helix itself.
_PY_HOOK_PURE = ("_writes_facts", "_p7_parse", "_mentions", "_redirect_targets", "_only_reads")
_PY_MODULES = ("os", "sys", "json", "re", "glob", "pathlib", "shutil", "collections", "itertools", "textwrap",
               "difflib", "hashlib", "datetime", "time", "math", "ast", "fnmatch", "factgate_hook")
_PY_PURE_CALLS = frozenset(
    [f"os.path.{f}" for f in ("join", "expanduser", "basename", "dirname", "exists", "isfile", "isdir",
                              "islink", "realpath", "abspath", "normpath", "relpath", "split", "splitext",
                              "getsize", "getmtime")]
    + [f"re.{f}" for f in ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn",
                           "split", "escape")]
    + ["os.listdir", "os.getcwd", "os.stat", "os.walk", "os.scandir", "os.environ.get", "os.getpid",
       "sys.exit", "sys.stdout.write", "sys.stderr.write", "sys.stdout.flush", "json.loads", "json.dumps",
       "json.load", "json.dump", "glob.glob", "fnmatch.fnmatch", "pathlib.Path", "collections.Counter",
       "collections.defaultdict", "collections.OrderedDict", "textwrap.dedent", "textwrap.shorten",
       "textwrap.fill", "textwrap.indent", "difflib.unified_diff", "hashlib.sha256", "hashlib.md5",
       "hashlib.sha1", "datetime.datetime.now", "datetime.date.today", "datetime.datetime.fromtimestamp",
       "datetime.datetime.fromisoformat", "time.time", "time.strftime", "time.localtime", "time.gmtime",
       "time.perf_counter", "ast.parse", "ast.walk", "ast.dump", "ast.literal_eval", "itertools.chain",
       "itertools.islice", "itertools.groupby", "math.floor", "math.ceil", "math.sqrt"])
_PY_VALUES = ("sys.argv", "sys.stdin", "sys.stdout", "sys.stderr", "os.sep", "os.linesep", "os.pathsep",
              "os.path.sep")
# What each call writes: the indexes of its path arguments. A copy or move into an existing directory
# writes the source's name inside it.
_PY_WRITE_CALLS = {"os.remove": (0,), "os.unlink": (0,), "os.rmdir": (0,), "os.mkdir": (0,),
                   "os.makedirs": (0,), "os.rename": (0, 1), "os.replace": (0, 1), "shutil.copy": (1,),
                   "shutil.copy2": (1,), "shutil.copyfile": (1,), "shutil.copytree": (1,),
                   "shutil.move": (0, 1), "shutil.rmtree": (0,)}
_PY_INTO_DIR = ("shutil.copy", "shutil.copy2", "shutil.move")
_PY_BUILTINS = frozenset((
    "print", "len", "str", "int", "float", "bool", "repr", "sorted", "list", "dict", "set", "tuple",
    "frozenset", "enumerate", "range", "min", "max", "sum", "any", "all", "zip", "isinstance", "abs", "round",
    "reversed", "map", "filter", "hex", "oct", "bin", "ord", "chr", "divmod", "iter", "next", "format",
    "bytes", "type", "id", "callable", "hash", "Exception", "StopIteration", "SystemExit"))
_PY_METHODS = frozenset((
    "read", "readline", "readlines", "write", "writelines", "close", "flush", "seek", "tell",
    "split", "rsplit", "splitlines", "strip", "lstrip", "rstrip", "join", "replace", "find", "rfind", "index",
    "rindex", "count", "startswith", "endswith", "lower", "upper", "title", "capitalize", "casefold", "format",
    "encode", "decode", "partition", "rpartition", "ljust", "rjust", "center", "zfill", "isdigit", "isalpha",
    "isalnum", "isspace", "isupper", "islower", "removeprefix", "removesuffix", "expandtabs",
    "get", "items", "keys", "values", "setdefault", "pop", "popitem", "append", "extend", "insert", "remove",
    "sort", "reverse", "add", "discard", "union", "intersection", "difference", "issubset", "issuperset",
    "copy", "clear", "update", "most_common", "hexdigest", "digest",
    "group", "groups", "groupdict", "start", "end", "span", "search", "match", "fullmatch", "findall",
    "finditer", "sub", "subn", "read_text", "read_bytes", "exists", "is_file", "is_dir", "iterdir", "glob",
    "rglob", "resolve", "stat", "with_suffix", "with_name", "relative_to", "as_posix", "is_symlink",
    "strftime", "isoformat", "timestamp", "total_seconds"))
_PY_NODES = (ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.While, ast.If, ast.With,
             ast.withitem, ast.Try, ast.ExceptHandler, ast.Pass, ast.Break, ast.Continue, ast.Return,
             ast.FunctionDef, ast.Lambda, ast.arguments, ast.arg, ast.Import, ast.ImportFrom, ast.alias,
             ast.Assert, ast.Raise, ast.Delete, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.IfExp, ast.Dict,
             ast.Set, ast.List, ast.Tuple, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
             ast.comprehension, ast.Compare, ast.Call, ast.keyword, ast.FormattedValue, ast.JoinedStr,
             ast.Constant, ast.Attribute, ast.Subscript, ast.Slice, ast.Starred, ast.Name, ast.NamedExpr,
             ast.expr_context, ast.operator, ast.boolop, ast.cmpop, ast.unaryop)
_PY_OPEN_KEYS = ("file", "mode", "encoding", "errors", "newline", "buffering")
_PY_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_PY_FLAGS = ("-B", "-u", "-I", "-E", "-s", "-S", "-O", "-OO", "-q", "-b", "-bb", "-P")
# A word that changes which python runs or what it loads, or defines a shell function or alias.
_PY_ENV_RE = re.compile(r"(?:PATH|PYTHON\w*|BASH_ENV|ENV|LD_\w+)\+?=")
_PY_SHELL_VERBS = ("source", ".", "eval", "alias", "function", "shopt", "enable", "hash", "unalias", "popd")
_PY_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=]", re.M)  # python decodes a heredoc by its cookie
_PY_PLAIN_RE = re.compile(r"[\w./@%+=:,-]+")
_PY_NONE = {"segs": {}, "docs": frozenset(), "targets": ()}


def _py_parts(command, cwd):
    """ORDER 62b: {"segs": {segment index: resolved targets}, "docs": heredoc positions those parts read
    their program from, "targets": every resolved target} for the cleared python parts of a Bash command
    (indexes and positions as _p7_parse gives them). Nothing is cleared if anything goes wrong."""
    key = ("py", command, cwd)
    if _real_memo is not None and key in _real_memo:
        return _real_memo[key]
    try:
        got = _py_parts_of(command, cwd) if cwd else _PY_NONE
    except Exception:
        got = _PY_NONE
    if _real_memo is not None:
        _real_memo[key] = got
    return got


def _py_parts_of(command, cwd):
    shell, docs = _p7_heredocs(command)
    segs, data, _code = _p7_parse(command, cwd)
    for s in segs:
        if any(_PY_ENV_RE.match(w) for w in s["words"]) or any(
                w == "()" or w.endswith("()") or w in _PY_SHELL_VERBS for w in s["words"][:2]):
            return _PY_NONE                              # python may not be python, nor PATH's
    if any(s["words"] and (s["words"][0] in _P7_KEYWORDS or s["words"][0][:1] in "({") for s in segs):
        return _PY_NONE                                  # a subshell, loop or branch: cwd and variables unsure
    plain = {}                                           # variables bash expands as hook reads them
    for j, s in enumerate(segs):
        if (s["words"] and not s["cmds"] and all(_P7_ASSIGN_RE.match(w) for w in s["words"])
                and (j == 0 or segs[j - 1]["sep"] in (";", "\n")) and s["sep"] in (";", "\n", "&&", "")):
            for w in s["words"]:
                name, value = w.split("=", 1)
                if (_PY_PLAIN_RE.fullmatch(value) and not name.endswith("+") and len(re.findall(
                        r"(?<![\w$])" + re.escape(name) + r"\+?=", shell)) == 1):
                    plain[name] = value
    data_pos = {d["pos"] for d in data}
    out = {"segs": {}, "docs": set(), "targets": []}
    for k, s in enumerate(segs):
        if len(s["cmds"]) != 1 or not s["cwd"]:
            continue
        verb, args, xargs, word = s["cmds"][0]
        if xargs or not _PY_PYTHON_RE.match(verb) or ("/" in word and _real(_resolve(word, s["cwd"])) not in (
                _real("/usr/bin/python3"), _real(f"{BRAIN}/.venv/bin/python"))):
            continue
        raw = shell[s["start"]:s["end"]]
        ok_vars = {n: v for n, v in plain.items() if s["known"].get(n) == v}
        if not _py_shell_exact(raw, ok_vars, s["known"]):
            continue
        prog = _py_program(args)
        if prog is None:
            continue
        code, argv = prog
        here = [d for d in docs if s["start"] <= d["pos"] <= s["end"]]
        if code is None:                                 # the program is the heredoc on stdin
            if (len(here) != 1 or here[0]["pos"] in data_pos or "<" in re.sub(r"<<-?", "", raw)
                    or not _py_heredoc_literal(shell, here[0])):
                continue
            code = here[0]["body"]
        if _PY_CODING_RE.search("\n".join(code.split("\n")[:2])):
            continue
        targets = _py_effects(code, argv, s["cwd"])
        if targets is None:
            continue
        out["segs"][k] = targets
        out["targets"] += targets
        if prog[0] is None:
            out["docs"].add(here[0]["pos"])
    return out


def _py_shell_exact(raw, ok_vars, known):
    """Does bash hand this part's words to python exactly as _p7_words reads them? No substitution,
    glob, tilde, comment, ANSI quote or line continuation; a backslash in double quotes only before a
    dollar, backtick, double quote or backslash; a variable only if it is in `ok_vars`; and no `$` the hook's own substitution would touch."""
    names = "|".join(re.escape(n) for n in known)
    if names and re.search(r"\\\$\{?(?:" + names + r")(?!\w)", raw):
        return False

    def var_end(i):
        m = re.match(r"\$(?:\{(\w+)\}|([A-Za-z_]\w*))", raw[i:])
        return i + m.end() if m and (m.group(1) or m.group(2)) in ok_vars else None

    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            if raw[i + 1:i + 2] in ("", "\n"):
                return False
            i += 2
        elif c == "'":
            j = raw.find("'", i + 1)
            if j == -1 or ("$" in raw[i + 1:j] and known):
                return False
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and raw[j] != '"':
                if raw[j] == "\\":
                    if raw[j + 1:j + 2] not in ("$", "`", '"', "\\"):
                        return False
                    j += 2
                elif raw[j] == "$":
                    j = var_end(j)
                    if j is None:
                        return False
                elif raw[j] == "`":
                    return False
                else:
                    j += 1
            if j >= n:
                return False
            i = j + 1
        elif c == "$":
            i = var_end(i)
            if i is None:
                return False
        elif c in "`*?[{}~#" or raw.startswith(("<(", ">("), i):
            return False
        else:
            i += 1
    return True


def _py_program(args):
    """(code or None for stdin, sys.argv) for python's arguments, or None for a script, -m or a flag
    not modelled here."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            return (args[i + 1], ["-c"] + args[i + 2:]) if i + 1 < len(args) else None
        if a == "-":
            return None, ["-"] + args[i + 1:]
        if a in ("-W", "-X"):
            i += 2
        elif a in _PY_FLAGS:
            i += 1
        else:
            return None
    return None, [""]


def _py_heredoc_literal(shell, d):
    """Is this heredoc's body what python reads? A quoted delimiter, or a body with nothing bash expands;
    never `<<-`, which strips tabs."""
    m = _P7_HEREDOC_RE.match(shell, d["pos"])
    if not m or m.group(1) == "-":
        return False
    if m.group(2) is not None or m.group(3) is not None or "\\" in m.group(0):
        return True
    return not re.search(r"[$`\\]", d["body"])


def _py_dotted(node, alias):
    """'os.path.join' for a chain rooted at an imported name, else None."""
    if isinstance(node, ast.Name):
        return alias.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _py_dotted(node.value, alias)
        return f"{base}.{node.attr}" if base else None
    return None


def _py_effects(code, argv, cwd):
    """The resolved absolute write targets of this python code, or None if it is not cleared."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    alias, binds, assigned, funcs, parent = {}, {}, {}, set(), {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if not isinstance(node, _PY_NODES):
            return None
        for child in ast.iter_child_nodes(node):
            parent[child] = node
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and (node.level or not node.module):
                return None
            for a in node.names:
                full = f"{node.module}.{a.name}" if isinstance(node, ast.ImportFrom) else a.name
                top = full.split(".")[0]
                if a.name == "*" or top not in _PY_MODULES:
                    return None
                if top == "factgate_hook" and _real(cwd) != _real(BRAIN):
                    return None
                if top != "factgate_hook" and os.path.lexists(os.path.join(cwd, top + ".py")) \
                        or os.path.lexists(os.path.join(cwd, top)) and top != "factgate_hook":
                    return None
                name = a.asname or (a.name if isinstance(node, ast.ImportFrom) else top)
                value = full if (a.asname or isinstance(node, ast.ImportFrom)) else top
                if alias.setdefault(name, value) != value:
                    return None
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                return None
            if not isinstance(node.ctx, ast.Load):
                binds[node.id] = binds.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            binds[node.arg] = binds.get(node.arg, 0) + 1
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                return None
            binds[node.name] = binds.get(node.name, 0) + 1
            funcs.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binds[node.name] = binds.get(node.name, 0) + 1
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or not isinstance(node.ctx, ast.Load):
                return None                              # no dunders, no `x.attr = …`
        elif isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            if not isinstance(node.value, ast.Name) or node.value.id in alias:
                return None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value
    if any(n in alias or n in _PY_BUILTINS or n == "open" for n in binds):
        return None                                      # a module, builtin or open rebound
    uses_path = any(v.startswith("pathlib") for v in alias.values())
    targets = []

    def text(node, depth=0):
        if depth > 20:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            ok = binds.get(node.id) == 1 and node.id in assigned
            return text(assigned[node.id], depth + 1) if ok else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and v.conversion in (-1, 115) and v.format_spec is None:
                    v = v.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, int):
                        parts.append(str(v.value))
                        continue
                p = text(v, depth + 1)
                if p is None:
                    return None
                parts.append(p)
            return "".join(parts)
        if isinstance(node, ast.Call) and not node.keywords and node.args \
                and not any(isinstance(a, ast.Starred) for a in node.args):
            d = _py_dotted(node.func, alias)
            vals = [text(a, depth + 1) for a in node.args]
            if None in vals:
                return None
            if d == "os.path.join":
                return os.path.join(*vals)
            if d == "os.path.expanduser" and len(vals) == 1:
                return os.path.expanduser(vals[0])
            return None
        if isinstance(node, ast.Subscript) and _py_dotted(node.value, alias) == "sys.argv" \
                and isinstance(node.slice, ast.Constant) and type(node.slice.value) is int:
            i = node.slice.value
            w = argv[i] if 0 <= i < len(argv) else None
            return w if w and not _P7_DYNAMIC_RE.search(w) else None
        return None

    def path_of(node, depth=0):
        if isinstance(node, ast.Name) and binds.get(node.id) == 1 and node.id in assigned and depth < 20:
            return path_of(assigned[node.id], depth + 1)
        if isinstance(node, ast.Call) and _py_dotted(node.func, alias) == "pathlib.Path" \
                and len(node.args) == 1 and not node.keywords:
            return text(node.args[0])
        return None

    def where(p):
        if not isinstance(p, str) or not p or "\0" in p:
            return None
        return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))

    for node in nodes:
        up = parent.get(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and not (
                node.id in alias or node.id in binds or node.id in _PY_BUILTINS or node.id == "open"
                or node.id.endswith("Error")):
            return None                                  # eval, exec, getattr, globals, __import__ …
        if isinstance(node, (ast.Name, ast.Attribute)) and not (
                isinstance(up, ast.Attribute) and up.value is node):
            d = _py_dotted(node, alias)
            if d and not (isinstance(up, ast.Call) and up.func is node) and not (
                    d in _PY_VALUES or d in _PY_PURE_CALLS or ("." in d and d.rsplit(".", 1)[1].isupper())):
                return None                              # a module or a writer passed around as a value
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        d = _py_dotted(f, alias)
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            return None
        if isinstance(f, ast.Name) and d is None:
            if f.id == "open":
                if any(k.arg not in _PY_OPEN_KEYS for k in node.keywords):
                    return None
                kw = {k.arg: k.value for k in node.keywords}
                file = node.args[0] if node.args else kw.get("file")
                mode = node.args[1] if len(node.args) > 1 else kw.get("mode")
                if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                    return None
                if mode is not None and any(c in mode.value for c in "wax+"):
                    t = where(text(file)) if file is not None else None
                    if t is None:
                        return None
                    targets.append(t)
            elif f.id not in funcs and f.id not in _PY_BUILTINS and not f.id.endswith("Error"):
                return None
        elif d is not None:
            if d in _PY_WRITE_CALLS:
                if node.keywords and d not in ("os.makedirs", "shutil.copytree", "shutil.rmtree"):
                    return None
                if any(k.arg in ("src", "dst", "path", "name", "onerror", "onexc", "copy_function")
                       for k in node.keywords):
                    return None
                for i in _PY_WRITE_CALLS[d]:
                    t = where(text(node.args[i])) if i < len(node.args) else None
                    if t is None:
                        return None
                    targets.append(t)
                if d in _PY_INTO_DIR and os.path.isdir(targets[-1]):
                    src = where(text(node.args[0])) if node.args else None
                    if src is None:
                        return None
                    targets.append(os.path.join(targets[-1], os.path.basename(src)))
            elif d not in _PY_PURE_CALLS and not (
                    d.startswith("factgate_hook.") and d.split(".", 1)[1] in _PY_HOOK_PURE):
                return None
        elif isinstance(f, ast.Attribute):
            if f.attr in ("write_text", "write_bytes"):
                t = where(path_of(f.value))
                if t is None:
                    return None
                targets.append(t)
            elif f.attr not in _PY_METHODS or (uses_path and f.attr == "replace"):
                return None
        else:
            return None
    return targets


def _py_without_code(command, cwd, drop=()):
    """The command as text: each cleared python part cut to its verb and its redirects, the heredoc it reads
    dropped, and so are the heredoc bodies at the positions in `drop`. Every other body is kept."""
    shell, docs = _p7_heredocs(command)
    py = _py_parts(command, cwd)
    if py["segs"]:
        segs = _p7_parse(command, cwd)[0]
        for k in sorted(py["segs"], reverse=True):
            s = segs[k]
            stub = s["cmds"][0][0] + "".join(" > " + json.dumps(t) if t else " >" for t in s["targets"])
            shell = shell[:s["start"]] + stub + shell[s["end"]:]
    drop = set(drop) | py["docs"]
    return "\n".join([shell] + [d["body"] for d in docs if d["pos"] not in drop])


def _py_lands(target, roots, owns):
    """Does one resolved python target land on a guarded thing, or on a directory that holds one?"""
    return owns(target) or any(_inside(r, target) for r in roots)


def _factgate_check():
    """(exit code, output lines). Raises on timeout or if factgate is not there."""
    if not os.path.isfile(FACTGATE):
        raise RuntimeError(f"factgate is not at {FACTGATE}")
    r = subprocess.run([sys.executable, FACTGATE, "--state-dir", FACTGATE_STATE,
                        "check", FACTS], capture_output=True, text=True, timeout=TIMEOUT)
    out = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode, [l for l in out if l and not l.startswith(("CHECK FAILED", "a fact that"))]


def _log_catch(rule, target, why, command=None):
    """One JSON line per refusal. Never lets a logging failure change the decision.

    ORDER 16: a Bash refusal also carries the full `command` (capped), because `where` is cut
    at 120 characters and #20 could not be diagnosed from it. `where` itself is unchanged."""
    try:
        n = 0
        if os.path.exists(CATCHES):
            for line in open(CATCHES, encoding="utf-8"):
                line = line.strip()
                if line:
                    try:
                        n = max(n, int(json.loads(line).get("n", 0)))
                    except ValueError:
                        pass
        rec = {"n": n + 1, "date": time.strftime("%Y-%m-%d"), "stage": "pre-commit",
               "caught_by": "factgate-hook", "where": f"{rule} {target}",
               "what": why, "fixed_in": "refused"}
        if command is not None:
            rec["command"] = command[:_CATCH_COMMAND_MAX]
        os.makedirs(os.path.dirname(CATCHES), exist_ok=True)
        with open(CATCHES, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass                                              # a deny is still a deny


def _deny(rule, target, why, command=None):
    _log_catch(rule, target, why, command)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def _allow():
    return {}


# ------------------------------------------------------------------- the hooks

async def pre_tool_use(input, tool_use_id, context):
    """Refuse a fact write the record cannot account for. Never raises."""
    global _real_memo
    on_fact_path = False
    _real_memo = {}
    try:
        tool = (input or {}).get("tool_name") or ""
        tool_input = (input or {}).get("tool_input")
        cwd = (input or {}).get("cwd") or BRAIN

        if tool in WRITE_TOOLS:
            for raw in _targets(tool_input):
                target = _resolve(raw, cwd)
                if _factgate_owns(target):
                    on_fact_path = True
                    return _deny("P3", target,
                                 "factgate is the check on your own writes, so you do not "
                                 "edit it. Ask the user if it needs changing.")
                if target == _real(RUNNER):
                    return _deny("P3", target, _P6_WHY)
                if _build_path_owns(target):
                    return _deny("P7", target, _P7_WHY)
                if _inside(target, FACTS):
                    on_fact_path = True
                    if os.path.dirname(target) != _real(FACTS):
                        return _deny("P2", target,
                                     "the fact base is flat: a file in a subdirectory is "
                                     "watched by nothing. Write it at the top level.")
                    code, findings = _factgate_check()
                    if code != 0:
                        head = "\n".join(findings[:5])
                        return _deny("P1", target,
                                     f"the fact base is not clean, so a new fact cannot be "
                                     f"told apart from the unexplained ones:\n{head}\n"
                                     f"{_CLEARED_BY}")

        elif tool == "Bash":
            command = (tool_input or {}).get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str) and command:
                if (_about_factgate(command, cwd) or _py_parts(command, cwd)["targets"]) and (
                        _runs_factgate_recording(command, cwd=cwd)
                        or _writes_factgate(command, cwd)):
                    on_fact_path = True
                    return _deny("P5", command[:120],
                                 "factgate is the check on your own writes, so you do not "
                                 "run its recording verbs or write to what it owns. "
                                 "Reading it — `check`, `cat`, `wc`, `grep` — is fine.",
                                 command)
                if _writes_facts(command, cwd):
                    on_fact_path = True
                    return _deny("P4", command[:120],
                                 "write facts with Write/Edit so they are recorded. A shell "
                                 "write leaves the fact base off-ledger and the nightly "
                                 "will flag it.", command)
                if _writes_build_path(command, cwd):
                    return _deny("P7", command[:120], _P7_BASH_WHY, command)
                if _starts_build_or_restart(command, cwd):
                    return _deny("P6", command[:120], _P6_WHY, command)
        return _allow()
    except Exception as exc:
        # Fail closed on the fact path, open everywhere else.
        if on_fact_path:
            return _deny("P0", "unknown", f"the fact gate could not decide, so it refused: "
                                          f"{type(exc).__name__}: {exc}. {_CLEARED_BY}")
        return _allow()
    finally:
        _real_memo = None


async def post_tool_use(input, tool_use_id, context):
    """Record a fact write that succeeded. Never raises, never blocks."""
    try:
        tool = (input or {}).get("tool_name") or ""
        if tool not in WRITE_TOOLS:
            return {}
        cwd = (input or {}).get("cwd") or BRAIN
        session = (input or {}).get("session_id") or "?"
        for raw in _targets((input or {}).get("tool_input")):
            target = _resolve(raw, cwd)
            if not _inside(target, FACTS) or os.path.dirname(target) != _real(FACTS):
                continue
            reason = (f"odin: {tool} tool_use {tool_use_id or '?'} session {session}")
            only_f5 = False
            try:
                r = subprocess.run([sys.executable, FACTGATE, "--state-dir", FACTGATE_STATE,
                                    "record", FACTS, os.path.basename(target),
                                    "--reason", reason],
                                   capture_output=True, text=True, timeout=TIMEOUT)
                ok = r.returncode == 0 or (r.returncode == 3 and "unchanged" in
                                           (r.stderr or "") + (r.stdout or ""))
                detail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[0]
                # ORDER 76: a missing Category: line is Odin's own fix, but only when it is the
                # ONE reason (if F1 also failed, a line would not help: Tell the user stays)
                only_f5 = bool(re.search(r"^refused on 1 precondition\(s\): F5_CATEGORY_DECLARED$",
                                         r.stderr or "", flags=re.M))
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if not ok and only_f5:
                return {"hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"factgate did NOT record {os.path.basename(target)}: it has no valid "
                        f"Category: line. Add one near the top (names: "
                        f"workspace/categories/CATEGORIES.txt), then write the file again.")}}
            if not ok:
                return {"hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"factgate did NOT record {os.path.basename(target)} ({detail}). "
                        f"The fact is written but off-ledger, and tonight's nightly will "
                        f"flag it. Tell the user rather than trying to record it yourself.")}}
        return {}
    except Exception:
        return {}


def record_remember(filename):
    """The /remember route's own record call. Returns (ok, detail); never raises."""
    try:
        r = subprocess.run([sys.executable, FACTGATE, "--state-dir", FACTGATE_STATE,
                            "record", FACTS, os.path.basename(filename),
                            "--reason", f"/remember (UI) {os.path.basename(filename)}"],
                           capture_output=True, text=True, timeout=TIMEOUT)
        ok = r.returncode == 0 or (r.returncode == 3 and "unchanged" in
                                   (r.stderr or "") + (r.stdout or ""))
        return ok, ((r.stderr or r.stdout or "").strip().splitlines() or [""])[0]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
