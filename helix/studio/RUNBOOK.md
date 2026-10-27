# Helix Studio — RUNBOOK

## What this is
Separate Flask app that owns all generative media: local images/edits via the
render daemon, API video with price-confirm, persistent SQLite job queue,
projects, lineage, art-director handoff. No torch in this process — all GPU
work goes over HTTP to render_daemon.

## Ports
| service       | port | bind      | venv              |
|---------------|------|-----------|-------------------|
| Helix (app.py)| 7777 | 127.0.0.1 | ~/helix/.venv     |
| render_daemon | 7900 | 127.0.0.1 | ~/render_env      |
| Helix Studio  | 7910 | 127.0.0.1 | ~/helix/.venv     |

## Start / stop
- Desktop: double-click "Helix Studio" (~/Desktop/HelixStudio.desktop), or:
  `bash ~/helix/studio/start_studio.sh` (starts only if not already running,
  then opens the browser).
- Manual start:
  `cd ~/helix/studio && setsid nohup ~/helix/.venv/bin/python studio.py >> studio.log 2>&1 &`
- Stop: `kill $(pgrep -f 'studio[.]py')`
- Daemon restart (only when needed):
  `kill $(pgrep -f 'render_daemon[.]py')`, then from a FRESH command:
  `cd ~/helix && setsid nohup ~/render_env/bin/python ~/helix/render_daemon.py > ~/helix/render_daemon.log 2>&1 &`

### The pkill trap
`pkill -f studio.py` / `pkill -f render_daemon.py` will match the shell YOU
typed it in (its command line contains the string) and can kill your own
session. Always use the bracket idiom: `pgrep -f 'studio[.]py'`, then `kill`
the reported PID.

## Restart-safety semantics
On boot Studio runs `recover_on_boot`:
- local jobs (image/inpaint/variation/upscale/crop) found `running` are
  RE-QUEUED — they're reproducible (prompt/model/seed stored), safe to redo.
- video jobs found `running` are marked `failed` with error
  "interrupted by restart" — never risk paying twice. Resubmit + re-confirm.

## Video = money
A `video` job is created as `awaiting_confirm` with a `price_quote_usd` and
can NEVER run without `POST /api/job/<id>/confirm`. That invariant lives in
the db layer, not just the API. Live provider needs `FAL_KEY` in the
environment and a model pick — see the header of `workers/video.py` for
candidate models/prices (snapshot 2026-09-05; re-verify before enabling).

## Where things live
- Outputs: `~/helix/static/studio/<project or 'default'>/job<id>_<n>.png|.mp4`
- Queue db: `~/helix/studio/studio.db` (gitignored)
- Log: `~/helix/studio/studio.log`
- Daemon file versioning: edits to `~/helix/render_daemon.py` are copied to
  `daemon_patches/render_daemon.py` before each commit.
- Models: `~/render_models/` (sd-v1-5-inpainting.ckpt, RealESRGAN_x4plus.pth
  added by this build, both sha256-verified).

## Tailnet (phone access) — needs the user, one time
```
sudo tailscale serve --bg --https=8443 http://127.0.0.1:7910
```
Attempted 2026-09-06 without sudo: "Access denied" (expected). After the user
runs it, the URL will be
`https://your-machine.your-tailnet.ts.net:8443/` — record
it here. Remote requests are gated: Tailscale-User-Login header (injected by
tailscale serve) == you@example.com, or `?k=<token from
config.json>`; localhost is always allowed; `/health` is always open.

## Tests
```
cd ~/helix/studio
~/helix/.venv/bin/python -m pytest              # unit suite (live excluded)
~/helix/.venv/bin/python -m pytest -m live      # daemon contract + endpoints
```
Live tests self-skip when the daemon is down. The `test_gen_contract.py` pins
are the daemon's public contract — if a daemon edit breaks them, fix the
daemon.

## UI status
`/` serves a static placeholder ("API live, UI awaiting Alice's design").
The real UI (Alice's approved mockup) is a separate pass — Task 16 in the
plan.
