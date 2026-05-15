from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.generator import PlanGenerationError, generate_plan
from app.services.models import Plan
from app.services.profile import AthleteProfile
from app.services.sanity_check import GoalAssessment, assess_goal

router = APIRouter()


class PlanPreviewRequest(BaseModel):
    profile: AthleteProfile
    goal: str
    weeks: int = Field(ge=4, le=32)
    race_date: date | None = None
    history_summary: dict | None = None
    notes: str | None = None


class PlanPreviewResponse(BaseModel):
    assessment: GoalAssessment
    plan: Plan
    tokens: dict


@router.post("/preview", response_model=PlanPreviewResponse)
def preview_plan(request: PlanPreviewRequest) -> PlanPreviewResponse:
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

    return PlanPreviewResponse(assessment=assessment, plan=plan, tokens=tokens)
