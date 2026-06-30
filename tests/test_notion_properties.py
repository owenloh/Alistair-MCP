"""Tests for build_properties / _coerce_property — the update_properties value layer.

A voice-mode session burned three attempts on a status write because the connector
takes a FLAT value ("Done"), while the model first tried the official Notion nested
object ({"status": {"name": "Done"}}) and got rejected. These tests pin down that
BOTH forms now produce the correct Notion REST payload, plus the other property types.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services import notion as n

R = []
def check(name, cond):
    R.append((name, bool(cond)))

# Schema as returned by retrieve_database().properties (type-only is all build_properties reads).
SCHEMA = {
    "Name": {"type": "title"},
    "Action Status": {"type": "status"},
    "Priority": {"type": "select"},
    "Tags": {"type": "multi_select"},
    "Estimate": {"type": "number"},
    "Done": {"type": "checkbox"},
    "Project": {"type": "relation"},
    "Owner": {"type": "people"},
    "Notes": {"type": "rich_text"},
}

def bp(props):
    return n.build_properties(props, SCHEMA)

# === status: the case from the transcript — flat string is the documented form ===
check("status flat string -> nested name",
      bp({"Action Status": "Done"})["Action Status"] == {"status": {"name": "Done"}})

# === status: the official Notion nested object must ALSO work (no more wasted attempts) ===
check("status official nested object passes through",
      bp({"Action Status": {"status": {"name": "Done"}}})["Action Status"] == {"status": {"name": "Done"}})
check("status partial {name} object -> nested name",
      bp({"Action Status": {"name": "Next"}})["Action Status"] == {"status": {"name": "Next"}})

# === regression guard: the dict must NOT be str()'d into a bogus option name ===
status_val = bp({"Action Status": {"status": {"name": "Done"}}})["Action Status"]["status"]["name"]
check("status name is 'Done', not a stringified dict", status_val == "Done" and "{" not in status_val)

# === select: same flat-vs-nested tolerance ===
check("select flat string", bp({"Priority": "High"})["Priority"] == {"select": {"name": "High"}})
check("select nested object",
      bp({"Priority": {"select": {"name": "High"}}})["Priority"] == {"select": {"name": "High"}})

# === multi_select: list of names, comma string, list of objects, and full nested ===
check("multi_select list of names",
      bp({"Tags": ["a", "b"]})["Tags"] == {"multi_select": [{"name": "a"}, {"name": "b"}]})
check("multi_select comma string",
      bp({"Tags": "a, b"})["Tags"] == {"multi_select": [{"name": "a"}, {"name": "b"}]})
check("multi_select list of objects",
      bp({"Tags": [{"name": "a"}, {"name": "b"}]})["Tags"] == {"multi_select": [{"name": "a"}, {"name": "b"}]})
check("multi_select full nested",
      bp({"Tags": {"multi_select": [{"name": "a"}]}})["Tags"] == {"multi_select": [{"name": "a"}]})

# === relation / people: bare ids and {"id": ...} objects ===
check("relation bare ids",
      bp({"Project": ["id1", "id2"]})["Project"] == {"relation": [{"id": "id1"}, {"id": "id2"}]})
check("relation object ids",
      bp({"Project": [{"id": "id1"}]})["Project"] == {"relation": [{"id": "id1"}]})
check("relation single scalar id",
      bp({"Project": "id1"})["Project"] == {"relation": [{"id": "id1"}]})
check("people object ids",
      bp({"Owner": [{"id": "u1"}]})["Owner"] == {"people": [{"id": "u1"}]})

# === other scalar types still behave ===
check("number passes through", bp({"Estimate": 3})["Estimate"] == {"number": 3})
check("checkbox __YES__", bp({"Done": "__YES__"})["Done"] == {"checkbox": True})
check("checkbox __NO__", bp({"Done": "__NO__"})["Done"] == {"checkbox": False})
check("title -> rich_text array", bp({"Name": "Hello"})["Name"]["title"][0]["text"]["content"] == "Hello")

# === date split still works alongside the new code path ===
dp = n.build_properties({"date:Due:start": "2026-07-01", "date:Due:is_datetime": "0"}, SCHEMA)
check("date split builds a date value", dp["Due"] == {"date": {"start": "2026-07-01"}})

# --- results ---
print("=== RESULTS ===")
ok = True
for nm, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {nm}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
