"""Strava OAuth and activities service."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.db import get_supabase_client

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STRAVA_SCOPE = "activity:read_all"


class StravaConfigError(RuntimeError):
    """Raised when Strava configuration is incomplete."""


class StravaFetchError(RuntimeError):
    """Raised when Strava data cannot be fetched for a connected user."""


def get_strava_auth_url(user_id: str) -> str:
    """Build a Strava OAuth URL for the current user."""
    params = {
        "client_id": _required_env("STRAVA_CLIENT_ID"),
        "redirect_uri": _required_env("STRAVA_REDIRECT_URI"),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": STRAVA_SCOPE,
        # Dev-only shortcut: state lets the callback recover the user id.
        # Production should use a nonce stored server-side for CSRF protection.
        "state": user_id,
    }
    return f"{STRAVA_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, state: str) -> dict[str, Any]:
    """Exchange a Strava OAuth code for access and refresh tokens."""
    _ = state
    response = httpx.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": _required_env("STRAVA_CLIENT_ID"),
            "client_secret": _required_env("STRAVA_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token_encrypted: str) -> dict[str, Any] | None:
    """Refresh a Strava access token using a stored encrypted refresh token."""
    try:
        refresh_token = _decrypt(refresh_token_encrypted)
    except (InvalidToken, StravaConfigError):
        return None

    try:
        response = httpx.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": _required_env("STRAVA_CLIENT_ID"),
                "client_secret": _required_env("STRAVA_CLIENT_SECRET"),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        response.raise_for_status()
    except (httpx.HTTPError, StravaConfigError):
        return None

    return response.json()


def save_tokens(user_id: str, token_response: dict[str, Any]) -> None:
    """Encrypt and upsert Strava token data for a user."""
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    expires_at = token_response.get("expires_at")
    if not access_token or not refresh_token or expires_at is None:
        raise ValueError("Strava token response is missing required token fields.")

    athlete = token_response.get("athlete") or {}
    payload = {
        "user_id": user_id,
        "access_token": _encrypt(str(access_token)),
        "refresh_token": _encrypt(str(refresh_token)),
        "expires_at": _expires_at_to_datetime(expires_at).isoformat(),
        "athlete_id": athlete.get("id"),
        "scope": token_response.get("scope"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    get_supabase_client().table("strava_tokens").upsert(payload, on_conflict="user_id").execute()


def get_valid_token(user_id: str) -> str | None:
    """Return a decrypted access token, refreshing it automatically when expired."""
    row = get_token_row(user_id)
    if row is None:
        return None

    try:
        expires_at = _parse_datetime(row["expires_at"])
    except (KeyError, ValueError, TypeError):
        return None

    if expires_at <= datetime.now(UTC):
        refreshed = refresh_access_token(str(row.get("refresh_token", "")))
        if not refreshed:
            return None
        try:
            save_tokens(user_id, refreshed)
        except Exception:
            return None
        row = get_token_row(user_id)
        if row is None:
            return None

    try:
        return _decrypt(str(row["access_token"]))
    except (KeyError, InvalidToken, StravaConfigError):
        return None


def get_recent_activities(user_id: str, per_page: int = 20) -> list[dict[str, Any]]:
    """Fetch recent Strava runs for the user, returning an empty list when disconnected."""
    access_token = get_valid_token(user_id)
    if not access_token:
        return []

    try:
        response = httpx.get(
            STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": per_page},
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []

    return [_activity_to_run(activity) for activity in response.json() if activity.get("type") == "Run"]


def compute_fitness_summary(user_id: str) -> dict[str, Any] | None:
    """Compute generation-time fitness inputs from the user's last 90 days of Strava runs."""
    if get_token_row(user_id) is None:
        return None

    access_token = get_valid_token(user_id)
    if not access_token:
        raise StravaFetchError("Could not fetch Strava data")

    after = int((datetime.now(UTC) - timedelta(days=90)).timestamp())
    try:
        response = httpx.get(
            STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after, "per_page": 100, "type": "Run"},
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise StravaFetchError("Could not fetch Strava data") from exc

    runs = [activity for activity in response.json() if activity.get("type") == "Run"]
    if not runs:
        return None

    distances_km = [float(run.get("distance") or 0) / 1000 for run in runs]
    total_km = sum(distances_km)
    longest_run_km = max(distances_km)
    run_dates = [_parse_datetime(str(run["start_date"])) for run in runs if run.get("start_date")]
    run_days = {run_date.date().isoformat() for run_date in run_dates}

    short_run_paces = [
        _speed_to_min_per_km(float(run.get("average_speed") or 0))
        for run in runs
        if float(run.get("distance") or 0) / 1000 < 10
    ]
    all_run_paces = [_speed_to_min_per_km(float(run.get("average_speed") or 0)) for run in runs]
    pace_candidates = [pace for pace in short_run_paces if pace is not None] or [
        pace for pace in all_run_paces if pace is not None
    ]

    # Simple 90-day approximation: divide by 13 weeks. This intentionally does
    # not try to infer injury gaps or pre-Strava history from sparse activity.
    days_per_week = round(len(run_days) / 13.0)
    days_per_week = min(7, max(3, days_per_week))

    return {
        "weekly_km_recent": round(total_km / 13.0, 1),
        "longest_run_km_recent": round(longest_run_km, 1),
        "easy_pace_min_per_km": round(median(pace_candidates), 2) if pace_candidates else None,
        "days_per_week": days_per_week,
        "run_count": len(runs),
        "date_range": {
            "from": min(run_dates).date().isoformat() if run_dates else None,
            "to": max(run_dates).date().isoformat() if run_dates else None,
        },
        "data_source": "strava_90_days",
    }


