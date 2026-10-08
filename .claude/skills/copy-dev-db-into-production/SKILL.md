---
name: copy-dev-db-into-production
description: Copy the dev database's financial data (the xbrl schema) into production, keeping production's users, conversations and roles, then rebuild and start production. Use after companies were added in dev with app.prime_new_companies, or when the user asks to copy the dev database or its data into production.
---

# Copy the dev database's financial data into production

Run from the repository root in Git Bash. Don't ask anything, don't commit.

What is copied: the rows of the five `xbrl` tables (company, filing, concept with
its embeddings, fact, load_run). Production's rows there are replaced; nothing
else in its database is touched, and nothing outside `xbrl` points into those
tables. Dev and production never run together, so production is down from
step 1 until step 9.

1. **Stop production, start dev's database:**

   ```bash
   docker compose -f docker-compose.prod.yml stop
   docker compose -f docker-compose.dev.yml up -d --wait db
   ```

2. **Note dev's migration and counts**, to check production against:

   ```bash
   uv run alembic current
   docker compose -f docker-compose.dev.yml exec -T db psql -U postgres -d verified_filings -c "SELECT (SELECT count(*) FROM xbrl.company) AS companies, (SELECT count(*) FROM xbrl.fact) AS facts, (SELECT count(*) FROM xbrl.concept WHERE embedding IS NULL) AS unembedded"
   ```

3. **Dump only the `xbrl` rows** (`--data-only`: production keeps its own
   tables, grants and roles):

   ```bash
   docker compose -f docker-compose.dev.yml exec -T db pg_dump -U postgres -d verified_filings --schema=xbrl --data-only --disable-triggers > xbrl-data.sql
   ```

4. **Stop all of dev, start production's database.** `--profile web` includes
   dev's `api` container, which a plain `stop` leaves running:

   ```bash
   docker compose -f docker-compose.dev.yml --profile web stop
   docker compose -f docker-compose.prod.yml up -d --wait db
   ```

5. **Check production is at dev's migration** -- a data-only copy needs the
   same tables. If the revision differs from step 2's, copy nothing: report both
   revisions and skip to step 8, so production comes back up with its old data:

   ```bash
   docker compose -f docker-compose.prod.yml run --rm ops -m alembic current
   ```

6. **Replace production's `xbrl` rows in one transaction** -- on any error
   everything rolls back and production keeps its old data:

   ```bash
   ( echo "TRUNCATE xbrl.fact, xbrl.filing, xbrl.load_run, xbrl.concept, xbrl.company;"; cat xbrl-data.sql ) | docker compose -f docker-compose.prod.yml exec -T db psql -U postgres -d verified_filings -v ON_ERROR_STOP=1 --single-transaction
   ```

   If it fails, report the error and go on to step 8 so production comes back
   up with its old data.

7. **Check the counts match step 2's.** If they don't, say so plainly:

   ```bash
   docker compose -f docker-compose.prod.yml exec -T db psql -U postgres -d verified_filings -c "SELECT (SELECT count(*) FROM xbrl.company) AS companies, (SELECT count(*) FROM xbrl.fact) AS facts, (SELECT count(*) FROM xbrl.concept WHERE embedding IS NULL) AS unembedded"
   ```

8. **Delete the dump** -- before the rebuild, so the untracked file does not mark
   the code version `+dirty`:

   ```bash
   rm xbrl-data.sql
   ```

9. **Rebuild and start all of production.** The api image holds its own copies
   of `corpus_companies.json`, `company_aliases.json` and `sic_numbers.json`, so
   it finds new companies by name only once rebuilt:

   ```bash
   V=$(git rev-parse --short=12 HEAD); [ -n "$(git status --porcelain)" ] && V="$V+dirty"
   CODE_VERSION="$V" docker compose -f docker-compose.prod.yml up -d --build --wait
   ```

Report: dev's and production's counts side by side, the `CODE_VERSION` now
running, and that Funnel is off after a production start -- `/open-the-door` if
the site should be public. If `corpus_companies.json`, `sic_numbers.json` or
`company_aliases.json` have uncommitted changes, say they should be committed so
the repo matches what production serves.
