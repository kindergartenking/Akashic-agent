from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import settings_api


def _client(tmp_path: Path) -> tuple[TestClient, settings_api.SettingsService]:
    service_router, service = settings_api.create_settings_router(tmp_path)
    application = FastAPI()
    application.include_router(service_router)
    application.middleware("http")(settings_api.settings_security_middleware)
    return TestClient(application), service


def test_model_apply_is_atomic_and_secret_free(monkeypatch, tmp_path: Path) -> None:
    async def no_network(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(settings_api, "_validate_live_candidate", no_network)
    client, service = _client(tmp_path)

    response = client.get("/api/settings/state")
    assert response.status_code == 200
    assert response.json()["mode"] == "needs_setup"

    payload = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "source_id": "deepseek-main",
        "source_name": "DeepSeek 官方",
        "api_key": "sk-test-secret",
        "base_url": "https://api.deepseek.com/v1",
        "expected_config_revision": "",
    }
    response = client.post(
        "/api/settings/apply",
        json=payload,
        headers={"Origin": "http://127.0.0.1:5173", "X-Akasic-CSRF": "1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"

    state = client.get("/api/settings/state").json()
    assert state["mode"] == "ready"
    assert state["runtimes"][0]["credential"]["configured"] is True
    assert "sk-test-secret" not in json.dumps(state)

    with sqlite3.connect(service.registry.path) as connection:
        connection.row_factory = sqlite3.Row
        connection_row = connection.execute(
            "SELECT id, provider, base_url, auth_id, auth_kind, auth_payload FROM model_connections"
        ).fetchone()
        model_row = connection.execute(
            "SELECT id, connection_id, model FROM model_definitions"
        ).fetchone()
        roles = connection.execute(
            "SELECT role, model_id FROM model_role_bindings ORDER BY role"
        ).fetchall()
    assert connection_row[0] == "deepseek-main"
    assert connection_row[1] == "deepseek"
    assert connection_row[2] == "https://api.deepseek.com/v1"
    assert connection_row[3].startswith("model_")
    assert connection_row[4] == "api_key"
    assert json.loads(connection_row[5])["access_token"] == "sk-test-secret"
    assert model_row[1] == connection_row[0]
    assert model_row[2] == "deepseek-chat"
    assert {row[0] for row in roles} == {"agent", "default", "fast", "vision"}

    stale = dict(payload)
    stale["api_key"] = "sk-new-secret"
    stale["expected_config_revision"] = "models:0"
    response = client.post(
        "/api/settings/apply",
        json=stale,
        headers={"Origin": "http://127.0.0.1:5173", "X-Akasic-CSRF": "1"},
    )
    assert response.status_code == 409


def test_settings_mutations_require_origin_and_csrf(tmp_path: Path) -> None:
    client, _service = _client(tmp_path)
    response = client.post("/api/settings/apply", json={"provider": "x", "model": "y"})
    assert response.status_code == 403
