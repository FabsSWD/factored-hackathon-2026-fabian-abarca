"""Smoke test of the Docker Compose stack (M19): the stack is up, only the frontend is published,
and a customer logs in and completes one turn through the public entry point.

    python scripts/smoke_stack.py --up        # docker compose up -d --wait, then check
    python scripts/smoke_stack.py             # check a stack that is already up
    python scripts/smoke_stack.py --base-url http://server:8080 --no-docker   # remote, HTTP only

Checks, in order (any failure exits with 1):
1. `docker compose config` is valid.
2. Every long-running service is running and healthy; `migrate` exited with 0.
3. No service but `frontend` publishes a port on the host (PostgreSQL, Kev and the API are
   internal).
4. Through the frontend: `GET /health`, the app's index page, `POST /auth/login` and
   `POST /auth/verify` with SMOKE_DOCUMENT (default SEED-0001, a seeded scenario customer) and
   TEST_OTP, then one `POST /api/turn` with the session.
5. With AGENT_API_TOKEN: the turn's trace shows that Kev answered (signals from `kev`, not the
   fallback). `--allow-kev-fallback` turns this into a warning.

TEST_OTP and AGENT_API_TOKEN come from the environment or .env and are never printed. Real
OpenAI and Kev calls are made: one turn.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_SERVICE = "frontend"
ONE_SHOT = {"migrate"}
EXPECTED_SERVICES = {"frontend", "api", "kev", "postgres", "migrate"}
TURN_MESSAGE = "Hola, no reconozco un cargo en mi tarjeta y quiero disputarlo."
TURN_STATUSES = {
    "in_progress",
    "awaiting_confirmation",
    "authentication_required",
    "case_created",
    "handed_off",
}


class SmokeError(RuntimeError):
    pass


def setting(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(ROOT / ".env").get(name) or ""
    return value or default


def ok(message: str) -> None:
    print(f"[ok] {message}", flush=True)


def compose(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise SmokeError(f"docker compose {' '.join(args)} failed ({result.returncode}) {detail}")
    return result.stdout or ""


def compose_services() -> list[dict[str, Any]]:
    """`docker compose ps` as a list (one JSON object per line, or a JSON array)."""
    out = compose("ps", "--all", "--format", "json").strip()
    if not out:
        return []
    if out.startswith("["):
        return list(json.loads(out))
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def check_services(services: list[dict[str, Any]]) -> None:
    found = {s["Service"] for s in services}
    missing = EXPECTED_SERVICES - found
    if missing:
        raise SmokeError(f"services not created: {', '.join(sorted(missing))}")
    for service in services:
        name, state = service["Service"], service.get("State", "")
        if name in ONE_SHOT:
            if state != "exited" or int(service.get("ExitCode", 1)) != 0:
                raise SmokeError(f"{name} did not finish cleanly (state {state})")
            continue
        health = service.get("Health", "")
        if state != "running" or health != "healthy":
            raise SmokeError(f"{name} is {state} ({health or 'no healthcheck'})")
    ok(f"services healthy: {', '.join(sorted(found - ONE_SHOT))}; migrate exited 0")


def published_ports(services: list[dict[str, Any]]) -> dict[str, list[int]]:
    ports: dict[str, list[int]] = {}
    for service in services:
        published = [
            int(p["PublishedPort"])
            for p in service.get("Publishers") or []
            if int(p.get("PublishedPort") or 0) > 0
        ]
        if published:
            ports.setdefault(service["Service"], []).extend(published)
    return ports


def check_ports(services: list[dict[str, Any]]) -> None:
    ports = published_ports(services)
    exposed = {name: p for name, p in ports.items() if name != PUBLIC_SERVICE}
    if exposed:
        raise SmokeError(f"internal services publish ports on the host: {exposed}")
    if not ports.get(PUBLIC_SERVICE):
        raise SmokeError("the frontend publishes no port")
    ok(f"only the frontend is published (ports {sorted(set(ports[PUBLIC_SERVICE]))})")


def frontend_url(services: list[dict[str, Any]]) -> str:
    port = sorted(set(published_ports(services)[PUBLIC_SERVICE]))[0]
    return f"http://127.0.0.1:{port}"


def expect(response: httpx.Response, code: int, what: str) -> Any:
    if response.status_code != code:
        raise SmokeError(f"{what}: HTTP {response.status_code} {response.text[:200]}")
    return response.json() if response.content else None


def check_http(base_url: str, *, allow_kev_fallback: bool, timeout: float) -> None:
    otp = setting("TEST_OTP")
    if not otp:
        raise SmokeError("TEST_OTP is not set (environment or .env)")
    document = setting("SMOKE_DOCUMENT", "SEED-0001")
    with httpx.Client(base_url=base_url, timeout=timeout) as http:
        health = expect(http.get("/health"), 200, "GET /health")
        ok(f"GET /health: {health['status']} (policy {health['policy_version']})")

        index = http.get("/agent")  # a client-side route: served by index.html
        if index.status_code != 200 or '<div id="root">' not in index.text:
            raise SmokeError(f"GET /agent did not serve the app (HTTP {index.status_code})")
        ok("the app is served, client-side routes included")

        expect(http.post("/auth/login", json={"document_number": document}), 202, "login")
        issued = expect(
            http.post("/auth/verify", json={"document_number": document, "otp": otp}),
            200,
            f"verify ({document})",
        )
        token = issued["access_token"]
        ok(f"logged in as {document}")

        turn = expect(
            http.post(
                "/api/turn",
                json={"message": TURN_MESSAGE},
                headers={"Authorization": f"Bearer {token}"},
            ),
            200,
            "POST /api/turn",
        )
        if not turn["reply"].strip() or turn["status"] not in TURN_STATUSES:
            raise SmokeError(f"unexpected turn response: status={turn['status']}")
        ok(f"turn answered: status={turn['status']}, language={turn['language']}")
        print(f"     reply: {turn['reply'][:160]}")

        agent_token = setting("AGENT_API_TOKEN")
        if not agent_token:
            print("[warn] AGENT_API_TOKEN not set: Kev's answer is not checked")
            return
        trace = expect(
            http.get(
                f"/api/audit/{turn['trace_id']}",
                headers={"Authorization": f"Bearer {agent_token}"},
            ),
            200,
            "GET /api/audit/{trace_id}",
        )
        check_kev(trace, allow_fallback=allow_kev_fallback)


def check_kev(trace: dict[str, Any], *, allow_fallback: bool) -> None:
    source = (trace.get("signals") or {}).get("source")
    kev_calls = [c for c in trace.get("model_calls", []) if c.get("provider") == "kev"]
    answered = [c for c in kev_calls if c.get("success")]
    if source == "kev" and answered:
        call = answered[0]
        info = call.get("model_info") or {}
        ok(
            f"Kev answered in {call['latency_ms']:.0f} ms "
            f"(server {call.get('server_latency_ms') or 0:.0f} ms, "
            f"run {info.get('run', call.get('response_model') or call['model'])}, "
            f"device {info.get('device', '?')}); outcome {trace.get('outcome')}"
        )
        return
    errors = "; ".join(str(c.get("error")) for c in kev_calls if not c.get("success"))
    message = f"signals came from {source!r}, not Kev ({errors or 'no Kev call'})"
    if allow_fallback:
        print(f"[warn] {message}")
        return
    raise SmokeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--up", action="store_true", help="docker compose up -d --wait")
    parser.add_argument("--no-docker", action="store_true", help="HTTP checks only")
    parser.add_argument("--base-url", help="public URL (default: the frontend's published port)")
    parser.add_argument("--allow-kev-fallback", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0, help="per HTTP request, seconds")
    args = parser.parse_args()
    try:
        base_url = args.base_url
        if not args.no_docker:
            compose("config", "--quiet")
            ok("docker compose config is valid")
            if args.up:
                print("[..] docker compose up -d --wait", flush=True)
                compose("up", "-d", "--wait", capture=False)
            services = compose_services()
            check_services(services)
            check_ports(services)
            base_url = base_url or frontend_url(services)
        if not base_url:
            raise SmokeError("--base-url is required with --no-docker")
        check_http(base_url, allow_kev_fallback=args.allow_kev_fallback, timeout=args.timeout)
    except (SmokeError, httpx.HTTPError) as exc:
        sys.exit(f"[fail] {exc}")
    print("[ok] smoke test passed")


if __name__ == "__main__":
    main()
