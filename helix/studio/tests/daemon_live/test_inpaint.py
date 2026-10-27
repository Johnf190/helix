import os
import tempfile

import numpy as np
import pytest
import requests
from PIL import Image

from tests.daemon_live.conftest import DAEMON

pytestmark = pytest.mark.live


def test_inpaint_repaints_masked_half():
    src = requests.post(DAEMON + "/gen", json={
        "prompt": "a plain grey concrete wall, photo",
        "model": "sd15", "width": 512, "height": 512, "steps": 8, "seed": 42,
    }, timeout=900).json()["images"][0]["file"]

    # white = repaint; mask the LEFT half
    mask = Image.new("L", (512, 512), 0)
    mask.paste(255, (0, 0, 256, 512))
    mask_path = os.path.join(tempfile.gettempdir(), "studio_test_mask.png")
    mask.save(mask_path)

    r = requests.post(DAEMON + "/inpaint", json={
        "image_path": src, "mask_path": mask_path,
        "prompt": "a bright red ball", "steps": 12, "seed": 7,
    }, timeout=900)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    out = data["images"][0]
    assert os.path.exists(out["file"])
    result = Image.open(out["file"]).convert("RGB")
    assert result.size == (512, 512)

    a = np.asarray(Image.open(src).convert("RGB"), dtype=np.float32)
    b = np.asarray(result, dtype=np.float32)
    diff = np.abs(a - b).mean(axis=2)
    masked_diff = diff[:, :256].mean()
    unmasked_diff = diff[:, 256:].mean()
    # repainted half must change much more than the preserved half
    assert masked_diff > unmasked_diff * 2
    assert masked_diff > 5.0


def test_inpaint_missing_files_400():
    r = requests.post(DAEMON + "/inpaint", json={
        "image_path": "/nonexistent/a.png", "mask_path": "/nonexistent/m.png",
        "prompt": "x"}, timeout=10)
    assert r.status_code == 400
