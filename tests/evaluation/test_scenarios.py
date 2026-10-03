"""M17: specifications, reference labels, the mix of seeded and real cases, the split and the
review sample."""

from __future__ import annotations

import csv
import importlib.util
import io
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from pydantic import ValidationError

from app.config import load_policy_config
from app.contracts import Tier
from app.evaluation.labeler import CaseLabel, label_all, label_case, tier_of
from app.evaluation.real import CATEGORY, LockedCase, SelectionLock, load_lock
from app.evaluation.scenarios import DEFAULT_SCENARIOS_DIR, Scenario, load_scenarios
from app.evaluation.split import Split, make_split

ROOT = Path(__file__).resolve().parent.parent.parent
SPLIT_FINGERPRINT = "87e49ba9c5af29eeb1f8a8e8dba1df2991493e8c7124b97094f75a63af96cdd4"
"""sha256 of the split fixed before any tuning (policy §16.4). A change here is a new split."""


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return load_scenarios()


@pytest.fixture(scope="module")
def labels(scenarios: list[Scenario]) -> list[CaseLabel]:
    return label_all(scenarios)


@pytest.fixture(scope="module")
def locked() -> SelectionLock:
    found = load_lock()
    assert found is not None, "config/eval_scenarios/real_selection.lock is versioned"
    return found


@pytest.fixture(scope="module")
def real(locked: SelectionLock) -> list[LockedCase]:
    return locked.cases


@pytest.fixture(scope="module")
def real_labels(real: list[LockedCase]) -> list[CaseLabel]:
    return [c.expected for c in real]


@pytest.fixture(scope="module")
def cases(scenarios: list[Scenario], real: list[LockedCase]) -> list[Scenario | LockedCase]:
    return [*scenarios, *real]


@pytest.fixture(scope="module")
def all_labels(labels: list[CaseLabel], real_labels: list[CaseLabel]) -> list[CaseLabel]:
    return labels + real_labels


@pytest.fixture(scope="module")
def stored_split() -> Split:
    raw = yaml.safe_load((DEFAULT_SCENARIOS_DIR / "split.yaml").read_text(encoding="utf-8"))
    return Split(seed=raw["seed"], calibration=raw["calibration"], evaluation=raw["evaluation"])


# --- Specifications ----------------------------------------------------------------------------


def test_the_generated_files_are_up_to_date() -> None:
    generator = _script("generate_scenarios")
    for name, content in generator.render(generator.build_all()).items():
        assert (DEFAULT_SCENARIOS_DIR / name).read_text(encoding="utf-8") == content, name


def test_every_identifier_is_a_seeded_one(scenarios: list[Scenario]) -> None:
    for s in scenarios:
        ids = [s.customer_id, s.document_number, *(s.product_id(p.key) for p in s.products)]
        ids += [s.transaction_id(t.key) for t in s.transactions]
        ids += [s.case_id(i) for i in range(1, len(s.prior_cases) + 1)]
        assert all(identifier.startswith("SEED-") for identifier in ids), s.id


def test_specifications_are_validated() -> None:
    base = yaml.safe_load(
        (DEFAULT_SCENARIOS_DIR / "cases_resolution.yaml").read_text(encoding="utf-8")
    )
    case = base["cases"][0]
    for broken in (
        {**case, "transactions": [{**case["transactions"][0], "product": "nope"}]},
        {**case, "disputes": [{"transaction": "nope"}]},
        {**case, "disputes": []},
        {**case, "products": [{**case["products"][0], "currency": "EUR"}]},
        {**case, "transactions": [{**case["transactions"][0], "at": "03:00:00"}]},
        {**case, "id": "X1"},
        {**case, "invented": True},
        {**case, "products": [{**case["products"][0], "type": "Cohete"}]},
        {**case, "transactions": [{**case["transactions"][0], "type": "Gift"}]},
        {**case, "transactions": [{**case["transactions"][0], "currency": "EUR"}]},
        {**case, "transactions": case["transactions"] * 2},
        {**case, "prior_cases": [{"transaction": "nope", "reason_code": "RC_FEE"}]},
    ):
        with pytest.raises(ValidationError):
            Scenario.model_validate(broken)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    text = (DEFAULT_SCENARIOS_DIR / "cases_resolution.yaml").read_text(encoding="utf-8")
    (tmp_path / "cases_a.yaml").write_text(text, encoding="utf-8")
    (tmp_path / "cases_b.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate scenario ids"):
        load_scenarios(tmp_path)


