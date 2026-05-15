"""Goal-vs-base assessment before plan generation."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel

from app.services.profile import AthleteProfile

GoalDistance = Literal["5k", "10k", "half_marathon", "marathon", "ultra", "base", "unknown"]

MARATHON_PEAKS = [(180, 100), (210, 85), (240, 75), (270, 60), (float("inf"), 50)]
HALF_PEAKS = [(85, 75), (95, 65), (105, 55), (120, 45), (float("inf"), 35)]
TEN_K_PEAKS = [(40, 70), (45, 60), (50, 50), (float("inf"), 35)]
FIVE_K_PEAKS = [(20, 60), (23, 50), (26, 40), (float("inf"), 30)]

DISTANCE_KM = {
    "5k": 5.0,
    "10k": 10.0,
    "half_marathon": 21.0975,
    "marathon": 42.195,
}


class GoalAssessment(BaseModel):
    feasible: bool
    confidence: Literal["high", "medium", "low", "very_low"]
    detected_distance: GoalDistance
    detected_target_minutes: float | None
    user_predicted_minutes: float | None
    required_peak_weekly_km: float
    current_weekly_km: float
    weeks_to_close_gap: int | None
    message: str
    suggested_alternative: str | None


def assess_goal(
    goal: str,
    profile: AthleteProfile,
    history_summary: dict | None,
    available_weeks: int = 12,
) -> GoalAssessment:
    """Assess whether a goal fits the athlete's current base."""
    detected_distance, detected_target_minutes = _parse_goal(goal)
    current_weekly_km = _current_weekly_km(profile, history_summary)
    predicted_minutes = _predict_minutes(profile, detected_distance)
    required_peak = _required_peak_km(detected_distance, detected_target_minutes)
    weeks_to_close_gap = _weeks_to_close_gap(current_weekly_km, required_peak)

    feasible, confidence = _feasibility(
        detected_target_minutes,
        predicted_minutes,
        weeks_to_close_gap,
        available_weeks,
        required_peak,
        current_weekly_km,
    )
    suggested = None if feasible else _suggest_alternative(detected_distance, predicted_minutes, detected_target_minutes)
    message = _message(
        goal,
        detected_distance,
        detected_target_minutes,
        predicted_minutes,
        required_peak,
        current_weekly_km,
        weeks_to_close_gap,
        confidence,
        feasible,
        suggested,
        profile,
    )
    return GoalAssessment(
        feasible=feasible,
        confidence=confidence,
        detected_distance=detected_distance,
        detected_target_minutes=_round_optional(detected_target_minutes),
        user_predicted_minutes=_round_optional(predicted_minutes),
        required_peak_weekly_km=round(required_peak, 2),
        current_weekly_km=round(current_weekly_km, 2),
        weeks_to_close_gap=weeks_to_close_gap,
        message=message,
        suggested_alternative=suggested,
    )


def _parse_goal(goal: str) -> tuple[GoalDistance, float | None]:
    text = goal.lower()
    if "ultra" in text:
        distance: GoalDistance = "ultra"
    elif "marathon" in text and "half" not in text:
        distance = "marathon"
    elif "half marathon" in text or re.search(r"\bhalf\b", text):
        distance = "half_marathon"
    elif re.search(r"\b10\s?k\b", text):
        distance = "10k"
    elif re.search(r"\b5\s?k\b", text):
        distance = "5k"
    elif "base" in text:
        distance = "base"
    else:
        distance = "unknown"

    target = _parse_target_time(text, distance)
    return distance, target


def _parse_target_time(text: str, distance: GoalDistance) -> float | None:
    match = re.search(r"\bsub[-\s]*(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?\b", text)
    if not match:
        return None
    first = int(match.group(1))
    second = int(match.group(2)) if match.group(2) is not None else None
    third = int(match.group(3)) if match.group(3) is not None else None

    if third is not None and second is not None:
        return first * 60 + second + third / 60
    if second is not None:
        # For 5K/10K goals, "sub-45" is minutes; "sub-1:45" is hours:minutes.
        if first <= 5:
            return first * 60 + second
        return first + second / 60
    if distance in ("marathon", "half_marathon") and first <= 12:
        return float(first * 60)
    return float(first)


def _current_weekly_km(profile: AthleteProfile, history_summary: dict | None) -> float:
    if history_summary:
        progression = history_summary.get("progression", {})
        value = progression.get("second_half_avg_km")
        if isinstance(value, int | float) and value > 0:
            return float(value)
        totals = history_summary.get("totals", {})
        period = history_summary.get("period", {})
        distance = totals.get("total_distance_km")
        weeks = period.get("weeks")
        if isinstance(distance, int | float) and isinstance(weeks, int | float) and weeks > 0:
            return float(distance) / float(weeks)
    return profile.weekly_km_recent


