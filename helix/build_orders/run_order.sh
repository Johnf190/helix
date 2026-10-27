#!/usr/bin/env bash
# run_order.sh — start ONE builder session on ONE work order, outside Helix's process tree.
#
# the user types `/build NN` in Helix's chat; app.py calls this. It is deliberately dull: a pile of
# refusals, then one `systemd-run`. Everything it refuses, it refuses with a reason on one line.
#
# Why systemd-run: the builder must outlive `app.py`, so a `/restart` (or a crash) cannot kill a
# run in progress. The unit lands in its own cgroup under app.slice with a hard RuntimeMaxSec.
#
# 🔴 The builder never sees Odin's transcript. No conversation, no memory preamble, no session id —
# it reads the order and the raw files. That independence is the whole point of having a builder.
#
# Order 64: everything below runs inside main(), and the last line is `main "$@"; exit $?`, so bash
# parses the whole file before any of it runs: an order that rewrites this file mid-run cannot
# break the run. Change it only by writing a new file beside it and mv-ing it over this one.
main() {
set -uo pipefail

BRAIN="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ORDERS="$BRAIN/build_orders"
RUNS="$ORDERS/runs"
LOCK="$RUNS/.lock"
PROTOCOL="$ORDERS/BUILD_PROTOCOL.md"
PROTOCOL_MIN_BYTES=2000          # a builder with no protocol is worse than no builder
CLI="$HOME/.local/bin/claude"
BUDGET_USD=25
MAX_SECONDS=5400                 # 90 minutes, the protocol's cap

die() { echo "$1" >&2; exit "${2:-1}"; }

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
NN="${1:-}"
TRIGGER="${BUILD_TRIGGER:-cli}"  # app.py sets this to say where the command came from

[ -n "$NN" ] || die "usage: run_order.sh [--dry-run] <NN>"
[[ "$NN" =~ ^[0-9]+[a-z]?$ ]] || die "refused: order id must be digits (optionally one letter), got '$NN'"

# --- refusal 1: exactly one order file
shopt -s nullglob
MATCHES=("$ORDERS"/ORDER_"$NN"_*.md)
shopt -u nullglob
[ "${#MATCHES[@]}" -ne 0 ] || die "refused: no ORDER_${NN}_*.md in $ORDERS"
[ "${#MATCHES[@]}" -eq 1 ] || die "refused: ${#MATCHES[@]} files match ORDER_${NN}_*.md — $(printf '%s ' "${MATCHES[@]##*/}")"
ORDER_FILE="${MATCHES[0]}"

# --- refusal 6 (ORDER 54c): the order lint. The order is checked against the live disk before a
# builder starts: header, fences, paths, restart, last-line rule. One way through: a clean lint.
LINT="$ORDERS/tools/orderlint.py"
[ -f "$LINT" ] || die "refused: no orderlint.py"
LINT_OUT="$(python3 "$LINT" "$ORDER_FILE" 2>&1)"
LINT_RC=$?
if [ "$LINT_RC" -ne 0 ]; then
  FIRST="$(printf '%s\n' "$LINT_OUT" | grep -m1 '^HARD: ')"
  [ -n "$FIRST" ] || FIRST="exit $LINT_RC: $(printf '%s\n' "$LINT_OUT" | head -1)"
  die "refused: order lint — $FIRST"
fi

# --- refusal 2: the nightly's window, and the nightly itself
NOW="${BUILD_FAKE_HHMM:-$(date +%H%M)}"      # BUILD_FAKE_HHMM is for the tests only
if [ "$NOW" -ge 330 ] && [ "$NOW" -le 530 ]; then
  die "refused: $NOW is inside the nightly window (03:30-05:30)"
fi
if pgrep -af "[n]ightly[.]py|[r]etry_optc[.]py" >/dev/null 2>&1; then
  die "refused: a nightly is running"
fi

# --- refusal 3: one builder at a time
if [ -f "$LOCK" ]; then
  LOCKPID="$(cat "$LOCK" 2>/dev/null || echo)"
  if [ -n "$LOCKPID" ] && kill -0 "$LOCKPID" 2>/dev/null; then
    die "refused: a builder run is already active (pid $LOCKPID, $LOCK)"
  fi
fi
# ORDER 77: the lock only lives until systemd-run returns, and the builder runs on in its own unit
# (76c was started twice, 33s apart). So ask systemd too. BUILD_FAKE_SYSTEMCTL is for the tests only.
SYSTEMCTL="${BUILD_FAKE_SYSTEMCTL:-systemctl}"
LIVE_UNIT="$("$SYSTEMCTL" --user list-units 'helix-builder-*' --state=active,activating --no-legend --plain 2>/dev/null \
  | awk '$1 ~ /^helix-builder-/ { print $1; exit }')"
[ -z "$LIVE_UNIT" ] || die "refused: a builder run is already active (unit $LIVE_UNIT)"

# --- refusal 4: never overwrite a report
REPORT="$ORDERS/reports/ORDER_${NN}_report.md"
[ ! -e "$REPORT" ] || die "refused: $REPORT already exists — move it aside to re-run order $NN"

# --- refusal 5: the pre-flight. A builder launched without the protocol looks exactly like a
# builder launched with it, because the CLI ignores flags it does not know. So check the file
# itself, and record its hash, so a protocol-less run is visible afterwards.
[ -f "$PROTOCOL" ] || die "refused: no protocol at $PROTOCOL"
PBYTES="$(wc -c < "$PROTOCOL")"
[ "$PBYTES" -gt "$PROTOCOL_MIN_BYTES" ] || \
  die "refused: $PROTOCOL is only $PBYTES bytes (need > $PROTOCOL_MIN_BYTES) — refusing to launch a builder with no protocol"
PSHA="$(sha256sum "$PROTOCOL" | cut -d' ' -f1)"

[ -x "$CLI" ] || die "refused: no claude CLI at $CLI"

TS="$(date -u +%Y%m%d_%H%M%SZ)"   # UTC (order 27)
UNIT="helix-builder-${NN}-${TS}"
STREAM="$RUNS/ORDER_${NN}_${TS}.jsonl"
LAUNCH_LOG="$RUNS/ORDER_${NN}_${TS}.launch.log"   # systemd-run's own output: why a unit did not start
PROMPT="Execute work order ${ORDER_FILE} exactly per the protocol. One order, then stop and write the report."

# The unit runs the wrapper, not the CLI directly: the wrapper records the BUILDER's exit code
# and posts the completion note, and it still writes a RUNS line if systemd kills it on timeout.
WRAPPER="$ORDERS/builder_run.sh"
[ -x "$WRAPPER" ] || die "refused: no builder_run.sh at $WRAPPER"
UNIT_ARGS=("$WRAPPER" "$NN" "$ORDER_FILE" "$UNIT" "$STREAM" "$REPORT" "$PSHA" "$TRIGGER" "$BUDGET_USD")

if [ "$DRY" = 1 ]; then
  echo "DRY RUN — nothing launched"
  echo "order:    $ORDER_FILE"
  echo "unit:     $UNIT"
  echo "protocol: $PROTOCOL ($PBYTES bytes, sha256 $PSHA)"
  echo "stream:   $STREAM"
  echo "trigger:  $TRIGGER"
  echo "env:      CLAUDE_CONFIG_DIR=$HOME/claude/config  ANTHROPIC_API_KEY=$([ -n "${ANTHROPIC_API_KEY:-}" ] && echo PRESENT || echo absent)"
  echo "command:"
  printf '  systemd-run --user --unit=%s --property=RuntimeMaxSec=%s --collect \\\n' "$UNIT" "$MAX_SECONDS"
  printf '    --working-directory=%s \\\n' "$BRAIN"
  printf '    --setenv=CLAUDE_CONFIG_DIR=%s/claude/config \\\n' "$HOME"
  printf '    --property=UnsetEnvironment=ANTHROPIC_API_KEY \\\n'
  printf '    --property=StandardInput=null \\\n'
  printf '    --property=StandardOutput=append:%s \\\n' "$STREAM"
  printf '    --property=StandardError=append:%s \\\n' "$STREAM"
  printf ' %q' "${UNIT_ARGS[@]}"
  printf '\n'
  echo "which then runs, with stdin closed:"
  printf '  %s -p %q \\\n' "$CLI" "$PROMPT"
  printf '    --append-system-prompt-file %s \\\n' "$PROTOCOL"
  printf '    --model opus --output-format stream-json --verbose \\\n'
  printf '    --max-budget-usd %s --permission-mode bypassPermissions < /dev/null\n' "$BUDGET_USD"
  exit 0
fi

mkdir -p "$RUNS" "$ORDERS/reports"
echo $$ > "$LOCK"

# stdin must be closed: the CLI waits 3s for piped input otherwise (measured 2026-09-25).
systemd-run --user --unit="$UNIT" --property=RuntimeMaxSec="$MAX_SECONDS" --collect \
  --working-directory="$BRAIN" \
  --setenv=CLAUDE_CONFIG_DIR="$HOME/claude/config" \
  --property=UnsetEnvironment=ANTHROPIC_API_KEY \
  --property=StandardInput=null \
  --property=StandardOutput="append:$STREAM" \
  --property=StandardError="append:$STREAM" \
  "${UNIT_ARGS[@]}" >"$LAUNCH_LOG" 2>&1
RC=$?

rm -f "$LOCK"
[ "$RC" -eq 0 ] || die "refused: systemd-run failed (exit $RC) — see $LAUNCH_LOG" "$RC"
echo "started order $NN (unit $UNIT)"
}
main "$@"; exit $?
