"""Identity gate — mirrors Helix app.py's _mobile_gate logic (app.py:1409-1420).

Allow if any of:
  - header Tailscale-User-Login == user_login (case-insensitive; injected by
    `tailscale serve` after cryptographic device auth, unforgeable from outside)
  - ?k=<token> query arg matches
  - request comes from localhost
Else 403. /health is always open so monitoring works.
"""
from flask import request, Response


def install_gate(app, user_login, token):
    user_login = (user_login or "").lower()

    @app.before_request
    def _gate():
        if request.path == "/health":
            return None
        if request.headers.get("Tailscale-User-Login", "").lower() == user_login:
            return None
        if token and request.args.get("k", "") == token:
            return None
        if request.remote_addr in ("127.0.0.1", "::1"):
            return None
        return Response("forbidden", status=403, mimetype="text/plain")
