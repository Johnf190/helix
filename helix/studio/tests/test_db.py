import json
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db

import pytest


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def test_schema_creates_tables():
    c = fresh()
    names = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "jobs"} <= names


def test_connect_returns_row_factory_conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_db(c)
    j = db.create_job(c, "user", "image", prompt="x")
    row = db.get_job(c, j)
    assert row["prompt"] == "x"
    c.close()


def test_projects_create_and_list():
    c = fresh()
    pid = db.create_project(c, "posters")
    projects = db.list_projects(c)
    assert len(projects) == 1
    assert projects[0]["id"] == pid
    assert projects[0]["name"] == "posters"


def test_image_job_starts_queued():
    c = fresh()
    j = db.create_job(c, "helix", "image", prompt="a cat", model="sd15",
                      seed=7, params={"width": 512})
    row = db.get_job(c, j)
    assert row["status"] == "queued"
    assert row["submitter"] == "helix"
    assert row["seed"] == 7
    assert json.loads(row["params"])["width"] == 512
    assert json.loads(row["outputs"]) == []


def test_video_needs_confirm():
    c = fresh()
    j = db.create_job(c, "user", "video", prompt="boat", price_quote_usd=0.5)
    assert db.get_job(c, j)["status"] == "awaiting_confirm"
    assert db.claim_next(c) is None
    db.confirm_video(c, j)
    assert db.claim_next(c)["id"] == j


def test_confirm_on_non_video_raises():
    c = fresh()
    j = db.create_job(c, "user", "image", prompt="x")
    with pytest.raises(ValueError):
        db.confirm_video(c, j)


def test_confirm_on_already_queued_video_raises():
    c = fresh()
    j = db.create_job(c, "user", "video", prompt="x", price_quote_usd=0.1)
    db.confirm_video(c, j)
    with pytest.raises(ValueError):
        db.confirm_video(c, j)


def test_video_cannot_be_forced_queued_without_confirm():
    # invariant enforced at db layer: create_job never lets a video in as queued
    c = fresh()
    j = db.create_job(c, "user", "video", prompt="x", price_quote_usd=0.1)
    assert db.get_job(c, j)["status"] == "awaiting_confirm"
    # claim_next must never pick up an unconfirmed video, ever
    for _ in range(3):
        assert db.claim_next(c) is None


def test_claim_order_fifo():
    c = fresh()
    a = db.create_job(c, "user", "image", prompt="a")
    b = db.create_job(c, "user", "image", prompt="b")
    d = db.create_job(c, "user", "image", prompt="c")
    assert db.claim_next(c)["id"] == a
    assert db.claim_next(c)["id"] == b
    assert db.claim_next(c)["id"] == d
    assert db.claim_next(c) is None


def test_claim_sets_running_and_started():
    c = fresh()
    j = db.create_job(c, "user", "image", prompt="a")
    claimed = db.claim_next(c)
    assert claimed["id"] == j
    row = db.get_job(c, j)
    assert row["status"] == "running"
    assert row["started"]


def test_finish_job():
    c = fresh()
    j = db.create_job(c, "user", "image", prompt="a")
    db.claim_next(c)
    db.finish_job(c, j, ["/static/studio/default/job1_0.png"], cost_usd=None)
    row = db.get_job(c, j)
    assert row["status"] == "done"
    assert row["finished"]
    assert json.loads(row["outputs"]) == ["/static/studio/default/job1_0.png"]


def test_fail_job():
    c = fresh()
    j = db.create_job(c, "user", "image", prompt="a")
    db.claim_next(c)
    db.fail_job(c, j, "boom")
    row = db.get_job(c, j)
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    assert row["finished"]


def test_list_jobs_filters():
    c = fresh()
    pid = db.create_project(c, "p1")
    db.create_job(c, "user", "image", prompt="a", project_id=pid)
    db.create_job(c, "helix", "image", prompt="b")
    v = db.create_job(c, "user", "video", prompt="v", price_quote_usd=0.2)
    assert len(db.list_jobs(c)) == 3
    assert len(db.list_jobs(c, submitter="helix")) == 1
    assert len(db.list_jobs(c, status="awaiting_confirm")) == 1
    assert db.list_jobs(c, status="awaiting_confirm")[0]["id"] == v
    assert len(db.list_jobs(c, project_id=pid)) == 1


def test_recover_on_boot():
    c = fresh()
    a = db.create_job(c, "helix", "image", prompt="x", model="sd15", seed=1)
    db.claim_next(c)
    v = db.create_job(c, "user", "video", prompt="y", price_quote_usd=0.4)
    db.confirm_video(c, v)
    db.claim_next(c)
    rq, fl = db.recover_on_boot(c)
    assert (rq, fl) == (1, 1)
    assert db.get_job(c, a)["status"] == "queued"
    row = db.get_job(c, v)
    assert row["status"] == "failed"
    assert "interrupted by restart" in row["error"]


def test_lineage_walks_to_root():
    c = fresh()
    a = db.create_job(c, "user", "image", prompt="root")
    b = db.create_job(c, "user", "variation", prompt="v", parent_job_id=a)
    d = db.create_job(c, "user", "upscale", parent_job_id=b)
    chain = db.lineage(c, d)
    assert [x["id"] for x in chain] == [d, b, a]


def test_bad_submitter_or_kind_rejected():
    c = fresh()
    with pytest.raises(Exception):
        db.create_job(c, "mallory", "image", prompt="x")
    with pytest.raises(Exception):
        db.create_job(c, "user", "hologram", prompt="x")
