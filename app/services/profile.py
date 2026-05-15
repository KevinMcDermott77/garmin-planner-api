"""Athlete profile models and storage helpers."""

from __future__ import annotations

import re
from datetime import date as Date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DayName = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class RecentRace(BaseModel):
    distance_km: float = Field(gt=0)
    time_minutes: float = Field(gt=0)
    date: Date | None = None
    note: str | None = None


class SchedulePreferences(BaseModel):
    long_run_day: DayName = "sun"
    quality_day_primary: DayName = "wed"
    quality_day_secondary: DayName | None = None
    days_off: list[DayName] = Field(default_factory=list)
    earliest_run_time: str | None = None
    notes: str | None = None


class AthleteProfile(BaseModel):
    name: str
    weekly_km_recent: float = Field(ge=0, description="Average km/week over last 4-6 weeks")
    longest_run_km_recent: float = Field(ge=0, description="Longest single run in last 8 weeks")
    easy_pace_min_per_km: float | None = Field(
        default=None,
        ge=3.0,
        le=10.0,
        description="Comfortable conversational pace",
    )
    days_per_week: int = Field(ge=3, le=7, description="Days available to train per week")
    recent_race: RecentRace | None = None
    injuries: str | None = None
    cross_training: str | None = None
    years_running: float | None = Field(default=None, ge=0)
    notes: str | None = None
    schedule: SchedulePreferences = Field(default_factory=SchedulePreferences)


def save_profile(profile: AthleteProfile, profiles_dir: Path) -> Path:
    """Save an athlete profile as pretty JSON."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{_slugify(profile.name)}.json"
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_profile(path: Path) -> AthleteProfile:
    """Load an athlete profile from JSON."""
    return AthleteProfile.model_validate_json(path.read_text(encoding="utf-8-sig"))


def list_profiles(profiles_dir: Path) -> list[Path]:
    """List saved profile files."""
    if not profiles_dir.exists():
        return []
    return sorted(profiles_dir.glob("*.json"))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "athlete"
