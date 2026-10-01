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
    assert response.json() == {"status": "ok", "policy_version": "0.4.5"}


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


def test_startup_fails_without_pseudonym_key() -> None:
    from datetime import date

    from app.settings import PseudonymKeyMissingError, Settings

    settings = Settings(_env_file=None, business_date=date(2026, 6, 17))
    with (
        pytest.raises(PseudonymKeyMissingError, match="PSEUDONYM_KEY"),
        TestClient(create_app(settings=settings)),
    ):
        pass


def test_startup_keeps_the_pseudonym_key() -> None:
    from datetime import date

    from pydantic import SecretStr

    from app.settings import Settings

    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        pseudonym_key=SecretStr("k"),
    )
    app = create_app(settings=settings)
    with TestClient(app):
        assert app.state.pseudonym_key == "k"
