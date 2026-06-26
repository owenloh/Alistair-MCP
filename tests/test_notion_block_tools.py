"""Tests for the block-ID primitives: list/append/update/delete/move by id."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import notion as n

R = []
def check(name, cond):
    R.append((name, bool(cond)))


def H(tag):
    """Deterministic dashed-UUID id from a label (extract_id is idempotent on it)."""
    h = hashlib.md5(tag.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def rtok(text):
    return {"type": "text", "plain_text": text, "text": {"content": text}}


class FC:
    def __init__(self):
        self.blocks = {}
        self.kids = {}
        self.deleted = []
        self.updated = []
        self.appends = []

    def add(self, bid, btype, text="", parent=None, children=None, **data):
        d = {"rich_text": [rtok(text)]} if text else {}
        d.update(data)
        self.blocks[bid] = {"id": bid, "type": btype, "has_children": bool(children),
                            btype: d, "parent": parent or {}}
        if children is not None:
            self.kids[bid] = list(children)
        return bid

    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

    def block_children(self, bid, page_size=100, start_cursor=None):
        return {"results": [self.blocks[c] for c in self.kids.get(bid, [])],
                "has_more": False, "next_cursor": None}
    def block_children_all(self, bid):
        return [self.blocks[c] for c in self.kids.get(bid, [])]
    def retrieve_block(self, bid):
        if bid not in self.blocks:
            raise n.ServiceError("nf", status_code=404)
        return self.blocks[bid]
    def update_block(self, bid, body):
        self.updated.append((bid, body))
        return {"type": next(iter(body), None)}
    def delete_block(self, bid):
        self.deleted.append(bid)
    def append_children(self, bid, children, after=None):
        self.appends.append({"parent": bid, "after": after, "children": children})
        return {"results": [{"id": f"new{i}"} for i, _ in enumerate(children)]}


S = Settings(notion_token="x")
PID = H("page")
_orig = n.NotionClient
def patched(fake):
    n.NotionClient = lambda settings: fake


# ---------------------------------------------------------------------------
# list_blocks
# ---------------------------------------------------------------------------
f = FC()
f.kids[PID] = [H("p1"), H("tg")]
f.add(H("p1"), "paragraph", "first", parent={"page_id": PID})
f.add(H("tg"), "toggle", "a toggle", parent={"page_id": PID}, children=[H("c1")])
f.add(H("c1"), "paragraph", "nested", parent={"block_id": H("tg")})
patched(f)
out = n.op_list_blocks(S, page_id=PID, recursive=False)
ids = [b["id"] for b in out["blocks"]]
check("list direct children", ids == [H("p1"), H("tg")])
check("list reports has_children + depth", out["blocks"][1]["has_children"] is True and out["blocks"][0]["depth"] == 0)
check("list reports parent_id", out["blocks"][0]["parent_id"] == PID)

out = n.op_list_blocks(S, page_id=PID, recursive=True)
ids = [b["id"] for b in out["blocks"]]
check("list recursive walks subtree", ids == [H("p1"), H("tg"), H("c1")])
check("list recursive depth of nested", out["blocks"][2]["depth"] == 1 and out["blocks"][2]["parent_id"] == H("tg"))
n.NotionClient = _orig


# ---------------------------------------------------------------------------
# append_blocks (typed objects, native nesting)
# ---------------------------------------------------------------------------
f = FC()
patched(f)
toggle = {"type": "toggle", "toggle": {"rich_text": [rtok("T")],
          "children": [{"type": "paragraph", "paragraph": {"rich_text": [rtok("kid")]}}]}}
out = n.op_append_blocks(S, parent_id=PID, blocks=[toggle], after=H("anchor1"))
sent = f.appends[0]["children"][0]
check("append tags object:block", sent.get("object") == "block")
check("append tags nested child object:block", sent["toggle"]["children"][0].get("object") == "block")
check("append passes after", f.appends[0]["after"] == H("anchor1"))
check("append returns block_ids", out["block_ids"] == ["new0"] and out["added_blocks"] == 1)
n.NotionClient = _orig


# ---------------------------------------------------------------------------
# update_block (extract type payload)
# ---------------------------------------------------------------------------
f = FC()
patched(f)
n.op_update_block(S, block_id=H("b1"), block={"paragraph": {"rich_text": [rtok("new")]}})
check("update_block passes type payload", f.updated[0][1] == {"paragraph": {"rich_text": [rtok("new")]}})
n.op_update_block(S, block_id=H("b2"), block={"type": "to_do", "to_do": {"checked": True}})
check("update_block strips type wrapper", f.updated[1][1] == {"to_do": {"checked": True}})
n.NotionClient = _orig


# ---------------------------------------------------------------------------
# delete_blocks (deterministic + child-page guard)
# ---------------------------------------------------------------------------
f = FC()
f.add(H("x1"), "paragraph", "a")
f.add(H("x2"), "paragraph", "b")
patched(f)
out = n.op_delete_blocks(S, block_ids=[H("x1"), H("x2")])
check("delete_blocks removes exactly the listed ids", set(f.deleted) == {H("x1"), H("x2")} and out["count"] == 2)
n.NotionClient = _orig

f = FC()
f.add(H("cp"), "child_page", parent={"page_id": PID})
f.blocks[H("cp")]["child_page"] = {"title": "Protected"}
patched(f)
err = None
try:
    n.op_delete_blocks(S, block_ids=[H("cp")])
except n.ServiceError as e:
    err = e
check("delete child_page guarded", err is not None and err.status_code == 400 and "Protected" in err.message)
check("delete child_page guard -> no delete", f.deleted == [])
out = n.op_delete_blocks(S, block_ids=[H("cp")], allow_deleting_content=True)
check("delete child_page with opt-in proceeds", f.deleted == [H("cp")])
n.NotionClient = _orig


# ---------------------------------------------------------------------------
# move_blocks (copy subtree after target, delete originals)
# ---------------------------------------------------------------------------
f = FC()
f.kids[PID] = [H("m1"), H("anchor")]
f.add(H("m1"), "paragraph", "movable", parent={"page_id": PID})
f.add(H("anchor"), "paragraph", "anchor", parent={"page_id": PID})
patched(f)
out = n.op_move_blocks(S, block_ids=[H("m1")], after_block_id=H("anchor"))
check("move appends a copy after anchor", f.appends and f.appends[0]["after"] == H("anchor"))
check("move copy carries the text",
      f.appends[0]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "movable")
check("move deletes the original", H("m1") in f.deleted)
check("move reports parent + new id", out["parent_id"] == PID and out["moved"][0]["old_id"] == H("m1"))
n.NotionClient = _orig


# ---------------------------------------------------------------------------
print("=== RESULTS ===")
ok = True
for nm, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {nm}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
