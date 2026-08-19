"""Focused acceptance tests for deterministic two-stage memory writes."""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings
from app.services import ServiceError
from app.services import memory as m

UTC = timezone.utc
T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 19, tzinfo=UTC)
R = []


def check(name, condition):
    R.append((name, bool(condition)))


def fresh_settings(**kwargs):
    root = tempfile.mkdtemp(prefix="alistair_mem_guardrails_")
    return Settings(memory_db_path=os.path.join(root, "memory.db"), **kwargs)


def fails(fn, status=None):
    try:
        fn()
    except ServiceError as exc:
        return status is None or exc.status_code == status
    return False


# Exact repeats append confirmation events while preserving one canonical memory.
s = fresh_settings()
created = m.op_save_memory(s, "User is based in London", now=T0)
refreshed = m.op_save_memory(s, "User is based in London.", now=T1)
store = m.op_list_memory(s, now=NOW)
check("exact repeat returns refreshed", refreshed["status"] == "refreshed")
check("exact repeat keeps stable memory id", refreshed["memory_id"] == created["memory_id"])
check("exact repeat leaves one active memory", store["count"] == 1)
check("exact repeat appends confirmation event", store["total_events"] == 2)
check("confirmation preserves created_at", store["entries"][0]["created_at"] == T0.isoformat())
check("confirmation refreshes last_confirmed_at", store["entries"][0]["last_confirmed_at"] == T1.isoformat())

# Merely reading never confirms a memory.
events_before_reads = store["total_events"]
m.op_get_memory(s, now=NOW)
m.op_search_memory(s, query="London", now=NOW)
m.op_list_memory(s, now=NOW)
check("reads never append confirmation events",
      m.op_list_memory(s, now=NOW)["total_events"] == events_before_reads)

# A recent confirmation, not original creation, drives ranking decay.
rank = fresh_settings(memory_top_n=10)
m.op_save_memory(rank, "Prefers concise operational reports", now=T0)
m.op_save_memory(rank, "Prefers detailed technical diagrams", now=T1)
m.op_save_memory(rank, "Prefers concise operational reports", now=NOW - timedelta(days=1))
ranked = m.op_list_memory(rank, now=NOW)["entries"]
check("last_confirmed_at drives score", "concise" in ranked[0]["content"].lower())
check("ranking does not rewrite original creation",
      ranked[0]["created_at"] == T0.isoformat())

# Ambiguity returns a bounded shortlist and writes nothing.
amb = fresh_settings()
original = m.op_save_memory(amb, "User is based in London", now=T0)
before = m.op_list_memory(amb, now=NOW)
possible = m.op_save_memory(amb, "User lives in London", now=T1)
after = m.op_list_memory(amb, now=NOW)
check("near match returns possible_duplicate", possible["status"] == "possible_duplicate")
check("possible duplicate includes stable id",
      possible["candidates"][0]["memory_id"] == original["memory_id"])
check("ambiguity writes no event", before["total_events"] == after["total_events"])
check("ambiguity writes no active memory", before["count"] == after["count"] == 1)

# The explicit semantic resolutions are safe and deterministic.
kept = m.op_save_memory(amb, "User lives in London", resolution="create", now=T1)
check("explicit create keeps both", kept["status"] == "created" and kept["kept_both"])
check("keep-both has two active memories", m.op_list_memory(amb, now=NOW)["count"] == 2)

refresh_store = fresh_settings()
canonical = m.op_save_memory(refresh_store, "User is based in London", now=T0)
same_meaning = m.op_save_memory(
    refresh_store,
    "User lives in London",
    resolution="refresh",
    target_memory_id=canonical["memory_id"],
    now=T1,
)
current = m.op_list_memory(refresh_store, now=NOW)
check("semantic refresh keeps canonical text", same_meaning["content"] == "User is based in London")
check("semantic refresh keeps one active memory", current["count"] == 1)
check("semantic refresh preserves created_at", current["entries"][0]["created_at"] == T0.isoformat())

sup_store = fresh_settings()
old = m.op_save_memory(sup_store, "User lives in London", now=T0)
superseded = m.op_save_memory(
    sup_store,
    "User lives in Manchester",
    resolution="supersede",
    target_memory_id=old["memory_id"],
    now=T1,
)
sup_current = m.op_list_memory(sup_store, now=NOW)
check("supersede reports both ids",
      superseded["status"] == "superseded" and
      superseded["superseded_memory_id"] == old["memory_id"] and
      superseded["memory_id"] != old["memory_id"])
