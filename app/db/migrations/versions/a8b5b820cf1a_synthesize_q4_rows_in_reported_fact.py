"""synthesize Q4 rows in reported_fact

No US filer reports a fourth quarter -- the 10-K covers it -- so a Q4 value is
the annual figure minus the nine-month year-to-date one, both sharing a start
date. That subtraction used to be asked of the SQL-writing model, which did
not do it: it returned Apple's FY2024 annual revenue as its Q4.

Doing it here makes it deterministic, tested once, and free to every consumer.
A Q4 becomes an ordinary row and nothing downstream has to know it was
computed -- except that ``is_synthesized`` says so, because a value nobody
filed must never pass for one that was.

Measured over the 20 loaded filers before writing this:

* the pairing is unambiguous: zero annual rows have more than one partner
  leaving a quarter-sized gap;
* it yields 9,464 Q4 rows across all 20 filers;
* 39 of those windows are ALSO filed directly, all by J&J, who report a
  fourth-quarter column. Every one of the 39 agrees to the cent with the
  subtraction -- the best available evidence that the arithmetic is right --
  and the anti-join below keeps the filed row rather than emitting a
  duplicate, which would break the one-row-per-coordinate grain the retrieval
  row count depends on.

``CREATE OR REPLACE`` rather than drop-and-create: it keeps the existing
grants, so ``vf_retrieval_role`` never loses access mid-migration. That is
only possible because ``is_synthesized`` is added at the END of the column
list; reordering or renaming would force a drop.

Revision ID: a8b5b820cf1a
Revises: 3fcc714d6050
Create Date: 2026-09-20

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8b5b820cf1a'
down_revision: Union[str, Sequence[str], None] = '3fcc714d6050'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Day counts, not fiscal labels. `filing` carries fiscal_year/fiscal_period but
# those are provenance -- which filing a number appeared in, not the period it
# describes (PITFALLS 1.1) -- so a 10-K's prior-year comparative column would
# be mis-grouped by them. The window lengths are the period itself.
ANNUAL_DAYS = (350, 380)

# The subtrahend is identified by what it LEAVES, not by its own length.
#
# A fixed "nine months is 260-285 days" bucket was tried first and was wrong:
# Costco's fiscal quarters run 12/12/12/16 weeks, so its year-to-date-through-
# Q3 is 251 days and fell outside it. Every Costco Q4 went missing, silently
# -- the view simply had no row. Defining the pair by the gap it leaves makes
# the rule indifferent to how a filer divides its year: whatever remains of
# the annual window must itself be about a quarter.
#
# Measured: 9,464 pairs across all 20 filers, zero of them ambiguous, and the
# leftover spans 89 to 119 days -- comfortably inside this range at both ends.
LEFTOVER_DAYS = (80, 120)

NEW_VIEW = f"""
CREATE OR REPLACE VIEW xbrl.reported_fact AS
WITH filed AS (
    SELECT f.company_cik,
           co.ticker,
           co.entity_name,
           f.concept_id,
           c.taxonomy,
           c.name        AS concept_name,
           c.label       AS concept_label,
           f.unit,
           f.is_instant,
           f.period_start,
           f.period_end,
           f.value
    FROM xbrl.fact f
    JOIN xbrl.company co ON co.cik = f.company_cik
    JOIN xbrl.concept  c ON c.id   = f.concept_id
    WHERE f.is_latest
),
annual AS (
    SELECT * FROM filed
    WHERE NOT is_instant
      AND (period_end - period_start)
          BETWEEN {ANNUAL_DAYS[0]} AND {ANNUAL_DAYS[1]}
),
fourth_quarter AS (
    SELECT a.company_cik,
           a.ticker,
           a.entity_name,
           a.concept_id,
           a.taxonomy,
           a.concept_name,
           a.concept_label,
           a.unit,
           false                 AS is_instant,
           n.period_end + 1      AS period_start,
           a.period_end          AS period_end,
           -- cast back to the column's own typmod: subtraction yields a
           -- bare `numeric`, and CREATE OR REPLACE refuses to change a view
           -- column's declared type (which would cost the grants).
           (a.value - n.value)::numeric(30, 6) AS value
    FROM annual a
    JOIN filed n
      ON  n.company_cik  = a.company_cik
      AND n.concept_id   = a.concept_id
      AND n.unit         = a.unit
      AND NOT n.is_instant
      AND n.period_start = a.period_start
      AND n.period_end   < a.period_end
      AND (a.period_end - n.period_end)
          BETWEEN {LEFTOVER_DAYS[0]} AND {LEFTOVER_DAYS[1]}
    WHERE NOT EXISTS (
        SELECT 1
        FROM filed x
        WHERE x.company_cik  = a.company_cik
          AND x.concept_id   = a.concept_id
          AND x.unit         = a.unit
          AND NOT x.is_instant
          AND x.period_start = n.period_end + 1
          AND x.period_end   = a.period_end
    )
)
SELECT company_cik, ticker, entity_name, concept_id, taxonomy, concept_name,
       concept_label, unit, is_instant, period_start, period_end, value,
       false AS is_synthesized
FROM filed
UNION ALL
SELECT company_cik, ticker, entity_name, concept_id, taxonomy, concept_name,
       concept_label, unit, is_instant, period_start, period_end, value,
       true AS is_synthesized
FROM fourth_quarter
"""

OLD_VIEW = """
CREATE OR REPLACE VIEW xbrl.reported_fact AS
SELECT f.company_cik,
       co.ticker,
       co.entity_name,
       f.concept_id,
       c.taxonomy,
       c.name        AS concept_name,
       c.label       AS concept_label,
       f.unit,
       f.is_instant,
       f.period_start,
       f.period_end,
       f.value
FROM xbrl.fact f
JOIN xbrl.company co ON co.cik = f.company_cik
JOIN xbrl.concept  c ON c.id   = f.concept_id
WHERE f.is_latest
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(NEW_VIEW)


def downgrade() -> None:
    """Downgrade schema."""
    # Dropping a column needs a real drop; CREATE OR REPLACE cannot shrink the
    # column list. The grant goes with it, so re-run `python -m app.db.roles`.
    op.execute("DROP VIEW xbrl.reported_fact")
    op.execute(OLD_VIEW)
