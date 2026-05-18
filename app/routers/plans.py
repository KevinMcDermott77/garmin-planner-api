from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services.generator import PlanGenerationError, generate_plan
from app.services.models import Plan
from app.services import persistence
from app.services.profile import AthleteProfile
from app.services.sanity_check import GoalAssessment, assess_goal

router = APIRouter()


class PlanGenerateRequest(BaseModel):
    profile: AthleteProfile
    goal: str
    weeks: int = Field(ge=4, le=32)
    race_date: date | None = None
    history_summary: dict | None = None
    notes: str | None = None


class PlanGenerateResponse(BaseModel):
    plan_id: str
    profile_id: str
    assessment: GoalAssessment
    plan: Plan
    tokens: dict


class PlanDeleteResponse(BaseModel):
    deleted: bool
    plan_id: str


AUTH_RESPONSES = {401: {"description": "Missing or invalid bearer token"}}


@router.post("/generate", response_model=PlanGenerateResponse, responses=AUTH_RESPONSES)
def generate_plan_endpoint(
    request: PlanGenerateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> PlanGenerateResponse:
    user_id = str(current_user["sub"])
    assessment = assess_goal(
        request.goal,
        request.profile,
        request.history_summary,
        available_weeks=request.weeks,
    )
    try:
        plan, tokens = generate_plan(
            goal=request.goal,
            weeks=request.weeks,
            history_summary=request.history_summary,
            race_date=request.race_date,
            notes=request.notes,
            profile=request.profile,
            goal_assessment=assessment,
        )
    except PlanGenerationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    profile_id: str | None = None
    try:
        profile_id = persistence.save_profile(request.profile, user_id=user_id)
        plan_id = persistence.save_plan(
            profile_id=profile_id,
            user_id=user_id,
            goal=request.goal,
            weeks=request.weeks,
            race_date=request.race_date,
            plan=plan,
            assessment=assessment,
            tokens=tokens,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Plan generated but persistence failed: {exc}",
                "profile_id": profile_id,
                "assessment": assessment.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "tokens": tokens,
            },
        ) from exc

    return PlanGenerateResponse(
        plan_id=plan_id,
        profile_id=profile_id,
        assessment=assessment,
        plan=plan,
        tokens=tokens,
    )


@router.get("", responses=AUTH_RESPONSES)
def list_saved_plans(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    try:
        return persistence.list_plans(user_id=str(current_user["sub"]))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not list plans: {exc}") from exc


@router.get("/{plan_id}", responses={**AUTH_RESPONSES, 404: {"description": "Plan not found"}})
def get_saved_plan(
    plan_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        plan = persistence.get_plan(plan_id, user_id=str(current_user["sub"]))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not retrieve plan: {exc}") from exc
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    return plan


@router.delete("/{plan_id}", response_model=PlanDeleteResponse, responses={**AUTH_RESPONSES, 404: {"description": "Plan not found"}})
def delete_saved_plan(
    plan_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> PlanDeleteResponse:
    try:
        deleted = persistence.delete_plan(plan_id=plan_id, user_id=str(current_user["sub"]))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Could not delete plan.") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Plan not found")
    return PlanDeleteResponse(deleted=True, plan_id=plan_id)
