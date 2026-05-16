from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.services.models import Plan


@dataclass
class FakeSupabaseClient:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)

    def table(self, name: str) -> "FakeSupabaseQuery":
        return FakeSupabaseQuery(self, name)


@dataclass
class FakeResponse:
    data: list[dict[str, Any]]


class FakeSupabaseQuery:
    def __init__(self, client: FakeSupabaseClient, table_name: str):
        self.client = client
        self.table_name = table_name
        self.action: str | None = None
        self.payload: dict[str, Any] | list[dict[str, Any]] | None = None
        self.filters: list[tuple[str, Any]] = []
        self.limit_count: int | None = None

    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> "FakeSupabaseQuery":
        self.action = "insert"
        self.payload = payload
        return self

    def select(self, *_args: str) -> "FakeSupabaseQuery":
        self.action = "select"
        return self

    def eq(self, key: str, value: Any) -> "FakeSupabaseQuery":
        self.filters.append((key, value))
        return self

    def limit(self, count: int) -> "FakeSupabaseQuery":
        self.limit_count = count
        return self

    def order(self, *_args: str, **_kwargs: Any) -> "FakeSupabaseQuery":
        return self

    def execute(self) -> FakeResponse:
        if self.action == "insert":
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            saved = []
            for row in rows:
                assert row is not None
                next_id = self.client.ids.get(self.table_name, 0) + 1
                self.client.ids[self.table_name] = next_id
                saved_row = dict(row)
                saved_row.setdefault("id", f"{self.table_name}-{next_id}")
                self.client.tables.setdefault(self.table_name, []).append(saved_row)
                saved.append(saved_row)
            return FakeResponse(saved)

        rows = list(self.client.tables.get(self.table_name, []))
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        if self.limit_count is not None:
            rows = rows[: self.limit_count]
        return FakeResponse(rows)


def test_generate_returns_saved_plan(monkeypatch):
    fake_supabase = FakeSupabaseClient()

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

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setattr("app.routers.plans.generate_plan", fake_generate_plan)
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    client = TestClient(app)

    response = client.post(
        "/api/plans/generate",
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
    assert data["profile_id"] == "profiles-1"
    assert data["plan_id"] == "plans-1"
    assert data["assessment"]["detected_distance"] == "marathon"
    assert data["plan"]["goal"] == "sub-4 marathon"
    assert data["plan"]["weeks"][0]["sessions"][0]["type"] == "easy"
    assert data["tokens"] == {"input": 10, "output": 20, "max": 64000}
    assert len(fake_supabase.tables["scheduled_sessions"]) == 2
    assert fake_supabase.tables["scheduled_sessions"][0]["scheduled_date"] is None


def test_get_and_list_plans_use_saved_supabase_rows(monkeypatch):
    fake_supabase = FakeSupabaseClient(
        tables={
            "plans": [
                {
                    "id": "plan-123",
                    "profile_id": "profile-123",
                    "goal": "sub-4 marathon",
                    "weeks": 16,
                    "status": "draft",
                    "created_at": "2026-05-16T20:00:00Z",
                }
            ],
            "scheduled_sessions": [
                {
                    "id": "session-123",
                    "plan_id": "plan-123",
                    "week_number": 1,
                    "day_of_week": 0,
                    "session_type": "easy",
                }
            ],
        }
    )
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    client = TestClient(app)

    plan_response = client.get("/api/plans/plan-123")
    assert plan_response.status_code == 200
    assert plan_response.json()["scheduled_sessions"][0]["session_type"] == "easy"

    list_response = client.get("/api/plans", params={"profile_id": "profile-123"})
    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == "plan-123"
