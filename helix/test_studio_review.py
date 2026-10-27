#!/usr/bin/env python3
"""The review loop: the user judges a series, Helix acts on it.

the user's words (2026-09-06):
  "it shows me all the images and each has a coment section and there will my verdict be
   the ones with no comments will have passed it shows me like a feed where i scroll down"

So the comment IS the verdict. There is no keep/reject control — silence is approval. A
passed image therefore has NO row in `notes`: absence is the signal, which is his model
directly rather than a translation of it into flags.

Pressing Go creates one child job per commented output, `parent_job_id` set so the existing
lineage() answers "where did 3b come from", with the note appended as direction.

Run:  .venv/bin/python test_studio_review.py
"""
import json, os, sys, tempfile

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"     # no renders in tests
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "studio"))
import app  # noqa: E402
import db as sdb  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh_db():
    """A throwaway studio DB with one finished 3-image job, wired into the Helix app."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "studio.db")
    conn = sdb.connect(path)
    sdb.init_db(conn)
    jid = sdb.create_job(conn, "user", "image", prompt="a lighthouse at dusk", model="sd15")
    sdb.set_status(conn, jid, "running")
    sdb.finish_job(conn, jid, ["/static/studio/a.png", "/static/studio/b.png", "/static/studio/c.png"])
    conn.close()
    app.app.config["DB_PATH"] = path
    return path, jid


# ── notes ─────────────────────────────────────────────────────────────────────

def test_a_comment_is_stored_against_one_image():
    path, jid = fresh_db()
    conn = sdb.connect(path)
    try:
        sdb.set_note(conn, jid, 1, "too dark")
        notes = sdb.get_notes(conn, jid)
        check("the comment is stored", len(notes) == 1, f"notes={notes}")
        check("against the right image", notes and notes[0]["output_idx"] == 1, f"notes={notes}")
        check("with the right text", notes and notes[0]["comment"] == "too dark", f"notes={notes}")
    finally:
        conn.close()


def test_passed_images_have_no_row_at_all():
    """Absence IS the verdict. Two of three commented means exactly two rows."""
    path, jid = fresh_db()
    conn = sdb.connect(path)
    try:
        sdb.set_note(conn, jid, 0, "wrong colour")
        sdb.set_note(conn, jid, 2, "hands")
        idxs = sorted(n["output_idx"] for n in sdb.get_notes(conn, jid))
        check("only the commented images have rows", idxs == [0, 2], f"idxs={idxs}")
    finally:
        conn.close()


def test_rewriting_a_comment_replaces_it():
    path, jid = fresh_db()
    conn = sdb.connect(path)
    try:
        sdb.set_note(conn, jid, 1, "too dark")
        sdb.set_note(conn, jid, 1, "actually too bright")
        notes = sdb.get_notes(conn, jid)
        check("one row, not two", len(notes) == 1, f"notes={notes}")
        check("the newer text wins", notes[0]["comment"] == "actually too bright", f"notes={notes}")
    finally:
        conn.close()


def test_clearing_a_comment_un_rejects_the_image():
    """Deleting the text is the same gesture as never having objected."""
    path, jid = fresh_db()
    conn = sdb.connect(path)
    try:
        sdb.set_note(conn, jid, 1, "too dark")
        sdb.set_note(conn, jid, 1, "   ")
        check("an emptied comment removes the row", sdb.get_notes(conn, jid) == [],
              f"notes={sdb.get_notes(conn, jid)}")
    finally:
        conn.close()


# ── regenerate ────────────────────────────────────────────────────────────────

def test_go_creates_one_child_per_commented_image():
    path, jid = fresh_db()
    with app.app.test_client() as tc:
        r = tc.post(f"/studio/api/job/{jid}/regenerate", json={"notes": [
            {"output_idx": 0, "comment": "too dark"},
            {"output_idx": 2, "comment": "the hands are wrong"},
        ]}).get_json()
        check("the call succeeds", r.get("ok") is True, f"{r}")
        check("it made exactly two children", r.get("created") == 2, f"{r}")

    conn = sdb.connect(path)
    try:
        kids = [j for j in sdb.list_jobs(conn) if j["parent_job_id"] == jid]
        check("both children point at the parent", len(kids) == 2, f"kids={len(kids)}")
        check("children are variations", all(k["kind"] == "variation" for k in kids),
              f"kinds={[k['kind'] for k in kids]}")
        check("each note reached its child's prompt",
              any("too dark" in (k["prompt"] or "") for k in kids)
              and any("hands are wrong" in (k["prompt"] or "") for k in kids),
              f"prompts={[k['prompt'] for k in kids]}")
        check("the original prompt is still there",
              all("lighthouse" in (k["prompt"] or "") for k in kids),
              f"prompts={[k['prompt'] for k in kids]}")
        check("children remember which image they came from",
              all(json.loads(k["params"]).get("source_output_idx") in (0, 2) for k in kids),
              f"params={[k['params'] for k in kids]}")
    finally:
        conn.close()


def test_a_fully_passed_series_regenerates_nothing():
    """The whole point of silence-as-approval."""
    path, jid = fresh_db()
    with app.app.test_client() as tc:
        r = tc.post(f"/studio/api/job/{jid}/regenerate", json={"notes": []}).get_json()
        check("nothing is created", r.get("created") == 0, f"{r}")
    conn = sdb.connect(path)
    try:
        check("no children exist", [j for j in sdb.list_jobs(conn) if j["parent_job_id"] == jid] == [])
    finally:
        conn.close()


def test_blank_comments_are_ignored_not_queued():
    path, jid = fresh_db()
    with app.app.test_client() as tc:
        r = tc.post(f"/studio/api/job/{jid}/regenerate", json={"notes": [
            {"output_idx": 0, "comment": "   "},
            {"output_idx": 1, "comment": "too dark"},
        ]}).get_json()
        check("only the real comment queued", r.get("created") == 1, f"{r}")


def test_regenerate_persists_the_notes_it_acted_on():
    """So Odin can read what the user objected to, later, in conversation."""
    path, jid = fresh_db()
    with app.app.test_client() as tc:
        tc.post(f"/studio/api/job/{jid}/regenerate", json={"notes": [
            {"output_idx": 1, "comment": "too dark"}]})
    conn = sdb.connect(path)
    try:
        check("the note is on record", [n["comment"] for n in sdb.get_notes(conn, jid)] == ["too dark"],
              f"notes={sdb.get_notes(conn, jid)}")
    finally:
        conn.close()


# ── the feed's data source ────────────────────────────────────────────────────

def test_the_feed_can_fetch_the_latest_finished_series():
    path, jid = fresh_db()
    with app.app.test_client() as tc:
        j = tc.get("/studio/api/queue/latest_done").get_json()
        check("it returns a job", (j.get("job") or {}).get("id") == jid, f"{j}")
        check("with its images", len((j.get("job") or {}).get("outputs") or []) == 3, f"{j}")


def test_a_fetched_job_carries_its_existing_notes():
    """Reopening the feed must show what was already written, not a blank slate."""
    path, jid = fresh_db()
    conn = sdb.connect(path); sdb.set_note(conn, jid, 2, "hands"); conn.close()
    with app.app.test_client() as tc:
        j = tc.get(f"/studio/api/job/{jid}").get_json()
        job = j.get("job") or j
        check("the saved note comes back with the job",
              [(n["output_idx"], n["comment"]) for n in (job.get("notes") or [])] == [(2, "hands")],
              f"job notes={job.get('notes')}")


print("\nHELIX — Studio review loop (comment = verdict)\n" + "=" * 52)
test_a_comment_is_stored_against_one_image()
test_passed_images_have_no_row_at_all()
test_rewriting_a_comment_replaces_it()
test_clearing_a_comment_un_rejects_the_image()
test_go_creates_one_child_per_commented_image()
test_a_fully_passed_series_regenerates_nothing()
test_blank_comments_are_ignored_not_queued()
test_regenerate_persists_the_notes_it_acted_on()
test_the_feed_can_fetch_the_latest_finished_series()
test_a_fetched_job_carries_its_existing_notes()
print("=" * 52)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
