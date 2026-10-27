"""Edit executors. Crop is pure PIL; variation/inpaint/upscale ride the
render_daemon's /img2img, /inpaint, /upscale endpoints (Tasks 9-11).

All of them resolve their source image from the parent job's outputs when
parent_job_id is set (params.source overrides), which is what gives every
edit a lineage chain back to the original generation."""
import json
import os

import requests
from PIL import Image

import db
from workers.images import TIMEOUT, copy_outputs, project_dir_name

WEB_PREFIX = "/static/studio/"


def job_params(job):
    params = job.get("params") or "{}"
    if isinstance(params, str):
        params = json.loads(params)
    return params


def resolve_source(source, out_root):
    """Map a /static/studio/... web path to a real file under out_root.
    Refuses anything that escapes out_root."""
    if not source or not source.startswith(WEB_PREFIX):
        raise ValueError("source must be a %s... path, got %r"
                         % (WEB_PREFIX, source))
    rel = source[len(WEB_PREFIX):]
    path = os.path.realpath(os.path.join(out_root, rel))
    root = os.path.realpath(out_root)
    if not path.startswith(root + os.sep):
        raise ValueError("source escapes output root: %r" % source)
    if not os.path.exists(path):
        raise ValueError("source not found: %r" % source)
    return path


def make_crop_executor(out_root):
    def run(job):
        params = job_params(job)
        src = resolve_source(params.get("source"), out_root)
        box = params.get("box")
        if not box or len(box) != 4:
            raise ValueError("params.box must be [left, top, right, bottom]")
        img = Image.open(src)
        cropped = img.crop(tuple(int(v) for v in box))
        project = project_dir_name(job)
        dest_dir = os.path.join(out_root, project)
        os.makedirs(dest_dir, exist_ok=True)
        name = "job%d_0.png" % job["id"]
        cropped.save(os.path.join(dest_dir, name))
        return ["%s%s/%s" % (WEB_PREFIX, project, name)], None
    return run


def _parent(job, db_path):
    if not job.get("parent_job_id"):
        return None
    conn = db.connect(db_path)
    try:
        return db.get_job(conn, job["parent_job_id"])
    finally:
        conn.close()


def _source_path(job, parent, out_root):
    """params.source wins; otherwise the parent job's first output."""
    src = job_params(job).get("source")
    if not src and parent is not None:
        outs = json.loads(parent.get("outputs") or "[]")
        if not outs:
            raise ValueError("parent job %s has no outputs" % parent["id"])
        src = outs[0]
    return resolve_source(src, out_root)


def _post_daemon(daemon_url, endpoint, payload):
    r = requests.post(daemon_url.rstrip("/") + endpoint, json=payload,
                      timeout=TIMEOUT)
    r.raise_for_status()
    return [im["file"] for im in r.json()["images"]]


def make_variation_executor(daemon_url, out_root, db_path):
    def run(job):
        parent = _parent(job, db_path)
        params = job_params(job)
        prompt = job.get("prompt")
        if not prompt and parent is not None:
            prompt = parent.get("prompt")
        if not prompt:
            raise ValueError("variation needs a prompt (own or parent's)")
        payload = {
            "image_path": _source_path(job, parent, out_root),
            "prompt": prompt,
            "model": job.get("model") or "sd15",
            "strength": params.get("strength", 0.55),
            "steps": params.get("steps", 28),
        }
        if job.get("negative"):
            payload["negative"] = job["negative"]
        if job.get("seed") is not None:
            payload["seed"] = job["seed"]
        files = _post_daemon(daemon_url, "/img2img", payload)
        return copy_outputs(files, out_root, job["id"],
                            project_dir_name(job)), None
    return run


def make_inpaint_executor(daemon_url, out_root, db_path):
    def run(job):
        parent = _parent(job, db_path)
        params = job_params(job)
        mask = params.get("mask_path") or params.get("mask")
        if mask and mask.startswith(WEB_PREFIX):
            mask = resolve_source(mask, out_root)
        if not mask or not os.path.isfile(mask):
            raise ValueError("inpaint needs params.mask_path (white=repaint)")
        payload = {
            "image_path": _source_path(job, parent, out_root),
            "mask_path": mask,
            "prompt": job.get("prompt") or "",
            "steps": params.get("steps", 28),
        }
        if job.get("negative"):
            payload["negative"] = job["negative"]
        if job.get("seed") is not None:
            payload["seed"] = job["seed"]
        files = _post_daemon(daemon_url, "/inpaint", payload)
        return copy_outputs(files, out_root, job["id"],
                            project_dir_name(job)), None
    return run


def make_upscale_executor(daemon_url, out_root, db_path):
    def run(job):
        parent = _parent(job, db_path)
        payload = {
            "image_path": _source_path(job, parent, out_root),
            "scale": job_params(job).get("scale", 4),
        }
        files = _post_daemon(daemon_url, "/upscale", payload)
        return copy_outputs(files, out_root, job["id"],
                            project_dir_name(job)), None
    return run
