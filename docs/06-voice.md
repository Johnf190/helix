# 6 · Voice

Both directions run **locally on CPU**. No audio leaves the machine, no speech API, no key.

Voice is **optional**. If the models aren't there, Helix runs fine — the TTS and STT loaders fail
quietly and you get a text-only assistant.

---

## Why the models aren't in the repo

Together they're about **495 MB**, which is far past what belongs in a git repo. Two commands and
they're yours.

## Text → speech: Kokoro

```bash
mkdir -p models/kokoro
curl -L -o models/kokoro/kokoro-v1.0.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/kokoro/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

**≈ 354 MB.** Loaded once on first use (~1 s), then cached.

### The two voices

`models/kokoro/odin_voice.npy` and `alice_voice.npy` **ship in this repo** (512 KB each). They're
blended voice embeddings, not code, and they're what make ODIN and ALICE sound like themselves.
Delete them and Helix falls back to the named voice in `KOKORO_VOICE`.

Tuning knobs at the top of `app.py`:

| constant | default | |
|---|---|---|
| `KOKORO_SPEED` | `1.05` | deliberate pace |
| `KOKORO_PITCH` | `0.94` | `<1` lowers pitch (0.94 ≈ one semitone down) |
| `KOKORO_ROBO` | `0.26` | robotic sheen — light ring-modulation plus a 6 ms metallic doubling. `0` off, `0.12` subtle, `0.32` strong |
| `KOKORO_VOICE` | `bm_fable` | fallback voice name if the blend file is missing |

### Speech starts before the answer finishes

`_TTSPipeline` is fed text **as it streams**, cuts complete sentences off the front, and synthesises
them on a single background worker. One worker on purpose: Kokoro is CPU-bound, and serial
execution keeps the clips in playback order for free. On a measured turn, audio began about **5 s
before the turn ended**.

Two traps in there, both already handled, both worth knowing if you touch it:

- **Code fences.** Markdown is stripped before synthesis, but mid-stream the *closing* fence hasn't
  arrived yet — so the pipeline holds output while the fence count is odd, rather than reading your
  code aloud.
- **Tables.** A markdown table becomes the cue *"table shown in the chat"*. Measured: one six-row
  table once became a single 33-second clip of unlistenable number soup. A table is for the eye.

Same for code blocks (*"code shown in the chat"*), links, and inline formatting — TTS should never
pronounce `*`, `#`, `|` or backticks.

## Speech → text: faster-whisper

**No download step.** `faster-whisper` pulls `base.en` (int8, CPU) from Hugging Face on first use,
about **141 MB** into `~/.cache/huggingface`. Just have network on your first **TALK**.

To use a bigger model, change `"base.en"` in `_warmup()` and in the two `WhisperModel(...)` call
sites in `app.py`.

### Two ways to talk

- **TALK** — press to start recording, press again to send. Deliberate, one turn at a time.
- **AUTO** — always listening. It only sends a sentence that starts with the wake word **"ODIN"**;
  the rest of that sentence is the message. This is what stops a room conversation becoming input.

Recording happens in the browser and is posted to the server, so any browser works and there is no
Web Speech API dependency.

## If you want no voice at all

Skip the download and don't install the optional block in `requirements.txt`:

```bash
.venv/bin/pip install Flask requests claude-agent-sdk
```

Everything else works. The TTS queue meter sits at zero and the audio events never fire.

---

← Back to the [README](../README.md)
