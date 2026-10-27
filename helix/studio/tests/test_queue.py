import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db
from queue_worker import Worker


def setup_db(tmp_path):
    path = str(tmp_path / "q.db")
    c = db.connect(path)
    db.init_db(c)
    return path, c


def test_run_once_executes_and_finishes(tmp_path):
    path, c = setup_db(tmp_path)
    j = db.create_job(c, "user", "image", prompt="a cat")
    calls = []

    def fake_exec(job):
        calls.append(job["id"])
        return ["/static/studio/default/out.png"], None

    w = Worker(path, {"image": fake_exec})
    assert w.run_once(c) is True
    row = db.get_job(c, j)
    assert row["status"] == "done"
    assert json.loads(row["outputs"]) == ["/static/studio/default/out.png"]
    assert calls == [j]


def test_executor_exception_marks_failed(tmp_path):
    path, c = setup_db(tmp_path)
    j = db.create_job(c, "user", "image", prompt="a cat")

    def boom(job):
        raise RuntimeError("daemon unreachable")

    w = Worker(path, {"image": boom})
    assert w.run_once(c) is True
    row = db.get_job(c, j)
    assert row["status"] == "failed"
    assert "daemon unreachable" in row["error"]


def test_run_once_empty_queue_returns_false(tmp_path):
    path, c = setup_db(tmp_path)
    w = Worker(path, {"image": lambda j: ([], None)})
    assert w.run_once(c) is False


def test_missing_executor_fails_job(tmp_path):
    path, c = setup_db(tmp_path)
    j = db.create_job(c, "user", "crop", params={"box": [0, 0, 1, 1]})
    w = Worker(path, {"image": lambda job: ([], None)})
    assert w.run_once(c) is True
    row = db.get_job(c, j)
    assert row["status"] == "failed"
    assert "no executor" in row["error"]


def test_serial_fifo_order(tmp_path):
    path, c = setup_db(tmp_path)
    ids = [db.create_job(c, "user", "image", prompt=str(i)) for i in range(3)]
    finished = []

    def fake_exec(job):
        finished.append(job["id"])
        return [], None

    w = Worker(path, {"image": fake_exec})
    for _ in range(3):
        assert w.run_once(c) is True
    assert w.run_once(c) is False
    assert finished == ids


def test_executor_records_cost(tmp_path):
    path, c = setup_db(tmp_path)
    j = db.create_job(c, "user", "video", prompt="x", price_quote_usd=0.4)
    db.confirm_video(c, j)
    w = Worker(path, {"video": lambda job: (["/x.mp4"], 0.42)})
    assert w.run_once(c) is True
    row = db.get_job(c, j)
    assert row["status"] == "done"
    assert row["cost_usd"] == 0.42


def test_start_stop_thread(tmp_path):
    path, c = setup_db(tmp_path)
    w = Worker(path, {}, poll=0.05)
    w.start()
    assert w._thread.daemon is True
    w.stop()
    w._thread.join(timeout=2)
    assert not w._thread.is_alive()
