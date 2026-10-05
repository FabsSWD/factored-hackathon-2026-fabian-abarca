"""Docker Compose stack (M19): the resolved configuration, the network boundary and the edge.

`docker compose config` resolves docker-compose.yml with placeholder secrets and an empty env
file, so the real .env is never read. Needs the docker CLI (no daemon). The live stack is checked
by scripts/smoke_stack.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import ROOT

SECRETS = {
    "POSTGRES_PASSWORD": "admin-secret",
    "POSTGRES_OWNER_PASSWORD": "owner-secret",
    "POSTGRES_APP_PASSWORD": "app-secret",
    "DOCUMENT_HASH_KEY": "doc-key",
    "PSEUDONYM_KEY": "pseudo-key",
    "JWT_SECRET": "jwt-secret",
    "TEST_OTP": "123456",
    "OPENAI_API_KEY": "sk-test",
    "LLM_MODEL": "gpt-test",
}
INTERNAL = {"postgres", "kev", "migrate"}


def compose_config(
    tmp_path: Path, *files: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("the docker CLI is required for the Compose tests", pytrace=False)
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if k not in SECRETS}
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME"):
        environment.pop(name, None)
    environment.update(SECRETS if env is None else env)
    selected = [arg for name in files or ("docker-compose.yml",) for arg in ("-f", name)]
    return subprocess.run(
        [docker, "compose", "--env-file", str(env_file), *selected, "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    result = compose_config(tmp_path_factory.mktemp("compose"))
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


def test_config_is_valid_with_the_four_services_and_the_bootstrap(config: dict[str, Any]) -> None:
    assert set(config["services"]) == {"frontend", "api", "kev", "postgres", "migrate"}


def test_only_the_frontend_publishes_a_port(config: dict[str, Any]) -> None:
    published = {name for name, s in config["services"].items() if s.get("ports")}
    assert published == {"frontend"}
    assert config["services"]["frontend"]["ports"][0]["target"] == 80


def test_kev_and_postgres_are_only_on_the_internal_network(config: dict[str, Any]) -> None:
    assert config["networks"]["internal"]["internal"] is True
    assert not config["networks"]["edge"].get("internal")
    services = config["services"]
    for name in INTERNAL:
        assert set(services[name]["networks"]) == {"internal"}, name
    assert set(services["api"]["networks"]) == {"internal", "edge"}
    assert set(services["frontend"]["networks"]) == {"edge"}


def test_every_long_running_service_has_a_healthcheck(config: dict[str, Any]) -> None:
    for name in ("frontend", "api", "kev", "postgres"):
        assert config["services"][name]["healthcheck"]["test"], name
    kev = config["services"]["kev"]["healthcheck"]
    assert "--healthcheck" in kev["test"]  # ready only after the warm-up


def test_startup_order(config: dict[str, Any]) -> None:
    services = config["services"]
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["frontend"]["depends_on"]["api"]["condition"] == "service_healthy"
    # The API answers without Kev (fallback), so it does not wait for it.
    assert "kev" not in services["api"]["depends_on"]
    assert services["migrate"]["restart"] == "no"
    assert services["migrate"]["command"] == ["python", "scripts/bootstrap_db.py"]


def test_the_api_gets_only_the_app_role(config: dict[str, Any]) -> None:
    env = config["services"]["api"]["environment"]
    assert env["DATABASE_URL"] == (
        "postgresql+psycopg://disputes_app:app-secret@postgres:5432/disputes"
    )
    assert "MIGRATION_DATABASE_URL" not in env
    assert "ADMIN_DATABASE_URL" not in env
    assert "POSTGRES_PASSWORD" not in env
    assert env["KEV_BASE_URL"] == "http://kev:8008"
    assert env["FORWARDED_ALLOW_IPS"] == "*"


def test_the_bootstrap_gets_the_admin_and_owner_roles(config: dict[str, Any]) -> None:
    env = config["services"]["migrate"]["environment"]
    assert env["ADMIN_DATABASE_URL"] == "postgresql://disputes:admin-secret@postgres:5432/postgres"
    assert env["MIGRATION_DATABASE_URL"] == (
        "postgresql+psycopg://disputes_owner:owner-secret@postgres:5432/disputes"
    )
    assert env["CORE_LOAD"] == "auto"
    assert env["SEED_SCENARIOS"] == "true"
    core = config["services"]["migrate"]["volumes"][0]
    assert core["target"] == "/srv/data/core" and core["read_only"] is True


def test_empty_optional_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    result = compose_config(
        tmp_path, env={**SECRETS, "RATE_LIMIT_REQUESTS_PER_MINUTE": "", "FRONTEND_PORT": "9090"}
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["api"]["environment"]["RATE_LIMIT_REQUESTS_PER_MINUTE"] == "20"
    assert services["frontend"]["ports"][0]["published"] == "9090"


@pytest.mark.parametrize("missing", sorted(SECRETS))
def test_a_missing_secret_is_refused(tmp_path: Path, missing: str) -> None:
    env = {k: v for k, v in SECRETS.items() if k != missing}
    result = compose_config(tmp_path, env=env)
    assert result.returncode != 0
    assert missing in result.stderr


def test_gpu_override_reserves_a_gpu_for_kev_only(tmp_path: Path) -> None:
    result = compose_config(tmp_path, "docker-compose.yml", "docker-compose.gpu.yml")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    devices = services["kev"]["deploy"]["resources"]["reservations"]["devices"]
    assert devices[0]["capabilities"] == ["gpu"]
    assert all("deploy" not in s for name, s in services.items() if name != "kev")


def test_jev_override_leaves_kev_out_and_points_the_api_at_jev(tmp_path: Path) -> None:
    env = {
        **SECRETS,
        "KEV_BASE_URL": "https://jev.test",
        "KEV_API_KEY": "jev-secret",
        "KEV_MODEL": "jev-latest",
    }
    result = compose_config(tmp_path, "docker-compose.yml", "docker-compose.jev.yml", env=env)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"frontend", "api", "postgres", "migrate"}
    api_env = services["api"]["environment"]
    assert api_env["KEV_BASE_URL"] == "https://jev.test"
    assert api_env["KEV_API_KEY"] == "jev-secret"
    assert api_env["KEV_MODEL"] == "jev-latest"
    assert api_env["KEV_TIMEOUT_SECONDS"] == "5"  # the cloud round trip is slower than Kev's
    assert "edge" in services["api"]["networks"]  # the way out to Jev


@pytest.mark.parametrize("missing", ["KEV_BASE_URL", "KEV_API_KEY"])
def test_jev_override_refuses_a_missing_url_or_key(tmp_path: Path, missing: str) -> None:
    env = {**SECRETS, "KEV_BASE_URL": "https://jev.test", "KEV_API_KEY": "k"}
    del env[missing]
    result = compose_config(tmp_path, "docker-compose.yml", "docker-compose.jev.yml", env=env)
    assert result.returncode != 0
    assert missing in result.stderr


def test_dev_override_publishes_on_loopback_only(tmp_path: Path) -> None:
    result = compose_config(tmp_path, "docker-compose.yml", "docker-compose.dev.yml")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name, port in (("postgres", 5432), ("kev", 8008)):
        published = services[name]["ports"]
        # Both loopbacks: "localhost" resolves to ::1 first, and an unpublished ::1 hangs.
        assert {p["host_ip"] for p in published} == {"127.0.0.1", "::1"}
        assert {p["target"] for p in published} == {port}


# --- The edge (nginx) and the images --------------------------------------------------------

NGINX = (ROOT / "docker" / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")


def location_block(text: str) -> str:
    """Up to the location's closing brace (its own line; ${VAR} also contains braces)."""
    return text.split("\n    }", 1)[0]


