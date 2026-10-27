"""Serial job queue worker.

One daemon thread, one job at a time -> free GPU serialization.
Executor signature: fn(job: dict) -> (outputs: list[str], cost_usd: float|None)
Exceptions from executors mark the job failed with str(e).
"""
import threading
import time

import db


class Worker:
    def __init__(self, db_path, executors, poll=1.0):
        self.db_path = db_path
        self.executors = executors
        self.poll = poll
        self._stop = threading.Event()
        self._thread = None

    def run_once(self, conn):
        """Claim and execute one job synchronously. Returns True if a job ran."""
        job = db.claim_next(conn)
        if job is None:
            return False
        fn = self.executors.get(job["kind"])
        if fn is None:
            db.fail_job(conn, job["id"], "no executor for kind %r" % job["kind"])
            return True
        try:
            outputs, cost_usd = fn(job)
            # Director handoff may park the job; don't override its status.
            if db.get_job(conn, job["id"])["status"] == "running":
                db.finish_job(conn, job["id"], outputs, cost_usd=cost_usd)
        except Exception as e:
            db.fail_job(conn, job["id"], str(e))
        return True

    def _loop(self):
        conn = db.connect(self.db_path)
        try:
            while not self._stop.is_set():
                if not self.run_once(conn):
                    self._stop.wait(self.poll)
        finally:
            conn.close()

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
