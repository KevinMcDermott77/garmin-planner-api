"""Race lookup helpers backed by Claude web search."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1600
TEMPERATURE = 0

Confidence = Literal["high", "medium", "low"]
RaceDistance = Literal["5k", "10k", "half_marathon", "marathon", "ultra", "other"]


class RaceLookupResult(BaseModel):
    race_name: str
    found: bool
    confidence: Confidence
    race_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    location: str | None = None
    distance: RaceDistance | None = None
    course_notes: str | None = None
    source_note: str


class RaceLookupError(Exception):
    """Raised when automatic race lookup cannot produce a usable response."""


def lookup_race(race_name: str) -> RaceLookupResult:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RaceLookupError("ANTHROPIC_API_KEY is required for race lookup.")

    client = anthropic.Anthropic(api_key=api_key)
    response = _create_message(client, race_name)
    tokens = _token_usage(response)
    _print_token_usage(tokens)
    text = _response_text(response)
    payload = _extract_json_object(text)
    try:
        result = RaceLookupResult.model_validate(payload)
    except ValidationError as exc:
        repaired = _repair_low_confidence(payload, race_name)
        if repaired is not None:
            return repaired
        raise RaceLookupError(f"Claude race lookup response failed validation: {exc}") from exc
    return _enforce_low_confidence_caveat(result)


def fallback_result(race_name: str) -> RaceLookupResult:
    return RaceLookupResult(
        race_name=race_name,
        found=False,
        confidence="low",
        race_date=None,
        location=None,
        distance=None,
        course_notes=None,
        source_note="Could not look up this race automatically - please enter details manually",
    )


def _create_message(client: anthropic.Anthropic, race_name: str) -> Any:
    return client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=_system_prompt(),
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
        messages=[
            {
                "role": "user",
                "content": f"Look up this race and return the JSON object only: {race_name}",
            }
        ],
    )


def _system_prompt() -> str:
    return """
You look up public running race details for a training-plan form.

Use web search to find the official race date, location, distance category, and
well-known course characteristics only when confidently known.

Rules:
- Prefer the official race website or organiser page for race_date.
- If the official date is not confidently found, set race_date=null and confidence="low".
- Never infer or assume a race date.
- course_notes must be null unless the characteristic is confidently known.
- ALWAYS include source_note.
- If confidence is not high, source_note must explicitly tell the user to verify the date on the official race website.
- Return ONLY valid JSON, no markdown fences, no preamble.

Schema:
{
  "race_name": "string",
  "found": true,
  "confidence": "high|medium|low",
  "race_date": "YYYY-MM-DD or null",
  "location": "string or null",
  "distance": "5k|10k|half_marathon|marathon|ultra|other|null",
  "course_notes": "string or null",
  "source_note": "string"
}
""".strip()


def _token_usage(response: Any) -> dict[str, int | str]:
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", "unknown")
    output_tokens = getattr(usage, "output_tokens", "unknown")
    return {"input": input_tokens, "output": output_tokens, "max": MAX_TOKENS}


def _print_token_usage(tokens: dict[str, int | str]) -> None:
    print(f"Claude race lookup tokens: input={tokens['input']}, output={tokens['output']}, max={tokens['max']}")


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    text = "\n".join(parts).strip()
    if not text:
        raise RaceLookupError("Claude returned an empty race lookup response.")
    return text


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _strip_code_fence(stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RaceLookupError("No JSON object found in race lookup response.")

    candidate = stripped[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        repaired = _repair_json(candidate)
        if repaired is None:
            raise RaceLookupError(f"Race lookup response was not valid JSON: {exc}") from exc
        data = repaired

    if not isinstance(data, dict):
        raise RaceLookupError("Race lookup response was not a JSON object.")
    return data


def _strip_code_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = text[start : end + 1]
    for repaired_candidate in (candidate, _close_open_json_containers(candidate)):
        try:
            data = json.loads(repaired_candidate)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else None
    return None


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


def _repair_low_confidence(payload: dict[str, Any], race_name: str) -> RaceLookupResult | None:
    payload = {
        "race_name": payload.get("race_name") or race_name,
        "found": bool(payload.get("found", False)),
        "confidence": payload.get("confidence") if payload.get("confidence") in {"high", "medium", "low"} else "low",
        "race_date": payload.get("race_date") if isinstance(payload.get("race_date"), str) else None,
        "location": payload.get("location") if isinstance(payload.get("location"), str) else None,
        "distance": payload.get("distance") if payload.get("distance") in {"5k", "10k", "half_marathon", "marathon", "ultra", "other"} else None,
        "course_notes": payload.get("course_notes") if isinstance(payload.get("course_notes"), str) else None,
        "source_note": payload.get("source_note") if isinstance(payload.get("source_note"), str) else "Date unverified - verify the date on the official race website.",
    }
    try:
        return _enforce_low_confidence_caveat(RaceLookupResult.model_validate(payload))
    except ValidationError:
        return None


def _enforce_low_confidence_caveat(result: RaceLookupResult) -> RaceLookupResult:
    if result.confidence != "high" and "official race website" not in result.source_note.lower():
        result.source_note = f"{result.source_note} Verify the date on the official race website."
    return result