def test_nginx_rate_limits_every_api_route() -> None:
    assert "limit_req_zone $binary_remote_addr zone=public_api" in NGINX
    assert "limit_req_status 429;" in NGINX
    for location in ("location /api/ {", "location /auth/ {"):
        block = location_block(NGINX.split(location, 1)[1])
        assert "limit_req zone=public_api" in block, location
        assert "proxy_pass $api;" in block, location


def test_nginx_overwrites_the_forwarded_client_ip() -> None:
    # Replaced, never appended: a forged header cannot dodge the API's per-IP limits.
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in NGINX
    assert "$proxy_add_x_forwarded_for" not in NGINX


def test_nginx_security_headers_are_not_dropped_by_locations() -> None:
    server = NGINX.split("server {", 1)[1]
    assert "Content-Security-Policy" in server
    assert "frame-ancestors 'none'" in server
    # add_header inside a location would drop the server's headers there.
    for block in server.split("\n    location ")[1:]:
        assert "add_header" not in location_block(block)
    assert "try_files $uri /index.html;" in NGINX  # client-side routes


def test_the_api_image_never_gets_data_or_secrets() -> None:
    lines = (ROOT / "docker" / "api" / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    entries = [line.strip() for line in lines.splitlines() if line.strip()]
    entries = [e for e in entries if not e.startswith("#")]
    assert entries[0] == "*"  # allowlist
    allowed = {e[1:] for e in entries if e.startswith("!")}
    assert not any(a.startswith(("data", ".env")) for a in allowed)
    assert "config/eval_scenarios/local/" in entries  # real customers' identifiers


def test_the_api_runs_one_worker_as_an_unprivileged_user() -> None:
    dockerfile = (ROOT / "docker" / "api" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER app" in dockerfile
    assert 'CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8000"]' in (
        dockerfile
    )


def test_kev_image_is_pinned_and_runs_offline() -> None:
    dockerfile = (ROOT / "docker" / "kev" / "Dockerfile").read_text(encoding="utf-8")
    assert "KEV_COMMIT=0fe8fc97c2bcc247fa3efb6e5c32af4e99770e91" in dockerfile
    assert "KEV_REVISION=9a45d25eb2ab761841196625383fa1dff0e56c1e" in dockerfile
    assert "FLA_VERSION=0.5.2" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
