"""Helix's front desk — pure logic, no Flask, no SDK, no model calls.

Everything in here is a function of its arguments plus one JSON file, so the whole
front-desk behaviour can be tested offline in milliseconds. app.py owns the wiring;
this module owns the decisions.
"""
import os, json, time, uuid, tempfile


def notepad_path(brain):
    return os.path.join(brain, "memory", "notepad.json")


def _empty():
    return {"notes": [], "last_job": None}


def load_pad(path):
    """A missing or corrupt pad reads as empty — a bad file must never stop Helix talking."""
    try:
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return _empty()
        return {"notes": list(d.get("notes") or []), "last_job": d.get("last_job")}
    except Exception:
        return _empty()


def save_pad(path, pad):
    """Atomic: write a sibling temp file and rename, so a crash mid-write cannot truncate
    the pad. The temp file lives in the same directory because rename is only atomic
    within one filesystem."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd_, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".notepad-", suffix=".tmp")
    try:
        with os.fdopen(fd_, "w") as f:
            json.dump(pad, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def add_note(path, text):
    text = (text or "").strip()
    pad = load_pad(path)
    if not text:
        return pad
    pad["notes"].append({"id": uuid.uuid4().hex[:8], "text": text,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_pad(path, pad)
    return pad


def clear_notes(path):
    pad = load_pad(path)
    pad["notes"] = []
    save_pad(path, pad)
    return pad


def set_last_job(path, job_id, question, answer):
    pad = load_pad(path)
    pad["last_job"] = {"job_id": job_id, "question": question, "answer": answer,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_pad(path, pad)
    return pad


# ---- control markers -------------------------------------------------------------------
# Helix has no tools (by design). It expresses every decision as a marker in its own text,
# which is stripped before anything is spoken. Order matters: JOB carries a payload, so it
# is checked first and everything after it belongs to the job.
M_JOB, M_SEND, M_FULL = "[[JOB]]", "[[SEND]]", "[[FULL]]"

# Said when Helix emits a marker and nothing else. Silence would read as a crash.
_ACK = "Right away, sir."


def parse_control(reply):
    """Split a front-lane reply into (what to say out loud, what to do)."""
    text = reply or ""
    i = text.find(M_JOB)
    if i != -1:
        spoken = text[:i].strip()
        job = text[i + len(M_JOB):].strip()
        if job:
            return (spoken or _ACK), {"kind": "job", "text": job}
        return (spoken or _ACK), {"kind": "talk"}
    for marker, kind in ((M_SEND, "send"), (M_FULL, "full")):
        i = text.find(marker)
        if i != -1:
            spoken = (text[:i] + " " + text[i + len(marker):]).strip()
            return (spoken or _ACK), {"kind": kind}
    return text.strip(), {"kind": "talk"}


# ---- the brief -------------------------------------------------------------------------
# This is the whole of Helix's mind. It gets NO memory files, NO fact files, NO tools — so
# this text plus the last few turns of talk is everything it knows. Keep it short: it is
# re-sent on every session and its length is paid for in latency.
BRIEF = f"""You are HELIX, speaking aloud to ONE man: the user. You are his front desk — the voice
that answers instantly so he is never left sitting in silence.

REGISTER: a formal butler. "Yes sir." "Of course, sir." "Right away, sir." Warm, dry, never
servile-to-the-point-of-parody, never chatty. One or two sentences. You are being SPOKEN, not
read, so no markdown, no lists, no code, no headings.

THE ONE HARD RULE — you may never state a fact and you may never touch a file.
You have no tools and no memory of his work. Anything factual (a number, a date, a result, what
a file says, what happened before), and anything that requires doing (reading, writing, running,
searching, building, checking), goes to ODIN. Odin is the one who actually works. You do not
guess, you do not half-remember, you do not summarise from your own head. If you are not certain
it is pure conversation, hand it to Odin — handing over something you could have handled is
harmless; answering something you could not is not.

HOW YOU HAND OVER: end your reply with a line beginning {M_JOB} followed by the job, written
clearly, in your own words, as an instruction to Odin. Say something short to the user first.
  the user: "how many photos are in a friend's archive?"
  You:  "I'll have Odin check, sir.
         {M_JOB} How many photographs are in the a friend archive? Report the count."

WHEN ODIN IS BUSY: do not interrupt him. Use {M_JOB} exactly the same way — the system will put
it on his notepad instead of sending it, and tell you it did. Say so: "Noted, sir. I'll give that
to Odin when he's free."

WHEN THERE ARE NOTES WAITING and Odin has just finished: read them back and ask whether to send
them. If the user agrees, end your reply with {M_SEND} on its own line. If he says no, or changes
one, do not send — just talk.

REPORTING A RESULT: when Odin finishes, you will be handed his answer. Tell the user the HEADLINE in
your own words — one or two sentences, the outcome first, no detail he did not ask for. Do not
recite it. Do not repeat numbers out of it unless he asked for that number. If he wants the whole
thing, end your reply with {M_FULL} on its own line and the system will give him Odin's own text.

Never mention these markers to the user, never explain them, never speak them aloud."""


def status_line(odin_busy, notes, has_last_job):
    """One line of world-state, prepended to each user message. Helix has no tools, so this
    is the only way it learns whether Odin is working or what is on the pad."""
    bits = ["[state: Odin is busy working" if odin_busy else "[state: Odin is free"]
    if odin_busy:
        bits.append(f"— do not interrupt him; a {M_JOB} now goes on the notepad instead")
    if notes:
        listed = "; ".join(n["text"] for n in notes)
        bits.append(f"| notepad waiting ({len(notes)}): {listed} "
                    f"— read these back and use {M_SEND} if he agrees")
    if has_last_job:
        bits.append(f"| Odin's last full answer is available — use {M_FULL} if he asks for it")
    return " ".join(bits) + "]"
