# Demo Script

| Field | Value |
|---|---|
| Status | Accepted |
| Version | 1.0.1 |
| Last updated | 2026-10-03 |
| Related | [Solution overview](solution-overview.md), [README](../README.md#docker-compose-m19) |

About 10 minutes. It uses three seeded demo customers (created for this project, evaluation cases S001, S004 and S065), so no real customer appears on screen. Each one logs in with its document number and the `TEST_OTP` from `.env`.

## Before the demo

1. Start the system and wait until every service is healthy:

   ```
   docker compose up -d --wait
   ```

2. Reset the demo customers to their initial state (it removes the cases and handoffs left by a rehearsal; the audit trail is kept):

   ```
   docker compose run --rm migrate
   ```

3. Open `http://localhost:8080` (the chat) and, in a second tab, `http://localhost:8080/agent` (the agent console). Have `AGENT_API_TOKEN` at hand.

The customer's lines below come from each case's script (`config/eval_scenarios/`). The system asks one thing at a time; answer with the line for what it asks. Its wording varies a little between runs, but the decisions do not: the evaluation (M18) ran these three cases three times each, with the expected outcome every time.

## 1. Spanish dispute, resolved automatically (T1)

Log in with the document number `SEED-0001` and the `TEST_OTP`: a customer with a 79.90 USD charge at Super Ahorro that they do not recognize.

| When the system asks for | Type |
|---|---|
| (first message) | Buenas, tengo un problema con un cobro. Es el cobro en el supermercado de más o menos 80 dólares, entre el 7 y el 9 de junio |
| the reason | Yo no hice esa compra, no la reconozco |
| whether the card is in hand | Sí, la tengo aquí conmigo |
| whether the details were shared | No, a nadie |
| blocking the card | No, prefiero no bloquearla por ahora |
| confirming the summary | Sí, confirmo |

**Expected.** The chat shows the case number. The amount is under 100 USD, so no person is needed.

**Say.** The model understood "the supermarket charge of about 80 dollars"; the rules checked that the charge is disputable, that it is under the T1 limit and that no fraud signal applies. The case number appears only after it was read back from the database.

## 2. The same in Portuguese

Log out, switch the language to Portuguese, and log in as `SEED-0004` (a 95 USD taxi charge).

| When the system asks for | Type |
|---|---|
| (first message) | Oi, preciso de ajuda com uma cobrança do cartão: uma cobrança de 95 dólares em um táxi dia 30 de maio. Não reconheço essa cobrança |
| whether the card is in hand | Sim, tenho ele aqui |
| whether the details were shared | Não, nunca passo minhas senhas |
| blocking the card | Não, prefiro não bloquear agora |
| confirming the summary | Sim, confirmo, está tudo certo |

**Expected.** A case number, every message in Portuguese.

**Say.** Same rules and templates in both languages; only the wording changes.

## 3. Escalation by amount, with handoff

Log out and log in as `SEED-0065` (a 1,450,000 ARS charge at Electro Mundo, about 1,450 USD).

| When the system asks for | Type |
|---|---|
| (first message) | Hola, quiero reclamar un cargo: un cargo de 1.450.000 pesos en Electro Mundo el 1 de junio. Me cobraron más de lo acordado |
| the agreed amount | Habíamos quedado en 1.200.000 pesos |

**Expected.** Above 1,000 USD the dispute is tier T3, which always goes to a person (`ESC-01`). The chat says it has been passed on and disables the input.

**Say.** The model did not decide this. A rule did, on the amount in USD read from the bank's records.

## 4. Agent console

In the agent tab, sign in with `AGENT_API_TOKEN`. The case from step 3 is at the top of the queue. Open it.

**Show.** The verified facts, each with its source table and record; the customer's claims, kept apart (the agreed 1,200,000); the reason for the escalation; the open questions. The agent can act without rereading the chat.

## 5. Trace viewer

From the handoff, open **Turn traces** and pick the last turn.

**Show.** The gates in order, the rule that fired, the model calls (the language model and Kev, with their latencies), the tokens and the cost of the turn.

**Say.** Every decision can be audited down to the rule and the data it read.

## 6. Metrics dashboard

Open **Metrics**.

**Show.** Outcomes, automation of the T1 and T2 tiers, latency p50/p95, and cost per case and per automated resolution. Each chart has a "Show the data" table.

## Closing message

Rules decide; models inform. The AI makes the conversation natural and finds what the customer means. The bank's policy, written as rules, decides what happens, and every decision can be explained and audited. Across 79 test conversations: 99.6% correct outcomes and 0 unsafe ones, against 84.4% and 8 for the same model deciding alone ([Solution overview](solution-overview.md#4-results)).
