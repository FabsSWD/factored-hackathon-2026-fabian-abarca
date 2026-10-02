# Backend Acceptance

| Field | Value |
|---|---|
| Status | Accepted, pending the `backend-v1` tag |
| Version | 0.2.0 |
| Last updated | 2026-10-02 |
| Milestone | M13, exit gate of phase 1 |
| Scope | Policy v0.4.10, templates 1.12.0, `extract@1.9.0`, `connect@1.2.0` |
| Related | [Dispute policy](dispute-policy.md), [Software architecture](software-architecture.md) |

## Contents

1. [Verdict](#1-verdict)
2. [How the backend was tested](#2-how-the-backend-was-tested)
3. [Results](#3-results)
4. [Policy coverage](#4-policy-coverage)
5. [Conversation behaviors](#5-conversation-behaviors)
6. [Security review](#6-security-review)
7. [Latency](#7-latency)
8. [Findings](#8-findings)
9. [Known limitations](#9-known-limitations)
10. [How to reproduce](#10-how-to-reproduce)

## 1. Verdict

The backend meets the acceptance criteria. The one defect found (D1, medium) was fixed in this milestone, and the reading of `GATE-02` (O1) was settled in policy 0.4.10.

- Every gate (`GATE-01` to `GATE-11`) and every trigger (`ESC-01` to `ESC-14`) was exercised at least once as a whole conversation through the HTTP API.
- There are zero false negatives on the hard rules: all 13 hard triggers escalate, to the queue and with the priority of policy §7, and none of them also creates a case.
- The security review found no leak of other customers' data, no accepted forged token, no internal error shown to a customer and no prohibited data in replies, packets or traces.
- Coverage is 99% overall, with the Policy Engine and the Tool Layer at 100%.
- The whole suite is green: 2529 tests.
- Two findings of low severity (O2, O3) go to M20.

## 2. How the backend was tested

The end-to-end suite (`tests/e2e/`) drives the real application through `/auth/*` and `/api/turn`.

| Component | In the end-to-end suite |
|---|---|
| FastAPI application, routes, rate limits | Real |
| Identity Service: document number and test OTP login, JWT sessions, expiry, revocation | Real |
| Input Guard, Policy Engine, templates, Orchestrator | Real |
| Tool Layer, Handoff Builder with database-numbered handoffs, handoff queue, audit tracer | Real, on PostgreSQL 16 |
| Core Banking data | The synthetic fixtures of `tests/fixtures/core_banking.py`, loaded through the real ingestion pipeline, plus rows a test adds |
| OpenAI (`extract`, `connect`) | Double: returns, for each customer message, what the extraction prompt asks for it |
| Kev | Double: unavailable by default, or the signals a test gives |

The behavior of the real model on the same kinds of messages is pinned separately by the recorded fixtures (`tests/llm_adapter/test_recorded_fixtures.py`, 37 tests on real `gpt-6-luna` answers).

Every reply of every end-to-end turn passes a common check: no rule identifier, threshold or internal identifier; no full card or document number; no trace of an internal error; no amount without its currency code; and no number of days other than the resolution commitment.

## 3. Results

| Suite | Tests | Result |
|---|---|---|
| Whole project (`pytest`) | 2529 | Passed |
| End to end (`tests/e2e/`) | 65 | Passed |
| Gates | 26 | Passed |
| Escalation triggers | 17 | Passed |
| Conversation behaviors | 8 | Passed |
| Security review | 12 | Passed |
| Light load (fast doubles) | 2 | Passed |

Coverage of `app/` is 99% (5520 statements). The Policy Engine (`engine.py`, `matching.py`) and the Tool Layer are at 100%, the Orchestrator at 98%. Ruff and mypy (strict) report nothing.

## 4. Policy coverage

### Gates

| Gate | Scenario through the API | Result |
|---|---|---|
| `GATE-01` | A message in English is asked once for a language, then escalates | `ESC-12` |
| `GATE-02` | Without a session nothing is read; an explicit refusal; three login requests without a login; the reason given before the login is kept after it | `CLARIFY`; `INFORM`; `INFORM` on the fourth turn (policy 0.4.10); kept |
| `GATE-03` | A suspended customer | `ESC-08` |
| `GATE-04` | Another customer's transaction, and one that does not exist | The same `REFUSE`; a security event |
| `GATE-05` | Several matches listed and one picked; nothing found; an approximate amount | List; "Busqué … y no encontré ninguna"; a candidate confirmed with yes |
| `GATE-06` | Pending, declined and reversed transactions | `INFORM` with a transfer offer |
| `GATE-07` | **H**: an unrecognized transfer; **N**: a purchase disputed as a bank fee | `ESC-14` to the fraud queue; `INFORM` |
| `GATE-08` | 90 days old; older than `LATE_WINDOW_DAYS` | `ESC-07`; `INFORM` |
| `GATE-09` | A closed card | `ESC-08` |
| `GATE-10` | Incorrect amount not exceeded; delivery date not reached; merchant not contacted; a bank fee; a duplicate charge; shared credentials | `INFORM` ×3; `RESOLVE`; `RESOLVE` on the later charge; `ESC-03` |
| `GATE-11` | A second dispute of a transaction with a case | `INFORM` with its reference and status, no second case |

The whole automated path is also covered: a card block offer that names the charge, a block confirmed and read back, the summary, and a case created and verified (`RESOLVE`).

### Escalation triggers

| Trigger | Scenario | Queue, priority |
|---|---|---|
| `ESC-01` | A USD 1,500 purchase (`T3`) | Disputes, normal |
| `ESC-02` | A fourth dispute after three cases in 90 days | Disputes, normal |
| `ESC-03` | A stolen phone (the block is offered first), and a batch of three unrecognized charges | Fraud, high |
| `ESC-04` | Fraud score 91.5 | Fraud, normal |
| `ESC-05` | A request for a person in the middle of the flow | Disputes, normal |
| `ESC-06` | A threat to sue | Disputes, high |
| `ESC-07` | A 90-day-old purchase | Disputes, normal |
| `ESC-08` | A suspended customer | Disputes, normal |
| `ESC-09` | Two answers without information | Disputes, normal |
| `ESC-10` | `ACT-02` fails after its retries | Disputes, normal |
| `ESC-11` | Both model services down (soft trigger) | Disputes, normal |
| `ESC-12` | English twice | Disputes, normal |
| `ESC-13` | An injection quoted as "the text of the transaction", then a second attempt | Security review, normal |
| `ESC-14` | An unrecognized transfer | Fraud, normal |

`tests/e2e/test_escalations.py::test_zero_false_negatives_on_hard_rules` runs the 13 hard triggers. For each one it checks the trace, the handoff packet in the database (rule, queue, priority, one escalation reason with evidence per rule) and that no case was created. While the thresholds are null, `ESC-11` also never fires on Kev signals (tested).

## 5. Conversation behaviors

These are the behaviors added in the M12 manual tests, checked end to end on the database:

- **Side questions.** A refund question during the card block offer is answered and the offer is repeated. The status of the customer's own case is given, and another customer's case reference gets "not found". A question outside disputes offers a transfer only once.
- **Tolerant search.** An approximate amount with a period of days lists the near charge. A generic "restaurante" is searched by category: it finds the charge of El Buen Sabor and not another Food purchase of a different amount, and no `flow_help` text is sent.
- **Card block.** "Ese no es" at the block offer blocks nothing.
- **Closing.** "Gracias" after `RESOLVE` gets the closing reply and keeps the outcome.
- **After a handoff.** The customer gets a neutral notice that reaches no model. The agent's packet gets the message, masked.

## 6. Security review

| Check | Result |
|---|---|
| A search never lists another customer's transactions (a merchant both customers share) | Passed |
| Another customer cannot continue a conversation (403, nothing confirmed) | Passed |
| A customer token reads no handoff, trace or metric (403 on the agent and audit APIs) | Passed |
| Forged tokens (changed signature, `alg: none`, garbage, appended bytes) get no session: login is asked, no account data | Passed |
| A revoked session reads nothing; an expired one asks to log in and reconfirm, and its confirmation does not count | Passed |
| An injection quoted as transaction text reaches no model and is logged; strikes survive a login; the customer is never told about the detection | Passed |
| An unexpected error in the Tool Layer reaches the customer only as a handoff, without its message; the trace keeps it | Passed |
| Malformed requests (empty, too long, path in the conversation ID, extra fields, invalid JSON) get 422 without internals | Passed |
| Packets and traces carry no customer ID, document or card number; the message is stored masked | Passed |
| Every reply of the 65 end-to-end tests passes the common check of §2 | Passed |

## 7. Latency

There were 100 turns, 20 conversations of 20 different customers running concurrently with 5 turns each: a dispute, its confirmation, a thank-you, a side question and an answer without details. The model doubles wait for a latency drawn uniformly from each profile, and the rest of the turn is real code. `extract` and Kev run in parallel, and `connect` follows.

| Doubles | `connect` | p50 | p95 | Max |
|---|---|---|---|---|
| Measured (`extract` 2.0–4.9 s, `connect` 1.5–2.5 s, Kev 0.2–0.4 s) | On | 4.84 s | 6.89 s | 7.34 s |
| Measured | Off (`LLM_CONNECT_ENABLED=false`) | 3.06 s | 4.58 s | 4.87 s |
| Fast (`extract` 50–120 ms, `connect` 30–60 ms, Kev 10–30 ms) | On | 0.14 s | 0.19 s | 0.20 s |
| Fast | Off | 0.08 s | 0.12 s | 0.14 s |

- **Targets.** The milestone's targets (p50 < 2 s, p95 < 5 s) are for fast doubles, and they are met with a wide margin. The backend's own work is about 30 to 40 ms per turn.
- **With the measured latencies,** the turn is the model time: p50 is 4.8 s with connecting sentences and 3.1 s without them. `connect` adds about 1.8 s at p50 and 2.3 s at p95.
- **The parallel calls are tested:** a turn takes max(`extract`, Kev) + `connect`, not their sum.
- **Different customers on purpose.** A first version used one customer for all 20 conversations, and depending on the interleaving `ESC-02` (dispute velocity) rightly fired after three cases.
- **What the run does not include:** database and network time. Closings and post-handoff replies skip `connect`, so the "on" rows include some turns without it.

Raw results: `reports/m13_load_results.json`.

## 8. Findings

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| D1 | Medium | When `extract` fails and Kev is unavailable, the Orchestrator derives `llm_fallback` signals from the empty rule-only extraction instead of leaving them `unavailable`. This contradicts architecture §8 ("If the extraction failed too, the signals stay unavailable and count as uncertainty"). With the thresholds null, `ESC-11` then never fires. The conversation keeps asking until `ESC-09` instead of escalating at once. No automated resolution is possible meanwhile, because nothing fills the slots or the confirmation. | **Fixed in M13.** When `extract` failed (or the token cap skipped it) and Kev did not answer, the turn's signals are `unavailable` and `ESC-11` fires; the rule detector's interrupts still arrive. Tested end to end and in the Orchestrator. |
| O1 | Low | `GATE-02` informed on the fifth turn, after four login requests. The policy's "when the attempts exceed" also read as the fourth turn, and the reading changes the labels of M17. | **Decided in M13 (policy 0.4.10).** `AUTH_MAX_ATTEMPTS` is the number of authentication requests: with 3, the third request is a `CLARIFY` and the fourth turn without a session is the `INFORM`. Engine and tests changed. |
| O2 | Low | The last relaxation step leaves the merchant out even when the customer named a concrete one that does not exist. "Zapatería Inventada, unos 70" lists a USD 75 purchase at Tienda Remota. The customer must pick it, so it is safe, but it can confuse. | **M20:** keep the step for categories and absent merchants only. |
| O3 | Low | With the measured latencies, p95 with connecting sentences is 6.9 s. | **M20:** decide whether `connect` stays on for the demo, or runs only on the first turn and on bad news. |

### Carried to M20

- **O2:** limit the last relaxation step of `GATE-05` (without the merchant) to merchants given as a category or left out, not to a concrete name that matches nothing.
- **O3:** decide the use of connecting sentences for the demo (on, off, or only on the first turn and on bad news), against p95 6.9 s with them and 4.6 s without them.

## 9. Known limitations

- **One worker.** Conversation state is in memory, in one process, so the API runs with a single worker (`scripts/serve.py`), and a restart loses open conversations.
- **Simulated handoff queue.** The handoff queue is the `handoff_packets` table read by the agent API; there is no real queue system.
- **Kev uncalibrated.** Kev answers the decision questions in English and is not calibrated: the `ESC-11` thresholds are null, and its signals inform nothing yet.
- **Paths without real data.** `RC_DUPLICATE` (the supplied data has no pair that meets the rule) and some tier and route combinations are exercised only on the synthetic fixtures.
- **Model doubles in the end-to-end suite.** It proves the backend's behavior given the extraction, not the model's extraction. That one is pinned by the recorded fixtures, and its error rate is measured in M17 and M18.
- **No database or network in the load test.** It runs in one process without them; the numbers are the model time plus the backend's own work.
- **Fixed business date.** Dates in the scenarios depend on the business date 2026-06-17 of the fixtures.

## 10. How to reproduce

```
pytest                              # whole suite (database tests need TEST_DATABASE_URL)
pytest tests/e2e                    # end to end only
pytest --cov=app                    # with coverage
python scripts/load_test.py         # light load with measured and fast doubles (about a minute)
```

`TEST_DATABASE_URL` must point to a PostgreSQL 16 maintenance database; each run creates and drops its own database (see `tests/conftest.py`).
