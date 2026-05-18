from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from tests.test_generate import make_client, mock_valid_jwks_auth


class FakeAnthropic:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.messages = FakeMessages()


class FakeMessages:
    def create(self, **kwargs):
        assert kwargs["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=123, output_tokens=45),
            content=[
                SimpleNamespace(
                    type="text",
                    text=(
                        '{"race_name":"Dublin Marathon","found":true,"confidence":"high",'
                        '"race_date":"2026-10-25","location":"Dublin, Ireland",'
                        '"distance":"marathon","course_notes":null,'
                        '"source_note":"Based on web search of official race site"}'
                    ),
                )
            ],
        )


class BrokenAnthropic:
    def __init__(self, api_key: str):
        self.messages = BrokenMessages()


class BrokenMessages:
    def create(self, **_kwargs):
        raise RuntimeError("network unavailable")


def test_race_lookup_requires_auth(monkeypatch):
    client = make_client(monkeypatch)

    response = client.post("/api/race/lookup", json={"race_name": "Dublin Marathon 2026"})

    assert response.status_code == 401


def test_race_lookup_returns_schema(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("app.services.race_lookup.anthropic.Anthropic", FakeAnthropic)
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.post(
        "/api/race/lookup",
        headers={"Authorization": "Bearer valid-token"},
        json={"race_name": "Dublin Marathon 2026"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "race_name": "Dublin Marathon",
        "found": True,
        "confidence": "high",
        "race_date": "2026-10-25",
        "location": "Dublin, Ireland",
        "distance": "marathon",
        "course_notes": None,
        "source_note": "Based on web search of official race site",
    }


def test_race_lookup_degrades_gracefully(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("app.services.race_lookup.anthropic.Anthropic", BrokenAnthropic)
    mock_valid_jwks_auth(monkeypatch)
    client = make_client(monkeypatch)

    response = client.post(
        "/api/race/lookup",
        headers={"Authorization": "Bearer valid-token"},
        json={"race_name": "Nonsense Race"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["found"] is False
    assert data["confidence"] == "low"
    assert data["race_date"] is None
    assert data["source_note"] == "Could not look up this race automatically - please enter details manually"
