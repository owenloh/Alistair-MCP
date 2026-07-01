"""Round-trip + structural tests for the write-side container parser and colors."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.notion import (
    markdown_to_blocks, render_page, rich_text_md, _inline_to_rich,
)

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))


# ---------------------------------------------------------------------------
# In-memory render adapter: turn write-shape blocks (inline children) into
# read-shape blocks addressable by a FakeClient, then render with production code.
# ---------------------------------------------------------------------------
_counter = [0]
def _fill_pt(obj):
    """Simulate Notion read responses: add plain_text to text/equation tokens."""
    if isinstance(obj, list):
        for x in obj:
            _fill_pt(x)
    elif isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), dict):
            obj["plain_text"] = obj["text"].get("content", "")
        elif obj.get("type") == "equation":
            obj["plain_text"] = (obj.get("equation") or {}).get("expression", "")
        for v in obj.values():
            _fill_pt(v)


def _prep(blocks, tree):
    out = []
    for b in blocks:
        _counter[0] += 1
        bid = f"id{_counter[0]}"
        t = b["type"]
        data = dict(b.get(t, {}))
        children = data.pop("children", None)
        nb = {"id": bid, "type": t, "has_children": bool(children), t: data}
        if children:
            tree[bid] = _prep(children, tree)
        out.append(nb)
    return out


class FakeClient:
    def __init__(self, tree):
        self.tree = tree
    def block_children_all(self, block_id):
        return self.tree.get(block_id, [])
    def block_children(self, block_id, page_size=100, start_cursor=None):
        return {"results": self.tree.get(block_id, []), "has_more": False, "next_cursor": None}


def roundtrip(md):
    """md -> blocks -> md (via production renderer)."""
    blocks = markdown_to_blocks(md)
    _fill_pt(blocks)
    tree = {}
    _counter[0] = 0
    top = _prep(blocks, tree)
    tree["ROOT"] = top
    out, _flat, _nc = render_page(FakeClient(tree), "ROOT", None, 100, {"n": 0})
    return out, blocks


# ---------------------------------------------------------------------------
# 1. Idempotent round-trip of a representative document
# ---------------------------------------------------------------------------
DOC = "\n".join([
    "# Heading one",
    "## Heading two {color=\"red\"}",
    "Plain paragraph with **bold**, *italic*, ~~strike~~, `code` and a [link](https://x.com).",
    "Para with a \\*literal-star\\* and cost \\$5 and \\<tag\\>.",
    "A <span color=\"blue\">blue span</span> and <span underline=\"true\">underlined</span> text.",
    "Inline math $`E=mc^2`$ inside text.",
    "<empty-block/>",
    "- bullet one",
    "- bullet two {color=\"green_bg\"}",
    "1. first",
    "1. second",
    "- [ ] todo open",
    "- [x] todo done",
    "> a quote {color=\"gray\"}",
    "---",
    "![a caption](https://img/x.png)",
    "<video src=\"https://v/clip.mp4\">a clip</video>",
    "<table_of_contents/>",
])

rt, blocks = roundtrip(DOC)
check("idempotent representative doc", rt == DOC)
if rt != DOC:
    print("=== EXPECTED ===")
    print(DOC)
    print("=== GOT ===")
    print(rt)
    # line-level diff
    for a, b in zip(DOC.split("\n"), rt.split("\n")):
        if a != b:
            print(f"  DIFF\n    in : {a!r}\n    out: {b!r}")


# ---------------------------------------------------------------------------
# 2. Container structures (write parser)
# ---------------------------------------------------------------------------
# Toggle with color + children
tg = markdown_to_blocks('<details color="blue">\n<summary>Title **b**</summary>\n\tchild para\n\t- inner bullet\n</details>')
check("toggle type", tg[0]["type"] == "toggle")
check("toggle color api", tg[0]["toggle"].get("color") == "blue_background" or tg[0]["toggle"].get("color") == "blue")
check("toggle summary rich", tg[0]["toggle"]["rich_text"][0]["text"]["content"] == "Title ")
check("toggle children count", len(tg[0]["toggle"].get("children", [])) == 2)
check("toggle child0 para", tg[0]["toggle"]["children"][0]["type"] == "paragraph")
check("toggle child1 bullet", tg[0]["toggle"]["children"][1]["type"] == "bulleted_list_item")

# Callout: first line rich_text, rest children
co = markdown_to_blocks('<callout icon="💡" color="yellow_bg">\n\tThis is the callout text\n\t- a child bullet\n\tanother child para\n</callout>')
check("callout type", co[0]["type"] == "callout")
check("callout icon", (co[0]["callout"].get("icon") or {}).get("emoji") == "💡")
check("callout color", co[0]["callout"].get("color") == "yellow_background")
check("callout rich_text", co[0]["callout"]["rich_text"][0]["text"]["content"] == "This is the callout text")
check("callout children", len(co[0]["callout"].get("children", [])) == 2)
check("callout child0 bullet", co[0]["callout"]["children"][0]["type"] == "bulleted_list_item")

# Columns
cols = markdown_to_blocks('<columns>\n\t<column>\n\t\tleft para\n\t</column>\n\t<column>\n\t\tright para\n\t\t- right bullet\n\t</column>\n</columns>')
check("column_list type", cols[0]["type"] == "column_list")
check("two columns", len(cols[0]["column_list"]["children"]) == 2)
c0 = cols[0]["column_list"]["children"][0]
check("column type", c0["type"] == "column")
check("column0 has 1 child", len(c0["column"]["children"]) == 1)
check("column0 child para", c0["column"]["children"][0]["type"] == "paragraph")
c1 = cols[0]["column_list"]["children"][1]
check("column1 has 2 children", len(c1["column"]["children"]) == 2)

# Table (multi-line)
tbl = markdown_to_blocks('<table header-row="true">\n\t<tr>\n\t\t<td>A</td>\n\t\t<td>B</td>\n\t</tr>\n\t<tr>\n\t\t<td>1</td>\n\t\t<td>2</td>\n\t</tr>\n</table>')
check("table type", tbl[0]["type"] == "table")
check("table width", tbl[0]["table"]["table_width"] == 2)
check("table header-row", tbl[0]["table"]["has_column_header"] is True)
check("table 2 rows", len(tbl[0]["table"]["children"]) == 2)
check("table row0 cells", tbl[0]["table"]["children"][0]["table_row"]["cells"][0][0]["text"]["content"] == "A")

# Table with colgroup (ignored) + cell colors (ignored)
tbl2 = markdown_to_blocks('<table>\n\t<colgroup>\n\t\t<col color="gray">\n\t</colgroup>\n\t<tr>\n\t\t<td color="red">X</td>\n\t\t<td>Y</td>\n\t</tr>\n</table>')
check("table2 width", tbl2[0]["table"]["table_width"] == 2)
check("table2 cell text", tbl2[0]["table"]["children"][0]["table_row"]["cells"][0][0]["text"]["content"] == "X")

# Synced block (new -> synced_from null)
sb = markdown_to_blocks('<synced_block>\n\tsynced child\n</synced_block>')
check("synced type", sb[0]["type"] == "synced_block")
check("synced_from null", sb[0]["synced_block"]["synced_from"] is None)
check("synced children", len(sb[0]["synced_block"].get("children", [])) == 1)

# Synced reference
sr = markdown_to_blocks('<synced_block_reference url="https://app.notion.com/p/11112222333344445555666677778888">\n</synced_block_reference>')
check("synced ref synced_from", (sr[0]["synced_block"]["synced_from"] or {}).get("block_id") == "11112222-3333-4444-5555-666677778888")

# Equation block
eq = markdown_to_blocks("$$\nx^2 + y^2 = z^2\n$$")
check("equation type", eq[0]["type"] == "equation")
check("equation expr", eq[0]["equation"]["expression"] == "x^2 + y^2 = z^2")

# Image with caption + color
im = markdown_to_blocks('![my cap](https://img/y.png) {color="red"}')
check("image type", im[0]["type"] == "image")
check("image url", im[0]["image"]["external"]["url"] == "https://img/y.png")
check("image caption", im[0]["image"]["caption"][0]["text"]["content"] == "my cap")
check("image color", im[0]["image"].get("color") == "red")

# page -> link_to_page
pg = markdown_to_blocks('<page url="https://app.notion.com/p/11112222333344445555666677778888">Title</page>')
check("page -> link_to_page", pg[0]["type"] == "link_to_page")
check("link_to_page id", pg[0]["link_to_page"]["page_id"] == "11112222-3333-4444-5555-666677778888")

# nested bullet via indentation
nb = markdown_to_blocks("- parent\n\t- child\n\t\t- grandchild")
check("nested bullet parent", nb[0]["type"] == "bulleted_list_item")
check("nested bullet child", nb[0]["bulleted_list_item"]["children"][0]["type"] == "bulleted_list_item")
check("nested bullet grandchild", nb[0]["bulleted_list_item"]["children"][0]["bulleted_list_item"]["children"][0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "grandchild")


# ---------------------------------------------------------------------------
# 3. Inline round-trips (write parser internals)
# ---------------------------------------------------------------------------
# span color
sp = _inline_to_rich('<span color="red">hi</span>')
check("span color annotation", sp[0]["annotations"].get("color") == "red")
check("span color content", sp[0]["text"]["content"] == "hi")
# span underline
su = _inline_to_rich('<span underline="true">hi</span>')
check("span underline annotation", su[0]["annotations"].get("underline") is True)
# span with inner bold
sib = _inline_to_rich('<span color="blue">a **b** c</span>')
check("span inner bold count", len(sib) == 3)
check("span inner all blue", all(t["annotations"].get("color") == "blue" for t in sib))
check("span inner bold mid", sib[1]["annotations"].get("bold") is True)
# br
br = _inline_to_rich("line1<br>line2")
check("br -> newline", br[0]["text"]["content"] == "line1\nline2")
# date mention
dm = _inline_to_rich('<mention-date start="2026-06-24" end="2026-06-25"/>')
check("date mention type", dm[0]["mention"]["type"] == "date")
check("date mention start", dm[0]["mention"]["date"]["start"] == "2026-06-24")
check("date mention end", dm[0]["mention"]["date"]["end"] == "2026-06-25")
# datetime mention
dtm = _inline_to_rich('<mention-date start="2026-06-24" startTime="14:30" timeZone="Europe/London"/>')
check("datetime start combined", dtm[0]["mention"]["date"]["start"] == "2026-06-24T14:30")
check("datetime tz", dtm[0]["mention"]["date"]["time_zone"] == "Europe/London")
# user mention with url
um = _inline_to_rich('<mention-user url="https://app.notion.com/p/11112222333344445555666677778888">Ada</mention-user>')
check("user mention id", um[0]["mention"]["user"]["id"] == "11112222-3333-4444-5555-666677778888")

# rich_text_md emits span for color + underline round-trip
rtmd = rich_text_md([
    {"type": "text", "plain_text": "x", "text": {"content": "x"}, "annotations": {"color": "red"}},
    {"type": "text", "plain_text": "y", "text": {"content": "y"}, "annotations": {"underline": True}},
])
check("rich_text_md span color", '<span color="red">x</span>' in rtmd)
check("rich_text_md span underline", '<span underline="true">y</span>' in rtmd)
# round trip color span
back = _inline_to_rich(rtmd)
check("color span round-trips", back[0]["annotations"].get("color") == "red")


# ---------------------------------------------------------------------------
# 4. Toggle + nesting ROUND-TRIP (write -> render -> assert structure) and the
#    space-indentation tolerance (children must NOT flatten into loose blocks).
# ---------------------------------------------------------------------------
TOGGLE_DOC = "\n".join([
    "<details>",
    "<summary>Day 1 headline</summary>",
    "\tintro paragraph",
    "\t- a bullet",
    "\t\t- nested bullet",
    "</details>",
])
rt_toggle, tblocks = roundtrip(TOGGLE_DOC)
check("toggle round-trips to a real toggle block", tblocks[0]["type"] == "toggle")
check("toggle round-trip keeps children (not flattened)",
      len(tblocks[0]["toggle"].get("children", [])) == 2)
check("toggle round-trip markdown re-emits <details>", "<details>" in rt_toggle and "</details>" in rt_toggle)
check("toggle round-trip is idempotent", rt_toggle == TOGGLE_DOC)

# Space-indented toggle children (4-space) must still nest, not flatten.
SPACE_TOGGLE = "\n".join([
    "<details>",
    "<summary>Spaced</summary>",
    "    a child paragraph",
    "    - a child bullet",
    "</details>",
])
sb = markdown_to_blocks(SPACE_TOGGLE)
check("space-indented toggle is a toggle", sb[0]["type"] == "toggle")
check("space-indented toggle keeps its children (no flatten)",
      len(sb[0]["toggle"].get("children", [])) == 2 and len(sb) == 1)

# 2-space unit detection: grandchild at 4 spaces nests under child at 2 spaces.
TWO_SPACE = "- parent\n  - child\n    - grandchild"
ts = markdown_to_blocks(TWO_SPACE)
check("2-space nesting: parent has one child",
      ts[0]["type"] == "bulleted_list_item" and len(ts[0]["bulleted_list_item"]["children"]) == 1)
check("2-space nesting: grandchild nested two deep",
      ts[0]["bulleted_list_item"]["children"][0]["bulleted_list_item"]["children"][0]
        ["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "grandchild")


# ---------------------------------------------------------------------------
print("\n=== RESULTS ===")
ok = True
for name, p in PASS:
    print(f"  {'PASS' if p else 'FAIL'}  {name}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _,p in PASS if p)}/{len(PASS)})")
sys.exit(0 if ok else 1)
