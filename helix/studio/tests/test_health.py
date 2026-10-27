import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from studio import create_app

def test_health():
    c = create_app(testing=True).test_client()
    r = c.get("/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True
