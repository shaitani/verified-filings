"""app/retrieval/validator.py -- the two controls the database cannot hold.

``vf_retrieval_role`` already cannot write, cannot issue DDL and can read one
view. What it cannot do is stop a statement turning ``statement_timeout`` off
(the setting is USERSET) or returning a million rows (PostgreSQL has no
per-role row cap). Those live here, so these tests are mostly about what gets
*refused*.

The last test runs a validated statement against the real test database, which
is what proves the validator's allow-list and the grant agree with each other.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.retrieval import (
    MAX_ROWS,
    ContractViolation,
    InvalidSQL,
    OutOfRole,
    validate,
)
from app.schemas.result import RESULT_COLUMNS

#: A statement of the shape the model is asked for: the plan's coordinates as a
#: VALUES list, joined against the view, projecting exactly the contract.
#: Written here rather than generated -- `build_prompt` writes no SQL, and a
#: test that needs Qwen to agree with it is not a test.
PLAN_JOIN_UNCAPPED = """
WITH plan(element_id, company_cik, fiscal_year, fiscal_period,
          period_start, period_end, concept_id, unit, is_instant) AS (
  VALUES ('e1', 320193, 2024, 'FY',
          DATE '2023-10-01', DATE '2024-09-28', 252, 'USD', false)
)
SELECT p.element_id, v.company_cik, v.ticker, v.entity_name,
       p.fiscal_year, p.fiscal_period,
       v.period_start, v.period_end, v.is_instant,
       v.value, v.unit, NULL::text AS derivation
FROM plan p
JOIN xbrl.reported_fact v
  ON  v.company_cik = p.company_cik
  AND v.concept_id  = p.concept_id
  AND v.unit        = p.unit
  AND v.is_instant  = p.is_instant
  AND v.period_end  = p.period_end
  AND (p.is_instant OR v.period_start = p.period_start)
"""

#: The same with the LIMIT the contract requires. `validate()` no longer adds
#: one -- it does not modify the statement at all -- so every accepted case has
#: to carry its own.
PLAN_JOIN = PLAN_JOIN_UNCAPPED + f"LIMIT {MAX_ROWS}"

#: A single branch, for the UNION ALL that mixing direct and residual bindings
#: needs. Self-contained so the two halves can be concatenated.
BRANCH = """SELECT 'e1' AS element_id, v.company_cik, v.ticker, v.entity_name,
       2024 AS fiscal_year, 'FY' AS fiscal_period,
       v.period_start, v.period_end, v.is_instant,
       v.value, v.unit, NULL::text AS derivation
FROM xbrl.reported_fact v"""


def _without_derivation(sql: str, replacement: str) -> str:
    return sql.replace("NULL::text AS derivation", replacement)


# --------------------------------------------------------------------------- #
# Accepted -- the statements the layer is actually built to run
# --------------------------------------------------------------------------- #


def test_an_accepted_statement_comes_back_byte_identical() -> None:
    """validate() judges; it does not edit. What executes is exactly what was
    inspected, so a wrong answer traces back to a statement somebody read."""
    assert validate(PLAN_JOIN) == PLAN_JOIN


def test_a_union_of_two_branches_is_accepted() -> None:
    """Mixing a direct and a residual binding in one statement needs this."""
    sql = BRANCH + "\nUNION ALL\n" + BRANCH + f"\nLIMIT {MAX_ROWS}"
    assert validate(sql) == sql


def test_aggregates_and_window_functions_are_accepted() -> None:
    """Qwen's half of the work -- ranking, growth, ratios -- is 44% of the
    answerable eval set. Refusing it would refuse the reason it is here."""
    sql = _without_derivation(
        PLAN_JOIN.replace("v.value, v.unit", "sum(v.value) OVER () AS value, v.unit"),
        "'share_of_total' AS derivation",
    )
    assert validate(sql)


@pytest.mark.parametrize(
    "derivation",
    [
        "'yoy_growth' AS derivation",
        "'yoy_growth'::text AS derivation",
        "CASE WHEN v.value IS NULL THEN NULL ELSE 'yoy_growth' END AS derivation",
    ],
)
def test_a_label_or_null_is_accepted_as_derivation(derivation: str) -> None:
    assert validate(_without_derivation(PLAN_JOIN, derivation))


@pytest.mark.parametrize(
    "derivation",
    [
        # q010, verbatim shape: the growth rate itself, in the label column.
        "(v.value - LAG(v.value) OVER (ORDER BY v.period_end))"
        " / LAG(v.value) OVER (ORDER BY v.period_end) AS derivation",
        "v.value AS derivation",
        "(v.value / 2)::text AS derivation",
        "CASE WHEN v.value IS NULL THEN 'none' ELSE v.value::text END AS derivation",
    ],
)
def test_a_computed_value_in_derivation_is_refused(derivation: str) -> None:
    """The label names what was computed; the number belongs in `value`."""
    with pytest.raises(InvalidSQL, match="derivation. must be a short text label"):
        validate(_without_derivation(PLAN_JOIN, derivation))


def test_a_subquery_derivation_is_checked_where_it_is_defined() -> None:
    """An outer `s.derivation` passes through; the inner definition is judged."""
    inner = _without_derivation(PLAN_JOIN_UNCAPPED, "v.value AS derivation")
    outer = (
        "SELECT s.element_id, s.company_cik, s.ticker, s.entity_name, s.fiscal_year, "
        "s.fiscal_period, s.period_start, s.period_end, s.is_instant, s.value, s.unit, "
        f"s.derivation FROM ({inner}) s LIMIT 10"
    )
    with pytest.raises(InvalidSQL, match="derivation. must be a short text label"):
        validate(outer)
    ok = outer.replace("v.value AS derivation", "'yoy_growth' AS derivation")
    assert validate(ok) == ok


def test_a_statement_under_the_cap_keeps_its_own_limit() -> None:
    sql = PLAN_JOIN_UNCAPPED + "LIMIT 36"
    assert validate(sql) == sql


def test_a_trailing_semicolon_is_accepted_and_kept() -> None:
    """One statement plus a terminator is still one statement, and tidying it
    away is not validate()'s job."""
    sql = PLAN_JOIN + ";"
    assert validate(sql) == sql


