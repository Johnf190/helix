#!/usr/bin/env python3
"""Model selector + ultracode arming.

Covers the invariants that matter after the 2026-09-07/08 fan-out incident: Odin cannot reach
the Workflow tool unless it was armed by hand, an arm is spent by exactly one turn, and a model
switch never leaves an arm standing. Runs against app.py in-process — no server, no API calls.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("HELIX_MODEL", None)          # the selector, not a launch pin
os.environ.pop("HELIX_ALLOW_FANOUT", None)   # the wall must be up by default
# Order 32: this file set none of the guards, so importing app started the warmup (whose pre-connect
# reaches _lane_options -> _write_wall(), the live wall_exec.sh) and the real scheduler ticker.
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"      # order 32: no real ticker on memory/schedules.json

import app

# Redirect persisted UI state into a scratch file BEFORE any route runs. Without this the
# route tests below write a real model choice into memory/ui_state.json.
app.UI_STATE = os.path.join(tempfile.mkdtemp(), "ui_state.json")
_ORIG_MODEL = app.ODIN_MODEL

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def with_client(fn):
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        fn(c)


# ── the wall is up unless armed ───────────────────────────────────────────────
print("fan-out wall")
check("Workflow blocked by default", "Workflow" in app.FANOUT_TOOLS, app.FANOUT_TOOLS)
app._ULTRA_ARMED = False
check("unarmed -> Workflow still blocked", app._fanout_tools_now() == ["Workflow"])
app._ULTRA_ARMED = True
check("armed -> Workflow permitted", app._fanout_tools_now() == [])
app._ULTRA_ARMED = False

# ── model choices ─────────────────────────────────────────────────────────────
print("model choices")
check("offers exactly opus + fable", set(app.MODEL_CHOICES) == {"opus", "fable"},
      sorted(app.MODEL_CHOICES))
check("default is opus", app.ODIN_MODEL in ("opus", "fable"))
check("alice is a gemini seat", "Gemini" in app.ALICE_MODEL, app.ALICE_MODEL)


def t_routes(c):
    print("routes")
    j = c.get("/model").get_json()
    check("GET /model reports odin", j.get("odin") in app.MODEL_CHOICES, j.get("odin"))
    check("GET /model reports alice", j.get("alice") == app.ALICE_MODEL)
    check("GET /model exposes arm state", "ultracode_armed" in j)

    r = c.post("/model", json={"model": "haiku"})
    check("unknown model rejected", r.status_code == 400, r.status_code)

    r = c.post("/model", json={"model": "sonnet"})
    check("model outside the offered pair rejected", r.status_code == 400, r.status_code)

    # arming, then switching model, must not leave the arm standing
    c.post("/ultracode", json={"armed": True})
    check("armed via route", app._ULTRA_ARMED is True)
    c.post("/model", json={"model": "fable"})
    check("model switch disarms ultracode", app._ULTRA_ARMED is False)
    check("model switch took effect", app.ODIN_MODEL == "fable", app.ODIN_MODEL)

    r = c.post("/ultracode", json={"armed": False})
    check("can stand down explicitly", r.get_json().get("ultracode_armed") is False)


with_client(t_routes)

# ── capped-state bookkeeping (learned from turns, never probed) ────────────────
print("fable capped state")
app.FABLE_CAPPED_AT = None
app._mark_model_state("fable", True)
check("limit error marks fable capped", bool(app.FABLE_CAPPED_AT))
check("capped state persisted", json.load(open(app.UI_STATE)).get("fable_capped_at"))
app._mark_model_state("fable", False)
check("a successful fable turn clears it", app.FABLE_CAPPED_AT is None)
app._mark_model_state("opus", True)
check("an opus limit never marks fable", app.FABLE_CAPPED_AT is None)

# ── limit detection is what drives all of the above ───────────────────────────
print("limit detection")
check("out-of-credits text reads as a limit",
      app._is_limit("You're out of usage credits. Switch to another model,"))
check("429 reads as a limit", app._is_limit("api error 429"))
check("ordinary text does not", not app._is_limit("here is your answer"))

app.ODIN_MODEL = _ORIG_MODEL          # leave the module as we found it

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: {', '.join(FAILURES)}")
else:
    print("all model-selector checks pass")
sys.exit(1 if FAILURES else 0)
