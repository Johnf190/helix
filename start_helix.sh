#!/usr/bin/env bash
# HELIX launcher — always restarts, so the latest code + memory are loaded, then opens the UI.
# (Safe: conversation persists to memory/sessions/log.md and is restored on load.)
BRAIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$BRAIN" || exit 1

pkill -f "$BRAIN/.venv/bin/python app.py" 2>/dev/null
pkill -f 'python app.py' 2>/dev/null
sleep 1

# start ODIN's local "front desk" model server if installed (harmless if absent)
export PATH="$HOME/.local/bin:$PATH"

# The .desktop launcher runs a NON-interactive shell, so ~/.bashrc never runs. If your Claude
# session lives somewhere other than ~/.claude, export CLAUDE_CONFIG_DIR here — otherwise the
# CLI can fall back to a stale credential file and die with "OAuth session expired".
# [ -d "$HOME/claude/config" ] && export CLAUDE_CONFIG_DIR="$HOME/claude/config"
if command -v ollama >/dev/null 2>&1 && ! ss -ltn 2>/dev/null | grep -q ':11434'; then
  nohup ollama serve >> "$BRAIN/ollama.log" 2>&1 & disown
fi
nohup "$BRAIN/.venv/bin/python" app.py >> "$BRAIN/odin.log" 2>&1 &
for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ':7777' && break; sleep 0.3; done

# A static path rather than a route, so the launcher keeps working even against an older app.py.
URL="http://127.0.0.1:7777/static/index.html"
# Prefer Brave (clean app window); any browser works now that speech-to-text is server-side.
if command -v brave >/dev/null 2>&1; then       exec brave --app="$URL"
elif command -v brave-browser >/dev/null 2>&1; then exec brave-browser --app="$URL"
else                                             exec xdg-open "$URL"; fi