def test_a_statement_with_no_limit_is_refused() -> None:
    """PostgreSQL has no per-role row cap, and nothing appends one any more."""
    with pytest.raises(ContractViolation, match="no LIMIT"):
        validate(PLAN_JOIN_UNCAPPED)


def test_a_statement_that_reads_nothing_is_refused() -> None:
    """The worst failure this project has. Asked to write the whole statement
    from a table of coordinates, qwen2.5-coder:7b returned a UNION ALL of
    invented literals -- `285000000000 AS value` -- with no FROM at all. Every
    other check passed. Apple's real FY2025 revenue is 416,161,000,000."""
    columns = ", ".join(f"NULL AS {name}" for name in RESULT_COLUMNS)
    with pytest.raises(OutOfRole, match="never reads"):
        validate(f"SELECT {columns} LIMIT 1")


# --------------------------------------------------------------------------- #
# The USERSET escape -- the reason this module exists
# --------------------------------------------------------------------------- #


def test_a_set_statement_is_refused() -> None:
    with pytest.raises(InvalidSQL, match="only SELECT runs here"):
        validate("SET statement_timeout = 0")


def test_set_config_inside_a_projection_is_refused() -> None:
    """The one that matters. `set_config` is SET in an expression: it needs no
    SET statement and hides in an otherwise ordinary select list."""
    sql = _without_derivation(
        PLAN_JOIN, "set_config('statement_timeout', '0', false) AS derivation"
    )
    with pytest.raises(InvalidSQL, match="set_config"):
        validate(sql)


def test_a_schema_qualified_denied_call_is_still_refused() -> None:
    """The bare name is what is checked, so qualification is not a way past."""
    sql = _without_derivation(
        PLAN_JOIN, "pg_catalog.set_config('statement_timeout', '0', false) AS derivation"
    )
    with pytest.raises(InvalidSQL, match="set_config"):
        validate(sql)


@pytest.mark.parametrize("call", ["pg_sleep(30)::text", "pg_read_file('/etc/passwd')"])
def test_catalogue_and_system_functions_are_refused(call: str) -> None:
    with pytest.raises(InvalidSQL, match="not allowed"):
        validate(_without_derivation(PLAN_JOIN, f"{call} AS derivation"))


# --------------------------------------------------------------------------- #
# One statement, and it is a read
# --------------------------------------------------------------------------- #


def test_two_statements_are_refused() -> None:
    with pytest.raises(InvalidSQL, match="2 statements"):
        validate("SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("delete", "WITH d AS (DELETE FROM xbrl.fact RETURNING *) SELECT 1 AS x FROM d"),
        (
            "update",
            "WITH u AS (UPDATE xbrl.fact SET value = 0 RETURNING *) SELECT 1 AS x FROM u",
        ),
        (
            "insert",
            "WITH i AS (INSERT INTO xbrl.fact DEFAULT VALUES RETURNING *) "
            "SELECT 1 AS x FROM i",
        ),
    ],
)
def test_a_writing_cte_is_refused_though_the_statement_starts_with_with(
    label: str, sql: str
) -> None:
    """`WITH d AS (DELETE ...) SELECT ...` starts with WITH and deletes rows.
    Checking only the top of the tree would pass it."""
    with pytest.raises(InvalidSQL, match="data-modifying statement inside a CTE"):
        validate(sql)


def test_select_into_is_refused() -> None:
    with pytest.raises(InvalidSQL, match="creates a table"):
        validate("SELECT 1 AS element_id INTO mine FROM xbrl.reported_fact")


def test_a_locking_clause_is_refused() -> None:
    with pytest.raises(InvalidSQL, match="not a read"):
        validate(f"{PLAN_JOIN} FOR UPDATE")


