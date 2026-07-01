"""Notion connector — HTTP mirror of Claude's Notion connector tools, backed by
the official Notion REST API (api.notion.com/v1) using the NOTION_TOKEN
integration secret.

Design goals:
  * One high-level `op_*` function per connector tool, so the router stays thin.
  * A small Notion-flavored-markdown <-> REST-blocks translation layer so the
    write tools (create-pages, update-page) accept/return markdown the way the
    real connector does.
  * Faithful where the public REST API allows it (search, fetch, query,
    properties, append/insert, comments, users, create/update db). The few
    operations Notion's public API does not expose (teamspaces, views, template
    apply, verification) raise a clear 501 instead of pretending.

This module also keeps `build_brief` — the authoritative filtered read that the
notion-master / daily-brief skills rely on (a port of scripts/notion_query.py).
"""
from __future__ import annotations

import copy
import re
from typing import Any

import httpx

from . import ServiceError
from ..config import Settings

API = "https://api.notion.com/v1/"
_TIMEOUT = httpx.Timeout(40.0)

# Caps so a single fetch response can't blow up. op_fetch now paginates top-level
# blocks (start_cursor/next_cursor), so the FULL page is always retrievable across
# calls — these only bound ONE response. _MAX_TOTAL_BLOCKS is a high safety net
# against a single deeply-nested top-level block exploding; depth covers real-world
# deep toggle/column nesting.
_MAX_CHILD_DEPTH = 6
_MAX_TOTAL_BLOCKS = 5000
_TRUNC = "<!-- truncated: response cap reached; re-fetch with start_cursor -->"

NEXT_PROJECT_STATUSES = {"Active", "Complete"}

