from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services.race_lookup import RaceLookupResult, fallback_result, lookup_race

router = APIRouter()


class RaceLookupRequest(BaseModel):
    race_name: str = Field(min_length=1)


AUTH_RESPONSES = {401: {"description": "Missing or invalid bearer token"}}


@router.post("/lookup", response_model=RaceLookupResult, responses=AUTH_RESPONSES)
def lookup_race_endpoint(
    request: RaceLookupRequest,
    _current_user: dict[str, Any] = Depends(get_current_user),
) -> RaceLookupResult:
    try:
        return lookup_race(request.race_name.strip())
    except Exception as exc:  # noqa: BLE001
        print(f"Race lookup failed gracefully: {exc}")
        return fallback_result(request.race_name.strip())
