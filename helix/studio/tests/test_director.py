import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest

import db
from queue_worker import Worker
from workers import director


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "d.db")
    conn = db.connect(db_path)
    db.init_db(conn)
    jobs_root = str(tmp_path / "ad_jobs")
    return {"db_path": db_path, "conn": conn, "jobs_root": jobs_root}


def test_art_director_image_job_parks_as_director_pending(env):
    conn = env["conn"]
    j = db.create_job(conn, "user", "image", prompt="a poster of a whale",
                      negative="text", model="sd15", seed=3,
                      params={"art_director": True, "width": 512,
                              "height": 512, "steps": 25})

    inner_calls = []
    ex = director.wrap_image_executor(
        lambda job: inner_calls.append(job) or (["/x.png"], None),
        env["db_path"], jobs_root=env["jobs_root"])
    w = Worker(env["db_path"], {"image": ex})
    assert w.run_once(conn) is True

    row = db.get_job(conn, j)
    assert row["status"] == "director_pending"
    assert inner_calls == []  # plain generation must NOT have run

    job_dir = os.path.join(env["jobs_root"], "studio_job%d" % j)
    state = json.load(open(os.path.join(job_dir, "job_state.json")))
    assert state["subjects"][0]["prompt"] == "a poster of a whale"
    assert state["subjects"][0]["model"] == "sd15"
    assert state["subjects"][0]["state"] == "pending"
    assert state["subjects"][0]["strikes"] == 0
    assert state["subjects"][0]["history"] == []


def test_plain_image_job_unaffected(env):
    conn = env["conn"]
    j = db.create_job(conn, "user", "image", prompt="a cat", params={})
    ex = director.wrap_image_executor(
        lambda job: (["/static/studio/default/cat.png"], None),
        env["db_path"], jobs_root=env["jobs_root"])
    w = Worker(env["db_path"], {"image": ex})
    w.run_once(conn)
    assert db.get_job(conn, j)["status"] == "done"


def test_attach_results_finishes_job(env):
    conn = env["conn"]
    j = db.create_job(conn, "user", "image", prompt="x",
                      params={"art_director": True})
    ex = director.wrap_image_executor(lambda job: ([], None),
                                      env["db_path"],
                                      jobs_root=env["jobs_root"])
    Worker(env["db_path"], {"image": ex}).run_once(conn)
    assert db.get_job(conn, j)["status"] == "director_pending"

    passed = ["/static/studio/default/job%d_0.png" % j]
    director.attach_results(conn, j, passed)
    row = db.get_job(conn, j)
    assert row["status"] == "done"
    assert json.loads(row["outputs"]) == passed


def test_attach_results_wrong_state_raises(env):
    conn = env["conn"]
    j = db.create_job(conn, "user", "image", prompt="x")
    with pytest.raises(ValueError):
        director.attach_results(conn, j, ["/a.png"])
