# Dispute Intake Policy

| Field | Value |
|---|---|
| Status | Draft |
| Version | 0.2.0 |
| Last updated | 2026-09-28 |
| Related | [Glossary](glossary.md), [Data label validity spike](spikes/2026-09-25-data-label-validity.md), [Decision flow](diagrams/dispute-decision-flow.md), [Case lifecycle](diagrams/dispute-case-lifecycle.md) |

> **Synthetic policy.** This policy is written for a synthetic bank in a hackathon prototype. It is not legal advice and has not been reviewed against the regulations of Mexico, Colombia, Argentina, or Brazil, nor against card network rules. See [§17](#17-assumptions-limitations-and-open-questions).

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Design principles](#2-design-principles)
3. [Reason codes](#3-reason-codes)
4. [Disputable transactions](#4-disputable-transactions)
5. [Gates](#5-gates)
6. [Automation tiers](#6-automation-tiers)
7. [Mandatory escalation triggers](#7-mandatory-escalation-triggers)
8. [Actions and confirmations](#8-actions-and-confirmations)
9. [Outcomes and precedence](#9-outcomes-and-precedence)
10. [Required information and clarification](#10-required-information-and-clarification)
11. [Customer communication](#11-customer-communication)
12. [Data handling and fairness](#12-data-handling-and-fairness)
13. [Handoff packet](#13-handoff-packet)
14. [Responsibility split](#14-responsibility-split)
15. [Parameters](#15-parameters)
16. [Deriving evaluation labels](#16-deriving-evaluation-labels)
17. [Assumptions, limitations, and open questions](#17-assumptions-limitations-and-open-questions)
18. [Change log](#18-change-log)

## 1. Purpose and scope

This policy defines how the system handles a customer who wants to dispute a transaction: which disputes it may register on its own, which require confirmation, when it must abstain, and when it must transfer to a human agent.

**Workflow.** Transaction-dispute *intake*. The system identifies the transaction, checks eligibility, collects the required information, registers a dispute case, and informs the customer of next steps. It does **not** decide the dispute, move money, or grant compensation. Investigation and final decisions belong to human back-office agents.

**Why this workflow.** In the supplied data, `Queja` contacts are 17.1% of call volume but 23.1% of agent handle time and 41.2% of all unresolved contacts, with a first-contact resolution rate of 43.6%. Within the complaints table, `Cargo no reconocido` and `Cobro indebido` account for about 40% of cases. Evidence and method are in the [data label validity spike](spikes/2026-09-25-data-label-validity.md).

**In scope**

- Disputes on posted transactions of the authenticated customer's own products.
- The five reason codes in [§3](#3-reason-codes).
- Protective card blocking for unrecognized card transactions.
- Spanish (`es`) and Portuguese (`pt`) conversations.

**Out of scope**

- Deciding the dispute, issuing refunds, reversals, provisional credit, or compensation.
- Credit decisions, loan restructuring, and account opening or closing.
- Changes to customer contact data or credentials.
- Status updates on existing cases beyond reporting the case reference and its current state.

## 2. Design principles

1. **Deterministic rules decide; models inform.** Only the deterministic policy engine chooses outcomes and authorizes actions. Model outputs are inputs to that engine, never substitutes for it.
2. **Autonomy is earned per case.** The system acts on its own only when every gate passes, no escalation trigger fires, and the amount falls in an automated tier.
3. **Verify before claiming.** The system reports an action only after reading back its result from the system of record.
4. **Escalation is cheap; unsafe automation is expensive.** When in doubt, the system escalates. Thresholds are set to minimize unsafe outcomes first and unnecessary escalations second.
5. **Explanations come from rules and records.** Every outcome is explained by the rule identifiers and records that produced it, never by hidden model reasoning.
6. **Protected attributes never drive outcomes.** See [DATA-02](#12-data-handling-and-fairness).

## 3. Reason codes

| Code | Name | Definition | Maps to supplied data |
|---|---|---|---|
| `RC_UNRECOGNIZED` | Unrecognized transaction | The customer states they did not make or authorize the transaction. | `complaints.subcategory = 'Cargo no reconocido'` |
| `RC_DUPLICATE` | Duplicate charge | The same charge was posted more than once. Must meet the duplicate criteria in [GATE-10](#5-gates). | No direct mapping |
| `RC_INCORRECT_AMOUNT` | Incorrect amount | The customer authorized the transaction but the posted amount is higher than agreed. | No direct mapping |
| `RC_NOT_RECEIVED` | Goods or services not received | The customer paid for goods or services that were not delivered. | No direct mapping |
| `RC_FEE` | Unjustified bank fee | The customer disputes a charge originated by the bank. | `complaints.subcategory = 'Cobro indebido'` |

A conversation may raise at most one reason code per transaction. If the customer raises several transactions, each one is evaluated independently and receives its own outcome.

## 4. Disputable transactions

Transaction types come from `transactions.transaction_type` in the data dictionary. The values in the table below match the data dictionary; how bank fees appear is still open (see [open questions](#17-assumptions-limitations-and-open-questions)).

Legend: **A** = eligible for automated intake (subject to all other rules); **H** = plausible dispute that is not automated, so the system escalates under [ESC-14](#7-mandatory-escalation-triggers); **N** = not disputable in this workflow, so the system informs the customer and offers a human transfer.

| `transaction_type` | `RC_UNRECOGNIZED` | `RC_DUPLICATE` | `RC_INCORRECT_AMOUNT` | `RC_NOT_RECEIVED` | `RC_FEE` |
|---|---|---|---|---|---|
| Purchase | A | A | A | A | N |
| Withdrawal | A | H | H | H | N |
| Transfer | H | H | N | N | N |
| Payment | H | H | N | N | N |
| Deposit | N | N | N | N | N |
| Adjustment (bank-originated) | N | A | N | N | A |

Rationale:

- **Purchase** is the classic merchant dispute and the main automation target.
- **Withdrawal** disputes other than unrecognized ones (for example, cash not dispensed) need ATM journal review, which our mock tools cannot provide.
- **Transfer** and **Payment** are initiated by the customer. Unauthorized ones suggest account takeover and belong to the fraud team.
- **Deposit** is a credit to the customer; missing deposits are an inquiry, not a dispute.
- **Adjustment** is how we expect bank fees to appear. This mapping is provisional until the transactions data is profiled.

## 5. Gates

Gates are evaluated in the order listed. Evaluation stops at the first gate that does not pass, and that gate determines the outcome. The same order appears in the [decision flow diagram](diagrams/dispute-decision-flow.md).

**Combining gates and escalation triggers.** Rules are evaluated in two channels, and [§9](#9-outcomes-and-precedence) precedence resolves among all candidate outcomes:

1. **Interrupts**: `ESC-03`, `ESC-05`, `ESC-06`, and `ESC-13` are evaluated on every turn, whatever the gate results, because they come from the conversation, not from the transaction record.
2. **Gates** run in order and stop at the first that does not pass. `ESC-07`, `ESC-08`, `ESC-12`, and `ESC-14` are the outcomes of their gates.
3. **Record-dependent triggers**: `ESC-01`, `ESC-02`, and `ESC-04` are evaluated only when every gate passes.

`ESC-09` replaces a `CLARIFY` once its limits are exceeded, `ESC-10` follows a failed action, and `ESC-11` is evaluated as described in [§7](#7-mandatory-escalation-triggers).

*Rationale.* The combination rule defines the reference labels, so it decides what counts as a missing or an unnecessary transfer. For example, a pending transaction with `fraud_score` 90 ends in `INFORM` (nothing can be disputed yet, and `GATE-06` stops before `ESC-04`), while a pending transaction where the customer mentions a lawyer ends in `ESCALATE` (`ESC-06` is an interrupt). Interrupts express what the customer needs now; record-dependent triggers only make sense for a transaction that can be disputed.

| ID | Gate | Pass condition | If it does not pass |
|---|---|---|---|
| `GATE-01` | Supported language | Detected language is `es` or `pt`. | One clarification asking for the preferred language, then [ESC-12](#7-mandatory-escalation-triggers). |
| `GATE-02` | Authenticated session | Session was issued by the identity service, is younger than `SESSION_MAX_AGE_MIN`, and has been idle less than `SESSION_IDLE_TIMEOUT_MIN`. | The system MUST NOT read or disclose account data. It asks the customer to authenticate (a `CLARIFY` turn targeting authentication, not counted as a slot clarification in [§10](#10-required-information-and-clarification)). If the customer declines, or does not authenticate within `AUTH_MAX_ATTEMPTS` attempts, the outcome is `INFORM`. |
| `GATE-03` | Customer status | `customers.customer_status = 'Active'`. | [ESC-08](#7-mandatory-escalation-triggers). |
| `GATE-04` | Ownership | Every transaction or product the customer references has `customer_id` equal to the session customer. Enforced in the tool layer, which returns `access_denied` for other customers' records. | `REFUSE`. The system MUST NOT confirm or deny that the record exists. The attempt is logged as a security event. |
| `GATE-05` | Transaction identified | Exactly one transaction of the session customer matches the reference given. | `CLARIFY` (see [§10](#10-required-information-and-clarification)). |
| `GATE-06` | Transaction status | `transactions.transaction_status = 'Approved'`. | `Pending`: `INFORM` (wait until posted). `Declined`: `INFORM` (nothing was charged). `Reversed`: `INFORM` (already reversed). |
| `GATE-07` | Disputable combination | The transaction type and reason code are marked **A** in [§4](#4-disputable-transactions). | **H**: [ESC-14](#7-mandatory-escalation-triggers). **N**: `INFORM` with an offer to transfer. |
| `GATE-08` | Filing window | Transaction age at filing is at most `DISPUTE_WINDOW_DAYS`. | Age up to `LATE_WINDOW_DAYS`: [ESC-07](#7-mandatory-escalation-triggers). Older: `INFORM` (outside the filing window) with an offer to transfer. |
| `GATE-09` | Product status | `products.product_status` is `Active` or `Blocked`. | [ESC-08](#7-mandatory-escalation-triggers). |
| `GATE-10` | Reason-specific preconditions | See the table below. | See the table below. |
| `GATE-11` | No duplicate case | No open case exists for the same `transaction_id`. | `INFORM` with the existing case reference and status. |

Reason-specific preconditions (`GATE-10`):

| Reason code | Pass condition | If it does not pass |
|---|---|---|
| `RC_UNRECOGNIZED` | Slots `card_in_possession` and `shared_credentials` are answered. `shared_credentials = no`. | `shared_credentials = yes`: [ESC-03](#7-mandatory-escalation-triggers). |
| `RC_DUPLICATE` | A second transaction with a different `transaction_id` exists with the same `product_id`, `merchant_name`, `amount`, and `currency`, status `Approved`, within `DUPLICATE_WINDOW_HOURS`. The earlier one is treated as legitimate and the later one is disputed. | `CLARIFY` once whether the customer means another reason. If it remains unresolved: [ESC-09](#7-mandatory-escalation-triggers). |
| `RC_INCORRECT_AMOUNT` | `expected_amount` is given in the transaction currency and is lower than the posted `amount`. | `expected_amount >= amount`: `INFORM` (the posted amount does not exceed what was agreed). |
| `RC_NOT_RECEIVED` | `expected_delivery_date` is in the past and `merchant_contacted = yes`. | Delivery date not reached: `INFORM` (wait until the date). Merchant not contacted: `INFORM` with guidance to contact the merchant first. |
| `RC_FEE` | The charged fee is identified. | `CLARIFY`. |

## 6. Automation tiers

Tiers use the USD-equivalent amount of the disputed transaction (see [Glossary](glossary.md)).

| Tier | USD-equivalent amount | Case creation | Provisional credit eligibility flag |
|---|---|---|---|
| `T1` | Up to `PROVISIONAL_CREDIT_AUTO_MAX_USD` | Automated (`ACT-02`) | `eligible` |
| `T2` | Above `PROVISIONAL_CREDIT_AUTO_MAX_USD`, up to `AUTO_INTAKE_MAX_USD` | Automated (`ACT-02`) | `requires_review` |
| `T3` | Above `AUTO_INTAKE_MAX_USD` | Not automated. [ESC-01](#7-mandatory-escalation-triggers) with a draft case in the handoff packet. | Decided by a human |

The flag is a recommendation to the back office. The system never applies credit (see [ACT-04](#8-actions-and-confirmations)).

Amounts are compared with `>` and `<=` exactly as written. An amount equal to a threshold belongs to the lower tier.

If `transactions.amount_usd` is null, the USD equivalent is computed with `daily_exchange_rates` for the transaction date and currency (see [Glossary](glossary.md)). If no rate is available, the tier is unknown and is treated as `T3`, so [ESC-01](#7-mandatory-escalation-triggers) fires. *Rationale:* the tier sets how much autonomy the system has, so an unknown amount is handled conservatively.

## 7. Mandatory escalation triggers

Hard triggers (`ESC-01` to `ESC-10`, `ESC-12` to `ESC-14`) always cause `ESCALATE`, regardless of model outputs. The soft trigger `ESC-11` is evaluated only if no hard rule has already decided the outcome. When each trigger is evaluated is defined in [§5](#5-gates).

`transactions.is_fraud` MUST NOT be used by any rule: it is a label the bank assigns afterwards, not a signal available when the dispute is filed. A null `fraud_score` (about 5% of rows) does not escalate, because the case is investigated by a person anyway and escalating it would inflate unnecessary transfers.

| ID | Trigger | Condition | Route |
|---|---|---|---|
| `ESC-01` | High amount | USD-equivalent amount `> AUTO_INTAKE_MAX_USD` (tier `T3`). | Disputes queue |
| `ESC-02` | Dispute velocity | Including the current dispute, the customer's disputed total in the last 30 days exceeds `AGG_DISPUTED_30D_MAX_USD`, **or** the customer has at least `REPEAT_DISPUTES_90D` cases in the last 90 days. | Disputes queue |
| `ESC-03` | Account takeover indicators | The customer reports an unknown login or device, a credential change they did not make, a lost or stolen phone, or sharing credentials or codes with a third party; **or** raises at least `UNRECOGNIZED_BATCH_MAX` unrecognized transactions in one conversation. | Fraud queue, high priority. A card block (`ACT-03`) is offered first. |
| `ESC-04` | Fraud score | `transactions.fraud_score >= FRAUD_SCORE_ESCALATE` on the disputed transaction. A null `fraud_score` does not fire this trigger; its absence is recorded in the audit record and, if there is a handoff, in `open_questions`. | Fraud queue |
| `ESC-05` | Human requested | The customer asks for a human at any point. | Disputes queue. Honored immediately; the system MUST NOT try to retain the customer. |
| `ESC-06` | Legal, regulatory, or vulnerability signals | The customer mentions legal action, a lawyer, a regulator complaint, or the media, or describes serious hardship or distress caused by the charge. | Disputes queue, high priority |
| `ESC-07` | Late filing | Transaction age is above `DISPUTE_WINDOW_DAYS` and at most `LATE_WINDOW_DAYS`. | Disputes queue |
| `ESC-08` | Ineligible status | Customer status is not `Active`, or product status is not `Active` or `Blocked`. | Disputes queue |
| `ESC-09` | Unresolved ambiguity | A slot is still missing or ambiguous after `MAX_CLARIFICATION_TURNS` for that slot, the conversation exceeds `MAX_TOTAL_CLARIFICATIONS`, or the customer's claim contradicts verified facts and clarification does not resolve it. | Disputes queue |
| `ESC-10` | Tool failure | A write action fails after `TOOL_MAX_RETRIES`, or its read-back verification does not match. | Disputes queue, with the failure in the handoff packet |
| `ESC-11` | Model uncertainty (soft) | The decision layer's top reason-code probability is below `DECISION_CONFIDENCE_MIN`, or its escalation-risk probability is at least `ESCALATION_RISK_THRESHOLD`. | Disputes queue |
| `ESC-12` | Unsupported language | The language is not `es` or `pt`, or remains ambiguous after one clarification. | Disputes queue |
| `ESC-13` | Manipulation attempts | The conversation contains at least `INJECTION_STRIKES_MAX` attempts to override instructions, impersonate staff, or request other customers' data. The first attempt is ignored and logged; automation ends at the threshold. | Security review queue |
| `ESC-14` | Plausible but unsupported dispute | The transaction type and reason code are marked **H** in [§4](#4-disputable-transactions). | Fraud queue for `RC_UNRECOGNIZED` on transfers and payments; disputes queue otherwise |

## 8. Actions and confirmations

All permissions are enforced in the tool layer using the session's customer ID. A model instruction cannot widen them.

| ID | Action | Allowed when | Confirmation | Verification |
|---|---|---|---|---|
| `ACT-01` | Read the customer's own profile, products, transactions, and cases | `GATE-02` passed | None | Not applicable |
| `ACT-02` | Create a dispute case | All gates passed, tier `T1` or `T2`, all slots filled, no escalation trigger | Required ([COM-03](#11-customer-communication)) | Read the case back and compare transaction, reason code, tier, and amount. The case reference is shown only after this check passes. |
| `ACT-03` | Temporarily block a card | `GATE-02` passed, and `RC_UNRECOGNIZED` on a card product or `ESC-03` fired, under the conditions below | Required ([COM-03](#11-customer-communication)) | Read back `product_status = 'Blocked'` |
| `ACT-04` | Record the provisional credit eligibility flag | Together with `ACT-02` | None (internal record) | Part of the `ACT-02` read-back |
| `ACT-05` | Transfer to a human with a handoff packet | Always | None | Queue acknowledgement received |
| `ACT-06` | Prohibited actions | Never | Not applicable | Not applicable |

`ACT-06` covers: moving money, reversing transactions, applying credit or compensation, closing or reopening cases, unblocking cards, changing contact data or credentials, disclosing any other customer's data, disclosing full product numbers, and disclosing fraud scores or exact escalation thresholds to the customer.

`ACT-02` uses the idempotency key `transaction_id + reason_code`, so a retry cannot create two cases.

A card block (`ACT-03`) MAY be executed before an escalation, because it protects the customer and can be reversed by a human agent.

**Card block offer.** The system MUST offer `ACT-03` when all of these hold, so that the expected actions of every case are deterministic:

- `GATE-02` passed. If `ESC-03` fires before the customer authenticates, no block is offered.
- The reason is `RC_UNRECOGNIZED`, or `ESC-03` fired.
- The product involved is a card: `products.product_type` is a card type (`Tarjeta Crédito` or `Tarjeta Débito` in the supplied data). The condition depends on the product, not on the transaction type: a purchase charged to a checking account has no card to block.
- `products.product_status = 'Active'`. If the card is already `Blocked`, the system says so and offers nothing. If it is `Closed` or `Suspended`, there is no offer and `GATE-09` escalates under `ESC-08`.

An `RC_UNRECOGNIZED` dispute on a transfer or payment (**H** in [§4](#4-disputable-transactions), routed by `ESC-14`) normally concerns an account, not a card, so there is no `ACT-03`; the handoff packet says so in `open_questions`.

**Explicit confirmation** means an affirmative reply to the templated summary in the same conversation ("sí, confirmo" in Spanish, "sim, confirmo" in Portuguese, or an equivalent). A hedged or unclear reply ("creo que sí", "acho que sim") is not confirmation: the system asks once more, and that question counts as a clarification turn.

A confirmation is valid only if it arrives in a session that passes `GATE-02`. If the session expired before the confirmation, the system asks the customer to authenticate again and then presents the [COM-03](#11-customer-communication) summary again.

## 9. Outcomes and precedence

Every conversation ends with exactly one outcome per disputed transaction.

| Outcome | Meaning | Challenge path |
|---|---|---|
| `RESOLVE` | Dispute case created and verified; customer informed of next steps. | Normal case: automated resolution |
| `CLARIFY` | Information is missing or ambiguous; the system asks. This is a turn outcome; a conversation cannot end in `CLARIFY` (it ends in `ESCALATE` via `ESC-09`, or in another outcome). | Ambiguous request |
| `INFORM` | The request is not disputable or cannot proceed; the system explains why and offers a human transfer. | Unsupported request: abstention |
| `ESCALATE` | A trigger in [§7](#7-mandatory-escalation-triggers) fired; the case is transferred with a handoff packet. | Human required |
| `REFUSE` | The request is unauthorized or unsafe; the system declines without disclosing information. | Safety abstention |

When more than one rule applies at the same time, the outcome with the highest precedence wins:

`REFUSE` > `ESCALATE` > `INFORM` > `CLARIFY` > `RESOLVE`

Examples:

- The customer asks for a human while a slot is missing: `ESCALATE` (ESC-05 beats CLARIFY).
- The transaction is pending and the customer mentions a lawyer: `ESCALATE` (ESC-06 beats INFORM).
- The customer asks about another person's account and also asks for a human: `REFUSE` for the other account; the customer's own request is handled separately.

## 10. Required information and clarification

A slot is filled only from the customer's statements or from verified records. A model MAY propose a value; the policy engine accepts it only if it passes the slot's validation.

| Slot | Required for | Validation |
|---|---|---|
| `transaction_ref` | All reason codes | A `transaction_id`, or a combination of date (±1 day), amount, and merchant that resolves to exactly one transaction of the session customer. |
| `reason_code` | All | One of the codes in [§3](#3-reason-codes). |
| `card_in_possession` | `RC_UNRECOGNIZED` | yes or no |
| `shared_credentials` | `RC_UNRECOGNIZED` | yes or no |
| `duplicate_ref` | `RC_DUPLICATE` | Found by the rule in `GATE-10`, then confirmed by the customer. |
| `expected_amount` | `RC_INCORRECT_AMOUNT` | Positive number, in the transaction currency. |
| `expected_delivery_date` | `RC_NOT_RECEIVED` | Valid date, on or after the transaction date. |
| `merchant_contacted` | `RC_NOT_RECEIVED` | yes or no |
| `fee_ref` | `RC_FEE` | Resolves to one bank-originated transaction. |
| `confirmation` | `ACT-02`, `ACT-03` | Explicit confirmation as defined in [§8](#8-actions-and-confirmations). |

Clarification rules:

- The system asks for one slot per turn, starting with `transaction_ref`, then `reason_code`, then reason-specific slots.
- When several transactions match, the system lists at most `MAX_CANDIDATES_SHOWN` candidates with date, masked product, merchant, and amount, and asks the customer to choose.
- Limits: `MAX_CLARIFICATION_TURNS` per slot and `MAX_TOTAL_CLARIFICATIONS` per conversation. Exceeding either fires [ESC-09](#7-mandatory-escalation-triggers).

## 11. Customer communication

| ID | Rule |
|---|---|
| `COM-01` | The system replies in the language of the customer's latest message (`es` or `pt`). If the language is mixed or unclear, it asks once which language the customer prefers. |
| `COM-02` | Commitments MUST come from versioned templates, never from free generation: case references, target times, next steps, eligibility wording, refusals, and escalation notices. The language model MAY write connecting sentences around them. |
| `COM-03` | Before `ACT-02` or `ACT-03`, the system presents a templated summary (transaction date, masked product, merchant, amount, reason, and the action to take) and asks for explicit confirmation. |
| `COM-04` | The system MUST NOT say an action happened unless its verification passed. |
| `COM-05` | The system MUST NOT promise a refund, credit, or result. It states that the case will be investigated within `RESOLUTION_TARGET_BUSINESS_DAYS` business days. |
| `COM-06` | Product numbers are masked to the last four digits. |
| `COM-07` | Explanations to the customer describe the reason in plain language ("the transaction is still pending"). Rule identifiers appear only in the audit record. |

Template examples (the canonical templates live in the codebase and are versioned):

| Template | `es` | `pt` |
|---|---|---|
| `case_created` | "Registramos su disputa con la referencia {case_ref}. Nuestro equipo la revisará en un plazo de hasta {days} días hábiles y le informaremos el resultado." | "Registramos sua contestação com a referência {case_ref}. Nossa equipe vai analisá-la em até {days} dias úteis e informaremos o resultado." |
| `pending_transaction` | "Esta transacción todavía está pendiente. Podrá disputarla cuando se haya procesado." | "Esta transação ainda está pendente. Você poderá contestá-la quando ela for processada." |
| `handoff` | "Voy a transferir su caso a un agente, que ya tendrá la información que me compartió." | "Vou transferir seu caso para um atendente, que já terá as informações que você me passou." |

## 12. Data handling and fairness

| ID | Rule |
|---|---|
| `DATA-01` | **Minimization.** The language model receives only what the current step needs: a pseudonymous customer reference, masked product numbers, and transaction date, amount, currency, merchant, and status. It MUST NOT receive document numbers, dates of birth, addresses, phone numbers, emails, or full names. |
| `DATA-02` | **Prohibited decision inputs.** These fields MUST NOT influence any outcome, threshold, or model feature: `segment`, `credit_score`, `estimated_monthly_income`, `gender`, `date_of_birth` (or age), `detected_accent`, `occupation`, `marital_status`, `education_level`, and `country` (except for currency conversion and language defaults). They MAY be used only to report outcomes by group for fairness analysis. |
| `DATA-03` | **Claims are not facts.** Customer statements are recorded as claims. Only records read from a data source are verified facts. |
| `DATA-04` | **Provenance.** Every verified fact in the audit record and handoff packet includes its source table and record ID. |
| `DATA-05` | **Synthetic data only.** The prototype uses the supplied synthetic dataset and team-generated data. No real customer data is used, and no customer records are sent to external model providers beyond what `DATA-01` allows. |

## 13. Handoff packet

Every `ESCALATE` outcome produces one handoff packet. It gives the human agent what they need to continue without rereading the conversation. The raw transcript is not included; a reference to it is.

```json
{
  "handoff_id": "HO-20260925-000123",
  "created_at": "2026-09-25T22:45:00Z",
  "language": "pt",
  "queue": "fraud",
  "priority": "high",
  "customer_ref": "CUS-pseudonym-7f3a",
  "auth": { "status": "authenticated", "method": "test_otp", "session_age_min": 6 },
  "request_summary": "Customer disputes an unrecognized purchase and reports a lost phone.",
  "reason_code": "RC_UNRECOGNIZED",
  "triggered_rules": ["ESC-03"],
  "verified_facts": [
    {
      "fact": "Purchase of 1,250.00 MXN at MERCHANT_X on 2026-06-10, status Approved",
      "source": "transactions",
      "record_id": "TXN-..."
    },
    {
      "fact": "Card ending 4821 is Active",
      "source": "products",
      "record_id": "PRD-..."
    }
  ],
  "customer_claims": [
    "Did not make the purchase",
    "Phone was stolen on 2026-06-09"
  ],
  "actions_taken": [
    { "action": "ACT-03", "result": "success", "verified": true, "detail": "Card ending 4821 blocked" }
  ],
  "draft_case": {
    "transaction_ref": "TXN-...",
    "amount_usd": 68.40,
    "tier": "T1",
    "provisional_credit_flag": "eligible"
  },
  "model_signals": {
    "reason_code_probs": { "RC_UNRECOGNIZED": 0.94, "RC_DUPLICATE": 0.03 },
    "escalation_risk": 0.81,
    "model_version": "decision-layer@0.1.0"
  },
  "open_questions": [
    "Were other transactions made after 2026-06-09?"
  ],
  "transcript_ref": "CONV-...",
  "policy_version": "0.1.0"
}
```

Field rules:

- `verified_facts` MUST each have `source` and `record_id` ([DATA-04](#12-data-handling-and-fairness)).
- `actions_taken` lists only actions that were attempted, with their verification result. Failed actions are included.
- `open_questions` lists what the agent still needs to establish. It is empty only if nothing is pending.
- `model_signals` are informative. The agent must not treat them as decisions.

## 14. Responsibility split

| Responsibility | Component | Why |
|---|---|---|
| Understanding free text in `es` and `pt`, extracting candidate slot values, writing connecting sentences | Language model | Natural language varies too much for rules. |
| Reason-code classification, ambiguity detection, escalation-risk signal | Decision layer (learned, typed probabilities) | Bounded decisions with calibrated confidence are cheaper and easier to measure than free-text generation. |
| Gates, tiers, triggers, precedence, permissions, confirmations | Deterministic policy engine | Must be predictable, auditable, and impossible to override with a prompt. |
| Commitments to the customer | Versioned templates | The wording has operational and legal weight. |
| Dispute decisions, credit, compensation, fraud investigation | Human agents | Outside what the challenge authorizes and what the system should decide alone. |

## 15. Parameters

All parameters live in one versioned configuration file in the codebase; this table is the reference for their meaning. **Status** says how a value was set: *Fixed* values are design choices; *Provisional* values will be revisited after profiling the transactions data; *Calibrated* values are chosen on the validation split and never on the test split.

| Parameter | Value | Unit | Used by | Status | Rationale |
|---|---|---|---|---|---|
| `SESSION_MAX_AGE_MIN` | 60 | minutes | `GATE-02` | Fixed | Bounds the lifetime of an authentication. |
| `SESSION_IDLE_TIMEOUT_MIN` | 15 | minutes | `GATE-02` | Fixed | Common idle timeout for banking sessions. |
| `DISPUTE_WINDOW_DAYS` | 60 | calendar days | `GATE-08` | Fixed | Automated intake only for recent transactions. |
| `LATE_WINDOW_DAYS` | 120 | calendar days | `GATE-08`, `ESC-07` | Fixed | Late filings still reach a human who can judge exceptions. |
| `DUPLICATE_WINDOW_HOURS` | 48 | hours | `GATE-10` | Provisional | Covers same-day and next-day reposting. |
| `PROVISIONAL_CREDIT_AUTO_MAX_USD` | 100 | USD | §6 | Provisional | Low-value disputes where review cost exceeds risk. |
| `AUTO_INTAKE_MAX_USD` | 1,000 | USD | §6, `ESC-01` | Provisional | Above this, a human reviews before a case exists. |
| `AGG_DISPUTED_30D_MAX_USD` | 2,000 | USD | `ESC-02` | Provisional | Limits exposure from many small automated disputes. |
| `REPEAT_DISPUTES_90D` | 3 | cases | `ESC-02` | Provisional | Repeated disputes need a human view of the pattern. |
| `UNRECOGNIZED_BATCH_MAX` | 3 | transactions | `ESC-03` | Fixed | Several unrecognized charges at once suggest compromise. |
| `FRAUD_SCORE_ESCALATE` | 80 | score (0–100) | `ESC-04` | Provisional | To be calibrated against the `fraud_score` distribution. |
| `MAX_CLARIFICATION_TURNS` | 2 | turns per slot | §10, `ESC-09` | Fixed | Avoids looping on the same question. |
| `MAX_TOTAL_CLARIFICATIONS` | 4 | turns per conversation | §10, `ESC-09` | Fixed | Bounds customer effort before a human takes over. |
| `MAX_CANDIDATES_SHOWN` | 3 | transactions | §10 | Fixed | Keeps the choice readable and limits disclosure. |
| `TOOL_MAX_RETRIES` | 2 | retries | `ESC-10` | Fixed | Bounded retries with idempotency keys. |
| `INJECTION_STRIKES_MAX` | 2 | attempts | `ESC-13` | Fixed | One attempt can be accidental; two show intent. |
| `DECISION_CONFIDENCE_MIN` | TBD | probability | `ESC-11` | Calibrated | Chosen on the validation split to keep unsafe outcomes at or below the target. |
| `ESCALATION_RISK_THRESHOLD` | TBD | probability | `ESC-11` | Calibrated | Same method as above. |
| `RESOLUTION_TARGET_BUSINESS_DAYS` | 10 | business days | `COM-05` | Fixed | Synthetic service level shown to customers. |

`AUTH_MAX_ATTEMPTS` (`GATE-02`) is pending: its value is an open question, and it will be added to this table once decided.

**Business date.** Rules that depend on transaction age or on calendar windows (`GATE-08`, `ESC-02`, `ESC-07`, and the delivery date of `RC_NOT_RECEIVED`) use a business date, `as_of`, instead of the real clock. The supplied data ends on 2026-06-17, so with the real clock every transaction would fail `GATE-08`. The prototype sets `as_of` from configuration (`BUSINESS_DATE`). Session age and idle time (`GATE-02`) always use real time, because the identity service issues sessions now. The two clocks are a limitation of the prototype.

## 16. Deriving evaluation labels

The [data label validity spike](spikes/2026-09-25-data-label-validity.md) shows that the supplied escalation labels carry no learnable signal and that transcripts are templated. Evaluation labels therefore come from this policy:

1. Each team-generated test case is defined by a **scenario specification**: the customer and transaction records involved (from the supplied data or a labeled test fixture), the customer's true intent and slot values, and any special conditions (for example, an injection attempt or an expired session).
2. The **reference label** of a case is the outcome, triggered rules, and actions produced by running the deterministic policy engine on the scenario specification. It does not depend on the conversation text or on any model.
3. A sample of at least 10% of cases (minimum 50) is reviewed by a person against this document. Disagreements are resolved by fixing the specification or, if the policy is unclear, by amending the policy and bumping its version.
4. The held-out split is created before any prompt, threshold, or model tuning, and it is never used to set `Calibrated` parameters.

The full evaluation design, including case mix and metrics, will be documented separately.

## 17. Assumptions, limitations, and open questions

**Assumptions**

- The policy is synthetic. A real deployment would require review against the rules of each country's financial authorities (for example, CONDUSEF in Mexico, the SFC in Colombia, the BCRA in Argentina) and against card network dispute rules.
- The case store, identity service, and card-block tool are mocks with documented contracts.
- Business days follow a single calendar. Country-specific holidays are not modeled.
- Transactions are deduplicated by `transaction_id` when the data is loaded (about 2% of rows are duplicates). Otherwise a data-quality duplicate would fail "exactly one transaction" in `GATE-05` or be mistaken for a real `RC_DUPLICATE`.
- `products.product_type` values in the supplied data: `Cuenta Ahorro`, `Cuenta Corriente`, `Inversión`, `Préstamo Hipotecario`, `Préstamo Personal`, `Seguro`, `Tarjeta Crédito`, `Tarjeta Débito`.

**Limitations**

- The supplied data contains Spanish only. Portuguese behavior is evaluated with team-generated cases, and results are reported separately by language.
- Thresholds marked *Provisional* are design choices, not values derived from loss data.
- The supplied transcripts and complaint descriptions cannot be used to validate natural-language understanding (see the spike).

**Open questions**

| # | Question | Affects |
|---|---|---|
| 1 | *Partly resolved in 0.2.0:* `transaction_type` and `transaction_status` values in the data dictionary match §4 and `GATE-06`. Still open: how are bank fees represented (assumed `Adjustment`)? | §4, `RC_FEE` |
| 2 | What is the null rate of `transactions.amount_usd`? | §6 |
| 3 | What is the distribution of `transactions.fraud_score`, and does `is_fraud` carry signal? | `ESC-04` |
| 4 | Can `complaints` be linked to `transactions` through `affected_product_id` to seed realistic scenarios? | §16 |
| 5 | What is the value of `AUTH_MAX_ATTEMPTS`? | `GATE-02` |

## 18. Change log

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-09-25 | First draft. |
| 0.2.0 | 2026-09-28 | Two-channel evaluation of gates and triggers (§5). `AUTH_MAX_ATTEMPTS` for `GATE-02`, value pending. Distinct `transaction_id` for `RC_DUPLICATE`. Unknown USD amount treated as `T3` (§6). Null `fraud_score` does not fire `ESC-04`, and `is_fraud` is excluded from rules (§7). Conditions for offering `ACT-03`; confirmation requires a valid session (§8). Business date `as_of` (§15). Deduplication and product types (§17). Open question 1 partly resolved. |
