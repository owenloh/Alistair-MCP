"""Notion connector endpoints.

HTTP mirror of Claude's Notion connector — one POST endpoint per connector tool,
backed by the Notion REST API. Tool descriptions are copied (near-)verbatim from
the connector (see _notion_docs) so a voice-mode (HTTP-only) Claude calls the
same tools the same way. Request models are defined inline; each endpoint
forwards to an `op_*` function in app.services.notion.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..security import require_api_key
from ..services import notion as notion_service
from . import _notion_docs as docs

router = APIRouter(
    prefix="/api/notion",
    tags=["notion"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    query_type: str = "internal"
    data_source_url: str | None = None
    page_url: str | None = None
    teamspace_id: str | None = None
    filters: dict | None = None
    page_size: int = 10
    max_highlight_length: int | None = None
    content_search_mode: str | None = None


class FetchRequest(BaseModel):
    id: str
    include_discussions: bool = False
    # Pagination for long pages: pass next_cursor back as start_cursor to read on.
    start_cursor: str | None = None
    page_size: int = 100


class PageSpec(BaseModel):
    content: str | None = None
    properties: dict | None = None
    icon: str | None = None
    cover: str | None = None
    template_id: str | None = None


class CreatePagesRequest(BaseModel):
    pages: list[PageSpec]
    parent: dict | None = None


class UpdatePageRequest(BaseModel):
    page_id: str
    command: str
    properties: dict | None = None
    content: str | None = None
    content_updates: list[dict] | None = None
    new_str: str | None = None
    position: dict | None = None
    after_block_id: str | None = None  # wrapper extension: target a precise insert
    allow_deleting_content: bool = False
    icon: str | None = None
    cover: str | None = None
    template_id: str | None = None
    verification_status: str | None = None
    verification_expiry_days: int | None = None


class MovePagesRequest(BaseModel):
    page_or_database_ids: list[str]
    new_parent: dict


class DuplicatePageRequest(BaseModel):
    page_id: str


class CreateDatabaseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_ddl: str = Field(alias="schema")
    title: str | None = None
    description: str | None = None
    parent: dict | None = None


class UpdateDataSourceRequest(BaseModel):
    data_source_id: str
    statements: str | None = None
    title: str | None = None
    description: str | None = None
    in_trash: bool | None = None
    is_inline: bool | None = None


class CreateCommentRequest(BaseModel):
    page_id: str | None = None
    markdown: str | None = None
    rich_text: list | None = None
    selection_with_ellipsis: str | None = None
    discussion_id: str | None = None


class GetCommentsRequest(BaseModel):
    page_id: str
    discussion_id: str | None = None
    include_all_blocks: bool = False
    include_resolved: bool = False


class GetUsersRequest(BaseModel):
    query: str | None = None
    user_id: str | None = None
    page_size: int = 100
    start_cursor: str | None = None


class GetTeamsRequest(BaseModel):
    query: str | None = None


class CreateViewRequest(BaseModel):
    data_source_id: str
    name: str
    type: str
    database_id: str | None = None
    parent_page_id: str | None = None
    configure: str | None = None


class UpdateViewRequest(BaseModel):
    view_id: str
    name: str | None = None
    configure: str | None = None


class QueryDatabaseRequest(BaseModel):
    database_id: str
    filter: dict | None = None
    sorts: list | None = None
    page_size: int = 100
    start_cursor: str | None = None


class ListBlocksRequest(BaseModel):
    page_id: str
    recursive: bool = False
    start_cursor: str | None = None


class AppendBlocksRequest(BaseModel):
    parent_id: str
    blocks: list[dict]
    after: str | None = None


class UpdateBlockRequest(BaseModel):
    block_id: str
    block: dict


class DeleteBlocksRequest(BaseModel):
    block_ids: list[str]
    allow_deleting_content: bool = False


class MoveBlocksRequest(BaseModel):
    block_ids: list[str]
    after_block_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/search", summary="Search Notion", description=docs.SEARCH)
def search(body: SearchRequest) -> dict:
    return notion_service.op_search(get_settings(), **body.model_dump())


@router.post("/fetch", summary="Fetch a page/database", description=docs.FETCH)
def fetch(body: FetchRequest) -> dict:
    return notion_service.op_fetch(get_settings(), **body.model_dump())


@router.post("/create-pages", summary="Create page(s)", description=docs.CREATE_PAGES)
def create_pages(body: CreatePagesRequest) -> dict:
    return notion_service.op_create_pages(
        get_settings(),
        pages=[p.model_dump() for p in body.pages],
        parent=body.parent,
    )


@router.post("/update-page", summary="Update a page", description=docs.UPDATE_PAGE)
def update_page(body: UpdatePageRequest) -> dict:
    return notion_service.op_update_page(get_settings(), **body.model_dump())


@router.post("/move-pages", summary="Move pages/databases", description=docs.MOVE_PAGES)
def move_pages(body: MovePagesRequest) -> dict:
    return notion_service.op_move_pages(get_settings(), **body.model_dump())


@router.post("/duplicate-page", summary="Duplicate a page", description=docs.DUPLICATE_PAGE)
def duplicate_page(body: DuplicatePageRequest) -> dict:
    return notion_service.op_duplicate_page(get_settings(), **body.model_dump())


@router.post("/create-database", summary="Create a database", description=docs.CREATE_DATABASE)
def create_database(body: CreateDatabaseRequest) -> dict:
    return notion_service.op_create_database(
        get_settings(),
        schema=body.schema_ddl,
        title=body.title,
        description=body.description,
        parent=body.parent,
    )


@router.post("/update-data-source", summary="Update a data source", description=docs.UPDATE_DATA_SOURCE)
def update_data_source(body: UpdateDataSourceRequest) -> dict:
    return notion_service.op_update_data_source(get_settings(), **body.model_dump())


@router.post("/create-comment", summary="Create a comment", description=docs.CREATE_COMMENT)
def create_comment(body: CreateCommentRequest) -> dict:
    return notion_service.op_create_comment(get_settings(), **body.model_dump())


@router.post("/get-comments", summary="Get comments", description=docs.GET_COMMENTS)
def get_comments(body: GetCommentsRequest) -> dict:
    return notion_service.op_get_comments(get_settings(), **body.model_dump())


@router.post("/get-users", summary="Get users", description=docs.GET_USERS)
def get_users(body: GetUsersRequest) -> dict:
    return notion_service.op_get_users(get_settings(), **body.model_dump())


@router.post("/get-teams", summary="Get teams", description=docs.GET_TEAMS)
def get_teams(body: GetTeamsRequest) -> dict:
    return notion_service.op_get_teams(get_settings(), **body.model_dump())


@router.post("/create-view", summary="Create a view", description=docs.CREATE_VIEW)
def create_view(body: CreateViewRequest) -> dict:
    return notion_service.op_create_view(get_settings(), **body.model_dump())


@router.post("/update-view", summary="Update a view", description=docs.UPDATE_VIEW)
def update_view(body: UpdateViewRequest) -> dict:
    return notion_service.op_update_view(get_settings(), **body.model_dump())


@router.post("/query-database", summary="Query a database (filtered)", description=docs.QUERY_DATABASE)
def query_database(body: QueryDatabaseRequest) -> dict:
    return notion_service.op_query_database(get_settings(), **body.model_dump())


@router.post("/list-blocks", summary="List a page's blocks by id", description=docs.LIST_BLOCKS)
def list_blocks(body: ListBlocksRequest) -> dict:
    return notion_service.op_list_blocks(get_settings(), **body.model_dump())


@router.post("/append-blocks", summary="Append typed blocks", description=docs.APPEND_BLOCKS)
def append_blocks(body: AppendBlocksRequest) -> dict:
    return notion_service.op_append_blocks(get_settings(), **body.model_dump())


@router.post("/update-block", summary="Update one block by id", description=docs.UPDATE_BLOCK)
def update_block(body: UpdateBlockRequest) -> dict:
    return notion_service.op_update_block(get_settings(), **body.model_dump())


@router.post("/delete-blocks", summary="Delete specific blocks by id", description=docs.DELETE_BLOCKS)
def delete_blocks(body: DeleteBlocksRequest) -> dict:
    return notion_service.op_delete_blocks(get_settings(), **body.model_dump())


@router.post("/move-blocks", summary="Move blocks by id", description=docs.MOVE_BLOCKS)
def move_blocks(body: MoveBlocksRequest) -> dict:
    return notion_service.op_move_blocks(get_settings(), **body.model_dump())


@router.post("/query", summary="Daily-brief filtered read", description=docs.QUERY_BRIEF)
def query() -> dict:
    return notion_service.build_brief(get_settings())
