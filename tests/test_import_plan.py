from __future__ import annotations

import importlib
from typing import Any

from fastapi.testclient import TestClient

from tests.test_generate import FakeSupabaseClient, mock_valid_jwks_auth


def cli_plan_payload() -> dict[str, Any]:
    return {
        "goal": "sub-4 marathon",
        "race_date": "2026-10-25",
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
                        "pace_low_min_per_km": None,
                        "pace_high_min_per_km": None,
                        "steps": [],
                    },
                ],
            }
        ],
    }


def make_reloaded_client(monkeypatch, dev_mode: str) -> TestClient:
    monkeypatch.setenv("DEV_MODE", dev_mode)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    import app.main as main

    return TestClient(importlib.reload(main).app)


def test_import_plan_route_is_not_registered_by_default(monkeypatch):
    client = make_reloaded_client(monkeypatch, "false")

    response = client.post("/api/dev/import-plan", json={"plan_json": cli_plan_payload()})

    assert response.status_code == 404


def test_import_plan_requires_auth(monkeypatch):
    client = make_reloaded_client(monkeypatch, "true")

    response = client.post("/api/dev/import-plan", json={"plan_json": cli_plan_payload()})

    assert response.status_code == 401


def test_import_plan_validates_and_persists_active_plan(monkeypatch):
    fake_supabase = FakeSupabaseClient()

    client = make_reloaded_client(monkeypatch, "true")
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    mock_valid_jwks_auth(monkeypatch)

    response = client.post(
        "/api/dev/import-plan",
        headers={"Authorization": "Bearer valid-token"},
        json={"plan_json": cli_plan_payload()},
    )

    assert response.status_code == 200
    assert response.json() == {
        "plan_id": "plans-1",
        "weeks": 1,
        "goal": "sub-4 marathon",
        "imported": True,
    }
    saved_plan = fake_supabase.tables["plans"][0]
    assert saved_plan["profile_id"] is None
    assert saved_plan["user_id"] == "user-123"
    assert saved_plan["goal"] == "sub-4 marathon"
    assert saved_plan["weeks"] == 1
    assert saved_plan["race_date"] == "2026-10-25"
    assert saved_plan["assessment_json"] is None
    assert saved_plan["tokens_json"] is None
    assert saved_plan["status"] == "active"
    assert saved_plan["plan_json"]["weeks"][0]["sessions"][0]["pace_low_min_per_km"] is None
    assert len(fake_supabase.tables["scheduled_sessions"]) == 2
    assert fake_supabase.tables["scheduled_sessions"][0]["pace_low_min_per_km"] is None


def test_import_plan_malformed_returns_422_without_persisting(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_save_plan(**kwargs):
        calls.append(kwargs)
        return "plans-1"

    client = make_reloaded_client(monkeypatch, "true")
    monkeypatch.setattr("app.routers.dev.persistence.save_plan", fake_save_plan)
    mock_valid_jwks_auth(monkeypatch)

    response = client.post(
        "/api/dev/import-plan",
        headers={"Authorization": "Bearer valid-token"},
        json={"plan_json": {"goal": "sub-4 marathon", "weeks": "not-a-list"}},
    )

    assert response.status_code == 422
    assert calls == []
