from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.auth import get_current_user
from app.services import strava

router = APIRouter()

AUTH_RESPONSES = {401: {"description": "Missing or invalid bearer token"}}
FRONTEND_STRAVA_URL = "http://localhost:3000/strava"


@router.get("/connect", responses=AUTH_RESPONSES)
def connect(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, str]:
    user_id = str(current_user["sub"])
    return {"auth_url": strava.get_strava_auth_url(user_id)}


@router.get("/callback")
def callback(code: str, state: str, scope: str | None = None) -> RedirectResponse:
    # Public OAuth redirect. In this dev slice, state is the user_id; production
    # must replace this with a CSRF-safe server-side nonce flow.
    try:
        token_response = strava.exchange_code(code, state)
        if scope and "scope" not in token_response:
            token_response["scope"] = scope
        strava.save_tokens(state, token_response)
    except Exception:
        return RedirectResponse(f"{FRONTEND_STRAVA_URL}?error=true")
    return RedirectResponse(f"{FRONTEND_STRAVA_URL}?connected=true")


@router.get("/activities", responses=AUTH_RESPONSES)
def activities(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = str(current_user["sub"])
    access_token = strava.get_valid_token(user_id)
    if not access_token:
        return {"activities": [], "connected": False}
    return {"activities": strava.get_recent_activities(user_id), "connected": True}


@router.get("/status", responses=AUTH_RESPONSES)
def status(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = str(current_user["sub"])
    return strava.get_connection_status(user_id)


@router.get("/fitness-summary", responses=AUTH_RESPONSES)
def fitness_summary(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = str(current_user["sub"])
    if strava.get_token_row(user_id) is None:
        return {"connected": False, "summary": None}

    try:
        summary = strava.compute_fitness_summary(user_id)
    except strava.StravaFetchError:
        return {"connected": True, "summary": None, "note": "Could not fetch Strava data"}

    return {"connected": True, "summary": summary}