check("supersede preserves one active version", sup_current["count"] == 1)
check("supersede activates updated information",
      sup_current["entries"][0]["content"] == "User lives in Manchester")
check("supersede appends retract plus assert", sup_current["total_events"] == 3)

conf_store = fresh_settings()
m.op_save_memory(conf_store, "User lives in London", now=T0)
conf_before = m.op_list_memory(conf_store, now=NOW)
conflict = m.op_save_memory(
    conf_store, "User lives in Manchester", resolution="conflict", now=T1
)
conf_after = m.op_list_memory(conf_store, now=NOW)
check("conflict is reported unwritten", conflict["status"] == "conflict")
check("conflict changes no events", conf_after["total_events"] == conf_before["total_events"])
check("invalid target is conflict-safe",
      fails(lambda: m.op_save_memory(
          conf_store, "User lives in Manchester", resolution="supersede",
          target_memory_id=999999, now=T1), status=409))
check("invalid target wrote nothing",
      m.op_list_memory(conf_store, now=NOW)["total_events"] == conf_before["total_events"])

# Candidate retrieval is bounded at three even across the full active store.
bounded = fresh_settings()
for index in range(6):
    m.op_save_memory(
        bounded,
        f"Project Atlas uses workflow alpha variant {index}",
        resolution="create" if index else None,
        now=T0 + timedelta(days=index),
    )
shortlist = m.op_save_memory(bounded, "Project Atlas uses workflow alpha", now=NOW)
check("candidate shortlist is capped at three",
      shortlist["status"] == "possible_duplicate" and len(shortlist["candidates"]) == 3)

# Deterministic relevance/transient gates reject silent pollution but permit explicit exceptions.
gates = fresh_settings()
check("relevance 5 requires core acknowledgement",
      fails(lambda: m.op_save_memory(gates, "GitHub owner is owenloh", relevance=5)))
check("core invariant can be explicitly stored",
      m.op_save_memory(gates, "GitHub owner is owenloh", relevance=5,
                       core_memory=True)["status"] == "created")
check("action is rejected by default",
      fails(lambda: m.op_save_memory(gates, "Ship feature", type_="action")))
check("summary is rejected by default",
      fails(lambda: m.op_save_memory(gates, "Session summary", type_="summary")))
check("relevance 1 is rejected by default",
      fails(lambda: m.op_save_memory(gates, "Weak uncertain note", relevance=1)))
check("transient logistics are rejected by default",
      fails(lambda: m.op_save_memory(gates, "Meeting at 4 today")))
check("explicitly requested exception is accepted",
      m.op_save_memory(gates, "Remember meeting at 4 today", explicitly_requested=True)["status"] == "created")
check("out-of-range relevance is rejected",
      fails(lambda: m.op_save_memory(gates, "Invalid relevance", relevance=6)))

# Existing Railway-volume rows need no ALTER/backfill and acquire stable derived ids.
legacy = fresh_settings()
conn = sqlite3.connect(legacy.memory_db_file())
conn.executescript(m._SCHEMA)
legacy_key = m._dedup_key("fact", "Legacy durable fact")
conn.execute(
    "INSERT INTO memory_events (ts,source,op,type,content,relevance,tags,dedup_key) "
    "VALUES (?,?,?,?,?,?,?,?)",
    (T0.isoformat(), "legacy", "assert", "fact", "Legacy durable fact", 3, None, legacy_key),
)
conn.commit()
conn.close()
legacy_current = m.op_list_memory(legacy, now=NOW)
check("legacy row folds without migration", legacy_current["count"] == 1)
check("legacy row gets stable derived memory id", legacy_current["entries"][0]["memory_id"] == 1)
check("legacy row defaults confirmation to assert time",
      legacy_current["entries"][0]["last_confirmed_at"] == T0.isoformat())
legacy_refresh = m.op_save_memory(legacy, "Legacy durable fact.", now=T1)
check("legacy row supports new confirmation event", legacy_refresh["status"] == "refreshed")
legacy_retract = m.op_save_memory(
    legacy, None, op="retract", target_memory_id=legacy_refresh["memory_id"], now=NOW
)
check("stable id retract does not require old content", legacy_retract["status"] == "retracted")

print("=== RESULTS ===")
ok = True
for name, passed in R:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    ok = ok and passed
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, passed in R if passed)}/{len(R)})")
sys.exit(0 if ok else 1)
