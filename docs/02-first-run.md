# 2 · First run

```bash
git clone https://github.com/Johnf190/helix.git
cd helix

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

./start_helix.sh
```

That's it. The launcher kills any previous instance, starts the server, waits for the port, and
opens the UI.

---

## What a healthy boot looks like

Helix binds **`127.0.0.1:7777`** — localhost only, never the network. Override with `HELIX_PORT`.

`start_helix.sh` backgrounds the server and appends to `odin.log`. Watch it:

```bash
tail -f odin.log
```

You want to see:

```
HELIX auth OK  · <how it found your Claude session>
HELIX online → http://127.0.0.1:7777
```

If the first line says **`HELIX AUTH PROBLEM`**, ODIN cannot reach Claude. Fix that before anything
else — see [01-prerequisites § ①](01-prerequisites.md). The check runs at boot on purpose, so a
dead sign-in surfaces immediately instead of halfway through your first answer.

The first turn is slower than the rest: a background warm-up thread is loading the TTS and STT
models and pre-connecting the agent session.

## Running it by hand

```bash
.venv/bin/python app.py
```

Then open <http://127.0.0.1:7777/>. Same thing, foreground, logs to your terminal.

## Verifying without launching the real thing

Two environment flags exist for exactly this:

```bash
HELIX_NO_WARMUP=1 HELIX_NO_PRECONNECT=1 HELIX_PORT=7999 .venv/bin/python app.py
```

`HELIX_NO_WARMUP=1` skips model loading; `HELIX_NO_PRECONNECT=1` skips opening a persistent agent
session. You get a bare Flask server in about a second, good for hitting `/stat`, `/blueprint`,
`/history` and the static files.

> ⚠️ **This matters more than it looks.** If you use Helix to edit Helix, you cannot verify your own
> fix by using yourself — Python does not hot-reload, and restarting the server kills the
> conversation you are having *inside* it. Verify headless with the flags above, then restart.
>
> Static files are the exception: `app.py` serves `static/` with `send_from_directory`, which reads
> from disk per request. **UI edits are live on a hard browser reload, no restart.**

## Common first-run problems

| Symptom | Cause |
|---|---|
| `HELIX AUTH PROBLEM` at boot | Claude CLI not signed in, or `CLAUDE_CONFIG_DIR` points at a stale config. |
| Server starts, browser shows nothing | Something else is on 7777. `HELIX_PORT=7778 ./start_helix.sh`. |
| Everything works, no audio | Voice models aren't downloaded. Expected — see [06-voice](06-voice.md). |
| `COUNCIL` returns six errors | `OPENROUTER_API_KEY` missing from `~/.bashrc`, or no credit. Failures are shown per seat, never fatal. |
| ALICE returns an error | `agy` not installed or not signed in. ODIN is unaffected. |
| `ensurepip is not available` | `sudo apt install python3.12-venv`. |

Next: [3 · Memory →](03-memory.md)
