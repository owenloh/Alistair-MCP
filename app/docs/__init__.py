"""Served documentation (specs) — each exposed BOTH as an MCP resource AND an
equivalent tool.

Rationale: no MCP client reliably auto-loads `resources` (Gemini/voice ignore
them entirely), but resource-capable clients (claude.ai, Claude Desktop, Cursor)
render them cleanly. So every doc here is reachable two ways — the resource for
clients that support it, an equivalent tool for everything else — and the model
gets the same text regardless. `register_docs(mcp)` in app.mcp_server wires both
from this one registry.
"""
from __future__ import annotations

NOTION_MARKDOWN_SPEC = r"""# Alistair Notion-flavored Markdown spec

This is the EXACT markdown dialect Alistair's Notion write tools accept and the
read tools emit. Read it before composing any markdown for `notion_create_pages`,
`notion_update_page` (insert_content / update_content / replace_content), or
`notion_append_blocks` body text. Do NOT guess syntax — anything not listed here
is treated as a paragraph.

Two ways to write structure:
- **Markdown** (this spec) — for `notion_create_pages` / `insert_content`. Round-trips.
- **Typed block objects** — for `notion_append_blocks` (native nesting, no markdown).
  Prefer the block-id tools for STRUCTURAL changes (nesting, reorder, delete by id).

## Blocks

### Headings
`# H1`, `## H2`, `### H3`. Notion has three heading levels — do not use `####`.
A collapsible (toggle) heading: `## Heading {toggle="true"}` — the heading collapses
the blocks indented under it.

### Paragraphs
Any plain line is a paragraph. An explicitly empty/blank block: `<empty-block/>`.

### Bulleted list
`- item` (or `* item`). Nest with TABS (see Nesting).

### Numbered list
`1. item` (any number; Notion renumbers).

### To-do (checkbox list)
`- [ ] open item` and `- [x] done item`.

### Quote
`> quoted text`.

### Divider
`---` on its own line (also `***` or `___`).

### Code block
```` ```language ````  … fenced lines …  ```` ``` ````. Language defaults to
"plain text". Content inside the fence is literal (not parsed).

### Equation (block)
A line `$$` then the LaTeX on following lines, then a closing `$$`.

### Image
`![caption](https://url)` — caption optional.

### Media (video/audio/file/pdf)
`<video src="https://url">caption</video>` (also `<audio>`, `<file>`, `<pdf>`).

### Table of contents
`<table_of_contents/>`.

## Toggles + Nesting (READ THIS — the #1 source of mistakes)

A toggle is a real container block. Write it as:

```
<details>
<summary>Toggle title (inline markdown allowed)</summary>
	First child block — indent children with ONE TAB per nesting level
	- a bullet inside the toggle
		- a nested bullet (TWO tabs)
</details>
```

Rules:
- Indent children with **TABS** — one tab per level. Tabs are canonical and
  unambiguous. (Leading spaces are tolerated and auto-converted, but a toggle whose
  children you indent inconsistently can nest wrong — use tabs.)
- The first `<summary>…</summary>` is the toggle's title; everything indented under
  it (until `</details>`) is its children, which may themselves be any block,
  including nested toggles.
- Optional color: `<details color="blue">`.
- A write of a toggle WITH children produces a real toggle block containing those
  children — never a flattened paragraph. If you only need a flat title, still use a
  toggle block, just with no indented children.

Generic nesting (not just toggles): any paragraph / list item / quote / callout /
toggle / heading may carry child blocks by indenting the children one tab deeper:

```
- parent bullet
	- child bullet
		- grandchild bullet
```

## Callouts

```
<callout icon="💡" color="yellow_background">
	Callout text (first indented line is the callout's own text)
	- an optional child block
</callout>
```

`icon` may be an emoji or an https URL. `color` is optional.

## Columns

```
<columns>
	<column>
		left content
	</column>
	<column>
		right content
	</column>
</columns>
```

Notion requires ≥2 columns.

## Tables

```
<table header-row="true">
	<tr>
		<td>A</td>
		<td>B</td>
	</tr>
	<tr>
		<td>1</td>
		<td>2</td>
	</tr>
</table>
```

`header-row="true"` / `header-column="true"` are optional.

## Inline formatting

- `**bold**`, `*italic*`, `***bold italic***`, `~~strikethrough~~`, `` `code` ``
- Link: `[text](https://url)`
- Underline / color span: `<span underline="true">x</span>`,
  `<span color="red">y</span>` (color may be a text color like `red` or a background
  like `red_background`).
- Line break inside a block: `<br>`.
- Inline math: `` $`E=mc^2`$ `` (dollar-backtick-LaTeX-backtick-dollar). Do NOT use
  plain `$…$`; it fails near **bold**. Keep equations out of bold lead-ins.
- Mentions: `<mention-date start="2026-06-24"/>`,
  `<mention-date start="2026-06-24" startTime="14:30" timeZone="Europe/London"/>`,
  `<mention-user url="https://notion.so/…">Name</mention-user>`,
  `<mention-page url="…"></mention-page>`.
- Link to an existing page as a block: `<page url="https://notion.so/…"></page>`.

## Colors

Block color attribute: append `{color="red"}` (or `{color="blue_background"}`) to a
line — e.g. `> a quote {color="gray"}`, `- bullet {color="green_background"}`.
Palette: default, gray, brown, orange, yellow, green, blue, purple, pink, red, and
their `_background` variants.

## Escaping
Backslash-escape a leading block marker to keep it literal text: `\# not a heading`,
`\- not a bullet`, `\$5`, `\<tag\>`.
"""

# Registry: uri -> served doc. `tool` is the equivalent tool name (registered
# separately); `text` is the body served by both the resource and the tool.
DOCS: dict[str, dict] = {
    "alistair://docs/notion-markdown-spec": {
        "name": "Notion markdown spec",
        "description": "The exact Notion-flavored markdown dialect Alistair reads/writes.",
        "mime": "text/markdown",
        "tool": "notion_markdown_spec",
        "text": NOTION_MARKDOWN_SPEC,
    },
}


def get_doc(uri: str) -> str | None:
    entry = DOCS.get(uri)
    return entry["text"] if entry else None
