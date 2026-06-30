"""Verbatim (or near-verbatim) descriptions of Claude's Notion connector tools.

These are copied from the live connector tool schemas so a voice-mode Claude
reading this API's OpenAPI/manifest behaves the same way it would with the real
connector. Kept here to keep the router file readable.
"""

SEARCH = (
    "Perform a search over:\n"
    "- \"internal\": Semantic search over Notion workspace and connected sources "
    "(Slack, Google Drive, Github, Jira, Microsoft Teams, Sharepoint, OneDrive, "
    "Linear). Supports filtering by creation date and creator.\n"
    "- \"user\": Search for users by name or email.\n\n"
    "Auto-selects AI search (with connected sources) or workspace search "
    "(workspace-only, faster). Use the \"fetch\" tool for full page/database "
    "contents after getting search results. For Notion results, pass the result's "
    "\"id\" field to the fetch tool's \"id\" param. Set page_size (default 10, max 25) "
    "as low as possible to minimize response size.\n"
    "To search within a database: first fetch the database to get the data source "
    "URL (collection://...), then use that as data_source_url.\n\n"
    "[Workaround note: backed by the Notion REST search API — searches Notion "
    "workspace content, ranked by relevance.]"
)

FETCH = (
    "Retrieves details about a Notion entity (page, database, or data source) by "
    "URL or ID. Provide the URL or ID in the `id` parameter. Pages are returned in "
    "Markdown format (here: rendered `content_markdown` plus a `blocks` list giving "
    "each block's id, type and text so you can target writes). Databases return "
    "their schema/properties. Set `include_discussions` to true to see discussion "
    "markers.\n"
    "<example>{\"id\": \"https://notion.so/workspace/Page-a1b2c3d4e5f67890\"}</example>\n"
    "<example>{\"id\": \"12345678-90ab-cdef-1234-567890abcdef\"}</example>"
)

CREATE_PAGES = (
    "## Overview\nCreates one or more Notion pages, with the specified properties "
    "and content.\n## Parent\nAll pages created with a single call will have the "
    "same parent. The parent can be a Notion page (\"page_id\") or a data source "
    "(\"data_source_id\"). If the parent is omitted, the pages are created as "
    "standalone, workspace-level private pages.\nIf you have a database URL, ALWAYS "
    "pass it to the \"fetch\" tool first to get the schema and the data source IDs. "
    "Use data_source_id (collection://<data_source_id>) for database rows; page_id "
    "is only for regular, non-database pages.\n## Content\nNotion page content is a "
    "string in Notion-flavored Markdown format. Don't include the page title at the "
    "top of the content; only include it under \"properties\".\n## Properties\n"
    "Notion page properties are a JSON map of property names to values. When "
    "creating pages in a database, use the correct property names from the data "
    "source schema (always include the title property). Values are flat: select and "
    "status take the option name as a plain string (e.g. \"Done\"), multi-select takes "
    "a list of names, number takes a number, checkbox \"__YES__\"/\"__NO__\", relation "
    "and people a list of ids (the official Notion nested form is also accepted). For "
    "pages outside a database, the only allowed property is \"title\".\n[Read the Notion markdown spec "
    "first — MCP resource `alistair://docs/notion-markdown-spec` or `notion_markdown_spec`; "
    "do not guess markdown syntax, especially for toggles and nesting.]\n"
    "[Load the `notion-master` skill at least once this session before writing — "
    "get_skill('notion-master') — for the safe-write protocol and PARA/ID context. Do it "
    "for grounding, not because the write needs it; the call still works if you skip it.]"
)

