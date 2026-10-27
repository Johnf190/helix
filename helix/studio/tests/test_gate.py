import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from flask import Flask, jsonify
from gate import install_gate

USER = "you@example.com"
TOKEN = "deadbeef" * 4
REMOTE = {"REMOTE_ADDR": "100.0.0.9"}


def make_client():
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/private")
    def private():
        return jsonify(secret=True)

    install_gate(app, USER, TOKEN)
    return app.test_client()


def test_remote_without_identity_403():
    c = make_client()
    assert c.get("/private", environ_overrides=REMOTE).status_code == 403


def test_correct_header_allows():
    c = make_client()
    r = c.get("/private", environ_overrides=REMOTE,
              headers={"Tailscale-User-Login": USER})
    assert r.status_code == 200


def test_header_case_insensitive():
    c = make_client()
    r = c.get("/private", environ_overrides=REMOTE,
              headers={"Tailscale-User-Login": "you@example.com"})
    assert r.status_code == 200


def test_wrong_header_value_403():
    c = make_client()
    r = c.get("/private", environ_overrides=REMOTE,
              headers={"Tailscale-User-Login": "mallory@example.com"})
    assert r.status_code == 403


def test_token_allows():
    c = make_client()
    assert c.get("/private?k=" + TOKEN,
                 environ_overrides=REMOTE).status_code == 200


def test_wrong_token_403():
    c = make_client()
    assert c.get("/private?k=nope",
                 environ_overrides=REMOTE).status_code == 403


def test_localhost_allows():
    c = make_client()
    assert c.get("/private",
                 environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    assert c.get("/private",
                 environ_overrides={"REMOTE_ADDR": "::1"}).status_code == 200


def test_health_always_open():
    c = make_client()
    assert c.get("/health", environ_overrides=REMOTE).status_code == 200


def test_studio_app_has_gate():
    from studio import create_app
    c = create_app(testing=True).test_client()
    assert c.get("/", environ_overrides=REMOTE).status_code == 403
    assert c.get("/health", environ_overrides=REMOTE).status_code == 200
    assert c.get("/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
