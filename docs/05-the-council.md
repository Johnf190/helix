# 5 · The council

Helix's premise is that different models are good at different things, and that **agreement between
models is worth something only when they were asked independently.**

There are three tiers. You are talking to tier 1 by default; the others cost money or a keystroke.

---

## ODIN and ALICE — the two minds

They are the same system, opposite poles. Toggle with the **◉** chip in the transport bar.

| | **ODIN** (Claude) | **ALICE** (Gemini) |
|---|---|---|
| role | executes | audits and designs |
| does | writes and runs code, edits files, uses tools, drives the shell | deep verification, edge-case hunting, breaking what ODIN built; UI and architecture |
| bias | constructive | **de**constructive |
| writes to | `workspace/` | `memory/alice/` |
| voice | male, British | female, British |
| persona file | `memory/BRAIN.md` | `memory/ALICE.md` |

The adversarial split is the point. An assistant that agrees with its own last answer is worth
less than one that has a colleague whose explicit job is to falsify it. ALICE inherits the full
conversation and the same `memory/facts/`, so she is auditing the same shared truth — she just isn't
the one who wrote it.

Neither may edit `memory/facts/` casually. That's the shared record, changed deliberately.

## The bench — press **COUNCIL**

Seven seats answer the same prompt **in parallel**, each blind to the others. You get seven
independent opinions, and the disagreements are the information.

The default bench in `app.py`:

| seat | model | role |
|---|---|---|
| Sol | `openai/gpt-5.6-sol` | frontier reasoning |
| DeepSeek-V4 | `deepseek/deepseek-v4-pro` | deep logic |
| Qwen3.8-Max | `qwen/qwen3.8-max` | code |
| Kimi-K3 | `moonshotai/kimi-k3` | long context / agentic |
| MiniMax-M3 | `minimax/minimax-m3` | efficient agentic |
| GLM-5.3 | `z-ai/glm-5.3` | general / agentic |
| Gemini | via `agy` | breadth |

Edit `BENCH` freely — it's a list of dicts at the top of `app.py`. A failing seat returns its error
as its answer. **Failures are shown, never fatal.**

### What it costs

Six OpenRouter calls, `max_tokens=700` each. Per-million-token prices, pulled live from OpenRouter
on **2026-08-30**:

| model | input $/M | output $/M |
|---|---|---|
| `moonshotai/kimi-k3` | 3.00 | 15.00 |
| `openai/gpt-5.6-sol` | 2.00 | 10.00 |
| `qwen/qwen3.8-max` | 2.00 | 6.00 |
| `z-ai/glm-5.3` | 1.40 | 4.40 |
| `minimax/minimax-m3` | 0.30 | 1.20 |
| `deepseek/deepseek-v4-pro` | 0.51 | 1.02 |

⚠️ **These move.** Check <https://openrouter.ai/models> for current rates.

The prompt is your memory digest plus recent conversation, so the *input* side dominates and grows
with your corpus. A convene is cents, not dollars — but it is not free, and it's the only thing in
Helix that isn't.

Wall-clock is the slowest seat, not the sum, because the fan-out is parallel. Seats appear in the
rail as they land.

## Two rules, and they matter more than the model list

**1 · Council output is opinion, not evidence.**
Seven models agreeing is seven models sharing a prior. It is not a result. Nothing a seat says
becomes a fact until a real test validates it — and then what you save is the *test*, not the
opinion. This is why `/remember` stamps new files `status: unverified`.

**2 · Route by capability, then synthesise.**
Hard logic → the deep-logic seat. Code → the code seat. Huge context or long multi-step agentic
work → the long-context seat. Breadth → Gemini. Final judgment and synthesis → Claude. Fan out in
parallel, then have one mind reconcile the answers rather than averaging them.

## A model is an unreliable narrator of its own identity

Observed directly while building this: asked *"which model are you?"*, one seat named a different
version of itself and another named a competitor entirely. **The OpenRouter model ID is the ground
truth; the self-report is not.** If you want to know what answered, read the ID.

Next: [6 · Voice →](06-voice.md)