_UUID_RE = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class NotionClient:
    def __init__(self, settings: Settings):
        if not settings.notion_token:
            raise ServiceError("NOTION_TOKEN is not configured.", status_code=503)
        self.settings = settings
        self._client = httpx.Client(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {settings.notion_token}",
                "Notion-Version": settings.notion_version,
                "Content-Type": "application/json",
            },
        )

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        try:
            resp = self._client.request(method, API + path, json=json_body, params=params)
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching Notion: {e}", status_code=502)
        if resp.status_code >= 400:
            raise ServiceError(
                f"Notion API returned HTTP {resp.status_code} for {method} {path}. "
                "A 401/404 usually means the token is wrong or the page/database is "
                "not shared with the integration.",
                status_code=502,
                detail=_safe_body(resp),
            )
        if not resp.content:
            return {}
        return resp.json()

    # ---- primitives ----
    def search(self, body: dict) -> dict:
        return self.request("POST", "search", json_body=body)

    def retrieve_page(self, page_id: str) -> dict:
        return self.request("GET", f"pages/{page_id}")

    def retrieve_database(self, db_id: str) -> dict:
        return self.request("GET", f"databases/{db_id}")

    def query_database(self, db_id: str, body: dict) -> dict:
        return self.request("POST", f"databases/{db_id}/query", json_body=body)

    def query_database_all(self, db_id: str, flt: dict | None = None) -> list[dict]:
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if flt:
                body["filter"] = flt
            if cursor:
                body["start_cursor"] = cursor
            data = self.query_database(db_id, body)
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return rows

    def block_children(self, block_id: str, page_size: int = 100, start_cursor: str | None = None) -> dict:
        params = {"page_size": page_size}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self.request("GET", f"blocks/{block_id}/children", params=params)

    def block_children_all(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            data = self.block_children(block_id, start_cursor=cursor)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    def append_children(self, block_id: str, children: list[dict], after: str | None = None) -> dict:
        body: dict = {"children": children}
        if after:
            body["after"] = after
        return self.request("PATCH", f"blocks/{block_id}/children", json_body=body)

    def retrieve_block(self, block_id: str) -> dict:
        return self.request("GET", f"blocks/{block_id}")

    def update_block(self, block_id: str, body: dict) -> dict:
        return self.request("PATCH", f"blocks/{block_id}", json_body=body)

    def delete_block(self, block_id: str) -> dict:
        return self.request("DELETE", f"blocks/{block_id}")

    def create_page(self, body: dict) -> dict:
        return self.request("POST", "pages", json_body=body)

    def update_page(self, page_id: str, body: dict) -> dict:
        return self.request("PATCH", f"pages/{page_id}", json_body=body)

    def create_database(self, body: dict) -> dict:
        return self.request("POST", "databases", json_body=body)

    def update_database(self, db_id: str, body: dict) -> dict:
        return self.request("PATCH", f"databases/{db_id}", json_body=body)

    def create_comment(self, body: dict) -> dict:
        return self.request("POST", "comments", json_body=body)

    def list_comments(self, block_id: str) -> dict:
        return self.request("GET", "comments", params={"block_id": block_id})

    def list_users(self, page_size: int = 100, start_cursor: str | None = None) -> dict:
        params = {"page_size": page_size}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self.request("GET", "users", params=params)

    def get_user(self, user_id: str) -> dict:
        return self.request("GET", f"users/{user_id}")


# ---------------------------------------------------------------------------
# Helpers: id parsing, property flattening
# ---------------------------------------------------------------------------
def extract_id(url_or_id: str) -> str:
    """Pull a 32-char Notion id out of a URL or id string and dash-format it."""
    if not url_or_id:
        raise ServiceError("An id or URL is required.", status_code=400)
    s = url_or_id.split("?")[0]
    matches = _UUID_RE.findall(s)
    if not matches:
        raise ServiceError(f"Could not find a Notion id in '{url_or_id}'.", status_code=400)
    raw = matches[-1].replace("-", "")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def rich_text_plain(rich: list[dict] | None) -> str:
    if not rich:
        return ""
    return "".join(t.get("plain_text", "") for t in rich)


def flatten_property(prop: dict) -> Any:
    """Flatten a Notion property value object to a readable Python value."""
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return rich_text_plain(prop.get(t))
    if t == "select":
        return (prop.get("select") or {}).get("name")
    if t == "status":
        return (prop.get("status") or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in prop.get("multi_select", [])]
    if t == "number":
        return prop.get("number")
    if t == "checkbox":
        return prop.get("checkbox")
    if t == "date":
        d = prop.get("date") or {}
        return {"start": d.get("start"), "end": d.get("end")} if d else None
    if t in ("url", "email", "phone_number"):
        return prop.get(t)
    if t == "people":
        return [p.get("name") or p.get("id") for p in prop.get("people", [])]
    if t == "relation":
        return [r.get("id") for r in prop.get("relation", [])]
    if t == "formula":
        f = prop.get("formula") or {}
        return f.get(f.get("type"))
    if t == "rollup":
        r = prop.get("rollup") or {}
        return r.get(r.get("type"))
    if t in ("created_time", "last_edited_time"):
        return prop.get(t)
    if t == "unique_id":
        u = prop.get("unique_id") or {}
        prefix = u.get("prefix")
        return f"{prefix}-{u.get('number')}" if prefix else u.get("number")
    return prop.get(t)


def flatten_properties(props: dict) -> dict:
    return {name: flatten_property(val) for name, val in props.items()}


def page_title(page: dict) -> str:
    for val in page.get("properties", {}).values():
        if val.get("type") == "title":
            return rich_text_plain(val.get("title"))
    return ""


# ---------------------------------------------------------------------------
# Markdown <-> blocks translation (pragmatic subset of Notion-flavored markdown)
# ---------------------------------------------------------------------------
_INLINE_RE = re.compile(
    r"(\*\*\*(?P<bolditalic>.+?)\*\*\*)"
    r"|(\*\*(?P<bold>.+?)\*\*)"
    r"|(~~(?P<strike>.+?)~~)"
    r"|(?<!\*)\*(?P<italic>[^*]+?)\*(?!\*)"
    r"|(`(?P<code>[^`]+?)`)"
    r"|(\[(?P<ltext>[^\]]+?)\]\((?P<lurl>[^)]+?)\))"
)
_MATH_RE = re.compile(r"\$`(?P<math>[^`]+?)`\$")
_MENTION_RE = re.compile(
    r'<mention-(?P<mkind>page|database|data-source|user|agent)'
    r'(?:\s+url="(?P<murl>[^"]*)")?\s*'
    r'(?:>(?P<mlabel>.*?)</mention-(?P=mkind)>|/>)',
    re.DOTALL,
)
# Non-greedy up to the closing /> so a '/' inside a value (e.g. timeZone="Europe/London") is kept.
_MENTION_DATE_RE = re.compile(r"<mention-date(?P<dattrs>.*?)\s*/>")
# Innermost span (its body contains no further <span); resolved repeatedly so
# nested spans collapse from the inside out.
_SPAN_RE = re.compile(
    r'<span(?P<attrs>(?:\s+[\w-]+="[^"]*")*)\s*>'
    r'(?P<body>(?:(?!<span\b)(?!</span>).)*?)</span>',
    re.DOTALL,
)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_ESC_RE = re.compile(r"\\([\\`*\[\]<>{}|$~^])")
_PLACEHOLDER_RE = re.compile(r"(\x00\d+\x00)")  # capturing group so split keeps the markers


def _inline_to_rich(text: str) -> list[dict]:
    """Parse connector-style inline markdown into a rich_text array.

    Handles **bold**, *italic*, ~~strike~~, `code`, [t](url), inline math
    $`expr`$, <span color/underline>, <br> line breaks, page/database/user/date
    mentions, and backslash escapes (\\* \\< \\` …) so a round-trip through the
    renderer is loss-free.
    """
    text = text.replace("<br>", "\n")
    stash: dict[str, dict] = {}

    def _put(obj: dict) -> str:
        key = f"\x00{len(stash)}\x00"
        stash[key] = obj
        return key

    # Stash equations, mentions and escaped chars FIRST so a span body (parsed
    # below against this same stash) can restore them.
    text = _MENTION_DATE_RE.sub(lambda m: _put(_date_mention_obj(m.group("dattrs"))), text)
    text = _MENTION_RE.sub(lambda m: _put(_mention_obj(m.group("mkind"), m.group("murl"), m.group("mlabel") or "")), text)
    text = _MATH_RE.sub(lambda m: _put({"type": "equation", "equation": {"expression": m.group("math")}}), text)
    text = _ESC_RE.sub(lambda m: _put({"_literal": m.group(1)}), text)

    # Resolve spans innermost-first, parsing each body against the SHARED stash so
    # nested spans/placeholders restore (no NUL leak) and color/underline folds in.
    while True:
        sm = _SPAN_RE.search(text)
        if not sm:
            break
        attrs = dict(_ATTR_RE.findall(sm.group("attrs")))
        tokens: list[dict] = []
        _parse_inline(sm.group("body"), {}, tokens, stash)
        color = attrs.get("color")
        underline = attrs.get("underline") == "true"
        for tok in tokens:
            _apply_span(tok, color, underline)
        text = text[:sm.start()] + _put({"_tokens": tokens}) + text[sm.end():]

    out: list[dict] = []
    _parse_inline(text, {}, out, stash)
    return out or [_text_obj("")]


def _parse_inline(text: str, ann: dict, out: list[dict], stash: dict[str, dict]) -> None:
    """Recursively tokenize bold/italic/strike/code/link, accumulating annotations.

    Recursion lets stacked markers (***bold-italic***, ~~**x**~~) and formatting
    inside [links](url) round-trip — each wrapper merges its annotation onto the
    tokens its inner content produces, instead of flattening to literal text.
    """
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            _emit_inline(out, text[pos:m.start()], stash, **ann)
        if m.group("bolditalic") is not None:
            _parse_inline(m.group("bolditalic"), {**ann, "bold": True, "italic": True}, out, stash)
        elif m.group("bold") is not None:
            _parse_inline(m.group("bold"), {**ann, "bold": True}, out, stash)
        elif m.group("strike") is not None:
            _parse_inline(m.group("strike"), {**ann, "strikethrough": True}, out, stash)
        elif m.group("italic") is not None:
            _parse_inline(m.group("italic"), {**ann, "italic": True}, out, stash)
        elif m.group("code") is not None:
            # Code content is literal — do not recurse into it.
            _emit_inline(out, m.group("code"), stash, **{**ann, "code": True})
        elif m.group("ltext") is not None:
            _parse_inline(m.group("ltext"), {**ann, "link": m.group("lurl")}, out, stash)
        pos = m.end()
    if pos < len(text):
        _emit_inline(out, text[pos:], stash, **ann)


def _emit_inline(out: list[dict], seg: str, stash: dict[str, dict], **ann) -> None:
    """Emit a text segment, restoring stashed equation/mention/span/literal placeholders.

    Literals merge into the surrounding annotated text; equations/mentions/span
    tokens become their own tokens (outer annotations are merged onto spans).
    """
    if not seg:
        return
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(_text_obj(buf, **ann))
            buf = ""

    for part in _PLACEHOLDER_RE.split(seg):
        obj = stash.get(part)
        if obj is None:
            buf += part
        elif "_literal" in obj:
            buf += obj["_literal"]
        elif "_tokens" in obj:
            flush()
            for tok in obj["_tokens"]:
                if ann:
                    _merge_ann(tok, ann)
                out.append(tok)
        else:
            flush()
            out.append(obj)
    flush()


def _merge_ann(tok: dict, ann: dict) -> None:
    """Fold an outer annotation (bold/italic/link/…) onto an inner span token."""
    if tok.get("type") not in ("text", None):
        return
    a = tok.setdefault("annotations", {})
    for k, v in ann.items():
        if not v:
            continue
        if k == "link":
            tok.setdefault("text", {})["link"] = {"url": v}
        else:
            a[k] = v


def _apply_span(tok: dict, color: str | None, underline: bool) -> None:
    """Apply a <span>'s color/underline to one inner token (text only)."""
    if tok.get("type") not in ("text", None):
        return
    a = tok.setdefault("annotations", {})
    if underline:
        a["underline"] = True
    if color and color != "default":
        a["color"] = _api_color(color)


def _mention_obj(kind: str, url: str | None, label: str) -> dict:
    """Build a Notion mention rich_text object, falling back to plain text."""
    rid = _id_from_url(url) if url else None
    if kind == "page" and rid:
        return {"type": "mention", "mention": {"type": "page", "page": {"id": rid}}}
    if kind == "database" and rid:
        return {"type": "mention", "mention": {"type": "database", "database": {"id": rid}}}
    if kind == "user" and rid:
        return {"type": "mention", "mention": {"type": "user", "user": {"id": rid}}}
    # data-source / agent mentions have no REST rich_text form; keep the label.
    return _text_obj(label)


def _date_mention_obj(attrs_str: str) -> dict:
    """Build a date mention from <mention-date start=.. startTime=.. end=.. timeZone=../>."""
    attrs = dict(_ATTR_RE.findall(attrs_str))
    start = attrs.get("start")
    if not start:
        return _text_obj("")
    if attrs.get("startTime"):
        start = f'{start}T{attrs["startTime"]}'
    d: dict = {"start": start}
    end = attrs.get("end")
    if end:
        if attrs.get("endTime"):
            end = f'{end}T{attrs["endTime"]}'
        d["end"] = end
    if attrs.get("timeZone"):
        d["time_zone"] = attrs["timeZone"]
    return {"type": "mention", "mention": {"type": "date", "date": d}}


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return extract_id(url)
    except ServiceError:
        return None


def _text_obj(content: str, bold=False, italic=False, code=False, strikethrough=False, link: str | None = None) -> dict:
    ann = {}
    if bold:
        ann["bold"] = True
    if italic:
        ann["italic"] = True
    if code:
        ann["code"] = True
    if strikethrough:
        ann["strikethrough"] = True
    obj: dict = {"type": "text", "text": {"content": content}}
    if link:
        obj["text"]["link"] = {"url": link}
    if ann:
        obj["annotations"] = ann
    return obj


# Block types that may carry indented child blocks (others ignore indentation).
# Headings included so toggle-heading children round-trip (is_toggleable set below).
_CHILD_OK = {
    "paragraph", "bulleted_list_item", "numbered_list_item", "to_do", "quote",
    "callout", "toggle", "heading_1", "heading_2", "heading_3", "heading_4",
}
# Lines that begin a block (used to tell a callout's leading rich_text from a child block).
_BLOCK_START_RE = re.compile(
    r"^(#{1,4}\s|[-*] |> |\d+\. |```|---$|\*\*\*$|___$|\$\$$"
    r"|<empty-block/>|<callout|<details|<columns|<column>|<table[ >]|<synced_block"
    r"|<page[ >]|<video |<audio |<file |<pdf |<table_of_contents|!\[)"
)
# Trailing {attr="v" ...} block attribute list (e.g. {color="red"} or
# {toggle="true" color="blue"}). The (?<!\\) keeps escaped \{ literal braces out.
_BLOCK_ATTR_RE = re.compile(r'(?<!\\)\s*\{([^{}]*)\}\s*$')
# Lines whose first rendered char is a block marker (so paragraph/callout text that
# begins this way must be lead-escaped, else it re-parses as a different block).
_LEAD_MARKER_RE = re.compile(r"^(#{1,6} |- |\d+[.)] |---$|___$)")


def markdown_to_blocks(md: str) -> list[dict]:
    """Convert Notion-flavored markdown into REST block objects.

    Recursive: container blocks (<details>, <callout>, <columns>/<column>,
    <table>, <synced_block>) wrap tab-indented children, and any text/list/quote
    block may carry indented child blocks. Mirrors render_blocks so writes
    round-trip what reads produce.

    Leading indentation is normalized to tabs first (`_normalize_indent`) so a
    toggle whose children the model indented with spaces still nests correctly
    instead of silently flattening into loose top-level blocks.
    """
    return _parse_blocks(_normalize_indent(md).split("\n"))


def _normalize_indent(md: str) -> str:
    """Convert leading-whitespace indentation to tabs (one tab per nesting level).

    The renderer emits one tab per level, so tab-indented input is unchanged. For
    space-indented input the indent unit is auto-detected as the smallest positive
    space indent in the document (handles both 2- and 4-space conventions). Lines
    inside ``` fences are left exactly as written.
    """
    raw = md.split("\n")
    # Detect the space-indent unit from space-indented, non-fenced lines.
    space_indents: list[int] = []
    in_fence = False
    for line in raw:
        if line.lstrip(" \t").startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line[:1] != " ":
            continue
        cnt = len(line) - len(line.lstrip(" "))
        if cnt:
            space_indents.append(cnt)
    unit = min(space_indents) if space_indents else 4

    out: list[str] = []
    in_fence = False
    for line in raw:
        if line.lstrip(" \t").startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        tabs = spaces = j = 0
        while j < len(line) and line[j] in ("\t", " "):
            if line[j] == "\t":
                tabs += 1
            else:
                spaces += 1
            j += 1
        level = tabs + (spaces // unit if unit else 0)
        out.append("\t" * level + line[j:])
    return "\n".join(out)


def _parse_blocks(lines: list[str]) -> list[dict]:
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        block, i = _parse_one(lines, i)
        if isinstance(block, list):
            blocks.extend(block)
        elif block is not None:
            blocks.append(block)
    return blocks


def _parse_one(lines: list[str], i: int) -> tuple[dict | None, int]:
    line = lines[i]
    stripped = line.strip()
    base = _indent_level(line)

    # ---- multi-line constructs ----
    if stripped.startswith("```"):
        lang = stripped[3:].strip() or "plain text"
        code_lines: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].strip().startswith("```"):
            code_lines.append(lines[i])
            i += 1
        return (_blk("code", {"rich_text": [_text_obj("\n".join(code_lines))], "language": lang}), i + 1)

    if stripped == "$$":
        expr: list[str] = []
        i += 1
        while i < len(lines) and lines[i].strip() != "$$":
            expr.append(lines[i].strip())
            i += 1
        return (_blk("equation", {"expression": "\n".join(expr)}), i + 1)

    m = re.match(r'^<details(?:\s+color="([^"]*)")?\s*>$', stripped)
    if m:
        j = _find_close(lines, i, "</details>")
        return (_parse_toggle(lines[i + 1:j], base, m.group(1)), j + 1)

    m = re.match(r'^<callout((?:\s+[\w-]+="[^"]*")*)\s*>$', stripped)
    if m:
        j = _find_close(lines, i, "</callout>")
        return (_parse_callout(lines[i + 1:j], m.group(1)), j + 1)

    if stripped == "<columns>":
        j = _find_close(lines, i, "</columns>")
        return (_parse_columns(lines[i + 1:j]), j + 1)

    m = re.match(r'^<table((?:\s+[\w-]+="[^"]*")*)\s*>$', stripped)
    if m:
        j = _find_close(lines, i, "</table>")
        return (_parse_table(lines[i + 1:j], m.group(1)), j + 1)

    m = re.match(r'^<synced_block(?:\s+url="([^"]*)")?\s*>$', stripped)
    if m:
        j = _find_close(lines, i, "</synced_block>")
        kids = _parse_blocks(_dedent_to_zero(lines[i + 1:j]))
        d: dict = {"synced_from": None}
        if kids:
            d["children"] = kids
        return (_blk("synced_block", d), j + 1)

    m = re.match(r'^<synced_block_reference\s+url="([^"]*)"[^>]*>$', stripped)
    if m:
        j = _find_close(lines, i, "</synced_block_reference>")
        rid = _id_from_url(m.group(1))
        # Without a resolvable id this is not a real reference; drop it rather than
        # emit synced_from:null (which would create an unintended NEW original).
        if not rid:
            return (None, j + 1)
        return (_blk("synced_block", {"synced_from": {"block_id": rid}}), j + 1)

    # ---- leaf line (+ generic indented children) ----
    block = _leaf_block(stripped)
    i += 1
    if block is None:
        return (None, i)
    if block["type"] in _CHILD_OK:
        child_lines: list[str] = []
        while i < len(lines) and (not lines[i].strip() or _indent_level(lines[i]) > base):
            child_lines.append(lines[i])
            i += 1
        kids = _parse_blocks(_dedent_to_zero(child_lines))
        if kids:
            block[block["type"]]["children"] = kids
            # Notion only allows children on a heading when it is toggleable.
            if block["type"].startswith("heading_"):
                block[block["type"]]["is_toggleable"] = True
    return (block, i)


def _leaf_block(s: str) -> dict | None:
    """Parse one non-container line into a block (block attrs honored)."""
    s, attrs = _strip_block_attrs(s)
    color = _api_color(attrs.get("color"))

    def col(d: dict) -> dict:
        if color:
            d["color"] = color
        return d

    # A leading block marker that was escaped on render is literal paragraph text.
    if s[:1] == "\\" and _LEAD_MARKER_RE.match(s[1:]):
        return _blk("paragraph", col({"rich_text": _inline_to_rich(s[1:])}))

    if s == "<empty-block/>":
        return _blk("paragraph", {"rich_text": []})
    if re.match(r"^<table_of_contents\b", s):
        return _blk("table_of_contents", col({}))

    m = re.match(r"^!\[([^\]]*)\]\(([^)]*)\)$", s)
    if m:
        url = m.group(2)
        if not url:
            return _blk("paragraph", {"rich_text": []})
        img: dict = {"type": "external", "external": {"url": url}}
        if m.group(1):
            img["caption"] = _inline_to_rich(m.group(1))
        return _blk("image", col(img))

    m = re.match(r'^<(video|audio|file|pdf)\s+src="([^"]*)"([^>]*)>(.*)</\1>$', s, re.DOTALL)
    if m:
        kind, url, rest, cap = m.group(1), m.group(2), m.group(3), m.group(4)
        data: dict = {"type": "external", "external": {"url": url}}
        if cap:
            data["caption"] = _inline_to_rich(cap)
        mattrs = dict(_ATTR_RE.findall(rest))
        if mattrs.get("color") and mattrs["color"] != "default":
            data["color"] = _api_color(mattrs["color"])
        return _blk(kind, data)

    m = re.match(r'^<page\s+url="([^"]*)"[^>]*>(.*)</page>$', s, re.DOTALL)
    if m:
        rid = _id_from_url(m.group(1))
        if rid:
            return _blk("link_to_page", {"type": "page_id", "page_id": rid})
        # New child page (no URL) can't be created via a block append — keep the title text.
        return _blk("paragraph", col({"rich_text": _inline_to_rich(m.group(2))}))

    if s in ("---", "***", "___"):
        return _blk("divider", {})
    toggle = attrs.get("toggle") == "true"
    if s.startswith("#### "):
        return _heading(4, s[5:], color, toggle)
    if s.startswith("### "):
        return _heading(3, s[4:], color, toggle)
    if s.startswith("## "):
        return _heading(2, s[3:], color, toggle)
    if s.startswith("# "):
        return _heading(1, s[2:], color, toggle)
    if re.match(r"^[-*] \[[ xX]\] ", s):
        checked = s[3] in ("x", "X")
        return _blk("to_do", col({"rich_text": _inline_to_rich(s[6:]), "checked": checked}))
    if s.startswith(("- ", "* ")):
        return _blk("bulleted_list_item", col({"rich_text": _inline_to_rich(s[2:])}))
    if re.match(r"^\d+\. ", s):
        return _blk("numbered_list_item", col({"rich_text": _inline_to_rich(re.sub(r"^\d+\. ", "", s))}))
    if s.startswith("> "):
        return _blk("quote", col({"rich_text": _inline_to_rich(s[2:])}))
    return _blk("paragraph", col({"rich_text": _inline_to_rich(s)}))


def _parse_toggle(body: list[str], base: int, color: str | None) -> dict:
    rt: list[dict] = []
    child_lines: list[str] = []
    seen_summary = False
    for ln in body:
        s = ln.strip()
        # Only the FIRST base-indent <summary> is this toggle's title; any later
        # <summary> belongs to a (possibly flat-nested) child toggle.
        if not seen_summary and _indent_level(ln) == base and s.startswith("<summary>") and s.endswith("</summary>"):
            rt = _inline_to_rich(s[len("<summary>"):-len("</summary>")])
            seen_summary = True
        else:
            child_lines.append(ln)
    d: dict = {"rich_text": rt}
    if color:
        d["color"] = _api_color(color)
    kids = _parse_blocks(_dedent_to_zero(child_lines))
    if kids:
        d["children"] = kids
    return _blk("toggle", d)


def _parse_callout(body: list[str], attr_str: str) -> dict:
    """Parse a <callout>. Heuristic: the first indented line, if it is plain inline
    text (not a block marker), is the callout's rich_text; the rest are children.
    A callout with empty rich_text whose first child is a plain paragraph is the one
    ambiguous case — it round-trips as rich_text + remaining children."""
    attrs = dict(_ATTR_RE.findall(attr_str))
    body = _dedent_to_zero(body)
    rt: list[dict] = []
    idx = next((k for k, ln in enumerate(body) if ln.strip()), None)
    if idx is not None and _is_inline_line(body[idx].strip()):
        rt = _inline_to_rich(body[idx].strip())
        body = body[idx + 1:]
    d: dict = {"rich_text": rt}
    icon = _icon_obj(attrs.get("icon"))
    if icon:
        d["icon"] = icon
    if attrs.get("color") and attrs["color"] != "default":
        d["color"] = _api_color(attrs["color"])
    kids = _parse_blocks(body)
    if kids:
        d["children"] = kids
    return _blk("callout", d)


def _icon_obj(s: str | None) -> dict | None:
    """Map an icon string to a Notion icon object, or None when unmappable.

    http(s) URL -> external icon; a single emoji char -> emoji icon; a bare word
    (e.g. a Notion built-in icon NAME) can't be expressed via REST, so omit it
    rather than send an invalid emoji (which Notion 400s)."""
    if not s:
        return None
    if s.startswith("http"):
        return {"type": "external", "external": {"url": s}}
    if not re.fullmatch(r"[\w-]+", s):  # emoji chars aren't \w, named icons are
        return {"type": "emoji", "emoji": s}
    return None


def _parse_columns(body: list[str]) -> dict | list[dict] | None:
    body = _dedent_to_zero(body)
    columns: list[dict] = []
    k = 0
    while k < len(body):
        if body[k].strip() == "<column>":
            cj = _find_close(body, k, "</column>")
            kids = _parse_blocks(_dedent_to_zero(body[k + 1:cj]))
            columns.append(_blk("column", {"children": kids or [_blk("paragraph", {"rich_text": []})]}))
            k = cj + 1
        else:
            k += 1
    # Notion rejects a column_list with <2 columns; degrade to the inner blocks
    # rather than POSTing an invalid payload (HTTP 400).
    if len(columns) < 2:
        flat: list[dict] = []
        for c in columns:
            flat.extend(c["column"]["children"])
        return flat or None
    return _blk("column_list", {"children": columns})


def _parse_table(body: list[str], attr_str: str) -> dict | None:
    attrs = dict(_ATTR_RE.findall(attr_str))
    rows: list[list[list[dict]]] = []
    k = 0
    while k < len(body):
        s = body[k].strip()
        if s == "<tr>" or s.startswith("<tr "):
            cells: list[list[dict]] = []
            k += 1
            while k < len(body) and body[k].strip() != "</tr>":
                # Tolerate an unclosed <td> (the closing tag is optional) so a single
                # malformed cell doesn't zero out table_width and drop the whole table.
                cm = re.match(r'^<td(?:\s+color="[^"]*")?>(.*?)(?:</td>)?$', body[k].strip(), re.DOTALL)
                if cm:
                    cells.append(_inline_to_rich(cm.group(1)) if cm.group(1) else [])
                k += 1
            rows.append(cells)
        k += 1
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return None
    children = [
        {"object": "block", "type": "table_row",
         "table_row": {"cells": r + [[] for _ in range(width - len(r))]}}
        for r in rows
    ]
    return _blk("table", {
        "table_width": width,
        "has_column_header": attrs.get("header-row") == "true",
        "has_row_header": attrs.get("header-column") == "true",
        "children": children,
    })


def _blk(t: str, data: dict) -> dict:
    return {"object": "block", "type": t, t: data}


def _heading(level: int, text: str, color: str | None = None, toggle: bool = False) -> dict:
    key = f"heading_{level}"
    data: dict = {"rich_text": _inline_to_rich(text)}
    if color:
        data["color"] = color
    if toggle:
        data["is_toggleable"] = True
    return {"object": "block", "type": key, key: data}


def _is_inline_line(s: str) -> bool:
    """True if a line is plain inline rich text rather than the start of a block."""
    return not _BLOCK_START_RE.match(s)


def _indent_level(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        else:
            break
    return n


def _dedent_to_zero(lines: list[str]) -> list[str]:
    """Strip the common leading-tab indentation so children parse at level 0."""
    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank:
        return lines
    n = min(_indent_level(ln) for ln in nonblank)
    return [ln[n:] if ln.strip() else "" for ln in lines]


def _find_close(lines: list[str], open_idx: int, close_tag: str) -> int:
    """Index of the matching close tag, accounting for nested same-tag containers.

    Depth-aware (so `<details>` inside `<details>` matches the right `</details>`)
    and indentation-agnostic (a close tag indented differently still matches). The
    open tag name is derived from close_tag (`</details>` -> `details`); only an
    EXACT tag-name open (`<details` followed by space or `>`) increments depth, so
    `<synced_block_reference>` does not count as a `<synced_block>` open.
    """
    name = close_tag[2:-1]
    open_re = re.compile(rf"^<{re.escape(name)}(?=[\s>])")
    depth = 1
    for j in range(open_idx + 1, len(lines)):
        s = lines[j].strip()
        if s == close_tag:
            depth -= 1
            if depth == 0:
                return j
        elif open_re.match(s):
            depth += 1
    return len(lines)


def _strip_block_attrs(s: str) -> tuple[str, dict]:
    """Pull a trailing {attr="v" ...} list off a line; return (text, attrs).

    Only strips when the braces actually contain attr="value" pairs, so plain
    prose ending in {something} is left intact."""
    m = _BLOCK_ATTR_RE.search(s)
    if m:
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        if attrs:
            return s[:m.start()].rstrip(), attrs
    return s, {}


def _lead_escape(text: str) -> str:
    """Backslash-escape a leading block marker so paragraph/callout text starting
    with '#', '- ', 'N. ', '---', '___' round-trips as text, not a new block."""
    return "\\" + text if _LEAD_MARKER_RE.match(text) else text


def _api_color(c: str | None) -> str | None:
    """Spec color (blue_bg) -> Notion API color (blue_background)."""
    if not c:
        return None
    return c.replace("_bg", "_background")


def _md_color(c: str) -> str:
    """Notion API color (blue_background) -> spec color (blue_bg)."""
    return c.replace("_background", "_bg")


def rich_text_md(rich: list[dict] | None) -> str:
    """Render a rich_text array to Notion-flavored inline markdown.

    Mirrors the connector: **bold**, *italic*, ~~strike~~, `code`, [text](url),
    inline equations as $`expr`$, and page/database/user mentions. Literal text is
    escaped (\\* \\< \\` …) so it round-trips through _inline_to_rich.
    """
    if not rich:
        return ""
    out: list[str] = []
    for t in rich:
        ttype = t.get("type")
        if ttype == "equation":
            expr = (t.get("equation") or {}).get("expression") or t.get("plain_text", "")
            out.append(f"$`{expr}`$")
            continue
        if ttype == "mention":
            out.append(_mention_md(t))
            continue
        raw = t.get("plain_text", "")
        if raw == "":
            continue
        ann = t.get("annotations") or {}
        if ann.get("code"):
            # Code spans are literal — wrap raw, don't markdown-escape inside.
            content = "`" + raw.replace("\n", "<br>") + "`"
        else:
            content = _escape_md(raw).replace("\n", "<br>")
            if ann.get("bold"):
                content = f"**{content}**"
            if ann.get("italic"):
                content = f"*{content}*"
            if ann.get("strikethrough"):
                content = f"~~{content}~~"
        text_obj = t.get("text") if isinstance(t.get("text"), dict) else {}
        link = (text_obj or {}).get("link")
        href = link.get("url") if isinstance(link, dict) else None
        if href:
            content = f"[{content}]({href})"
        # Underline + non-default color have no markdown form; wrap in <span>.
        span_attrs = ""
        if ann.get("underline"):
            span_attrs += ' underline="true"'
        color = ann.get("color")
        if color and color != "default":
            span_attrs += f' color="{_md_color(color)}"'
        if span_attrs:
            content = f"<span{span_attrs}>{content}</span>"
        out.append(content)
    return "".join(out)


# Exact escape set from the connector's enhanced-markdown spec: \ * ~ ` $ [ ] < > { } | ^
_MD_ESCAPE_RE = re.compile(r"([\\`*\[\]<>{}|$~^])")


def _escape_md(text: str) -> str:
    """Escape connector-significant characters in literal text for loss-free round-trip."""
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def _notion_url(nid: str | None) -> str:
    # Match the connector's canonical link form: https://app.notion.com/p/<id>.
    raw = (nid or "").replace("-", "")
    return f"https://app.notion.com/p/{raw}" if raw else ""


def _mention_md(token: dict) -> str:
    """Render a mention rich_text token to the connector's <mention-*> syntax."""
    m = token.get("mention") or {}
    mtype = m.get("type")
    label = token.get("plain_text", "")
    if mtype == "page":
        return f'<mention-page url="{_notion_url((m.get("page") or {}).get("id"))}">{label}</mention-page>'
    if mtype == "database":
        return f'<mention-database url="{_notion_url((m.get("database") or {}).get("id"))}">{label}</mention-database>'
    if mtype == "user":
        uid = (m.get("user") or {}).get("id")
        url = _notion_url(uid)
        return f'<mention-user url="{url}">{label}</mention-user>' if url else f"<mention-user>{label}</mention-user>"
    if mtype == "date":
        d = m.get("date") or {}
        attrs = f' start="{d["start"]}"' if d.get("start") else ""
        if d.get("end"):
            attrs += f' end="{d["end"]}"'
        if d.get("time_zone"):
            attrs += f' timeZone="{d["time_zone"]}"'
        return f"<mention-date{attrs}/>"
    return label


def _file_url(data: dict) -> str:
    for k in ("external", "file"):
        u = (data.get(k) or {}).get("url")
        if u:
            return u
    return ""


def _block_url(block: dict) -> str:
    return _notion_url(block.get("id"))


def _callout_open(data: dict) -> str:
    attrs = ""
    icon = data.get("icon") or {}
    if icon.get("type") == "emoji" and icon.get("emoji"):
        attrs += f' icon="{icon["emoji"]}"'
    color = data.get("color")
    if color and color != "default":
        attrs += f' color="{color.replace("_background", "_bg")}"'
    return f"<callout{attrs}>"


def _color_suffix(data: dict) -> str:
    """The connector's ` {color="X"}` trailing block-color attribute, or ''."""
    c = data.get("color")
    return f' {{color="{_md_color(c)}"}}' if c and c != "default" else ""


def _heading_suffix(data: dict) -> str:
    """Combined ` {toggle="true" color="X"}` attribute list for a heading line."""
    attrs = []
    if data.get("is_toggleable"):
        attrs.append('toggle="true"')
    c = data.get("color")
    if c and c != "default":
        attrs.append(f'color="{_md_color(c)}"')
    return f' {{{" ".join(attrs)}}}' if attrs else ""


def block_to_markdown(block: dict) -> str:
    """Render a single (non-container) block to a connector-style markdown line.

    Container blocks (toggle, callout, column_list, column) are handled in
    render_blocks because they wrap their children; this covers leaf blocks.
    """
    t = block.get("type", "")
    data = block.get(t, {})
    if not isinstance(data, dict):
        data = {}
    text = rich_text_md(data.get("rich_text"))
    cs = _color_suffix(data)
    if t in ("heading_1", "heading_2", "heading_3", "heading_4"):
        return f"{'#' * int(t.split('_')[1])} {text}{_heading_suffix(data)}"
    if t == "bulleted_list_item":
        return f"- {text}{cs}"
    if t == "numbered_list_item":
        return f"1. {text}{cs}"
    if t == "to_do":
        return f"- [{'x' if data.get('checked') else ' '}] {text}{cs}"
    if t == "quote":
        return f"> {text}{cs}"
    if t == "code":
        lang = data.get("language") or "plain text"
        return f"```{lang}\n{rich_text_plain(data.get('rich_text'))}\n```"
    if t == "divider":
        return "---"
    if t == "equation":
        return f"$$\n{data.get('expression', '')}\n$$"
    if t == "image":
        return f"![{rich_text_plain(data.get('caption'))}]({_file_url(data)}){cs}"
    if t in ("video", "audio", "file", "pdf"):
        color = f' color="{_md_color(data["color"])}"' if data.get("color") and data["color"] != "default" else ""
        return f'<{t} src="{_file_url(data)}"{color}>{rich_text_plain(data.get("caption"))}</{t}>'
    if t in ("bookmark", "embed", "link_preview"):
        url = data.get("url", "")
        label = rich_text_plain(data.get("caption")) or url
        return f"[{label}]({url})" if url else ""
    if t == "link_to_page":
        # A database target must not round-trip as <page> (write would mis-type it
        # as page_id -> HTTP 400); emit a non-destructive database mention instead.
        if data.get("database_id"):
            return f'<mention-database url="{_notion_url(data["database_id"])}"></mention-database>'
        return f'<page url="{_notion_url(data.get("page_id"))}"></page>'
    if t == "child_page":
        return f'<page url="{_block_url(block)}">{data.get("title", "")}</page>'
    if t == "child_database":
        return f'<database url="{_block_url(block)}" inline="true">{data.get("title", "")}</database>'
    if t == "table_of_contents":
        return "<table_of_contents/>"
    if t == "breadcrumb":
        return ""
    if t == "paragraph":
        return f"{_lead_escape(text)}{cs}" if text else "<empty-block/>"
    if text:
        return f"{_lead_escape(text)}{cs}"
    return "<unknown/>"


def _render_children(client: NotionClient, blk: dict, depth: int, counter: dict, flat: list[dict]) -> str:
    sub_md, sub_flat = render_blocks(client, blk["id"], depth + 1, counter)
    flat.extend(sub_flat)
    return sub_md


def _render_one(client: NotionClient, blk: dict, depth: int, counter: dict,
                md_parts: list[str], flat: list[dict], parent_id: str | None = None) -> None:
    """Render one block (and its children) into md_parts/flat, connector-style.

    toggle -> <details>/<summary>, callout -> <callout>, columns ->
    <columns>/<column>, table -> <table>/<tr><td>, synced_block -> transparent,
    everything else through block_to_markdown. Child content is tab-indented.
    """
    counter["n"] += 1
    t = blk.get("type")
    data = blk.get(t, {})
    if not isinstance(data, dict):
        data = {}
    flat.append({
        "id": blk["id"],
        "type": t,
        "text": rich_text_plain(data.get("rich_text")),
        "has_children": blk.get("has_children", False),
        "parent_id": parent_id,
        "depth": depth,
    })
    has_kids = bool(blk.get("has_children")) and depth < _MAX_CHILD_DEPTH

    if t == "toggle":
        color = data.get("color")
        cattr = f' color="{_md_color(color)}"' if color and color != "default" else ""
        md_parts.append(f"<details{cattr}>")
        md_parts.append(f"<summary>{rich_text_md(data.get('rich_text'))}</summary>")
        if has_kids:
            sub = _render_children(client, blk, depth, counter, flat)
            if sub:
                md_parts.append(_indent(sub))
        md_parts.append("</details>")
    elif t == "callout":
        md_parts.append(_callout_open(data))
        body = _lead_escape(rich_text_md(data.get("rich_text")))
        if body:
            md_parts.append(_indent(body))
        if has_kids:
            sub = _render_children(client, blk, depth, counter, flat)
            if sub:
                md_parts.append(_indent(sub))
        md_parts.append("</callout>")
    elif t in ("column_list", "column"):
        tag = "columns" if t == "column_list" else "column"
        md_parts.append(f"<{tag}>")
        if has_kids:
            sub = _render_children(client, blk, depth, counter, flat)
            if sub:
                md_parts.append(_indent(sub))
        md_parts.append(f"</{tag}>")
    elif t == "table":
        # Connector spec: multi-line, tab-indented; attrs only emitted when true.
        attrs = ""
        if data.get("has_column_header"):
            attrs += ' header-row="true"'
        if data.get("has_row_header"):
            attrs += ' header-column="true"'
        md_parts.append(f"<table{attrs}>")
        if has_kids:
            for row in client.block_children_all(blk["id"]):
                if counter["n"] >= _MAX_TOTAL_BLOCKS:
                    break
                if row.get("type") != "table_row":
                    continue
                counter["n"] += 1
                md_parts.append("\t<tr>")
                for cell in (row.get("table_row") or {}).get("cells", []):
                    md_parts.append(f"\t\t<td>{rich_text_md(cell)}</td>")
                md_parts.append("\t</tr>")
        md_parts.append("</table>")
    elif t == "synced_block":
        synced_from = data.get("synced_from")
        if synced_from:
            md_parts.append(f'<synced_block_reference url="{_notion_url((synced_from or {}).get("block_id"))}">')
            close = "</synced_block_reference>"
        else:
            md_parts.append(f'<synced_block url="{_block_url(blk)}">')
            close = "</synced_block>"
        if has_kids:
            sub = _render_children(client, blk, depth, counter, flat)
            if sub:
                md_parts.append(_indent(sub))
        md_parts.append(close)
    else:
        line = block_to_markdown(blk)
        if line:
            md_parts.append(line)
        if has_kids and t not in ("child_page", "child_database"):
            sub = _render_children(client, blk, depth, counter, flat)
            if sub:
                md_parts.append(_indent(sub))


def render_blocks(client: NotionClient, block_id: str, depth: int, counter: dict) -> tuple[str, list[dict]]:
    """Render ALL children of block_id to (markdown, flat list). Used by writes."""
    md_parts: list[str] = []
    flat: list[dict] = []
    for blk in client.block_children_all(block_id):
        if counter["n"] >= _MAX_TOTAL_BLOCKS:
            md_parts.append(_TRUNC)
            break
        _render_one(client, blk, depth, counter, md_parts, flat, parent_id=block_id)
    return "\n".join(md_parts), flat


def render_page(client: NotionClient, block_id: str, start_cursor: str | None,
                page_size: int, counter: dict) -> tuple[str, list[dict], str | None]:
    """Render ONE page of top-level blocks (children fully expanded) for fetch.

    Returns (markdown, flat, next_cursor); next_cursor is None when no more
    top-level blocks remain, so the caller pages until it is None to read it all.
    """
    md_parts: list[str] = []
    flat: list[dict] = []
    data = client.block_children(block_id, page_size=page_size, start_cursor=start_cursor)
    for blk in data.get("results", []):
        if counter["n"] >= _MAX_TOTAL_BLOCKS:
            md_parts.append(_TRUNC)
            break
        _render_one(client, blk, 0, counter, md_parts, flat, parent_id=block_id)
    next_cursor = data.get("next_cursor") if data.get("has_more") else None
    return "\n".join(md_parts), flat, next_cursor


def _indent(text: str) -> str:
    return "\n".join("\t" + ln for ln in text.split("\n"))


# ---------------------------------------------------------------------------
# Property building (for create-pages / update-page update_properties)
# ---------------------------------------------------------------------------
def _schema_props(client: NotionClient, database_id: str) -> dict:
    db = client.retrieve_database(database_id)
    return db.get("properties", {})


def build_properties(props_in: dict, schema: dict | None) -> dict:
    """Convert a flat {name: value} map (connector style) to Notion property values.

    Handles expanded keys used by the connector:
      - date:{prop}:start / :end / :is_datetime
      - checkbox values "__YES__" / "__NO__"
      - "userDefined:URL" / "userDefined:id" prefixes
    """
    # Gather expanded date / place parts first.
    date_parts: dict[str, dict] = {}
    place_parts: dict[str, dict] = {}
    simple: dict[str, Any] = {}
    for key, value in props_in.items():
        if key.startswith("date:"):
            _, pname, part = key.split(":", 2)
            date_parts.setdefault(pname, {})[part] = value
        elif key.startswith("place:"):
            _, pname, part = key.split(":", 2)
            place_parts.setdefault(pname, {})[part] = value
        else:
            name = key[len("userDefined:"):] if key.startswith("userDefined:") else key
            simple[name] = value

    out: dict = {}
    for pname, parts in date_parts.items():
        is_dt = _truthy(parts.get("is_datetime", "1"))
        start = parts.get("start")
        end = parts.get("end")
        if not is_dt:
            start = _date_only(start)
            end = _date_only(end)
        d: dict = {}
        if start is not None:
            d["start"] = start
        if end:
            d["end"] = end
        out[pname] = {"date": d or None}

    for pname, parts in place_parts.items():
        place: dict = {}
        for key in ("name", "address", "google_place_id"):
            if parts.get(key):
                place[key] = parts[key]
        for geo in ("latitude", "longitude"):
            v = parts.get(geo)
            if v not in (None, ""):
                try:
                    place[geo] = float(v)
                except (TypeError, ValueError):
                    place[geo] = v
        out[pname] = {"place": place or None}

    for name, value in simple.items():
        ptype = (schema.get(name, {}).get("type") if schema else None)
        out[name] = _coerce_property(ptype, value)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "__yes__")
    return bool(value)


def _date_only(value: Any) -> Any:
    """Drop any time component so a date-only property isn't stored as a datetime."""
    if isinstance(value, str) and "T" in value:
        return value.split("T", 1)[0]
    return value


def _option_name(value: Any) -> str:
    """Extract a select/status option name from the various shapes a model may send.

    Accepts a bare name ("Done"), a partial object ({"name": "Done"}), or the full
    official Notion value ({"select": {"name": "Done"}} / {"status": {"name": "Done"}}).
    Without this a nested object would be str()'d into a bogus option name (e.g.
    "{'name': 'Done'}") and Notion would reject the write with a 400.
    """
    if isinstance(value, dict):
        inner = value.get("select") or value.get("status")
        if isinstance(inner, dict):
            value = inner
        name = value.get("name")
        if isinstance(name, str):
            return name
    return str(value)


def _ref_id(value: Any) -> str:
    """Extract a relation/people id from a bare id string or a {"id": ...} object."""
    if isinstance(value, dict):
        return str(value.get("id", ""))
    return str(value)


def _coerce_property(ptype: str | None, value: Any) -> dict:
    # Title / unknown text falls back to title or rich_text based on schema type.
    if value is None:
        return {ptype: None} if ptype else {"rich_text": []}
    # Accept the official Notion property-value object as-is. Models trained on the
    # Notion REST API often pass the nested form (e.g. {"status": {"name": "Done"}} or
    # {"multi_select": [{"name": "A"}]}) instead of the flat connector value; without
    # this we'd str() the dict into a bogus option name and Notion would 400. The flat
    # connector form (a plain string / list) still works via the branches below.
    if ptype and isinstance(value, dict) and ptype in value:
        return {ptype: value[ptype]}
    if ptype == "title" or ptype is None and isinstance(value, str):
        # default unknown string -> title only if we truly don't know; else rich_text
        if ptype == "title":
            return {"title": _inline_to_rich(str(value))}
    if ptype == "title":
        return {"title": _inline_to_rich(str(value))}
    if ptype == "rich_text":
        return {"rich_text": _inline_to_rich(str(value))}
    if ptype == "number":
        return {"number": value if isinstance(value, (int, float)) else float(value)}
    if ptype == "select":
        return {"select": {"name": _option_name(value)}}
    if ptype == "status":
        return {"status": {"name": _option_name(value)}}
    if ptype == "multi_select":
        vals = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return {"multi_select": [{"name": _option_name(v)} for v in vals]}
    if ptype == "checkbox":
        if isinstance(value, str):
            return {"checkbox": value.strip().upper() in ("__YES__", "TRUE", "YES", "1")}
        return {"checkbox": bool(value)}
    if ptype in ("url", "email", "phone_number"):
        return {ptype: str(value)}
    if ptype == "date":
        return {"date": {"start": str(value)}}
    if ptype == "people":
        ids = value if isinstance(value, list) else [value]
        return {"people": [{"id": _ref_id(i)} for i in ids]}
    if ptype == "relation":
        ids = value if isinstance(value, list) else [value]
        return {"relation": [{"id": _ref_id(i)} for i in ids]}
    # Fallback: treat as rich_text
    return {"rich_text": _inline_to_rich(str(value))}


def _parent_body(parent: dict | None) -> dict:
    """Map a connector parent ({page_id|database_id|data_source_id}) to a REST parent."""
    if not parent:
        return {"type": "workspace", "workspace": True}
    if parent.get("page_id"):
        return {"type": "page_id", "page_id": extract_id(parent["page_id"])}
    if parent.get("database_id"):
        return {"type": "database_id", "database_id": extract_id(parent["database_id"])}
    if parent.get("data_source_id"):
        # REST has no data_source parent; the collection id is used as database_id.
        # Works when the database is single-source.
        return {"type": "database_id", "database_id": extract_id(parent["data_source_id"])}
    if parent.get("type") == "workspace":
        return {"type": "workspace", "workspace": True}
    raise ServiceError("Unrecognized parent. Use page_id, database_id, or data_source_id.", status_code=400)


# ===========================================================================
# High-level operations (one per connector tool)
# ===========================================================================
def op_search(settings: Settings, *, query: str, query_type: str = "internal",
              data_source_url: str | None = None, page_url: str | None = None,
              teamspace_id: str | None = None, filters: dict | None = None,
              page_size: int = 10, **_ignored) -> dict:
    with NotionClient(settings) as c:
        if query_type == "user":
            users = c.list_users(page_size=100).get("results", [])
            q = query.lower()
            hits = [
                {"id": u["id"], "name": u.get("name"), "type": u.get("type"),
                 "email": (u.get("person") or {}).get("email")}
                for u in users
                if q in (u.get("name") or "").lower()
                or q in ((u.get("person") or {}).get("email") or "").lower()
            ]
            return {"type": "user", "results": hits}

        body: dict = {"query": query, "page_size": min(page_size, 25)}
        # Scope to pages or databases if the caller clearly wants a data source.
        data = c.search(body)
        results = []
        for r in data.get("results", []):
            obj = r.get("object")
            if obj == "page":
                results.append({"id": r["id"], "object": "page", "title": page_title(r), "url": r.get("url")})
            elif obj == "database":
                results.append({
                    "id": r["id"], "object": "database",
                    "title": rich_text_plain(r.get("title")), "url": r.get("url"),
                })
        return {"type": "workspace", "query": query, "results": results,
                "note": "Backed by Notion REST search (Notion content, title/relevance ranked)."}


def op_fetch(settings: Settings, *, id: str, include_discussions: bool = False,
             start_cursor: str | None = None, page_size: int = 100, **_ignored) -> dict:
    nid = extract_id(id)
    with NotionClient(settings) as c:
        # Try page first, then database.
        try:
            page = c.retrieve_page(nid)
        except ServiceError:
            page = None
        if page and page.get("object") == "page":
            counter = {"n": 0}
            md, flat, next_cursor = render_page(c, nid, start_cursor, min(max(page_size, 1), 100), counter)
            return {
                "object": "page",
                "id": nid,
                "url": page.get("url"),
                "title": page_title(page),
                "properties": flatten_properties(page.get("properties", {})),
                "content_markdown": md,
                "blocks": flat,
                # Long pages page in: if has_more, re-fetch with start_cursor=next_cursor.
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            }
        db = c.retrieve_database(nid)
        return {
            "object": "database",
            "id": nid,
            "url": db.get("url"),
            "title": rich_text_plain(db.get("title")),
            "description": rich_text_plain(db.get("description")),
            "properties": {
                name: {"type": p.get("type")} for name, p in db.get("properties", {}).items()
            },
            "data_source_id": nid,
        }


def op_query_database(settings: Settings, *, database_id: str, filter: dict | None = None,
                      sorts: list | None = None, page_size: int = 100,
                      start_cursor: str | None = None, **_ignored) -> dict:
    with NotionClient(settings) as c:
        body: dict = {"page_size": min(page_size, 100)}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = c.query_database(extract_id(database_id), body)
        rows = [
            {"id": r["id"], "url": r.get("url"), "properties": flatten_properties(r.get("properties", {}))}
            for r in data.get("results", [])
        ]
        return {"results": rows, "has_more": data.get("has_more", False), "next_cursor": data.get("next_cursor")}


def op_create_pages(settings: Settings, *, pages: list[dict], parent: dict | None = None, **_ignored) -> dict:
    with NotionClient(settings) as c:
        parent_body = _parent_body(parent)
        schema = None
        if parent_body.get("type") == "database_id":
            schema = _schema_props(c, parent_body["database_id"])
        created = []
        for spec in pages:
            body: dict = {"parent": parent_body}
            props_in = spec.get("properties") or {}
            if schema is not None:
                body["properties"] = build_properties(props_in, schema)
            else:
                # Page parent: only a title is allowed.
                title = props_in.get("title") or props_in.get("Title") or ""
                body["properties"] = {"title": {"title": _inline_to_rich(str(title))}}
            if spec.get("content"):
                body["children"] = markdown_to_blocks(spec["content"])
            _apply_icon_cover(body, spec.get("icon"), spec.get("cover"))
            page = c.create_page(body)
            created.append({"id": page.get("id"), "url": page.get("url"), "title": page_title(page)})
        return {"created": created, "count": len(created)}


def op_update_page(settings: Settings, *, page_id: str, command: str,
                   properties: dict | None = None, content: str | None = None,
                   content_updates: list[dict] | None = None, new_str: str | None = None,
                   position: dict | None = None, after_block_id: str | None = None,
                   allow_deleting_content: bool = False, icon: str | None = None,
                   cover: str | None = None, **_ignored) -> dict:
    pid = extract_id(page_id)
    with NotionClient(settings) as c:
        if command == "update_properties":
            if not properties:
                raise ServiceError("update_properties requires 'properties'.", status_code=400)
            page = c.retrieve_page(pid)
            schema = None
            parent = page.get("parent", {})
            if parent.get("type") == "database_id":
                schema = _schema_props(c, parent["database_id"])
            body = {"properties": build_properties(properties, schema)}
            _apply_icon_cover(body, icon, cover)
            c.update_page(pid, body)
            return {"updated": True, "command": command, "page_id": pid}

        if command == "insert_content":
            if not content:
                raise ServiceError("insert_content requires 'content'.", status_code=400)
            blocks = markdown_to_blocks(content)
            after = extract_id(after_block_id) if after_block_id else None
            # REST appends to the end; 'after' lets us target a position. 'start'
            # without an anchor is not expressible in REST, so it appends to end.
            c.append_children(pid, blocks, after=after)
            if icon or cover:
                b = {}
                _apply_icon_cover(b, icon, cover)
                c.update_page(pid, b)
            return {"updated": True, "command": command, "inserted_blocks": len(blocks),
                    "position": "after" if after else (position or {}).get("type", "end")}

        if command == "update_content":
            if not content_updates:
                raise ServiceError("update_content requires 'content_updates'.", status_code=400)
            _, flat = render_blocks(c, pid, 0, {"n": 0})
            results = [_apply_content_update(c, pid, flat, u, allow_deleting_content)
                       for u in content_updates]
            return {"updated": True, "command": command, "operations": results}

        if command == "replace_content":
            if new_str is None:
                raise ServiceError("replace_content requires 'new_str'.", status_code=400)
            children = c.block_children_all(pid)
            flat_top = [{"id": b["id"], "type": b.get("type"),
                         "text": (b.get(b.get("type"), {}) or {}).get("title", ""),
                         "has_children": b.get("has_children", False)}
                        for b in children]
            protected = _collect_protected(c, flat_top, list(range(len(flat_top))))
            if protected and not allow_deleting_content:
                raise ServiceError(_child_guard_msg(protected), status_code=400)
            for b in children:
                c.delete_block(b["id"])
            new_blocks = markdown_to_blocks(new_str)
            if new_blocks:
                c.append_children(pid, new_blocks)
            return {"updated": True, "command": command, "replaced_blocks": len(children),
                    "new_blocks": len(new_blocks)}

        if command in ("apply_template", "update_verification"):
            raise ServiceError(
                f"'{command}' is not available through the Notion REST API and so cannot "
                "be mirrored by this wrapper.",
                status_code=501,
            )

        raise ServiceError(f"Unknown update-page command '{command}'.", status_code=400)


# ===========================================================================
# Block-ID primitives — deterministic structural ops (claude.ai's connector has
# none of these). Target blocks by their unique id from notion_list_blocks /
# notion_fetch's `blocks` list, NOT by text match. The safe way to delete
# duplicates or restructure nesting.
# ===========================================================================
def _block_summary(b: dict, parent_id: str | None, depth: int) -> dict:
    t = b.get("type")
    data = b.get(t, {}) if isinstance(b.get(t), dict) else {}
    text = rich_text_plain(data.get("rich_text"))
    if not text and t in ("child_page", "child_database"):
        text = data.get("title", "")
    return {"id": b["id"], "type": t, "text": text,
            "has_children": b.get("has_children", False),
            "parent_id": parent_id, "depth": depth}


def _list_blocks_recursive(c: NotionClient, block_id: str, parent_id: str,
                           depth: int, counter: dict) -> list[dict]:
    out: list[dict] = []
    if depth >= _MAX_CHILD_DEPTH:
        return out
    for b in c.block_children_all(block_id):
        if counter["n"] >= _MAX_TOTAL_BLOCKS:
            break
        counter["n"] += 1
        out.append(_block_summary(b, parent_id, depth))
        if b.get("has_children") and b.get("type") not in ("child_page", "child_database"):
            out.extend(_list_blocks_recursive(c, b["id"], b["id"], depth + 1, counter))
    return out


def op_list_blocks(settings: Settings, *, page_id: str, recursive: bool = False,
                   start_cursor: str | None = None, **_ignored) -> dict:
    """List a page/block's children as {id, type, text, has_children, parent_id, depth}.

    recursive=false returns one paginated page of direct children (pass next_cursor
    back as start_cursor). recursive=true walks the whole subtree (bounded).
    """
    pid = extract_id(page_id)
    with NotionClient(settings) as c:
        if recursive:
            blocks = _list_blocks_recursive(c, pid, pid, 0, {"n": 0})
            return {"page_id": pid, "blocks": blocks, "has_more": False, "next_cursor": None}
        data = c.block_children(pid, start_cursor=start_cursor)
        blocks = [_block_summary(b, pid, 0) for b in data.get("results", [])]
        nc = data.get("next_cursor") if data.get("has_more") else None
        return {"page_id": pid, "blocks": blocks, "has_more": nc is not None, "next_cursor": nc}


def _normalize_block(b: dict) -> dict:
    """Tag a typed Notion block object with object:"block" (recursively for children)."""
    if not isinstance(b, dict):
        raise ServiceError("Each block must be a typed Notion block object.", status_code=400)
    out = dict(b)
    out.setdefault("object", "block")
    t = out.get("type")
    if t and isinstance(out.get(t), dict):
        data = dict(out[t])
        if isinstance(data.get("children"), list):
            data["children"] = [_normalize_block(ch) for ch in data["children"]]
            out[t] = data
    return out


def op_append_blocks(settings: Settings, *, parent_id: str, blocks: list[dict],
                     after: str | None = None, **_ignored) -> dict:
    """Append typed Notion block objects under a parent (native nesting; no markdown
    round-trip). `after` places them after an existing child block by id."""
    if not blocks:
        raise ServiceError("append_blocks requires a non-empty 'blocks' list.", status_code=400)
    pid = extract_id(parent_id)
    children = [_normalize_block(b) for b in blocks]
    with NotionClient(settings) as c:
        after_id = extract_id(after) if after else None
        res = c.append_children(pid, children, after=after_id)
        results = res.get("results", [])
        return {"appended": True, "parent_id": pid, "after": after_id,
                "added_blocks": len(results),
                "block_ids": [r.get("id") for r in results]}


def _block_update_body(block: dict) -> dict:
    """Reduce a block object to the {type: data} payload PATCH /blocks/{id} accepts."""
    b = dict(block)
    for k in ("object", "id", "has_children", "parent", "created_time",
              "last_edited_time", "created_by", "last_edited_by"):
        b.pop(k, None)
    out: dict = {}
    if "archived" in b:
        out["archived"] = b.pop("archived")
    t = b.pop("type", None)
    if t and t in b:
        out[t] = b[t]
    else:
        out.update(b)
    if not out:
        raise ServiceError("update_block needs a block payload (e.g. {\"paragraph\": "
                           "{\"rich_text\": [...]}}).", status_code=400)
    return out


def op_update_block(settings: Settings, *, block_id: str, block: dict, **_ignored) -> dict:
    if not block or not isinstance(block, dict):
        raise ServiceError("update_block requires a 'block' object.", status_code=400)
    bid = extract_id(block_id)
    body = _block_update_body(block)
    with NotionClient(settings) as c:
        res = c.update_block(bid, body)
        return {"updated": True, "block_id": bid, "type": res.get("type")}


def op_delete_blocks(settings: Settings, *, block_ids: list[str],
                     allow_deleting_content: bool = False, **_ignored) -> dict:
    """Delete specific blocks by id (deterministic; only the listed blocks). Guards
    child pages/databases the same way as update_content."""
    if not block_ids:
        raise ServiceError("delete_blocks requires a non-empty 'block_ids' list.", status_code=400)
    ids = [extract_id(b) for b in block_ids]
    with NotionClient(settings) as c:
        flat = []
        for bid in ids:
            blk = c.retrieve_block(bid)
            t = blk.get("type")
            flat.append({"id": bid, "type": t,
                         "text": (blk.get(t, {}) or {}).get("title", ""),
                         "has_children": blk.get("has_children", False)})
        protected = _collect_protected(c, flat, list(range(len(flat))))
        if protected and not allow_deleting_content:
            raise ServiceError(_child_guard_msg(protected), status_code=400)
        results: dict = {}
        for bid in ids:
            try:
                c.delete_block(bid)
                results[bid] = "deleted"
            except ServiceError as e:
                results[bid] = f"error: {e.message}"
        deleted = [b for b, v in results.items() if v == "deleted"]
        return {"deleted": deleted, "results": results, "count": len(deleted)}


def _writable_block_data(data: dict) -> dict:
    """Strip a read-shape block's type-data to fields the create/append API accepts."""
    data = data or {}
    out: dict = {}
    if data.get("rich_text") is not None:
        out["rich_text"] = [_writable_token(tok) for tok in data["rich_text"]]
    for k in ("checked", "color", "language", "icon", "is_toggleable", "expression",
              "type", "external", "url", "table_width", "has_column_header",
              "has_row_header", "cells"):
        if k in data:
            out[k] = data[k]
    if isinstance(data.get("caption"), list):
        out["caption"] = [_writable_token(tok) for tok in data["caption"]]
    if isinstance(data.get("cells"), list):
        out["cells"] = [[_writable_token(tok) for tok in cell] for cell in data["cells"]]
    return out


def _copy_subtree(c: NotionClient, block_id: str, depth: int = 0) -> dict:
    blk = c.retrieve_block(block_id)
    t = blk.get("type")
    payload: dict = {"object": "block", "type": t, t: _writable_block_data(blk.get(t, {}))}
    if (blk.get("has_children") and t not in ("child_page", "child_database")
            and depth < _MAX_CHILD_DEPTH):
        kids = [_copy_subtree(c, ch["id"], depth + 1) for ch in c.block_children_all(block_id)]
        if kids:
            payload[t] = dict(payload[t])
            payload[t]["children"] = kids
    return payload


def op_move_blocks(settings: Settings, *, block_ids: list[str],
                   after_block_id: str, **_ignored) -> dict:
    """Move blocks to sit after another block. The REST API (2022-06-28) has no native
    move, so this copies each block's full subtree after the target, then deletes the
    originals (children preserved by value; ids change)."""
    if not block_ids:
        raise ServiceError("move_blocks requires 'block_ids'.", status_code=400)
    if not after_block_id:
        raise ServiceError("move_blocks requires 'after_block_id'.", status_code=400)
    ids = [extract_id(b) for b in block_ids]
    after_id = extract_id(after_block_id)
    with NotionClient(settings) as c:
        after_blk = c.retrieve_block(after_id)
        parent = after_blk.get("parent", {})
        parent_id = parent.get("page_id") or parent.get("block_id")
        if not parent_id:
            raise ServiceError("Could not resolve the parent of after_block_id.", status_code=400)
        moved = []
        cur_after = after_id
        for bid in ids:
            subtree = _copy_subtree(c, bid)
            res = c.append_children(parent_id, [subtree], after=cur_after)
            new_id = (res.get("results") or [{}])[0].get("id")
            moved.append({"old_id": bid, "new_id": new_id})
            if new_id:
                cur_after = new_id
        for bid in ids:
            c.delete_block(bid)
        return {"moved": moved, "count": len(moved), "parent_id": parent_id}


# ---------------------------------------------------------------------------
# Coarse Alistair write tools — Actions-row create + References-Tray append.
# Both are INSERT/CREATE-ONLY (never replace_content) and the tray append
# follows the sacred read-first -> insert -> re-fetch -> verify protocol.
# The target page ids come from settings (REFERENCES_TRAY_PAGE_ID / etc.), so
# nothing personal is hardcoded here.
# ---------------------------------------------------------------------------


def _block_plain(block: dict) -> str:
    t = block.get("type")
    data = block.get(t, {}) or {}
    return rich_text_plain(data.get("rich_text"))


def _is_end_of_tray(block: dict) -> bool:
    return block.get("type") == "callout" and "END OF TRAY" in _block_plain(block).upper()


def _is_tray_trailing(block: dict) -> bool:
    """Blocks that form the tray's closing boundary (divider/images/empties)."""
    t = block.get("type")
    if t in ("divider", "image", "video", "embed", "file"):
        return True
    if t == "paragraph" and not _block_plain(block).strip():
        return True
    return False


def op_add_action(settings: Settings, *, name: str, status: str = "Next",
                  due: str | None = None, project: str | list[str] | None = None,
                  **_ignored) -> dict:
    """Create ONE row in the Actions database (non-destructive create).

    Optionally file it under one or more Projects: pass `project` as a Notion page
    id/URL (or a list of them) and it is written to the 'Project' relation, so the
    Action lands under the right Project — and therefore its Area — in PARA. Without
    it the Action is created unlinked (loose in the Actions DB).
    """
    name = (name or "").strip()
    if not name:
        raise ServiceError("add_action requires a non-empty 'name'.", status_code=400)
    if not settings.actions_db_id:
        raise ServiceError("ACTIONS_DB_ID is not configured.", status_code=503)
    status = (status or "Next").strip() or "Next"
    props: dict = {"Name": name, "Action Status": status}
    if due:
        props["date:Due:start"] = due
    proj_ids = None
    if project:
        proj_list = project if isinstance(project, list) else [project]
        proj_ids = [extract_id(p) for p in proj_list if p] or None
        if proj_ids:
            props["Project"] = proj_ids  # schema-typed 'relation' -> {"relation": [{"id": ...}]}
    res = op_create_pages(
        settings,
        pages=[{"properties": props}],
        parent={"database_id": settings.actions_db_id},
    )
    created = (res.get("created") or [{}])[0]
    return {"added_action": {"name": name, "status": status, "project_ids": proj_ids,
                             "id": created.get("id"), "url": created.get("url")}}


def op_save_reference(settings: Settings, *, title: str, body: str | None = None,
                      link: str | None = None, dry_run: bool = False,
                      _client=None, **_ignored) -> dict:
    """Append an entry to the References Tray (insert-only, read-first + verified).

    Read-first -> find the last real entry above the trailing END-OF-TRAY boundary
    -> insert (NEVER replace) one <empty-block/> spacer + the entry -> re-fetch and
    verify. Aborts rather than guessing if the END OF TRAY marker is missing.
    """
    title = (title or "").strip()
    if not title:
        raise ServiceError("save_reference requires a non-empty 'title'.", status_code=400)
    tray_id = settings.references_tray_page_id
    if not tray_id:
        raise ServiceError("REFERENCES_TRAY_PAGE_ID is not configured.", status_code=503)

    lines = [f"#### {title}"]
    if body and body.strip():
        lines.append(body.strip())
    if link and link.strip():
        lines.append(link.strip())
    entry_md = "\n".join(lines)
    new_blocks = markdown_to_blocks("<empty-block/>\n" + entry_md)

    if _client is not None:
        return _save_reference_with(_client, title, entry_md, new_blocks, dry_run, tray_id)
    with NotionClient(settings) as c:
        return _save_reference_with(c, title, entry_md, new_blocks, dry_run, tray_id)


def _save_reference_with(c, title: str, entry_md: str, new_blocks: list, dry_run: bool,
                         tray_id: str) -> dict:
    blocks = c.block_children_all(tray_id)
    if not blocks:
        raise ServiceError(
            "References Tray came back empty; refusing to write blindly.", status_code=502
        )
    eot_idx = next((i for i, b in enumerate(blocks) if _is_end_of_tray(b)), None)
    if eot_idx is None:
        raise ServiceError(
            "Could not find the 'END OF TRAY' marker on the References Tray; refusing to "
            "write (wrong page or changed structure).",
            status_code=409,
        )
    i = eot_idx - 1
    while i >= 0 and _is_tray_trailing(blocks[i]):
        i -= 1
    if i < 0:
        raise ServiceError(
            "No content entry found above the tray boundary; refusing to guess placement.",
            status_code=409,
        )
    anchor = blocks[i]
    plan = {
        "tray_page": tray_id,
        "anchor_id": anchor["id"],
        "anchor_preview": _block_plain(anchor)[:80],
        "entry_md": entry_md,
        "new_block_count": len(new_blocks),
    }
    if dry_run:
        return {"wrote": False, "dry_run": True, "plan": plan}

    before = len(blocks)
    c.append_children(tray_id, new_blocks, after=anchor["id"])

    # Re-fetch and verify only the intended change landed.
    after_blocks = c.block_children_all(tray_id)
    grew_by = len(after_blocks) - before
    entry_present = any(title in _block_plain(b) for b in after_blocks)
    eot_after = next((j for j, b in enumerate(after_blocks) if _is_end_of_tray(b)), None)
    tail_ok = eot_after is not None and all(
        _is_tray_trailing(after_blocks[k]) for k in range(eot_after + 1, len(after_blocks))
    )
    verified = grew_by == len(new_blocks) and entry_present and tail_ok
    return {
        "wrote": True,
        "verified": verified,
        "plan": plan,
        "grew_by": grew_by,
        "warning": None if verified else
        "Inserted, but post-write verification was not fully satisfied; eyeball the tray.",
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _to_plain(line: str) -> str:
    """Normalize an inline-markdown line to its plain text (strip **bold** etc.).

    So `old_str` matches whether the model copied the plain `text` of a block or a
    marked-up snippet from `content_markdown`. Reuses the inline parser so the
    stripping logic stays in one place; `_tok_plain` reads the write-shape tokens it
    produces (text.content) the same way it reads read-shape tokens (plain_text).
    """
    return "".join(_tok_plain(tok) for tok in _inline_to_rich(line))


def _old_plain(old: str) -> str:
    """Plain-text form of (possibly multi-line) old_str, newlines preserved."""
    return "\n".join(_to_plain(ln) for ln in old.split("\n"))


def _find_all_exact(hay: str, needle: str) -> list[tuple[int, int]]:
    """Non-overlapping exact occurrences of needle in hay as (start, end) offsets."""
    if not needle:
        return []
    out: list[tuple[int, int]] = []
    i = hay.find(needle)
    while i != -1:
        out.append((i, i + len(needle)))
        i = hay.find(needle, i + len(needle))
    return out


def _index_matches(flat: list[dict], old: str) -> list[dict]:
    """Locate every place old_str occurs, block-boundary aware.

    Returns a list of match dicts:
      single (within one block):  {kind:"single", idxs:[i], char_start, char_end, whole_block}
      cross  (contiguous run):    {kind:"cross", idxs:[i, i+1, ...]}
    Single matches are found first (exact substring, then a whitespace-tolerant
    whole-block fallback). Cross matches are only formed when no single match exists
    and old_str spans a run of WHOLE blocks (each line equal to one block), which is
    what keeps a short repeated word from being treated as a multi-block range.
    """
    op_plain = _old_plain(old)
    op_norm = _norm(op_plain)

    # Pass 1: exact substring within a single block (gives real char offsets).
    single: list[dict] = []
    for i, e in enumerate(flat):
        bp = e.get("text", "")
        if not bp:
            continue
        for cs, ce in _find_all_exact(bp, op_plain):
            single.append({"kind": "single", "idxs": [i], "char_start": cs,
                           "char_end": ce, "whole_block": _norm(bp) == op_norm})
    if single:
        return single

    # Pass 1b: whitespace-tolerant WHOLE-block equality (old_str is a full block,
    # whitespace differs). Span = the whole block, so delete/replace stay safe.
    for i, e in enumerate(flat):
        bp = e.get("text", "")
        if bp and _norm(bp) == op_norm and op_norm:
            single.append({"kind": "single", "idxs": [i], "char_start": 0,
                           "char_end": len(bp), "whole_block": True})
    if single:
        return single

    # Pass 2: cross-block contiguous run of whole blocks (multi-line old_str only).
    lines_all = [_norm(_to_plain(ln)) for ln in old.split("\n")]
    while lines_all and lines_all[0] == "":
        lines_all.pop(0)
    while lines_all and lines_all[-1] == "":
        lines_all.pop()
    cross: list[dict] = []
    L = len(lines_all)
    if L >= 2:
        norms = [_norm(e.get("text", "")) for e in flat]
        i = 0
        while i + L <= len(flat):
            if all(norms[i + k] == lines_all[k] for k in range(L)) and any(lines_all):
                cross.append({"kind": "cross", "idxs": list(range(i, i + L))})
                i += L
            else:
                i += 1
    return cross


def _classify_op(old: str, new: str) -> str:
    if new.startswith(old) and len(new) > len(old):
        return "append"
    if _norm(new) == "":
        return "delete"
    return "replace"


def _snippet(text: str, cs: int, ce: int, pad: int = 30) -> str:
    a = max(0, cs - pad)
    b = min(len(text), ce + pad)
    lead = "…" if a > 0 else ""
    trail = "…" if b < len(text) else ""
    return f"{lead}{text[a:cs]}»{text[cs:ce]}«{text[ce:b]}{trail}"


def _multi_match_msg(flat: list[dict], matches: list[dict]) -> str:
    lines = []
    for k, m in enumerate(matches, 1):
        if m["kind"] == "single":
            e = flat[m["idxs"][0]]
            lines.append(f"  [{k}] block {e['id']} ({e['type']}): "
                         f"{_snippet(e.get('text', ''), m['char_start'], m['char_end'])}")
        else:
            ids = ", ".join(flat[i]["id"] for i in m["idxs"])
            lines.append(f"  [{k}] blocks {ids}")
    return (
        f"old_str matched {len(matches)} places; refusing to guess which. Set "
        "replace_all_matches=true to apply to all, narrow old_str to a unique snippet, "
        "or use notion_delete_blocks with specific block ids for structural deletes.\n"
        + "\n".join(lines)
    )


def _child_guard_msg(protected: list[dict]) -> str:
    items = "; ".join(f"{p['type']} {p['id']} ({p['title']})" for p in protected)
    return (
        f"This update would delete {len(protected)} child page(s)/database(s): {items}. "
        "Set allow_deleting_content=true to proceed, or use notion_delete_blocks."
    )


def _walk_protected(c: NotionClient, block_id: str, depth: int) -> list[dict]:
    if depth >= _MAX_CHILD_DEPTH:
        return []
    out: list[dict] = []
    for ch in c.block_children_all(block_id):
        t = ch.get("type")
        if t in ("child_page", "child_database"):
            out.append({"id": ch["id"], "type": t, "title": (ch.get(t) or {}).get("title", "")})
        if ch.get("has_children"):
            out.extend(_walk_protected(c, ch["id"], depth + 1))
    return out


def _collect_protected(c: NotionClient, flat: list[dict], idxs: list[int]) -> list[dict]:
    """Child pages/databases that deleting the given blocks (and their subtrees) removes."""
    out: list[dict] = []
    for i in idxs:
        e = flat[i]
        if e["type"] in ("child_page", "child_database"):
            out.append({"id": e["id"], "type": e["type"], "title": e.get("text") or ""})
        if e.get("has_children"):
            out.extend(_walk_protected(c, e["id"], 0))
    return out


def _writable_ann(ann: dict) -> dict:
    keep = ("bold", "italic", "strikethrough", "underline", "code", "color")
    return {k: ann[k] for k in keep if k in ann}


def _clone_trim_text(tok: dict, content: str) -> dict:
    txt: dict = {"content": content}
    link = (tok.get("text") or {}).get("link")
    if link:
        txt["link"] = link
    out: dict = {"type": "text", "text": txt}
    ann = tok.get("annotations")
    if ann:
        wa = _writable_ann(ann)
        if wa:
            out["annotations"] = wa
    return out


def _writable_token(tok: dict) -> dict:
    """Strip a read-shape rich_text token down to what the REST write API accepts."""
    ttype = tok.get("type", "text")
    if ttype == "equation":
        out: dict = {"type": "equation",
                     "equation": {"expression": (tok.get("equation") or {}).get("expression", "")}}
    elif ttype == "mention":
        out = {"type": "mention", "mention": copy.deepcopy(tok.get("mention", {}))}
    else:
        content = (tok.get("text") or {}).get("content")
        if content is None:
            content = tok.get("plain_text", "")
        return _clone_trim_text(tok, content)
    ann = tok.get("annotations")
    if ann:
        wa = _writable_ann(ann)
        if wa:
            out["annotations"] = wa
    return out


def _tok_plain(tok: dict) -> str:
    ptext = tok.get("plain_text")
    if ptext is None:
        ptext = (tok.get("text") or {}).get("content", "")
    return ptext


def _slice_rich_text(rich: list[dict], start: int, end: int) -> list[dict]:
    """Rich-text tokens covering plain-text [start, end), trimming boundary tokens
    while preserving each token's annotations/link. Refuses to bisect an atomic
    mention/equation token (raises 400) rather than corrupt it."""
    out: list[dict] = []
    pos = 0
    for tok in rich:
        ptext = _tok_plain(tok)
        tlen = len(ptext)
        tstart, tend = pos, pos + tlen
        pos = tend
        if tlen == 0 or tend <= start or tstart >= end:
            continue
        a, b = max(start, tstart), min(end, tend)
        if a == tstart and b == tend:
            out.append(_writable_token(tok))
        elif tok.get("type", "text") != "text":
            raise ServiceError(
                "old_str splits an inline mention/equation; widen old_str to whole tokens.",
                status_code=400,
            )
        else:
            out.append(_clone_trim_text(tok, ptext[a - tstart:b - tstart]))
    return out


def _rebuild_rich_text_update(block: dict, cs: int, ce: int, new_md: str) -> dict:
    """Splice [cs, ce) of a block's rich_text with new_md, preserving the rest of the
    block's text, its inline annotations, and all other block fields (checked, color,
    language, icon). new_md is parsed as inline markdown (literal for code)."""
    t = block.get("type", "paragraph")
    data = block.get(t, {}) or {}
    rich = data.get("rich_text", []) or []
    total = sum(len(_tok_plain(tok)) for tok in rich)
    before = _slice_rich_text(rich, 0, cs)
    after = _slice_rich_text(rich, ce, total)
    if not new_md:
        mid: list[dict] = []
    elif t == "code":
        mid = [{"type": "text", "text": {"content": new_md}}]
    else:
        mid = _inline_to_rich(new_md)
    return {t: {"rich_text": before + mid + after}}


def _apply_content_update(c: NotionClient, pid: str, flat: list[dict], update: dict,
                          call_allow_deleting: bool = False) -> dict:
    """Apply ONE {old_str, new_str, ...} edit, block-boundary aware and fail-safe.

    Guards (mirrors claude.ai, then exceeds it):
      * multi-match  -> 409 unless replace_all_matches (lists each match + snippet)
      * cross-block  -> 400 unless allow_cross_block (delete only; replace rejected)
      * child-page   -> 400 unless allow_deleting_content (lists the pages/databases)
    Every target is validated BEFORE any mutation, so a guard never leaves a
    half-applied edit.
    """
    old = update.get("old_str") or ""
    new = update.get("new_str")
    if new is None:
        new = ""
    if not _norm(old):
        raise ServiceError(
            "Each content update needs a non-empty old_str (whitespace-only is rejected).",
            status_code=400,
        )
    replace_all = bool(update.get("replace_all_matches", False))
    allow_cross = bool(update.get("allow_cross_block", False))
    allow_deleting = bool(update.get("allow_deleting_content", call_allow_deleting))

    op = _classify_op(old, new)
    matches = _index_matches(flat, old)
    total = len(matches)
    if total == 0:
        raise ServiceError(f"old_str not found on page: {old[:80]!r}", status_code=404)
    if total > 1 and not replace_all:
        raise ServiceError(_multi_match_msg(flat, matches), status_code=409)
    targets = matches if replace_all else [matches[0]]

    # ---- validate ALL targets before mutating anything ----
    protected: list[dict] = []
    for t in targets:
        if t["kind"] == "cross":
            if op != "delete":
                raise ServiceError(
                    "old_str spans multiple blocks; only deletion (empty new_str) is "
                    "supported across blocks. Narrow old_str to one block to replace/append, "
                    "or use the block-id tools for structural changes.",
                    status_code=400,
                )
            if not allow_cross:
                ids = ", ".join(flat[i]["id"] for i in t["idxs"])
                raise ServiceError(
                    f"old_str can only be satisfied by spanning {len(t['idxs'])} blocks "
                    f"({ids}) — a multi-block range delete. Set allow_cross_block=true on this "
                    "content_updates item to confirm, or use notion_delete_blocks by id.",
                    status_code=400,
                )
            protected += _collect_protected(c, flat, t["idxs"])
        elif op == "delete" and t.get("whole_block"):
            protected += _collect_protected(c, flat, t["idxs"])
    if op == "delete" and protected and not allow_deleting:
        raise ServiceError(_child_guard_msg(protected), status_code=400)

    # ---- apply ----
    if op == "append":
        remainder = new[len(old):].strip("\n")
        blocks = markdown_to_blocks(remainder)
        anchors = []
        for t in targets:
            anchor_id = flat[t["idxs"][-1]]["id"]
            c.append_children(pid, blocks, after=anchor_id)
            anchors.append(anchor_id)
        return {"op": "append", "anchors": anchors, "added_blocks": len(blocks)}

    if op == "delete":
        # Whole-block (or cross) -> delete the block(s); partial -> splice it out.
        del_idxs: set[int] = set()
        spliced: list[str] = []
        for t in targets:
            if t["kind"] == "cross" or t.get("whole_block"):
                del_idxs.update(t["idxs"])
            else:
                i = t["idxs"][0]
                bid = flat[i]["id"]
                block = c.retrieve_block(bid)
                c.update_block(bid, _rebuild_rich_text_update(block, t["char_start"], t["char_end"], ""))
                spliced.append(bid)
        deleted = []
        for i in sorted(del_idxs, reverse=True):  # bottom-up keeps positions stable
            c.delete_block(flat[i]["id"])
            deleted.append(flat[i]["id"])
        return {"op": "delete", "deleted_blocks": deleted, "spliced_blocks": spliced}

    # in-place replace (single-block targets only)
    blocks_changed = []
    for t in targets:
        i = t["idxs"][0]
        bid = flat[i]["id"]
        block = c.retrieve_block(bid)
        c.update_block(bid, _rebuild_rich_text_update(block, t["char_start"], t["char_end"], new))
        blocks_changed.append(bid)
    return {"op": "replace", "blocks": blocks_changed}


def _apply_icon_cover(body: dict, icon: str | None, cover: str | None) -> None:
    if icon is not None:
        if icon == "none":
            body["icon"] = None
        elif icon.startswith("http"):
            body["icon"] = {"type": "external", "external": {"url": icon}}
        else:
            body["icon"] = {"type": "emoji", "emoji": icon}
    if cover is not None:
        body["cover"] = None if cover == "none" else {"type": "external", "external": {"url": cover}}


def op_move_pages(settings: Settings, *, page_or_database_ids: list[str], new_parent: dict, **_ignored) -> dict:
    parent_body = _parent_body(new_parent)
    with NotionClient(settings) as c:
        moved, errors = [], []
        for raw in page_or_database_ids:
            pid = extract_id(raw)
            try:
                c.update_page(pid, {"parent": parent_body})
                moved.append(pid)
            except ServiceError as e:
                errors.append({"id": pid, "error": e.message})
        return {"moved": moved, "errors": errors}


def op_duplicate_page(settings: Settings, *, page_id: str, **_ignored) -> dict:
    pid = extract_id(page_id)
    with NotionClient(settings) as c:
        src = c.retrieve_page(pid)
        parent = src.get("parent", {})
        if parent.get("type") == "page_id":
            new_parent = {"type": "page_id", "page_id": parent["page_id"]}
            props = {"title": {"title": _inline_to_rich(page_title(src) + " (copy)")}}
        elif parent.get("type") == "database_id":
            new_parent = {"type": "database_id", "database_id": parent["database_id"]}
            props = src.get("properties", {})
        else:
            raise ServiceError("Can only duplicate pages under a page or database.", status_code=400)
        children = c.block_children_all(pid)
        clean = [_strip_block(b) for b in children if b.get("type") not in ("child_page", "child_database")]
        body: dict = {"parent": new_parent, "properties": props}
        if clean:
            body["children"] = clean[:100]
        new_page = c.create_page(body)
        return {"duplicated": True, "new_page_id": new_page.get("id"), "url": new_page.get("url"),
                "note": "Best-effort shallow copy (top-level blocks; child pages/databases skipped)."}


def _strip_block(block: dict) -> dict:
    t = block.get("type")
    data = block.get(t, {})
    keep = {"object": "block", "type": t, t: {}}
    if isinstance(data, dict) and "rich_text" in data:
        keep[t]["rich_text"] = data.get("rich_text", [])
        if t == "to_do":
            keep[t]["checked"] = data.get("checked", False)
        if t == "code":
            keep[t]["language"] = data.get("language", "plain text")
    elif t == "divider":
        keep[t] = {}
    else:
        keep = {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [_text_obj(block_to_markdown(block))]}}
    return keep


def op_create_comment(settings: Settings, *, page_id: str | None = None, markdown: str | None = None,
                      rich_text: list | None = None, discussion_id: str | None = None,
                      selection_with_ellipsis: str | None = None, **_ignored) -> dict:
    if selection_with_ellipsis:
        raise ServiceError(
            "Commenting on a specific selection is not supported by the Notion REST "
            "API; omit selection_with_ellipsis for a page-level comment.",
            status_code=501,
        )
    rt = rich_text if rich_text else _inline_to_rich(markdown or "")
    body: dict = {"rich_text": rt}
    if discussion_id:
        body["discussion_id"] = discussion_id.split("/")[-1]
    elif page_id:
        body["parent"] = {"page_id": extract_id(page_id)}
    else:
        raise ServiceError("Provide page_id or discussion_id.", status_code=400)
    with NotionClient(settings) as c:
        res = c.create_comment(body)
        return {"created": True, "comment_id": res.get("id"), "discussion_id": res.get("discussion_id")}


def op_get_comments(settings: Settings, *, page_id: str, **_ignored) -> dict:
    with NotionClient(settings) as c:
        data = c.list_comments(extract_id(page_id))
        comments = [
            {"id": cm.get("id"), "discussion_id": cm.get("discussion_id"),
             "text": rich_text_plain(cm.get("rich_text")),
             "created_time": cm.get("created_time")}
            for cm in data.get("results", [])
        ]
        return {"comments": comments,
                "note": "Notion REST returns open (unresolved) comments only."}


def op_get_users(settings: Settings, *, query: str | None = None, user_id: str | None = None,
                 page_size: int = 100, start_cursor: str | None = None, **_ignored) -> dict:
    with NotionClient(settings) as c:
        if user_id:
            uid = "me" if user_id == "self" else user_id
            u = c.get_user(uid)
            return {"user": _fmt_user(u)}
        data = c.list_users(page_size=min(page_size, 100), start_cursor=start_cursor)
        users = [_fmt_user(u) for u in data.get("results", [])]
        if query:
            q = query.lower()
            users = [u for u in users if q in (u.get("name") or "").lower() or q in (u.get("email") or "").lower()]
        return {"users": users, "next_cursor": data.get("next_cursor"), "has_more": data.get("has_more", False)}


def _fmt_user(u: dict) -> dict:
    return {"id": u.get("id"), "name": u.get("name"), "type": u.get("type"),
            "email": (u.get("person") or {}).get("email")}


def op_get_teams(settings: Settings, **_ignored) -> dict:
    raise ServiceError(
        "Teamspaces are not exposed by the Notion REST API, so this connector "
        "tool cannot be mirrored. (Reads/writes of pages and databases work.)",
        status_code=501,
    )


def op_create_view(settings: Settings, **_ignored) -> dict:
    raise ServiceError(
        "Database views are not exposed by the Notion REST API; create-view cannot "
        "be mirrored. Use query-database for filtered reads instead.",
        status_code=501,
    )


def op_update_view(settings: Settings, **_ignored) -> dict:
    raise ServiceError(
        "Database views are not exposed by the Notion REST API; update-view cannot "
        "be mirrored.",
        status_code=501,
    )


# ---- create / update database (SQL DDL subset) ----
def op_create_database(settings: Settings, *, schema: str, title: str | None = None,
                       description: str | None = None, parent: dict | None = None, **_ignored) -> dict:
    props = _parse_create_table(schema)
    if not props:
        raise ServiceError("Could not parse any columns from the schema DDL.", status_code=400)
    with NotionClient(settings) as c:
        if not parent or not parent.get("page_id"):
            raise ServiceError("create-database requires a parent page_id.", status_code=400)
        body: dict = {
            "parent": {"type": "page_id", "page_id": extract_id(parent["page_id"])},
            "properties": props,
        }
        if title:
            body["title"] = _inline_to_rich(title)
        if description:
            body["description"] = _inline_to_rich(description)
        db = c.create_database(body)
        return {"created": True, "database_id": db.get("id"), "url": db.get("url"),
                "data_source_id": db.get("id")}


def op_update_data_source(settings: Settings, *, data_source_id: str, statements: str | None = None,
                          title: str | None = None, description: str | None = None,
                          in_trash: bool | None = None, **_ignored) -> dict:
    with NotionClient(settings) as c:
        body: dict = {}
        if title:
            body["title"] = _inline_to_rich(title)
        if description:
            body["description"] = _inline_to_rich(description)
        if in_trash is not None:
            body["archived"] = in_trash
        if statements:
            body["properties"] = _parse_alter_statements(statements)
        if not body:
            raise ServiceError("Nothing to update.", status_code=400)
        db = c.update_database(extract_id(data_source_id), body)
        return {"updated": True, "database_id": db.get("id")}


_TYPE_MAP = {
    "TITLE": {"title": {}}, "RICH_TEXT": {"rich_text": {}}, "DATE": {"date": {}},
    "PEOPLE": {"people": {}}, "CHECKBOX": {"checkbox": {}}, "URL": {"url": {}},
    "EMAIL": {"email": {}}, "PHONE_NUMBER": {"phone_number": {}}, "STATUS": {"status": {}},
    "FILES": {"files": {}}, "CREATED_TIME": {"created_time": {}},
    "LAST_EDITED_TIME": {"last_edited_time": {}}, "NUMBER": {"number": {"format": "number"}},
}
_COLOR = {"default", "gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red"}


def _column_to_property(type_expr: str) -> dict | None:
    t = type_expr.strip()
    up = t.upper()
    for key, val in _TYPE_MAP.items():
        if up.startswith(key):
            return dict(val)
    m = re.match(r"(MULTI_SELECT|SELECT)\s*\((.*)\)", t, re.IGNORECASE)
    if m:
        kind = "multi_select" if m.group(1).upper() == "MULTI_SELECT" else "select"
        options = []
        for opt in re.findall(r"'([^']+)'(?::(\w+))?", m.group(2)):
            name, color = opt[0], opt[1] if opt[1] in _COLOR else "default"
            options.append({"name": name, "color": color})
        return {kind: {"options": options}}
    return None


def _parse_create_table(ddl: str) -> dict:
    m = re.search(r"\((.*)\)", ddl, re.DOTALL)
    inner = m.group(1) if m else ddl
    props: dict = {}
    for col in _split_columns(inner):
        cm = re.match(r'\s*"([^"]+)"\s+(.*)', col, re.DOTALL)
        if not cm:
            continue
        name, type_expr = cm.group(1), cm.group(2)
        prop = _column_to_property(type_expr)
        if prop:
            props[name] = prop
    if not any("title" in p for p in props.values()):
        props = {"Name": {"title": {}}, **props}
    return props


def _parse_alter_statements(statements: str) -> dict:
    props: dict = {}
    for stmt in statements.split(";"):
        s = stmt.strip()
        if not s:
            continue
        add = re.match(r'ADD COLUMN\s+"([^"]+)"\s+(.*)', s, re.IGNORECASE | re.DOTALL)
        drop = re.match(r'DROP COLUMN\s+"([^"]+)"', s, re.IGNORECASE)
        rename = re.match(r'RENAME COLUMN\s+"([^"]+)"\s+TO\s+"([^"]+)"', s, re.IGNORECASE)
        alter = re.match(r'ALTER COLUMN\s+"([^"]+)"\s+SET\s+(.*)', s, re.IGNORECASE | re.DOTALL)
        if add:
            prop = _column_to_property(add.group(2))
            if prop:
                props[add.group(1)] = prop
        elif drop:
            props[drop.group(1)] = None
        elif rename:
            props[rename.group(1)] = {"name": rename.group(2)}
        elif alter:
            prop = _column_to_property(alter.group(2))
            if prop:
                props[alter.group(1)] = prop
    return props


def _split_columns(inner: str) -> list[str]:
    cols, depth, buf = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        cols.append(buf)
    return cols


# ===========================================================================
# build_brief — authoritative filtered read (port of scripts/notion_query.py)
# ===========================================================================
def build_brief(settings: Settings) -> dict:
    if not settings.projects_db_id or not settings.actions_db_id:
        raise ServiceError("PROJECTS_DB_ID / ACTIONS_DB_ID are not configured.", status_code=503)

    with NotionClient(settings) as c:
        all_projects = c.query_database_all(settings.projects_db_id)

        proj_name: dict[str, str] = {}
        proj_status: dict[str, str | None] = {}
        for p in all_projects:
            props = p["properties"]
            proj_name[p["id"]] = _title_or(props, "Project") or "(untitled project)"
            proj_status[p["id"]] = (props.get("Status", {}).get("select") or {}).get("name")

        def project_ids(action: dict) -> list[str]:
            rel = (action["properties"].get("Project") or {}).get("relation") or []
            return [r["id"] for r in rel]

        def project_labels(action: dict) -> str:
            names = [proj_name.get(i, "?") for i in project_ids(action)]
            return ", ".join(names) if names else "(no project)"

        def qualifies(action: dict) -> bool:
            ids = project_ids(action)
            if not ids:
                return True
            return any(proj_status.get(i) in NEXT_PROJECT_STATUSES for i in ids)

        active = [p for p in all_projects if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "Active"]
        someday_p = [p for p in all_projects if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "Someday"]

        next_raw = c.query_database_all(settings.actions_db_id, {
            "property": "Action Status", "select": {"equals": "Next"}
        })
        next_actions = [a for a in next_raw if qualifies(a)]
        someday_a = c.query_database_all(settings.actions_db_id, {
            "property": "Action Status", "select": {"equals": "Someday"}
        })

    return {
        "ACTIVE_PROJECTS": [
            {"name": _title_or(p["properties"], "Project"),
             "direction": rich_text_plain(p["properties"].get("Direction", {}).get("rich_text"))}
            for p in active
        ],
        "NEXT_ACTIONS": [
            {"name": _title_or(a["properties"], "Name"), "project": project_labels(a)}
            for a in next_actions
        ],
        "SOMEDAY_PROJECTS": [_title_or(p["properties"], "Project") for p in someday_p],
        "SOMEDAY_ACTIONS": [
            {"name": _title_or(a["properties"], "Name"), "project": project_labels(a)}
            for a in someday_a
        ],
    }


def _title_or(props: dict, name: str) -> str:
    return rich_text_plain((props.get(name) or {}).get("title"))


def _safe_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("message", data))[:300]
    except Exception:
        return resp.text[:300]
