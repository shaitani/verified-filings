
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_query_mapper_role') THEN
                CREATE ROLE vf_query_mapper_role LOGIN;
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role CONNECTION LIMIT -1
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role PASSWORD 'vf_query_mapper_role-pw'
-- ---------------------------------------------------------------
GRANT CONNECT ON DATABASE "verified_filings" TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA xbrl TO vf_query_mapper_role
-- ---------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA xbrl FROM vf_query_mapper_role
-- ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA xbrl REVOKE SELECT ON TABLES FROM vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.company TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.filing TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.fact TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.concept TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.reported_fact TO vf_query_mapper_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO vf_query_mapper_role
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role SET search_path = xbrl, public
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM vf_query_mapper_role
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role SET default_transaction_read_only = on
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role SET statement_timeout = '10s'
-- ---------------------------------------------------------------
ALTER ROLE vf_query_mapper_role SET idle_in_transaction_session_timeout = '30s'
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_retrieval_role') THEN
                CREATE ROLE vf_retrieval_role LOGIN;
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role CONNECTION LIMIT 4
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role PASSWORD 'vf_retrieval_role-pw'
-- ---------------------------------------------------------------
GRANT CONNECT ON DATABASE "verified_filings" TO vf_retrieval_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA xbrl TO vf_retrieval_role
-- ---------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA xbrl FROM vf_retrieval_role
-- ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA xbrl REVOKE SELECT ON TABLES FROM vf_retrieval_role
-- ---------------------------------------------------------------
GRANT SELECT ON xbrl.reported_fact TO vf_retrieval_role
-- ---------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM vf_retrieval_role
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role SET search_path = xbrl
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM vf_retrieval_role
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role SET default_transaction_read_only = on
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role SET statement_timeout = '10s'
-- ---------------------------------------------------------------
ALTER ROLE vf_retrieval_role SET idle_in_transaction_session_timeout = '30s'
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'verified_filings_ro') THEN
                EXECUTE 'DROP OWNED BY verified_filings_ro';
                EXECUTE 'REVOKE ALL ON DATABASE "verified_filings" FROM verified_filings_ro';
                EXECUTE 'DROP ROLE verified_filings_ro';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
REVOKE ALL ON DATABASE "verified_filings" FROM PUBLIC
