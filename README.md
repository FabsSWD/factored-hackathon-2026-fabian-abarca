# Dispute intake

AI-first customer service for transaction-dispute intake at a synthetic LATAM bank (Factored AI & Data Hackathon 2026). Design, policy and architecture documents are in [docs/](docs/README.md).

## Pre-commit hook

The repository is public and the dataset is privately distributed, so commits are guarded against customer, product and transaction identifiers and against secrets. Install the hook once per clone:

```
git config core.hooksPath scripts/hooks
```

Before each commit, [scripts/hooks/pre-commit](scripts/hooks/pre-commit) does the following, and any failure blocks the commit:

- **Identifier test.** It runs `tests/test_reports.py` (`reports/*.json`, and `config/eval_scenarios/` outside `local/`) on a copy of the staged files, so it checks exactly what the commit would contain.
- **Secret scan.** It runs `gitleaks protect --staged` when [gitleaks](https://github.com/gitleaks/gitleaks) is installed. Without gitleaks it prints a warning and skips the scan, so installing it is recommended.

It uses `.venv`'s Python. Set `HOOK_PYTHON` to use another interpreter.

## Database roles

PostgreSQL is used by three roles. Each connection URL in `.env` belongs to exactly one of them, and the owner and the app must be different from the admin.

| Role | Example name | URL | Used by | Privileges |
|---|---|---|---|---|
| Admin | `disputes` | `--admin-url` of `scripts/db_roles.py` (or `ADMIN_DATABASE_URL`), and `TEST_DATABASE_URL` | Administration only: creating and updating the other two roles, and creating the temporary test databases. | Bootstrap superuser of the container (`POSTGRES_USER`). The roles script never modifies it. |
| Owner | `disputes_owner` | `MIGRATION_DATABASE_URL` | `alembic upgrade head` and `scripts/load_core_banking.py`. | Owns the database, every table (including `alembic_version`) and every sequence. `NOSUPERUSER`, no `CREATEROLE`, no `CREATEDB`. |
| App | `disputes_app` | `DATABASE_URL` | The running application. | `SELECT` on Core Banking (`customers`, `products`, `transactions`); `SELECT`, `INSERT`, `UPDATE` on Cases and Audit; `DELETE` only on `otp_challenges` and `otp_failures`. No DDL. |

If `MIGRATION_DATABASE_URL` is empty, migrations and the load fall back to `DATABASE_URL` (a single-role local setup).

### Setting up the roles

1. Start the database (`docker compose up db`) with `POSTGRES_USER` and `POSTGRES_PASSWORD` set.
2. Set `DATABASE_URL` and `MIGRATION_DATABASE_URL` in `.env` with the names and passwords the app and owner roles should have.
3. On a new database only, create the tables once as the admin, overriding the variable for that command alone (PowerShell: `$env:MIGRATION_DATABASE_URL = "<admin url>"; alembic upgrade head; Remove-Item Env:MIGRATION_DATABASE_URL`). A database whose tables already exist skips this step.
4. Create the roles and transfer ownership:

   ```
   python scripts/db_roles.py --admin-url postgresql://disputes:<admin password>@localhost:5432/postgres
   ```

   The script connects to the database named in `DATABASE_URL`, creates or updates both roles, transfers ownership of the database, tables and sequences to the owner object by object, applies the grants, and verifies them with `has_table_privilege` and `has_sequence_privilege`. Any difference is an error. The admin password must be written in `--admin-url`; `PGPASSWORD` and password files are ignored, and the output says how the server authenticated the admin.
5. Run it again after every migration that adds a table or sequence. To only check the current state (for example after a deployment):

   ```
   python scripts/db_roles.py --admin-url ... --verify
   ```

The roles are defined once in [scripts/sql/roles.sql](scripts/sql/roles.sql), which can also be applied with `psql` (see the header of that file).

## Manual testing

Requires the database loaded, `DATABASE_URL`, `OPENAI_API_KEY`, `KEV_*`, `PSEUDONYM_KEY`, `TEST_OTP` and `AGENT_API_TOKEN` in `.env`. These steps call OpenAI and Kev for real; local use only.

1. Start the server with exactly one worker (conversation state is kept in memory, so more workers would split conversations; a restart loses open ones):

   ```
   python scripts/serve.py
   ```

   It listens on `http://127.0.0.1:8000` (`--host` and `--port` to change it). Do not start it with `uvicorn --workers N`.

2. Find a customer for the scenario to test (read-only, prints to screen and writes no file):

   ```
   python scripts/find_test_customers.py
   ```

   For each scenario (`t1_purchase`, `pending`, `duplicate_pair`, `same_day`, `high_fraud`) it prints the `customer_id`, the document number to log in with (read from `data/raw/customers.csv`, since the database keeps only its HMAC) and the relevant transactions with date, merchant, amount and currency. The current data has no pair that meets `RC_DUPLICATE`; that flow is shown with `python scripts/demo_conversation.py`, which uses test doubles.

3. Chat as the customer:

   ```
   python scripts/manual_chat.py            # asks for the document number, logs in with TEST_OTP
   python scripts/manual_chat.py --anon     # without logging in
   ```

   Each turn prints the reply, the `trace_id`, the latency seen by the client and, read from the turn's trace with `AGENT_API_TOKEN`, the engine's result: outcome, failed gates, triggered rules, authorized actions, executed tools and model calls. Commands: `/handoff` prints the handoff packet of the conversation (agent API), `/new` starts a new conversation, `/quit` exits. Secrets are never printed.

## Customer Chat (M14)

The browser chat is a React + TypeScript + Vite app in [frontend/](frontend/). Requires Node 20.19+ or 22.12+. With the server from step 1 running:

```
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api and /auth to http://127.0.0.1:8000
```

`VITE_API_TARGET` points the proxy at another address. To open it from a phone on the same network, use `npm run dev -- --host` and the address it prints.

- Every text comes from the backend: the interface texts from `GET /api/ui/texts/{es|pt}` (catalog in [config/ui_texts.yaml](config/ui_texts.yaml)), the conversation from `/api/turn`. The only exception is the bilingual message shown when those texts cannot be loaded at all.
- `/api/turn` returns a `status` (`in_progress`, `awaiting_confirmation`, `authentication_required`, `case_created`, `handed_off`) and, once the case is created and read back, its `case_reference`. The chat follows that status.
- The session token is kept in memory only and sent in the `Authorization` header; it never goes in a URL or in browser storage. Only the language preference is stored (`localStorage`).

Checks: `npm run lint`, `npm run typecheck`, `npm test` (or `npm run coverage`), `npm run build`. The TypeScript types come from the API: after changing it, run `python scripts/export_openapi.py` and `npm run gen:api` (a backend test fails if `frontend/openapi.json` is out of date).

## Evaluation scenarios (M17)

The evaluation cases are specifications in `config/eval_scenarios/`; their labels come from the policy engine ([docs/evaluation-design.md](docs/evaluation-design.md)). Their records are seeded as `SEED-` rows with the owner role:

```
python scripts/seed_scenarios.py            # seed (idempotent: resets every case to its specification)
python scripts/seed_scenarios.py --remove   # delete every SEED- row, nothing else (see below)
```

It needs `MIGRATION_DATABASE_URL` (the owner role; the application's role is refused), `DOCUMENT_HASH_KEY` and `BUSINESS_DATE=2026-06-17`. A seeded customer logs in with its document number (`SEED-0007` for case `S007`) and `TEST_OTP`. Re-seeding revokes old sessions and never touches `audit_logs`. `--remove` refuses, and removes nothing, while audit records point to sessions of `SEED-` customers: the audit trail is append-only, so seeded customers with traces stay by design. To start from scratch, use a new database. To regenerate the files after changing the archetypes: `python scripts/generate_scenarios.py`, then `python scripts/label_scenarios.py` (it refuses to change the fixed split).

The cases on real records are selected and labeled read-only, and their records are never written. Their identifiers stay in `config/eval_scenarios/local/` (git-ignored). The repository keeps `config/eval_scenarios/real_selection.lock`: the criteria version, the seed, the counts, the expected label per case key and a SHA-256 of the selected identifiers.

```
python scripts/select_real_scenarios.py           # writes local/ (and the lock, if missing)
python scripts/select_real_scenarios.py --check   # selects again and compares with the lock
python scripts/label_scenarios.py --review        # writes reports/m17_review/review_sample.csv (git-ignored)
```

## Evaluation harness (M18)

The harness runs the system and a baseline (GPT-6 Luna deciding alone) on the evaluation split, and writes the metrics. It serves the real application in-process: a simulated customer logs in with the case's document and `TEST_OTP` and plays the case's script over the API. Faults are injected only where a case asks for them (`ESC-10` and `ESC-11`).

```
python scripts/run_evaluation.py --dry-run                              # guards, cases and estimate; no model call
python scripts/run_evaluation.py                                        # 3 system runs, 1 with connect off, baseline
python scripts/run_evaluation.py --cases S001,R005 --runs 1 --skip-baseline   # smoke run
```

- **Before each run.** The seeded cases are seeded again, and the run refuses if a real customer of the evaluation has a case or a card block.
- **After each run.** The cases the run created on real customers are removed with the owner role. `audit_logs` is never touched.
- **When it refuses to run.** It refuses when the evaluation split does not match its fingerprint, when an evaluation case appears in `config/eval_scenarios/calibration_log.yaml`, or when the local real cases are not the locked selection.
- **Outputs.** `reports/m18/results.csv` (git-ignored), `reports/m18_evaluation.json` (the M16 dashboard) and `docs/evaluation.md`.
- **Exit code.** It exits with 2 when a system run misses a hard rule.
