from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import POLICY_PATH_ENV, PolicyConfigError, load_policy_config
from app.main import create_app


def test_health_returns_200_with_policy_version() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "policy_version": "0.3.0"}


def test_injected_config_is_used() -> None:
    config = load_policy_config().model_copy(update={"policy_version": "1.2.3"})
    with TestClient(create_app(config)) as client:
        assert client.get("/health").json()["policy_version"] == "1.2.3"


def test_startup_fails_with_invalid_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "policy.yaml"
    broken.write_text("policy_version: '0.1.0'\nparameters: {}\n", encoding="utf-8")
    monkeypatch.setenv(POLICY_PATH_ENV, str(broken))
    with pytest.raises(PolicyConfigError), TestClient(create_app()):
        pass


def test_unknown_route_is_404() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/nope").status_code == 404
