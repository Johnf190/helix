#!/usr/bin/env bash
# ORDER 31 (item 20) — the nightly restart. Started ONLY by app.py (_nightly_restart_launch), in
# its own session, after the 04:30 nightly has finished and been pushed, inside 04:00–06:59, once
# a day, when Helix is idle. No route reaches it, and Odin's shell can neither run nor edit it
# (factgate_hook P6/P7, and the write wall ro-binds it).
#
#   1. `start` line in workspace/cycle/RESTARTS.jsonl: app.py sha256, old pid.
#   2. start_helix.sh with HELIX_NO_BROWSER=1 (it pkills the old app.py and starts the new one).
#   3. Health: within 90 s, /stat → 200 AND /model → JSON with `odin` AND exactly one app.py.
#   4. Healthy → `ok` line; app.py is copied to .lkg/app.py (last known good) + its sha256.
#   5. Not healthy → ONE rollback, never more: only if .lkg/app.py differs from app.py AND the
#      ledger's last `ok` line vouches for its sha. The bad app.py is kept as
#      app.py.failed.<UTC>, .lkg/app.py is restored, start_helix.sh runs once more, one health
#      check → `rolled_back` or `down`. Otherwise `down` straight away, no second start.
#   6. `down` → also ~/Desktop/HELIX_DID_NOT_COME_BACK.txt: a dead Helix cannot say so in chat.
# Rollback restores app.py ONLY. Nothing else that changed is rolled back.
#
# Exit: 0 ok · 3 rolled_back · 4 down · 2 refused.
#
# Tests only: HELIX_NR_TEST_ROOT=<temp dir> moves every path under that dir and needs
# HELIX_NR_TEST_LAUNCHER, _URL and _PATTERN too (optional _HEALTH_S). app.py strips these
# variables before it starts this script.
set -u
BRAIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

refuse() { echo "nightly_restart.sh: refused: $*" >&2; exit 2; }

if [ -n "${HELIX_NR_TEST_ROOT:-}" ]; then
  ROOT="$(readlink -f "$HELIX_NR_TEST_ROOT")"
  [ -d "$ROOT" ] || refuse "test root $ROOT is not a directory"
  [ "$ROOT" = "$BRAIN" ] && refuse "test root is the real helix dir"
  case "$ROOT/" in "$BRAIN/"*) refuse "test root is inside the real helix dir";; esac
  [ "$ROOT" = "$(readlink -f "$HOME")" ] && refuse "test root is \$HOME"
  [ -n "${HELIX_NR_TEST_LAUNCHER:-}" ] && [ -n "${HELIX_NR_TEST_URL:-}" ] \
    && [ -n "${HELIX_NR_TEST_PATTERN:-}" ] || refuse "test mode needs _LAUNCHER, _URL and _PATTERN"
  APP="$ROOT/app.py"
  LKG="$ROOT/.lkg"
  LEDGER="$ROOT/RESTARTS.jsonl"
  DESKTOP="$ROOT/Desktop"
  ODINLOG="$ROOT/odin.log"
  LAUNCHER="$HELIX_NR_TEST_LAUNCHER"
  URL="$HELIX_NR_TEST_URL"
  PATTERN="$HELIX_NR_TEST_PATTERN"
  HEALTH_S="${HELIX_NR_TEST_HEALTH_S:-90}"
else
  for v in HELIX_NR_TEST_LAUNCHER HELIX_NR_TEST_URL HELIX_NR_TEST_PATTERN HELIX_NR_TEST_HEALTH_S; do
    [ -n "${!v:-}" ] && refuse "$v is set without HELIX_NR_TEST_ROOT"
  done
  APP="$BRAIN/app.py"
  LKG="$BRAIN/.lkg"
  LEDGER="$BRAIN/workspace/cycle/RESTARTS.jsonl"
  DESKTOP="$HOME/Desktop"
  ODINLOG="$BRAIN/odin.log"
  LAUNCHER="$BRAIN/start_helix.sh"
  URL="http://127.0.0.1:7777"
  # the exact command line start_helix.sh starts: "<brain>/.venv/bin/python app.py"
  PATTERN="^$(printf '%s' "$BRAIN" | sed 's/[.[\*^$]/\\&/g')/\.venv/bin/python app\.py\$"
  HEALTH_S=90
fi

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
utc() { date -u +%Y%m%dT%H%M%SZ; }
now_s() { date +%s.%N; }
# one pid → a JSON number ("#1234"); none or several → a string ("", "12,34")
app_pids() { local p; p=$(pgrep -f -- "$PATTERN" | paste -sd, -); [[ "$p" =~ ^[0-9]+$ ]] && p="#$p"; echo "$p"; }
app_count() { pgrep -fc -- "$PATTERN"; }

# One chained line: the same format app.py's _nr_append writes (ts UTC Z, local, seq, prev =
# sha256 of the previous line's text, `genesis` first), under flock on the ledger itself.
ledger() {
  python3 - "$LEDGER" "$1" <<'PY'
import fcntl, hashlib, json, os, sys, time
path, rec = sys.argv[1], json.loads(sys.argv[2])
fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(path, "rb") as fh:
        old = fh.read().decode("utf-8", "replace").splitlines()
    rec.setdefault("local", time.strftime("%Y-%m-%dT%H:%M:%S"))
    rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), seq=len(old) + 1,
               prev=hashlib.sha256(old[-1].encode("utf-8")).hexdigest() if old else "genesis")
    os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
finally:
    os.close(fd)
PY
}

# json KEY VALUE ... → one JSON object; a value starting with '#' is a number.
json() {
  python3 - "$@" <<'PY'
import json, sys
a = sys.argv[1:]
out = {}
for k, v in zip(a[::2], a[1::2]):
    out[k] = float(v[1:]) if v.startswith("#") and "." in v else int(v[1:]) if v.startswith("#") else v
print(json.dumps(out, ensure_ascii=False))
PY
}