def get_activity_matches(user_id: str, plan: dict[str, Any], today: date) -> dict[str, dict[str, Any]]:
    """Match past planned running sessions to Strava runs without storing results."""
    race_date_value = plan.get("race_date")
    if not race_date_value:
        return {}

    access_token = get_valid_token(user_id)
    if not access_token:
        return {}

    try:
        race_date = date.fromisoformat(str(race_date_value))
        plan_weeks = _plan_week_count(plan)
    except (TypeError, ValueError):
        return {}

    start_date = race_date - timedelta(days=plan_weeks * 7)
    runs = _fetch_runs_between(access_token, start_date, today)
    matches: dict[str, dict[str, Any]] = {}
    used_activity_ids: set[Any] = set()

    for week in plan.get("weeks", []):
        week_number = int(week.get("week_number") or 0)
        if week_number <= 0:
            continue

        for session in week.get("sessions", []):
            session_type = session.get("type")
            if session_type in {"rest", "cross"}:
                continue

            day_of_week = int(session.get("day_of_week") or 0)
            session_date = start_date + timedelta(days=(week_number - 1) * 7 + day_of_week)
            if session_date >= today:
                continue

            key = f"week_{week_number}_day_{day_of_week}"
            planned_distance_km = _optional_float(session.get("distance_km"))
            match = _best_activity_for_session(runs, session_date, planned_distance_km, used_activity_ids)
            if match is None:
                matches[key] = _missed_match()
                continue
            used_activity_ids.add(match.get("id"))

            actual_distance_km = round(float(match.get("distance") or 0) / 1000, 2)
            matches[key] = {
                "status": _match_status(actual_distance_km, planned_distance_km),
                "actual_distance_km": actual_distance_km,
                "actual_pace_min_per_km": _rounded_pace(match.get("average_speed")),
                "actual_duration_min": round(float(match.get("moving_time") or 0) / 60, 1)
                if match.get("moving_time") is not None
                else None,
                "strava_activity_id": match.get("id"),
                "strava_activity_name": match.get("name"),
            }

    return matches


def compute_plan_start_date(plan: dict[str, Any]) -> date | None:
    """Compute a plan start date from race_date and plan length."""
    race_date_value = plan.get("race_date")
    if not race_date_value:
        return None
    try:
        race_date = date.fromisoformat(str(race_date_value))
        plan_weeks = _plan_week_count(plan)
    except (TypeError, ValueError):
        return None
    return race_date - timedelta(days=plan_weeks * 7)


