# Solution Overview

| Field | Value |
|---|---|
| Status | Accepted |
| Version | 1.1.0 |
| Last updated | 2026-10-03 |
| Related | [Dispute policy](dispute-policy.md), [Software architecture](software-architecture.md), [Evaluation](evaluation.md), [Demo script](demo-script.md) |

An AI assistant that takes card disputes from start to finish, in Spanish and Portuguese, without ever breaking the bank's policy. This page explains what it does and why it works, without the technical detail; the linked documents have it.

## 1. The problem

When a customer questions a card charge, the bank has to take a **dispute**: find the exact transaction, understand why the customer disputes it, check whether it can be disputed at all, and either open a case or pass it to a person. Today call-centre agents do this by hand. The work is repetitive, and every mistake is expensive: a case that should not exist, a missed fraud signal, or a refund promised without approval costs the bank money and the customer's trust.

Generic chatbots do not solve it. They talk well, but they cannot be trusted to apply a bank's policy to the letter, every time, for every customer.

## 2. The solution

**Rules decide; AI models inform.**

The assistant puts each kind of intelligence where it is strongest:

- **AI models understand the customer.** They work out the language, which charge the customer means ("the supermarket one, about 80 dollars, early June"), why they dispute it and what is still missing. A second, compact model (Kev) runs inside the deployment and adds measurable probabilities, such as how ambiguous a message is.
- **The bank's policy decides.** Every outcome comes from an explicit, versioned rulebook: can this transaction be disputed, can it be resolved automatically, is there a fraud signal, has the customer reached a limit. The same case always gets the same answer, and every answer can be explained to a customer, an agent or a regulator.
- **Nothing is promised until it is done.** A case number reaches the customer only after it has been read back from the database, and every message that commits the bank comes from approved templates.

Every conversation ends in one of five outcomes: **resolve** (a case is created), **clarify** (one more question), **inform** (why the charge cannot be disputed, for example while it is still pending), **escalate** (a person takes over) or **refuse** (for example, a request for another customer's data).

## 3. What each person gets

**The customer** logs in with a one-time code and describes the problem in their own words. The assistant finds the charge, asks only for what is missing, one question at a time, and reads back a summary to confirm. The customer leaves with a case number, and can block the card in the same conversation. If the charge cannot be disputed, they learn why on the spot. If the case needs a person, they are told it has been passed on, and never have to repeat their story.

**The human agent** receives only the cases that need judgement, in a queue sorted by priority. Each one arrives as a one-page handoff: the verified facts with the record they came from, what the customer claimed (kept apart from the facts), what was already done, the open questions and why it was escalated. The agent starts working at once, without rereading the chat.

**The supervisor or auditor** can open any turn of any conversation and see which rules fired, what each model said, how long every step took and what it cost. A live dashboard shows outcomes, automation rate, response time and cost per case.

## 4. Results

I measured the assistant on 79 conversations: 42 in Spanish, 36 in Portuguese and 1 in English, covering routine disputes, ambiguous requests and cases that must go to a person. The benchmark is a strong one: the same AI model, given the whole policy and asked to decide on its own.

| | The assistant | AI model deciding alone |
|---|---|---|
| Correct outcome | **99.6%** | 84.4% |
| Unsafe outcomes (for example, a case opened that should have gone to a person) | **0** | 8 |
| Mandatory escalations missed | **0** | 8 |
| Disputes resolved with no person involved | **66.7%** | not applicable |
| Cost per conversation | **USD 0.001** | USD 0.0005 |
| Response time per message (median / 95th percentile) | **5.8 s / 8.2 s** | 7.4 s / 19.3 s |

What stands behind these numbers:

- **Safety by design, not by luck.** The model on its own misses escalations because the policy depends on data that must never reach an AI model: fraud scores, account status and earlier cases. The assistant's rules read that data directly, so it gets these cases right without exposing anything.
- **Two out of three disputes need no agent**, at a tenth of a US cent each. The agents' time goes to the cases that really need it.
- **Faster and steadier.** Slow messages take less than half as long as with the model alone (8.2 s against 19.3 s at the 95th percentile). About 2 seconds of each reply go to friendlier connecting sentences; the bank can trade them for speed (3.8 s median) without changing a single decision.
- **Fair across the board.** Country, age and gender never reach a model or a rule, and accuracy stays between 97.6% and 100% in every country, age band and gender.
- **Measured in the open.** Every evaluation run is published in full, including the first, blind one, where the assistant was already more than twice as accurate as the model alone (90.3% against 41.8%) ([Evaluation](evaluation.md)).

## 5. Key decisions

| Decision | Why |
|---|---|
| Write the dispute policy first, as a numbered rulebook | The rules are what the bank is accountable for. Code, tests, audit logs and evaluation labels all refer to the same rule identifiers. |
| Let the rules decide and the models inform | Rules give the same answer every time and can be explained. Models bring what rules cannot: understanding free, messy language in two languages. |
| Build test cases from the policy, not from historical labels | Analysis of the bank's historical escalation labels showed they carry no learnable signal (an AUC of 0.50, the same as chance) ([spike](spikes/2026-09-25-data-label-validity.md)). The assistant is measured against the policy itself. |
| Run a compact model of its own (Kev) next to the large one | It is fast and cheap, runs inside the deployment so the data never leaves it, and gives probabilities that can be measured and calibrated. |
| Keep restricted data away from the AI models | Documents, birth dates, contact details, fraud scores and demographics never reach a model. This protects customers and removes a source of bias. |
| Confirm before acting, and read the result back | No case or card block happens without the customer's explicit "yes", and nothing is promised until the database confirms it. |
| Hand over a complete package, not a transcript | The agent starts from verified facts and open questions, and the customer never repeats the story. |
| One command to start everything | `docker compose up -d --wait` starts the database, the application, Kev and the website, with the data loaded and checked. |

## 6. From demo to full version

The demo runs on a synthetic Latin American bank, with its own policy, customers and transactions, and every piece a real bank needs is already in place behind a defined interface. The full version builds on that:

1. **Plugs into the bank's systems.** The identity provider, the core banking system and the case management system connect through the interfaces the demo already uses, and the policy rulebook takes the bank's own thresholds and rules.
2. **Kev, tuned to the bank.** In the demo Kev runs in advisory mode: its signals are recorded in every audit trail. The full version calibrates it on the bank's own Spanish and Portuguese conversations, so its confidence can send uncertain cases to a person automatically under the existing rule (`ESC-11`).
3. **Scales out.** Conversations move to a shared store, so the assistant runs on as many servers as the traffic needs.
4. **Personal agent accounts.** Each agent signs in with their own account, so every action in the audit trail is attributed to a person.
5. **Model choice.** The assistant works with any comparable language model. A bank with strict data-residency rules can run a self-hosted one; the regression tests and the evaluation suite confirm the decisions stay the same.
6. **A pilot with real customers**, measured against the bank's agents on the same cases, with the same evaluation suite.
