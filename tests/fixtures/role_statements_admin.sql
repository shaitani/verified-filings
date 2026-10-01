
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                CREATE ROLE vf_admin_role LOGIN;
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role CONNECTION LIMIT 4
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role PASSWORD 'vf_admin_role-pw'
-- ---------------------------------------------------------------
GRANT CONNECT ON DATABASE "verified_filings" TO vf_admin_role
-- ---------------------------------------------------------------
GRANT USAGE ON SCHEMA web TO vf_admin_role
-- ---------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA web FROM vf_admin_role
-- ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA web REVOKE ALL ON TABLES FROM vf_admin_role
-- ---------------------------------------------------------------

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'xbrl') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA xbrl FROM vf_admin_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA xbrl '
                     || 'REVOKE ALL ON TABLES FROM vf_admin_role';
                EXECUTE 'REVOKE ALL ON SCHEMA xbrl FROM vf_admin_role';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------
GRANT SELECT ON web."user" TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.oauth_account TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.access_token TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.invite TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.conversation TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.job TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.job_trace TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.job_feedback TO vf_admin_role
-- ---------------------------------------------------------------
GRANT SELECT ON web.admin_action TO vf_admin_role
-- ---------------------------------------------------------------
GRANT UPDATE (is_active, hashed_password) ON web."user" TO vf_admin_role
-- ---------------------------------------------------------------
GRANT DELETE ON web."user" TO vf_admin_role
-- ---------------------------------------------------------------
GRANT DELETE ON web.access_token TO vf_admin_role
-- ---------------------------------------------------------------
GRANT INSERT (id, kind, email, code_hash, github_account_id, expires_at, created_by) ON web.invite TO vf_admin_role
-- ---------------------------------------------------------------
GRANT UPDATE (revoked_at) ON web.invite TO vf_admin_role
-- ---------------------------------------------------------------
GRANT INSERT ON web.admin_action TO vf_admin_role
-- ---------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM vf_admin_role
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role SET search_path = web
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM vf_admin_role
-- ---------------------------------------------------------------
REVOKE CREATE ON SCHEMA web FROM vf_admin_role
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role SET default_transaction_read_only = off
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role SET statement_timeout = '10s'
-- ---------------------------------------------------------------
ALTER ROLE vf_admin_role SET idle_in_transaction_session_timeout = '30s'
-- ---------------------------------------------------------------

        DO $$
        DECLARE
            existing record;
        BEGIN
            IF to_regclass('web."user"') IS NULL THEN
                RETURN;
            END IF;
            EXECUTE 'ALTER TABLE web."user" ENABLE ROW LEVEL SECURITY';
            FOR existing IN
                SELECT policyname FROM pg_policies
                WHERE schemaname = 'web' AND tablename = 'user'
            LOOP
                EXECUTE format('DROP POLICY %I ON web."user"', existing.policyname);
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_web_role') THEN
                EXECUTE 'CREATE POLICY vf_web_role_all ON web."user" FOR ALL TO vf_web_role USING (true) WITH CHECK (true)';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                EXECUTE 'CREATE POLICY vf_admin_role_reads ON web."user" FOR SELECT TO vf_admin_role USING (true)';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                EXECUTE 'CREATE POLICY vf_admin_role_changes_readers ON web."user" FOR UPDATE TO vf_admin_role USING (NOT is_superuser) WITH CHECK (NOT is_superuser)';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                EXECUTE 'CREATE POLICY vf_admin_role_deletes_readers ON web."user" FOR DELETE TO vf_admin_role USING (NOT is_superuser)';
            END IF;
        END
        $$
        
-- ---------------------------------------------------------------

        DO $$
        DECLARE
            existing record;
        BEGIN
            IF to_regclass('web.access_token') IS NULL THEN
                RETURN;
            END IF;
            EXECUTE 'ALTER TABLE web.access_token ENABLE ROW LEVEL SECURITY';
            FOR existing IN
                SELECT policyname FROM pg_policies
                WHERE schemaname = 'web' AND tablename = 'access_token'
            LOOP
                EXECUTE format('DROP POLICY %I ON web.access_token', existing.policyname);
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_web_role') THEN
                EXECUTE 'CREATE POLICY vf_web_role_all ON web.access_token FOR ALL TO vf_web_role USING (true) WITH CHECK (true)';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                EXECUTE 'CREATE POLICY vf_admin_role_reads ON web.access_token FOR SELECT TO vf_admin_role USING (true)';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vf_admin_role') THEN
                EXECUTE 'CREATE POLICY vf_admin_role_ends_readers_sessions ON web.access_token FOR DELETE TO vf_admin_role USING (NOT EXISTS (SELECT 1 FROM web."user" u WHERE u.id = user_id AND u.is_superuser))';
            END IF;
        END
        $$
        
