
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_invite_watch_role') THEN
                CREATE ROLE vf_invite_watch_role LOGIN;
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role CONNECTION LIMIT 1
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role PASSWORD 'vf_invite_watch_role-pw'
-- ---------------------------------------------------------------
GRANT CONNECT ON DATABASE "verified_filings" TO vf_invite_watch_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA web TO vf_invite_watch_role
-- ---------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA web FROM vf_invite_watch_role
-- ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA web REVOKE SELECT ON TABLES FROM vf_invite_watch_role
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'xbrl') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA xbrl FROM vf_invite_watch_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA xbrl '
                     || 'REVOKE ALL ON TABLES FROM vf_invite_watch_role';
                EXECUTE 'REVOKE ALL ON SCHEMA xbrl FROM vf_invite_watch_role';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
GRANT SELECT (id, used_at, used_by) ON web.invite TO vf_invite_watch_role
-- ---------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM vf_invite_watch_role
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role SET search_path = web
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM vf_invite_watch_role
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role SET default_transaction_read_only = on
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role SET statement_timeout = '10s'
-- ---------------------------------------------------------------
ALTER ROLE vf_invite_watch_role SET idle_in_transaction_session_timeout = '30s'