# --- Reference labels --------------------------------------------------------------------------


def test_labels_are_reproducible_and_stored(
    scenarios: list[Scenario], labels: list[CaseLabel]
) -> None:
    assert label_all(scenarios) == labels  # the engine on the specification: deterministic
    stored = yaml.safe_load((DEFAULT_SCENARIOS_DIR / "labels.yaml").read_text(encoding="utf-8"))
    assert [CaseLabel.model_validate(raw) for raw in stored["labels"]] == labels


def test_a_label_does_not_depend_on_the_script(scenarios: list[Scenario]) -> None:
    case = scenarios[0]
    rewritten = case.model_copy(
        update={"script": case.script.model_copy(update={"first": "otra cosa"})}
    )
    assert label_case(rewritten) == label_case(case)


def test_every_gate_and_trigger_is_covered(labels: list[CaseLabel]) -> None:
    rules = {rule for label in labels for rule in label.triggered_rules}
    gates = {gate for label in labels for d in label.disputes for gate in d.failed_gates}
    assert rules == {f"ESC-{n:02d}" for n in range(1, 15)}
    assert gates == {f"GATE-{n:02d}" for n in range(1, 12)}


def test_no_case_ends_in_clarify(labels: list[CaseLabel]) -> None:
    # Policy §9: a conversation ends in ESCALATE (ESC-09) or another outcome, never in CLARIFY.
    assert all(label.outcome.value != "CLARIFY" for label in labels)


def test_the_path_matches_the_label(labels: list[CaseLabel]) -> None:
    unsupported = {"ESC-09", "ESC-12", "ESC-14"}
    for label in labels:
        if label.path.value == "resolution":
            assert label.outcome.value == "RESOLVE", label.case_id
        elif label.path.value == "human":
            assert (
                label.outcome.value == "ESCALATE" and not set(label.triggered_rules) & unsupported
            )
        else:
            assert (
                label.outcome.value in ("INFORM", "REFUSE")
                or set(label.triggered_rules) <= unsupported
            )


def test_actions_follow_the_outcome(labels: list[CaseLabel]) -> None:
    for label in labels:
        for d in label.disputes:
            assert ("ACT-02" in d.actions) is (d.outcome.value == "RESOLVE")
            assert ("ACT-05" in d.actions) is (d.outcome.value == "ESCALATE")
            assert d.queue is not None if d.outcome.value == "ESCALATE" else d.queue is None


def test_the_escalation_routes_follow_the_policy(labels: list[CaseLabel]) -> None:
    by_rule = {
        "ESC-03": ("fraud", "high"),
        "ESC-04": ("fraud", "normal"),
        "ESC-06": ("disputes", "high"),
        "ESC-13": ("security_review", "normal"),
    }
    for label in labels:
        last = label.disputes[-1]
        for rule, route in by_rule.items():
            if label.triggered_rules == [rule]:
                assert (last.queue, last.priority) == route, label.case_id


# --- The mix -----------------------------------------------------------------------------------


def test_the_mix(scenarios: list[Scenario], labels: list[CaseLabel]) -> None:
    languages = Counter(s.language for s in scenarios)
    paths = Counter(s.path.value for s in scenarios)
    assert 85 <= len(scenarios) <= 100
    assert languages["pt"] >= 30
    assert min(paths.values()) >= 25 and set(paths) == {
        "resolution",
        "ambiguous_or_unsupported",
        "human",
    }


