"""Claude-powered running plan generation."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Any

import anthropic
from pydantic import ValidationError

from app.services.models import Plan
from app.services.profile import AthleteProfile
from app.services.sanity_check import GoalAssessment

MODEL = "claude-opus-4-5"
MAX_TOKENS = 64000
MAX_ATTEMPTS = 3
TEMPERATURE = 0.3
RETRY_JSON_INSTRUCTION = (
    "IMPORTANT: The previous attempt produced malformed JSON. Return ONLY a valid, "
    "complete JSON object matching the Plan schema. No markdown fences, no preamble, "
    "no truncation. Double-check all commas and brackets.\n\n"
)


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
    )
    final_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            click_like_retry_message = f"Retry attempt {attempt}/{MAX_ATTEMPTS}..."
            print(click_like_retry_message)
            time.sleep(1)

        user_message = base_user_prompt if attempt == 1 else RETRY_JSON_INSTRUCTION + base_user_prompt
        try:
            response = _create_message(client, user_message)
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


def _create_message(client: anthropic.Anthropic, user_message: str) -> Any:
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=_system_prompt(),
        messages=[
            {
                "role": "user",
                "content": user_message,
            }
        ],
    ) as stream:
        return stream.get_final_message()


def _system_prompt() -> str:
    return """
You are an experienced running coach creating a personalised training plan.

Use the provided Garmin history summary as the baseline, including weekly volume,
pace distribution, HR zones, recent long runs, rest pattern, and weekly progression.

Programming rules:
- The plan must be specific to the goal distance and target time. Do not produce a
  generic beginner plan unless the goal assessment says that is the safest outcome.
- If a goal assessment is provided, use required_peak_weekly_km as the intended
  peak volume for feasible goals. Peak week distance should land within about 10%
  of that number, unless injury context or very low confidence makes that unsafe.
- If the goal is low-confidence or infeasible but the user confirmed it anyway,
  build the safest possible bridge plan toward the required peak volume and avoid
  pretending the target is guaranteed.
- Start from Garmin history as ground truth when present. Athlete interview answers
  are context, especially when Garmin history is missing or incomplete.
- Apply progressive overload. Do not increase weekly volume by more than about 10%
  except when the athlete's recent pattern clearly supports it.
- Cut back volume every 3-4 weeks to absorb training.
- Include exactly one long run most weeks.
- Include one quality session most weeks, either tempo or intervals.
- Respect athlete_profile.schedule when present: put the long run on
  long_run_day, put primary quality on quality_day_primary, use
  quality_day_secondary only when a second quality session is appropriate, and
  avoid days_off for running sessions.
- Fill the rest with easy runs, recovery runs, cross training, or rest.
- Include at least one rest day every week.
- Taper in the final 2-3 weeks if a race date is supplied.
- Keep sessions realistic for the athlete's recent volume and longest run.
- For marathon goals, build long runs and weekly volume enough to support the
  target, using the required_peak_weekly_km value as a hard planning anchor.
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
""".strip()


def _user_prompt(
    goal: str,
    weeks: int,
    history_summary: dict[str, Any] | None,
    race_date: date | None,
    notes: str | None,
    profile: AthleteProfile | None,
    goal_assessment: GoalAssessment | None,
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
    return (
        "Create a personalised running plan from this request, athlete profile, "
        "goal assessment, and Garmin history if present.\n"
        "Treat Garmin history as ground truth. Treat athlete profile answers as context.\n"
        "Respect required_peak_weekly_km in the goal assessment when feasible.\n"
        "Schedule preferences:\n"
        "- Use athlete_profile.schedule.long_run_day for the weekly long run whenever possible.\n"
        "- Use athlete_profile.schedule.quality_day_primary for the main tempo/interval workout.\n"
        "- If athlete_profile.schedule.quality_day_secondary is set, use it only for weeks where a second quality session is safe.\n"
        "- Do not schedule running sessions on athlete_profile.schedule.days_off; use rest or cross training instead.\n"
        "- Keep the number of running days compatible with athlete_profile.days_per_week.\n"
        "- Treat earliest_run_time and schedule notes as context for session realism.\n"
        "Return only the JSON plan object.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    text = "\n".join(parts).strip()
    if not text:
        raise PlanGenerationError("Claude returned an empty response.")
    return text


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
