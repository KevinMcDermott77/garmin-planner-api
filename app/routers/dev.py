from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pydantic import ValidationError

from app.auth import get_current_user
from app.db import validate_supabase_env
from app.services import persistence
from app.services.models import Plan

router = APIRouter()


class DevTokenRequest(BaseModel):
    email: str
    password: str


class DevImportPlanRequest(BaseModel):
    plan_json: dict[str, Any]


class DevImportPlanResponse(BaseModel):
    plan_id: str
    weeks: int
    goal: str
    imported: bool


@router.post("/token")
def create_dev_token(request: DevTokenRequest) -> dict[str, str]:
    try:
        response = _create_dev_auth_client().auth.sign_in_with_password(
            {"email": request.email, "password": request.password}
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Could not sign in test user: {exc}") from exc

    session = getattr(response, "session", None)
    token = getattr(session, "access_token", None)
    if not token:
        raise HTTPException(status_code=401, detail="Supabase did not return an access token.")
    return {"access_token": token}


@router.post("/import-plan", response_model=DevImportPlanResponse)
def import_plan(
    request: DevImportPlanRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DevImportPlanResponse:
    try:
        plan = Plan.model_validate(request.plan_json)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_input=False),
        ) from exc

    try:
        plan_id = persistence.save_plan(
            profile_id=None,
            user_id=str(current_user["sub"]),
            goal=plan.goal,
            weeks=len(plan.weeks),
            race_date=plan.race_date,
            plan=plan,
            assessment=None,
            tokens=None,
            status="active",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not import plan: {exc}") from exc

    return DevImportPlanResponse(
        plan_id=plan_id,
        weeks=len(plan.weeks),
        goal=plan.goal,
        imported=True,
    )


def _create_dev_auth_client():
    """Create an isolated client so sign-in never mutates the DB service-role singleton."""
    validate_supabase_env()
    import os

    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
