"""Studio HTTP API — thin blueprint: parse, validate, call db."""
import json

from flask import Blueprint, current_app, g, jsonify, request

import db

api = Blueprint("api", __name__, url_prefix="/api")

SUBMITTERS = ("user", "helix")


def get_conn():
    if "db" not in g:
        g.db = db.connect(current_app.config["DB_PATH"])
    return g.db


def close_conn(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def get_video_provider():
    """Tests may inject VIDEO_PROVIDER_FACTORY; default is the fal provider
    (None when FAL_KEY is absent -> 503)."""
    factory = current_app.config.get("VIDEO_PROVIDER_FACTORY")
    if factory is not None:
        return factory()
    from workers.video import get_provider
    return get_provider(current_app.config.get("OUT_ROOT"))


def job_view(conn, row):
    out = dict(row)
    out["params"] = json.loads(out["params"] or "{}")
    out["outputs"] = json.loads(out["outputs"] or "[]")
    return out


@api.post("/job")
def submit_job():
    p = request.get_json(force=True, silent=True) or {}
    submitter = p.get("submitter")
    kind = p.get("kind")
    if submitter not in SUBMITTERS:
        return jsonify(error="submitter must be one of %s" % (SUBMITTERS,)), 400
    if kind not in db.KINDS:
        return jsonify(error="kind must be one of %s" % (db.KINDS,)), 400

    price = None
    if kind == "video":
        provider = get_video_provider()
        if provider is None:
            return jsonify(error="video provider not configured"), 503
        try:
            price = provider.quote(p)
        except ValueError as e:
            return jsonify(error=str(e)), 400

    conn = get_conn()
    jid = db.create_job(
        conn, submitter, kind,
        prompt=p.get("prompt"), negative=p.get("negative"),
        model=p.get("model"), seed=p.get("seed"), params=p.get("params"),
        project_id=p.get("project_id"), parent_job_id=p.get("parent_job_id"),
        price_quote_usd=price)
    job = db.get_job(conn, jid)
    resp = {"id": jid, "status": job["status"]}
    if price is not None:
        resp["price_quote_usd"] = price
    return jsonify(resp)


@api.post("/job/<int:job_id>/confirm")
def confirm(job_id):
    conn = get_conn()
    if db.get_job(conn, job_id) is None:
        return jsonify(error="no such job"), 404
    try:
        db.confirm_video(conn, job_id)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    return jsonify(id=job_id, status="queued")


@api.get("/job/<int:job_id>")
def get_job(job_id):
    conn = get_conn()
    row = db.get_job(conn, job_id)
    if row is None:
        return jsonify(error="no such job"), 404
    out = job_view(conn, row)
    out["lineage"] = [job_view(conn, n) for n in db.lineage(conn, job_id)]
    # Reopening the review feed must show what was already written, not a blank slate.
    out["notes"] = db.get_notes(conn, job_id)
    return jsonify(out)


@api.get("/queue")
def queue():
    conn = get_conn()
    jobs = db.list_jobs(conn,
                        status=request.args.get("status") or None,
                        submitter=request.args.get("submitter") or None,
                        project_id=request.args.get("project_id", type=int))
    return jsonify(jobs=[job_view(conn, j) for j in jobs])


@api.post("/project")
def create_project():
    p = request.get_json(force=True, silent=True) or {}
    name = (p.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    conn = get_conn()
    try:
        pid = db.create_project(conn, name)
    except Exception:
        return jsonify(error="project name already exists"), 400
    return jsonify(id=pid, name=name)


@api.get("/projects")
def projects():
    return jsonify(projects=db.list_projects(get_conn()))


# ── review loop ───────────────────────────────────────────────────────────────
# the user reviews a finished series as a scrolling feed and comments on the ones he wants
# changed. No comment means it passed. Pressing Go turns each comment into one child job.

@api.get("/queue/latest_done")
def latest_done():
    """What the review feed opens on: the newest finished series."""
    conn = get_conn()
    job = db.latest_done_job(conn)
    if not job:
        return jsonify(job=None)
    view = job_view(conn, job)
    view["notes"] = db.get_notes(conn, job["id"])
    return jsonify(job=view)


@api.post("/job/<int:job_id>/regenerate")
def regenerate(job_id):
    """One child per commented image; nothing for the ones he left alone.

    Kind is `variation` and `parent_job_id` is set, so the child keeps the original's
    composition and the existing lineage() answers "where did this come from". The comment is
    appended to the prompt as direction rather than replacing it — he is saying what is wrong
    with THIS image, not describing a new one.
    """
    conn = get_conn()
    job = db.get_job(conn, job_id)
    if not job:
        return jsonify(ok=False, error="no such job"), 404

    outputs = json.loads(job["outputs"] or "[]")
    created = 0
    for item in (request.json or {}).get("notes", []):
        try:
            idx = int(item.get("output_idx"))
        except (TypeError, ValueError):
            continue
        comment = (item.get("comment") or "").strip()
        if not comment or not (0 <= idx < len(outputs)):
            continue                      # blank comment = passed; bad index = ignore

        db.set_note(conn, job_id, idx, comment)   # on record, so Odin can read it later
        params = json.loads(job["params"] or "{}")
        params["source_output_idx"] = idx
        params["source_image"] = outputs[idx]
        params["review_comment"] = comment
        db.create_job(conn, "user", "variation",
                      prompt=f"{job['prompt']}\n\n[change requested: {comment}]",
                      negative=job["negative"], model=job["model"], seed=job["seed"],
                      params=params, project_id=job["project_id"], parent_job_id=job_id)
        created += 1

    return jsonify(ok=True, created=created)


@api.delete("/job/<int:job_id>/output/<int:idx>")
def delete_output(job_id, idx):
    """Delete one image for good — the row AND the file.

    The path comes out of the database and becomes an unlink(), so it is resolved and checked
    against OUT_ROOT first. A stored path that escapes the output root drops the reference but
    is never followed to the filesystem.
    """
    import os

    conn = get_conn()
    removed = db.delete_output(conn, job_id, idx)
    if removed is None:
        return jsonify(ok=False, error="no such image"), 404

    out_root = os.path.realpath(current_app.config.get("OUT_ROOT") or "")
    rel = removed.split("/static/studio/", 1)[-1].lstrip("/")
    target = os.path.realpath(os.path.join(out_root, rel))
    if out_root and (target == out_root or target.startswith(out_root + os.sep)):
        try:
            os.remove(target)
        except FileNotFoundError:
            pass                      # already gone; the row is what mattered
        except OSError as e:
            return jsonify(ok=True, removed=removed, file_error=str(e))
    return jsonify(ok=True, removed=removed)


@api.get("/daemon")
def daemon_health():
    """Is the renderer actually up?

    Generate is a promise, and it can only be kept if render_daemon is running. When the daemon
    was down (2026-09-07) Studio still offered a live Generate button and accepted jobs that
    could never render — nothing on screen said why, so it simply looked broken. The UI polls
    this and says so instead.

    Cached briefly: the queue polls every few seconds and this must not turn into a health-check
    flood against a daemon that is busy generating.
    """
    import time as _t
    import requests

    url = (current_app.config.get("DAEMON_URL") or "http://127.0.0.1:7900").rstrip("/")
    cache = current_app.extensions.setdefault("daemon_health", {})
    now = _t.time()
    if cache.get("at", 0) + 3 > now:
        return jsonify(up=cache["up"], url=url, cached=True)

    try:
        # A busy daemon is still UP — it just isn't answering promptly while a render holds it.
        up = requests.get(url + "/health", timeout=2).status_code == 200
    except Exception:
        up = False
    cache.update(up=up, at=now)
    return jsonify(up=up, url=url, cached=False)
