# Glossary

| Field | Value |
|---|---|
| Status | Draft |
| Version | 0.1.1 |
| Last updated | 2026-09-28 |
| Related | [Dispute policy](dispute-policy.md) |

Terms are listed alphabetically. When a term in another document has a specific meaning, it is defined here and used with that meaning everywhere.

| Term | Definition |
|---|---|
| **Abstention** | The system declines to act or to answer because the request is out of scope, not disputable, or unsafe. Abstention always includes an explanation to the customer and, where applicable, an offer to transfer to a human. Produces the `INFORM` or `REFUSE` outcome. |
| **Audit record** | The structured execution log of one conversation: rules evaluated, tools called, inputs and outputs, verification results, and outcome. Hidden model reasoning is never part of the audit record. |
| **Authenticated session** | A session issued by the identity service after a successful challenge (test OTP). Knowing a customer ID or document number alone does not create an authenticated session. |
| **Automated resolution** | For dispute intake, a case that ends with a correctly created dispute case (right transaction, reason code, tier, and required documentation) and a customer informed of next steps, with no human intervention. It does not mean the dispute was decided in the customer's favor. |
| **Case** | A dispute record created in the case store. Its lifecycle is described in the [case lifecycle diagram](diagrams/dispute-case-lifecycle.md). |
| **Clarification turn** | One system message asking the customer for a missing or ambiguous piece of information (a slot). |
| **Containment** | A conversation that ends without transfer to a human. Containment alone does not show that the problem was solved. |
| **Decision layer** | The learned component that returns typed decisions with probabilities (reason code, ambiguity, escalation risk). Its outputs are signals; deterministic rules have the final say. |
| **Deterministic policy engine** | Code that evaluates the rules in the [dispute policy](dispute-policy.md). It is the only component allowed to decide outcomes and authorize actions. |
| **Dispute** | A customer's claim that a posted transaction on their own product is incorrect or unauthorized. |
| **Handoff packet** | The structured JSON given to a human agent on escalation: request, verified facts, actions taken, evidence, triggered rules, and open questions. It does not contain the raw transcript. |
| **Outcome** | The single final result of a conversation: `RESOLVE`, `CLARIFY`, `INFORM`, `ESCALATE`, or `REFUSE`. Defined in [dispute policy §9](dispute-policy.md#9-outcomes-and-precedence). |
| **Provisional credit** | A temporary credit a bank may grant while a dispute is investigated. In this system it is only recorded as an eligibility flag for back-office review; the system never moves money. |
| **Reason code** | The category of a dispute, such as `RC_UNRECOGNIZED` or `RC_DUPLICATE`. Defined in [dispute policy §3](dispute-policy.md#3-reason-codes). |
| **Slot** | A required piece of information for a reason code, such as the transaction reference or the expected amount. |
| **Tier** | The automation level assigned to a dispute based on its USD-equivalent amount. Defined in [dispute policy §6](dispute-policy.md#6-automation-tiers). |
| **Unsafe outcome** | An unauthorized disclosure, an unauthorized action, or a materially incorrect outcome, such as creating a case for a transaction the customer does not own. |
| **USD equivalent** | A transaction amount in USD: `transactions.amount_usd` when supplied; the amount itself for USD transactions; otherwise the amount converted with the latest `daily_exchange_rates` rate dated on or before the transaction date, if that rate is at most `FX_MAX_STALENESS_DAYS` days old. With no such rate the USD equivalent is unknown, and the tier is treated as `T3`. The source is recorded in `transactions.amount_usd_source` ([dispute policy §6](dispute-policy.md#6-automation-tiers)). |
| **Verified fact** | A statement backed by a record read from a data source during the session, with its source table and record ID. Customer statements are not verified facts; they are recorded as claims. |
