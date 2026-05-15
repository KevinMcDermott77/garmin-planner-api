"""Pydantic schemas for generated running plans."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class WorkoutStep(BaseModel):
    step_type: Literal["warmup", "interval", "recovery", "cooldown"]
    duration_type: Literal["time", "distance"]
    duration_value: float = Field(gt=0)
    target_type: Literal["pace", "heart_rate", "none"]
    target_low: float | None = None
    target_high: float | None = None


class Session(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    type: Literal["easy", "long", "tempo", "intervals", "recovery", "rest", "cross"]
    description: str
    distance_km: float | None = Field(default=None, ge=0)
    duration_min: int | None = Field(default=None, ge=0)
    steps: list[WorkoutStep] = Field(default_factory=list)


class Week(BaseModel):
    week_number: int = Field(ge=1)
    focus: str
    sessions: list[Session]


class Plan(BaseModel):
    goal: str
    race_date: date | None = None
    weeks: list[Week]
