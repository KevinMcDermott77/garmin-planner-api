from __future__ import annotations

import importlib
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


def authed_user() -> dict[str, str]:
    return {"sub": "user-123", "email": "runner@example.com"}


def make_client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    return TestClient(app)


class FakeSigningKey:
    key = "public-key"


class FakePyJWKClient:
    def __init__(self, url: str):
        self.url = url

    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        assert token == "valid-token"
        assert self.url == "https://example.supabase.co/auth/v1/.well-known/jwks.json"
        return FakeSigningKey()


def mock_valid_jwks_auth(monkeypatch):
    monkeypatch.setattr("app.auth._jwks_client", None)
    monkeypatch.setattr("app.auth.PyJWKClient", FakePyJWKClient)
    monkeypatch.setattr("app.auth.jwt.get_unverified_header", lambda token: {"alg": "ES256", "kid": "test-kid"})

    def fake_decode(token, key, algorithms, audience):
        assert token == "valid-token"
        assert key == "public-key"
        assert algorithms == ["ES256", "RS256"]
        assert audience == "authenticated"
        return authed_user()

    monkeypatch.setattr("app.auth.jwt.decode", fake_decode)


def test_generate_requires_auth(monkeypatch):
    client = make_client(monkeypatch)

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

    assert response.status_code == 401


def test_read_endpoints_require_auth(monkeypatch):
    client = make_client(monkeypatch)

    list_response = client.get("/api/plans")
    get_response = client.get("/api/plans/plan-123")

    assert list_response.status_code == 401
    assert get_response.status_code == 401


def test_generate_returns_saved_plan(monkeypatch):
    fake_supabase = FakeSupabaseClient()
    persistence_calls: dict[str, Any] = {}

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
                            "pace_low_min_per_km": 6.15,
                            "pace_high_min_per_km": 6.45,
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
        return Plan.model_validate(payload), {"input": 10, "output": 20, "max": 64000}

    monkeypatch.setattr("app.routers.plans.generate_plan", fake_generate_plan)

    def fake_save_profile(profile, user_id):
        persistence_calls["save_profile_user_id"] = user_id
        return "profiles-1"

    def fake_save_plan(**kwargs):
        persistence_calls["save_plan_user_id"] = kwargs["user_id"]
        return "plans-1"

    monkeypatch.setattr("app.routers.plans.persistence.save_profile", fake_save_profile)
    monkeypatch.setattr("app.routers.plans.persistence.save_plan", fake_save_plan)
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer valid-token"},
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
    assert persistence_calls == {
        "save_profile_user_id": "user-123",
        "save_plan_user_id": "user-123",
    }


def test_persistence_uses_service_client_and_plain_user_id(monkeypatch):
    fake_supabase = FakeSupabaseClient()
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    plan = Plan.model_validate(
        {
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
                            "pace_low_min_per_km": 6.15,
                            "pace_high_min_per_km": 6.45,
                            "steps": [],
                        }
                    ],
                }
            ],
        }
    )
    profile_payload = {
        "name": "Test Runner",
        "weekly_km_recent": 30,
        "longest_run_km_recent": 15,
        "easy_pace_min_per_km": 6.0,
        "days_per_week": 4,
    }
    from app.services import persistence
    from app.services.profile import AthleteProfile
    from app.services.sanity_check import assess_goal

    profile = AthleteProfile.model_validate(profile_payload)
    assessment = assess_goal("sub-4 marathon", profile, None, available_weeks=4)

    profile_id = persistence.save_profile(profile, user_id="user-123")
    plan_id = persistence.save_plan(
        profile_id=profile_id,
        user_id="user-123",
        goal="sub-4 marathon",
        weeks=4,
        race_date=None,
        plan=plan,
        assessment=assessment,
        tokens={"input": 10, "output": 20, "max": 64000},
    )

    assert profile_id == "profiles-1"
    assert plan_id == "plans-1"
    assert fake_supabase.tables["profiles"][0]["user_id"] == "user-123"
    assert fake_supabase.tables["plans"][0]["user_id"] == "user-123"
    assert len(fake_supabase.tables["scheduled_sessions"]) == 1
    assert fake_supabase.tables["scheduled_sessions"][0]["pace_low_min_per_km"] == 6.15
    assert fake_supabase.tables["scheduled_sessions"][0]["pace_high_min_per_km"] == 6.45
    assert not hasattr(fake_supabase, "auth")


def test_db_client_is_created_with_service_key_only(monkeypatch):
    calls = []

    def fake_create_client(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeSupabaseClient()

    import app.db as db

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-key")
    monkeypatch.setattr(db, "_supabase_client", None)
    monkeypatch.setitem(__import__("sys").modules, "supabase", type("FakeSupabaseModule", (), {
        "create_client": staticmethod(fake_create_client)
    }))

    client = db.get_supabase_client()

    assert isinstance(client, FakeSupabaseClient)
    assert calls == [(("https://example.supabase.co", "service-role-key"), {})]


def test_get_and_list_plans_scope_to_current_user(monkeypatch):
    fake_supabase = FakeSupabaseClient(
        tables={
            "plans": [
                {
                    "id": "plan-123",
                    "profile_id": "profile-123",
                    "user_id": "user-123",
                    "goal": "sub-4 marathon",
                    "weeks": 16,
                    "status": "draft",
                    "created_at": "2026-05-16T20:00:00Z",
                },
                {
                    "id": "plan-456",
                    "profile_id": "profile-456",
                    "user_id": "other-user",
                    "goal": "sub-3 marathon",
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
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    plan_response = client.get("/api/plans/plan-123", headers={"Authorization": "Bearer valid-token"})
    assert plan_response.status_code == 200
    assert plan_response.json()["scheduled_sessions"][0]["session_type"] == "easy"

    other_plan_response = client.get("/api/plans/plan-456", headers={"Authorization": "Bearer valid-token"})
    assert other_plan_response.status_code == 404

    list_response = client.get("/api/plans", headers={"Authorization": "Bearer valid-token"})
    assert list_response.status_code == 200
    assert [row["id"] for row in list_response.json()] == ["plan-123"]


def test_dev_token_route_is_not_registered_by_default(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    import app.main as main

    reloaded_main = importlib.reload(main)
    client = TestClient(reloaded_main.app)

    response = client.post("/api/dev/token", json={"email": "runner@example.com", "password": "secret"})

    assert response.status_code == 404
