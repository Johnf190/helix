import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from PIL import Image

from workers.edit_local import make_crop_executor


def test_crop(tmp_path):
    out_root = str(tmp_path / "studio_out")
    src_dir = os.path.join(out_root, "default")
    os.makedirs(src_dir)
    Image.new("RGB", (100, 100), (200, 50, 50)).save(
        os.path.join(src_dir, "job1_0.png"))

    fn = make_crop_executor(out_root)
    job = {"id": 2, "kind": "crop", "parent_job_id": 1,
           "params": json.dumps({"source": "/static/studio/default/job1_0.png",
                                 "box": [10, 10, 60, 60]}),
           "project_name": None}
    outputs, cost = fn(job)
    assert cost is None
    assert outputs == ["/static/studio/default/job2_0.png"]
    out = os.path.join(out_root, "default", "job2_0.png")
    assert os.path.exists(out)
    assert Image.open(out).size == (50, 50)


def test_crop_rejects_source_outside_out_root(tmp_path):
    out_root = str(tmp_path / "studio_out")
    os.makedirs(out_root)
    fn = make_crop_executor(out_root)
    job = {"id": 3, "kind": "crop",
           "params": json.dumps({"source": "/static/studio/../../etc/passwd",
                                 "box": [0, 0, 1, 1]})}
    with pytest.raises(ValueError):
        fn(job)
