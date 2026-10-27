"""Art-director handoff — thin adapter, NO critique logic here.

Image jobs submitted with params.art_director == true are not generated
directly: the worker writes an art_director job dir (the exact job_state.json
format that ~/helix/render_tools/art_director/art_director.py cmd_init
produces, mirrored field-for-field) and parks the studio job as
'director_pending'. Odin drives the critique loop as today (fact 113);
when images pass, attach_results() completes the studio job.
"""
import json
import os
import time

import db

DEFAULT_JOBS_ROOT = os.path.expanduser("~/helix/render_tools/art_director/jobs")

# mirrored from art_director.py MODEL_DEFAULTS
MODEL_DEFAULTS = {
    "sd15": {"width": 512, "height": 512, "steps": 25},
    "sdxl": {"width": 768, "height": 768, "steps": 20},
    "flux": {"width": 768, "height": 768, "steps": 4},
}


def _params(job):
    params = job.get("params") or "{}"
    if isinstance(params, str):
        params = json.loads(params)
    return params


def create_director_job(job, jobs_root=None):
    """Write jobs/<name>/job_state.json in art_director's cmd_init format.
    Returns the job dir path."""
    jobs_root = jobs_root or DEFAULT_JOBS_ROOT
    params = _params(job)
    model = job.get("model") or "sd15"
    md = MODEL_DEFAULTS.get(model, {})
    subject = {
        "id": "job%d" % job["id"],
        "prompt": job.get("prompt") or "",
        "negative": job.get("negative") or "",
        "model": model,
        "width": params.get("width", md.get("width", 768)),
        "height": params.get("height", md.get("height", 768)),
        "steps": params.get("steps", md.get("steps", 20)),
        "state": "pending",
        "strikes": 0,
        "current_file": None,
        "history": [],
    }
    st = {
        "name": "studio_job%d" % job["id"],
        "rubrics": [os.path.abspath(r) for r in params.get("rubrics", [])],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "subjects": [subject],
    }
    job_dir = os.path.join(jobs_root, st["name"])
    os.makedirs(job_dir, exist_ok=True)
    path = os.path.join(job_dir, "job_state.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, path)
    return job_dir


def attach_results(conn, job_id, passed_paths):
    """Finish a director_pending studio job with the images that passed."""
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError("no such job %s" % job_id)
    if job["status"] != "director_pending":
        raise ValueError("job %s is %s, not director_pending"
                         % (job_id, job["status"]))
    db.finish_job(conn, job_id, passed_paths)


def wrap_image_executor(inner, db_path, jobs_root=None):
    """Route image jobs: art_director param -> handoff + park; else inner."""
    def run(job):
        if _params(job).get("art_director"):
            create_director_job(job, jobs_root)
            conn = db.connect(db_path)
            try:
                db.set_status(conn, job["id"], "director_pending")
            finally:
                conn.close()
            return [], None
        return inner(job)
    return run
