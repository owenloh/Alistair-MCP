"""Tests for the coarse persona tools: load_context + daily_brief."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import alistair as a
from app.services import memory as m

R = []
def check(name, cond):
    R.append((name, bool(cond)))

def fresh_settings(**kw):
    d = tempfile.mkdtemp(prefix="alistair_ctx_")
    return Settings(memory_db_path=os.path.join(d, "mem.db"), **kw)

# === load_context: shape + content ===
s = fresh_settings()
ctx = a.load_context(s)
check("has persona", ctx["persona"]["name"] == "Alistair")
check("persona has voice", "brutally honest" in ctx["persona"]["voice"])
check("no em dashes in voice", "—" not in ctx["persona"]["voice"])
check("routing present", isinstance(ctx["routing"], list) and len(ctx["routing"]) >= 3)
check("routing maps references", any("references" in r["says"] for r in ctx["routing"]))
check("routing maps github account discovery", any("github_whoami" in r["use"] for r in ctx["routing"]))
check("id registry has projects/actions", "projects_db" in ctx["id_registry"] and "actions_db" in ctx["id_registry"])
check("id registry references tray", ctx["id_registry"]["references_tray_page"].startswith("37e6f0cc"))
check("id registry briefing page", ctx["id_registry"]["briefing_page"].startswith("3806f0cc"))
check("library hub distinct from tray", ctx["id_registry"]["library_hub_page"] != ctx["id_registry"]["references_tray_page"])
check("safety mentions replace_content", any("replace_content" in x for x in ctx["safety"]))
check("safety mentions sacred read-first", any("read-first" in x for x in ctx["safety"]))
check("skills index present", isinstance(ctx["skills"], list) and len(ctx["skills"]) >= 1)
check("memory block present", "memory_block" in ctx["memory"])
check("how_to mentions Alistair", "Alistair" in ctx["how_to"])

# === load_context: live memory composed in ===
m.op_save_memory(s, "Owen is allergic to penicillin", type_="fact", relevance=5)
ctx2 = a.load_context(s)
check("memory block reflects saved fact", "penicillin" in ctx2["memory"]["memory_block"].lower())
check("memory total_entries == 1", ctx2["memory"]["total_entries"] == 1)

# === daily_brief: graceful degradation with no tokens configured ===
brief = a.daily_brief(s)
check("brief returns dict with keys", set(["notion", "calendar", "intray", "unavailable"]).issubset(brief))
# nothing is configured in this test env -> all three sources should be 'unavailable', not crash
check("unconfigured sources reported", len(brief["unavailable"]) == 3)
check("each unavailable has reason", all("reason" in u and "source" in u for u in brief["unavailable"]))
check("notion source 503 (no token)", any(u["source"] == "notion" and u["status"] == 503 for u in brief["unavailable"]))
check("brief never raised", brief["deliver_as"].startswith("Follow the daily-brief skill"))

# === HTTP layer via TestClient ===
os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "http_ctx.db")
from app import config as _cfg
_cfg.get_settings.cache_clear()
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
lc = c.post("/api/alistair/load-context")
check("HTTP load-context 200", lc.status_code == 200)
check("HTTP load-context persona", lc.json()["persona"]["name"] == "Alistair")
db = c.post("/api/alistair/daily-brief")
check("HTTP daily-brief 200 (degrades, not 500)", db.status_code == 200)
check("HTTP daily-brief reports unavailable", len(db.json()["unavailable"]) == 3)
mani = c.get("/api/manifest").json()
check("manifest has alistair group", "alistair" in mani["function_apis"])
check("manifest alistair has 5 tools", mani["counts"].get("alistair") == 5)
check("load-context is first shortcut", mani["shortcuts"][0]["calls"][0] == "POST /api/alistair/load-context")

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
