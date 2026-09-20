"""Plan -> SQL -> rows. The layer between a ``QueryPlan`` and a rendered answer.

Four steps, of which only the last touches the database::

    build_prompt(plan)   -> str        the plan, rendered for Qwen      no DB
    generate(plan)       -> str        calls Qwen, returns SQL          no DB
    validate(sql)        -> str        raises, or returns the statement no DB
    execute(sql, plan)   -> ResultSet  reads as vf_retrieval_role

Qwen never holds a credential. It is a language model behind an HTTP endpoint:
it takes text and returns text, and ``execute()`` runs what it wrote as a role
that can reach exactly one relation -- the ``xbrl.reported_fact`` view.

Qwen also does not write all of the SQL. Retrieval -- getting the values a
``Binding`` names -- is bounded to four shapes (direct/residual x
instant/duration) and should be deterministic code. Qwen writes the layer
*above* it: ranking, growth, ratios across companies, filters on computed
values. Measured on the eval set, 20 of 45 answerable questions (44%) need
that layer.

Built: ``validate`` (``validator.py``). The other three are stubs with settled
signatures. See ``app/retrieval/DESIGN.md``.
"""

from app.retrieval.validator import MAX_ROWS, InvalidSQL, validate

__all__ = ["MAX_ROWS", "InvalidSQL", "validate"]
