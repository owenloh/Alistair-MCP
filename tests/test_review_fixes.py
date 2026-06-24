"""Regression tests for the 16 adversarial-review findings on the write parser."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.notion import (
    markdown_to_blocks, render_page, rich_text_md, _inline_to_rich, block_to_markdown,
)

R = []
def check(name, cond):
    R.append((name, bool(cond)))

# --- round-trip adapter (simulate Notion read: write-shape -> read-shape) ---
_c = [0]
def _fill_pt(o):
    if isinstance(o, list):
        for x in o: _fill_pt(x)
    elif isinstance(o, dict):
        if o.get("type") == "text" and isinstance(o.get("text"), dict):
            o["plain_text"] = o["text"].get("content", "")
        elif o.get("type") == "equation":
            o["plain_text"] = (o.get("equation") or {}).get("expression", "")
        elif o.get("type") == "mention":
            o.setdefault("plain_text", o.get("plain_text", ""))
        for v in o.values(): _fill_pt(v)
def _prep(blocks, tree):
    out = []
    for b in blocks:
        _c[0] += 1; bid = f"id{_c[0]}"; t = b["type"]
        data = dict(b.get(t, {})); kids = data.pop("children", None)
        out.append({"id": bid, "type": t, "has_children": bool(kids), t: data})
        if kids: tree[bid] = _prep(kids, tree)
    return out
class FakeClient:
    def __init__(self, tree): self.tree = tree
    def block_children_all(self, b): return self.tree.get(b, [])
    def block_children(self, b, page_size=100, start_cursor=None):
        return {"results": self.tree.get(b, []), "has_more": False, "next_cursor": None}
def render(blocks):
    _fill_pt(blocks); tree = {}; _c[0] = 0; tree["ROOT"] = _prep(blocks, tree)
    md, _f, _n = render_page(FakeClient(tree), "ROOT", None, 100, {"n": 0}); return md

def has_nul(obj):
    if isinstance(obj, str): return "\x00" in obj
    if isinstance(obj, list): return any(has_nul(x) for x in obj)
    if isinstance(obj, dict): return any(has_nul(v) for v in obj.values())
    return False

# === H1: nested span — no NUL leak, correct nested annotations ===
sp = _inline_to_rich('<span color="red">a <span underline="true">b</span> c</span>')
check("H1 no NUL leak", not has_nul(sp))
check("H1 three tokens", len(sp) == 3)
check("H1 all red", all(t.get("annotations", {}).get("color") == "red" for t in sp))
check("H1 inner underline", sp[1]["annotations"].get("underline") is True)
check("H1 contents", "".join(t["text"]["content"] for t in sp) == "a b c")

# === H2: stacked annotations round-trip ===
def ann_of(md):
    toks = _inline_to_rich(md)
    return toks[0].get("annotations", {}) if toks else {}
check("H2 bold+italic ***", ann_of("***hi***").get("bold") and ann_of("***hi***").get("italic"))
check("H2 bold+italic single token", len(_inline_to_rich("***hi***")) == 1)
check("H2 bold+italic content", _inline_to_rich("***hi***")[0]["text"]["content"] == "hi")
check("H2 italic+strike", ann_of("~~*hi*~~").get("italic") and ann_of("~~*hi*~~").get("strikethrough"))
check("H2 bold+strike", ann_of("~~**hi**~~").get("bold") and ann_of("~~**hi**~~").get("strikethrough"))
check("H2 all three", all(ann_of("~~***hi***~~").get(k) for k in ("bold", "italic", "strikethrough")))
# read->write round trip of bold+italic
bi = [{"type": "text", "plain_text": "hi", "text": {"content": "hi"}, "annotations": {"bold": True, "italic": True}}]
check("H2 read emits ***", rich_text_md(bi) == "***hi***")
back = _inline_to_rich(rich_text_md(bi))
check("H2 roundtrip bold+italic", back[0]["annotations"].get("bold") and back[0]["annotations"].get("italic") and back[0]["text"]["content"] == "hi")
# formatting inside link now works
lk = _inline_to_rich("[**Kelly**](https://k)")
check("H2 bold-in-link", lk[0]["annotations"].get("bold") and lk[0]["text"]["content"] == "Kelly" and lk[0]["text"]["link"]["url"] == "https://k")

# === H3: flat nested same-tag toggles ===
tg = markdown_to_blocks("<details>\n<summary>outer</summary>\n<details>\n<summary>inner</summary>\nbody\n</details>\n</details>")
check("H3 one top toggle", len(tg) == 1 and tg[0]["type"] == "toggle")
check("H3 outer summary kept", tg[0]["toggle"]["rich_text"][0]["text"]["content"] == "outer")
inner = [c for c in tg[0]["toggle"].get("children", []) if c["type"] == "toggle"]
check("H3 inner toggle nested", len(inner) == 1 and inner[0]["toggle"]["rich_text"][0]["text"]["content"] == "inner")
check("H3 no stray </details>", "</details>" not in str(tg))

# === H4: close tag indented deeper than open ===
h4 = markdown_to_blocks("<details>\n<summary>s</summary>\nbody\n\t</details>")
check("H4 single toggle", len(h4) == 1 and h4[0]["type"] == "toggle")
check("H4 no literal close", "</details>" not in str(h4))
check("H4 body child only", len(h4[0]["toggle"].get("children", [])) == 1)

# === H5: <2 columns degrades, not column_list ===
one = markdown_to_blocks("<columns>\n<column>\nhello\n</column>\n</columns>")
check("H5 one column -> no column_list", all(b["type"] != "column_list" for b in one))
check("H5 one column -> paragraph kept", any(b["type"] == "paragraph" for b in one))
zero = markdown_to_blocks("<columns>\nloose text\n</columns>")
check("H5 zero column -> no column_list", all(b["type"] != "column_list" for b in zero))
two = markdown_to_blocks("<columns>\n<column>\na\n</column>\n<column>\nb\n</column>\n</columns>")
check("H5 two columns -> column_list", two[0]["type"] == "column_list" and len(two[0]["column_list"]["children"]) == 2)

# === H6: callout icon validation ===
named = markdown_to_blocks('<callout icon="lightbulb">\nx\n</callout>')
check("H6 named icon omitted", "icon" not in named[0]["callout"])
url = markdown_to_blocks('<callout icon="https://e/i.png">\nx\n</callout>')
check("H6 url icon external", named and url[0]["callout"]["icon"]["type"] == "external")
emo = markdown_to_blocks('<callout icon="💡">\nx\n</callout>')
check("H6 emoji icon kept", emo[0]["callout"]["icon"] == {"type": "emoji", "emoji": "💡"})

# === H7: link_to_page database read -> mention-database (no 400 on rewrite) ===
db_link = {"id": "x", "type": "link_to_page", "link_to_page": {"type": "database_id", "database_id": "11112222-3333-4444-5555-666677778888"}}
md_dbl = block_to_markdown(db_link)
check("H7 db link -> mention-database", md_dbl.startswith("<mention-database"))
rewritten = markdown_to_blocks(md_dbl)
check("H7 rewrite not page_id link", all(b["type"] != "link_to_page" for b in rewritten))
pg_link = {"id": "y", "type": "link_to_page", "link_to_page": {"type": "page_id", "page_id": "11112222-3333-4444-5555-666677778888"}}
check("H7 page link still <page>", block_to_markdown(pg_link).startswith("<page"))
back_pg = markdown_to_blocks(block_to_markdown(pg_link))
check("H7 page link round-trips link_to_page", back_pg[0]["type"] == "link_to_page" and back_pg[0]["link_to_page"]["page_id"] == "11112222-3333-4444-5555-666677778888")

# === H8: paragraph text starting with a block marker round-trips as paragraph ===
for txt in ["# not a heading", "- not a bullet", "1. not numbered", "---", "___"]:
    para = {"id": "p", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "plain_text": txt, "text": {"content": txt}}]}}
    md_p = block_to_markdown(para)
    rt = markdown_to_blocks(md_p)
    ok = len(rt) == 1 and rt[0]["type"] == "paragraph" and rt[0]["paragraph"]["rich_text"][0]["text"]["content"] == txt
    check(f"H8 paragraph {txt!r} stays paragraph", ok)

# === M1: table with unclosed <td> still parses ===
t1 = markdown_to_blocks("<table>\n<tr>\n<td>x\n</tr>\n</table>")
check("M1 unclosed td -> table survives", t1 and t1[0]["type"] == "table")
check("M1 unclosed td content", t1[0]["table"]["children"][0]["table_row"]["cells"][0][0]["text"]["content"] == "x")

# === M2: toggle heading children + is_toggleable round-trip ===
th = markdown_to_blocks('# Big {toggle="true"}\n\tchild under heading')
check("M2 heading toggleable", th[0]["type"] == "heading_1" and th[0]["heading_1"].get("is_toggleable") is True)
check("M2 heading has child", len(th[0]["heading_1"].get("children", [])) == 1)
# render a toggle heading w/ children -> markdown -> back
th_block = {"id": "h", "type": "heading_2", "has_children": True,
            "heading_2": {"rich_text": [{"type": "text", "plain_text": "T", "text": {"content": "T"}}], "is_toggleable": True}}
tree = {"ROOT": [th_block], "h": [{"id": "c", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": [{"type": "text", "plain_text": "kid", "text": {"content": "kid"}}]}}]}
md_th, _f, _n = render_page(FakeClient(tree), "ROOT", None, 100, {"n": 0})
check("M2 render emits toggle attr", 'toggle="true"' in md_th)
rt_th = markdown_to_blocks(md_th)
check("M2 roundtrip toggleable", rt_th[0]["heading_2"].get("is_toggleable") is True and len(rt_th[0]["heading_2"].get("children", [])) == 1)

# === M4: date mention timezone round-trips ===
dmt = {"type": "mention", "plain_text": "", "mention": {"type": "date", "date": {"start": "2026-06-24", "time_zone": "Europe/London"}}}
md_d = rich_text_md([dmt])
check("M4 read emits timeZone", 'timeZone="Europe/London"' in md_d)
back_d = _inline_to_rich(md_d)
check("M4 timezone parsed", back_d[0]["mention"]["date"].get("time_zone") == "Europe/London")

# === L1: synced reference with bad url dropped ===
bad = markdown_to_blocks("<synced_block_reference url=\"not-an-id\">\n</synced_block_reference>")
check("L1 bad ref dropped", bad == [])
good = markdown_to_blocks('<synced_block_reference url="https://app.notion.com/p/11112222333344445555666677778888">\n</synced_block_reference>')
check("L1 good ref kept", good and good[0]["synced_block"]["synced_from"]["block_id"] == "11112222-3333-4444-5555-666677778888")

# === L4: media color round-trips ===
vid = {"id": "v", "type": "video", "video": {"type": "external", "external": {"url": "http://v/x.mp4"}, "caption": [], "color": "red_background"}}
md_v = block_to_markdown(vid)
check("L4 read emits media color", 'color="red_bg"' in md_v)
back_v = markdown_to_blocks(md_v)
check("L4 media color parsed", back_v[0]["video"].get("color") == "red_background")

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
