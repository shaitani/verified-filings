"""recompute filed per use in reported_fact

``filed`` -- every latest fact joined to its company and concept -- is
referenced four times in the view (the filed rows, the annual side of a Q4,
its nine-month partner, and the anti-join), so PostgreSQL materialized it: it
built ``filed`` over **every** fact in the store before any caller's predicate
applied, then scanned that copy without indexes. At 20 companies and ~174k
facts that fit; at 113 companies and ~920k it did not. Measured 2026-10-16 on
dev, one company's revenue took 25.2 s, past the mapper role's 10 s
``statement_timeout`` -- the eval question q038 ("largest single-quarter
revenue decline") failed in the mapper's coverage query.

``NOT MATERIALIZED`` inlines ``filed`` at each use, so a caller's
``company_cik`` / ``concept_id`` filter reaches the ``fact`` indexes. Same
query, same rows: one company's revenue 25.2 s -> 0.015 s, all companies'
revenue 1.75 s -> 0.66 s, and the two definitions returned identical rows in
both directions (``EXCEPT ALL`` empty either way).

``CREATE OR REPLACE`` with the same columns in the same order, both ways, so
the grants on the view are kept and no role needs re-provisioning.

Revision ID: 43686d468581
Revises: 79cb6541df12
Create Date: 2026-10-16

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '43686d468581'
down_revision: Union[str, Sequence[str], None] = '79cb6541df12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The view as a8b5b820cf1a wrote it, with its day ranges written out (annual
#: 350-380 days, a Q4's leftover 80-120). Only how ``filed`` is held differs.
VIEW = """
CREATE OR REPLACE VIEW xbrl.reported_fact AS
WITH filed AS {materialization}(
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
          BETWEEN 350 AND 380
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
          BETWEEN 80 AND 120
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


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(VIEW.format(materialization="NOT MATERIALIZED "))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(VIEW.format(materialization=""))
