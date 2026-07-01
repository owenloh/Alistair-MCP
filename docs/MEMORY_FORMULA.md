# Alistair Memory — scoring + selection formula (V1)

Source of truth for task #5 (memory layer). Provided by the operator; mirrors the Pipecat
local-memory model (`backend/memory/store.py`). Two parts: **scoring** (rank) +
**selection** (pin core, fill budget, trim).

## 1. Per-entry score

```
score(e) = (relevance / 5) * exp( -max(0, age_days) / 30 )
```

- `relevance` = int 1–5 (set at write; default 3). `/5` → 0–1.
- `age_days` = `now − created_at`, in days (`julianday('now') − julianday(created_at)`).
- `max(0, …)` clamps future timestamps (clock skew) to 0.
- `TAU = 30d` decay constant → **half-life ≈ 21 days** (`30·ln2`). Tune TAU to move half-life.
- Pure recency × relevance. **No embeddings / semantic match in V1.**

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

## 3. Write + dedup

```
norm(s) = lowercase, strip punctuation [^\w\s]→space, collapse whitespace
on insert: skip if norm(content) == norm(existing) for any entry of SAME type
```

Catches case/punct/spacing variants ("User is vegan." == "user is VEGAN"). Paraphrase
dedup deferred (needs embeddings). Default relevance 3 if unset.

## 4. Mapping to the MCP event-log (build-spec §3)

Current store = mutable rows. MCP spec wants an **append-only event log**. The formula is
unchanged — apply it to the *folded* state:

1. Fold log → current entries: latest `assert` per `dedup_key`, minus `retract`s.
   (`dedup_key` = `norm(content)` + type, replacing the inline dedup.)
2. Run scoring + core-pin selection on the folded set, **identical math**.
3. `relevance`, `created_at` carried on each event. On `merge`/re-assert, keep
   **earliest** `created_at` (recommended = true fact age, so reaffirming doesn't reset
   decay) — or **latest** for "last reaffirmed" recency. Pick one and document it.
   **Decision: earliest.**

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
