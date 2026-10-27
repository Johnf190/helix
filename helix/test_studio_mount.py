#!/usr/bin/env python3
"""Studio, served by Helix itself.

Studio worked but was a second program on 7910 with its own DB, its own auth gate and its own
UI, and `app.py` had zero references to it. The cost the user actually feels: `tailscale serve`
proxies only 127.0.0.1:7777, so his PHONE cannot reach Studio at all — the render tiers are
desktop-only. Plus two gates where `studio/gate.py` says in its own docstring that it mirrors
`_mobile_gate`.

Mounting means Studio's routes are Helix routes: one process, one port, one gate, and the
phone gets it over the tunnel that already exists.

render_daemon on 7900 deliberately stays separate — it holds GPU models resident and must be
able to restart without touching a live conversation.

Run:  .venv/bin/python test_studio_mount.py
"""
import os, sys

os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"\n          {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_studio_api_is_served_by_helix():
    """The queue must answer on 7777, not only on 7910."""
    with app.app.test_client() as tc:
        r = tc.get("/studio/api/queue")
        check("Helix serves /studio/api/queue", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            check("it returns the queue shape", isinstance(r.get_json(), (dict, list)),
                  f"body={r.get_data(as_text=True)[:120]}")


def test_the_studio_page_is_served_by_helix():
    with app.app.test_client() as tc:
        r = tc.get("/studio")
        check("Helix serves the /studio page", r.status_code == 200, f"status={r.status_code}")
        check("it is html", "text/html" in r.headers.get("Content-Type", ""),
              f"ctype={r.headers.get('Content-Type')}")


def test_helix_config_carries_studio_paths():
    check("DB_PATH is configured", bool(app.app.config.get("DB_PATH")))
    check("OUT_ROOT is configured", bool(app.app.config.get("OUT_ROOT")))
    check("OUT_ROOT is inside Helix static so images are servable",
          "static/studio" in (app.app.config.get("OUT_ROOT") or ""),
          f"OUT_ROOT={app.app.config.get('OUT_ROOT')}")


def test_studio_routes_are_behind_helix_own_gate():
    """A remote request without the token must be refused — by Helix's gate, not a second one.

    _mobile_gate treats any request carrying X-Forwarded-For as remote (that header is added
    by `tailscale serve`), so this is exactly what an un-tokened phone request looks like.
    """
    with app.app.test_client() as tc:
        r = tc.get("/studio/api/queue", headers={"X-Forwarded-For": "100.99.99.99"})
        check("a remote, un-tokened Studio request is refused", r.status_code == 403,
              f"status={r.status_code}")
        r2 = tc.get("/studio/api/queue",
                    headers={"X-Forwarded-For": "100.99.99.99"},
                    query_string={"k": app.mobile_token()})
        check("the same request WITH Helix's token is allowed", r2.status_code == 200,
              f"status={r2.status_code}")


def test_the_duplicate_studio_gate_is_not_installed():
    """Studio's own gate mirrored _mobile_gate. Mounting should delete the duplicate, not
    stack a second before_request that could disagree with the first."""
    funcs = [f.__qualname__ for f in app.app.before_request_funcs.get(None, [])]
    check("studio's gate is not stacked onto Helix", not any("install_gate" in f for f in funcs),
          f"before_request funcs: {funcs}")


def test_metrics_reports_the_studio_queue():
    with app.app.test_client() as tc:
        j = tc.get("/metrics").get_json()
    check("/metrics reports studio queue depth", "studio_queued" in j,
          f"keys={sorted(j.keys())}")


def test_importing_app_without_a_worker_does_not_touch_live_jobs():
    """Tests import app, which mounts Studio. That must not requeue REAL in-flight jobs.

    recover_on_boot exists so a worker can pick up jobs a crash orphaned. With no worker
    starting there is nothing to pick them up, and running it anyway rewrites the production
    database from every test process — which knocked the user's rendering job #7 back to 'queued'
    mid-flight on 2026-09-07.
    """
    import tempfile, sqlite3
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio"))
    import db as sdb
    path = os.path.join(tempfile.mkdtemp(), "s.db")
    conn = sdb.connect(path); sdb.init_db(conn)
    jid = sdb.create_job(conn, "user", "image", prompt="mid-flight")
    sdb.set_status(conn, jid, "running")
    conn.close()

    real, app.app.config["DB_PATH"] = app.app.config.get("DB_PATH"), path
    try:
        os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
        app._mount_studio()
        conn = sdb.connect(path)
        try:
            status = sdb.get_job(conn, jid)["status"]
        finally:
            conn.close()
        check("a running job is left alone when no worker starts", status == "running",
              f"status={status} — the mount requeued a live job")
    finally:
        app.app.config["DB_PATH"] = real


def test_studio_reports_whether_the_renderer_is_actually_up():
    """Generate is a promise. If the render daemon is down, Studio has to say so.

    When the daemon was killed (2026-09-07), Studio still showed a live Generate button and
    accepted jobs that could never render. Nothing on screen said why — it just looked broken.
    """
    with app.app.test_client() as tc:
        r = tc.get("/studio/api/daemon")
        check("there is a renderer health endpoint", r.status_code == 200, f"status={r.status_code}")
        if r.status_code != 200:
            return
        j = r.get_json()
        check("it says whether the renderer is up", isinstance(j.get("up"), bool), f"body={j}")
        check("it names where it looked", bool(j.get("url")), f"body={j}")


print("\nHELIX — Studio mounted into Helix\n" + "=" * 52)
test_studio_api_is_served_by_helix()
test_the_studio_page_is_served_by_helix()
test_helix_config_carries_studio_paths()
test_studio_routes_are_behind_helix_own_gate()
test_the_duplicate_studio_gate_is_not_installed()
test_metrics_reports_the_studio_queue()
test_importing_app_without_a_worker_does_not_touch_live_jobs()
test_studio_reports_whether_the_renderer_is_actually_up()
print("=" * 52)
print(f"{len(FAILURES)} failing" if FAILURES else "all green")
sys.exit(1 if FAILURES else 0)
