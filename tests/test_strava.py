from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app


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
        self.action: str | None = None
        self.payload: dict[str, Any] | None = None
        self.filters: list[tuple[str, Any]] = []
        self.limit_count: int | None = None

    def select(self, *_args: str) -> "FakeSupabaseQuery":
        self.action = "select"
        return self

    def upsert(self, payload: dict[str, Any], **_kwargs: Any) -> "FakeSupabaseQuery":
        self.action = "upsert"
        self.payload = payload
        return self

    def eq(self, key: str, value: Any) -> "FakeSupabaseQuery":
        self.filters.append((key, value))
        return self

    def limit(self, count: int) -> "FakeSupabaseQuery":
        self.limit_count = count
        return self

    def execute(self) -> FakeResponse:
        if self.action == "upsert":
            assert self.payload is not None
            rows = self.client.tables.setdefault(self.table_name, [])
            existing = next((row for row in rows if row.get("user_id") == self.payload["user_id"]), None)
            if existing:
                existing.update(self.payload)
                return FakeResponse([existing])
            rows.append(dict(self.payload))
            return FakeResponse([self.payload])

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


def mock_auth(monkeypatch):
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
        return {"sub": "00000000-0000-0000-0000-000000000001", "email": "runner@example.com"}

    monkeypatch.setattr("app.auth.jwt.decode", fake_decode)


def make_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    return TestClient(app)


