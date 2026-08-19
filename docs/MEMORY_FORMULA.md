# Alistair Memory — scoring, selection, and guarded writes

Source of truth for task #5 (memory layer). Provided by the user; mirrors the Pipecat
local-memory model (`backend/memory/store.py`). Two parts: **scoring** (rank) +
**selection** (pin core, fill budget, trim).

## 1. Per-entry score

```
score(e) = (relevance / 5) * exp( -max(0, age_days) / 30 )
```

- `relevance` = int 1–5 (set at write; default 3). `/5` → 0–1.
- `age_days` = `now − last_confirmed_at`, in days. For legacy rows and never-confirmed
  memories, `last_confirmed_at = created_at`.
- `max(0, …)` clamps future timestamps (clock skew) to 0.
- `TAU = 30d` decay constant → **half-life ≈ 21 days** (`30·ln2`). Tune TAU to move half-life.
- Pure confirmation recency × relevance. Reads never change `last_confirmed_at`.
  **No embeddings, model API, or server-side semantic decision.**

## 2. Selection (`read_memory_block`)

Inputs: `top_n` (=8), `max_tokens` (=1200), `core_relevance` (=5).

```
CORE = entries WHERE relevance >= core_relevance, order by score desc, id asc   # pinned
REST = entries WHERE relevance <  core_relevance, order by score desc, id asc, take top_n
selected = CORE + REST
while tokens(format(selected)) > max_tokens and len(selected) > len(CORE):
    selected.pop()      # drop lowest-scored REST first; NEVER drop CORE
return format(selected)
```

**Key rule: core (rel ≥ 5) is pinned — never evicted by token budget or recency.**
Fixes recency crowding out standing facts (allergies, identity, safety). REST is the
decayed tail.

- `tokens(text) = (len(text)+3)//4` (~4 chars/tok).
- Tie-break `id ASC` after score → **deterministic per DB state** → stable cache prefix.
- `format`: group by type order `[fact, preference, action, summary]`, labels
  `Facts / Preferences / Open items / Recent summary`, one `- line` each. Empty content
  filtered (`content IS NOT NULL AND TRIM != ''`).

## 3. Write, confirmation, and deterministic candidates

```
norm(s) = lowercase, strip punctuation [^\w\s]→space, collapse whitespace
dedup_key = norm(content) + 0x1f + type
exact active match -> append confirm event; keep canonical text + created_at
otherwise -> rank lexical candidates from the full active folded store; return <= 3
```

Catches case/punctuation/spacing variants. Candidate retrieval uses normalized content/tag
token overlap plus type, with stable tie-breaking. It deliberately makes no semantic claim.
If candidates are returned, the first call writes nothing. The connected client model must
make a second explicit call:

- `create`: genuinely new; keep both.
- `refresh`: same meaning; append `confirm` for the target and keep one canonical entry.
- `supersede`: target is outdated; append target `retract` plus replacement `assert` in one transaction.
- `conflict`: leave unwritten.

The shortlist exposes `memory_id`, derived from the first assertion row in the current active
lifecycle. It remains stable across confirmations. Retraction by ID avoids making a model resend
old text. Existing SQLite volumes need no table rewrite: `confirm` is a new value in the existing
text `op` column, and legacy rows derive `memory_id` and `last_confirmed_at` during fold.

## 4. Mapping to the MCP event-log (build-spec §3)

Current store = mutable rows. MCP spec wants an **append-only event log**. The formula is
unchanged — apply it to the *folded* state:

1. Fold log → current entries: latest `assert` per `dedup_key`, `confirm` updates only
   `last_confirmed_at`, and `retract` removes the active entry.
   (`dedup_key` = `norm(content)` + type, replacing the inline dedup.)
2. Run scoring + core-pin selection on the folded set, **identical math**.
3. Preserve the earliest `created_at` for provenance. Rank by the latest explicit
   `last_confirmed_at`, so reaffirmation refreshes ranking while mere recall does not.

## 5. Relevance and transient-write guardrails

- **5:** permanent cross-client identity/address-form, safety, tool-ownership, or
  core-workflow invariant. Requires `core_memory=true`.
- **4:** durable and important, but situational.
- **3:** default durable context.
- **2:** narrow, uncertain, or deferred context.
- **1:** normally reject or route elsewhere.

`action`, `summary`, relevance 1, and obvious transient logistics are rejected unless
`explicitly_requested=true`, which means the user explicitly asked to remember that exact
exception. Normal tasks, plans, run/brew logs, and today's logistics belong in the in-tray
or Notion.

## Tunables (lift into MCP config)

| Param | V1 value | Effect |
|---|---|---|
| `TAU_DAYS` | 30 | bigger = slower decay (longer memory) |
| `REL_DIVISOR` | 5 | match max relevance |
| `core_relevance` | 5 | pin threshold; rel ≥ this never evicted |
| `top_n` | 8 | REST cap before token trim |
| `max_tokens` | 1200 | block budget |

Implementable as-is. Storage = SQLite append-only event log on the Railway **volume**
(see ROADMAP #2); single writer = the MCP process; Notion = one-way human-readable mirror.
