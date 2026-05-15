from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.models import Plan


def test_preview_returns_valid_plan(monkeypatch):
    def fake_generate_plan(**kwargs):
        payload = {
            "goal": kwargs["goal"],
            "race_date": kwargs["race_date"],
            "weeks": [
                {
                    "week_number": 1,
                    "focus": "Base week",
                    "sessions": [
                        {
                            "day_of_week": 0,
                            "type": "easy",
                            "description": "Easy run",
                            "distance_km": 5.0,
                            "duration_min": 30,
                            "steps": [],
                        },
                        {
                            "day_of_week": 1,
                            "type": "rest",
                            "description": "Rest day",
                            "distance_km": None,
                            "duration_min": None,
                            "steps": [],
                        },
                    ],
                }
            ],
        }
        return Plan.model_validate(payload), {"input": 10, "output": 20, "max": 64000}

    monkeypatch.setattr("app.routers.plans.generate_plan", fake_generate_plan)
    client = TestClient(app)

    response = client.post(
        "/api/plans/preview",
        json={
            "profile": {
                "name": "Test Runner",
                "weekly_km_recent": 30,
                "longest_run_km_recent": 15,
                "easy_pace_min_per_km": 6.0,
                "days_per_week": 4,
            },
            "goal": "sub-4 marathon",
            "weeks": 4,
            "race_date": "2026-10-25",
            "history_summary": None,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["assessment"]["detected_distance"] == "marathon"
    assert data["plan"]["goal"] == "sub-4 marathon"
    assert data["plan"]["weeks"][0]["sessions"][0]["type"] == "easy"
    assert data["tokens"] == {"input": 10, "output": 20, "max": 64000}
