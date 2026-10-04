"""M14: the customer chat's interface texts come from the backend, in Spanish and Portuguese,
and every turn says which state the chat shows."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.turn import turn_status
from app.config import load_policy_config
from app.contracts import Outcome
from app.main import create_app
from app.orchestrator.service import TurnResult
from app.templates.ui_texts import UiTextsError, load_ui_texts, ui_texts


def test_both_languages_have_the_same_texts() -> None:
    catalog = load_ui_texts()
    assert set(catalog["es"].texts) == set(catalog["pt"].texts)
    assert catalog["es"].version == catalog["pt"].version
    assert catalog["es"].texts["send"] and catalog["pt"].texts["retry"] == "Tentar de novo"
    # The chat needs these to render every state of M14.
    needed = {"login_title", "document_label", "otp_label", "verify_code", "input_label", "send",
              "confirm_yes", "confirm_no", "case_created_title", "case_reference_label",
              "handed_off_notice", "handoff_reference_label", "network_error", "retry",
              "session_expired",
              "language_es", "language_pt", "after_login_message"}  # fmt: skip
    assert needed <= set(catalog["es"].texts)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("ui_texts_version: '1.0.0'\ntexts:\n  a: {es: 'Hola'}\n", "exactly"),
        ("ui_texts_version: '1.0.0'\ntexts:\n  a: {es: 'Hola', pt: ''}\n", "empty"),
        ("ui_texts_version: '1.0.0'\ntexts:\n  a: {es: 'Hola {x}', pt: 'Olá'}\n", "placeholders"),
        ("ui_texts_version: 1\ntexts:\n  a: {es: 'Hola', pt: 'Olá'}\n", "version"),
        ("ui_texts_version: '1.0.0'\n", "texts"),
        ("- a\n", "mapping"),
    ],
)
def test_invalid_catalogs_are_rejected(tmp_path: Path, body: str, error: str) -> None:
    path = tmp_path / "ui_texts.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(UiTextsError, match=error):
        load_ui_texts(path)
    with pytest.raises(UiTextsError, match="not readable"):
        load_ui_texts(tmp_path / "missing.yaml")


def test_the_endpoint_serves_each_language_without_a_session() -> None:
    with TestClient(create_app(load_policy_config())) as client:
        es = client.get("/api/ui/texts/es")
        pt = client.get("/api/ui/texts/pt")
        assert es.status_code == 200 and pt.status_code == 200
        assert es.json() == ui_texts()["es"].model_dump()
        assert pt.json()["language"] == "pt" and pt.json()["texts"]["send"] == "Enviar"
        assert client.get("/api/ui/texts/en").status_code == 422  # only es and pt


def result(kind: str, *, closed: bool = False, case: str | None = None) -> TurnResult:
    return TurnResult("C", "T", 0, "texto", kind, Outcome.CLARIFY, None, closed, case)


@pytest.mark.parametrize(
    ("turn", "status"),
    [
        (result("handoff", closed=True), "handed_off"),
        (result("already_transferred", closed=True), "handed_off"),
        (result("case_created", case="DSP-20261003-000001"), "case_created"),
        (result("case_created"), "in_progress"),  # no read-back reference: never a success
        (result("clarify:authentication"), "authentication_required"),
        (result("summary"), "awaiting_confirmation"),
        (result("clarify:confirmation"), "awaiting_confirmation"),
        (result("block_offer"), "awaiting_confirmation"),
        (result("clarify:transaction_ref"), "in_progress"),
        (result("inform:transaction_pending"), "in_progress"),
    ],
)
def test_the_state_of_each_reply(turn: TurnResult, status: str) -> None:
    assert turn_status(turn) == status
