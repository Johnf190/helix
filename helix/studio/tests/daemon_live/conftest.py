import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pytest
import requests

DAEMON = "http://127.0.0.1:7900"


def daemon_up():
    try:
        return requests.get(DAEMON + "/health", timeout=3).status_code == 200
    except Exception:
        return False


_UP = None


def pytest_collection_modifyitems(config, items):
    global _UP
    for item in items:
        if "live" in item.keywords:
            if _UP is None:
                _UP = daemon_up()
            if not _UP:
                item.add_marker(pytest.mark.skip(
                    reason="render_daemon not responding on %s" % DAEMON))
