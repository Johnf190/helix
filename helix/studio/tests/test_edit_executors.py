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

import db
from workers.edit_local import (make_variation_executor,
                                make_inpaint_executor,
                                make_upscale_executor)


@pytest.fixture
def stub_daemon(tmp_path):
    """Fake render_daemon with /img2img, /inpaint, /upscale (real shapes)."""
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    app = Flask(__name__)
    seen = {}

    def result(name, size):
        p = str(render_dir / (name + ".png"))
        Image.new("RGB", size, (9, 99, 199)).save(p)
        return jsonify(ok=True, images=[{"file": p,
                                         "url": "/static/render/%s.png" % name}],
                       model="stub", seconds=0.1)

    @app.post("/img2img")
    def i2i():
        seen["img2img"] = freq.get_json(force=True)
        return result("i2i", (64, 64))

    @app.post("/inpaint")
    def inp():
        seen["inpaint"] = freq.get_json(force=True)
        return result("inp", (64, 64))

    @app.post("/upscale")
    def up():
        seen["upscale"] = freq.get_json(force=True)
        return result("up", (256, 256))

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


@pytest.fixture
def env(tmp_path):
    out_root = str(tmp_path / "out")
    src_dir = os.path.join(out_root, "default")
    os.makedirs(src_dir)
    db_path = str(tmp_path / "s.db")
    conn = db.connect(db_path)
    db.init_db(conn)
    # a finished parent image job with a real output file
    a = db.create_job(conn, "user", "image", prompt="a red fox", model="sd15",
                      seed=5)
    db.claim_next(conn)
    Image.new("RGB", (64, 64), (200, 30, 30)).save(
        os.path.join(src_dir, "job%d_0.png" % a))
    db.finish_job(conn, a, ["/static/studio/default/job%d_0.png" % a])
    return {"out_root": out_root, "db_path": db_path, "conn": conn, "a": a}


def test_variation_inherits_parent_prompt_and_source(env, stub_daemon):
    url, seen = stub_daemon
    conn, a = env["conn"], env["a"]
    v = db.create_job(conn, "user", "variation", parent_job_id=a,
                      params={"strength": 0.6})
    fn = make_variation_executor(url, env["out_root"], env["db_path"])
    job = db.get_job(conn, v)
    outputs, cost = fn(job)
    assert cost is None
    assert outputs == ["/static/studio/default/job%d_0.png" % v]
    assert os.path.exists(os.path.join(env["out_root"], "default",
                                       "job%d_0.png" % v))
    # inherited the parent's prompt, resolved the parent's output as source
    assert seen["img2img"]["prompt"] == "a red fox"
    assert seen["img2img"]["strength"] == 0.6
    assert seen["img2img"]["image_path"].endswith("job%d_0.png" % a)
    assert os.path.isabs(seen["img2img"]["image_path"])


def test_variation_own_prompt_wins(env, stub_daemon):
    url, seen = stub_daemon
    conn, a = env["conn"], env["a"]
    v = db.create_job(conn, "user", "variation", prompt="a blue fox",
                      parent_job_id=a)
    fn = make_variation_executor(url, env["out_root"], env["db_path"])
    fn(db.get_job(conn, v))
    assert seen["img2img"]["prompt"] == "a blue fox"


def test_inpaint_executor(env, stub_daemon):
    url, seen = stub_daemon
    conn, a = env["conn"], env["a"]
    mask = os.path.join(env["out_root"], "mask.png")
    Image.new("L", (64, 64), 255).save(mask)
    j = db.create_job(conn, "user", "inpaint", prompt="a red ball",
                      parent_job_id=a, params={"mask_path": mask})
    fn = make_inpaint_executor(url, env["out_root"], env["db_path"])
    outputs, _ = fn(db.get_job(conn, j))
    assert outputs == ["/static/studio/default/job%d_0.png" % j]
    assert seen["inpaint"]["mask_path"] == mask
    assert seen["inpaint"]["prompt"] == "a red ball"
    assert seen["inpaint"]["image_path"].endswith("job%d_0.png" % a)


def test_upscale_executor_and_lineage_via_api(env, stub_daemon, tmp_path):
    url, seen = stub_daemon
    conn, a = env["conn"], env["a"]
    v = db.create_job(conn, "user", "variation", parent_job_id=a)
    fn_v = make_variation_executor(url, env["out_root"], env["db_path"])
    out_v, _ = fn_v(db.get_job(conn, v))
    db.finish_job(conn, v, out_v)

    u = db.create_job(conn, "user", "upscale", parent_job_id=v,
                      params={"scale": 4})
    fn_u = make_upscale_executor(url, env["out_root"], env["db_path"])
    out_u, _ = fn_u(db.get_job(conn, u))
    db.finish_job(conn, u, out_u)
    assert out_u == ["/static/studio/default/job%d_0.png" % u]
    assert seen["upscale"]["scale"] == 4
    assert seen["upscale"]["image_path"].endswith("job%d_0.png" % v)

    # lineage image -> variation -> upscale reads back 3 nodes via the API
    from studio import create_app
    c = create_app(testing=True, db_path=env["db_path"]).test_client()
    data = c.get("/api/job/%d" % u).get_json()
    assert [n["id"] for n in data["lineage"]] == [u, v, a]
    assert data["lineage"][-1]["prompt"] == "a red fox"


def test_studio_executor_registry():
    from studio import build_executors
    ex = build_executors()
    for kind in ("image", "crop", "variation", "inpaint", "upscale"):
        assert kind in ex, "missing executor for %s" % kind
