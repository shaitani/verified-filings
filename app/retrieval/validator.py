"""``validate(sql)`` -- refuse a generated statement, or return the one to run.

The part of this layer that cannot be skipped.

Why it exists when the role is already read-only
------------------------------------------------
``vf_retrieval_role`` cannot write, cannot issue DDL, and holds ``SELECT`` on
exactly one relation. That is a real boundary and a role cannot widen it. But
two of the guards around it are not boundaries at all:

* ``statement_timeout`` and ``default_transaction_read_only`` are **USERSET**.
  A statement beginning ``SET statement_timeout = 0`` shrugs them off, and so
  does ``SELECT set_config('statement_timeout', '0', false)`` -- the function
  form, which needs no ``SET`` and hides inside an ordinary projection. Only a
  refusal at the string level stops either.
* PostgreSQL has **no per-role row limit**. The cap on how much comes back can
  only live here.

So this module is not defence-in-depth decoration. It holds the two controls
the database cannot.

Why PostgreSQL's own parser
---------------------------
``pglast`` wraps libpg_query -- the real PostgreSQL grammar, not a
reimplementation of it. A validator that parses a string differently from the
server that will execute it has a gap between the two readings, and that gap is
the whole attack. This has no such gap by construction.

What this function does and does not do
---------------------------------------
It **inspects a string and returns a verdict**. That is all.

* It does not run the statement. ``execute()`` is the only thing that does.
* It does not *modify* the statement. An earlier version appended a missing
  ``LIMIT``; it no longer does, because a validator that edits its input means
  what runs is not what was read, and the statement that gets blamed for a
  wrong answer is then nobody's. A missing ``LIMIT`` is now a refusal, and the
  prompt tells the model to include one.
* It returns the statement **byte-identical** to what it was given, so a
  caller can pass its result straight to ``execute()`` without wondering.

Two kinds of rejection, because they mean different things
-----------------------------------------------------------
``ContractViolation``
    The model wrote a statement that does not fit the contract -- wrong
    columns, no ``LIMIT``, an unnamed expression. An ordinary mistake. Logged
    at INFO.

``OutOfRole``
    The model tried to do something outside what it is for: change a session
    setting, read a relation it has no business in, modify data. Not a
    mistake in degree -- a different kind of thing, and the one worth being
    told about. **Logged at WARNING**, naming what was attempted, so it shows
    up whether or not anyone is reading return values.

Both are ``InvalidSQL``, so a caller that does not care can catch one type.

What it enforces
----------------
Conservative throughout: anything not positively understood is refused.

1. The text parses, and is **exactly one statement**.
2. The statement is a ``SELECT``. Not "starts with SELECT" -- ``WITH d AS
   (DELETE FROM xbrl.fact RETURNING *) SELECT * FROM d`` starts with ``WITH``
   and deletes rows, so *every* statement node in the tree must be a
   ``SelectStmt``.
3. No ``SELECT ... INTO`` and no locking clause.
4. No denied function call anywhere -- ``set_config`` above all.
5. Every relation named is the view or a CTE defined in the same statement,
   **and the view is named at least once** -- a statement that reads nothing
   invented whatever it returns.
6. The projection is exactly ``RESULT_COLUMNS``, in any order.
7. A ``LIMIT`` present, a plain integer, and at or under ``max_rows``.
   ``FETCH ... WITH TIES`` is refused, because it returns more rows than its
   own count.

See ``app/retrieval/DESIGN.md`` §7.
"""

from __future__ import annotations

import logging

from pglast import ast, parse_sql
from pglast.enums import LimitOption
from pglast.parser import ParseError
from pglast.visitors import Visitor

from app.schemas.result import RESULT_COLUMNS

logger = logging.getLogger(__name__)

#: Ceiling on rows returned by one execution. PostgreSQL has no per-role row
#: limit, so this is the only place it can be set.
MAX_ROWS = 500

#: The one relation ``vf_retrieval_role`` can read. See the migration
#: ``3fcc714d6050`` and ``app/retrieval/DESIGN.md`` §3.
VIEW_SCHEMA = "xbrl"
VIEW_NAME = "reported_fact"

#: Function names refused wherever they appear. ``set_config`` is the reason
#: this list exists: it is ``SET`` in an expression, so it reaches
#: ``statement_timeout`` from inside a projection that otherwise looks
#: ordinary. The rest are refused because nothing answering a question about
#: filings has any use for them.
DENIED_FUNCTIONS = frozenset({"set_config", "dblink", "dblink_exec", "query_to_xml"})