def get_token_row(user_id: str) -> dict[str, Any] | None:
    """Return a stored Strava token row for the user, if present."""
    response = (
        get_supabase_client()
        .table("strava_tokens")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def get_connection_status(user_id: str) -> dict[str, Any]:
    """Return status fields without calling Strava."""
    row = get_token_row(user_id)
    if row is None:
        return {"connected": False, "athlete_id": None, "scope": None}
    return {
        "connected": True,
        "athlete_id": row.get("athlete_id"),
        "scope": row.get("scope"),
    }


def _activity_to_run(activity: dict[str, Any]) -> dict[str, Any]:
    distance_km = round(float(activity.get("distance") or 0) / 1000, 3)
    moving_time = int(activity.get("moving_time") or 0)
    average_speed = float(activity.get("average_speed") or 0)
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "start_date": activity.get("start_date"),
        "distance": distance_km,
        "moving_time": _format_duration(moving_time),
        "elapsed_time": _format_duration(int(activity.get("elapsed_time") or 0)),
        "average_speed": _format_pace(average_speed),
        "total_elevation_gain": activity.get("total_elevation_gain"),
        "type": activity.get("type"),
    }


def _format_duration(seconds: int) -> str:
    minutes, remaining_seconds = divmod(max(0, seconds), 60)
    return f"{minutes}:{remaining_seconds:02d}"


def _format_pace(metres_per_second: float) -> str | None:
    if metres_per_second <= 0:
        return None
    seconds_per_km = round(1000 / metres_per_second)
    minutes, seconds = divmod(seconds_per_km, 60)
    return f"{minutes}:{seconds:02d}"


def _speed_to_min_per_km(metres_per_second: float) -> float | None:
    if metres_per_second <= 0:
        return None
    return (1000 / metres_per_second) / 60


def _fetch_runs_between(access_token: str, start_date: date, today: date) -> list[dict[str, Any]]:
    try:
        response = httpx.get(
            STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "after": int(datetime.combine(start_date, time.min, tzinfo=UTC).timestamp()),
                "before": int(datetime.combine(today, time.min, tzinfo=UTC).timestamp()),
                "per_page": 100,
                "type": "Run",
            },
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise StravaFetchError("Could not fetch Strava data") from exc
    return [activity for activity in response.json() if activity.get("type") == "Run"]


def _best_activity_for_session(
    runs: list[dict[str, Any]],
    session_date: date,
    planned_distance_km: float | None,
    used_activity_ids: set[Any],
) -> dict[str, Any] | None:
    candidates = [
        run
        for run in runs
        if run.get("id") not in used_activity_ids
        and run.get("start_date")
        and abs((_parse_datetime(str(run["start_date"])).date() - session_date).days) <= 1
    ]
    if not candidates:
        return None

    if planned_distance_km is None:
        return min(candidates, key=lambda run: abs((_parse_datetime(str(run["start_date"])).date() - session_date).days))

    return min(candidates, key=lambda run: abs(float(run.get("distance") or 0) / 1000 - planned_distance_km))


def _match_status(actual_distance_km: float, planned_distance_km: float | None) -> str:
    if planned_distance_km is None or planned_distance_km <= 0:
        return "completed"
    if actual_distance_km >= planned_distance_km * 0.9:
        return "completed"
    if actual_distance_km >= planned_distance_km * 0.5:
        return "partial"
    return "partial"


def _missed_match() -> dict[str, Any]:
    return {
        "status": "missed",
        "actual_distance_km": None,
        "actual_pace_min_per_km": None,
        "actual_duration_min": None,
        "strava_activity_id": None,
        "strava_activity_name": None,
    }


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded_pace(value: Any) -> float | None:
    try:
        speed = float(value or 0)
    except (TypeError, ValueError):
        return None
    pace = _speed_to_min_per_km(speed)
    return round(pace, 2) if pace is not None else None


def _plan_week_count(plan: dict[str, Any]) -> int:
    weeks_value = plan.get("weeks")
    if isinstance(weeks_value, list):
        return len(weeks_value)
    return int(weeks_value)


def _expires_at_to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromtimestamp(int(value), tz=UTC)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def _fernet() -> Fernet:
    return Fernet(_required_env("STRAVA_TOKEN_ENCRYPTION_KEY").encode("utf-8"))


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise StravaConfigError(f"Missing required environment variable: {name}")
    return value
