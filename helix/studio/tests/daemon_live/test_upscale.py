import os

import pytest
import requests
from PIL import Image

from tests.daemon_live.conftest import DAEMON

pytestmark = pytest.mark.live


def test_upscale_4x():
    src = requests.post(DAEMON + "/gen", json={
        "prompt": "a small wooden boat on a calm sea, oil painting",
        "model": "sd15", "width": 512, "height": 512, "steps": 8, "seed": 42,
    }, timeout=900).json()["images"][0]["file"]

    r = requests.post(DAEMON + "/upscale", json={
        "image_path": src, "scale": 4}, timeout=900)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    out = data["images"][0]
    assert out["url"].startswith("/static/render/")
    assert os.path.exists(out["file"])
    img = Image.open(out["file"])
    assert img.format == "PNG"
    assert img.size == (2048, 2048)


def test_upscale_missing_image_400():
    r = requests.post(DAEMON + "/upscale", json={
        "image_path": "/nonexistent/a.png"}, timeout=10)
    assert r.status_code == 400


def test_upscale_bad_scale_400():
    r = requests.post(DAEMON + "/upscale", json={
        "image_path": "/tmp/x.png", "scale": 3}, timeout=10)
    assert r.status_code == 400
