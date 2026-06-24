import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.notion import (
    render_blocks, render_page, block_to_markdown, rich_text_md,
    _inline_to_rich, markdown_to_blocks,
)

HEXID = "11112222333344445555666677778888"
DASHID = "11112222-3333-4444-5555-666677778888"


class FakeClient:
    def __init__(self, tree):
        self.tree = tree

    def block_children_all(self, block_id):
        return self.tree.get(block_id, [])

    def block_children(self, block_id, page_size=100, start_cursor=None):
        kids = self.tree.get(block_id, [])
        return {"results": kids, "has_more": False, "next_cursor": None}


def rt(s, **ann):
    return [{"type": "text", "plain_text": s, "text": {"content": s}, "annotations": ann}]


def mention_page(label, pid):
    return [{"type": "mention", "mention": {"type": "page", "page": {"id": pid}}, "plain_text": label}]


root = [
    {"id": "h", "type": "heading_4", "heading_4": {"rich_text": rt("ipXchange")}, "has_children": False},
    {"id": "e", "type": "paragraph", "paragraph": {"rich_text": []}, "has_children": False},
    {"id": "tbl", "type": "table", "table": {"has_column_header": True}, "has_children": True},
    {"id": "men", "type": "paragraph", "paragraph": {"rich_text": rt("see ") + mention_page("My Page", HEXID)}, "has_children": False},
    {"id": "esc", "type": "paragraph", "paragraph": {"rich_text": rt("cost $5 *x* [y] <z>")}, "has_children": False},
    {"id": "bk", "type": "bookmark", "bookmark": {"url": "https://ex.com", "caption": rt("Ex")}, "has_children": False},
    {"id": "cdb", "type": "child_database", "child_database": {"title": "Tasks DB"}, "has_children": False},
    {"id": "sync", "type": "synced_block", "synced_block": {}, "has_children": True},
]
tree = {
    "root": root,
    "tbl": [
        {"id": "r1", "type": "table_row", "table_row": {"cells": [rt("A"), rt("B")]}, "has_children": False},
        {"id": "r2", "type": "table_row", "table_row": {"cells": [rt("1"), rt("2")]}, "has_children": False},
    ],
    "sync": [{"id": "s1", "type": "paragraph", "paragraph": {"rich_text": rt("synced line")}, "has_children": False}],
}

md, flat, nxt = render_page(FakeClient(tree), "root", None, 100, {"n": 0})
print("=== RENDERED MARKDOWN ===")
print(md)
print("=== next_cursor:", nxt)

checks = {
    "heading_4 -> ####": "#### ipXchange" in md,
    "empty paragraph -> <empty-block/>": "<empty-block/>" in md,
    "table open header-row=true": '<table header-row="true">' in md,
    "table rows multi-line": "\t<tr>" in md and "\t\t<td>A</td>" in md and "\t\t<td>B</td>" in md and "\t</tr>" in md,
    "table close": "</table>" in md,
    "page mention": f'<mention-page url="https://app.notion.com/p/{HEXID}">My Page</mention-page>' in md,
    "escaping $ * [ ] < >": r"cost \$5 \*x\* \[y\] \<z\>" in md,
    "bookmark -> [cap](url)": "[Ex](https://ex.com)" in md,
    "child_database -> <database>": '<database url="https://app.notion.com/p/cdb">Tasks DB</database>' in md or "<database" in md,
    "synced wrapped": "synced line" in md and "<synced_block" in md and "</synced_block>" in md,
}

# inline render: bold + link + math + strike
inline = rich_text_md(
    rt("see ") +
    [{"type": "text", "plain_text": "Kelly", "text": {"content": "Kelly", "link": {"url": "https://k"}}, "annotations": {"bold": True}}] +
    [{"type": "equation", "plain_text": "f^*", "equation": {"expression": "f^*"}}] +
    rt("gone", strikethrough=True)
)
checks["inline bold+link+math+strike"] = inline == "see [**Kelly**](https://k)$`f^*`$~~gone~~"

# round-trip: escaped text -> rich -> plain stays literal
rich = _inline_to_rich(r"cost \$5 \*x\* \[y\]")
plain = "".join(t.get("text", {}).get("content", "") for t in rich if t.get("type") == "text")
checks["escape round-trips to literal"] = plain == "cost $5 *x* [y]" and len(rich) == 1

# round-trip: math
rich_m = _inline_to_rich("val $`x^2`$ end")
checks["math round-trips to equation"] = any(t.get("type") == "equation" and t["equation"]["expression"] == "x^2" for t in rich_m)

# round-trip: page mention
rich_men = _inline_to_rich(f'<mention-page url="https://app.notion.com/p/{HEXID}">My Page</mention-page>')
checks["mention round-trips"] = any(
    t.get("type") == "mention" and t["mention"]["page"]["id"] == DASHID for t in rich_men
)

# write parser: heading_4, empty-block, image
wb = markdown_to_blocks("#### Title\n<empty-block/>\n![](https://img/x.png)")
types = [(b["type"], b.get(b["type"])) for b in wb]
checks["write #### -> heading_4"] = wb[0]["type"] == "heading_4"
checks["write <empty-block/> -> empty paragraph"] = wb[1]["type"] == "paragraph" and wb[1]["paragraph"]["rich_text"] == []
checks["write ![](url) -> image"] = wb[2]["type"] == "image" and wb[2]["image"]["external"]["url"] == "https://img/x.png"

print("\n=== CHECKS ===")
ok = True
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = ok and v
print("\ninline render:", repr(inline))
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
