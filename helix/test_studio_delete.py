#!/usr/bin/env python3
"""Deleting one image from a series — without corrupting the verdicts on the others.

Notes are keyed by `output_idx`. Remove image #1 from a series of four and every later image
shifts down one, so a comment written about #2 would silently start describing #3. Deleting is
therefore not just "unlink the file": the surviving notes have to move with their images.

Deletion is real and irreversible — the file leaves the disk. So the endpoint also refuses to
touch anything outside OUT_ROOT, because it takes a path that came back from the database and
turns it into an unlink().

Run:  .venv/bin/python test_studio_delete.py
"""
import json, os, sys, tempfile

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
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


def fresh():
    """A finished 4-image job whose files really exist under a temp OUT_ROOT."""
    d = tempfile.mkdtemp()
    out_root = os.path.join(d, "studio")
    os.makedirs(os.path.join(out_root, "default"), exist_ok=True)
    urls = []
    for i in range(4):
        rel = f"default/job9_{i}.png"
        open(os.path.join(out_root, rel), "wb").write(b"\x89PNG\r\n\x1a\n" + bytes([i]))
        urls.append("/static/studio/" + rel)

    path = os.path.join(d, "studio.db")
    conn = sdb.connect(path)
    sdb.init_db(conn)
    jid = sdb.create_job(conn, "user", "image", prompt="four lighthouses", model="sd15")
    sdb.set_status(conn, jid, "running")
    sdb.finish_job(conn, jid, urls)
    conn.close()

    app.app.config["DB_PATH"] = path
    app.app.config["OUT_ROOT"] = out_root
    return path, out_root, jid


def outputs_of(path, jid):
    conn = sdb.connect(path)
    try:
        return json.loads(sdb.get_job(conn, jid)["outputs"])
    finally:
        conn.close()


def notes_of(path, jid):
    conn = sdb.connect(path)
    try:
        return [(n["output_idx"], n["comment"]) for n in sdb.get_notes(conn, jid)]
    finally:
        conn.close()


def test_deleting_an_image_removes_it_from_the_series():
    path, out_root, jid = fresh()
    with app.app.test_client() as tc:
        r = tc.delete(f"/studio/api/job/{jid}/output/1")
        check("the call succeeds", r.status_code == 200, f"status={r.status_code}")
    outs = outputs_of(path, jid)
    check("three images remain", len(outs) == 3, f"outputs={outs}")
    check("the right one went", all("job9_1.png" not in o for o in outs), f"outputs={outs}")


def test_deleting_an_image_removes_the_file_from_disk():
    path, out_root, jid = fresh()
    victim = os.path.join(out_root, "default/job9_2.png")
    check("the file exists first", os.path.exists(victim))
    with app.app.test_client() as tc:
        tc.delete(f"/studio/api/job/{jid}/output/2")
    check("the file is gone from disk", not os.path.exists(victim))
    check("the others are untouched",
          os.path.exists(os.path.join(out_root, "default/job9_0.png"))
          and os.path.exists(os.path.join(out_root, "default/job9_3.png")))


def test_surviving_comments_follow_their_own_images():
    """THE TRAP. Comments on #2 and #3 must still describe #2 and #3 after #1 is deleted."""
    path, out_root, jid = fresh()
    conn = sdb.connect(path)
    sdb.set_note(conn, jid, 1, "about image ONE")
    sdb.set_note(conn, jid, 2, "about image TWO")
    sdb.set_note(conn, jid, 3, "about image THREE")
    conn.close()

    with app.app.test_client() as tc:
        tc.delete(f"/studio/api/job/{jid}/output/1")

    notes = dict(notes_of(path, jid))
    check("the deleted image's comment is gone", "about image ONE" not in notes.values(),
          f"notes={notes}")
    check("the comment for the old #2 moved to index 1", notes.get(1) == "about image TWO",
          f"notes={notes}")
    check("the comment for the old #3 moved to index 2", notes.get(2) == "about image THREE",
          f"notes={notes}")
    check("no note points past the end", max(notes) < len(outputs_of(path, jid)), f"notes={notes}")


def test_deleting_the_last_image_leaves_an_empty_series_not_a_broken_one():
    path, out_root, jid = fresh()
    with app.app.test_client() as tc:
        for _ in range(4):
            tc.delete(f"/studio/api/job/{jid}/output/0")
    check("the series is empty", outputs_of(path, jid) == [], f"outputs={outputs_of(path, jid)}")
    conn = sdb.connect(path)
    try:
        check("the job record survives as history", sdb.get_job(conn, jid) is not None)
    finally:
        conn.close()


def test_a_bad_index_is_refused():
    path, out_root, jid = fresh()
    with app.app.test_client() as tc:
        check("out of range is a 404", tc.delete(f"/studio/api/job/{jid}/output/9").status_code == 404)
    check("nothing was removed", len(outputs_of(path, jid)) == 4)


def test_deletion_cannot_escape_the_output_root():
    """The path comes out of the DB and becomes an unlink(). Guard it."""
    path, out_root, jid = fresh()
    outside = os.path.join(os.path.dirname(out_root), "precious.txt")
    open(outside, "w").write("do not delete me")

    conn = sdb.connect(path)
    conn.execute("UPDATE jobs SET outputs=? WHERE id=?",
                 (json.dumps(["/static/studio/../../precious.txt"]), jid))
    conn.commit(); conn.close()

    with app.app.test_client() as tc:
        tc.delete(f"/studio/api/job/{jid}/output/0")
    check("a path escaping OUT_ROOT is not unlinked", os.path.exists(outside),
          "traversal deleted a file outside the output root")


print("\nHELIX — deleting images without corrupting verdicts\n" + "=" * 52)
test_deleting_an_image_removes_it_from_the_series()
test_deleting_an_image_removes_the_file_from_disk()
test_surviving_comments_follow_their_own_images()
test_deleting_the_last_image_leaves_an_empty_series_not_a_broken_one()
test_a_bad_index_is_refused()
test_deletion_cannot_escape_the_output_root()
print("=" * 52)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