def test_flows_without_real_data_are_included(
    scenarios: list[Scenario], labels: list[CaseLabel]
) -> None:
    by_id = {label.case_id: label for label in labels}
    duplicates = [
        s
        for s in scenarios
        if any(d.reason_code and d.reason_code.value == "RC_DUPLICATE" for d in s.disputes)
    ]
    assert any(by_id[s.id].outcome.value == "RESOLVE" for s in duplicates)
    assert any(d.tier == "T3" for label in labels for d in label.disputes)
    assert any((t.fraud_score or 0) > 35 for s in scenarios for t in s.transactions)


def test_the_special_conditions_appear(scenarios: list[Scenario]) -> None:
    conditions = [s.conditions for s in scenarios]
    assert any(c.injection_strikes >= 2 for c in conditions)
    assert any(c.human_requested for c in conditions)
    assert any(c.legal_or_vulnerability for c in conditions)
    assert any(c.expired_session for c in conditions)
    assert any(c.hedged_confirmation for c in conditions)
    assert any(s.script.side_questions for s in scenarios)


def test_customers_are_spread_over_segments(scenarios: list[Scenario]) -> None:
    assert len({s.customer.country for s in scenarios}) == 3
    assert len({s.customer.age_band for s in scenarios}) == 6
    assert len({s.customer.gender for s in scenarios}) == 3


COUNTRY_CURRENCY = {"Colombia": "COP", "Argentina": "ARS", "México": "USD"}
CONTRACTIBLE = re.compile(r"\b(em|de) (o|a|os|as)\b", re.IGNORECASE)


def _texts(s: Scenario) -> list[str]:
    return [s.script.first, *s.script.answers.values(), *s.script.side_questions]


def test_currencies_follow_the_country(scenarios: list[Scenario]) -> None:
    for s in scenarios:
        currency = COUNTRY_CURRENCY[s.customer.country]
        used = {t.currency for t in s.transactions} | {p.currency for p in s.products}
        assert used == {currency}, s.id
        units = ("dólares", "USD") if currency == "USD" else ("pesos",)
        others = ("pesos",) if currency == "USD" else ("dólares", "USD")
        assert not any(o in text for o in others for text in _texts(s)), s.id
        amount = re.compile(r"\d+(,\d+)? (dólares|pesos)|USD \d")
        with_amounts = [text for text in _texts(s) if amount.search(text)]
        assert all(any(u in text for u in units) for text in with_amounts), s.id


def test_portuguese_scripts_use_contractions(scenarios: list[Scenario]) -> None:
    for s in scenarios:
        if s.language == "pt":
            found = [text for text in _texts(s) if CONTRACTIBLE.search(text)]
            assert found == [], (s.id, found)
    assert CONTRACTIBLE.search("uma cobrança em o cinema")  # the check itself works
    assert not CONTRACTIBLE.search("dia 3 de abril, no cinema")


REPEATED = re.compile(r"\b(\w+(?:\s+\w+){0,2})\s+\1\b", re.IGNORECASE)


def test_scripts_never_repeat_words_in_a_row(scenarios: list[Scenario]) -> None:
    for s in scenarios:
        found = [text for text in _texts(s) if REPEATED.search(text)]
        assert found == [], (s.id, found)
    assert REPEATED.search("tem uma cobrança uma cobrança de 50 dólares")  # the check works
    assert REPEATED.search("no no reconozco")
    assert not REPEATED.search("uma cobrança de 50 dólares, dia 5 de junho")


def test_a_withdrawal_points_to_the_confirmation_rule() -> None:
    labeler = _script("label_scenarios")
    assert labeler._no_rule_reference("dispute_withdrawn").startswith("§8 COM-03")
    assert labeler._no_rule_reference(None) == "§9 RESOLVE"


def test_a_t3_purchase_has_a_credible_merchant(
    scenarios: list[Scenario], labels: list[CaseLabel]
) -> None:
    by_id = {s.id: s for s in scenarios}
    for label in labels:
        if "ESC-01" in label.triggered_rules:
            disputed = label.disputes[-1].transaction
            assert disputed is not None
            assert by_id[label.case_id].transaction(disputed).merchant == "Electro Mundo"


