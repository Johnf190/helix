#!/bin/bash
# Written by app.py (_write_wall) at every options build. Edits are overwritten.
[ $# -eq 1 ] || { echo "WALL: expected one command argument, got $#" >&2; exit 126; }
exec /usr/bin/bwrap \
  --bind / / \
  --dev-bind /dev /dev \
  --bind __HOME__/helix __HOME__/helix \
  --bind __HOME__/helix/memory __HOME__/helix/memory \
  --bind __HOME__/helix/workspace __HOME__/helix/workspace \
  --bind __HOME__/helix/workspace/seam __HOME__/helix/workspace/seam \
  --ro-bind-try __HOME__/helix/memory/facts __HOME__/helix/memory/facts \
  --ro-bind-try __HOME__/helix/workspace/factgate __HOME__/helix/workspace/factgate \
  --ro-bind-try __HOME__/helix/workspace/seam/registry __HOME__/helix/workspace/seam/registry \
  --ro-bind-try __HOME__/helix/workspace/catches __HOME__/helix/workspace/catches \
  --ro-bind-try __HOME__/helix/factgate_hook.py __HOME__/helix/factgate_hook.py \
  --ro-bind-try __HOME__/helix/test_factgate_hook.py __HOME__/helix/test_factgate_hook.py \
  --ro-bind-try __HOME__/helix/app.py __HOME__/helix/app.py \
  --ro-bind-try __HOME__/helix/start_helix.sh __HOME__/helix/start_helix.sh \
  --ro-bind-try __HOME__/helix/wall_exec.sh __HOME__/helix/wall_exec.sh \
  --ro-bind-try __HOME__/helix/build_orders __HOME__/helix/build_orders \
  --ro-bind-try __HOME__/helix/workspace/cycle __HOME__/helix/workspace/cycle \
  --ro-bind-try __HOME__/helix/nightly_restart.sh __HOME__/helix/nightly_restart.sh \
  --ro-bind-try __HOME__/helix/.lkg __HOME__/helix/.lkg \
  --ro-bind-try __HOME__/helix/memory/PINNED.txt __HOME__/helix/memory/PINNED.txt \
  --ro-bind-try __HOME__/helix/workspace/cycle/PATCHES.jsonl __HOME__/helix/workspace/cycle/PATCHES.jsonl \
  --ro-bind-try __HOME__/helix/memory/archive __HOME__/helix/memory/archive \
  -- /bin/bash -c "$1"
