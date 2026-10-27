import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest

import db
from queue_worker import Worker
from studio import create_app
from workers.video import FalProvider, MockProvider, get_provider


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    out_root = str(tmp_path / "out")
    db_path = str(tmp_path / "v.db")
    app = create_app(testing=True, db_path=db_path)
    mock = MockProvider(out_root)
    app.config["VIDEO_PROVIDER_FACTORY"] = lambda: mock
    return {"app": app, "client": app.test_client(), "db_path": db_path,
            "out_root": out_root, "mock": mock}


def submit_video(client):
    return client.post("/api/job", json={
        "submitter": "user", "kind": "video", "prompt": "a boat at dawn",
        "model": "mock", "params": {"seconds": 5}}).get_json()


def test_submit_video_quotes_and_awaits_confirm(env):
    data = submit_video(env["client"])
    assert data["price_quote_usd"] == 0.42
    assert data["status"] == "awaiting_confirm"
    job = env["client"].get("/api/job/%d" % data["id"]).get_json()
    assert job["price_quote_usd"] == 0.42


def test_worker_refuses_unconfirmed_video(env):
    data = submit_video(env["client"])
    conn = db.connect(env["db_path"])
    w = Worker(env["db_path"], {"video": env["mock"].run})
    assert w.run_once(conn) is False
    assert db.get_job(conn, data["id"])["status"] == "awaiting_confirm"


def test_confirmed_video_runs_and_records_cost(env):
    data = submit_video(env["client"])
    r = env["client"].post("/api/job/%d/confirm" % data["id"])
    assert r.status_code == 200 and r.get_json()["status"] == "queued"
    conn = db.connect(env["db_path"])
    w = Worker(env["db_path"], {"video": env["mock"].run})
    assert w.run_once(conn) is True
    row = db.get_job(conn, data["id"])
    assert row["status"] == "done"
    assert row["cost_usd"] == 0.42
    import json as _json
    outs = _json.loads(row["outputs"])
    assert len(outs) == 1 and outs[0].endswith(".mp4")
    disk = os.path.join(env["out_root"],
                        outs[0].replace("/static/studio/", "", 1))
    assert os.path.exists(disk)


def test_double_confirm_409(env):
    data = submit_video(env["client"])
    env["client"].post("/api/job/%d/confirm" % data["id"])
    assert env["client"].post(
        "/api/job/%d/confirm" % data["id"]).status_code == 409


def test_restart_recovery_fails_running_video_end_to_end(env):
    data = submit_video(env["client"])
    env["client"].post("/api/job/%d/confirm" % data["id"])
    conn = db.connect(env["db_path"])
    assert db.claim_next(conn)["id"] == data["id"]  # now "running"
    rq, fl = db.recover_on_boot(conn)               # simulated reboot
    assert (rq, fl) == (0, 1)
    job = env["client"].get("/api/job/%d" % data["id"]).get_json()
    assert job["status"] == "failed"
    assert "interrupted by restart" in job["error"]


def test_get_provider_none_without_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert get_provider("/tmp") is None


def test_get_provider_fal_with_key(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key-123")
    p = get_provider("/tmp")
    assert isinstance(p, FalProvider)


def test_fal_quote_from_price_table(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key-123")
    p = get_provider("/tmp")
    model = next(iter(p.PRICES))
    q = p.quote({"model": model, "params": {"seconds": 5}})
    assert q == pytest.approx(p.PRICES[model] * 5)
    with pytest.raises(ValueError):
        p.quote({"model": "not-a-model", "params": {}})


def test_fal_run_requires_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    p = FalProvider("/tmp")
    with pytest.raises(RuntimeError, match="FAL_KEY not set"):
        p.run({"id": 1, "prompt": "x", "model": "any", "params": "{}"})
