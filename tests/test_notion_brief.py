"""Regression tests for build_brief's database filters.

"Action Status" (Actions DB) is a Notion *status*-type property, while "Status"
(Projects DB) is a *select*-type property. build_brief must filter each with the
matching condition. The historical bug filtered "Action Status" with a `select`
condition, which Notion rejects with HTTP 400 — so the brief silently lost all of
its Next / Someday actions. The FakeClient below mirrors that real API behaviour:
a `select` filter on the status property raises, exactly as Notion does.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import notion as n

R = []
def check(name, cond):
    R.append((name, bool(cond)))


def rt(text):
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


PROJ_DB = "projects-db"
ACT_DB = "actions-db"
P_ACTIVE, P_SOMEDAY, P_DROPPED = "proj-active", "proj-someday", "proj-dropped"


def project(pid, title, status):
    return {"id": pid, "properties": {
        "Project": {"title": rt(title)},
        "Status": {"select": {"name": status}},
        "Direction": {"rich_text": rt(f"{title} direction")},
    }}


def action(name, status, project_ids):
    return {"id": f"act-{name}", "properties": {
        "Name": {"title": rt(name)},
        "Action Status": {"status": {"name": status}},
        "Project": {"relation": [{"id": p} for p in project_ids]},
    }}


PROJECTS = [
    project(P_ACTIVE, "Coffee Startup", "Active"),
    project(P_SOMEDAY, "Someday Idea", "Someday"),
    project(P_DROPPED, "Abandoned", "Dropped"),
]
ACTIONS = [
    action("Email Kelly", "Next", [P_ACTIVE]),   # qualifies (project Active)
    action("Buy beans", "Next", []),             # qualifies (no project)
    action("Ancient task", "Next", [P_DROPPED]), # dropped -> filtered out by qualifies()
    action("Maybe later", "Someday", [P_SOMEDAY]),
    action("Draft the deck", "In progress", [P_ACTIVE]),   # the Now backbone
    action("Wrap up taxes", "In progress", []),            # in-progress, project-less
    action("Finish and drop", "In progress", [P_DROPPED]), # explicit In-progress wins over project status
]


class FakeClient:
    """Records every database filter and mimics Notion's type-checking of it."""
    def __init__(self):
        self.filters = []

    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

    def query_database_all(self, db_id, flt=None):
        self.filters.append({"db": db_id, "filter": flt})
        if db_id == PROJ_DB:
            return list(PROJECTS)
        # Actions DB: emulate Notion rejecting a mismatched-type filter (400).
        if flt and "select" in flt:
            raise n.ServiceError(
                "Notion API returned HTTP 400: Action Status is expected to be status.",
                status_code=502,
            )
        want = ((flt or {}).get("status") or {}).get("equals")
        return [a for a in ACTIONS
                if (a["properties"]["Action Status"]["status"]["name"] == want)]


S = Settings(projects_db_id=PROJ_DB, actions_db_id=ACT_DB)
_orig = n.NotionClient
n.NotionClient = lambda settings: FakeClient()
# Capture the instance actually used so we can inspect the filters it recorded.
_captured = {}
def _factory(settings):
    fc = FakeClient()
    _captured["fc"] = fc
    return fc
n.NotionClient = _factory

try:
    brief = n.build_brief(S)
finally:
    n.NotionClient = _orig

fc = _captured["fc"]
act_filters = [f["filter"] for f in fc.filters if f["db"] == ACT_DB]

# The regression guard: Action Status must be queried as a status, never a select.
check("brief did not raise (no select-on-status 400)", isinstance(brief, dict))
check("action filters use 'status' key",
      all("status" in f and "select" not in f for f in act_filters))
check("queried Action Status == Next", {"property": "Action Status", "status": {"equals": "Next"}} in act_filters)
check("queried Action Status == In progress", {"property": "Action Status", "status": {"equals": "In progress"}} in act_filters)
check("queried Action Status == Someday", {"property": "Action Status", "status": {"equals": "Someday"}} in act_filters)

# Content assembled correctly from the (now working) filtered reads.
next_names = {a["name"] for a in brief["NEXT_ACTIONS"]}
check("Next actions include the qualifying ones", {"Email Kelly", "Buy beans"} <= next_names)
check("Next actions exclude actions under a Dropped project", "Ancient task" not in next_names)
check("Someday actions captured", {a["name"] for a in brief["SOMEDAY_ACTIONS"]} == {"Maybe later"})

# In-progress = the Now backbone. It is NOT project-qualified: an explicit In-progress
# signal is a deliberate "I'm doing this" and shows regardless of the project's status.
inprog_names = {a["name"] for a in brief["IN_PROGRESS_ACTIONS"]}
check("In-progress actions captured", {"Draft the deck", "Wrap up taxes"} <= inprog_names)
check("In-progress ignores the project qualifier (explicit signal wins)", "Finish and drop" in inprog_names)
check("In-progress kept separate from Next", not (inprog_names & next_names))

# Projects Status is a select type and must still be read that way.
check("Active projects read via select", [p["name"] for p in brief["ACTIVE_PROJECTS"]] == ["Coffee Startup"])
check("Someday projects read via select", brief["SOMEDAY_PROJECTS"] == ["Someday Idea"])

# --- results ---
print("=== RESULTS ===")
ok = True
for nm, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {nm}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
