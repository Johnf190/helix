"""Regression pin of render_daemon's EXISTING /gen contract.

Written BEFORE any daemon edit (plan Task 8). If these fail after a daemon
change, the change broke a consumer contract — fix the daemon, not the tests.
"""
import os

import pytest
import requests
from PIL import Image

from tests.daemon_live.conftest import DAEMON

pytestmark = pytest.mark.live

GEN = {"prompt": "a small wooden boat on a calm sea, oil painting",
       "model": "sd15", "width": 512, "height": 512, "steps": 8, "seed": 42,
       "count": 1}


def test_health():
    r = requests.get(DAEMON + "/health", timeout=5)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["loaded"], list)


def test_gen_sd15_contract():
    r = requests.post(DAEMON + "/gen", json=GEN, timeout=900)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["model"] == "sd15"
    assert isinstance(data["seconds"], (int, float))
    assert len(data["images"]) == 1
    entry = data["images"][0]
    assert entry["url"].startswith("/static/render/")
    assert os.path.exists(entry["file"])
    img = Image.open(entry["file"])
    img.verify()
    assert Image.open(entry["file"]).size == (512, 512)


def test_gen_deterministic_for_same_seed():
    a = requests.post(DAEMON + "/gen", json=GEN, timeout=900).json()
    b = requests.post(DAEMON + "/gen", json=GEN, timeout=900).json()
    fa, fb = a["images"][0]["file"], b["images"][0]["file"]
    with open(fa, "rb") as f1, open(fb, "rb") as f2:
        assert f1.read() == f2.read()


def test_model_field_validation():
    # unknown model -> 400 without generating anything
    r = requests.post(DAEMON + "/gen", json={"prompt": "x", "model": "nope"},
                      timeout=10)
    assert r.status_code == 400
    # sd15/sdxl/flux must stay registered model keys (static pin against the
    # daemon source -- generating with sdxl/flux is minutes of GPU, keep cheap)
    src = open(os.path.expanduser("~/helix/render_daemon.py")).read()
    for key in ("\"sd15\":", "\"sdxl\":", "\"flux\":"):
        assert key in src
