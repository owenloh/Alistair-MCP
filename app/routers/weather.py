"""POST /api/weather — forecast for a place and a time, in one call.

Thin router over app/services/weather.py. Read-only: it queries Open-Meteo's public
API (no key) and stores nothing. The description lives in _weather_docs.py so both
the HTTP layer and the MCP tool share one contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..models import WeatherRequest
from ..security import require_api_key
from ..services import weather as weather_service
from . import _weather_docs as _docs

router = APIRouter(
    prefix="/api/weather",
    tags=["weather"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "",
    summary="Weather for a location and a time (now / tonight / a date / a range)",
    description=_docs.WEATHER,
)
def weather(body: WeatherRequest) -> dict:
    return weather_service.weather(
        get_settings(), location=body.location, when=body.when, units=body.units
    )
