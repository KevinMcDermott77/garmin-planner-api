"""Supabase persistence for athlete profiles and generated plans."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.db import get_supabase_client
from app.services.models import Plan
from app.services.profile import AthleteProfile
from app.services.sanity_check import GoalAssessment


class PersistenceError(RuntimeError):
    """Raised when a persistence operation cannot be completed."""


def save_profile(profile: AthleteProfile, user_id: str) -> str:
    """Persist an athlete profile and return its generated id."""
    payload = profile.model_dump(mode="json")
    payload["user_id"] = user_id
    response = get_supabase_client().table("profiles").insert(payload).execute()
    row = _single_row(response, "profile")
    return str(row["id"])


def save_plan(
    profile_id: str,
    user_id: str,
    goal: str,
    weeks: int,
    race_date: date | None,
    plan: Plan,
    assessment: GoalAssessment,
    tokens: dict[str, Any],
) -> str:
    """Persist a plan and its scheduled sessions, returning the plan id."""
    plan_payload = {
        "profile_id": profile_id,
        "user_id": user_id,
        "goal": goal,
        "weeks": weeks,
        "race_date": race_date.isoformat() if race_date else None,
        "plan_json": plan.model_dump(mode="json"),
        "assessment_json": assessment.model_dump(mode="json"),
        "tokens_json": tokens,
    }
    response = get_supabase_client().table("plans").insert(plan_payload).execute()
    row = _single_row(response, "plan")
    plan_id = str(row["id"])

    session_rows = _scheduled_session_rows(plan_id, plan)
    if session_rows:
        get_supabase_client().table("scheduled_sessions").insert(session_rows).execute()

    return plan_id


def get_plan(plan_id: str, user_id: str) -> dict[str, Any] | None:
    """Return a plan row with scheduled sessions, or None when not found."""
    client = get_supabase_client()
    plan_response = client.table("plans").select("*").eq("id", plan_id).eq("user_id", user_id).limit(1).execute()
    rows = _rows(plan_response)
    if not rows:
        return None

    plan_row = rows[0]
    sessions_response = (
        client.table("scheduled_sessions")
        .select("*")
        .eq("plan_id", plan_id)
        .order("week_number")
        .order("day_of_week")
        .execute()
    )
    plan_row["scheduled_sessions"] = _rows(sessions_response)
    return plan_row


def list_plans(user_id: str) -> list[dict[str, Any]]:
    """Return summary rows for saved plans."""
    query = (
        get_supabase_client()
        .table("plans")
        .select("id, goal, weeks, status, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
    )
    return _rows(query.execute())


def _scheduled_session_rows(plan_id: str, plan: Plan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for week in plan.weeks:
        for session in week.sessions:
            rows.append(
                {
                    "plan_id": plan_id,
                    "week_number": week.week_number,
                    "day_of_week": session.day_of_week,
                    "scheduled_date": None,
                    "session_type": session.type,
                    "description": session.description,
                    "distance_km": session.distance_km,
                    "duration_min": session.duration_min,
                    "steps": [step.model_dump(mode="json") for step in session.steps],
                }
            )
    return rows


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if data is None:
        raise PersistenceError("Supabase response did not include data.")
    if not isinstance(data, list):
        raise PersistenceError("Supabase response data was not a list.")
    return data


def _single_row(response: Any, label: str) -> dict[str, Any]:
    rows = _rows(response)
    if not rows:
        raise PersistenceError(f"Supabase did not return a saved {label} row.")
    return rows[0]
