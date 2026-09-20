"""add reported_fact view

The one relation ``vf_retrieval_role`` may read. It bakes in the ``is_latest``
filter and the three joins, and -- deliberately -- exposes no fiscal year or
fiscal period: those are provenance on ``filing``, not the period a fact
describes, and a column of that name is an invitation to PITFALLS.md 1.1.
Labels reach the result from the query plan instead.

See app/retrieval/DESIGN.md 3.

Revision ID: 3fcc714d6050
Revises: 3011d3c40ff8
Create Date: 2026-09-19 19:07:50.818795

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3fcc714d6050'
down_revision: Union[str, Sequence[str], None] = '3011d3c40ff8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# security_invoker is left OFF (the default) on purpose: the view runs with its
# owner's rights, which is what lets a role hold SELECT on it while holding
# nothing on fact / filing / company / concept.
CREATE_VIEW = """
CREATE VIEW xbrl.reported_fact AS
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
    op.execute(CREATE_VIEW)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW IF EXISTS xbrl.reported_fact")