def set_strava_env(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("STRAVA_CLIENT_ID", "client-123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "secret-123")
    monkeypatch.setenv("STRAVA_REDIRECT_URI", "http://localhost:8000/api/strava/callback")
    monkeypatch.setenv("STRAVA_TOKEN_ENCRYPTION_KEY", key)
    return key


def test_connect_returns_auth_url(monkeypatch):
    set_strava_env(monkeypatch)
    mock_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.get("/api/strava/connect", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    auth_url = response.json()["auth_url"]
    assert "https://www.strava.com/oauth/authorize" in auth_url
    assert "client_id=client-123" in auth_url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fapi%2Fstrava%2Fcallback" in auth_url
    assert "scope=activity%3Aread_all" in auth_url


def test_callback_exchanges_code_saves_tokens_and_redirects(monkeypatch):
    calls: dict[str, Any] = {}

    def fake_exchange_code(code: str, state: str) -> dict[str, Any]:
        calls["exchange"] = {"code": code, "state": state}
        return {"access_token": "access", "refresh_token": "refresh", "expires_at": 1}

    def fake_save_tokens(user_id: str, token_response: dict[str, Any]) -> None:
        calls["save"] = {"user_id": user_id, "token_response": token_response}

    monkeypatch.setattr("app.routers.strava.strava.exchange_code", fake_exchange_code)
    monkeypatch.setattr("app.routers.strava.strava.save_tokens", fake_save_tokens)
    client = make_client(monkeypatch)

    response = client.get(
        "/api/strava/callback?code=code-123&state=user-123&scope=activity:read_all",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "http://localhost:3000/strava?connected=true"
    assert calls == {
        "exchange": {"code": "code-123", "state": "user-123"},
        "save": {
            "user_id": "user-123",
            "token_response": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 1,
                "scope": "activity:read_all",
            },
        },
    }


def test_activities_returns_disconnected_when_no_token(monkeypatch):
    mock_auth(monkeypatch)
    monkeypatch.setattr("app.routers.strava.strava.get_valid_token", lambda user_id: None)
    client = make_client(monkeypatch)

    response = client.get("/api/strava/activities", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {"activities": [], "connected": False}


def test_activities_returns_activity_list_when_token_valid(monkeypatch):
    activities = [
        {
            "id": 123,
            "name": "Morning Run",
            "start_date": "2026-05-20T07:00:00Z",
            "distance": 5.0,
            "moving_time": "25:00",
            "elapsed_time": "26:10",
            "average_speed": "5:00",
            "total_elevation_gain": 42,
            "type": "Run",
        }
    ]
    mock_auth(monkeypatch)
    monkeypatch.setattr("app.routers.strava.strava.get_valid_token", lambda user_id: "access-token")
    monkeypatch.setattr("app.routers.strava.strava.get_recent_activities", lambda user_id: activities)
    client = make_client(monkeypatch)

    response = client.get("/api/strava/activities", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {"activities": activities, "connected": True}


def test_token_refresh_is_called_when_expired(monkeypatch):
    set_strava_env(monkeypatch)
    from app.services import strava

    user_id = "00000000-0000-0000-0000-000000000001"
    expired_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    fake_supabase = FakeSupabaseClient(
        tables={
            "strava_tokens": [
                {
                    "user_id": user_id,
                    "access_token": strava._encrypt("old-access"),
                    "refresh_token": strava._encrypt("old-refresh"),
                    "expires_at": expired_at,
                    "athlete_id": 99,
                    "scope": "activity:read_all",
                }
            ]
        }
    )
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: fake_supabase)
    calls: dict[str, Any] = {}

    def fake_refresh(refresh_token_encrypted: str) -> dict[str, Any]:
        calls["refresh_token"] = strava._decrypt(refresh_token_encrypted)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
            "athlete": {"id": 99},
            "scope": "activity:read_all",
        }

    monkeypatch.setattr("app.services.strava.refresh_access_token", fake_refresh)

    access_token = strava.get_valid_token(user_id)

    assert access_token == "new-access"
    assert calls == {"refresh_token": "old-refresh"}
    saved_row = fake_supabase.tables["strava_tokens"][0]
    assert strava._decrypt(saved_row["access_token"]) == "new-access"
    assert strava._decrypt(saved_row["refresh_token"]) == "new-refresh"


def test_auth_requirements(monkeypatch):
    client = make_client(monkeypatch)

    connect_response = client.get("/api/strava/connect")
    activities_response = client.get("/api/strava/activities")
    status_response = client.get("/api/strava/status")
    callback_response = client.get(
        "/api/strava/callback?code=code-123&state=user-123",
        follow_redirects=False,
    )

    assert connect_response.status_code == 401
    assert activities_response.status_code == 401
    assert status_response.status_code == 401
    assert callback_response.status_code == 307


def test_fitness_summary_returns_disconnected_when_no_token(monkeypatch):
    mock_auth(monkeypatch)
    monkeypatch.setattr("app.routers.strava.strava.get_token_row", lambda user_id: None)
    client = make_client(monkeypatch)

    response = client.get("/api/strava/fitness-summary", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {"connected": False, "summary": None}


def test_fitness_summary_returns_computed_summary(monkeypatch):
    mock_auth(monkeypatch)
    set_strava_env(monkeypatch)
    from app.services import strava

    user_id = "00000000-0000-0000-0000-000000000001"
    fake_supabase = FakeSupabaseClient(
        tables={
            "strava_tokens": [
                {
                    "user_id": user_id,
                    "access_token": strava._encrypt("valid-access"),
                    "refresh_token": strava._encrypt("refresh"),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "athlete_id": 99,
                    "scope": "activity:read_all",
                }
            ]
        }
    )
    monkeypatch.setattr("app.services.strava.get_supabase_client", lambda: fake_supabase)

    class FakeHttpxResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "type": "Run",
                    "start_date": "2026-03-01T08:00:00Z",
                    "distance": 5000,
                    "average_speed": 2.7777777778,
                },
                {
                    "id": 2,
                    "type": "Run",
                    "start_date": "2026-03-03T08:00:00Z",
                    "distance": 8000,
                    "average_speed": 2.5,
                },
                {
                    "id": 3,
                    "type": "Run",
                    "start_date": "2026-03-10T08:00:00Z",
                    "distance": 16000,
                    "average_speed": 2.2222222222,
                },
                {
                    "id": 4,
                    "type": "Ride",
                    "start_date": "2026-03-11T08:00:00Z",
                    "distance": 30000,
                    "average_speed": 8,
                },
            ]

    def fake_get(url, headers, params, timeout):
        assert url == strava.STRAVA_ACTIVITIES_URL
        assert headers == {"Authorization": "Bearer valid-access"}
        assert params["per_page"] == 100
        assert params["type"] == "Run"
        assert isinstance(params["after"], int)
        assert timeout == 15
        return FakeHttpxResponse()

    monkeypatch.setattr("app.services.strava.httpx.get", fake_get)
    client = make_client(monkeypatch)

    response = client.get("/api/strava/fitness-summary", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "summary": {
            "weekly_km_recent": 2.2,
            "longest_run_km_recent": 16.0,
            "easy_pace_min_per_km": 6.33,
            "days_per_week": 3,
            "run_count": 3,
            "date_range": {"from": "2026-03-01", "to": "2026-03-10"},
            "data_source": "strava_90_days",
        },
    }


def test_fitness_summary_returns_note_when_strava_api_fails(monkeypatch):
    from app.services.strava import StravaFetchError

    mock_auth(monkeypatch)
    monkeypatch.setattr("app.routers.strava.strava.get_token_row", lambda user_id: {"user_id": user_id})
    monkeypatch.setattr(
        "app.routers.strava.strava.compute_fitness_summary",
        lambda user_id: (_ for _ in ()).throw(StravaFetchError()),
    )
    client = make_client(monkeypatch)

    response = client.get("/api/strava/fitness-summary", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "summary": None,
        "note": "Could not fetch Strava data",
    }


def test_fitness_summary_requires_auth(monkeypatch):
    client = make_client(monkeypatch)

    response = client.get("/api/strava/fitness-summary")

    assert response.status_code == 401
