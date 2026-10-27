import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from flask import Flask, jsonify, request as freq
from PIL import Image

from workers.images import make_image_executor


@pytest.fixture
def stub_daemon(tmp_path):
    """Fake render_daemon: /gen writes a real PNG and returns its path."""
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    app = Flask(__name__)
    seen = {}

    @app.post("/gen")
    def gen():
        # Mirrors the REAL daemon response shape (pinned in
        # tests/daemon_live/test_gen_contract.py):
        # {"ok": true, "images": [{"file":..., "url":...}], "model":..., "seconds":...}
        seen["payload"] = freq.get_json(force=True)
        images = []
        for n in range(seen["payload"].get("count", 1)):
            p = str(render_dir / ("out_%d.png" % n))
            Image.new("RGB", (64, 64), (n * 40 % 255, 10, 200)).save(p)
            images.append({"file": p, "url": "/static/render/out_%d.png" % n})
        return jsonify(ok=True, images=images, model="sd15", seconds=0.1)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True),
        daemon=True)
    t.start()
    time.sleep(0.4)
    return "http://127.0.0.1:%d" % port, seen


def test_executor_copies_outputs(tmp_path, stub_daemon):
    url, seen = stub_daemon
    out_root = str(tmp_path / "studio_out")
    fn = make_image_executor(url, out_root)
    job = {"id": 7, "kind": "image", "prompt": "a fox", "negative": "blur",
           "model": "sd15", "seed": 42,
           "params": json.dumps({"width": 512, "height": 512, "steps": 8,
                                 "count": 2}),
           "project_id": None, "project_name": None}
    outputs, cost = fn(job)
    assert cost is None
    assert outputs == ["/static/studio/default/job7_0.png",
                       "/static/studio/default/job7_1.png"]
    for n in range(2):
        f = os.path.join(out_root, "default", "job7_%d.png" % n)
        assert os.path.exists(f)
        Image.open(f).verify()
    # daemon got the generation payload
    assert seen["payload"]["prompt"] == "a fox"
    assert seen["payload"]["negative"] == "blur"
    assert seen["payload"]["model"] == "sd15"
    assert seen["payload"]["seed"] == 42
    assert seen["payload"]["count"] == 2


def test_executor_uses_project_name(tmp_path, stub_daemon):
    url, _ = stub_daemon
    out_root = str(tmp_path / "studio_out")
    fn = make_image_executor(url, out_root)
    job = {"id": 3, "kind": "image", "prompt": "x", "negative": None,
           "model": "sd15", "seed": 1, "params": "{}",
           "project_name": "posters"}
    outputs, _ = fn(job)
    assert outputs == ["/static/studio/posters/job3_0.png"]
    assert os.path.exists(os.path.join(out_root, "posters", "job3_0.png"))


# ── per-model step defaults ───────────────────────────────────────────────────
# flux-schnell is DISTILLED for 4 steps at guidance_scale=0. The executor used to send a flat
# steps=20 for every model, which overrode the daemon's own flux-aware default of 4 and made a
# flux render take ~10 minutes on the 1070 instead of ~2 — for no quality gain, because extra
# steps do almost nothing on a distilled model. (Caught on the user's job #7, 2026-09-07.)

def test_flux_defaults_to_four_steps(tmp_path, stub_daemon):
    url, seen = stub_daemon
    ex = make_image_executor(url, str(tmp_path))
    ex({"id": 1, "prompt": "p", "model": "flux", "params": "{}"})
    assert seen["payload"]["steps"] == 4, seen["payload"]


def test_sdxl_and_sd15_keep_their_own_defaults(tmp_path, stub_daemon):
    url, seen = stub_daemon
    ex = make_image_executor(url, str(tmp_path))
    ex({"id": 1, "prompt": "p", "model": "sdxl", "params": "{}"})
    assert seen["payload"]["steps"] == 20, seen["payload"]
    ex({"id": 2, "prompt": "p", "model": "sd15", "params": "{}"})
    assert seen["payload"]["steps"] == 25, seen["payload"]


def test_an_explicit_step_count_still_wins(tmp_path, stub_daemon):
    """A deliberate override must survive — the default only fills the gap."""
    url, seen = stub_daemon
    ex = make_image_executor(url, str(tmp_path))
    ex({"id": 1, "prompt": "p", "model": "flux", "params": json.dumps({"steps": 12})})
    assert seen["payload"]["steps"] == 12, seen["payload"]
