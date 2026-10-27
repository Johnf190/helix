"""Image executor: POST the job to render_daemon /gen and collect outputs.

Daemon contract (existing, pinned by tests/daemon_live/test_gen_contract.py):
POST /gen {prompt, negative, model, width, height, steps, seed, count}
  -> {"ok": true, "images": [{"file": "/abs/path/...", "url": "/static/render/..."}],
      "model": ..., "seconds": ...}
"""
import json
import os
import shutil

import requests

TIMEOUT = 900  # flux 1024x1024 on the 1070 takes 4-5 min


def project_dir_name(job):
    return job.get("project_name") or "default"


def copy_outputs(files, out_root, job_id, project):
    """Copy daemon result files into out_root/<project>/job<id>_<n>.png and
    return the corresponding /static/studio/... web paths."""
    dest_dir = os.path.join(out_root, project)
    os.makedirs(dest_dir, exist_ok=True)
    outputs = []
    for n, src in enumerate(files):
        name = "job%d_%d.png" % (job_id, n)
        shutil.copyfile(src, os.path.join(dest_dir, name))
        outputs.append("/static/studio/%s/%s" % (project, name))
    return outputs


# Steps are per-model, not one number. flux-schnell is DISTILLED for 4 steps at
# guidance_scale=0 — running it at 20 costs ~10 minutes on the 1070 instead of ~2 and adds
# nothing, because the extra steps do almost nothing on a distilled model. A flat default of 20
# also silently overrode the daemon's own flux-aware default, so the daemon never got to apply
# it. (Caught on the user's job #7, 2026-09-07.)
DEFAULT_STEPS = {"flux": 4, "sdxl": 20, "sd15": 25}


def make_image_executor(daemon_url, out_root):
    def run(job):
        params = job.get("params") or "{}"
        if isinstance(params, str):
            params = json.loads(params)
        payload = {
            "prompt": job.get("prompt") or "",
            "model": job.get("model") or "sd15",
            "width": params.get("width", 512),
            "height": params.get("height", 512),
            "steps": params.get("steps",
                                DEFAULT_STEPS.get(job.get("model") or "sd15", 20)),
            "count": params.get("count", 1),
        }
        if job.get("negative"):
            payload["negative"] = job["negative"]
        if job.get("seed") is not None:
            payload["seed"] = job["seed"]
        r = requests.post(daemon_url.rstrip("/") + "/gen", json=payload,
                          timeout=TIMEOUT)
        r.raise_for_status()
        files = [im["file"] for im in r.json()["images"]]
        outputs = copy_outputs(files, out_root, job["id"],
                               project_dir_name(job))
        return outputs, None
    return run
