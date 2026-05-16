from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

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


@router.post("/generate", response_model=PlanGenerateResponse)
def generate_plan_endpoint(request: PlanGenerateRequest) -> PlanGenerateResponse:
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
        profile_id = persistence.save_profile(request.profile)
        plan_id = persistence.save_plan(
            profile_id=profile_id,
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


@router.get("")
def list_saved_plans(profile_id: str | None = Query(default=None)) -> list[dict[str, Any]]:
    try:
        return persistence.list_plans(profile_id=profile_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not list plans: {exc}") from exc


@router.get("/{plan_id}")
def get_saved_plan(plan_id: str) -> dict[str, Any]:
    try:
        plan = persistence.get_plan(plan_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not retrieve plan: {exc}") from exc
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    return plan
