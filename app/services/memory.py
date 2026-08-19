"""Alistair memory — append-only SQLite event log on a Railway volume.

Implements docs/MEMORY_FORMULA.md exactly:

  * **Append-only event log.** Every write is an `assert` or `retract` row; rows
    are never mutated. Current state = *fold* the log (latest `assert` per
    `dedup_key`, minus `retract`s).
  * **Score** `score(e) = (relevance/5) * exp(-max(0, age_days)/TAU)` — pure
    confirmation-recency x relevance, no embeddings or model dependency.
  * **Selection** pins CORE (`relevance >= core_relevance`, never evicted), fills
    the decayed REST tail up to `top_n`, then trims REST to the token budget.
  * **dedup_key** = `norm(content) + 0x1f + type`. `confirm` events refresh
    last_confirmed_at while preserving the original created_at and canonical text.
  * **Two-stage writes.** Cheap deterministic overlap over the full active store
    returns at most three plausible duplicates. A client model must explicitly
    create/keep-both, refresh, supersede, or flag a conflict.

Single writer = this process: a module-level lock serialises the read-fold-append
of `save`, and SQLite runs in WAL mode with a busy timeout so concurrent readers
never block writers. Per-call connections keep it dependency-light and thread-safe
under FastAPI's sync threadpool.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone

from . import ServiceError
from ..config import Settings

# --- formula constants (the divisor is fixed; TAU/top_n/etc. are tunable) ---
REL_DIVISOR = 5
ALLOWED_TYPES = ("fact", "preference", "action", "summary")
TYPE_LABELS = {
    "fact": "Facts",
    "preference": "Preferences",
    "action": "Open items",
    "summary": "Recent summary",
}

_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    source    TEXT,
    op        TEXT NOT NULL,
    type      TEXT NOT NULL,
    content   TEXT,
    relevance INTEGER NOT NULL DEFAULT 3,
    tags      TEXT,
    dedup_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_dedup ON memory_events(dedup_key);
"""

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_TRANSIENT_RE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (?:morning|afternoon|evening|week)|"
    r"next (?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"remind me|todo|to-do|meeting at|appointment at|brew (?:number|#)|run log)\b",
    re.IGNORECASE,
)
_CANDIDATE_LIMIT = 3
_STOP_WORDS = frozenset(
    "a an and are as at be been being by for from has have he her hers him his i in "
    "is it its me my of on or our ours she that the their theirs them they this to was "
    "we were with you your yours user owen".split()
)