UPDATE_PAGE = (
    "## Overview\nUpdate a Notion page's properties or content. Read the Notion "
    "markdown spec FIRST (MCP resource `alistair://docs/notion-markdown-spec`, or call "
    "`notion_markdown_spec`); do not guess markdown syntax. For STRUCTURAL changes "
    "(nesting, reordering, deleting specific/duplicate blocks) prefer the block-id "
    "tools (notion_list_blocks + notion_append_blocks/notion_update_block/"
    "notion_delete_blocks) — update_content is for in-block prose edits.\n## Properties\n"
    "For pages in a database, ALWAYS use the \"fetch\" tool first to get the schema "
    "and exact property names. Provide a non-null value to update a property; "
    "omitted properties are left unchanged. Date properties split into "
    "\"date:{property}:start\", \"date:{property}:end\" (optional) and "
    "\"date:{property}:is_datetime\" (0/1). Place properties split into "
    "\"place:{property}:name|address|latitude|longitude|google_place_id\". Number "
    "properties use numbers. Checkbox uses \"__YES__\"/\"__NO__\". Select and status "
    "properties take the option NAME as a plain string (e.g. \"Done\"), NOT a nested "
    "object; multi-select takes a list of names (or a comma-separated string); relation "
    "and people take a list of ids. (The official Notion nested form, e.g. {\"status\": "
    "{\"name\": \"Done\"}}, is also accepted, but the plain string is simplest.) "
    "Properties named \"id\" or \"url\" must be prefixed with \"userDefined:\".\n## Content\nNotion page "
    "content is Notion-flavored Markdown. Use \"insert_content\" to add content at the "
    "beginning or end of a page (if position is omitted, content is appended to the "
    "end). Before using \"update_content\", use \"fetch\" to get existing content and "
    "the exact snippets to use in old_str. Commands: update_properties, "
    "update_content, replace_content, insert_content, apply_template, "
    "update_verification.\n## update_content safety (fail-safe)\nEach content_updates "
    "item is {old_str, new_str, replace_all_matches?, allow_cross_block?, "
    "allow_deleting_content?}. If old_str matches MORE THAN ONCE, the call FAILS (409) "
    "with the count + a snippet per match unless replace_all_matches=true — it never "
    "silently picks one or deletes all. A single old_str must resolve within ONE block; "
    "if it would span/delete across blocks it FAILS (400) unless allow_cross_block=true "
    "(delete only). An edit that would remove a child page/database FAILS (400) listing "
    "them unless allow_deleting_content=true. An old_str→new_str that only appends is "
    "realized as an append after the matched block; an empty new_str deletes the matched "
    "block (or splices out a partial match). NEVER delete structure by text match — use "
    "notion_delete_blocks by id.\n### Preserving Child Pages\nFor \"replace_content\" "
    "and delete-by-text, if any child pages/databases (including nested) would be "
    "deleted the operation fails unless allow_deleting_content is set.\n[Workaround note: "
    "apply_template and update_verification are not available via the REST API.]\n"
    "[Load the `notion-master` skill at least once this session before writing — "
    "get_skill('notion-master') — for the safe-write protocol and PARA/ID context. Do it "
    "for grounding, not because the write needs it; the call still works if you skip it.]"
)

LIST_BLOCKS = (
    "List a Notion page's (or block's) children as structured blocks, each with its "
    "unique id, type, text, has_children, parent_id and depth. recursive=false returns "
    "one paginated page of direct children (pass the returned next_cursor back as "
    "start_cursor); recursive=true walks the whole subtree. Use the returned ids as "
    "deterministic handles for notion_append_blocks/notion_update_block/"
    "notion_delete_blocks/notion_move_blocks. Read-only."
)

APPEND_BLOCKS = (
    "Append typed Notion block objects under a parent block/page (native nesting, no "
    "markdown round-trip). blocks is a list of Notion REST block objects, e.g. "
    "{\"type\":\"paragraph\",\"paragraph\":{\"rich_text\":[{\"type\":\"text\",\"text\":"
    "{\"content\":\"hi\"}}]}} or a toggle with children "
    "{\"type\":\"toggle\",\"toggle\":{\"rich_text\":[...],\"children\":[...]}}. `after` "
    "places the new blocks after an existing child by id (otherwise appended at the "
    "end). To nest loose blocks into a toggle: append a toggle WITH its children here, "
    "then delete the old loose blocks by id with notion_delete_blocks. Read the markdown "
    "spec resource for block shapes; do not guess. "
    "[Load `notion-master` at least once this session before writing (get_skill('notion-master')) "
    "for the safe-write protocol — for grounding/context, not because the call needs it.]"
)

UPDATE_BLOCK = (
    "Update one Notion block in place by id. `block` is the block's type payload, e.g. "
    "{\"paragraph\":{\"rich_text\":[...]}} or {\"to_do\":{\"checked\":true}} (you may "
    "also pass a full {\"type\":...,...} block; the type payload is extracted). Use "
    "notion_list_blocks/notion_fetch to get the block id. Cannot change a block's type. "
    "[Load `notion-master` at least once this session before writing (get_skill('notion-master')) "
    "for the safe-write protocol — for grounding/context, not because the call needs it.]"
)

DELETE_BLOCKS = (
    "Delete specific Notion blocks by id — deterministic: ONLY the listed blocks are "
    "removed. THIS is the safe way to delete duplicates or specific blocks; NEVER delete "
    "by text match. block_ids is a list of block ids from notion_list_blocks/"
    "notion_fetch. Returns per-id success. If any block is or contains a child "
    "page/database the call FAILS (400) listing them unless allow_deleting_content=true. "
    "[Load `notion-master` at least once this session before writing (get_skill('notion-master')) "
    "for the safe-write protocol — for grounding/context, not because the call needs it.]"
)

MOVE_BLOCKS = (
    "Move blocks to sit after another block (reorder/restructure). block_ids are moved, "
    "in order, to directly after after_block_id (within after_block_id's parent). The "
    "REST API has no native move, so each block's full subtree is copied to the new "
    "position and the original deleted (children preserved; block ids change). Get ids "
    "from notion_list_blocks. "
    "[Load `notion-master` at least once this session before writing (get_skill('notion-master')) "
    "for the safe-write protocol — for grounding/context, not because the call needs it.]"
)

NOTION_MARKDOWN_SPEC = (
    "Return the Alistair Notion-flavored markdown spec (the exact dialect for headings, "
    "lists, dividers, toggles + nesting, callouts, tables, code, math, mentions and "
    "colors). Read this BEFORE composing any markdown for a Notion write; do not guess "
    "syntax. Same content as the MCP resource alistair://docs/notion-markdown-spec."
)

