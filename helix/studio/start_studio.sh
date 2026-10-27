#!/usr/bin/env bash
# Helix Studio launcher. Unlike start_helix.sh this does NOT restart a running
# instance: the studio owns a job queue, and a restart mid-job requeues local
# work / fails video work (see RUNBOOK.md restart semantics) — so we only
# start if it's not already up, then open the UI.
STUDIO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$STUDIO" || exit 1

# NOTE the [.] trick: 'pgrep -f studio.py' would match THIS script's own
# command line (it contains the string) — the classic pkill/pgrep self-match
# trap. Never 'pkill -f studio.py' from a shell whose cmdline mentions it.
if ! pgrep -f 'studio[.]py' >/dev/null 2>&1; then
  nohup "$HOME/helix/.venv/bin/python" studio.py >> "$STUDIO/studio.log" 2>&1 &
  disown
fi
for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ':7910' && break; sleep 0.3; done

URL="http://127.0.0.1:7910/"
if command -v brave >/dev/null 2>&1; then           exec brave --app="$URL"
elif command -v brave-browser >/dev/null 2>&1; then exec brave-browser --app="$URL"
else                                                 exec xdg-open "$URL"; fi
