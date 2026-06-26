"""Safety tests for the hardened update_content path: multi-match guard,
block-boundary awareness, child-page deletion guard, and precise in-place replace.

Drives the FULL op_update_page path (render_blocks -> flat -> _apply_content_update)
by monkeypatching NotionClient with an in-memory fake that records every mutation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import notion as n

R = []
def check(name, cond):
    R.append((name, bool(cond)))


def rt(text, **ann):
    tok = {"type": "text", "plain_text": text, "text": {"content": text}}
    if ann:
        tok["annotations"] = ann
    return [tok]


def b(bid, btype, text="", children=None, **data):
    d = dict(data)
    if text or btype not in ("divider", "image"):
        d.setdefault("rich_text", rt(text))
    return {"id": bid, "type": btype, "has_children": bool(children),
            btype: d, "_children": children or []}


class FakeClient:
    """In-memory Notion stand-in. tree is the page's top-level blocks; nested blocks
    carry their own '_children'. Records deleted/updated/appended."""
    def __init__(self, pid, blocks):
        self.pid = n.extract_id(pid)  # op_update_page dash-formats the page id
        self.index = {}
        self._kids = {}
        self._register(self.pid, blocks)
        self.deleted = []
        self.updated = []
        self.appended = []

    def _register(self, parent_id, blocks):
        self._kids[parent_id] = [dict(bl) for bl in blocks]
        for bl in blocks:
            kids = bl.get("_children") or []
            self.index[bl["id"]] = bl
            if kids:
                self._register(bl["id"], kids)

    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

    def block_children(self, block_id, page_size=100, start_cursor=None):
        return {"results": [self._read(bl) for bl in self._kids.get(block_id, [])],
                "has_more": False, "next_cursor": None}

    def block_children_all(self, block_id):
        return [self._read(bl) for bl in self._kids.get(block_id, [])]

    def _read(self, bl):
        out = {k: v for k, v in bl.items() if k != "_children"}
        return out

    def retrieve_block(self, block_id):
        bl = self.index.get(block_id)
        if not bl:
            raise n.ServiceError("block not found", status_code=404)
        return self._read(bl)

    def update_block(self, block_id, body):
        self.updated.append((block_id, body))
        return {"type": next(iter(body), None)}

    def delete_block(self, block_id):
        self.deleted.append(block_id)
        return {}

    def append_children(self, block_id, children, after=None):
        self.appended.append({"parent": block_id, "after": after, "n": len(children)})
        return {"results": [{"id": f"app{i}"} for i, _ in enumerate(children)]}


S = Settings(notion_token="x")
PID = "11111111111111111111111111111111"


def run(blocks, update, allow_deleting=False):
    """Run one update_content op through op_update_page against a fresh FakeClient."""
    fake = FakeClient(PID, blocks)
    orig = n.NotionClient
    n.NotionClient = lambda settings: fake
    try:
        res = n.op_update_page(S, page_id=PID, command="update_content",
                               content_updates=[update],
                               allow_deleting_content=allow_deleting)
        return res, fake, None
    except n.ServiceError as e:
        return None, fake, e
    finally:
        n.NotionClient = orig


# ---------------------------------------------------------------------------
# 1. Multi-match guard
# ---------------------------------------------------------------------------
res, fake, err = run([b("b1", "paragraph", "Hello world")],
                     {"old_str": "world", "new_str": "planet"})
check("single match replace applies", err is None and fake.updated and fake.updated[0][0] == "b1")
check("single replace not deleted", fake.deleted == [])

res, fake, err = run([b("b1", "paragraph", "Resurface this"),
                      b("b2", "paragraph", "Resurface that")],
                     {"old_str": "Resurface", "new_str": ""})
check("duplicate old_str -> 409", err is not None and err.status_code == 409)
check("duplicate old_str -> no deletes", fake.deleted == [])
check("409 message has count", err is not None and "matched 2 places" in err.message)

res, fake, err = run([b("b1", "paragraph", "Resurface this"),
                      b("b2", "paragraph", "Resurface that")],
                     {"old_str": "Resurface", "new_str": "Revive", "replace_all_matches": True})
check("replace_all updates both", err is None and len(fake.updated) == 2)


# ---------------------------------------------------------------------------
# 2. The exact regression: "Resurface" must NOT delete 38 blocks
# ---------------------------------------------------------------------------
disaster = []
for i in range(38):
    word = "Resurface" if i % 2 == 0 else ("Protect" if i % 3 == 0 else "misc")
    disaster.append(b(f"d{i}", "paragraph", f"{word} item {i}"))
# spread some inside a toggle to mirror the original cross-toggle damage
toggle_kids = [b("tk1", "paragraph", "Resurface inside toggle"),
               b("tk2", "paragraph", "Protect inside toggle")]
disaster.append(b("tg", "toggle", "A toggle", children=toggle_kids))
res, fake, err = run(disaster, {"old_str": "Resurface", "new_str": ""})
check("Resurface delete -> 409 (fail-safe)", err is not None and err.status_code == 409)
check("Resurface delete -> ZERO deletions", fake.deleted == [])
check("Resurface delete -> ZERO updates", fake.updated == [])


# ---------------------------------------------------------------------------
# 3. Block-boundary awareness
# ---------------------------------------------------------------------------
# partial substring replace preserves surrounding text
res, fake, err = run([b("b1", "paragraph", "alpha BETA gamma")],
                     {"old_str": "BETA", "new_str": "DELTA"})
body = fake.updated[0][1]["paragraph"]["rich_text"] if fake.updated else []
joined = "".join(t.get("text", {}).get("content", "") for t in body)
check("substring replace preserves surrounding", err is None and joined == "alpha DELTA gamma")

# preserves an inline bold token outside the matched span
bolded = {"id": "bb", "type": "paragraph", "has_children": False, "_children": [],
          "paragraph": {"rich_text": rt("pre ") + rt("MID", bold=True) + rt(" post")}}
res, fake, err = run([bolded], {"old_str": "post", "new_str": "tail"})
body = fake.updated[0][1]["paragraph"]["rich_text"] if fake.updated else []
has_bold_mid = any(t.get("annotations", {}).get("bold") and t["text"]["content"] == "MID" for t in body)
plain = "".join(t["text"]["content"] for t in body)
check("replace preserves inline bold token", err is None and has_bold_mid and plain == "pre MID tail")

# cross-block delete requires opt-in
two = [b("l1", "paragraph", "Line A"), b("l2", "paragraph", "Line B")]
res, fake, err = run(list(two), {"old_str": "Line A\nLine B", "new_str": ""})
check("cross-block delete -> 400 without opt-in", err is not None and err.status_code == 400)
check("cross-block delete -> no deletes without opt-in", fake.deleted == [])

res, fake, err = run(list(two), {"old_str": "Line A\nLine B", "new_str": "", "allow_cross_block": True})
check("cross-block delete with opt-in deletes both", err is None and set(fake.deleted) == {"l1", "l2"})

res, fake, err = run(list(two), {"old_str": "Line A\nLine B", "new_str": "X", "allow_cross_block": True})
check("cross-block replace rejected", err is not None and err.status_code == 400 and not fake.updated)


# ---------------------------------------------------------------------------
# 4. Child-page deletion guard (deleting a block that CONTAINS a child page —
#    you target child pages themselves by id with notion_delete_blocks, not text)
# ---------------------------------------------------------------------------
cp = {"id": "cp", "type": "child_page", "has_children": False, "_children": [],
      "child_page": {"title": "Important Sub-Page"}}
wrap = b("wrapcp", "toggle", "Archive Section", children=[cp])
res, fake, err = run([wrap], {"old_str": "Archive Section", "new_str": ""})
check("delete block containing child_page -> 400 guarded", err is not None and err.status_code == 400)
check("child guard names the page", err is not None and "Important Sub-Page" in err.message)
check("child guard -> no delete", fake.deleted == [])

res, fake, err = run([wrap], {"old_str": "Archive Section", "new_str": ""}, allow_deleting=True)
check("delete with opt-in proceeds", err is None and "wrapcp" in fake.deleted)

# nested child database, two levels down, still guarded
nested_cp = {"id": "ncp", "type": "child_database", "has_children": False, "_children": [],
             "child_database": {"title": "Nested DB"}}
inner = b("inner", "toggle", "Inner", children=[nested_cp])
tg2 = b("tg2", "toggle", "Wrapper", children=[inner])
res, fake, err = run([tg2], {"old_str": "Wrapper", "new_str": ""})
check("nested child_database guarded", err is not None and err.status_code == 400 and "Nested DB" in err.message)


# ---------------------------------------------------------------------------
# 5. Op shapes preserved + edge cases
# ---------------------------------------------------------------------------
res, fake, err = run([b("i1", "paragraph", "Intro")],
                     {"old_str": "Intro", "new_str": "Intro\nMore text"})
check("append remainder after anchor", err is None and fake.appended and fake.appended[0]["after"] == "i1")
check("append did not delete/update", fake.deleted == [] and fake.updated == [])

# append must respect the multi-match guard too
res, fake, err = run([b("a1", "paragraph", "Same"), b("a2", "paragraph", "Same")],
                     {"old_str": "Same", "new_str": "Same plus"})
check("append unambiguous-only (409)", err is not None and err.status_code == 409 and not fake.appended)

res, fake, err = run([b("x", "paragraph", "hi")], {"old_str": "   ", "new_str": "y"})
check("whitespace old_str -> 400", err is not None and err.status_code == 400)

res, fake, err = run([b("x", "paragraph", "hi")], {"old_str": "absent", "new_str": "y"})
check("old_str not found -> 404", err is not None and err.status_code == 404)

# old_str longer than any block, not a whole run -> not found (no partial over-delete)
res, fake, err = run([b("x", "paragraph", "short")],
                     {"old_str": "short tail that is much much longer than the block", "new_str": ""})
check("over-long old_str -> 404 (no over-delete)", err is not None and err.status_code == 404 and fake.deleted == [])

# whole-block delete (single, unambiguous) removes the block
res, fake, err = run([b("w1", "paragraph", "delete me"), b("w2", "paragraph", "keep me")],
                     {"old_str": "delete me", "new_str": ""})
check("whole-block delete removes block", err is None and fake.deleted == ["w1"])

# to_do checked is not clobbered by an in-place replace
todo = {"id": "td", "type": "to_do", "has_children": False, "_children": [],
        "to_do": {"rich_text": rt("task"), "checked": True}}
res, fake, err = run([todo], {"old_str": "task", "new_str": "done"})
body = fake.updated[0][1] if fake.updated else {}
check("in-place replace does not reset to_do.checked",
      err is None and "checked" not in body.get("to_do", {}))


# ---------------------------------------------------------------------------
print("=== RESULTS ===")
ok = True
for nm, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {nm}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
