# Dispute intake

AI-first customer service for transaction-dispute intake at a synthetic LATAM bank (Factored AI & Data Hackathon 2026). Design, policy and architecture documents are in [docs/](docs/README.md).

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
