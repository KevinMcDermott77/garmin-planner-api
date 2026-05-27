"""Claude-powered running plan generation."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from typing import Any

import anthropic
from pydantic import ValidationError

from app.services.models import Plan
from app.services.profile import AthleteProfile
from app.services.sanity_check import GoalAssessment

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 64000
MAX_ATTEMPTS = 3
TEMPERATURE = 0.3
RETRY_JSON_INSTRUCTION = (
    "IMPORTANT: The previous attempt produced malformed JSON or failed plan validation. "
    "Return ONLY a valid, complete JSON object matching the Plan schema. No markdown "
    "fences, no preamble, no truncation. Double-check all commas, brackets, and race-day "
    "placement and schedule-preference rules.\n\n"
)
DAY_LABELS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
DAY_INDEXES = {day: index for index, day in enumerate(DAY_LABELS)}


class PlanGenerationError(Exception):
    """Raised when Claude cannot produce a valid running plan."""


class JsonExtractionError(ValueError):
    """Raised when Claude response text cannot be decoded as a JSON object."""


def generate_plan(
    goal: str,
    weeks: int,
    history_summary: dict[str, Any] | None,
    race_date: date | None = None,
    notes: str | None = None,
    profile: AthleteProfile | None = None,
    goal_assessment: GoalAssessment | None = None,
    current_fitness: dict[str, Any] | None = None,
) -> tuple[Plan, dict[str, int | str]]:
    """Generate and validate a structured running plan using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise PlanGenerationError("ANTHROPIC_API_KEY is required. Add it to .env before running generate.")

    client = anthropic.Anthropic(api_key=api_key)
    base_user_prompt = _user_prompt(
        goal,
        weeks,
        history_summary,
        race_date,
        notes,
        profile,
        goal_assessment,
        current_fitness,
    )
    final_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            click_like_retry_message = f"Retry attempt {attempt}/{MAX_ATTEMPTS}..."
            print(click_like_retry_message)
            time.sleep(1)

        user_message = base_user_prompt if attempt == 1 else _retry_prompt(final_error, base_user_prompt)
        try:
            response = _create_message(client, user_message, _system_prompt(goal, weeks, profile, race_date, goal_assessment, current_fitness))
        except Exception as exc:  # noqa: BLE001
            raise PlanGenerationError(f"Claude API call failed: {exc}") from exc

        tokens = _token_usage(response)
        _print_token_usage(tokens)
        text = _response_text(response)
        try:
            payload = _extract_json_object(text)
        except JsonExtractionError as exc:
            final_error = exc
            continue

        try:
            plan = Plan.model_validate(payload)
        except ValidationError as exc:
            final_error = exc
            continue

        if len(plan.weeks) != weeks:
            final_error = PlanGenerationError(
                f"Claude response contained {len(plan.weeks)} weeks, expected {weeks}."
            )
            continue

        try:
            _validate_race_session(plan, race_date, goal_assessment)
            _validate_schedule_preferences(plan, race_date, profile)
        except PlanGenerationError as exc:
            final_error = exc
            continue

        return plan, tokens

    raise PlanGenerationError(
        f"Claude response did not produce a valid complete Plan after {MAX_ATTEMPTS} attempts: {final_error}"
    )


def _token_usage(response: Any) -> dict[str, int | str]:
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", "unknown")
    output_tokens = getattr(usage, "output_tokens", "unknown")
    return {"input": input_tokens, "output": output_tokens, "max": MAX_TOKENS}


def _print_token_usage(tokens: dict[str, int | str]) -> None:
    print(f"Claude tokens: input={tokens['input']}, output={tokens['output']}, max={tokens['max']}")


def _create_message(client: anthropic.Anthropic, user_message: str, system_prompt: str) -> Any:
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": user_message,
            }
        ],
    ) as stream:
        return stream.get_final_message()


