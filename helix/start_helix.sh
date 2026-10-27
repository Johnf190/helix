#!/usr/bin/env bash
# ODIN launcher — ALWAYS restart so the latest code + memory are loaded, then open in Chrome (Brave fallback).
# (Safe: conversation persists to memory/sessions/log.md and is restored on load.)
BRAIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$BRAIN" || exit 1

pkill -f "$BRAIN/.venv/bin/python app.py" 2>/dev/null
pkill -f 'python app.py' 2>/dev/null
sleep 1

# start ODIN's local "front desk" model server if installed (harmless if absent)
export PATH="$HOME/.local/bin:$PATH"

# Claude config lives on the HDD (~/claude -> /media/.../claude). The .desktop launcher runs a
# NON-interactive shell, so ~/.bashrc never runs and CLAUDE_CONFIG_DIR is unset — `claude -p` then
# falls back to the dead ~/.claude credentials and dies with "OAuth session expired". Set it here.
# The -d test dangles (false) if the HDD is unmounted, which is the correct fallback.
[ -d "$HOME/claude/config" ] && export CLAUDE_CONFIG_DIR="$HOME/claude/config"

if command -v ollama >/dev/null 2>&1 && ! ss -ltn 2>/dev/null | grep -q ':11434'; then
  nohup ollama serve >> "$BRAIN/ollama.log" 2>&1 & disown
fi
# the render daemon (image generator, 127.0.0.1:7900; order 40). Started only when nothing listens on
# :7900 and never killed: a running one may hold models. It loads them lazily, so starting holds no VRAM.
if [ -x "$HOME/render_env/bin/python" ] && ! ss -ltn 2>/dev/null | grep -qE ':7900[[:space:]]'; then
  setsid nohup "$HOME/render_env/bin/python" "$BRAIN/render_daemon.py" >> "$BRAIN/render_daemon.log" 2>&1 < /dev/null & disown
fi
nohup "$BRAIN/.venv/bin/python" app.py >> "$BRAIN/odin.log" 2>&1 &
for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ':7777' && break; sleep 0.3; done

# /restart from Helix's chat sets this: the window that sent it is still open and reconnects on
# its own, so opening a second one would leave two (order 15).
[ "${HELIX_NO_BROWSER:-}" = "1" ] && exit 0

# The desktop app now opens the v4 MTRX surface. index.html is untouched and still served at
# "/" — Helix-classic.desktop (or just visiting http://127.0.0.1:7777/) gets you back to it
# without a restart if v4 ever misbehaves.
# The desktop app opens v7 — the mind UI (order 58), the default. v6, the workbench, is the
# fallback: static/index_v6.html, or /v6, works without a restart. Everything before it too:
#   static/index_v5.html    -> the work lens v6 was built from
#   Helix (v4).desktop      -> the MTRX surface with the right-hand work stream + inspector
#   Helix (classic).desktop -> the original UI, untouched since 2026-08-21
# A static path rather than a route, so the launcher works even on an app.py that predates
# this pass.
URL="http://127.0.0.1:7777/static/index_v7.html"
# Prefer Google Chrome (order 69): the deb Chrome renders on the GPU; snap Brave renders in software
# (its gpu-process sat at ~99% of a core). Brave stays as the fallback; any browser works now that
# speech-to-text is server-side.
# Each browser gets its OWN Helix profile (order 16), so a launch can close the old Helix window
# without touching the user's normal Chrome or Brave (or sidekick's Chrome window): only processes started
# with one of these exact --user-data-dir paths are killed. Both are closed before either launches,
# so switching browsers never leaves a second Helix window behind.
# Brave is a snap, which can only write under ~/snap/brave/, hence its path.
HELIX_CHROME_PROFILE="$HOME/.config/helix-chrome-profile"
HELIX_PROFILE="$HOME/snap/brave/common/helix-profile"
close_old_helix_windows() {
  pkill -f -- "--user-data-dir=$HELIX_CHROME_PROFILE" 2>/dev/null
  pkill -f -- "--user-data-dir=$HELIX_PROFILE" 2>/dev/null
  sleep 1
}
if command -v google-chrome >/dev/null 2>&1; then
  close_old_helix_windows
  exec google-chrome --user-data-dir="$HELIX_CHROME_PROFILE" --app="$URL" --start-fullscreen
elif command -v brave >/dev/null 2>&1; then
  close_old_helix_windows
  exec brave --user-data-dir="$HELIX_PROFILE" --app="$URL" --start-fullscreen
elif command -v brave-browser >/dev/null 2>&1; then
  close_old_helix_windows
  exec brave-browser --user-data-dir="$HELIX_PROFILE" --app="$URL" --start-fullscreen
else                                             exec xdg-open "$URL"; fi
