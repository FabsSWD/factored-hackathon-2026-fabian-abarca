"""Chat with the running server over HTTP, as a customer would, and see what the system did.

    python scripts/serve.py                      # in another terminal
    python scripts/manual_chat.py                # asks for the document, logs in with TEST_OTP
    python scripts/manual_chat.py --anon         # without logging in
    python scripts/manual_chat.py --base-url http://127.0.0.1:8080

Each turn prints the reply, the trace_id and the latency seen by this client; with
AGENT_API_TOKEN set it also reads the turn's trace (GET /api/audit/{trace_id}) and prints the
engine's result: outcome, failed gates, triggered rules, authorized actions and executed tools.

Commands:  /handoff  print the handoff packet of this conversation (agent API)
           /new      start a new conversation (same session)
           /quit     exit

Secrets (TEST_OTP, AGENT_API_TOKEN, the session token) are read from .env and never printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.settings import get_settings  # noqa: E402

TIMEOUT_SECONDS = 60.0


class Chat:
    def __init__(
        self, client: httpx.Client, agent_token: str | None, out: Any = sys.stdout
    ) -> None:
        self._client = client
        self._agent = {"Authorization": f"Bearer {agent_token}"} if agent_token else None
        self._customer: dict[str, str] = {}
        self._out = out
        self.conversation_id: str | None = None
        self.handoff_id: str | None = None

    def print(self, text: str = "") -> None:
        print(text, file=self._out)

    def login(self, document: str, otp: str) -> bool:
        started = self._client.post("/auth/login", json={"document_number": document})
        if started.status_code != 202:
            self.print(f"login failed: HTTP {started.status_code}")
            return False
        verified = self._client.post("/auth/verify", json={"document_number": document, "otp": otp})
        if verified.status_code != 200:
            self.print(f"verify failed: HTTP {verified.status_code} {_detail(verified)}")
            return False
        self._customer = {"Authorization": f"Bearer {verified.json()['access_token']}"}
        self.print("logged in")
        return True

    def new(self) -> None:
        self.conversation_id = self.handoff_id = None
        self.print("-- new conversation --")

    def turn(self, message: str) -> None:
        body: dict[str, Any] = {"message": message}
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        started = time.perf_counter()
        response = self._client.post("/api/turn", json=body, headers=self._customer)
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            self.print(f"[HTTP {response.status_code}] {_detail(response)} ({latency_ms:.0f} ms)")
            return
        data = response.json()
        self.conversation_id = data["conversation_id"]
        self.print(f"bot> {data['reply']}")
        closed = "   CLOSED (handed off)" if data["handed_off"] else ""
        self.print(
            f"     turn {data['turn_index']}  lang {data['language']}"
            f"  trace {data['trace_id']}  {latency_ms:.0f} ms{closed}"
        )
        self._engine(data["trace_id"])

    def handoff(self) -> None:
        if self._agent is None:
            self.print("AGENT_API_TOKEN is not set: the agent API is not available")
            return
        if self.handoff_id is None:
            self.print("this conversation has no handoff yet")
            return
        response = self._client.get(f"/api/agent/handoffs/{self.handoff_id}", headers=self._agent)
        if response.status_code != 200:
            self.print(f"[HTTP {response.status_code}] {_detail(response)}")
            return
        self.print(json.dumps(response.json(), indent=2, ensure_ascii=False))

    def _engine(self, trace_id: str) -> None:
        if self._agent is None:
            self.print("     (engine result: set AGENT_API_TOKEN to read the trace)")
            return
        response = self._client.get(f"/api/audit/{trace_id}", headers=self._agent)
        if response.status_code != 200:
            self.print(f"     (trace not available: HTTP {response.status_code})")
            return
        trace = response.json()
        if trace.get("handoff_id"):
            self.handoff_id = trace["handoff_id"]
        for decision in trace.get("decisions", []):
            failed = [g["gate_id"] for g in decision.get("gates_evaluated", []) if not g["passed"]]
            details = {
                "txn": decision.get("transaction_id"),
                "reason": decision.get("reason_code"),
                "tier": decision.get("tier"),
                "clarify": decision.get("clarify_target"),
                "inform": decision.get("inform_reason"),
                "queue": decision.get("queue"),
                "priority": decision.get("priority"),
            }
            shown = "  ".join(f"{k} {v}" for k, v in details.items() if v)
            self.print(f"     engine {decision['outcome']}  {shown}")
            if failed:
                self.print(f"            failed gates {', '.join(failed)}")
            if decision.get("triggered_rules"):
                self.print(f"            rules {', '.join(decision['triggered_rules'])}")
            if decision.get("authorized_actions"):
                self.print(f"            authorized {', '.join(decision['authorized_actions'])}")
        if not trace.get("decisions"):
            self.print(f"     engine not called  outcome {trace.get('outcome')}")
        for tool in trace.get("tool_calls", []):
            record = f" {tool['record_id']}" if tool.get("record_id") else ""
            self.print(f"     tool {tool['action']} {tool['status']}{record}")
        calls = trace.get("model_calls", [])
        if calls:
            models = ", ".join(
                f"{c['provider']}/{c.get('purpose') or '-'} {c['latency_ms']:.0f} ms"
                + ("" if c["success"] else " FAILED")
                for c in calls
            )
            self.print(f"     models {models}")
        server = trace.get("total_latency_ms")
        if server is not None:
            self.print(f"     server {server:.0f} ms  handoff {trace.get('handoff_id') or '-'}")
        if trace.get("error"):
            self.print(f"     error {trace['error']}")

    def run(self, lines: Any) -> None:
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line == "/quit":
                break
            try:
                if line == "/new":
                    self.new()
                elif line == "/handoff":
                    self.handoff()
                else:
                    self.turn(line)
            except httpx.HTTPError as error:
                self.print(f"[server not reachable: {type(error).__name__}]")


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))
    except ValueError:
        return ""


def _prompt() -> Any:
    while True:
        try:
            yield input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--anon", action="store_true", help="chat without logging in")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    with httpx.Client(base_url=args.base_url, timeout=TIMEOUT_SECONDS) as client:
        chat = Chat(client, settings.agent_token())
        if not args.anon:
            document = input("document number: ").strip()
            otp = (
                settings.test_otp.get_secret_value()
                if settings.test_otp
                else getpass.getpass("OTP (TEST_OTP is not set): ")
            )
            try:
                logged_in = chat.login(document, otp)
            except httpx.HTTPError as error:
                sys.exit(f"server not reachable at {args.base_url}: {type(error).__name__}")
            if not logged_in:
                sys.exit(1)
        print("type a message; /handoff, /new, /quit")
        chat.run(_prompt())


if __name__ == "__main__":
    main()
