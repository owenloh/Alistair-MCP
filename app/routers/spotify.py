"""Spotify connector endpoints (unofficial API via SpotAPI).

HTTP surface for controlling the user's Spotify: browse playlists, search + play
songs, and list/choose the device playback runs on. One POST endpoint per tool,
each forwarding parsed args to a function in ``app.services.spotify``. Request
models are defined inline here (like the calendar/notion routers); the verbatim
tool descriptions live in ``_spotify_docs`` so both the HTTP layer and the MCP
wrappers share one contract.

Everything degrades gracefully: with no SPOTIFY_COOKIES configured the service
raises a clean 503, so the rest of the API is unaffected.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import get_settings
from ..security import require_api_key
from ..services import spotify as spotify_service
from . import _spotify_docs as docs

router = APIRouter(
    prefix="/api/spotify",
    tags=["spotify"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ListPlaylistsRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=343)


class PlaylistTracksRequest(BaseModel):
    playlist: str = Field(description="Playlist uri, URL or id.")
    limit: int = Field(default=50, ge=1, le=343)


class SearchRequest(BaseModel):
    query: str = Field(description="Search text (song and/or artist).")
    limit: int = Field(default=10, ge=1, le=50)


class TransferRequest(BaseModel):
    device: str = Field(description="Target device id, or a unique part of its name.")


class PlayRequest(BaseModel):
    track: str = Field(description="Track uri, URL or id to play.")
    playlist: str | None = Field(
        default=None, description="Optional playlist uri the track belongs to (its play context)."
    )
    device: str | None = Field(
        default=None, description="Optional device id/name to play on."
    )


class QueueRequest(BaseModel):
    track: str = Field(description="Track uri, URL or id to add to the queue.")


class ControlRequest(BaseModel):
    action: str = Field(
        description="pause|resume|next|previous|restart|shuffle_on|shuffle_off|"
        "repeat_on|repeat_off|volume|seek"
    )
    value: float | None = Field(
        default=None, description="For volume: 0-100. For seek: position in milliseconds."
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/playlists", summary="List my Spotify playlists", description=docs.LIST_PLAYLISTS)
def list_playlists(body: ListPlaylistsRequest) -> dict:
    return spotify_service.list_playlists(get_settings(), limit=body.limit)


@router.post("/playlist-tracks", summary="List tracks in a playlist", description=docs.PLAYLIST_TRACKS)
def playlist_tracks(body: PlaylistTracksRequest) -> dict:
    return spotify_service.playlist_tracks(get_settings(), body.playlist, limit=body.limit)


@router.post("/search", summary="Search tracks", description=docs.SEARCH)
def search(body: SearchRequest) -> dict:
    return spotify_service.search_tracks(get_settings(), body.query, limit=body.limit)


@router.post("/devices", summary="List available playback devices", description=docs.DEVICES)
def devices() -> dict:
    return spotify_service.list_devices(get_settings())


@router.post("/transfer", summary="Move playback to a device", description=docs.TRANSFER)
def transfer(body: TransferRequest) -> dict:
    return spotify_service.transfer_playback(get_settings(), body.device)


@router.post("/status", summary="What's playing now", description=docs.STATUS)
def status() -> dict:
    return spotify_service.now_playing(get_settings())


@router.post("/play", summary="Play a specific song", description=docs.PLAY)
def play(body: PlayRequest) -> dict:
    return spotify_service.play(
        get_settings(), track=body.track, playlist=body.playlist, device=body.device
    )


@router.post("/queue", summary="Add a track to the queue", description=docs.QUEUE)
def queue(body: QueueRequest) -> dict:
    return spotify_service.queue(get_settings(), body.track)


@router.post("/control", summary="Control playback", description=docs.CONTROL)
def control(body: ControlRequest) -> dict:
    return spotify_service.control(get_settings(), body.action, body.value)