# --- Cases on real records ---------------------------------------------------------------------


def test_the_real_mix(
    locked: SelectionLock, real: list[LockedCase], cases: list[Scenario | LockedCase]
) -> None:
    assert 20 <= len(real) <= 25
    assert 3 * sum(1 for c in real if c.language == "pt") >= len(real)  # at least a third
    assert {c.category for c in real} == set(CATEGORY) == set(locked.counts)
    for category, by_language in locked.counts.items():
        for language, count in by_language.items():
            assert sum(1 for c in real if (c.category, c.language) == (category, language)) == count
    assert {c.data_source for c in cases} == {"real", "seeded"}
    assert not {c.id for c in real} & {c.id for c in cases if isinstance(c, Scenario)}
    assert (locked.seed, str(locked.business_date)) == ("20261004", "2026-06-17")


def test_real_labels_give_what_their_category_intends(real: list[LockedCase]) -> None:
    for c in real:
        category, label = CATEGORY[c.category], c.expected
        last = label.disputes[-1]
        assert label.case_id == c.id and label.language == c.language
        assert (label.outcome, tuple(label.triggered_rules)) == (category.outcome, category.rules)
        assert label.path == c.path == category.path
        assert category.tier is None or last.tier == category.tier
        assert last.inform_reason == category.inform_reason
        assert all(d.transaction is None for d in label.disputes)  # the lock names no record


def test_real_identifiers_stay_local() -> None:
    assert not list(DEFAULT_SCENARIOS_DIR.glob("real_*.yaml"))  # only the lock is versioned
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config/eval_scenarios/local/" in ignored


# --- The split ---------------------------------------------------------------------------------


def test_the_split_is_stable_and_pinned(
    cases: list[Scenario | LockedCase], all_labels: list[CaseLabel], stored_split: Split
) -> None:
    assert make_split(cases, all_labels) == stored_split  # same seed, same split
    assert make_split(cases, all_labels) == make_split(cases, all_labels)
    assert stored_split.fingerprint == SPLIT_FINGERPRINT


def test_the_split_does_not_overlap_and_covers_every_case(
    cases: list[Scenario | LockedCase], stored_split: Split
) -> None:
    calibration, evaluation = set(stored_split.calibration), set(stored_split.evaluation)
    assert not calibration & evaluation
    assert calibration | evaluation == {c.id for c in cases}
    assert 30 <= len(calibration) <= 45 and 70 <= len(evaluation) <= 90


def test_the_evaluation_split_has_both_sources_portuguese_and_every_rule(
    cases: list[Scenario | LockedCase], all_labels: list[CaseLabel], stored_split: Split
) -> None:
    evaluation = set(stored_split.evaluation)
    pt = {c.id for c in cases if c.language == "pt"}
    assert len(pt & evaluation) >= 20
    by_source = Counter(c.data_source for c in cases if c.id in evaluation)
    assert by_source["real"] >= 10 and by_source["seeded"] >= 50
    evaluated = [label for label in all_labels if label.case_id in evaluation]
    rules = {r for label in evaluated for r in label.triggered_rules}
    gates = {g for label in evaluated for d in label.disputes for g in d.failed_gates}
    assert len(rules) == 14 and len(gates) == 11


def test_another_seed_gives_another_split(
    cases: list[Scenario | LockedCase], all_labels: list[CaseLabel]
) -> None:
    assert make_split(cases, all_labels, seed=1) != make_split(cases, all_labels)


# --- The review sample -------------------------------------------------------------------------


