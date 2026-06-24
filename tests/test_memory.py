"""Tests for the memory event-log + scoring/selection formula (docs/MEMORY_FORMULA.md)."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import memory as m

R = []
def check(name, cond):
    R.append((name, bool(cond)))

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)        # "old"
T1 = datetime(2026, 6, 1, tzinfo=UTC)        # "newer"
NOW = datetime(2026, 6, 24, tzinfo=UTC)      # scoring instant

def fresh_settings(**kw):
    d = tempfile.mkdtemp(prefix="alistair_mem_")
    return Settings(memory_db_path=os.path.join(d, "mem.db"), **kw)

# === pure: norm + dedup_key ===
check("norm lowercases+strips punct", m._norm("User is VEGAN!!") == "user is vegan")
check("norm collapses whitespace", m._norm("a   b\tc") == "a b c")
check("dedup same across case/punct",
      m._dedup_key("fact", "User is vegan.") == m._dedup_key("fact", "user is VEGAN"))
check("dedup differs by type",
      m._dedup_key("fact", "x") != m._dedup_key("preference", "x"))

# === pure: fold (assert/retract/reassert earliest created_at) ===
def row(i, ts, op, type_, content, rel=3, key=None):
    return {"id": i, "ts": ts.isoformat(), "op": op, "type": type_, "content": content,
            "relevance": rel, "tags": None, "source": "test",
            "dedup_key": key or m._dedup_key(type_, content)}
folded = m._fold([
    row(1, T0, "assert", "fact", "likes coffee", 3),
    row(2, T1, "assert", "fact", "likes coffee", 5),   # re-assert: keep earliest created_at, new rel
])
check("fold collapses to one", len(folded) == 1)
check("fold keeps earliest created_at", folded[0]["created_at"] == T0.isoformat())
check("fold takes latest relevance", folded[0]["relevance"] == 5)

folded2 = m._fold([
    row(1, T0, "assert", "fact", "temp note", 3),
    row(2, T1, "retract", "fact", "temp note", 3),
])
check("fold retract removes entry", folded2 == [])

folded3 = m._fold([
    row(1, T0, "assert", "fact", "x", 3),
    row(2, T1, "retract", "fact", "x", 3),
    row(3, NOW, "assert", "fact", "x", 3),   # re-assert after retract -> fresh age
])
check("reassert after retract is fresh", len(folded3) == 1 and folded3[0]["created_at"] == NOW.isoformat())

check("fold drops empty content", m._fold([row(1, T0, "assert", "fact", "   ", 3)]) == [])

# === pure: score (recency x relevance, future clamp) ===
old = {"id": 1, "created_at": T0.isoformat(), "relevance": 3}
new = {"id": 2, "created_at": T1.isoformat(), "relevance": 3}
check("newer scores higher", m._score(new, NOW, 30.0) > m._score(old, NOW, 30.0))
hi = {"id": 3, "created_at": T1.isoformat(), "relevance": 5}
check("higher relevance scores higher", m._score(hi, NOW, 30.0) > m._score(new, NOW, 30.0))
future = {"id": 4, "created_at": (NOW + timedelta(days=10)).isoformat(), "relevance": 5}
check("future clamps to age 0 (score==rel/5)", abs(m._score(future, NOW, 30.0) - 1.0) < 1e-9)

# === pure: tokens + format ordering ===
check("tokens ~ chars/4", m._tokens("abcd") == 1 and m._tokens("abcde") == 2)
ents = [
    {"id": 1, "type": "summary", "content": "S", "relevance": 3},
    {"id": 2, "type": "fact", "content": "F", "relevance": 3},
    {"id": 3, "type": "action", "content": "A", "relevance": 3},
    {"id": 4, "type": "preference", "content": "P", "relevance": 3},
]
blk = m._format(ents)
check("format groups in canonical order",
      blk.index("Facts") < blk.index("Preferences") < blk.index("Open items") < blk.index("Recent summary"))
check("format renders bullets", "- F" in blk and "- P" in blk)
check("format skips empty groups", "Preferences" in m._format([ents[3]]) and "Facts" not in m._format([ents[3]]))

# === pure: selection — core pinned, top_n cap, token trim ===
def ent(i, type_, content, rel, created):
    return {"id": i, "type": type_, "content": content, "relevance": rel, "created_at": created.isoformat()}
core_e = ent(1, "fact", "ALLERGIC TO PENICILLIN", 5, T0)         # old but core
rest = [ent(10 + i, "fact", f"trivia number {i}", 3, NOW) for i in range(12)]
sel, core = m._select([core_e] + rest, NOW, 30.0, core_relevance=5, top_n=8, max_tokens=100000)
check("core detected", len(core) == 1 and core[0]["id"] == 1)
check("rest capped at top_n", len(sel) == 1 + 8)
# tiny budget -> everything but core trimmed away
sel2, core2 = m._select([core_e] + rest, NOW, 30.0, core_relevance=5, top_n=8, max_tokens=5)
check("core survives tiny budget", any(e["id"] == 1 for e in sel2))
check("rest evicted under tiny budget", len(sel2) == len(core2) == 1)
blk2 = m._format(sel2)
check("core content present after trim", "ALLERGIC TO PENICILLIN" in blk2)
check("rest content absent after trim", "trivia number" not in blk2)

# === IO: save/get/list round trip with injected time ===
s = fresh_settings(memory_top_n=8, memory_max_tokens=1200, memory_core_relevance=5)
check("created on first save",
      m.op_save_memory(s, "Owen is allergic to penicillin", type_="fact", relevance=5, now=T0)["status"] == "created")
check("noop on identical save",
      m.op_save_memory(s, "Owen is allergic to penicillin", type_="fact", relevance=5, now=T1)["status"] == "noop")
check("updated on relevance change",
      m.op_save_memory(s, "Owen is allergic to penicillin", type_="fact", relevance=4, now=T1)["status"] == "updated")
# restore to core (rel 5) so the recall assertions below treat it as pinned
m.op_save_memory(s, "Owen is allergic to penicillin", type_="fact", relevance=5, now=T1)
m.op_save_memory(s, "Prefers concise replies", type_="preference", relevance=3, now=T1)
m.op_save_memory(s, "Ship the memory layer", type_="action", relevance=3, now=T1)

got = m.op_get_memory(s, now=NOW)
check("get returns block", isinstance(got["memory_block"], str) and got["memory_block"])
check("get core_count==1", got["core_count"] == 1)
check("get total_entries==3", got["total_entries"] == 3)
check("block has the core fact", "penicillin" in got["memory_block"].lower())
check("block has preference", "concise" in got["memory_block"].lower())
check("block ordered facts before prefs",
      got["memory_block"].index("Facts") < got["memory_block"].index("Preferences"))

lst = m.op_list_memory(s, now=NOW)
check("list count==3", lst["count"] == 3)
check("list reports non-persistent (no volume)", lst["persistent"] is False)
check("list scores present + sorted desc",
      all("score" in e for e in lst["entries"]) and
      lst["entries"] == sorted(lst["entries"], key=lambda e: -e["score"]))

# created_at preserved across the relevance update (earliest = T0)
pen = [e for e in lst["entries"] if "penicillin" in (e["content"] or "").lower()][0]
check("created_at kept earliest across update", pen["created_at"] == T0.isoformat())
check("relevance reflects latest assert (5)", pen["relevance"] == 5)

# === IO: retract drops it from recall ===
m.op_save_memory(s, "Ship the memory layer", type_="action", op="retract", now=NOW)
got2 = m.op_get_memory(s, now=NOW)
check("retract removes from block", "ship the memory layer" not in got2["memory_block"].lower())
check("retract decrements total", got2["total_entries"] == 2)
check("retract noop when absent",
      m.op_save_memory(s, "never existed", type_="fact", op="retract", now=NOW)["status"] == "noop")

# === IO: validation ===
def expect_error(fn):
    try:
        fn(); return False
    except m.ServiceError:
        return True
check("rejects unknown type", expect_error(lambda: m.op_save_memory(s, "x", type_="bogus")))
check("rejects empty content", expect_error(lambda: m.op_save_memory(s, "   ", type_="fact")))
check("rejects bad op", expect_error(lambda: m.op_save_memory(s, "x", type_="fact", op="nuke")))

# === IO: persistence across reconnects (same db file) ===
s2 = Settings(memory_db_path=s.memory_db_file())
check("data survives new connection", m.op_get_memory(s2, now=NOW)["total_entries"] == 2)

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