# The lkg sha the ledger vouches for: lkg_sha of the last `ok` line, or nothing.
vouched_lkg() {
  python3 - "$LEDGER" <<'PY'
import json, sys
last = ""
try:
    for l in open(sys.argv[1], encoding="utf-8"):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("event") == "ok" and r.get("lkg_sha"):
            last = r["lkg_sha"]
except OSError:
    pass
print(last)
PY
}

launch() { HELIX_NO_BROWSER=1 bash "$LAUNCHER" </dev/null >/dev/null 2>&1; }

# health T0 → 0 healthy (sets HEALTHY_S) / 1 not (sets WHY). Polls once a second up to HEALTH_S
# seconds after T0.
health() {
  local t0="$1" deadline code n
  deadline=$(python3 -c "print($t0 + $HEALTH_S)")
  WHY="never checked"
  while :; do
    code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$URL/stat" 2>/dev/null)
    if [ "$code" = "200" ]; then
      if curl -s -m 3 "$URL/model" 2>/dev/null \
          | python3 -c 'import json,sys; sys.exit(0 if "odin" in json.load(sys.stdin) else 1)' 2>/dev/null; then
        n=$(app_count)
        if [ "$n" = "1" ]; then
          HEALTHY_S=$(python3 -c "print(round($(now_s) - $t0))")
          return 0
        fi
        WHY="app.py count $n"
      else
        WHY="/model has no odin field"
      fi
    else
      WHY="/stat answered ${code:-nothing}"
    fi
    python3 -c "import sys; sys.exit(0 if $(now_s) < $deadline else 1)" || return 1
    sleep 1
  done
}

down() {
  local why="$1"
  ledger "$(json event down reason nightly why "$why" desktop "$DESKTOP/HELIX_DID_NOT_COME_BACK.txt")"
  mkdir -p "$DESKTOP"
  {
    echo "HELIX DID NOT COME BACK — nightly restart, $(date '+%F %T %Z')"
    echo
    echo "The nightly restart (order 31) restarted Helix and it did not come back healthy."
    echo "Why: $why"
    echo "Helix is probably not running. Start it by hand (the Helix desktop icon, or"
    echo "bash ~/helix/start_helix.sh) and read the lines below."
    echo
    echo "== the last RESTARTS.jsonl lines ($LEDGER)"
    tail -n 8 "$LEDGER" 2>/dev/null
    echo
    echo "== the last 60 lines of $ODINLOG"
    tail -n 60 "$ODINLOG" 2>/dev/null
  } > "$DESKTOP/HELIX_DID_NOT_COME_BACK.txt"
  echo "nightly_restart.sh: DOWN: $why" >&2
  exit 4
}

# ---- 1. start
START_SHA=$(sha "$APP")
ledger "$(json event start reason nightly app_sha "$START_SHA" old_pid "$(app_pids)")"

# ---- 2 + 3. restart, health
T0=$(now_s)
launch
if health "$T0"; then
  # ---- 4. healthy: keep what just proved itself as last known good
  mkdir -p "$LKG"
  tmp="$LKG/app.py.tmp.$$"
  cp -p "$APP" "$tmp"
  if [ "$(sha "$tmp")" = "$START_SHA" ]; then
    mv -f "$tmp" "$LKG/app.py"
    echo "$START_SHA  app.py" > "$LKG/app.py.sha256"
    ledger "$(json event ok reason nightly seconds "#$HEALTHY_S" pid "$(app_pids)" lkg_sha "$START_SHA")"
  else
    rm -f "$tmp"
    ledger "$(json event ok reason nightly seconds "#$HEALTHY_S" pid "$(app_pids)" \
              lkg "not updated: app.py changed during the restart")"
  fi
  exit 0
fi
FIRST_WHY="$WHY"

# ---- 5. one rollback, never more
CUR_SHA=$(sha "$APP")
LKG_SHA=$(sha "$LKG/app.py")
VOUCHED=$(vouched_lkg)
if [ -z "$LKG_SHA" ]; then
  down "unhealthy after $HEALTH_S s ($FIRST_WHY), and no last known good to roll back to"
fi
if [ "$LKG_SHA" = "$CUR_SHA" ]; then
  down "unhealthy after $HEALTH_S s ($FIRST_WHY); app.py is the same as last known good, so no second restart"
fi
if [ "$LKG_SHA" != "$VOUCHED" ] || [ "$(cut -d' ' -f1 "$LKG/app.py.sha256" 2>/dev/null)" != "$LKG_SHA" ]; then
  down "unhealthy after $HEALTH_S s ($FIRST_WHY); .lkg/app.py is not vouched for by the ledger, so no rollback"
fi
FAILED="app.py.failed.$(utc)"
cp -p "$APP" "$(dirname "$APP")/$FAILED"
tmp="$APP.tmp.$$"
cp "$LKG/app.py" "$tmp" && chmod --reference="$(dirname "$APP")/$FAILED" "$tmp" && mv -f "$tmp" "$APP"
T1=$(now_s)
launch
if health "$T1"; then
  ledger "$(json event rolled_back reason nightly failed_copy "$FAILED" restored_sha "$LKG_SHA" \
            first_failure "$FIRST_WHY" seconds "#$HEALTHY_S" pid "$(app_pids)")"
  echo "nightly_restart.sh: ROLLED BACK to last known good; bad copy kept as $FAILED" >&2
  exit 3
fi
down "rolled back to last known good, still unhealthy ($WHY); first failure: $FIRST_WHY; bad copy kept as $FAILED"