#: Function name *prefixes* refused the same way -- the catalogue and
#: large-object families, neither of which this layer has business touching.
DENIED_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink_")

#: Statement nodes that may appear in the tree. ``RawStmt`` is libpg_query's
#: wrapper around each parsed statement, not a statement itself.
ALLOWED_STATEMENT_NODES = frozenset({"RawStmt", "SelectStmt"})


class InvalidSQL(ValueError):
    """A generated statement this layer will not run.

    A ``ValueError`` because it is a bad value, not a failure: refusing is a
    normal, expected outcome of asking a language model for SQL.
    """


class ContractViolation(InvalidSQL):
    """The statement does not fit the result contract. An ordinary mistake."""


class OutOfRole(InvalidSQL):
    """The statement tried to do something the model is not there to do.

    Changing a session setting, reading a relation outside the view, modifying
    data. Raised *and* logged at WARNING, because the point of this class is
    that someone finds out.
    """


def _out_of_role(attempted: str, detail: str) -> OutOfRole:
    logger.warning("generated SQL attempted %s outside its role: %s", attempted, detail)
    return OutOfRole(detail)


class _Inspector(Visitor):
    """One walk of the tree, collecting everything the checks need.

    A visitor rather than a series of targeted lookups because the hazards are
    *nested*: a ``DELETE`` inside a CTE, a ``set_config`` inside a projection,
    a catalogue table inside a subquery. Anything that only examined the top
    of the tree would pass all three.
    """

    def __init__(self) -> None:
        super().__init__()
        self.statement_nodes: list[str] = []
        self.functions: list[str] = []
        self.relations: list[tuple[str | None, str]] = []
        self.cte_names: set[str] = set()
        self.derivations: list[ast.Node] = []

    def visit(self, ancestors, node) -> None:  # noqa: ARG002 - visitor protocol
        name = type(node).__name__
        if name.endswith("Stmt"):
            self.statement_nodes.append(name)

    def visit_FuncCall(self, ancestors, node) -> None:  # noqa: ARG002
        # funcname is a tuple of String nodes: ("count",) or
        # ("pg_catalog", "set_config"). The bare name is what is denied, so a
        # schema qualification cannot be used to slip past the list.
        parts = [part.sval for part in node.funcname if isinstance(part, ast.String)]
        if parts:
            self.functions.append(parts[-1].lower())

    def visit_RangeVar(self, ancestors, node) -> None:  # noqa: ARG002
        self.relations.append((node.schemaname, node.relname))

    def visit_CommonTableExpr(self, ancestors, node) -> None:  # noqa: ARG002
        self.cte_names.add(node.ctename)

    def visit_ResTarget(self, ancestors, node) -> None:  # noqa: ARG002
        # Every level, not just the top: a subquery's `derivation` is what an
        # outer `s.derivation` passes through.
        if _target_name(node) == "derivation":
            self.derivations.append(node.val)


def _target_name(target: ast.ResTarget) -> str | None:
    """The column name PostgreSQL will give one select target.

    Only two forms count: an explicit ``AS`` alias, and a bare column
    reference, which takes its own name. Anything else -- an expression, a
    cast, ``*`` -- has no name this function will guess at, and the caller
    refuses it. Guessing is how ``NULL::text`` silently becomes a column
    called ``text``.
    """
    if target.name:
        return target.name
    value = target.val
    if isinstance(value, ast.ColumnRef) and value.fields:
        last = value.fields[-1]
        if isinstance(last, ast.String):
            return last.sval
    return None


def _leftmost_select(statement: ast.SelectStmt) -> ast.SelectStmt:
    """The branch a set operation takes its column names from.

    ``SELECT ... UNION ALL SELECT ...`` has no target list of its own;
    PostgreSQL names the result's columns after the leftmost branch, so that is
    the one the projection is checked against.
    """
    while statement.targetList is None and statement.larg is not None:
        statement = statement.larg
    return statement


