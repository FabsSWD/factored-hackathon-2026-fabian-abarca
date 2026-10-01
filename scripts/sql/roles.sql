-- Database roles for the dispute-intake system (milestone M9).
--
-- Three roles:
-- * admin: the bootstrap superuser (the container's POSTGRES_USER). It runs this script and is
--   never modified by it; Postgres does not allow removing SUPERUSER from it.
-- * owner: owns the database, every table and every sequence (MIGRATION_DATABASE_URL).
-- * app: the running application (DATABASE_URL).
--
-- Run as the admin, connected to the application database, AFTER `alembic upgrade head`:
--
--   psql -v owner_role=disputes_owner -v owner_password=... \
--        -v app_role=disputes_app -v app_password=... \
--        -d disputes -f scripts/sql/roles.sql
--
-- or `python scripts/db_roles.py --admin-url ...`, which takes the role names and passwords
-- from MIGRATION_DATABASE_URL and DATABASE_URL and applies this same file.
--
-- Grants of the app role:
--   - Core Banking (customers, products, transactions): SELECT only. The application never
--     writes Core Banking; card blocks (ACT-03) go to card_blocks.
--   - Cases (cases, handoff_packets, card_blocks) and Audit (audit_logs, sessions,
--     otp_challenges, otp_failures): SELECT, INSERT, UPDATE.
--   - DELETE only on otp_challenges and otp_failures: the identity service clears them after
--     a successful OTP. No DELETE anywhere else, so cases and audit records cannot be removed.
--
-- Ownership is transferred object by object (ALTER ... OWNER TO), never with REASSIGN OWNED BY
-- on the bootstrap user, which fails on system objects.
--
-- The script is idempotent: it creates missing roles, resets their passwords, and re-applies
-- ownership and grants. Re-run it after every migration that adds a table or sequence;
-- `python scripts/db_roles.py --verify` checks the result.

\set ON_ERROR_STOP on

-- Guard: the admin running this script is never the owner or the app role.
SELECT format('DO $$ BEGIN RAISE EXCEPTION %L; END $$', 'owner_role and app_role must differ from the user running this script')
WHERE :'owner_role' = current_user OR :'app_role' = current_user OR :'owner_role' = :'app_role' \gexec

-- Roles ----------------------------------------------------------------------------------
SELECT format('CREATE ROLE %I LOGIN', :'owner_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner_role') \gexec
SELECT format('CREATE ROLE %I LOGIN', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'owner_role', :'owner_password') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_role', :'app_password') \gexec

-- Database and schema --------------------------------------------------------------------
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), :'owner_role') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'owner_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role') \gexec

-- Ownership: the owner role owns the database, every table (alembic_version included) and
-- every sequence. Sequences tied to a table change owner with it.
SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'owner_role') \gexec
SELECT format('ALTER TABLE public.%I OWNER TO %I', tablename, :'owner_role')
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename \gexec
SELECT format('ALTER SEQUENCE public.%I OWNER TO %I', c.relname, :'owner_role')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_depend d ON d.objid = c.oid AND d.deptype IN ('a', 'i')
WHERE c.relkind = 'S' AND n.nspname = 'public' AND d.objid IS NULL
ORDER BY c.relname \gexec

-- Application grants ---------------------------------------------------------------------
SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', :'app_role') \gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', :'app_role') \gexec

-- Core Banking: read-only
SELECT format('GRANT SELECT ON public.customers, public.products, public.transactions TO %I', :'app_role') \gexec

-- Cases and Audit
SELECT format('GRANT SELECT, INSERT, UPDATE ON public.cases, public.handoff_packets, public.card_blocks, public.audit_logs, public.sessions, public.otp_challenges, public.otp_failures TO %I', :'app_role') \gexec
SELECT format('GRANT DELETE ON public.otp_challenges, public.otp_failures TO %I', :'app_role') \gexec

-- Sequences used by the application's inserts
SELECT format('GRANT USAGE, SELECT ON SEQUENCE public.case_number_seq, public.handoff_number_seq, public.audit_logs_id_seq, public.otp_failures_id_seq TO %I', :'app_role') \gexec
