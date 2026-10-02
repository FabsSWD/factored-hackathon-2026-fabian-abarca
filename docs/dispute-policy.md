# Dispute Intake Policy

| Field | Value |
|---|---|
| Status | Draft |
| Version | 0.4.8 |
| Last updated | 2026-10-01 |
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

Transaction types come from `transactions.transaction_type`. The values in the table below are the values observed in the supplied data, which match the data dictionary exactly. Figures and value sets in this policy always come from profiling the data, never from the dictionary (see [§17](#17-assumptions-limitations-and-open-questions)).

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
- **Adjustment** is treated as a bank-originated charge, which is how bank fees are assumed to appear. The data does not label fees, and every `Adjustment` has a positive amount with no field that gives its direction; see [§17](#17-assumptions-limitations-and-open-questions).

## 5. Gates

Gates `GATE-01` to `GATE-09` are evaluated in the order listed, then `GATE-11`, the record-dependent triggers, and `GATE-10` last (see the evaluation order below). Evaluation stops at the first gate that does not pass, and that gate determines the outcome. The same order appears in the [decision flow diagram](diagrams/dispute-decision-flow.md).

**Combining gates and escalation triggers.** Rules are evaluated in two channels, and [§9](#9-outcomes-and-precedence) precedence resolves among all candidate outcomes:

1. **Interrupts**: `ESC-03`, `ESC-05`, `ESC-06`, and `ESC-13` are evaluated on every turn, whatever the gate results, because they come from the conversation, not from the transaction record.
2. **Gates** `GATE-01` to `GATE-09` run in order and stop at the first that does not pass. `ESC-07`, `ESC-08`, `ESC-12`, and `ESC-14` are the outcomes of their gates. The one exception is `GATE-04`: it is evaluated per record and does not depend on `GATE-03`, so a record of another customer is refused even when the customer is not active (see the `GATE-04` row).
3. **`GATE-11`** (no duplicate case) runs next, before any slot is asked: a transaction that already has a case gets no questions.
4. **Record-dependent triggers**: `ESC-01`, `ESC-02`, and `ESC-04` are evaluated next, on the disputed transaction (for `RC_DUPLICATE`, the later charge of the pair). If one fires, the outcome is `ESCALATE` and the reason-specific slots still missing go to the handoff packet's `open_questions`. For `RC_DUPLICATE`, a twin the customer has not confirmed is only assumed: an existing case on it is not reported until the customer confirms that charge (the turn asks for `duplicate_ref` first), while an escalation on it still stands.
5. **`GATE-10`** (reason-specific slots and preconditions) runs last. When a record-dependent trigger has fired, `GATE-10` is still evaluated on the slots the customer already gave, but only its escalations count (for example `ESC-03` for shared credentials); its `CLARIFY` and `INFORM` outcomes are dropped, as the precedence of [§9](#9-outcomes-and-precedence) would drop them anyway. A dropped `INFORM` reason (for example, an expected amount that is not lower than the charge) is passed to the agent in the handoff's `open_questions` as context.

*Why this order.* The record-dependent triggers depend only on verified records, not on the reason-specific slots, so asking for a delivery date or a merchant contact before escalating a high-amount case would not change the outcome: it would only spend the customer's turns and the clarification limits of `ESC-09`. The agent receives the missing slots as open questions instead.

`ESC-09` replaces a `CLARIFY` once its limits are exceeded, `ESC-10` follows a failed action, and `ESC-11` is evaluated only when no hard rule decided the outcome, that is, when the candidate outcome is `CLARIFY` or `RESOLVE` (an `INFORM` or `REFUSE` is decided by a gate on verified records), as described in [§7](#7-mandatory-escalation-triggers).

*Rationale.* The combination rule defines the reference labels, so it decides what counts as a missing or an unnecessary transfer. For example, a pending transaction with `fraud_score` 90 ends in `INFORM` (nothing can be disputed yet, and `GATE-06` stops before `ESC-04`), while a pending transaction where the customer mentions a lawyer ends in `ESCALATE` (`ESC-06` is an interrupt). Interrupts express what the customer needs now; record-dependent triggers only make sense for a transaction that can be disputed.

| ID | Gate | Pass condition | If it does not pass |
|---|---|---|---|
| `GATE-01` | Supported language | Detected language is `es` or `pt`. | One clarification asking for the preferred language, then [ESC-12](#7-mandatory-escalation-triggers). |
| `GATE-02` | Authenticated session | Session was issued by the identity service, is younger than `SESSION_MAX_AGE_MIN`, and has been idle less than `SESSION_IDLE_TIMEOUT_MIN`. | The system MUST NOT read or disclose account data. It asks the customer to authenticate (a `CLARIFY` turn targeting authentication). An explicit refusal gives `INFORM` at once; exceeding `AUTH_MAX_ATTEMPTS` gives `INFORM` with an offer to transfer. See the authentication attempts rule below. |
| `GATE-03` | Customer status | `customers.customer_status = 'Active'`. It decides on the customer's own records. | [ESC-08](#7-mandatory-escalation-triggers). |
| `GATE-04` | Ownership | Every transaction or product the customer references has `customer_id` equal to the session customer. Enforced in the tool layer, which returns `access_denied` for other customers' records. Evaluated per record and independently of `GATE-03`. | `REFUSE` for that record, even when the customer is not active; the customer's own records are evaluated separately (an inactive customer's own record still gives `ESC-08`). The system MUST NOT confirm or deny that the record exists. The attempt is logged as a security event. |
| `GATE-05` | Transaction identified | Exactly one transaction of the session customer matches the reference given (see the matching rule below). | `CLARIFY` (see [§10](#10-required-information-and-clarification)): with 2 to `MAX_CANDIDATES_SHOWN` matches, or 1 to `MAX_CANDIDATES_SHOWN` found by a relaxed search, the candidates are listed; with more, the customer is asked for the missing detail that best narrows them; with none, the customer is told what was searched. |
| `GATE-06` | Transaction status | `transactions.transaction_status = 'Approved'`. | `Pending`: `INFORM` (wait until posted). `Declined`: `INFORM` (nothing was charged). `Reversed`: `INFORM` (already reversed). |
| `GATE-07` | Disputable combination | The transaction type and reason code are marked **A** in [§4](#4-disputable-transactions). | **H**: [ESC-14](#7-mandatory-escalation-triggers). **N**: `INFORM` with an offer to transfer. |
| `GATE-08` | Filing window | Transaction age at filing is at most `DISPUTE_WINDOW_DAYS`. The age is the number of calendar days between `BUSINESS_DATE` and the business day of the transaction (its `transaction_date` minus `BUSINESS_DAY_CUTOFF`); the same function serves `DISPUTE_WINDOW_DAYS` and `LATE_WINDOW_DAYS`. | Age up to `LATE_WINDOW_DAYS`: [ESC-07](#7-mandatory-escalation-triggers). Older: `INFORM` (outside the filing window) with an offer to transfer. |
| `GATE-09` | Product status | `products.product_status` is `Active` or `Blocked`. | [ESC-08](#7-mandatory-escalation-triggers). |
| `GATE-10` | Reason-specific preconditions | See the table below. | See the table below. |
| `GATE-11` | No duplicate case | No case exists for the same `transaction_id`, whatever its status (open or closed). A `Draft` case exists only inside a conversation and never counts. | `INFORM` with the existing case reference and status, and an offer to transfer. A dispute that was rejected or closed is never reopened automatically; reopening it is a human decision. A `Draft` status is never shown to the customer. |

**Authentication attempts (`GATE-02`).** Every turn in which the system asks the customer to authenticate and the customer does not end up authenticated counts as one attempt, including a wrong OTP. When the attempts exceed `AUTH_MAX_ATTEMPTS`, the outcome is `INFORM` with an offer to transfer. An explicit refusal to authenticate gives `INFORM` immediately. Authentication is not a slot of [§10](#10-required-information-and-clarification), so attempts do not count toward `MAX_CLARIFICATION_TURNS` or `MAX_TOTAL_CLARIFICATIONS`. Every failed OTP is recorded as a security event in the audit record.

**Values outside the data contract.** Every value a rule reads (transaction type and status, product and customer status) belongs to a set fixed when the data is loaded. A value outside it stops the evaluation with an error instead of being guessed. The system then answers safely: a handoff with the tool-failure notice and an audit event, never an error page with technical details.

**Transaction matching (`GATE-05`).** A `transaction_id` matches only if the customer gave it, or picked it among candidates shown to them, and it is consistent with any date, amount, or merchant they also gave; otherwise the reference is ambiguous. Without an ID, the details are compared with the customer's transactions within `LATE_WINDOW_DAYS`:

- Date: ±1 business day. A period instead of one day ("entre el 15 y el 19 de junio", "a mediados de junio", "la semana pasada") matches the business days in it, ±1 day; a period of more than 31 days does not narrow the search.
- Amount: exact, in the transaction currency. When the customer qualifies it ("como de", "unos", "más o menos", "cerca de", "uns"), it matches within a tolerance: `AMOUNT_TOLERANCE_PCT` of the amount given or `AMOUNT_TOLERANCE_USD` converted to the transaction currency, whichever is larger.
- Merchant: case- and accent-insensitive and by words, ignoring generic words ("restaurante", "tienda", "loja", "el", "la", ...): the customer's words are all in the merchant name, or the other way round. "el buen sabor" matches "Restaurante El Buen Sabor". A merchant named only with generic words is a kind of business: "un restaurante", "una farmacia" match the transaction's `merchant_category` through the word list of `config/merchant_categories.yaml`; generic words with no category there ("una tienda", "uma loja") are no detail at all, and the search uses the others.

Details that resolve to exactly one transaction identify it. Details that resolve to none are relaxed in steps: the amount with tolerance, then without the amount, then without the amount and the date; a step needs at least one detail left. What a relaxed step finds, or what a qualified amount finds, is listed as candidates even when there is only one: it is identified only when the customer picks it. With more than `MAX_CANDIDATES_SHOWN` matches, the customer is asked for the detail they did not give that best narrows the matches (merchant, date, amount on a tie). When nothing is found, the customer is told what was searched and asked for a detail they did not give (merchant, date, amount, in that order), or for the merchant as it appears on the statement when they gave all three. The `COM-03` confirmation protects against a wrong match.

Reason-specific preconditions (`GATE-10`):

| Reason code | Pass condition | If it does not pass |
|---|---|---|
| `RC_UNRECOGNIZED` | Slots `card_in_possession` and `shared_credentials` are answered. `shared_credentials = no`. `card_in_possession = no` (a lost or stolen card) passes; the card block is offered under `ACT-03`. | `shared_credentials = yes`: [ESC-03](#7-mandatory-escalation-triggers). |
| `RC_DUPLICATE` | A second transaction with a different `transaction_id` exists with the same `product_id`, `merchant_name`, `amount`, and `currency`, status `Approved`, within `DUPLICATE_WINDOW_HOURS`. The earlier one is treated as legitimate and the later one is disputed: if the customer names the earlier charge, the later one becomes the disputed charge and is the one shown for confirmation. | `CLARIFY` exactly once per conversation whether the customer means another reason (recorded with its own indicator, not with the `reason_code` clarification count). If it remains unresolved: [ESC-09](#7-mandatory-escalation-triggers). The question is an ordinary clarification, so the §10 limits also apply to it. |
| `RC_INCORRECT_AMOUNT` | `expected_amount` is given in the transaction currency and is lower than the posted `amount`. | `expected_amount >= amount`: `INFORM` (the posted amount does not exceed what was agreed). |
| `RC_NOT_RECEIVED` | `expected_delivery_date` is in the past (before `BUSINESS_DATE`; a delivery due on `BUSINESS_DATE` itself is not yet reached) and `merchant_contacted = yes`. | Delivery date not reached: `INFORM` (wait until the date). Merchant not contacted: `INFORM` with guidance to contact the merchant first. |
| `RC_FEE` | The charged fee is identified: the resolved transaction is an `Adjustment`, which is a charge (see the assumption in [§17](#17-assumptions-limitations-and-open-questions)). | `CLARIFY` for the fee reference (`fee_ref`). |

## 6. Automation tiers

Tiers use the USD-equivalent amount of the disputed transaction (see [Glossary](glossary.md)).

| Tier | USD-equivalent amount | Case creation | Provisional credit eligibility flag |
|---|---|---|---|
| `T1` | Up to `PROVISIONAL_CREDIT_AUTO_MAX_USD` | Automated (`ACT-02`) | `eligible` |
| `T2` | Above `PROVISIONAL_CREDIT_AUTO_MAX_USD`, up to `AUTO_INTAKE_MAX_USD` | Automated (`ACT-02`) | `requires_review` |
| `T3` | Above `AUTO_INTAKE_MAX_USD` | Not automated. [ESC-01](#7-mandatory-escalation-triggers) with a draft case in the handoff packet. | Decided by a human |

The flag is a recommendation to the back office. The system never applies credit (see [ACT-04](#8-actions-and-confirmations)).

Amounts are compared with `>` and `<=` exactly as written. An amount equal to a threshold belongs to the lower tier.

**USD equivalent.** It is established when the data is loaded, and the source is recorded per row in `transactions.amount_usd_source`:

| Source | Rule |
|---|---|
| `source` | `transactions.amount_usd` is supplied. |
| `identity` | The transaction is in USD, so the amount is already in USD. |
| `fx_rate` | The latest `daily_exchange_rates` rate to USD dated on or before the transaction date (as-of lookup), if it is at most `FX_MAX_STALENESS_DAYS` days old. The date of the rate used is stored. |
| `missing` | No rate within that margin. |

When the source is `missing`, the tier is unknown and is treated as `T3`, so [ESC-01](#7-mandatory-escalation-triggers) fires. *Rationale:* the tier sets how much autonomy the system has, so an unknown amount is handled conservatively. The as-of lookup exists because test cases concentrate at the end of the data (the 60-day window), where the latest day may not have a rate yet; recent transactions should not escalate because of how the dataset was cut.

In the supplied data, `amount_usd` is null for every USD transaction (2,437,979 rows). That null is structural, not random, so it is resolved by `identity`. Loading resolves every other row with `source` (1,887,552) or `fx_rate` (99,477, of which 35 used the rate of the previous day), and leaves none `missing` (data profile of 2026-09-28, dataset 2023-06-17 to 2026-06-17).

## 7. Mandatory escalation triggers

Hard triggers (`ESC-01` to `ESC-10`, `ESC-12` to `ESC-14`) always cause `ESCALATE`, regardless of model outputs. The soft trigger `ESC-11` is evaluated only if no hard rule has already decided the outcome. When each trigger is evaluated is defined in [§5](#5-gates).

**Several triggers at once.** The packet goes to the most restrictive queue among the triggered rules (security review > fraud > disputes), with high priority if any triggered rule asks for it, and lists every triggered rule.

`transactions.is_fraud` MUST NOT be used by any rule: it is a label the bank assigns afterwards, not a signal available when the dispute is filed. A null `fraud_score` does not escalate, because the case is investigated by a person anyway and escalating it would inflate unnecessary transfers. The null rate is 20.0% (885,157 of 4,425,008 rows) and is spread evenly across `transaction_type`, `channel`, `is_fraud`, `transaction_status`, currency, and year (19.8% to 20.6% in every group), so ignoring it does not bias any group (data profile of 2026-09-28, dataset 2023-06-17 to 2026-06-17).

| ID | Trigger | Condition | Route |
|---|---|---|---|
| `ESC-01` | High amount | USD-equivalent amount `> AUTO_INTAKE_MAX_USD` (tier `T3`). | Disputes queue |
| `ESC-02` | Dispute velocity | Including the current dispute, the customer's disputed total in the last 30 days exceeds `AGG_DISPUTED_30D_MAX_USD`, **or** the customer has at least `REPEAT_DISPUTES_90D` cases in the last 90 days. Both windows end at `as_of` and count cases by their business creation date (see [§15](#15-parameters)). The 30-day total includes the current dispute; the 90-day count includes only previous cases. `Draft` cases never count. | Disputes queue |
| `ESC-03` | Account takeover indicators | The customer reports an unknown login or device, a credential change they did not make, a lost or stolen phone, or sharing credentials or codes with a third party; **or** raises at least `UNRECOGNIZED_BATCH_MAX` unrecognized transactions in one conversation. | Fraud queue, high priority. A card block (`ACT-03`) is offered first. |
| `ESC-04` | Fraud score | `transactions.fraud_score >= FRAUD_SCORE_ESCALATE` on the disputed transaction. A null `fraud_score` does not fire this trigger; its absence is recorded in the audit record and, if there is a handoff, in `open_questions`. | Fraud queue |
| `ESC-05` | Human requested | The customer asks for a human at any point. | Disputes queue. Honored immediately; the system MUST NOT try to retain the customer. |
| `ESC-06` | Legal, regulatory, or vulnerability signals | The customer mentions legal action, a lawyer, a regulator complaint, or the media, or describes serious hardship or distress caused by the charge. | Disputes queue, high priority |
| `ESC-07` | Late filing | Transaction age is above `DISPUTE_WINDOW_DAYS` and at most `LATE_WINDOW_DAYS`. | Disputes queue |
| `ESC-08` | Ineligible status | Customer status is not `Active`, or product status is not `Active` or `Blocked`. | Disputes queue |
| `ESC-09` | Unresolved ambiguity | A slot is still missing or ambiguous after `MAX_CLARIFICATION_TURNS` for that slot, the conversation exceeds `MAX_TOTAL_CLARIFICATIONS`, or the customer's claim contradicts verified facts and clarification does not resolve it. | Disputes queue |
| `ESC-10` | Tool failure | A write action fails after `TOOL_MAX_RETRIES`, or its read-back verification does not match. | Disputes queue, with the failure in the handoff packet |
| `ESC-11` | Model uncertainty (soft) | The decision layer's top reason-code probability is below `DECISION_CONFIDENCE_MIN`, or its escalation-risk probability is at least `ESCALATION_RISK_THRESHOLD`. The thresholds apply only to signals from Kev; the fallback derived from the extraction (0/1, uncalibrated) never fires this trigger. While the thresholds are null, it fires only when the signals are unavailable. It is evaluated only once the session is authenticated, and only when the candidate outcome is `CLARIFY` or `RESOLVE`. | Disputes queue |
| `ESC-12` | Unsupported language | The language is not `es` or `pt`, or remains ambiguous after one clarification. | Disputes queue |
| `ESC-13` | Manipulation attempts | The conversation contains at least `INJECTION_STRIKES_MAX` attempts to override instructions, impersonate staff, or request other customers' data. The first attempt is ignored and logged; automation ends at the threshold. Attempts are counted per conversation from its first message, before authentication, and re-authenticating does not reset them. Impersonation means a first-person claim made to the assistant; a customer reporting what a caller or message claimed (vishing) is an account-takeover signal under `ESC-03`, not an attempt. | Security review queue |
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
- The reason is `RC_UNRECOGNIZED`, or `ESC-03` fired. When `ESC-03` fires without an identified transaction, the block is offered only if the customer has exactly one active card; with several or none there is no offer, and the handoff packet says so in `open_questions`.
- The product involved is a card: `products.product_type` is a card type (`Tarjeta Crédito` or `Tarjeta Débito` in the supplied data). The condition depends on the product, not on the transaction type: a purchase charged to a checking account has no card to block.
- `products.product_status = 'Active'`. If the card is already `Blocked`, the system says so and offers nothing. If it is `Closed` or `Suspended`, there is no offer and `GATE-09` escalates under `ESC-08`.

An `RC_UNRECOGNIZED` dispute on a transfer or payment (**H** in [§4](#4-disputable-transactions), routed by `ESC-14`) normally concerns an account, not a card, so there is no `ACT-03`; the handoff packet says so in `open_questions`.

**Explicit confirmation** means an affirmative reply to the templated summary in the same conversation ("sí, confirmo" in Spanish, "sim, confirmo" in Portuguese, or an equivalent). A hedged or unclear reply ("creo que sí", "acho que sim") is not confirmation: the system asks once more, and that question counts as a clarification turn.

The reply to the summary is classified as confirmed, hedged, declined, or withdrawn:

- **Confirmed**: `ACT-02` is authorized.
- **Hedged**: the system asks once more (a clarification turn).
- **Declined** ("no, eso no es correcto"): the system asks which detail is wrong and offers to drop the dispute (a clarification turn), then shows the summary again.
- **Withdrawn** ("no, ya no quiero", "deixa pra lá"): the conversation ends without a case, with an offer to transfer (`INFORM`).

The same classification answers the duplicate question of `RC_DUPLICATE`: a yes fills `duplicate_ref` with the candidate the rule found; a no leads to the clarification of `GATE-10`.

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
- Limits: `MAX_CLARIFICATION_TURNS` per slot and `MAX_TOTAL_CLARIFICATIONS` per conversation. Exceeding either fires [ESC-09](#7-mandatory-escalation-triggers). A clarification counts toward the limits only when the customer's answer brings no new information (no new or changed detail and no answer to the question asked); a customer who keeps adding details is not escalated for it.
- The same clarification text is never sent two turns in a row for the same slot: the second time, a variant names what is already known.

## 11. Customer communication

| ID | Rule |
|---|---|
| `COM-01` | The system replies in the language of the customer's latest message (`es` or `pt`). If the language is mixed or unclear, it asks once which language the customer prefers. |
| `COM-02` | Commitments MUST come from versioned templates, never from free generation: case references, target times, next steps, eligibility wording, refusals, and escalation notices. The language model MAY write connecting sentences around them. |
| `COM-03` | Before `ACT-02` or `ACT-03`, the system presents a templated summary and asks for explicit confirmation. **One confirmation covers exactly one action.** `ACT-03` is confirmed on its own, with its consequence (the card stops working for all purchases and payments, including automatic ones). `ACT-02` is confirmed with a summary of the transaction date, masked product, merchant, amount, and reason. When both apply, the card block is confirmed first, because it is protective and urgent; declining it does not affect the dispute, and the flow continues to the `ACT-02` summary. The `ACT-03` offer names the charge it is about; if the customer says that charge is not the one they mean, the transaction is identified again (`GATE-05`) and nothing is blocked. An offer that gets no clear answer after one repetition is not executed and the customer is told so; the offer stays available until the case is created, so a later request to block the card is honored after its own confirmation. A question the customer asks instead of answering is not an unclear answer. |
| `COM-04` | The system MUST NOT say an action happened unless its verification passed. |
| `COM-05` | The system MUST NOT promise a refund, credit, or result. It states that the case will be investigated within `RESOLUTION_TARGET_BUSINESS_DAYS` business days. |
| `COM-06` | Product numbers are masked to the last four digits. |
| `COM-07` | Explanations to the customer describe the reason in plain language ("the transaction is still pending"). Rule identifiers appear only in the audit record. |
| `COM-08` | Amounts are shown with the ISO currency code first and formatted for the customer's locale (conversation language plus customer country): `es-MX` writes `USD 1,250.00`; `es-CO`, `es-AR`, and `pt-BR` write `USD 1.250,00`. The `$` sign is never used, because it is ambiguous between pesos and dollars. |

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
| `DATA-02` | **Prohibited decision inputs.** These fields MUST NOT influence any outcome, threshold, or model feature: `segment`, `credit_score`, `estimated_monthly_income`, `gender`, `date_of_birth` (or age), `detected_accent`, `occupation`, `marital_status`, `education_level`, and `country` (except for currency conversion, language defaults, and number formatting under `COM-08`). They MAY be used only to report outcomes by group for fairness analysis. |
| `DATA-03` | **Claims are not facts.** Customer statements are recorded as claims. Only records read from a data source are verified facts. |
| `DATA-04` | **Provenance.** Every verified fact in the audit record and handoff packet includes its source table and record ID. |
| `DATA-05` | **Synthetic data only.** The prototype uses the supplied synthetic dataset and team-generated data. No real customer data is used, and no customer records are sent to external model providers beyond what `DATA-01` allows. |
| `DATA-06` | **Storage minimization and retention.** Core Banking stores only the columns the system uses plus the `DATA-02` attributes needed for fairness reporting. Names, emails, phone numbers, addresses, city, state, postal codes, balances, and coordinates are not loaded. `date_of_birth` is replaced by an age band computed at the business date. `document_number` is stored only as an HMAC with a secret key (`DOCUMENT_HASH_KEY`), so the identity service can match a document without keeping it. Product numbers keep only their last four digits. |

## 13. Handoff packet

Every `ESCALATE` outcome produces one handoff packet. It gives the human agent what they need to continue without rereading the conversation. The raw transcript is not included; a reference to it is.

```json
{
  "handoff_id": "HO-20261001-000123",
  "created_at": "2026-10-01T15:04:00Z",
  "business_date": "2026-06-17",
  "language": "pt",
  "queue": "fraud",
  "priority": "high",
  "customer_ref": "CUS-78b3d06b5a3fdeb3",
  "auth": { "status": "authenticated", "method": "test_otp", "session_age_min": 6 },
  "request_summary": "Unrecognized charge of USD 50.00 at Cafe Sintetico (2026-06-10); account takeover indicators (ESC-03); card ****4821 blocked.",
  "reason_code": "RC_UNRECOGNIZED",
  "triggered_rules": ["ESC-03"],
  "escalation_reasons": [
    {
      "rule_id": "ESC-03",
      "description": "Possible account takeover: the customer reported a takeover indicator or several unrecognized charges.",
      "evidence": [
        {
          "kind": "flag",
          "name": "account_takeover_reported",
          "value": "true",
          "origin": "customer statement (LLM extraction)",
          "source": null,
          "record_id": null,
          "claims": ["O celular foi roubado no dia 9 de junho"]
        }
      ]
    }
  ],
  "verified_facts": [
    {
      "fact": "Purchase of USD 50.00 at Cafe Sintetico on 2026-06-10, status Approved, 7 days before the business date",
      "source": "transactions",
      "record_id": "TRX-..."
    },
    {
      "fact": "Card ending 4821 is Blocked (blocked by ACT-03 at 2026-10-01 15:03 UTC)",
      "source": "products",
      "record_id": "PRD-..."
    }
  ],
  "customer_claims": [
    "Não fiz essa compra",
    "O celular foi roubado no dia 9 de junho"
  ],
  "collected_slots": [
    { "name": "transaction_ref", "value": "candidate 2 of the list shown", "turn_index": 0, "verified": false },
    { "name": "reason_code", "value": "RC_UNRECOGNIZED", "turn_index": 0, "verified": false },
    { "name": "card_in_possession", "value": "yes", "turn_index": 1, "verified": false },
    { "name": "shared_credentials", "value": "no", "turn_index": 1, "verified": false }
  ],
  "actions_taken": [
    {
      "action": "ACT-03",
      "result": "success",
      "verified": true,
      "detail": "Card ending 4821 blocked",
      "at": "2026-10-01T15:03:20Z"
    }
  ],
  "draft_case": {
    "transaction_ref": "TRX-...",
    "amount_usd": 50.00,
    "tier": "T1",
    "provisional_credit_flag": "eligible"
  },
  "model_signals": {
    "source": "kev",
    "reason_code_probs": {
      "RC_UNRECOGNIZED": 0.94,
      "RC_DUPLICATE": 0.03,
      "RC_INCORRECT_AMOUNT": 0.01,
      "RC_NOT_RECEIVED": 0.01,
      "RC_FEE": 0.0
    },
    "reason_code_other": 0.01,
    "escalation_risk": 0.58,
    "model_version": "jaredpalmer/kev-0.8b@2026-09-24",
    "model_info": { "run": "jaredpalmer/kev-0.8b", "release_date": "2026-09-24" },
    "calibrated": false
  },
  "open_questions": [
    "Were other charges made after the phone was stolen?"
  ],
  "transcript_ref": "CONV-...",
  "policy_version": "0.4.3"
}
```

Field rules:

- `request_summary` and `escalation_reasons` are built from templates and verified data. Each triggered rule has one reason with its evidence: the slot, flag, counter, record, signal, or tool result that made it fire, with its origin. Evidence is copied from the inputs, never generated.
- `verified_facts` MUST each have `source` and `record_id` ([DATA-04](#12-data-handling-and-fairness)). Facts describe the records after the actions of the turn: a card blocked by `ACT-03` is reported as blocked. The customer record is identified by `customer_ref`, never by the customer ID.
- `business_date` is the business clock ([§15](#15-parameters)). Transaction ages in the facts are counted against it, with the same function as `GATE-08`, never against `created_at`.
- `customer_claims` are only what the customer said, in the conversation language. The system never writes a claim for the customer. The evidence of a slot or flag lists, in `claims`, the customer's own words behind it (for example, the stolen phone behind `account_takeover_reported`). Everything the system writes (summary, reasons, facts, questions) is in English, the language of the agent console.
- `collected_slots` are the slots the customer already gave, with the turn that set them, always unverified claims (DATA-03); `confirmation` is never one of them. Their values, like the customer claims, pass through the same masking as the audit trail. For `transaction_ref` the value is what the customer said (date, amount, merchant, or the candidate picked from a list), never the ID the engine resolved, which is in `verified_facts`; an ID appears only if the customer typed it. Likewise `duplicate_ref` says that the customer confirmed the duplicate charge the system found, whose record is in `verified_facts`.
- `open_questions` include every slot that is still missing: the transaction reference, the reason, and the reason-specific slots ([§10](#10-required-information-and-clarification), `GATE-10`), with the same table the policy engine uses to ask for them. A collected slot is never asked again.
- `actions_taken` records when each action finished. `model_signals` names its `source` (`kev` or `llm_fallback`), the serving details of Kev, and `calibrated`, which stays false until the `ESC-11` thresholds are calibrated.
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
| `AUTH_MAX_ATTEMPTS` | 3 | attempts | `GATE-02` | Fixed | Usual OTP lockout threshold: tolerates a typo without allowing brute force. |
| `DISPUTE_WINDOW_DAYS` | 60 | calendar days | `GATE-08` | Fixed | Automated intake only for recent transactions. |
| `LATE_WINDOW_DAYS` | 120 | calendar days | `GATE-08`, `ESC-07` | Fixed | Late filings still reach a human who can judge exceptions. |
| `DUPLICATE_WINDOW_HOURS` | 48 | hours | `GATE-10` | Fixed | Covers same-day and next-day reposting. No data contradicts it. |
| `FX_MAX_STALENESS_DAYS` | 3 | calendar days | §6 | Fixed | An as-of rate a few days old is close enough for a tier; older rates are not trusted. |
| `PROVISIONAL_CREDIT_AUTO_MAX_USD` | 100 | USD | §6 | Fixed | Low-value disputes where review cost exceeds risk. 19% of purchases, 17% of withdrawals and 9% of adjustments fall in `T1`. |
| `AUTO_INTAKE_MAX_USD` | 1,000 | USD | §6, `ESC-01` | Fixed | Above this, a human reviews before a case exists. Purchases, withdrawals and adjustments almost never exceed it in the data (0, 0 and 16 rows). |
| `AGG_DISPUTED_30D_MAX_USD` | 2,000 | USD | `ESC-02` | Fixed | Limits exposure from many small automated disputes: two `T2` disputes near the maximum already exceed it. |
| `REPEAT_DISPUTES_90D` | 3 | cases | `ESC-02` | Fixed | Repeated disputes need a human view of the pattern; three previous cases in a quarter is already unusual. |
| `UNRECOGNIZED_BATCH_MAX` | 3 | transactions | `ESC-03` | Fixed | Several unrecognized charges at once suggest compromise. |
| `FRAUD_SCORE_ESCALATE` | 35 | score (0–100) | `ESC-04` | Fixed | Calibrated against `is_fraud` on the full Core Banking table: no legitimate transaction scores above 30.00, so 35 catches 2,238 of 4,316 frauds (51.9%) with no false positives, against 673 (15.6%) with 80. The step at 30.00 is an artifact of the synthetic generator; 35 leaves a margin over it instead of fitting it, and production would recalibrate it. Evidence: `reports/policy_calibration_data.json`. |
| `MAX_CLARIFICATION_TURNS` | 2 | turns per slot | §10, `ESC-09` | Fixed | Avoids looping on the same question. |
| `MAX_TOTAL_CLARIFICATIONS` | 4 | turns per conversation | §10, `ESC-09` | Fixed | Bounds customer effort before a human takes over. |
| `MAX_CANDIDATES_SHOWN` | 3 | transactions | §10 | Fixed | Keeps the choice readable and limits disclosure. |
| `AMOUNT_TOLERANCE_PCT` | 10 | percent | §5 | Fixed | A customer who says "como de 40" for a charge of 38.50 still finds it. |
| `AMOUNT_TOLERANCE_USD` | 5 | USD | §5 | Fixed | The same for small amounts, where 10% is less than a rounding. |
| `TOOL_MAX_RETRIES` | 2 | retries | `ESC-10` | Fixed | Bounded retries with idempotency keys. |
| `INJECTION_STRIKES_MAX` | 2 | attempts | `ESC-13` | Fixed | One attempt can be accidental; two show intent. |
| `DECISION_CONFIDENCE_MIN` | TBD | probability | `ESC-11` | Calibrated | Chosen on the validation split by the calibration objective below. |
| `ESCALATION_RISK_THRESHOLD` | TBD | probability | `ESC-11` | Calibrated | Same method as above. |
| `RESOLUTION_TARGET_BUSINESS_DAYS` | 10 | business days | `COM-05` | Fixed | Synthetic service level shown to customers. |

**Calibration objective (`ESC-11`).** Kev's answers are first rescaled with one temperature per question, fitted by minimizing the log loss on the validation split. Then, among the thresholds that leave zero improper `RESOLVE` outcomes on the validation split, the one with the fewest unnecessary escalations is chosen: Kev only adds escalations, so its role is to catch what the rules let through without inflating transfers. If no threshold qualifies, or the validation split has fewer than 30 cases per relevant class, the thresholds stay null (the signals do not influence outcomes) and the report says so. The test split is never used (§16).

**Windows.** Every window in this policy is inclusive and uses one convention: a value is inside when `end - start <= length`, so a value exactly at the window length is inside. It applies to `GATE-08` (60 and 120 days between the transaction's business day and `BUSINESS_DATE`), the `GATE-05` matching pool (`LATE_WINDOW_DAYS`), `ESC-02` (30 and 90 days between a case's `business_created_at` and `as_of`), and `RC_DUPLICATE` (`DUPLICATE_WINDOW_HOURS` between the two charges). The engine implements it in a single function.

**Business date.** Rules that depend on transaction age or on calendar windows (`GATE-08`, `ESC-02`, `ESC-07`, and the delivery date of `RC_NOT_RECEIVED`) use a business date instead of the real clock. The supplied data ends on 2026-06-17, so with the real clock every transaction would fail `GATE-08`. Session age and idle time (`GATE-02`) always use real time, because the identity service issues sessions now. The two clocks are a limitation of the prototype.

- In the data, the business day of a partition `process_date = D` runs from D at 06:00 to D+1 at 05:59: 25% of transactions (1,106,307) have a `transaction_date` on the calendar day after their `process_date`, and none has one before it.
- The configuration sets `BUSINESS_DATE` (2026-06-17, the last partition) and `BUSINESS_DAY_CUTOFF` (06:00). The engine uses `as_of = BUSINESS_DATE + 1 day at BUSINESS_DAY_CUTOFF`.
- Loading fails if any row has `process_date` after `BUSINESS_DATE`. It does not compare `transaction_date`, because rows of the last business day legitimately fall on the next calendar day.
- Dataset timestamps are naive: local time without a time zone, although the data covers three countries. `as_of` is naive too, and the two are compared as they are.
- Every rule about transactions or cases uses the business clock. Only the session (`GATE-02`) and technical times (retries, latencies) use real time.
- A case stores two timestamps: `created_at` (real time, for audit) and `business_created_at` (the `as_of` when it was created). `ESC-02` counts cases by `business_created_at`. Evaluation scenarios may seed earlier cases with an explicit business date.

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
- Transactions are deduplicated by `transaction_id` across partitions when the data is loaded. The supplied data has no duplicates, by key or by full row, in `transactions`, `customers`, or `products`; deduplication stays as a safeguard, because a data-quality duplicate would fail "exactly one transaction" in `GATE-05` or be mistaken for a real `RC_DUPLICATE`.
- `products.product_type` values in the supplied data: `Cuenta Ahorro`, `Cuenta Corriente`, `Inversión`, `Préstamo Hipotecario`, `Préstamo Personal`, `Seguro`, `Tarjeta Crédito`, `Tarjeta Débito`.
- Every `Adjustment` is a bank-originated charge that `RC_FEE` can dispute (open question 1, closed with this assumption).
- **Data profile.** Figures in this policy come from profiling the supplied data on 2026-09-28: 1,097 daily partitions from 2023-06-17 to 2026-06-17, 4,425,008 transactions, 400,000 products, and 150,000 customers. Values in the data are in Spanish while the data dictionary is in English; the policy and the code use the values observed in the data.

**Limitations**

- The supplied data contains Spanish only. Portuguese behavior is evaluated with team-generated cases, and results are reported separately by language.
- The amount and velocity thresholds are design choices checked against the data profile, not values derived from loss data.
- The supplied transcripts and complaint descriptions cannot be used to validate natural-language understanding (see the spike).
- 44% of frauds cannot be told apart by `fraud_score` (a low score like legitimate transactions, or a null score). `ESC-04` does not cover them; they are covered by the `RC_UNRECOGNIZED` flow and the card block offer.
- `FRAUD_SCORE_ESCALATE` was calibrated with `is_fraud` on the full Core Banking table, not on the evaluation set of M17. `is_fraud` is never read at run time.
- For purchases, withdrawals and adjustments the real data has almost no `T3` amounts, so `ESC-01` almost never fires on real data. Evaluation (M17) and the demo seed synthetic `T3` scenarios, and scenarios with `fraud_score` above 35 for `ESC-04`.
- Fees cannot be told apart from other adjustments. All 132,118 `Adjustment` rows have a positive amount, no merchant, no category, and no field that gives their direction, and they appear only on loans, investments, and insurance (`Préstamo Personal`, `Préstamo Hipotecario`, `Inversión`, `Seguro`), never on accounts or cards. If some adjustments were credits in the customer's favor, `RC_FEE` would wrongly treat them as disputable charges.
- Timestamps carry no time zone, so transaction ages across Mexico, Colombia, and Argentina are compared on one naive clock.
- The handoff queue is simulated: the acknowledgement of `ACT-05` is the packet written to the handoff store and read back unchanged. There is no external queue.
- Card blocks (`ACT-03`) are recorded in a separate store because Core Banking is read-only for the application; reads report the card as `Blocked` while the block exists. Unblocking a card, including by an agent, is out of scope (`ACT-06`).
- The reported-speech exception of `ESC-13` can be evaded on purpose: a first-person staff claim preceded by a reporting verb ("me dijo…", "dizendo…") or placed in quotes is not counted. Not flagging fraud victims who report what a scammer said has priority, and claiming to be staff grants nothing, because `GATE-04` is enforced in the Tool Layer with the session's customer.
- `ESC-13` attempts are counted per conversation: a customer who starts a new conversation starts again at zero. Attempts per customer across conversations are monitored in the audit record, not used as a rule.
- The data dictionary lists `MXN`, but no transaction or product uses it: transactions are in `USD`, `COP`, and `ARS`, and every transaction of a customer in Mexico is in `USD` (2,216,431 rows). `daily_exchange_rates` includes `MXN` rates, which the system does not use.

**Open questions**

| # | Question | Affects |
|---|---|---|
| 1 | *Closed with an assumption in 0.3.0:* `transaction_type` and `transaction_status` values observed in the data match §4 and `GATE-06`. Bank fees are assumed to be `Adjustment` rows (see limitations). | §4, `RC_FEE` |
| 2 | *Resolved in 0.3.0:* `amount_usd` is null for every USD transaction (structural) and for about 5% of COP and ARS rows; all are resolved at load time (§6). | §6 |
| 3 | *Partly resolved in 0.3.0:* `fraud_score` has a median of 15.0, a 99th percentile of 29.7, and 673 rows at or above 80; 20% is null. The mean score is 49.5 when `is_fraud` is true and 15.0 when false. *Closed in 0.4.0:* `FRAUD_SCORE_ESCALATE` = 35, calibrated against `is_fraud` (§15). | `ESC-04` |
| 4 | Can `complaints` be linked to `transactions` through `affected_product_id` to seed realistic scenarios? | §16 |
| 5 | *Closed in 0.3.0:* `AUTH_MAX_ATTEMPTS` = 3 (§15). | `GATE-02` |

## 18. Change log

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-09-25 | First draft. |
| 0.2.0 | 2026-09-28 | Two-channel evaluation of gates and triggers (§5). `AUTH_MAX_ATTEMPTS` for `GATE-02`, value pending. Distinct `transaction_id` for `RC_DUPLICATE`. Unknown USD amount treated as `T3` (§6). Null `fraud_score` does not fire `ESC-04`, and `is_fraud` is excluded from rules (§7). Conditions for offering `ACT-03`; confirmation requires a valid session (§8). Business date `as_of` (§15). Deduplication and product types (§17). Open question 1 partly resolved. |
| 0.3.0 | 2026-09-28 | Figures from the data profile of 2026-09-28. `AUTH_MAX_ATTEMPTS` = 3 and how attempts are counted (§5). USD equivalent with `amount_usd_source` and an as-of exchange rate within `FX_MAX_STALENESS_DAYS` (§6). Null rate of `fraud_score` (§7). `DATA-06` storage minimization (§12). Business-day cutoff and naive timestamps (§15). No duplicates in the data; fees assumed to be `Adjustment` (§17). Open questions 1, 2, and 5 closed; 3 partly resolved. |
| 0.3.1 | 2026-09-28 | Clock rule: transactions and cases use the business clock; cases store `business_created_at`, which `ESC-02` counts (§7, §15). |
| 0.3.2 | 2026-09-28 | `COM-08` amount format by locale with the currency code first; `country` allowed for number formatting (`DATA-02`). No `MXN` in the data (§17). |
| 0.3.3 | 2026-09-28 | `COM-03`: one confirmation per action; the card block is confirmed first and separately, and declining it does not affect the dispute. |
| 0.3.4 | 2026-09-28 | `ESC-13`: counted per conversation; impersonation is a first-person claim, and reported vishing belongs to `ESC-03`. Limitation: a new conversation starts at zero (§17). |
| 0.3.5 | 2026-09-28 | §17: the reported-speech exception of `ESC-13` is evadable by design, and why that is acceptable. |
| 0.3.6 | 2026-09-28 | `GATE-11`: `Draft` cases do not count as open and their status is never shown to the customer. |
| 0.4.0 | 2026-10-01 | Rules for the Policy Engine: transaction matching (`GATE-05`), age computation (`GATE-08`), any non-draft case blocks (`GATE-11`), `RC_UNRECOGNIZED` with a lost card, `RC_FEE` identification, `ESC-02` counting, `ESC-11` only with Kev signals or unavailable signals, queue precedence for several triggers, `ACT-03` with exactly one active card, declined and withdrawn confirmations. Provisional parameters fixed with data; `FRAUD_SCORE_ESCALATE` = 35. Calibration objective for `ESC-11`. |
| 0.4.1 | 2026-10-01 | Policy Engine review: `ESC-11` only on `CLARIFY` or `RESOLVE` candidates (§5, §7). `RC_DUPLICATE` asks for another reason exactly once, with its own indicator; the later charge is disputed even if the customer names the earlier one. A delivery due on `BUSINESS_DATE` is not reached. One inclusive window convention (§15). `GATE-04` is per record and independent of `GATE-03`. Values outside the data contract stop evaluation and get a safe answer. |
| 0.4.2 | 2026-10-01 | Handoff packet (§13): `business_date`, `escalation_reasons` with evidence, templated `request_summary`, facts after actions, action times, model signal source, serving details and calibration state, and the language rule for system text and claims. Limitations: simulated handoff queue and card blocks without unblocking (§17). |
| 0.4.3 | 2026-10-01 | Handoff packet (§13): claims are only the customer's words, and evidence links to them; `collected_slots` with the turn that set them; open questions list the missing slots and never a collected one. |
| 0.4.4 | 2026-10-01 | Evaluation order (§5): `GATE-11` and the record-dependent triggers (`ESC-01`, `ESC-02`, `ESC-04`) before the reason-specific slots (`GATE-10`), so a case that will escalate is not asked questions that cannot change its outcome. |
| 0.4.5 | 2026-10-01 | Handoff packet (§13): a collected `duplicate_ref` says the customer confirmed the charge the system found, instead of showing its ID. |
| 0.4.6 | 2026-10-01 | `COM-03`: the card block offer names the charge ("that is not the one" identifies the transaction again); an offer without a clear answer is told and stays available until the case is created; a side question is not an unclear answer. |
| 0.4.7 | 2026-10-01 | `GATE-05`: merchant matched by words without generic words, approximate amounts with tolerance (`AMOUNT_TOLERANCE_PCT`, `AMOUNT_TOLERANCE_USD`), relaxed search in steps whose results the customer picks, the most useful detail asked for. §10: a clarification answered with new information does not count toward `ESC-09`; no clarification text twice in a row. |
| 0.4.8 | 2026-10-01 | `GATE-05`: a merchant named only with generic words is searched by category, or left out when it has none; periods of days match the business days in them. |