def _check_projection(statement: ast.SelectStmt) -> None:
    targets = _leftmost_select(statement).targetList
    if not targets:
        raise ContractViolation("the statement projects no columns")

    names: list[str] = []
    for position, target in enumerate(targets, start=1):
        name = _target_name(target)
        if name is None:
            raise ContractViolation(
                f"column {position} has no name: give every projected column an "
                f"explicit alias (for example `NULL::text AS derivation`), and do "
                f"not use `SELECT *`"
            )
        names.append(name)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ContractViolation(f"column name(s) projected more than once: {duplicates}")

    expected = set(RESULT_COLUMNS)
    found = set(names)
    if found != expected:
        missing = sorted(expected - found)
        unexpected = sorted(found - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unexpected:
            detail.append(f"unexpected {unexpected}")
        raise ContractViolation(
            f"the projection must be exactly {list(RESULT_COLUMNS)}, in any order: "
            + "; ".join(detail)
        )


#: Type names a label may be cast to. ``NULL::text`` is the as-filed form.
_LABEL_TYPES = frozenset({"text", "varchar", "bpchar"})


def _is_label(node: ast.Node | None) -> bool:
    """Whether an expression can only ever produce a text label or NULL.

    A string literal, NULL, either cast to text, a CASE whose every branch is
    one of those, or a bare reference to an inner level's ``derivation``
    (checked where it is defined). Everything else is refused, including a
    number cast to text -- a label names what was computed, it is not the
    computation.
    """
    if isinstance(node, ast.A_Const):
        return node.isnull or isinstance(node.val, ast.String)
    if isinstance(node, ast.TypeCast):
        names = [n.sval for n in node.typeName.names if isinstance(n, ast.String)]
        return bool(names) and names[-1] in _LABEL_TYPES and _is_label(node.arg)
    if isinstance(node, ast.CaseExpr):
        results = [when.result for when in node.args or ()]
        if node.defresult is not None:
            results.append(node.defresult)
        return bool(results) and all(_is_label(r) for r in results)
    if isinstance(node, ast.ColumnRef) and node.fields:
        last = node.fields[-1]
        return isinstance(last, ast.String) and last.sval == "derivation"
    return False


def _check_derivation_is_a_label(inspector: _Inspector) -> None:
    """``derivation`` holds a name for what was computed, never the number.

    Measured on q010 ("how fast has NVIDIA's revenue grown"), 2026-09-25: the
    model put the growth rate itself in ``derivation`` and the as-filed figure
    in ``value``. The column names were all right, so the projection check
    passed, and the statement crashed building ``ResultRow`` after it ran.
    That is decidable from the text, so it is refused here instead.
    """
    if not all(_is_label(node) for node in inspector.derivations):
        raise ContractViolation(
            "`derivation` must be a short text label naming what was computed "
            "(for example 'yoy_growth'), or NULL::text on an as-filed row -- never "
            "a computed value. Put the computed number in `value` and name it in "
            "`derivation`"
        )


def _check_reads_the_view(inspector: _Inspector) -> None:
    """The statement must actually read the view.

    Measured, not hypothetical. Asked to write the whole statement from a
    table of coordinates, qwen2.5-coder:7b returned a ``UNION ALL`` of literal
    rows -- ``285000000000 AS value``, ``'Apple Inc.' AS entity_name`` -- with
    no ``FROM`` at all. Every other check passed: one statement, a SELECT, the
    right twelve columns, a LIMIT. The numbers were invented, and Apple's real
    FY2025 revenue is 416,161,000,000.

    A statement that never names the view cannot have got its values from the
    database, so this is decidable from the text alone. It is the cheapest
    guard in the file and it catches the worst failure the project has: a
    fluent, plausible, entirely fabricated figure.

    It does not catch *partial* fabrication -- a statement that reads the view
    and then overrides one column with a literal. Nothing here can; that is
    what ``execute()``'s attribution and row-count verdict are for.
    """
    reads = sum(
        1
        for schema, relation in inspector.relations
        if relation == VIEW_NAME and schema in (None, VIEW_SCHEMA)
    )
    if reads == 0:
        raise _out_of_role(
            "answering without reading the database",
            f"the statement never reads {VIEW_SCHEMA}.{VIEW_NAME}, so whatever it "
            f"returns was written into the SQL rather than looked up. Every value "
            f"must come from the relation",
        )


def _check_relations(inspector: _Inspector) -> None:
    for schema, relation in inspector.relations:
        if schema is None and relation in inspector.cte_names:
            continue
        if relation == VIEW_NAME and schema in (None, VIEW_SCHEMA):
            continue
        named = f"{schema}.{relation}" if schema else relation
        raise _out_of_role(
            f"a read of {named}",
            f"{named} cannot be read: the only relation available is "
            f"{VIEW_SCHEMA}.{VIEW_NAME}, which already applies the is_latest "
            f"filter and the company/concept joins",
        )


def _check_no_denied_calls(inspector: _Inspector) -> None:
    for function in inspector.functions:
        if function in DENIED_FUNCTIONS or function.startswith(DENIED_FUNCTION_PREFIXES):
            raise _out_of_role(
                f"a call to {function}()",
                f"{function}() is not allowed. Session settings such as "
                f"statement_timeout are USERSET, so a call that changes one is a "
                f"way around limits this layer is responsible for",
            )


def _limit_value(statement: ast.SelectStmt) -> int | None:
    """The statement's own row cap, or ``None`` when it has none.

    Raises when a ``LIMIT`` is present but is not a plain integer -- an
    expression cannot be checked against a ceiling without evaluating it, and
    evaluating it is the database's job, after this function has decided
    whether to let the statement run at all.
    """
    limit = statement.limitCount
    if limit is None:
        return None
    if statement.limitOption == LimitOption.LIMIT_OPTION_WITH_TIES:
        raise ContractViolation(
            "FETCH ... WITH TIES is not allowed: it returns however many rows tie "
            "at the cut-off, so its own count is not a cap"
        )
    if not isinstance(limit, ast.A_Const) or not isinstance(limit.val, ast.Integer):
        raise ContractViolation("LIMIT must be a plain integer")
    return limit.val.ival


def _parse_one(sql: str) -> ast.SelectStmt:
    """Parse, and return the single ``SELECT`` statement, or raise."""
    try:
        statements = parse_sql(sql)
    except ParseError as error:
        raise ContractViolation(f"the statement does not parse: {error}") from error

    if not statements:
        raise ContractViolation("no statement was given")
    if len(statements) > 1:
        raise _out_of_role(
            f"{len(statements)} statements in one execution",
            f"{len(statements)} statements were given; exactly one runs per execution",
        )

    statement = statements[0].stmt
    if not isinstance(statement, ast.SelectStmt):
        raise _out_of_role(
            type(statement).__name__,
            f"only SELECT runs here, and this is {type(statement).__name__}",
        )
    return statement


def validate(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Return the statement **unchanged**, or raise.

    Raises ``OutOfRole`` when the model tried to step outside what it is for
    (a session setting, another relation, a data-modifying statement). That
    one is also logged at WARNING, so it surfaces without anyone inspecting a
    return value. Raises ``ContractViolation`` for an ordinary mistake. Both
    are ``InvalidSQL``.

    Nothing is rewritten. What comes back is byte-identical to what went in,
    so ``execute()`` runs exactly the statement that was inspected.

    There used to be a ``min_view_reads`` here, demanding two reads of the
    relation for a plan with a Q4 in it, because the model would not write the
    subtraction and a single read returned the whole year. The view computes
    the fourth quarter now, so there is no subtraction to police.
    """
    if not sql or not sql.strip():
        raise ContractViolation("no statement was given")

    statement = _parse_one(sql)

    if statement.intoClause is not None:
        raise _out_of_role(
            "SELECT ... INTO",
            "SELECT ... INTO creates a table; only reads run here",
        )
    if statement.lockingClause:
        raise _out_of_role(
            "a locking clause",
            "a locking clause (FOR UPDATE / FOR SHARE) is not a read",
        )

    inspector = _Inspector()
    inspector(parse_sql(sql)[0])

    nested = sorted(set(inspector.statement_nodes) - ALLOWED_STATEMENT_NODES)
    if nested:
        raise _out_of_role(
            ", ".join(nested),
            f"the statement contains {nested}. A data-modifying statement inside a "
            f"CTE still modifies data, whatever the statement starts with",
        )

    _check_no_denied_calls(inspector)
    _check_relations(inspector)
    _check_reads_the_view(inspector)
    _check_projection(statement)
    _check_derivation_is_a_label(inspector)

    limit = _limit_value(statement)
    if limit is None:
        raise ContractViolation(
            f"the statement has no LIMIT. PostgreSQL has no per-role row cap, so one "
            f"has to be written into the statement; add LIMIT {max_rows} or less"
        )
    if limit > max_rows:
        raise ContractViolation(f"LIMIT {limit} exceeds the ceiling of {max_rows}")

    return sql
