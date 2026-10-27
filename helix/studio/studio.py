import json, os
from flask import Flask, jsonify

import db
from api import api, close_conn
from gate import install_gate
from queue_worker import Worker

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))


def _with_project_name(fn, db_path):
    """Executors name output dirs after the job's project; enrich the raw db
    job dict with project_name before handing it over."""
    def run(job):
        if job.get("project_id") and not job.get("project_name"):
            conn = db.connect(db_path)
            try:
                row = conn.execute("SELECT name FROM projects WHERE id=?",
                                   (job["project_id"],)).fetchone()
            finally:
                conn.close()
            if row:
                job = dict(job, project_name=row["name"])
        return fn(job)
    return run


def build_executors(db_path=None):
    """Executor registry for the queue worker: kind -> fn(job)->(outputs, cost).
    video is wired in Task 13 (provider + quote/confirm)."""
    from workers.edit_local import (make_crop_executor,
                                    make_inpaint_executor,
                                    make_upscale_executor,
                                    make_variation_executor)
    from workers.images import make_image_executor

    from workers.director import wrap_image_executor

    db_path = db_path or os.path.join(HERE, CFG["db"])
    daemon, out_root = CFG["daemon"], CFG["out_root"]
    ex = {
        "image": wrap_image_executor(make_image_executor(daemon, out_root),
                                     db_path),
        "crop": make_crop_executor(out_root),
        "variation": make_variation_executor(daemon, out_root, db_path),
        "inpaint": make_inpaint_executor(daemon, out_root, db_path),
        "upscale": make_upscale_executor(daemon, out_root, db_path),
    }
    from workers.video import get_provider
    provider = get_provider(out_root)
    if provider is not None:
        ex["video"] = provider.run
    return {kind: _with_project_name(fn, db_path) for kind, fn in ex.items()}


def create_app(testing=False, db_path=None, executors=None, start_worker=None):
    app = Flask(__name__, static_folder=None)
    app.config["TESTING"] = testing
    app.config["DB_PATH"] = db_path or os.path.join(HERE, CFG["db"])
    app.config["OUT_ROOT"] = CFG["out_root"]
    install_gate(app, CFG["user_login"], CFG["token"])
    app.register_blueprint(api)
    app.teardown_appcontext(close_conn)

    conn = db.connect(app.config["DB_PATH"])
    db.init_db(conn)
    if start_worker is None:
        start_worker = not testing
    if start_worker:
        requeued, failed = db.recover_on_boot(conn)
        if requeued or failed:
            print("recover_on_boot: requeued=%d failed=%d" % (requeued, failed))
        worker = Worker(app.config["DB_PATH"],
                        executors or build_executors(app.config["DB_PATH"]))
        worker.start()
        app.extensions["studio_worker"] = worker
    conn.close()

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return open(os.path.join(HERE, "static_placeholder/index.html")).read()

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=CFG["port"])