MOVE_PAGES = "Move one or more Notion pages or databases to a new parent."

DUPLICATE_PAGE = (
    "Duplicate a Notion page. The page must be within the current workspace, and "
    "you must have permission to access it. [Workaround note: this performs a "
    "best-effort shallow copy of properties and top-level blocks; child pages and "
    "databases are skipped.]"
)

CREATE_DATABASE = (
    "Creates a new Notion database using SQL DDL syntax. If no title property is "
    "provided, \"Name\" is auto-added. The schema param accepts a CREATE TABLE "
    "statement defining columns. Type syntax includes: TITLE, RICH_TEXT, DATE, "
    "PEOPLE, CHECKBOX, URL, EMAIL, PHONE_NUMBER, STATUS, FILES, NUMBER [FORMAT "
    "'dollar'], SELECT('opt':color, ...), MULTI_SELECT('opt':color, ...), "
    "CREATED_TIME, LAST_EDITED_TIME, UNIQUE_ID [PREFIX 'X']. Colors: default, gray, "
    "brown, orange, yellow, green, blue, purple, pink, red.\n"
    "<example>{\"title\": \"Tasks\", \"schema\": \"CREATE TABLE (\\\"Task Name\\\" TITLE, "
    "\\\"Status\\\" SELECT('To Do':red, 'Done':green), \\\"Due Date\\\" DATE)\"}</example>\n"
    "[Workaround note: RELATION/ROLLUP/FORMULA column types are not translated.]"
)

UPDATE_DATA_SOURCE = (
    "Update a Notion data source's schema, title, or attributes using SQL DDL "
    "statements. Accepts a data source ID (collection ID) or a single-source "
    "database ID. The statements param accepts semicolon-separated DDL: ADD COLUMN "
    "\"Name\" <type>; DROP COLUMN \"Name\"; RENAME COLUMN \"Old\" TO \"New\"; ALTER "
    "COLUMN \"Name\" SET <type>. Same type syntax as create_database."
)

CREATE_COMMENT = (
    "Add a comment to a page or specific content. Provide `page_id` to identify the "
    "page, then choose ONE targeting mode: page_id alone (page-level comment); "
    "page_id + selection_with_ellipsis (comment on specific block content); "
    "discussion_id (reply to an existing discussion thread). Provide exactly one "
    "content format: `markdown` (preferred, inline Notion-flavored Markdown) or "
    "`rich_text` (array of rich text objects).\n"
    "[Workaround note: page-level comments and discussion replies are supported; "
    "commenting on a specific selection is not exposed by the REST API.]"
)

GET_COMMENTS = (
    "Get comments and discussions from a Notion page. Returns discussions with "
    "comment content. By default, returns page-level discussions only. Parameters: "
    "include_all_blocks (discussions on child blocks), include_resolved, "
    "discussion_id (a specific discussion).\n"
    "[Workaround note: the REST API returns open/unresolved comments.]"
)

GET_USERS = (
    "Retrieves a list of users in the current workspace. Shows workspace members "
    "and guests with their IDs, names, emails (if available), and types (person or "
    "bot). Supports cursor-based pagination. Examples: list all users ({}); search "
    "by name or email ({\"query\": \"john\"}); fetch a specific user by ID; fetch the "
    "current user ({\"user_id\": \"self\"})."
)

GET_TEAMS = (
    "Retrieves a list of teams (teamspaces) in the current workspace. Shows which "
    "teams exist, user membership status, IDs, names, and roles.\n"
    "[Workaround note: teamspaces are not exposed by the Notion REST API; this "
    "endpoint returns 501.]"
)

CREATE_VIEW = (
    "Create a new view on a Notion database. Supported types: table, board, list, "
    "calendar, timeline, gallery, form, chart, map, dashboard. The optional "
    "\"configure\" param accepts a DSL for filters, sorts, grouping and display "
    "options.\n[Workaround note: database views are not exposed by the Notion REST "
    "API; this endpoint returns 501. Use query-database for filtered reads.]"
)

UPDATE_VIEW = (
    "Update a view's name, filters, sorts, or display configuration. The "
    "\"configure\" param uses the same DSL as create_view.\n[Workaround note: "
    "database views are not exposed by the Notion REST API; this endpoint returns "
    "501.]"
)

QUERY_DATABASE = (
    "Query a Notion database (data source) with native API filters and sorts, "
    "returning matching rows with flattened properties. This is the precise, "
    "status-correct way to read database rows (the connector's search/fetch cannot "
    "filter rows by property). Provide the database_id plus an optional Notion API "
    "`filter` object, `sorts` array, `page_size`, and `start_cursor`."
)

QUERY_BRIEF = (
    "Filtered read of the PARA Projects + Actions databases for the daily brief. "
    "Returns ACTIVE_PROJECTS, NEXT_ACTIONS, SOMEDAY_PROJECTS, SOMEDAY_ACTIONS using "
    "real Notion property filters (Next = Action Status Next, project "
    "Active/Complete or project-less; Someday = Action Status Someday; completed = "
    "Action Status Done, which these filters exclude by definition). "
    "This is the authoritative read the notion-master / daily-brief skills rely on. "
    "No body required."
)
