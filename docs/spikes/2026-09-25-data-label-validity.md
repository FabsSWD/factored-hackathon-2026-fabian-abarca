# Spike: Data Label Validity and Workflow Selection

| Field | Value |
|---|---|
| Status | Complete |
| Version | 1.0.0 |
| Last updated | 2026-09-25 |
| Related | [Dispute policy](../dispute-policy.md), [Glossary](../glossary.md) |

## Questions

1. Which customer-service workflow does the supplied data support prioritizing?
2. Can the supplied labels (`was_escalated`, `was_resolved`, complaint `status`) and texts (transcripts, complaint descriptions) be used to train and evaluate a learned component?

## Summary

- **Workflow.** `Queja` contacts are 17.1% of volume but 23.1% of agent handle time and **41.2% of all unresolved contacts**, with the lowest first-contact resolution (43.6%). About 40% of complaints are unrecognized charges or undue fees. This supports **transaction-dispute intake**.
- **Escalation labels carry no signal.** `was_escalated` is about 10% in every slice of every feature we checked, and models reach AUC 0.50 on it. Complaint `status = 'Escalated'` is about 5% in every category and priority.
- **Texts are templated.** 171,321 transcripts contain 546 distinct texts and 42 distinct customer utterances, 100% with unfilled placeholders such as `{monto}`. Transcript content does not match the contact category. Complaint descriptions have 5 distinct values, each restating the category.
- **Decision.** Use the supplied data for workflow selection, as the system of record for grounding, and as the operational baseline. Train and evaluate the learned component on **team-generated** cases labeled by the deterministic [dispute policy](../dispute-policy.md#16-deriving-evaluation-labels).

## Data and method

**Data.** Supplied (synthetic) LATAM Bank dataset v1.0.0, converted from raw CSV to Parquet by our ingestion step (deduplicated by primary key, keeping the latest `last_updated` or `process_date`).

| Table | Rows after deduplication | Rows stated in the dataset summary | Date range |
|---|---|---|---|
| `call_center_interactions` | 686,296 | 800,000 | 2023-06-17 to 2026-06-18 |
| `call_transcripts` | 171,321 | 200,000 | 2023-06-17 to 2026-06-17 |
| `complaints` | 67,095 | 80,000 | 2023-06-17 to 2026-06-18 |
| `service_agents` | 1,200 | 1,200 | Not applicable |

All 1,097 daily partitions are present for each fact table. The gap to the stated row counts (about 14% in each fact table) is larger than the documented ~2% duplicate rate. Its cause is not established yet; see [open items](#open-items).

**Method.**

- Descriptive statistics by contact reason, channel, sentiment, accent, agent experience, hour, and month.
- Text diversity counts on transcripts and complaint descriptions.
- Signal tests with a **temporal split**: train on interactions before 2025-07-01, test on and after that date (115,888 and 55,433 transcripts respectively). Seed 42.
  - Text baseline: TF-IDF (unigrams and bigrams, `min_df=2`) with logistic regression.
  - Structured baseline: gradient-boosted trees (`HistGradientBoostingClassifier`) on duration, wait time, sentiment score, interaction type, channel, reason category, detected sentiment, and accent.

**Reproduction.** `python scripts/eda_findings.py` from the repository root writes every table below to `reports/eda/` and the model results to `reports/eda/label_signal.json`.

## Results

### Workload by contact reason

| Reason category | % volume | Avg. handle time (s) | % agent time | First-contact resolution | Unresolved | % of all unresolved |
|---|---|---|---|---|---|---|
| Transaccional | 35.0 | 221 | 24.0 | 91.5% | 20,385 | 12.7 |
| Producto | 22.0 | 266 | 18.2 | 89.6% | 15,650 | 9.8 |
| **Queja** | **17.1** | **435** | **23.1** | **43.6%** | **66,000** | **41.2** |
| Técnico | 15.0 | 360 | 16.8 | 69.9% | 30,940 | 19.3 |
| Comercial | 8.0 | 540 | 13.4 | 65.2% | 19,093 | 11.9 |
| Retención | 3.0 | 479 | 4.5 | 60.2% | 8,198 | 5.1 |

`contact_reason` and `reason_category` hold identical values, so there is no finer-grained reason in the interactions table.

### Complaints by subcategory

| Category | Subcategory | Cases | % | % escalated |
|---|---|---|---|---|
| Transactions | Cargo no reconocido | 12,297 | 18.3 | 5.0 |
| Fees | Cobro indebido | 12,194 | 18.2 | 5.2 |
| Technical | Problema con app | 12,128 | 18.1 | 4.9 |
| Branch | Atención en sucursal | 11,892 | 17.7 | 4.5 |
| Service | Calidad de servicio | 11,886 | 17.7 | 5.3 |
| (five categories) | null | 6,698 | 10.0 | 4.2–4.8 |

Transactions and Fees together, including their null subcategories, are 40.4% of complaints. The five subcategories are almost equally sized, which suggests a uniform generator rather than a real demand pattern; we treat the complaint mix as weak evidence and rely mainly on the interactions workload.

### Escalation label

| Slice | Escalation rate range |
|---|---|
| By reason category (6 values) | 9.8%–10.1% |
| By detected sentiment (5 values) | 9.90%–10.24% |
| By channel (6 values) | 9.91%–10.52% |
| By customer accent (3 values and null) | 9.85%–10.11% |
| By sentiment score quintile | 9.94%–10.01% |
| By `requires_followup` | 9.9%–10.0% |
| By `was_resolved` | 9.94%–9.97% |

Escalated and non-escalated interactions have the same mean duration (321 s vs 322 s), wait time (120 s), and sentiment score (−0.037).

### Model signal

| Target | Features | Metric | Result | Reference |
|---|---|---|---|---|
| `was_escalated` | Transcript text | ROC AUC | 0.492 | 0.5 = random |
| `was_escalated` | Structured | ROC AUC | 0.501 | 0.5 = random |
| `was_resolved` | Transcript text | ROC AUC | 0.495 | 0.5 = random |
| `was_resolved` | Structured | ROC AUC | 0.773 | 0.5 = random |
| `reason_category` | Transcript text | Accuracy | 0.351 | 0.351 = always predict the majority class |
| `reason_category` | Transcript text | Macro F1 | 0.087 | Not applicable |

`was_resolved` is predictable from structured fields, mostly through reason category and sentiment. It is the only label with signal, and it measures first-contact resolution, not escalation.

### Text diversity

| Source | Rows | Distinct values | Notes |
|---|---|---|---|
| `call_transcripts.full_text` | 171,321 | 546 | 100% contain unfilled placeholders (`{monto}`, `{moneda}`). |
| `call_transcripts.customer_text` | 171,321 | 42 | Two opening templates cover all rows (a credit card balance inquiry and a savings balance inquiry). |
| `call_transcripts.detected_intents` | 171,321 | 1 | `consulta_general` or null. |
| `call_transcripts.main_topics` | 171,321 | 6 | Always equal to the interaction's `reason_category` (label leakage). |
| `complaints.description` | 67,095 | 5 | `Queja relacionada con {category}`. |

Example: an interaction labeled `Queja` whose transcript is a savings balance inquiry.

### Other observations

- Call volume is flat across the 24 hours of the day (about 28,300 to 29,100 contacts per hour) and across months, with no seasonality.
- Agent experience level has no effect on resolution (76.6%–77.0%).
- Matching the customer's accent has no effect on resolution (76.59% vs 76.68%).
- `complaints.origin_interaction_id` is null in 100% of rows, so complaints cannot be linked to interactions.
- `customer_detected_accent` is null in 29.83% of interactions.
- No orphan `agent_id` values in interactions.

## Decision and consequences

1. **Workflow**: transaction-dispute intake, justified by the Queja workload above.
2. **Grounding**: customers, products, and transactions act as the system of record that the agent reads through its tools.
3. **Baseline**: current handle time and first-contact resolution for Queja are the operational baseline for projected savings, labeled as projections.
4. **Learned component**: trained and evaluated on team-generated conversations in `es` and `pt`, with reference labels produced by the policy engine ([dispute policy §16](../dispute-policy.md#16-deriving-evaluation-labels)).
5. **Not used for training or evaluation**: `was_escalated`, complaint `status`, transcript text, `detected_intents`, `main_topics`, and complaint `description`.
6. **Reported as a data limitation** in the final submission, with this spike as evidence.

## Open items

| # | Item | Next step |
|---|---|---|
| 1 | The fact tables have about 14% fewer rows than stated after deduplication. | Compare `raw_rows` and `pk_duplicate_rows` in the ingestion quality reports. |
| 2 | Transactions, customers, and products are not yet profiled. | New spike covering the [policy's open questions](../dispute-policy.md#17-assumptions-limitations-and-open-questions). |
| 3 | Whether the organizers intend the label and text issues. | Ask in the hackathon `#technical-help` channel. |
