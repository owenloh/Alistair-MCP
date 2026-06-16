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

import re
from typing import Any

import httpx

from . import ServiceError
from ..config import Settings

API = "https://api.notion.com/v1/"
_TIMEOUT = httpx.Timeout(40.0)

# Caps so a fetch of a huge page can't blow up the response.
_MAX_CHILD_DEPTH = 2
_MAX_TOTAL_BLOCKS = 300

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
    r"(\*\*(?P<bold>.+?)\*\*)"
    r"|(?<!\*)\*(?P<italic>[^*]+?)\*(?!\*)"
    r"|(`(?P<code>[^`]+?)`)"
    r"|(\[(?P<ltext>[^\]]+?)\]\((?P<lurl>[^)]+?)\))"
)


def _inline_to_rich(text: str) -> list[dict]:
    """Parse a subset of inline markdown (**bold**, *italic*, `code`, [t](url))."""
    out: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(_text_obj(text[pos:m.start()]))
        if m.group("bold") is not None:
            out.append(_text_obj(m.group("bold"), bold=True))
        elif m.group("italic") is not None:
            out.append(_text_obj(m.group("italic"), italic=True))
        elif m.group("code") is not None:
            out.append(_text_obj(m.group("code"), code=True))
        elif m.group("ltext") is not None:
            out.append(_text_obj(m.group("ltext"), link=m.group("lurl")))
        pos = m.end()
    if pos < len(text):
        out.append(_text_obj(text[pos:]))
    return out or [_text_obj("")]


def _text_obj(content: str, bold=False, italic=False, code=False, link: str | None = None) -> dict:
    ann = {}
    if bold:
        ann["bold"] = True
    if italic:
        ann["italic"] = True
    if code:
        ann["code"] = True
    obj: dict = {"type": "text", "text": {"content": content}}
    if link:
        obj["text"]["link"] = {"url": link}
    if ann:
        obj["annotations"] = ann
    return obj


def markdown_to_blocks(md: str) -> list[dict]:
    """Convert a markdown string into Notion block objects (common types)."""
    blocks: list[dict] = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "plain text"
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "object": "block", "type": "code",
                "code": {"rich_text": [_text_obj("\n".join(code_lines))], "language": lang},
            })
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif stripped.startswith("#### "):
            blocks.append(_heading(3, stripped[5:]))
        elif stripped.startswith("### "):
            blocks.append(_heading(3, stripped[4:]))
        elif stripped.startswith("## "):
            blocks.append(_heading(2, stripped[3:]))
        elif stripped.startswith("# "):
            blocks.append(_heading(1, stripped[2:]))
        elif re.match(r"^[-*] \[[ xX]\] ", stripped):
            checked = stripped[3] in ("x", "X")
            blocks.append({
                "object": "block", "type": "to_do",
                "to_do": {"rich_text": _inline_to_rich(stripped[6:]), "checked": checked},
            })
        elif stripped.startswith(("- ", "* ")):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _inline_to_rich(stripped[2:])},
            })
        elif re.match(r"^\d+\. ", stripped):
            content = re.sub(r"^\d+\. ", "", stripped)
            blocks.append({
                "object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _inline_to_rich(content)},
            })
        elif stripped.startswith("> "):
            blocks.append({
                "object": "block", "type": "quote",
                "quote": {"rich_text": _inline_to_rich(stripped[2:])},
            })
        else:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _inline_to_rich(stripped)},
            })
        i += 1
    return blocks


def _heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _inline_to_rich(text)}}


def block_to_markdown(block: dict) -> str:
    """Render a single block to a markdown-ish line (for fetch + anchor matching)."""
    t = block.get("type", "")
    data = block.get(t, {})
    text = rich_text_plain(data.get("rich_text")) if isinstance(data, dict) else ""
    if t == "heading_1":
        return f"# {text}"
    if t == "heading_2":
        return f"## {text}"
    if t == "heading_3":
        return f"### {text}"
    if t == "bulleted_list_item":
        return f"- {text}"
    if t == "numbered_list_item":
        return f"1. {text}"
    if t == "to_do":
        return f"- [{'x' if data.get('checked') else ' '}] {text}"
    if t == "quote":
        return f"> {text}"
    if t == "code":
        return f"```\n{text}\n```"
    if t == "divider":
        return "---"
    if t == "child_page":
        return f"[child page: {data.get('title', '')}]"
    if t == "child_database":
        return f"[child database: {data.get('title', '')}]"
    return text


def render_blocks(client: NotionClient, block_id: str, depth: int, counter: dict) -> tuple[str, list[dict]]:
    """Recursively render block children to (markdown, flat block list with ids)."""
    md_parts: list[str] = []
    flat: list[dict] = []
    for blk in client.block_children_all(block_id):
        if counter["n"] >= _MAX_TOTAL_BLOCKS:
            md_parts.append("\n_(truncated: page has more blocks)_")
            break
        counter["n"] += 1
        line = block_to_markdown(blk)
        md_parts.append(line)
        flat.append({
            "id": blk["id"],
            "type": blk.get("type"),
            "text": rich_text_plain((blk.get(blk.get("type"), {}) or {}).get("rich_text")) if isinstance(blk.get(blk.get("type")), dict) else "",
            "has_children": blk.get("has_children", False),
        })
        if blk.get("has_children") and depth < _MAX_CHILD_DEPTH and blk.get("type") not in ("child_page", "child_database"):
            sub_md, sub_flat = render_blocks(client, blk["id"], depth + 1, counter)
            if sub_md:
                md_parts.append(_indent(sub_md))
            flat.extend(sub_flat)
    return "\n".join(md_parts), flat


