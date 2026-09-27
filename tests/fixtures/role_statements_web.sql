
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'web') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA web FROM vf_query_mapper_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA web '
                     || 'REVOKE ALL ON TABLES FROM vf_query_mapper_role';
                EXECUTE 'REVOKE ALL ON SCHEMA web FROM vf_query_mapper_role';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'web') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA web FROM vf_retrieval_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA web '
                     || 'REVOKE ALL ON TABLES FROM vf_retrieval_role';
                EXECUTE 'REVOKE ALL ON SCHEMA web FROM vf_retrieval_role';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_web_role') THEN
                CREATE ROLE vf_web_role LOGIN;
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role CONNECTION LIMIT 10
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role PASSWORD 'vf_web_role-pw'
-- ---------------------------------------------------------------
GRANT CONNECT ON DATABASE "verified_filings" TO vf_web_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA web TO vf_web_role
-- ---------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA web FROM vf_web_role
-- ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA web REVOKE ALL ON TABLES FROM vf_web_role
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'xbrl') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA xbrl FROM vf_web_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA xbrl '
                     || 'REVOKE ALL ON TABLES FROM vf_web_role';
                EXECUTE 'REVOKE ALL ON SCHEMA xbrl FROM vf_web_role';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
GRANT SELECT ON web."user" TO vf_web_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.oauth_account TO vf_web_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.access_token TO vf_web_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.invite TO vf_web_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.conversation TO vf_web_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.job TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT (id, email, hashed_password, is_active, is_verified, created_at) ON web."user" TO vf_web_role
-- ---------------------------------------------------------------
GRANT UPDATE (email, hashed_password, is_active, is_verified) ON web."user" TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.oauth_account TO vf_web_role
-- ---------------------------------------------------------------
GRANT UPDATE ON web.oauth_account TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.access_token TO vf_web_role
-- ---------------------------------------------------------------
GRANT DELETE ON web.access_token TO vf_web_role
-- ---------------------------------------------------------------
GRANT UPDATE (used_at, used_by) ON web.invite TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.conversation TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.job TO vf_web_role
-- ---------------------------------------------------------------
GRANT UPDATE ON web.job TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.job_trace TO vf_web_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.job_feedback TO vf_web_role
-- ---------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM vf_web_role
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role SET search_path = web
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM vf_web_role
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA web FROM vf_web_role
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role SET default_transaction_read_only = off
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role SET statement_timeout = '10s'
-- ---------------------------------------------------------------
ALTER ROLE vf_web_role SET idle_in_transaction_session_timeout = '30s'