def test_unparseable_text_is_refused() -> None:
    with pytest.raises(InvalidSQL, match="does not parse"):
        validate("not sql at all")


@pytest.mark.parametrize("sql", ["", "   \n  "])
def test_nothing_is_refused(sql: str) -> None:
    with pytest.raises(InvalidSQL, match="no statement"):
        validate(sql)


# --------------------------------------------------------------------------- #
# The view is the only relation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relation", ["xbrl.fact", "xbrl.filing", "xbrl.load_run", "pg_catalog.pg_authid"]
)
def test_naming_anything_but_the_view_is_refused(relation: str) -> None:
    """Redundant with the grant on purpose: the error names the problem here,
    instead of arriving as `permission denied` from a live connection."""
    with pytest.raises(InvalidSQL, match="cannot be read"):
        validate(PLAN_JOIN.replace("xbrl.reported_fact", relation))


def test_a_base_table_hidden_in_a_second_union_branch_is_refused() -> None:
    sql = f"{BRANCH}\nUNION ALL\n{BRANCH.replace('xbrl.reported_fact', 'xbrl.fact')}"
    with pytest.raises(InvalidSQL, match="xbrl.fact cannot be read"):
        validate(sql)


def test_the_unqualified_view_name_is_accepted() -> None:
    """search_path is set to xbrl for the role, so this is how it will usually
    be written."""
    assert validate(PLAN_JOIN.replace("xbrl.reported_fact", "reported_fact"))


# --------------------------------------------------------------------------- #
# The projection is the contract
# --------------------------------------------------------------------------- #


def test_a_wrong_projection_is_refused_and_says_what_is_missing() -> None:
    with pytest.raises(InvalidSQL) as caught:
        validate("SELECT 1 AS element_id FROM xbrl.reported_fact")
    message = str(caught.value)
    assert "missing" in message
    assert "company_cik" in message


def test_an_extra_column_is_refused() -> None:
    sql = PLAN_JOIN.replace(
        "NULL::text AS derivation", "NULL::text AS derivation, v.concept_id"
    )
    with pytest.raises(InvalidSQL, match=r"unexpected \['concept_id'\]"):
        validate(sql)


def test_select_star_is_refused() -> None:
    """A star projection cannot be checked against a contract, and would not
    satisfy it anyway."""
    with pytest.raises(InvalidSQL, match="no name"):
        validate("SELECT * FROM xbrl.reported_fact")


def test_an_unnamed_expression_is_refused_rather_than_guessed_at() -> None:
    """`NULL::text` with no alias is a column called `text`. Guessing the name
    is how that becomes a silently wrong projection."""
    sql = _without_derivation(PLAN_JOIN, "NULL::text")
    with pytest.raises(InvalidSQL, match="no name"):
        validate(sql)


def test_a_duplicated_column_name_is_refused() -> None:
    sql = PLAN_JOIN.replace("v.unit,", "v.unit AS value, v.unit,")
    with pytest.raises(InvalidSQL, match="more than once"):
        validate(sql)


# --------------------------------------------------------------------------- #
# The row cap -- PostgreSQL has nowhere else to put it
# --------------------------------------------------------------------------- #


def test_a_limit_over_the_ceiling_is_refused() -> None:
    with pytest.raises(InvalidSQL, match="exceeds the ceiling"):
        validate(PLAN_JOIN_UNCAPPED + "LIMIT 100000")


def test_a_non_constant_limit_is_refused() -> None:
    """It cannot be compared with a ceiling without evaluating it, and
    evaluating it is the database's job -- after this decision, not before."""
    with pytest.raises(InvalidSQL, match="plain integer"):
        validate(PLAN_JOIN_UNCAPPED + "LIMIT (SELECT 9999)")


def test_with_ties_is_refused() -> None:
    """It returns however many rows tie at the cut-off, so its own count is
    not a cap."""
    sql = PLAN_JOIN_UNCAPPED + "ORDER BY v.value FETCH FIRST 5 ROWS WITH TIES"
    with pytest.raises(InvalidSQL, match="WITH TIES"):
        validate(sql)


def test_the_cap_is_caller_settable() -> None:
    sql = PLAN_JOIN_UNCAPPED + "LIMIT 36"
    assert validate(sql, max_rows=36) == sql
    with pytest.raises(ContractViolation, match="exceeds the ceiling of 10"):
        validate(sql, max_rows=10)


# --------------------------------------------------------------------------- #
# Against the real database
# --------------------------------------------------------------------------- #


async def test_a_validated_statement_runs_and_returns_the_contract(
    test_session_factory,
) -> None:
    """The one test that proves the validator's allow-list and the database
    agree. A validator that accepted only statements PostgreSQL rejects would
    pass every test above."""
    async with test_session_factory() as session:
        result = await session.execute(text(validate(PLAN_JOIN)))
        assert tuple(result.keys()) == RESULT_COLUMNS