# ---------------------------------------------------------------------------
# Pure helpers (no IO) — directly unit-testable
# ---------------------------------------------------------------------------
def _norm(s: str | None) -> str:
    """lowercase, punctuation -> space, collapse whitespace (dedup normaliser)."""
    s = (s or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _dedup_key(type_: str, content: str | None) -> str:
    return _norm(content) + "\x1f" + (type_ or "fact")


def _clamp_rel(relevance) -> int:
    try:
        return max(1, min(5, int(relevance)))
    except (TypeError, ValueError):
        return 3


def _fold(rows) -> list[dict]:
    """Fold the ascending event log into current entries.

    Latest `assert` wins per dedup_key; `confirm` refreshes only confirmation
    recency; `retract` removes. created_at is the earliest assert ts in the
    current active lifecycle. memory_id is that first assert row id, so it is
    stable across confirmations and metadata re-assertions without a migration.
    """
    state: dict[str, dict] = {}
    for r in rows:  # ascending id == chronological
        key = r["dedup_key"]
        if r["op"] == "assert":
            prev = state.get(key)
            created = r["ts"]
            memory_id = r["id"]
            if prev:
                created = min(prev["created_at"], created)
                memory_id = prev["memory_id"]
            state[key] = {
                "id": r["id"],
                "memory_id": memory_id,
                "type": r["type"],
                "content": r["content"],
                "relevance": _clamp_rel(r["relevance"]),
                "tags": r["tags"],
                "source": r["source"],
                "ts": r["ts"],
                "created_at": created,
                "last_confirmed_at": r["ts"],
                "dedup_key": key,
            }
        elif r["op"] == "confirm":
            prev = state.get(key)
            if prev:
                prev["id"] = r["id"]
                prev["ts"] = r["ts"]
                prev["last_confirmed_at"] = r["ts"]
                prev["source"] = r["source"] or prev["source"]
        elif r["op"] == "retract":
            state.pop(key, None)
    return [e for e in state.values() if (e["content"] or "").strip()]


def _score(entry: dict, now: datetime, tau_days: float) -> float:
    raw = entry.get("last_confirmed_at") or entry.get("created_at") or entry.get("ts")
    try:
        created = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        created = now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    rel = _clamp_rel(entry.get("relevance"))
    return (rel / REL_DIVISOR) * math.exp(-age_days / tau_days)


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4  # ~4 chars/token


def _format(entries: list[dict]) -> str:
    """Group by the canonical type order with human labels, one bullet each."""
    out: list[str] = []
    for t in ALLOWED_TYPES:
        group = [e for e in entries if e["type"] == t and (e["content"] or "").strip()]
        if not group:
            continue
        out.append(TYPE_LABELS[t])
        out.extend("- " + e["content"].strip() for e in group)
    return "\n".join(out)


def _select(entries, now, tau_days, core_relevance, top_n, max_tokens):
    """CORE (pinned) + REST (top_n by score), trimmed to the token budget."""
    for e in entries:
        e["score"] = _score(e, now, tau_days)
    core = sorted(
        (e for e in entries if e["relevance"] >= core_relevance),
        key=lambda e: (-e["score"], e["id"]),
    )
    rest = sorted(
        (e for e in entries if e["relevance"] < core_relevance),
        key=lambda e: (-e["score"], e["id"]),
    )[: max(0, top_n)]
    selected = core + rest
    # drop the lowest-scored REST until we fit; CORE is never evicted.
    while _tokens(_format(selected)) > max_tokens and len(selected) > len(core):
        selected.pop()
    return selected, core


def _word_tokens(*values: str | None) -> set[str]:
    """Meaningful normalized words for deterministic candidate retrieval."""
    return {
        token
        for value in values
        for token in _norm(value).split()
        if token and token not in _STOP_WORDS and len(token) > 1
    }


def _candidate_score(
    entry: dict, content: str, type_: str, tags: str | None
) -> tuple[float, int] | None:
    """Return deterministic lexical similarity, or None when implausible."""
    proposed = _word_tokens(content, tags)
    existing = _word_tokens(entry.get("content"), entry.get("tags"))
    if not proposed or not existing:
        return None
    overlap = proposed & existing
    count = len(overlap)
    if not count:
        return None
    containment = count / min(len(proposed), len(existing))
    jaccard = count / len(proposed | existing)
    phrase = _norm(content) in _norm(entry.get("content")) or _norm(
        entry.get("content")
    ) in _norm(content)
    plausible = phrase or count >= 2 and (containment >= 0.4 or jaccard >= 0.25)
    plausible = plausible or count == 1 and min(len(proposed), len(existing)) <= 2
    if not plausible:
        return None
    score = 0.55 * containment + 0.35 * jaccard
    if entry.get("type") == type_:
        score += 0.1
    return score, count


def _find_candidates(
    entries: Iterable[dict], content: str, type_: str, tags: str | None
) -> list[dict]:
    """Return at most three full-store lexical candidates in a stable order."""
    ranked = []
    for entry in entries:
        similarity = _candidate_score(entry, content, type_, tags)
        if similarity is None:
            continue
        score, overlap = similarity
        ranked.append((score, overlap, entry))
    ranked.sort(
        key=lambda item: (-item[0], -item[1], -item[2]["relevance"], item[2]["memory_id"])
    )
    return [
        {
            "memory_id": entry["memory_id"],
            "type": entry["type"],
            "content": entry["content"],
            "relevance": entry["relevance"],
            "tags": entry["tags"],
            "created_at": entry["created_at"],
            "last_confirmed_at": entry["last_confirmed_at"],
            "similarity": round(score, 4),
        }
        for score, _overlap, entry in ranked[:_CANDIDATE_LIMIT]
    ]


def _validate_write_guardrails(
    content: str,
    type_: str,
    relevance,
    *,
    explicitly_requested: bool,
    core_memory: bool,
) -> int:
    """Enforce the parts of the memory rubric the storage layer can know."""
    try:
        raw_rel = int(relevance)
    except (TypeError, ValueError):
        raw_rel = 3
    if raw_rel < 1 or raw_rel > 5:
        raise ServiceError("relevance must be an integer from 1 to 5.", status_code=400)
    if raw_rel == 5 and not core_memory:
        raise ServiceError(
            "relevance 5 is reserved for permanent cross-client identity, safety, "
            "tool-ownership, or core-workflow invariants; retry with core_memory=true "
            "only when that standard is met.",
            status_code=400,
        )
    transient_reason = type_ in ("action", "summary") or raw_rel == 1 or bool(
        _TRANSIENT_RE.search(content)
    )
    if transient_reason and not explicitly_requested:
        raise ServiceError(
            "This looks transient, automated, or too low-value for shared memory. Route "
            "tasks/logistics to the in-tray or Notion. Retry with explicitly_requested=true "
            "only when the user explicitly asked for this exact item to be remembered.",
            status_code=400,
        )
    return raw_rel


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def _now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as e:
        raise ServiceError(f"Could not open memory store: {e}", status_code=503)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(_SCHEMA)
    return conn


def _all_rows(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM memory_events ORDER BY id ASC").fetchall()


# ---------------------------------------------------------------------------
# Operations (router entry points)
# ---------------------------------------------------------------------------
def op_save_memory(
    settings: Settings,
    content: str | None,
    type_: str = "fact",
    relevance=3,
    tags: str | None = None,
    source: str | None = "voice",
    op: str = "assert",
    resolution: str | None = None,
    target_memory_id: int | None = None,
    explicitly_requested: bool = False,
    core_memory: bool = False,
    now: datetime | None = None,
) -> dict:
    """The only write path: append an event after deterministic duplicate checks."""
    type_ = (type_ or "fact").strip().lower()
    if type_ not in ALLOWED_TYPES:
        raise ServiceError(
            f"Unknown memory type '{type_}'. Use one of: {', '.join(ALLOWED_TYPES)}.",
            status_code=400,
        )
    op = (op or "assert").strip().lower()
    if op not in ("assert", "retract"):
        raise ServiceError("op must be 'assert' or 'retract'.", status_code=400)
    content = (content or "").strip()
    if not content and not (op == "retract" and target_memory_id is not None):
        raise ServiceError("memory 'content' is required.", status_code=400)
    resolution = (resolution or "").strip().lower() or None
    if resolution not in (None, "create", "refresh", "supersede", "conflict"):
        raise ServiceError(
            "resolution must be create, refresh, supersede, or conflict.",
            status_code=400,
        )
    rel = (
        _validate_write_guardrails(
            content,
            type_,
            relevance,
            explicitly_requested=explicitly_requested,
            core_memory=core_memory,
        )
        if op == "assert"
        else _clamp_rel(relevance)
    )
    key = _dedup_key(type_, content)
    ts = _now_iso(now)
    path = settings.memory_db_file()

    with _WRITE_LOCK:
        conn = _connect(path)
        try:
            current = {e["dedup_key"]: e for e in _fold(_all_rows(conn))}
            existing = current.get(key)
            if op == "assert":
                if existing:
                    conn.execute(
                        "INSERT INTO memory_events "
                        "(ts, source, op, type, content, relevance, tags, dedup_key) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            ts,
                            source,
                            "confirm",
                            existing["type"],
                            existing["content"],
                            existing["relevance"],
                            existing["tags"],
                            existing["dedup_key"],
                        ),
                    )
                    conn.commit()
                    return {
                        "status": "refreshed",
                        "reason": "exact memory confirmed",
                        "memory_id": existing["memory_id"],
                        "type": existing["type"],
                        "content": existing["content"],
                        "created_at": existing["created_at"],
                        "last_confirmed_at": ts,
                    }

                entries = list(current.values())
                candidates = _find_candidates(entries, content, type_, tags)
                by_id = {e["memory_id"]: e for e in entries}
                if resolution == "conflict":
                    return {
                        "status": "conflict",
                        "reason": "client flagged a conflict; memory was not changed",
                        "candidates": candidates,
                    }
                if candidates and resolution is None:
                    return {
                        "status": "possible_duplicate",
                        "reason": (
                            "No write occurred. Compare the proposed memory with these candidates, "
                            "then retry with resolution=create, refresh, supersede, or conflict."
                        ),
                        "proposed": {
                            "type": type_,
                            "content": content,
                            "relevance": rel,
                            "tags": tags,
                        },
                        "candidates": candidates,
                    }
                if resolution in ("refresh", "supersede"):
                    target = by_id.get(target_memory_id)
                    candidate_ids = {c["memory_id"] for c in candidates}
                    if target is None or target_memory_id not in candidate_ids:
                        raise ServiceError(
                            "target_memory_id must identify one of the current candidates for "
                            f"resolution='{resolution}'. No write occurred.",
                            status_code=409,
                        )
                    if resolution == "refresh":
                        conn.execute(
                            "INSERT INTO memory_events "
                            "(ts, source, op, type, content, relevance, tags, dedup_key) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                ts,
                                source,
                                "confirm",
                                target["type"],
                                target["content"],
                                target["relevance"],
                                target["tags"],
                                target["dedup_key"],
                            ),
                        )
                        conn.commit()
                        return {
                            "status": "refreshed",
                            "reason": "client confirmed the candidate has the same meaning",
                            "memory_id": target["memory_id"],
                            "type": target["type"],
                            "content": target["content"],
                            "created_at": target["created_at"],
                            "last_confirmed_at": ts,
                        }

                    conn.execute(
                        "INSERT INTO memory_events "
                        "(ts, source, op, type, content, relevance, tags, dedup_key) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            ts,
                            source,
                            "retract",
                            target["type"],
                            target["content"],
                            target["relevance"],
                            target["tags"],
                            target["dedup_key"],
                        ),
                    )
                    cursor = conn.execute(
                        "INSERT INTO memory_events "
                        "(ts, source, op, type, content, relevance, tags, dedup_key) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (ts, source, "assert", type_, content, rel, tags, key),
                    )
                    conn.commit()
                    return {
                        "status": "superseded",
                        "superseded_memory_id": target["memory_id"],
                        "memory_id": cursor.lastrowid,
                        "type": type_,
                        "content": content,
                        "relevance": rel,
                    }

                cursor = conn.execute(
                    "INSERT INTO memory_events "
                    "(ts, source, op, type, content, relevance, tags, dedup_key) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ts, source, "assert", type_, content, rel, tags, key),
                )
                conn.commit()
                return {
                    "status": "created",
                    "memory_id": cursor.lastrowid,
                    "type": type_,
                    "content": content,
                    "relevance": rel,
                    "kept_both": bool(candidates and resolution == "create"),
                }
            # retract
            target = (
                next(
                    (
                        e
                        for e in current.values()
                        if e["memory_id"] == target_memory_id
                    ),
                    None,
                )
                if target_memory_id is not None
                else existing
            )
            if not target:
                return {
                    "status": "noop",
                    "reason": "no matching memory to retract",
                    "type": type_,
                    "content": content,
                }
            conn.execute(
                "INSERT INTO memory_events "
                "(ts, source, op, type, content, relevance, tags, dedup_key) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    source,
                    "retract",
                    target["type"],
                    target["content"],
                    target["relevance"],
                    target["tags"],
                    target["dedup_key"],
                ),
            )
            conn.commit()
            return {
                "status": "retracted",
                "memory_id": target["memory_id"],
                "type": target["type"],
                "content": target["content"],
            }
        finally:
            conn.close()