def _system_prompt(
    goal: str,
    weeks: int,
    profile: AthleteProfile | None = None,
    race_date: date | None = None,
    goal_assessment: GoalAssessment | None = None,
    current_fitness: dict[str, Any] | None = None,
) -> str:
    coaching_principles = _coaching_principles_prompt(goal, weeks, profile, goal_assessment, current_fitness)
    prompt = """
You are an experienced running coach creating a personalised training plan.

Use the provided Garmin history summary as the baseline, including weekly volume,
pace distribution, HR zones, recent long runs, rest pattern, and weekly progression.

Programming rules:
- The plan must be specific to the goal distance and target time. Do not produce a
  generic beginner plan unless the goal assessment says that is the safest outcome.
- If the goal is low-confidence or infeasible but the user confirmed it anyway,
  build the safest possible bridge plan and avoid pretending the target is guaranteed.
- Start from Garmin history as ground truth when present. Athlete interview answers
  are context, especially when Garmin history is missing or incomplete.
{coaching_principles}
- RACE DAY OVERRIDES ALL SCHEDULE PREFERENCES. When race_date is supplied,
  the race MUST be placed on the exact race_date provided as a mandatory
  session of type "long". It is the goal event and a run: never type "rest",
  never type "cross", never moved, never converted, and never duplicated onto
  an adjacent day. Schedule preferences, including days_off, long_run_day, and
  quality days, do NOT apply to the race date itself. If race_date falls on a
  normally preferred rest day, that is fine and expected: keep the race there
  as type "long".
- The race session must contain the full race distance in distance_km
  (marathon=42.2, half marathon=21.1, 10k=10.0, 5k=5.0; infer from the goal),
  a sensible duration_min derived from the goal time, race-strategy text in
  description, and pace_low_min_per_km/pace_high_min_per_km set to the goal
  race pace range.
- The final week must be structured so the race lands exactly on race_date as
  the type "long" race session. day_of_week uses 0=Monday through 6=Sunday, so
  the race session's day_of_week MUST equal the weekday of race_date. Taper
  sessions in the final week still respect schedule preferences, but the race
  date itself is exempt and mandatory.
- Post-race day, if it falls within the plan range, may be rest as normal.
- Fill the rest with easy runs, recovery runs, cross training, or rest.
- Include at least one rest day every week.
- Keep sessions realistic for the athlete's recent volume and longest run.
- Use steps for structured workouts when useful, especially intervals and tempo work.
- Every unstructured running session must include a concrete target pace range.
  For easy, long, and recovery sessions with distance_km set and steps=[],
  populate pace_low_min_per_km and pace_high_min_per_km. Derive the range from
  the athlete's current easy pace when supplied, and from the goal pace and
  session type when a time goal is supplied. For a marathon goal with marathon
  pace (MP), useful defaults are:
  - easy: roughly MP + 0:45 to MP + 1:15 per km
  - long: roughly MP + 0:30 to MP + 1:00 per km
  - recovery: roughly MP + 1:00 to MP + 1:30 per km
  Use comparable effort-based ranges for other distances. Keep paces realistic
  for the athlete's recent training. Tempo and intervals keep pace targets in
  their steps only, with top-level pace fields null. Rest and cross sessions
  must also leave top-level pace fields null.

Return ONLY valid JSON. Do not include markdown, code fences, comments, preamble,
or explanatory text.

The JSON must match this schema exactly:
{
  "goal": "string",
  "race_date": "YYYY-MM-DD or null",
  "weeks": [
    {
      "week_number": 1,
      "focus": "string",
      "sessions": [
        {
          "day_of_week": 0,
          "type": "easy | long | tempo | intervals | recovery | rest | cross",
          "description": "string",
          "distance_km": 5.0 or null,
          "duration_min": 45 or null,
          "pace_low_min_per_km": 6.0 or null,
          "pace_high_min_per_km": 6.5 or null,
          "steps": [
            {
              "step_type": "warmup | interval | recovery | cooldown",
              "duration_type": "time | distance",
              "duration_value": 10.0,
              "target_type": "pace | heart_rate | none",
              "target_low": 5.0 or null,
              "target_high": 6.0 or null
            }
          ]
        }
      ]
    }
  ]
}

day_of_week uses 0=Monday through 6=Sunday.
pace_low_min_per_km and pace_high_min_per_km are required for easy, long, and
recovery sessions when steps=[]; they are decimal minutes per kilometre. For
tempo and intervals, leave top-level pace fields null because pace lives in
steps. For rest and cross sessions, leave top-level pace fields null.
For rest days, use distance_km=null, duration_min=null, pace_low_min_per_km=null,
pace_high_min_per_km=null, and steps=[].
For target_type="none", target_low and target_high must be null.
Distances are kilometres. Durations are minutes except WorkoutStep duration_value,
which follows duration_type: minutes for "time", kilometres for "distance".
""".replace("{coaching_principles}", coaching_principles).strip()
    constraints = _schedule_constraints_prompt(profile, race_date)
    if constraints:
        prompt += "\n\n" + constraints.strip()
    return prompt


