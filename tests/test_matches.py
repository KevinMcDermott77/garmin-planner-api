from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app


USER_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class FakeResponse:
    data: list[dict[str, Any]]


@dataclass
class FakeSupabaseClient:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def table(self, name: str) -> "FakeSupabaseQuery":
        return FakeSupabaseQuery(self, name)


class FakeSupabaseQuery:
    def __init__(self, client: FakeSupabaseClient, table_name: str):
        self.client = client
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []
        self.limit_count: int | None = None

    def select(self, *_args: str) -> "FakeSupabaseQuery":
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
        rows = list(self.client.tables.get(self.table_name, []))
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        if self.limit_count is not None:
            rows = rows[: self.limit_count]
        return FakeResponse(rows)


class FakeSigningKey:
    key = "public-key"


class FakePyJWKClient:
    def __init__(self, url: str):
        self.url = url

    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        assert token == "valid-token"
        return FakeSigningKey()


def mock_auth(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setattr("app.auth._jwks_client", None)
    monkeypatch.setattr("app.auth.PyJWKClient", FakePyJWKClient)
    monkeypatch.setattr("app.auth.jwt.get_unverified_header", lambda _token: {"alg": "ES256"})

    def fake_decode(token, key, algorithms, audience, leeway):
        assert token == "valid-token"
        assert key == "public-key"
        assert algorithms == ["ES256", "RS256"]
        assert audience == "authenticated"
        assert leeway == 60
        return {"sub": USER_ID, "email": "runner@example.com"}

    monkeypatch.setattr("app.auth.jwt.decode", fake_decode)


def make_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    return TestClient(app)


def strava_key(monkeypatch) -> None:
    monkeypatch.setenv("STRAVA_CLIENT_ID", "client-123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "secret-123")
    monkeypatch.setenv("STRAVA_REDIRECT_URI", "http://localhost:8000/api/strava/callback")
    monkeypatch.setenv("STRAVA_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def plan_json(race_date: str | None = "2026-02-26") -> dict[str, Any]:
    return {
        "goal": "Half marathon",
        "race_date": race_date,
        "weeks": [
            {
                "week_number": 1,
                "focus": "Base",
                "sessions": [
                    {
                        "day_of_week": 0,
                        "type": "easy",
                        "description": "Easy run",
                        "distance_km": 10,
                        "duration_min": 60,
                        "steps": [],
                    },
                    {
                        "day_of_week": 1,
                        "type": "tempo",
                        "description": "Tempo run",
                        "distance_km": 10,
                        "duration_min": 55,
                        "steps": [],
                    },
                    {
                        "day_of_week": 2,
                        "type": "long",
                        "description": "Long run",
                        "distance_km": 20,
                        "duration_min": 120,
                        "steps": [],
                    },
                    {
                        "day_of_week": 3,
                        "type": "easy",
                        "description": "Easy run",
                        "distance_km": 8,
                        "duration_min": 45,
                        "steps": [],
                    },
                    {
                        "day_of_week": 4,
                        "type": "rest",
                        "description": "Rest",
                        "distance_km": None,
                        "duration_min": None,
                        "steps": [],
                    },
                ],
            }
        ],
    }


def fake_db(plan: dict[str, Any], token: str | None = "access") -> FakeSupabaseClient:
    tables: dict[str, list[dict[str, Any]]] = {
        "plans": [
            {
                "id": "plan-123",
                "user_id": USER_ID,
                "profile_id": "profile-123",
                "goal": "Half marathon",
                "weeks": 1,
                "race_date": plan.get("race_date"),
                "plan_json": plan,
                "assessment_json": None,
                "tokens_json": {},
            }
        ],
        "scheduled_sessions": [],
    }
    if token is not None:
        from app.services import strava

        tables["strava_tokens"] = [
            {
                "user_id": USER_ID,
                "access_token": strava._encrypt(token),
                "refresh_token": strava._encrypt("refresh"),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        ]
    return FakeSupabaseClient(tables=tables)


def test_matches_requires_auth(monkeypatch):
    client = make_client(monkeypatch)

    response = client.get("/api/plans/plan-123/matches")

    assert response.status_code == 401


def test_matches_returns_404_for_plan_not_owned(monkeypatch):
    mock_auth(monkeypatch)
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: FakeSupabaseClient())
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: FakeSupabaseClient())
    client = make_client(monkeypatch)

    response = client.get("/api/plans/plan-123/matches", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 404


def test_matches_empty_when_no_strava_token(monkeypatch):
    mock_auth(monkeypatch)
    strava_key(monkeypatch)
    db = fake_db(plan_json(), token=None)
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: db)
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: db)
    client = make_client(monkeypatch)

    response = client.get("/api/plans/plan-123/matches", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {
        "matches": {},
        "start_date": "2026-02-19",
        "strava_connected": False,
    }


def test_matches_empty_when_plan_has_no_race_date(monkeypatch):
    mock_auth(monkeypatch)
    strava_key(monkeypatch)
    db = fake_db(plan_json(race_date=None), token="access")
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: db)
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: db)
    client = make_client(monkeypatch)

    response = client.get("/api/plans/plan-123/matches", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {
        "matches": {},
        "start_date": None,
        "strava_connected": True,
    }


def test_matches_returns_completed_partial_and_missed(monkeypatch):
    mock_auth(monkeypatch)
    strava_key(monkeypatch)
    db = fake_db(plan_json(), token="access")
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: db)
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: db)
    monkeypatch.setattr("app.routers.plans.date", type("FakeDate", (), {"today": staticmethod(lambda: __import__("datetime").date(2026, 2, 25))}))

    class FakeHttpxResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": 101,
                    "name": "Easy ten",
                    "type": "Run",
                    "start_date": "2026-02-19T08:00:00Z",
                    "distance": 9200,
                    "average_speed": 2.5555555556,
                    "moving_time": 3600,
                },
                {
                    "id": 102,
                    "name": "Short tempo",
                    "type": "Run",
                    "start_date": "2026-02-20T08:00:00Z",
                    "distance": 6000,
                    "average_speed": 2.5,
                    "moving_time": 2400,
                },
                {
                    "id": 103,
                    "name": "Bike ignored",
                    "type": "Ride",
                    "start_date": "2026-02-21T08:00:00Z",
                    "distance": 50000,
                    "average_speed": 8,
                    "moving_time": 6000,
                },
            ]

    def fake_get(url, headers, params, timeout):
        assert headers == {"Authorization": "Bearer access"}
        assert params["after"] == 1771459200
        assert params["before"] == 1771977600
        assert params["per_page"] == 100
        assert params["type"] == "Run"
        assert timeout == 15
        return FakeHttpxResponse()

    monkeypatch.setattr("app.services.strava.httpx.get", fake_get)
    client = make_client(monkeypatch)

    response = client.get("/api/plans/plan-123/matches", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    data = response.json()
    assert data["start_date"] == "2026-02-19"
    assert data["strava_connected"] is True
    assert data["matches"]["week_1_day_0"] == {
        "status": "completed",
        "actual_distance_km": 9.2,
        "actual_pace_min_per_km": 6.52,
        "actual_duration_min": 60.0,
        "strava_activity_id": 101,
        "strava_activity_name": "Easy ten",
    }
    assert data["matches"]["week_1_day_1"]["status"] == "partial"
    assert data["matches"]["week_1_day_1"]["actual_distance_km"] == 6.0
    assert data["matches"]["week_1_day_2"] == {
        "status": "missed",
        "actual_distance_km": None,
        "actual_pace_min_per_km": None,
        "actual_duration_min": None,
        "strava_activity_id": None,
        "strava_activity_name": None,
    }
    assert "week_1_day_4" not in data["matches"]