def _predict_minutes(profile: AthleteProfile, distance: GoalDistance) -> float | None:
    target_distance = DISTANCE_KM.get(distance)
    if target_distance is None:
        return None
    if profile.recent_race:
        return profile.recent_race.time_minutes * (target_distance / profile.recent_race.distance_km) ** 1.06
    if profile.easy_pace_min_per_km is None:
        return None
    margins = {
        "5k": 1.0,
        "10k": 50 / 60,
        "half_marathon": 35 / 60,
        "marathon": 20 / 60,
    }
    race_pace = max(2.5, profile.easy_pace_min_per_km - margins[distance])
    return race_pace * target_distance


def _required_peak_km(distance: GoalDistance, target_minutes: float | None) -> float:
    target = target_minutes if target_minutes is not None else float("inf")
    table = {
        "marathon": MARATHON_PEAKS,
        "half_marathon": HALF_PEAKS,
        "10k": TEN_K_PEAKS,
        "5k": FIVE_K_PEAKS,
    }.get(distance)
    if table is None:
        return 30.0
    for upper, peak in table:
        if target <= upper:
            return float(peak)
    return float(table[-1][1])


def _weeks_to_close_gap(current: float, required: float) -> int | None:
    if current <= 0:
        return None
    if current >= required:
        return 0
    return math.ceil(math.log(required / current) / math.log(1.10))


def _feasibility(
    target: float | None,
    predicted: float | None,
    weeks_to_close_gap: int | None,
    available_weeks: int,
    required_peak: float,
    current_weekly_km: float,
) -> tuple[bool, Literal["high", "medium", "low", "very_low"]]:
    volume_closeable = weeks_to_close_gap is not None and weeks_to_close_gap <= max(0, available_weeks - 4)
    volume_already_close = current_weekly_km >= required_peak * 0.8

    if target is None:
        if volume_closeable or volume_already_close:
            return True, "medium"
        if current_weekly_km > 0:
            return True, "low"
        return False, "very_low"
    if predicted is None:
        if volume_closeable or volume_already_close:
            return True, "medium"
        if current_weekly_km >= required_peak * 0.5:
            return True, "low"
        return False, "very_low"

    ratio = predicted / target
    if ratio <= 1.10 and (volume_closeable or volume_already_close):
        return True, "high"
    if ratio <= 1.20 or volume_closeable:
        return True, "medium"
    if ratio <= 1.30:
        return True, "low"
    return False, "very_low"


def _suggest_alternative(distance: GoalDistance, predicted: float | None, target: float | None) -> str | None:
    if distance not in DISTANCE_KM:
        return None
    basis = predicted or target
    if basis is None:
        return None
    suggested_minutes = _round_up_to_clean_target(basis * 1.03)
    return f"sub-{_format_target(suggested_minutes, distance)} {_distance_label(distance)}"


def _round_up_to_clean_target(minutes: float) -> float:
    if minutes < 60:
        return float(math.ceil(minutes))
    return float(math.ceil(minutes / 5) * 5)


def _message(
    goal: str,
    distance: GoalDistance,
    target: float | None,
    predicted: float | None,
    required_peak: float,
    current_weekly_km: float,
    weeks_to_close_gap: int | None,
    confidence: str,
    feasible: bool,
    suggested: str | None,
    profile: AthleteProfile,
) -> str:
    predicted_text = f" Predicted {_distance_label(distance)} fitness is around {_format_minutes(predicted)}." if predicted else ""
    gap_text = (
        "no safe progression estimate because current weekly volume is 0 km"
        if weeks_to_close_gap is None
        else f"{weeks_to_close_gap} weeks of healthy 10%/week progression"
    )
    race_text = ""
    if profile.recent_race:
        race_text = (
            f" Based on your recent race/time trial ({profile.recent_race.distance_km:g} km "
            f"in {_format_minutes(profile.recent_race.time_minutes)}),"
        )

    if feasible:
        return (
            f"{goal} looks realistic enough to plan for from your current base. "
            f"Confidence: {confidence}. Peak weeks should reach about {required_peak:.0f} km."
            f"{predicted_text}"
        )

    target_text = f" Target time is {_format_minutes(target)}." if target else ""
    suggestion_text = f" Suggested alternative: {suggested}." if suggested else ""
    return (
        f"{race_text} current weekly volume is {current_weekly_km:.0f} km."
        f"{predicted_text}{target_text} This goal would require peak weekly volume of "
        f"{required_peak:.0f} km, and you would need {gap_text} to reach that safely. "
        f"Confidence: {confidence}.{suggestion_text}"
    ).strip()


def _format_minutes(value: float | None) -> str:
    if value is None:
        return "unknown"
    total_seconds = int(round(value * 60))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_target(minutes: float, distance: GoalDistance) -> str:
    if distance in ("5k", "10k") and minutes < 60:
        return str(int(minutes))
    total_minutes = int(minutes)
    hours, mins = divmod(total_minutes, 60)
    if hours:
        return f"{hours}:{mins:02d}"
    return str(mins)


def _distance_label(distance: GoalDistance) -> str:
    return {
        "5k": "5K",
        "10k": "10K",
        "half_marathon": "half marathon",
        "marathon": "marathon",
        "ultra": "ultra",
        "base": "base",
        "unknown": "goal distance",
    }[distance]


def _round_optional(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)
