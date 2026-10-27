import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest

from studio import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(testing=True, db_path=str(tmp_path / "api.db"))
    return app.test_client()


def test_submit_image_job(client):
    r = client.post("/api/job", json={
        "submitter": "user", "kind": "image", "prompt": "a lighthouse",
        "model": "sd15", "seed": 42, "params": {"width": 512}})
    assert r.status_code == 200
    data = r.get_json()
    assert data["id"] >= 1
    assert data["status"] == "queued"
    q = client.get("/api/queue").get_json()
    assert any(j["id"] == data["id"] for j in q["jobs"])


def test_bad_submitter_400(client):
    r = client.post("/api/job", json={"submitter": "mallory", "kind": "image"})
    assert r.status_code == 400


def test_bad_kind_400(client):
    r = client.post("/api/job", json={"submitter": "user", "kind": "hologram"})
    assert r.status_code == 400


def test_video_without_provider_503(client):
    # no FAL_KEY in the test environment -> get_provider() is None -> 503
    r = client.post("/api/job", json={
        "submitter": "user", "kind": "video", "prompt": "a boat"})
    assert r.status_code == 503
    assert "not configured" in r.get_json()["error"]


def test_confirm_on_image_409(client):
    jid = client.post("/api/job", json={
        "submitter": "user", "kind": "image", "prompt": "x"}).get_json()["id"]
    assert client.post("/api/job/%d/confirm" % jid).status_code == 409


def test_confirm_missing_job_404(client):
    assert client.post("/api/job/999/confirm").status_code == 404


def test_project_create_and_list(client):
    r = client.post("/api/project", json={"name": "posters"})
    assert r.status_code == 200
    pid = r.get_json()["id"]
    names = [p["name"] for p in client.get("/api/projects").get_json()["projects"]]
    assert "posters" in names
    # duplicate name -> 400
    assert client.post("/api/project", json={"name": "posters"}).status_code == 400
    # job can join the project
    r = client.post("/api/job", json={
        "submitter": "user", "kind": "image", "prompt": "x", "project_id": pid})
    assert r.get_json()["status"] == "queued"


def test_get_job_includes_lineage(client):
    a = client.post("/api/job", json={
        "submitter": "user", "kind": "image", "prompt": "root"}).get_json()["id"]
    b = client.post("/api/job", json={
        "submitter": "user", "kind": "variation", "prompt": "v",
        "parent_job_id": a}).get_json()["id"]
    data = client.get("/api/job/%d" % b).get_json()
    assert data["id"] == b
    assert [n["id"] for n in data["lineage"]] == [b, a]


def test_get_missing_job_404(client):
    assert client.get("/api/job/999").status_code == 404


def test_queue_filters(client):
    client.post("/api/job", json={"submitter": "user", "kind": "image", "prompt": "a"})
    client.post("/api/job", json={"submitter": "helix", "kind": "image", "prompt": "b"})
    jobs = client.get("/api/queue?submitter=helix").get_json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["submitter"] == "helix"
    jobs = client.get("/api/queue?status=queued").get_json()["jobs"]
    assert len(jobs) == 2