def _indent(text: str) -> str:
    return "\n".join("    " + ln for ln in text.split("\n"))


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
    # Gather expanded date parts first.
    date_parts: dict[str, dict] = {}
    simple: dict[str, Any] = {}
    for key, value in props_in.items():
        if key.startswith("date:"):
            _, pname, part = key.split(":", 2)
            date_parts.setdefault(pname, {})[part] = value
        else:
            name = key[len("userDefined:"):] if key.startswith("userDefined:") else key
            simple[name] = value

    out: dict = {}
    for pname, parts in date_parts.items():
        d: dict = {}
        if parts.get("start") is not None:
            d["start"] = parts["start"]
        if parts.get("end"):
            d["end"] = parts["end"]
        out[pname] = {"date": d or None}

    for name, value in simple.items():
        ptype = (schema.get(name, {}).get("type") if schema else None)
        out[name] = _coerce_property(ptype, value)
    return out


def _coerce_property(ptype: str | None, value: Any) -> dict:
    # Title / unknown text falls back to title or rich_text based on schema type.
    if value is None:
        return {ptype: None} if ptype else {"rich_text": []}
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
        return {"select": {"name": str(value)}}
    if ptype == "status":
        return {"status": {"name": str(value)}}
    if ptype == "multi_select":
        vals = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return {"multi_select": [{"name": str(v)} for v in vals]}
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
        return {"people": [{"id": i} for i in ids]}
    if ptype == "relation":
        ids = value if isinstance(value, list) else [value]
        return {"relation": [{"id": i} for i in ids]}
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


def op_fetch(settings: Settings, *, id: str, include_discussions: bool = False, **_ignored) -> dict:
    nid = extract_id(id)
    with NotionClient(settings) as c:
        # Try page first, then database.
        try:
            page = c.retrieve_page(nid)
        except ServiceError:
            page = None
        if page and page.get("object") == "page":
            counter = {"n": 0}
            md, flat = render_blocks(c, nid, 0, counter)
            return {
                "object": "page",
                "id": nid,
                "url": page.get("url"),
                "title": page_title(page),
                "properties": flatten_properties(page.get("properties", {})),
                "content_markdown": md,
                "blocks": flat,
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
            results = [_apply_content_update(c, pid, flat, u) for u in content_updates]
            return {"updated": True, "command": command, "operations": results}

        if command == "replace_content":
            if new_str is None:
                raise ServiceError("replace_content requires 'new_str'.", status_code=400)
            children = c.block_children_all(pid)
            has_child_page = any(b.get("type") in ("child_page", "child_database") for b in children)
            if has_child_page and not allow_deleting_content:
                raise ServiceError(
                    "replace_content would delete child pages/databases. Set "
                    "allow_deleting_content=true to proceed.",
                    status_code=400,
                )
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


def _apply_content_update(c: NotionClient, pid: str, flat: list[dict], update: dict) -> dict:
    old = update.get("old_str", "")
    new = update.get("new_str", "")
    if not old:
        raise ServiceError("Each content update needs a non-empty old_str.", status_code=400)

    anchor = _find_anchor_block(flat, old)
    # Append case: new_str extends old_str -> append the remainder after the anchor.
    if new.startswith(old) and len(new) > len(old):
        remainder = new[len(old):].strip("\n")
        if not anchor:
            raise ServiceError(f"Could not find old_str anchor to append after: {old[:60]!r}", status_code=400)
        blocks = markdown_to_blocks(remainder)
        c.append_children(pid, blocks, after=anchor["id"])
        return {"op": "append", "after": anchor["id"], "added_blocks": len(blocks)}
    # Delete case.
    if new.strip() == "":
        targets = _find_span_blocks(flat, old)
        for b in targets:
            c.delete_block(b["id"])
        return {"op": "delete", "deleted_blocks": [b["id"] for b in targets]}
    # In-place replace (single block).
    if anchor:
        c.update_block(anchor["id"], _block_text_update(anchor, new))
        return {"op": "replace", "block": anchor["id"]}
    raise ServiceError(f"Could not locate old_str to update: {old[:60]!r}", status_code=400)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _find_anchor_block(flat: list[dict], old: str) -> dict | None:
    """Find the block matching the LAST non-empty line of old_str."""
    last_line = ""
    for ln in reversed(old.split("\n")):
        if ln.strip():
            last_line = _norm(ln)
            break
    if not last_line:
        return None
    for b in flat:
        if _norm(b.get("text", "")) and (last_line in _norm(b.get("text", "")) or _norm(b.get("text", "")) in last_line):
            anchor = b
        # keep the last match
    anchor = None
    for b in flat:
        bt = _norm(b.get("text", ""))
        if bt and (last_line in bt or bt in last_line):
            anchor = b
    return anchor


def _find_span_blocks(flat: list[dict], old: str) -> list[dict]:
    lines = [_norm(ln) for ln in old.split("\n") if ln.strip()]
    out = []
    for b in flat:
        bt = _norm(b.get("text", ""))
        if bt and any(ln in bt or bt in ln for ln in lines):
            out.append(b)
    return out


def _block_text_update(block: dict, new_text: str) -> dict:
    t = block.get("type", "paragraph")
    return {t: {"rich_text": _inline_to_rich(new_text)}}


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
            "and": [
                {"property": "Action Status", "select": {"equals": "Next"}},
                {"property": "Checkbox", "checkbox": {"equals": False}},
            ]
        })
        next_actions = [a for a in next_raw if qualifies(a)]
        someday_a = c.query_database_all(settings.actions_db_id, {
            "and": [
                {"property": "Action Status", "select": {"equals": "Someday"}},
                {"property": "Checkbox", "checkbox": {"equals": False}},
            ]
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