def op_get_memory(
    settings: Settings,
    top_n: int | None = None,
    max_tokens: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Return the rendered memory block (for an LLM) + selection metadata."""
    now = now or datetime.now(timezone.utc)
    tau = settings.memory_tau_days
    core_rel = settings.memory_core_relevance
    top_n = settings.memory_top_n if top_n is None else int(top_n)
    max_tokens = settings.memory_max_tokens if max_tokens is None else int(max_tokens)

    conn = _connect(settings.memory_db_file())
    try:
        entries = _fold(_all_rows(conn))
    finally:
        conn.close()

    selected, core = _select(entries, now, tau, core_rel, top_n, max_tokens)
    block = _format(selected)
    return {
        "memory_block": block,
        "selected_count": len(selected),
        "core_count": len(core),
        "total_entries": len(entries),
        "tokens_estimate": _tokens(block),
        "persistent": settings.memory_is_persistent,
    }


def _match_score(entry: dict, terms: list[str]) -> float:
    """How well an entry matches the query terms (0 = no match). Substring-on-content/
    tags; more matched terms ranks higher, then relevance scales it."""
    hay = (_norm(entry.get("content")) + " " + _norm(entry.get("tags"))).strip()
    if not terms:
        return 1.0
    hits = sum(1 for t in terms if t and t in hay)
    if not hits:
        return 0.0
    coverage = hits / len(terms)
    return coverage * (1.0 + _clamp_rel(entry.get("relevance")) / REL_DIVISOR)


def op_search_memory(
    settings: Settings,
    query: str | None = None,
    limit: int = 20,
    type_: str | None = None,
    now: datetime | None = None,
) -> dict:
    """On-demand recall over the FULL store (not just the loaded block).

    Returns every entry matching `query` (case-insensitive token match on content/tags),
    ranked by match strength then recency-x-relevance, REGARDLESS of decay — so older or
    low-relevance facts that `get_memory` omits are still retrievable. An empty query
    returns the whole store ranked by score (acts as a full list). Optional `type_`
    filters to one of fact/preference/action/summary.
    """
    now = now or datetime.now(timezone.utc)
    conn = _connect(settings.memory_db_file())
    try:
        entries = _fold(_all_rows(conn))
    finally:
        conn.close()

    if type_:
        type_ = type_.strip().lower()
        entries = [e for e in entries if e["type"] == type_]

    terms = [t for t in _norm(query).split(" ") if t] if query else []
    scored = []
    for e in entries:
        ms = _match_score(e, terms)
        if ms <= 0:
            continue
        e["match"] = round(ms, 4)
        e["score"] = round(_score(e, now, settings.memory_tau_days), 6)
        scored.append(e)
    # rank: match strength first, then recency x relevance
    scored.sort(key=lambda e: (-e["match"], -e["score"], e["id"]))
    limit = max(1, int(limit or 20))
    top = scored[:limit]
    return {
        "query": query or "",
        "type": type_,
        "match_count": len(scored),
        "returned": len(top),
        "total_entries": len(entries),
        "results": [
            {
                "memory_id": e["memory_id"],
                "type": e["type"],
                "content": e["content"],
                "relevance": e["relevance"],
                "created_at": e["created_at"],
                "last_confirmed_at": e["last_confirmed_at"],
                "match": e["match"],
                "score": e["score"],
                "tags": e["tags"],
            }
            for e in top
        ],
    }


def op_list_memory(settings: Settings, now: datetime | None = None) -> dict:
    """Raw folded view (debug + the basis for the one-way Notion mirror)."""
    now = now or datetime.now(timezone.utc)
    conn = _connect(settings.memory_db_file())
    try:
        rows = _all_rows(conn)
    finally:
        conn.close()
    entries = _fold(rows)
    for e in entries:
        e["score"] = round(_score(e, now, settings.memory_tau_days), 6)
    entries.sort(key=lambda e: (-e["score"], e["id"]))
    return {
        "count": len(entries),
        "total_events": len(rows),
        "persistent": settings.memory_is_persistent,
        "db_path": settings.memory_db_file(),
        "entries": [
            {
                "memory_id": e["memory_id"],
                "type": e["type"],
                "content": e["content"],
                "relevance": e["relevance"],
                "created_at": e["created_at"],
                "last_confirmed_at": e["last_confirmed_at"],
                "score": e["score"],
                "tags": e["tags"],
                "source": e["source"],
            }
            for e in entries
        ],
    }
