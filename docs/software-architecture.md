# Software Architecture

| Field | Value |
|---|---|
| Status | Draft |
| Version | 0.1.4 |
| Last updated | 2026-09-28 |
| Related | [Dispute policy](dispute-policy.md), [Glossary](glossary.md), [Decision flow](diagrams/dispute-decision-flow.md), [Case lifecycle](diagrams/dispute-case-lifecycle.md) |

## Contents

1. [Overview](#1-overview)
2. [Architecture diagram](#2-architecture-diagram)
3. [Components](#3-components)
4. [Request walkthrough](#4-request-walkthrough)
5. [Technology decisions](#5-technology-decisions)
6. [Deployment](#6-deployment)
7. [Security and data boundaries](#7-security-and-data-boundaries)
8. [Reliability](#8-reliability)
9. [Observability](#9-observability)
10. [Artifacts built offline](#10-artifacts-built-offline)
11. [Limitations and open items](#11-limitations-and-open-items)
12. [Change log](#12-change-log)

## 1. Overview

The system is a **modular monolith**: one Python backend split into modules with clear responsibilities, one React frontend, one PostgreSQL database, and one self-hosted decision model. Only components with a technical reason to run separately are separate services:

- **Kev** runs in its own container because it loads model weights and has a different lifecycle: it is retrained and replaced without touching the backend.
- **OpenAI** is an external API.

Everything else lives in the backend process. For a ten-day build by a single developer, fewer services mean fewer things to deploy, debug, and explain, while the module boundaries keep the design ready to split later.

The central design rule comes from the [dispute policy](dispute-policy.md#2-design-principles): **deterministic rules decide; models inform.** Models interpret language and produce signals. Only the Policy Engine decides outcomes, and only the Tool Layer executes actions, each with its own checks.

## 2. Architecture diagram

![Software architecture](diagrams/images/software-architecture.png)

**Reading the diagram**

- The diagram shows only what runs at request time. Artifacts produced once, such as the loaded banking data and the model weights, are described in [§10](#10-artifacts-built-offline).
- Arrows show the direction in which data flows toward the user: from storage and models, through the backend, to the frontend.
- **Blue lines** carry data into and within the backend. **Orange lines** carry data from the backend to the frontend.
- **Cubes** mark the two components that decide and act: the Policy Engine and the Tool Layer.
- The diagram was drawn in [Isoflow](https://isoflow.io/). The PNG in `docs/diagrams/images/` is the exported version.

## 3. Components

### Storage: PostgreSQL 16 with SQLAlchemy and Alembic

| Component | What it does | Why it exists |
|---|---|---|
| **Core Banking** | Stores customers, products, and transactions loaded from the supplied dataset, keeping only the columns the system uses plus the fairness attributes ([DATA-06](dispute-policy.md#12-data-handling-and-fairness)). The application only reads it. | Source of truth for verified facts. Responses must be grounded in permitted account and transaction data. |
| **Cases** | Stores dispute cases and handoff packets. | Where actions are written and read back for verification ([ACT-02](dispute-policy.md#8-actions-and-confirmations)). |
| **Audit** | Stores sessions and the execution record of every turn. | Explanations must come from execution records, not from hidden model reasoning. |

### Model services

| Component | What it does | Why it exists |
|---|---|---|
| **Kev 0.8B** | Answers closed questions with probabilities: reason code, ambiguity, escalation risk, and manipulation attempts. Self-hosted, runs on CPU. | The learned component evaluated against a baseline. Cheap, fast, and its outputs can be measured and calibrated. |
| **GPT-6 Luna** (OpenAI API) | Understands the customer's free text and writes the sentences that connect templates. | Natural language in Spanish and Portuguese varies too much for rules. |

### Backend: Python 3.12 with FastAPI

| Component | What it does | Why it exists |
|---|---|---|
| **Orchestrator** | Receives each customer message and calls the other modules in order, running the LLM and Kev calls in parallel. | Single entry point per turn. Parallel calls reduce p50 and p95 latency. |
| **Identity Service** | Authenticates with a document number and a test OTP, and manages revocable sessions with expiry, using JWTs signed with HS256 only (PyJWT). The document is looked up by its HMAC. Its own lockout (`OTP_MAX_FAILURES` failed OTPs per document within a window) is separate from the policy's `AUTH_MAX_ATTEMPTS`, which counts conversation turns and is applied by the Orchestrator. A mock of a real identity provider. | The challenge requires a trusted test session; a customer ID alone does not prove identity ([GATE-02](dispute-policy.md#5-gates)). |
| **Input Guard** | Detects prompt injection and impersonation attempts before the text reaches the LLM, combining rules with a Kev question. | Prompt injection must be evaluated and handled ([ESC-13](dispute-policy.md#7-mandatory-escalation-triggers)). |
| **LLM Adapter** | Wraps the OpenAI API, requests JSON-schema structured outputs, and removes fields the model does not need. | Isolates the provider so a change is a configuration change. Enforces data minimization ([DATA-01](dispute-policy.md#12-data-handling-and-fairness)). |
| **Decision Client** | Queries Kev through its TypeSafe-compatible API using `httpx`. Falls back to the LLM with structured output if Kev is unavailable. | Keeps the learned component behind one interface so it can be compared, replaced, and degraded safely. |
| **Policy Engine** | Evaluates gates, escalation triggers, and precedence. Parameters are read from a versioned `policy.yaml`. The only component that decides outcomes. | Policy must be enforced outside model-generated prose. Deterministic and unit-testable. The same code produces the reference labels for evaluation. |
| **Tool Layer** | Runs reads and actions (create case, block card) with its own permission checks, Pydantic contracts, idempotency keys, bounded retries (`tenacity`), and read-back verification. | Permissions must be enforced in the tool layer, and only verified actions may be reported. It refuses improper requests even if a model asks for them. |
| **Handoff Builder** | Builds the handoff packet for the human agent: verified facts, actions taken, triggered rules, and open questions. | Handoffs must carry useful context without the raw transcript ([policy §13](dispute-policy.md#13-handoff-packet)). |
| **Templates** | Holds versioned Spanish and Portuguese texts for every customer commitment. | Case references, deadlines, and refusals must not be freely generated ([COM-02](dispute-policy.md#11-customer-communication)). |
| **Audit & Tracing** | Assigns a `trace_id` to every turn and records the rules evaluated, calls made, results, latencies, and token usage. | Provides tracing and execution records, and feeds latency and cost metrics. |

### Frontend: React with TypeScript and Vite

One single-page application with four views:

| Component | What it does | Why it exists |
|---|---|---|
| **Customer Chat** | Customer conversation with OTP login, in Spanish or Portuguese. | Main demo surface. |
| **Agent Console** | Queue of escalated cases with their handoff packets. | Shows safe escalation to a human. |
| **Audit Viewer** | Displays the trace of each turn. | Makes the reason behind every decision visible to agents and reviewers. |
| **Metrics Dashboard** | Shows evaluation results and operational metrics, built with Recharts. | Communicates the measured quality and efficiency of the system. |

## 4. Request walkthrough

What happens when a customer sends one message. Step numbers match the order of the [decision flow](diagrams/dispute-decision-flow.md).

1. **Customer Chat** sends the message with the session token to the **Orchestrator**, which opens a trace.
2. **Identity Service** validates the session. If it is missing or expired, the turn ends with an authentication request.
3. **Input Guard** checks the message for manipulation attempts.
4. In parallel, the **LLM Adapter** extracts candidate slot values and the **Decision Client** asks Kev for the reason code, ambiguity, and escalation risk.
5. The **Policy Engine** evaluates gates and triggers using the verified records it requests from the **Tool Layer**, the candidate slots, and the model signals. It returns exactly one outcome.
6. If the outcome authorizes an action, the **Tool Layer** executes it, retries within bounds, and reads the result back. An unverified action is never reported to the customer.
7. On `ESCALATE`, the **Handoff Builder** writes the packet to Cases, where the **Agent Console** picks it up.
8. **Templates** produce the committed text; the LLM Adapter may add connecting sentences around it.
9. **Audit & Tracing** writes the turn record, and the Orchestrator returns the reply to **Customer Chat**.

## 5. Technology decisions

| Decision | Choice | Alternatives considered | Rationale |
|---|---|---|---|
| Backend language | Python 3.12 | Node.js with TypeScript | The Policy Engine must be the same code in production and in the reference labeler, and the decision model, training, and evaluation tooling are all Python. |
| Web framework | FastAPI | Flask | Native Pydantic validation for data contracts, `async` for parallel model calls, and automatic OpenAPI output that generates the frontend's TypeScript types. |
| Database | PostgreSQL 16 | SQLite | Handles concurrent writes from a public deployment and matches what a bank would operate. Runs as a single container. |
| ORM and migrations | SQLAlchemy 2 with Alembic | Prisma | Prisma Client Python was deprecated in March 2025 and archived in April 2025. SQLAlchemy is the standard Python ORM and integrates with FastAPI and Pydantic. |
| Conversational model | OpenAI GPT-6 Luna | Qwen (hosted API or self-hosted) | Reliable JSON-schema structured outputs and simple governance for a demo. Self-hosting large Qwen models is not possible on the available hardware. Cost is not a deciding factor at this scale. |
| Decision model | Kev 0.8B on CPU | Jev (hosted API), LLM structured output | Open weights, runs on the available hardware, can be fine-tuned, and exposes typed probabilities that can be calibrated. The LLM path remains as a fallback and as a comparison. |
| Architecture style | Modular monolith | Microservices | Fewer moving parts for a single developer in ten days, with module boundaries that allow a later split. |
| Frontend | React, TypeScript, Vite | Angular | Fast setup and a single app with four routes. |
| Deployment | Docker Compose | Kubernetes, managed services | One command to run anywhere: a personal server or a cloud VM. |

## 6. Deployment

The deployable unit is one Docker Compose project with four containers:

| Container | Contents | Exposed |
|---|---|---|
| `frontend` | React build served as static files | Public |
| `api` | FastAPI backend (all backend modules) | Public, behind rate limiting |
| `kev` | Kev server with its weights | Internal network only |
| `postgres` | PostgreSQL 16 | Internal network only |

The OpenAI API is called from the `api` container over HTTPS. Configuration and secrets come from environment variables (see `.env.example`); nothing secret is baked into images.

Target hosts, in order of preference: the developer's own server, or a cloud VM if hosting credits become available. The same Compose file is used in both cases.

## 7. Security and data boundaries

- **Permissions in the Tool Layer.** Every tool call uses the customer ID from the authenticated session, never from model output. Requests for other customers' records return `access_denied` ([GATE-04](dispute-policy.md#5-gates)).
- **Policy outside prompts.** Outcomes and action authorizations come from the Policy Engine. Prompts cannot widen permissions.
- **Data minimization.** The LLM Adapter sends only the fields allowed by [DATA-01](dispute-policy.md#12-data-handling-and-fairness). Kev runs inside the deployment, so its inputs never leave it.
- **Internal services.** Kev and PostgreSQL are reachable only on the Compose internal network.
- **Abuse and cost limits.** The public API applies per-session rate limits and a token cap per conversation, and the OpenAI account has a spending limit.
- **Secrets.** Kept in `.env`, excluded from version control.

## 8. Reliability

| Failure | Handling |
|---|---|
| OpenAI call fails or times out | Bounded retries. If it still fails, the turn escalates or asks the customer to retry; no action is taken on partial output. |
| Kev unavailable | The Decision Client falls back to the LLM with structured output and records the fallback in the trace. |
| Tool write fails | Bounded retries with idempotency keys. After `TOOL_MAX_RETRIES`, the case escalates under [ESC-10](dispute-policy.md#7-mandatory-escalation-triggers). |
| Read-back mismatch | The action is treated as failed and escalated; the customer is not told it succeeded ([COM-04](dispute-policy.md#11-customer-communication)). |
| Session expired mid-conversation | The next evaluation stops at GATE-02 and asks the customer to authenticate again. A confirmation received with an expired session is not valid: after re-authentication, the COM-03 summary is presented again. |

## 9. Observability

Every turn has a `trace_id` that links the customer message, rule evaluations, model calls (with model and prompt versions), tool calls, outcome, latency, and token usage. Traces are stored in the Audit database and displayed in the Audit Viewer. The same records produce the operating metrics required by the challenge: p50/p95 latency and cost per attempted case and per successful automated resolution.

## 10. Artifacts built offline

Some inputs to the running system are produced once, outside the request path:

| Artifact | Produced by | Consumed by |
|---|---|---|
| Core Banking data | `scripts/ingest.py` (raw CSV to silver Parquet: all partitions, deduplication by key, source file per row), then `scripts/load_core_banking.py` (silver to core Parquet: data contract, minimization, USD equivalent; then an idempotent upsert into PostgreSQL). Writes `reports/core_build.json`. | Tool Layer |
| Kev weights | Fine-tuning on team-generated, policy-labeled cases | Kev server |
| Calibrated thresholds | Validation split during model evaluation | `policy.yaml` |
| Evaluation reports | Evaluation harness running baseline and system on the held-out set | Metrics Dashboard, final submission |

The data and ML pipelines will be documented separately.

## 11. Limitations and open items

- The identity service, core banking data, and case store are mocks with documented contracts; they are not production integrations.
- A production deployment with strict data residency would replace the OpenAI API with a self-hosted model; that requires GPU hardware not available for this prototype.
- Kev 0.8B has a limited knowledge base and its calibration is verified only on our evaluation data.
- The conversational model is identified only by its alias: the API reports `gpt-6-luna` as the model and no `system_fingerprint`, so the provider can change the underlying model without notice. Mitigation: parser regression tests on recorded answers (`tests/fixtures/llm/`), and repeated runs per case in the evaluation (M18), since no `temperature` is sent.
- Capacity limits of the deployment have not been measured yet; they will be reported with the evaluation results.
- Two clocks: transaction-age rules use a simulated business date (`BUSINESS_DATE`, because the supplied data ends on 2026-06-17), while session age uses real time ([policy §15](dispute-policy.md#15-parameters)).

## 12. Change log

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-09-26 | First draft. |
| 0.1.1 | 2026-09-28 | Two clocks limitation; confirmation after mid-conversation session expiry (policy 0.2.0). |
| 0.1.2 | 2026-09-28 | Core Banking minimization and the offline loading pipeline (policy 0.3.0). |
| 0.1.3 | 2026-09-28 | Identity Service: document + OTP login, HS256 only, revocable sessions, OTP lockout. |
| 0.1.4 | 2026-09-28 | Limitation: the conversational model is identified only by its alias. |