def _user_prompt(
    goal: str,
    weeks: int,
    history_summary: dict[str, Any] | None,
    race_date: date | None,
    notes: str | None,
    profile: AthleteProfile | None,
    goal_assessment: GoalAssessment | None,
    current_fitness: dict[str, Any] | None = None,
) -> str:
    payload = {
        "goal": goal,
        "weeks": weeks,
        "race_date": race_date.isoformat() if race_date else None,
        "notes": notes,
        "athlete_profile": profile.model_dump(mode="json") if profile else None,
        "goal_assessment": goal_assessment.model_dump(mode="json") if goal_assessment else None,
        "history_summary": history_summary,
    }
    coaching_principles = _coaching_principles_prompt(goal, weeks, profile, goal_assessment, current_fitness)
    return (
        "Create a personalised running plan from this request, athlete profile, "
        "goal assessment, and Garmin history if present.\n"
        "Treat Garmin history as ground truth. Treat athlete profile answers as context.\n"
        "Use this concrete coaching calibration for this exact request:\n"
        f"{coaching_principles}\n"
        "Schedule preferences:\n"
        "- Use athlete_profile.schedule.long_run_day for the weekly long run whenever possible.\n"
        "- Use athlete_profile.schedule.quality_day_primary for the main tempo/interval workout.\n"
        "- If athlete_profile.schedule.quality_day_secondary is set, use it only for weeks where a second quality session is safe.\n"
        "- Do not schedule running sessions on athlete_profile.schedule.days_off; use rest or cross training instead.\n"
        "- RACE DAY OVERRIDES ALL SCHEDULE PREFERENCES: if race_date is supplied, place the race on exactly that date as a type \"long\" running session with the full inferred race distance. The race session day_of_week must equal race_date's weekday using 0=Monday through 6=Sunday. Do not move it, rest it, cross-train it, or duplicate it because of days_off or preferred long-run days.\n"
        "- Keep the number of running days compatible with athlete_profile.days_per_week.\n"
        "- Treat earliest_run_time and schedule notes as context for session realism.\n"
        f"{_schedule_constraints_prompt(profile, race_date)}"
        "Return only the JSON plan object.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


def _coaching_principles_prompt(
    goal: str,
    weeks: int,
    profile: AthleteProfile | None,
    goal_assessment: GoalAssessment | None,
    current_fitness: dict[str, Any] | None = None,
) -> str:
    days_per_week = profile.days_per_week if profile else 4
    weekly_km_recent = profile.weekly_km_recent if profile else 40.0
    longest_run_km_recent = profile.longest_run_km_recent if profile else 16.0
    phase_text = _phase_boundaries_prompt(weeks)
    peak_weekly_km = min(weekly_km_recent * 1.3, 65)
    absolute_max_km = 65 if days_per_week <= 4 else 75
    peak_long_run_km = min(32, weekly_km_recent * 0.8)
    race_sim_week = max(1, weeks - 3)
    pace_text = _pace_guidance_prompt(goal, goal_assessment, current_fitness, weeks)
    pattern_text = _weekly_pattern_prompt(profile)

    return f"""1. Session structure per week (non-taper weeks):
- For a 4-day/week plan:
  - The weekly pattern MUST be: quality1 on day A, REST on day B, quality2/easy-by-phase on day C, long run on day D, easy recovery on day E, with the athlete's days_off filling the remaining slots.
  - Never place running sessions on 4 consecutive days.
  - Always have at least one rest day between quality session 1 and quality session 2.
  - There must also be recovery separation after the long run before the next quality session.
- For a 5-day/week plan: quality1, easy, quality2, easy, long, with days_off filling the rest.
- For a 3-day/week plan: one quality session (alternating intervals/tempo), one easy, one long.
- Computed weekly pattern for this athlete:
{pattern_text}
- Every non-cutback, non-taper week MUST follow the computed hard/easy structure above. Never place two quality sessions on consecutive days.
- This athlete requested days_per_week={days_per_week}. Keep every week compatible with that running-day count and the schedule constraints below.

2. Quality session progression:
{phase_text}
- Phase 1 (base weeks): quality1 day = short intervals (400m-800m repeats); quality2 day = EASY recovery run when it falls on Sunday/day_of_week=6; long day = easy long run; easy recovery day = easy recovery. Sessions feel controlled and building.
- Phase 2 (development weeks): quality1 day = mile repeats or cruise intervals; quality2 day = threshold tempo (25-30 min); long day = long run with easy effort; easy recovery day = easy recovery. Sessions feel like solid work.
- Phase 3 (specific weeks): quality1 day = marathon-pace intervals or cruise intervals at MP; quality2 day = marathon-pace tempo (30-40 min); long day = long run WITH marathon-pace finish section (last 5-10km at MP); easy recovery day = easy recovery. Sessions feel race-specific.
- Phase 4 + peak weeks: quality1 day = under-and-overs or progressive intervals; quality2 day = race-simulation tempo; long day = progressive long run building to MP; easy recovery day = easy recovery. Sessions feel like race prep.
- Taper weeks: quality1 day = short sharp intervals to maintain neuromuscular sharpness; quality2 day = short MP tempo around 20 min; long day = reduced long run; easy recovery day = easy shakeout. Sessions feel fresh and sharp.
- Cutback weeks: keep the cutback behavior intentionally lighter. quality1 day = light intervals with reduced volume/intensity; the rest-between day remains REST; quality2 day becomes EASY instead of tempo/MP; long day = reduced long run; easy recovery day = easy. Four running days max, all easy/light except one light interval session.
- Sunday (day_of_week=6) session type by phase: BASE and CUTBACK weeks = easy recovery run (NOT tempo, NOT intervals). DEVELOPMENT/SPECIFIC/PEAK weeks = quality session 2 (tempo or MP work). TAPER weeks = easy shakeout or very short tempo only.
- Vary session names, descriptions, and structure week to week within each phase. Do not copy-paste the same interval/tempo template every week; each phase must have a different feel and different session emphasis.

3. Volume calibration:
- Peak weekly km MUST NOT exceed {peak_weekly_km:.1f}km. Compute as min(weekly_km_recent * 1.3, 65). For a {days_per_week}-day/week plan, the absolute maximum is {absolute_max_km}km/week.
- The total weekly volume (sum of ALL sessions including easy runs) MUST NOT exceed {peak_weekly_km:.1f}km in any week. This is a hard cap, not a guideline.
- A runner doing 40km/week should peak at about 52km, not 75km. Do not use weekly_km_recent * 1.8 or higher.
- Apply progressive overload. Do not increase weekly volume by more than about 10% except when the athlete's recent pattern clearly supports it.
- Cutback weeks drop to 60% of the preceding block's peak volume, not 80%. Place cutback weeks every 3-4 weeks to absorb training.

4. Long run progression:
- Weeks 1-4: long run = longest_run_km_recent * 0.8 to 1.0. With longest_run_km_recent={longest_run_km_recent:.1f}km, start long runs around {longest_run_km_recent * 0.8:.1f}-{longest_run_km_recent:.1f}km and do not start above current longest.
- Build long runs by max 2km per week.
- Peak long run: min(32, weekly_km_recent * 0.8) = {peak_long_run_km:.1f}km. For a 40km/week runner, peak long run is about 32km max.
- Race simulation week is week {race_sim_week}: replace the long run with "Half Marathon Race Practice -- 21km at goal marathon pace, treat as a dress rehearsal. This is NOT a rest week."
- Taper long run progression: 18km -> 13km -> race.

5. Pace guidance:
{pace_text}"""


def _phase_boundaries_prompt(weeks: int) -> str:
    phase1_end = max(1, int(weeks * 0.25))
    phase2_end = max(phase1_end + 1, int(weeks * 0.5))
    phase3_end = max(phase2_end + 1, int(weeks * 0.75))
    phase4_end = max(phase3_end, weeks - 3)
    final_start = max(1, weeks - 2)
    lines = [
        f"- Phase 1 is weeks 1-{phase1_end}.",
        f"- Phase 2 is weeks {phase1_end + 1}-{phase2_end}.",
        f"- Phase 3 is weeks {phase2_end + 1}-{phase3_end}.",
    ]
    if phase3_end + 1 <= phase4_end:
        lines.append(f"- Phase 4 is weeks {phase3_end + 1}-{phase4_end}.")
    else:
        lines.append("- Phase 4 is absorbed into the peak/taper transition because this is a short plan.")
    lines.append(f"- Final 3 weeks are weeks {final_start}-{weeks}.")
    lines.append(f"- Race week is week {weeks}.")
    return "\n".join(lines)


def _weekly_pattern_prompt(profile: AthleteProfile | None) -> str:
    if profile is None:
        return "- No athlete schedule was provided. Build the pattern from days_per_week, long_run_day, quality_day_primary, and days_off when available."

    roles = _computed_day_roles(profile)
    running_days = roles["running_days"]
    days_off = roles["days_off"]
    quality1 = roles["quality1"]
    quality2 = roles["quality2"]
    long_day = roles["long_day"]
    easy_days = roles["easy_days"]

    lines = [
        f"- running_days={_format_day_list(running_days)}.",
        f"- quality_day_1={_format_day_role(quality1)} (from athlete_profile.schedule.quality_day_primary).",
        f"- long_run_day={_format_day_role(long_day)} (from athlete_profile.schedule.long_run_day).",
    ]
    if quality2 is not None:
        lines.append(
            f"- quality_day_2={_format_day_role(quality2)} (first eligible running day after the long run that is not quality_day_1, not long_run_day, and not the day immediately before long_run_day)."
        )
    else:
        lines.append("- quality_day_2=none (not enough eligible running days after applying days_off, quality_day_1, long_run_day, and the pre-long-run exclusion).")
    lines.append(f"- easy_days={_format_day_list(easy_days)}.")
    lines.append("- Day-by-day mandatory schedule derived from the athlete profile:")

    for day in range(7):
        label = _day_label_from_index(day)
        if day in days_off:
            lines.append(f"  - day_of_week {day} ({label}): REST (days_off).")
        elif day == quality1:
            lines.append(f"  - day_of_week {day} ({label}): quality session 1 (intervals/speed).")
        elif day == long_day:
            lines.append(f"  - day_of_week {day} ({label}): LONG RUN -- mandatory every week.")
        elif quality2 is not None and day == quality2:
            if day == DAY_INDEXES["sun"]:
                lines.append(
                    f"  - day_of_week {day} ({label}): phase-based running day. "
                    "Sunday (day_of_week=6) session type by phase: BASE and CUTBACK weeks = easy recovery run "
                    "(NOT tempo, NOT intervals). DEVELOPMENT/SPECIFIC/PEAK weeks = quality session 2 "
                    "(tempo or MP work). TAPER weeks = easy shakeout or very short tempo only."
                )
            else:
                lines.append(f"  - day_of_week {day} ({label}): quality session 2 (tempo/MP work).")
        elif day in easy_days:
            phase_rule = ""
            if day == DAY_INDEXES["wed"]:
                phase_rule = " -- phase rule: TEMPO in base/cutback weeks, EASY in development/specific/peak/taper weeks"
            lines.append(f"  - day_of_week {day} ({label}): easy recovery run{phase_rule}.")

    lines.append("- EVERY day listed as running MUST have a running session every week.")
    if running_days:
        running_numbers = ", ".join(str(day) for day in running_days)
        lines.append(f"- NEVER place rest on day_of_week {running_numbers} for this athlete because those days are not in days_off.")
    if days_off:
        rest_numbers = ", ".join(str(day) for day in days_off)
        lines.append(f"- day_of_week {rest_numbers} are the only regular rest days because they are days_off.")
    return "\n".join(lines)


def _computed_day_roles(profile: AthleteProfile) -> dict[str, list[int] | int | None]:
    schedule = profile.schedule
    days_off = [day for day in range(7) if day in {DAY_INDEXES[name] for name in schedule.days_off}]
    running_days = [day for day in range(7) if day not in days_off]
    quality1 = DAY_INDEXES[schedule.quality_day_primary]
    long_day = DAY_INDEXES[schedule.long_run_day]
    day_before_long = (long_day - 1) % 7

    quality2_excluded = {quality1, long_day, day_before_long}
    quality2 = _first_running_day_after(long_day, running_days, quality2_excluded)
    if quality2 is None:
        quality2 = next((day for day in running_days if day not in {quality1, long_day}), None)

    anchors = {quality1, long_day}
    if quality2 is not None:
        anchors.add(quality2)
    easy_days = [day for day in running_days if day not in anchors]

    return {
        "running_days": running_days,
        "days_off": days_off,
        "quality1": quality1,
        "quality2": quality2,
        "long_day": long_day,
        "easy_days": easy_days,
    }


def _first_running_day_after(start_day: int, running_days: list[int], excluded: set[int]) -> int | None:
    for offset in range(1, 8):
        candidate = (start_day + offset) % 7
        if candidate in running_days and candidate not in excluded:
            return candidate
    return None


def _format_day_list(day_indexes: list[int]) -> str:
    if not day_indexes:
        return "[]"
    return "[" + ", ".join(_format_day_role(day) for day in day_indexes) + "]"


def _format_day_role(day_index: int | None) -> str:
    if day_index is None:
        return "none"
    return f"{_day_label_from_index(day_index).lower()} (day_of_week {day_index})"


def _pace_guidance_prompt(
    goal: str,
    goal_assessment: GoalAssessment | None,
    current_fitness: dict[str, Any] | None = None,
    weeks: int = 20,
) -> str:
    goal_minutes = _goal_minutes(goal, goal_assessment)
    distance_km = _race_distance_km(goal, goal_assessment)

    if (
        current_fitness is not None
        and current_fitness.get("easy_pace_min_per_km") is not None
        and current_fitness.get("predicted_finish_minutes") is not None
        and goal_minutes is not None
        and distance_km is not None
    ):
        return _progressive_pace_prompt(current_fitness, goal_minutes, distance_km, weeks)

    if goal_minutes is None or distance_km is None:
        return (
            "- No exact target time was detected. Use athlete easy pace, recent history, "
            "and goal distance to set realistic easy, long, tempo, interval, and recovery paces."
        )

    mp = goal_minutes / distance_km
    easy_low = mp + 60 / 60
    easy_high = mp + 75 / 60
    long_low = mp + 45 / 60
    long_high = mp + 60 / 60
    tempo_low = mp - 15 / 60
    tempo_high = mp + 15 / 60
    interval_low = mp - 45 / 60
    interval_high = mp - 30 / 60
    recovery = mp + 90 / 60
    return (
        f'- For "{goal}" (goal {goal_minutes:.0f} min): '
        f"MP={_format_pace_value(mp)}/km, "
        f"easy={_format_pace_value(easy_low)}-{_format_pace_value(easy_high)}/km, "
        f"long={_format_pace_value(long_low)}-{_format_pace_value(long_high)}/km, "
        f"tempo={_format_pace_value(tempo_low)}-{_format_pace_value(tempo_high)}/km, "
        f"intervals={_format_pace_value(interval_low)}-{_format_pace_value(interval_high)}/km, "
        f"recovery={_format_pace_value(recovery)}/km+."
    )


def _progressive_pace_prompt(
    current_fitness: dict[str, Any],
    goal_minutes: float,
    distance_km: float,
    weeks: int,
) -> str:
    """Build phase-by-phase pace targets interpolating from current fitness to goal paces."""
    easy_pace = float(current_fitness["easy_pace_min_per_km"])
    predicted_minutes = float(current_fitness["predicted_finish_minutes"])

    current_mp = predicted_minutes / distance_km
    goal_mp = goal_minutes / distance_km

    phase1_end = max(1, int(weeks * 0.25))
    phase2_end = max(phase1_end + 1, int(weeks * 0.5))
    phase3_end = max(phase2_end + 1, int(weeks * 0.75))
    phase4_end = max(phase3_end, weeks - 3)
    taper_start = phase4_end + 1

    phases: list[tuple[str, str, float]] = [
        ("Phase 1", f"weeks 1-{phase1_end}", 0.0),
        ("Phase 2", f"weeks {phase1_end + 1}-{phase2_end}", 0.33),
        ("Phase 3", f"weeks {phase2_end + 1}-{phase3_end}", 0.66),
    ]
    if phase3_end + 1 <= phase4_end:
        phases.append(("Phase 4/peak", f"weeks {phase3_end + 1}-{phase4_end}", 1.0))
    if taper_start <= weeks:
        phases.append(("Taper", f"weeks {taper_start}-{weeks}", 1.0))

    lines = ["PACE TARGETS BY PHASE (use these exact ranges):"]
    for label, week_range, r in phases:
        p = _phase_paces(r, easy_pace, current_mp, goal_mp)
        lines.append(
            f"{label} ({week_range}): "
            f"easy={p['easy_low']}-{p['easy_high']}/km, "
            f"tempo={p['tempo_low']}-{p['tempo_high']}/km, "
            f"intervals={p['intervals_low']}-{p['intervals_high']}/km, "
            f"long={p['long_low']}-{p['long_high']}/km"
        )
    return "\n".join(lines)


def _phase_paces(r: float, easy_pace: float, current_mp: float, goal_mp: float) -> dict[str, str]:
    """Compute pace range strings for a phase at interpolation ratio r (0=current, 1=goal)."""
    def lerp(a: float, b: float) -> float:
        return a + r * (b - a)

    easy_low = lerp(easy_pace, goal_mp + 4 / 60)
    easy_high = easy_low + 15 / 60

    long_low = lerp(current_mp + 12 / 60, goal_mp)
    long_high = lerp(current_mp + 37 / 60, goal_mp + 19 / 60)

    tempo_low = lerp(current_mp - 24 / 60, goal_mp - 41 / 60)
    tempo_high = lerp(current_mp - 4 / 60, goal_mp - 21 / 60)

    intervals_low = lerp(current_mp - 54 / 60, goal_mp - 61 / 60)
    intervals_high = lerp(current_mp - 39 / 60, goal_mp - 45 / 60)

    return {
        "easy_low": _format_pace_value(easy_low),
        "easy_high": _format_pace_value(easy_high),
        "long_low": _format_pace_value(long_low),
        "long_high": _format_pace_value(long_high),
        "tempo_low": _format_pace_value(tempo_low),
        "tempo_high": _format_pace_value(tempo_high),
        "intervals_low": _format_pace_value(intervals_low),
        "intervals_high": _format_pace_value(intervals_high),
    }


def _goal_minutes(goal: str, goal_assessment: GoalAssessment | None) -> float | None:
    if goal_assessment and goal_assessment.detected_target_minutes is not None:
        return float(goal_assessment.detected_target_minutes)
    distance = _distance_name(goal, goal_assessment)
    return _parse_target_time(goal.lower(), distance)


def _distance_name(goal: str, goal_assessment: GoalAssessment | None) -> str:
    detected = goal_assessment.detected_distance if goal_assessment else None
    if detected:
        return detected
    text = goal.lower()
    if "marathon" in text and "half" not in text:
        return "marathon"
    if "half marathon" in text or re.search(r"\bhalf\b", text):
        return "half_marathon"
    if re.search(r"\b10\s?k\b", text):
        return "10k"
    if re.search(r"\b5\s?k\b", text):
        return "5k"
    return "unknown"


def _parse_target_time(text: str, distance: str) -> float | None:
    match = re.search(r"\bsub[-\s]*(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?\b", text)
    if not match:
        return None
    first = int(match.group(1))
    second = int(match.group(2)) if match.group(2) is not None else None
    third = int(match.group(3)) if match.group(3) is not None else None
    if third is not None and second is not None:
        return first * 60 + second + third / 60
    if second is not None:
        if first <= 5:
            return first * 60 + second
        return first + second / 60
    if distance in ("marathon", "half_marathon") and first <= 12:
        return float(first * 60)
    return float(first)


def _format_pace_value(value: float) -> str:
    total_seconds = int(round(value * 60))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _schedule_constraints_prompt(profile: AthleteProfile | None, race_date: date | None) -> str:
    if profile is None:
        return ""

    schedule = profile.schedule
    lines = ["Concrete schedule constraints for this exact athlete:"]
    if schedule.days_off:
        days_off = ", ".join(_day_label(day) for day in schedule.days_off)
        rest_rules = " and ".join(f"every {_day_label(day)}" for day in schedule.days_off)
        lines.append(
            f"- The athlete's days off are: {days_off}. {rest_rules} in EVERY week "
            "MUST be type 'rest'. No exceptions, including recovery weeks and race week, "
            "except the race date itself which overrides this rule per the race-day rule. "
            "Never schedule any running, quality, long, easy, recovery, or cross session "
            "on these days_off days."
        )
    else:
        lines.append("- The athlete did not list any days_off.")

    lines.append(
        f"- The long run MUST be placed on {_day_label(schedule.long_run_day)} "
        "every week it occurs, except the race date itself if race_date requires a different day."
    )
    lines.append(
        f"- The primary quality session MUST be on {_day_label(schedule.quality_day_primary)}."
    )
    if schedule.quality_day_secondary:
        lines.append(
            f"- Use the secondary quality day, {_day_label(schedule.quality_day_secondary)}, "
            "only when a second quality session is safe and appropriate."
        )

    lines.append(_weekly_pattern_prompt(profile))

    if _cross_training_allowed(profile):
        lines.append(
            f"- Cross-training is explicitly allowed because profile.cross_training is: {profile.cross_training}."
        )
    else:
        lines.append(
            "- profile.cross_training is empty/null. Do NOT introduce cross-training. "
            "The plan must contain ZERO sessions of type 'cross'."
        )

    lines.append(
        f"- Honour days_per_week={profile.days_per_week}: each week should contain exactly "
        f"{profile.days_per_week} non-rest running days when feasible, and MUST NOT exceed "
        f"{profile.days_per_week} running days. Count only easy, long, tempo, intervals, "
        "and recovery as running days; rest and cross are not running days."
    )
    if race_date is not None:
        lines.append(
            f"- race_date={race_date.isoformat()} falls on {_day_label_from_index(race_date.weekday())}; "
            "this single date still overrides all schedule preferences as already specified."
        )
    return "\n".join(lines) + "\n"


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    text = "\n".join(parts).strip()
    if not text:
        raise PlanGenerationError("Claude returned an empty response.")
    return text


def _retry_prompt(final_error: Exception | None, base_user_prompt: str) -> str:
    detail = f"Previous validation error: {final_error}\n\n" if final_error else ""
    return RETRY_JSON_INSTRUCTION + detail + base_user_prompt


def _validate_race_session(
    plan: Plan,
    requested_race_date: date | None,
    goal_assessment: GoalAssessment | None,
) -> None:
    if requested_race_date is None:
        return

    if plan.race_date != requested_race_date:
        raise PlanGenerationError(
            "Race date was supplied, but the generated plan did not preserve "
            f"race_date={requested_race_date.isoformat()}."
        )

    race_distance = _race_distance_km(plan.goal, goal_assessment)
    if race_distance is None:
        return

    if not plan.weeks:
        raise PlanGenerationError("Race date was supplied, but the plan contains no weeks.")

    final_week = plan.weeks[-1]
    race_longs = [
        session
        for session in final_week.sessions
        if session.type == "long" and session.distance_km is not None and abs(session.distance_km - race_distance) <= 0.2
    ]
    if len(race_longs) != 1:
        raise PlanGenerationError(
            "Race date was supplied, but the final week must contain exactly one "
            f"type 'long' race session at {race_distance:.1f} km."
        )

    race_session = race_longs[0]
    expected_day = requested_race_date.weekday()
    if race_session.day_of_week != expected_day:
        raise PlanGenerationError(
            "Race date was supplied, but the final-week race session has "
            f"day_of_week={race_session.day_of_week}; race_date "
            f"{requested_race_date.isoformat()} requires day_of_week={expected_day}."
        )

    if not _description_mentions_race(race_session.description, plan.goal, goal_assessment):
        raise PlanGenerationError(
            "Race date was supplied, but the final-week long race session description "
            "must clearly identify it as the race."
        )


def _validate_schedule_preferences(
    plan: Plan,
    requested_race_date: date | None,
    profile: AthleteProfile | None,
) -> None:
    if profile is None:
        return

    roles = _computed_day_roles(profile)
    days_off = set(roles["days_off"])
    running_days = set(roles["running_days"])
    running_session_types = {"easy", "long", "tempo", "intervals", "recovery"}

    race_day = requested_race_date.weekday() if requested_race_date is not None else None
    final_week_number = plan.weeks[-1].week_number if plan.weeks else None
    for week in plan.weeks:
        for session in week.sessions:
            if session.day_of_week not in days_off:
                continue
            if (
                requested_race_date is not None
                and week.week_number == final_week_number
                and session.day_of_week == race_day
            ):
                continue
            if session.type != "rest":
                raise PlanGenerationError(
                    "Schedule preferences require days_off to be rest, but "
                    f"week {week.week_number} {_day_label_from_index(session.day_of_week)} "
                    f"was generated as type '{session.type}'."
                )

    if not _cross_training_allowed(profile):
        cross_sessions = [
            f"week {week.week_number} {_day_label_from_index(session.day_of_week)}"
            for week in plan.weeks
            for session in week.sessions
            if session.type == "cross"
        ]
        if cross_sessions:
            locations = ", ".join(cross_sessions)
            raise PlanGenerationError(
                "profile.cross_training is empty/null, but the generated plan contains "
                f"type 'cross' sessions at: {locations}."
            )

    for week in plan.weeks:
        sessions_by_day = {session.day_of_week: session for session in week.sessions}
        for day in running_days:
            session = sessions_by_day.get(day)
            if session is None:
                raise PlanGenerationError(
                    "Every non-days_off day must have a running session, but "
                    f"week {week.week_number} {_day_label_from_index(day)} is missing."
                )
            if session.type not in running_session_types:
                raise PlanGenerationError(
                    "Every non-days_off day must have a running session, but "
                    f"week {week.week_number} {_day_label_from_index(day)} "
                    f"was generated as type '{session.type}'."
                )


def _cross_training_allowed(profile: AthleteProfile) -> bool:
    return bool(profile.cross_training and profile.cross_training.strip())


def _day_label(day: str) -> str:
    return DAY_LABELS.get(day, day)


def _day_label_from_index(day_index: int) -> str:
    for day, index in DAY_INDEXES.items():
        if index == day_index:
            return DAY_LABELS[day]
    return f"day_of_week={day_index}"


def _race_distance_km(goal: str, goal_assessment: GoalAssessment | None) -> float | None:
    detected = goal_assessment.detected_distance if goal_assessment else None
    if detected == "marathon":
        return 42.2
    if detected == "half_marathon":
        return 21.1
    if detected == "10k":
        return 10.0
    if detected == "5k":
        return 5.0

    goal_text = goal.lower()
    if "marathon" in goal_text and "half" not in goal_text:
        return 42.2
    if "half marathon" in goal_text or "half-marathon" in goal_text:
        return 21.1
    if "10k" in goal_text or "10 k" in goal_text:
        return 10.0
    if "5k" in goal_text or "5 k" in goal_text:
        return 5.0
    return None


def _description_mentions_race(description: str, goal: str, goal_assessment: GoalAssessment | None) -> bool:
    text = description.lower()
    if "race" in text:
        return True
    detected = goal_assessment.detected_distance if goal_assessment else None
    if detected == "marathon" or ("marathon" in goal.lower() and "half" not in goal.lower()):
        return "marathon" in text
    if detected == "half_marathon" or "half" in goal.lower():
        return "half" in text
    if detected == "10k" or "10k" in goal.lower() or "10 k" in goal.lower():
        return "10k" in text or "10 k" in text
    if detected == "5k" or "5k" in goal.lower() or "5 k" in goal.lower():
        return "5k" in text or "5 k" in text
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _strip_code_fence(stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise JsonExtractionError("no JSON object braces found in Claude response")

    candidate = stripped[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        data = _repair_json(candidate, exc)

    if not isinstance(data, dict):
        raise JsonExtractionError("expected a JSON object at the top level")
    return data


def _strip_code_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_json(text: str, original_error: json.JSONDecodeError) -> Any:
    if not _is_repairable_error(original_error):
        raise JsonExtractionError(_format_json_error(original_error)) from original_error

    cutoff = _nearest_json_boundary(text, original_error.pos)
    if cutoff is None:
        raise JsonExtractionError(_format_json_error(original_error)) from original_error

    truncated = text[: cutoff + 1].rstrip()
    candidates = [truncated, _close_open_json_containers(truncated)]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise JsonExtractionError(_format_json_error(original_error)) from original_error


def _is_repairable_error(exc: json.JSONDecodeError) -> bool:
    return "Expecting ','" in exc.msg or "Expecting property name" in exc.msg


def _nearest_json_boundary(text: str, position: int) -> int | None:
    object_boundary = text.rfind("}", 0, position)
    array_boundary = text.rfind("]", 0, position)
    boundary = max(object_boundary, array_boundary)
    return boundary if boundary >= 0 else None


def _close_open_json_containers(text: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            expected = "{" if char == "}" else "["
            if stack[-1] == expected:
                stack.pop()

    closers = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return text.rstrip().rstrip(",") + closers


def _format_json_error(exc: json.JSONDecodeError) -> str:
    return f"{exc.msg}: line {exc.lineno} column {exc.colno} (char {exc.pos})"
