# Memory roadmap — consolidation (V1 → V2)

Context: Alistair's memory is an append-only SQLite event log on a Railway volume
(`docs/MEMORY_FORMULA.md`), shared across every connected client (claude.ai, voice,
Gemini, ChatGPT). `get_memory` loads a CONSOLIDATED block (core pinned + decayed
top-N, token-budgeted); `search_memory` recalls ANY entry on demand. Exponential
decay/importance handles **volume** (ranking + eviction) but NOT **coherence** —
it never merges near-duplicates or resolves superseded/conflicting facts. The store
grows with the number of distinct durable facts; the *loaded cost* stays bounded
(cap + budget), but near-dups, conflicts, and `relevance=5` core bloat accumulate.
Consolidation closes that gap. (A one-shot manual consolidation already took the
store 78 → 32 entries.)

## V1 — client-run consolidation, no server LLM (CURRENT PLAN)
Trigger: **at brief time.** When the operator asks for a brief, fold a light
`memory-maintenance` pass into it (daily = obvious-duplicate merges only + surface
anything ambiguous; weekly = a fuller sweep). On explicit request ("tidy your
memory") run the full procedure. Mechanism = the existing `memory-maintenance`
skill + `search_memory`/list + `save_memory` (assert/retract); the intelligence is
the connected client LLM, the server stays no-LLM. Append-only log stays ground
truth, so every pass is reversible. Goal of V1: keep the store coherent now AND
**observe the real growth/duplication rate** before committing to V2.

## V2 — native server-side LLM consolidation + summary-backed get_memory (FUTURE)
When V1 shows the store grows/duplicates faster than brief-time passes can keep up,
or a reliable headless trigger exists, add a background consolidation job:
- A cheap model (e.g. Haiku) clusters near-duplicates, merges them into canonical
  entries (proposing retracts + asserts on the log), and writes a derived
  **consolidated summary** (a coherent profile), like claude.ai's memory.
- **`get_memory` then serves the cached summary** instead of rendering raw entries;
  `search_memory` still hits the raw log.
Hard constraints:
1. The append-only log stays the single source of truth — consolidation only
   derives from it; a bad run must be fully recoverable from raw history.
2. The LLM is NEVER on `get_memory`'s hot path — consolidation is a background job
   (cron or threshold-triggered: entries grew by N, or dup-ratio crossed a line);
   `get_memory` serves a cached summary with zero synchronous LLM cost/latency.
3. Guardrails: identity/safety (`relevance=5`) facts may be rephrased but never
   dropped; the job reports what it merged; the summary is regenerable.

Decision: ship V1 (brief-triggered) now, gate V2 on what V1 teaches about growth.
