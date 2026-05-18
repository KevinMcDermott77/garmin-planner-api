from __future__ import annotations

from typing import Any

from tests.test_generate import FakeResponse, FakeSupabaseClient, FakeSupabaseQuery, make_client, mock_valid_jwks_auth


class RecordingSupabaseClient(FakeSupabaseClient):
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        super().__init__(tables=tables)
        self.delete_filters: list[tuple[str, Any]] = []

    def table(self, name: str) -> "RecordingSupabaseQuery":
        return RecordingSupabaseQuery(self, name)


class RecordingSupabaseQuery(FakeSupabaseQuery):
    client: RecordingSupabaseClient

    def execute(self) -> FakeResponse:
        if self.action == "delete":
            self.client.delete_filters = list(self.filters)
        return super().execute()


def test_delete_plan_requires_auth(monkeypatch):
    client = make_client(monkeypatch)

    response = client.delete("/api/plans/plan-123")

    assert response.status_code == 401


def test_delete_plan_scopes_delete_by_id_and_user_id(monkeypatch):
    fake_supabase = RecordingSupabaseClient(
        tables={
            "plans": [
                {
                    "id": "plan-123",
                    "user_id": "user-123",
                    "goal": "sub-4 marathon",
                    "weeks": 16,
                },
                {
                    "id": "plan-456",
                    "user_id": "other-user",
                    "goal": "sub-3 marathon",
                    "weeks": 16,
                },
            ]
        }
    )
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.delete("/api/plans/plan-123", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "plan_id": "plan-123"}
    assert fake_supabase.delete_filters == [("id", "plan-123"), ("user_id", "user-123")]
    assert [row["id"] for row in fake_supabase.tables["plans"]] == ["plan-456"]


def test_delete_plan_not_found_or_not_owned_returns_404(monkeypatch):
    fake_supabase = RecordingSupabaseClient(
        tables={
            "plans": [
                {
                    "id": "plan-456",
                    "user_id": "other-user",
                    "goal": "sub-3 marathon",
                    "weeks": 16,
                }
            ]
        }
    )
    monkeypatch.setattr("app.services.persistence.get_supabase_client", lambda: fake_supabase)
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.delete("/api/plans/plan-456", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Plan not found"
    assert fake_supabase.delete_filters == [("id", "plan-456"), ("user_id", "user-123")]
    assert [row["id"] for row in fake_supabase.tables["plans"]] == ["plan-456"]


def test_delete_route_requires_auth_like_other_plan_routes(monkeypatch):
    client = make_client(monkeypatch)

    list_response = client.get("/api/plans")
    get_response = client.get("/api/plans/plan-123")
    delete_response = client.delete("/api/plans/plan-123")

    assert list_response.status_code == 401
    assert get_response.status_code == 401
    assert delete_response.status_code == 401
