import os

import pytest
import requests
from PIL import Image

from tests.daemon_live.conftest import DAEMON

pytestmark = pytest.mark.live


def test_img2img_sd15():
    src = requests.post(DAEMON + "/gen", json={
        "prompt": "a small wooden boat on a calm sea, oil painting",
        "model": "sd15", "width": 512, "height": 512, "steps": 8, "seed": 42,
    }, timeout=900).json()["images"][0]["file"]

    r = requests.post(DAEMON + "/img2img", json={
        "image_path": src,
        "prompt": "a small wooden boat on a stormy sea at night, oil painting",
        "model": "sd15", "strength": 0.6, "steps": 12, "seed": 7,
    }, timeout=900)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["seconds"], (int, float))
    out = data["images"][0]
    assert out["url"].startswith("/static/render/")
    assert os.path.exists(out["file"])
    Image.open(out["file"]).verify()
    assert Image.open(out["file"]).size == (512, 512)
    with open(src, "rb") as a, open(out["file"], "rb") as b:
        assert a.read() != b.read()


def test_img2img_missing_image_400():
    r = requests.post(DAEMON + "/img2img", json={
        "image_path": "/nonexistent/nope.png", "prompt": "x",
        "model": "sd15"}, timeout=10)
    assert r.status_code == 400


def test_img2img_flux_rejected():
    r = requests.post(DAEMON + "/img2img", json={
        "image_path": "/tmp/x.png", "prompt": "x", "model": "flux"},
        timeout=10)
    assert r.status_code == 400