def test_the_review_sample(cases: list[Scenario | LockedCase], all_labels: list[CaseLabel]) -> None:
    labeler = _script("label_scenarios")
    size = labeler.review_size(cases)
    chosen = labeler.review_sample(cases, all_labels, size)
    assert chosen == labeler.review_sample(cases, all_labels, size)  # fixed seed
    assert len(chosen) >= max(50, len(cases) // 10)
    must = {
        label.case_id
        for label in all_labels
        if set(label.triggered_rules) & {"ESC-03", "ESC-06", "ESC-13"}
    }
    assert must <= set(chosen)
    picked = [c for c in cases if c.id in chosen]
    real = [c for c in picked if isinstance(c, LockedCase)]
    assert len(real) >= 15 and {c.category for c in real} == set(CATEGORY)
    assert {c.language for c in picked} >= {"es", "pt"}
    assert {c.path.value for c in picked} == {"resolution", "ambiguous_or_unsupported", "human"}


def test_the_review_rows(
    cases: list[Scenario | LockedCase], all_labels: list[CaseLabel], stored_split: Split
) -> None:
    labeler = _script("label_scenarios")
    chosen = labeler.review_sample(cases, all_labels, labeler.review_size(cases))
    real_rows = {c.id: ("records read at run time", "reason_code=RC_UNRECOGNIZED", "first")
                 for c in cases if isinstance(c, LockedCase) and c.id in chosen}  # fmt: skip
    rows = list(csv.DictReader(io.StringIO(
        labeler.review_csv(cases, all_labels, stored_split, chosen, real_rows))))  # fmt: skip
    assert [row["case_id"] for row in rows] == chosen
    assert all(row["reviewer_verdict"] == "" and row["expected_outcome"] for row in rows)
    real = [row for row in rows if row["data_source"] == "real"]
    assert real and all(row["records"] == "records read at run time" for row in real)
    assert "document" not in rows[0]


def test_the_labeling_outputs_are_up_to_date() -> None:
    labeler = _script("label_scenarios")
    for path, content in labeler.build().items():
        assert path.read_text(encoding="utf-8") == content, path


def test_tiers_of_earlier_cases() -> None:
    config = load_policy_config()
    assert tier_of(Decimal("40"), config) is Tier.T1
    assert tier_of(Decimal("400"), config) is Tier.T2
    assert tier_of(Decimal("4000"), config) is Tier.T3
    assert tier_of(None, config) is Tier.T3  # no USD amount: never automated


def test_a_reviewed_sample_is_never_overwritten(tmp_path: Path) -> None:
    labeler = _script("label_scenarios")
    sample = tmp_path / "review_sample.csv"
    assert not labeler.has_verdicts(sample)  # no file yet
    sample.write_text("case_id,reviewer_verdict,reviewer_notes\nS001,,\n", encoding="utf-8")
    assert not labeler.has_verdicts(sample)  # drawn, not reviewed
    sample.write_text("case_id,reviewer_verdict,reviewer_notes\nS001,ok,\n", encoding="utf-8")
    assert labeler.has_verdicts(sample)
    sample.write_text("case_id,reviewer_verdict,reviewer_notes\nS001,,ver S013\n", encoding="utf-8")
    assert labeler.has_verdicts(sample)


def test_the_review_summary() -> None:
    summarizer = _script("summarize_review")
    row = {"data_source": "real", "language": "es", "path": "resolution", "reviewer_notes": ""}
    rows = [
        {**row, "case_id": "R002", "reviewer_verdict": "OK"},
        {
            **row,
            "case_id": "S049",
            "reviewer_verdict": "Discrepancia",
            "reviewer_notes": "TRX-8YRH1U8OHC6RHVPOX4KZ no es pendiente",
        },
    ]
    summary = summarizer.summarize(rows)
    assert summary["reviewed"] == 2 and summary["confirmed"] == 1
    assert summary["discrepancies"] == [
        {
            "case_key": "S049",
            "verdict": "Discrepancia",
            "reason": "[ref] no es pendiente",
            "resolution": "pending",
        }
    ]
    with pytest.raises(SystemExit, match="without a verdict"):
        summarizer.summarize([{**row, "case_id": "S001", "reviewer_verdict": ""}])
