"""Helix Studio database layer.

SQLite, stdlib only. All timestamps are UTC via datetime('now').

Video invariant (enforced HERE, not just in the API): a job of kind 'video'
is created with status 'awaiting_confirm' and can only reach 'queued' through
confirm_video(). claim_next() only ever picks up 'queued' jobs, so an
unconfirmed video can never run.
"""
import json
import sqlite3

LOCAL_KINDS = ("image", "inpaint", "variation", "upscale", "crop")
KINDS = LOCAL_KINDS + ("video",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  output_idx INTEGER NOT NULL,
  comment TEXT NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(job_id, output_idx));

CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY,
  submitter TEXT NOT NULL CHECK(submitter IN ('user','helix')),
  kind TEXT NOT NULL CHECK(kind IN ('image','inpaint','variation','upscale','crop','video')),
  prompt TEXT, negative TEXT, model TEXT, seed INTEGER,
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','awaiting_confirm','running','done','failed','cancelled','director_pending')),
  error TEXT, price_quote_usd REAL, cost_usd REAL,
  project_id INTEGER REFERENCES projects(id),
  parent_job_id INTEGER REFERENCES jobs(id),
  outputs TEXT NOT NULL DEFAULT '[]',
  created TEXT NOT NULL, started TEXT, finished TEXT);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def create_project(conn, name):
    cur = conn.execute(
        "INSERT INTO projects(name, created) VALUES(?, datetime('now'))",
        (name,))
    conn.commit()
    return cur.lastrowid


def list_projects(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM projects ORDER BY id")]


def create_job(conn, submitter, kind, prompt=None, negative=None, model=None,
               seed=None, params=None, project_id=None, parent_job_id=None,
               price_quote_usd=None):
    # Video invariant: never enters the queue directly.
    status = "awaiting_confirm" if kind == "video" else "queued"
    if params is None:
        params = {}
    if not isinstance(params, str):
        params = json.dumps(params)
    cur = conn.execute(
        """INSERT INTO jobs(submitter, kind, prompt, negative, model, seed,
                            params, status, price_quote_usd, project_id,
                            parent_job_id, created)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (submitter, kind, prompt, negative, model, seed, params, status,
         price_quote_usd, project_id, parent_job_id))
    conn.commit()
    return cur.lastrowid


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(conn, status=None, submitter=None, project_id=None):
    q, args = "SELECT * FROM jobs WHERE 1=1", []
    if status is not None:
        q += " AND status=?"; args.append(status)
    if submitter is not None:
        q += " AND submitter=?"; args.append(submitter)
    if project_id is not None:
        q += " AND project_id=?"; args.append(project_id)
    q += " ORDER BY id"
    return [dict(r) for r in conn.execute(q, args)]


def confirm_video(conn, job_id):
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError("no such job %s" % job_id)
    if job["kind"] != "video":
        raise ValueError("job %s is not a video job" % job_id)
    if job["status"] != "awaiting_confirm":
        raise ValueError("job %s is %s, not awaiting_confirm"
                         % (job_id, job["status"]))
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
    conn.commit()


def claim_next(conn):
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status='running', started=datetime('now') WHERE id=?",
        (row["id"],))
    conn.commit()
    return get_job(conn, row["id"])


def finish_job(conn, job_id, outputs, cost_usd=None):
    conn.execute(
        """UPDATE jobs SET status='done', outputs=?, cost_usd=?,
           finished=datetime('now') WHERE id=?""",
        (json.dumps(list(outputs)), cost_usd, job_id))
    conn.commit()


def fail_job(conn, job_id, error):
    conn.execute(
        """UPDATE jobs SET status='failed', error=?,
           finished=datetime('now') WHERE id=?""",
        (str(error), job_id))
    conn.commit()


def recover_on_boot(conn):
    """Local jobs found running are reproducible -> requeue.
    Video jobs found running may already have spent money -> fail them."""
    ph = ",".join("?" * len(LOCAL_KINDS))
    requeued = conn.execute(
        "UPDATE jobs SET status='queued', started=NULL "
        "WHERE status='running' AND kind IN (%s)" % ph,
        LOCAL_KINDS).rowcount
    failed = conn.execute(
        "UPDATE jobs SET status='failed', error='interrupted by restart', "
        "finished=datetime('now') WHERE status='running' AND kind='video'"
    ).rowcount
    conn.commit()
    return requeued, failed


def lineage(conn, job_id):
    """Walk parent_job_id up to the root. Returns [job, parent, ..., root]."""
    chain, seen = [], set()
    cur = get_job(conn, job_id)
    while cur is not None and cur["id"] not in seen:
        chain.append(cur)
        seen.add(cur["id"])
        pid = cur["parent_job_id"]
        cur = get_job(conn, pid) if pid is not None else None
    return chain


def set_status(conn, job_id, status):
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    conn.commit()


# ── review notes ──────────────────────────────────────────────────────────────
# the user's verdict on a generated image IS his comment: "the ones with no comments will have
# passed" (2026-09-06). So a PASSED image has no row here at all — absence is the signal,
# rather than a flag column that would let "passed" and "never looked at" drift apart.

def set_note(conn, job_id, output_idx, comment):
    """Write or replace one image's comment. An empty comment DELETES the row, because
    clearing the text is the same gesture as never having objected."""
    comment = (comment or "").strip()
    if not comment:
        conn.execute("DELETE FROM notes WHERE job_id=? AND output_idx=?", (job_id, output_idx))
        conn.commit()
        return None
    conn.execute(
        """INSERT INTO notes(job_id, output_idx, comment, created)
           VALUES(?,?,?,datetime('now'))
           ON CONFLICT(job_id, output_idx)
           DO UPDATE SET comment=excluded.comment, created=excluded.created""",
        (job_id, output_idx, comment))
    conn.commit()
    return comment


def get_notes(conn, job_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notes WHERE job_id=? ORDER BY output_idx", (job_id,))]


def latest_done_job(conn):
    """The newest finished job that actually produced images — what the review feed opens on."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='done' AND outputs NOT IN ('[]','') "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def delete_output(conn, job_id, output_idx):
    """Drop one image from a job and keep the surviving verdicts pointing at the right pictures.

    Notes are keyed by output_idx, so compacting the outputs list shifts every later image down
    one. Without the reindex below, a comment written about #2 would silently start describing
    #3 — the verdict survives but attaches to the wrong image, which is worse than losing it.

    Returns the removed URL, or None if the index does not exist.
    """
    job = get_job(conn, job_id)
    if job is None:
        return None
    outputs = json.loads(job["outputs"] or "[]")
    if not (0 <= output_idx < len(outputs)):
        return None

    removed = outputs.pop(output_idx)
    conn.execute("UPDATE jobs SET outputs=? WHERE id=?", (json.dumps(outputs), job_id))
    conn.execute("DELETE FROM notes WHERE job_id=? AND output_idx=?", (job_id, output_idx))
    # Shift later notes down to follow their images. Ordered ascending so the UNIQUE
    # (job_id, output_idx) constraint is never transiently violated.
    for row in conn.execute(
            "SELECT id, output_idx FROM notes WHERE job_id=? AND output_idx>? ORDER BY output_idx",
            (job_id, output_idx)).fetchall():
        conn.execute("UPDATE notes SET output_idx=? WHERE id=?", (row["output_idx"] - 1, row["id"]))
    conn.commit()
    return removed
