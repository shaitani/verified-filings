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
5. Every relation named is the view or a CTE defined in the same statement.
   Redundant with the grant, and kept because an error here names the problem
   instead of surfacing as ``permission denied`` from a live connection.
6. The projection is exactly ``RESULT_COLUMNS``, in any order.
7. A ``LIMIT`` at or under ``max_rows``, appended when absent and **re-parsed
   to confirm it took**. ``FETCH ... WITH TIES`` is refused, because it returns
   more rows than its own count.

Every message is written to be shown to a model. Whether a rejected statement
is handed back for another attempt is an open decision -- see ``generator.py``.

See ``app/retrieval/DESIGN.md`` §7.
"""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.enums import LimitOption
from pglast.parser import ParseError
from pglast.visitors import Visitor

from app.schemas.result import RESULT_COLUMNS

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
        raise InvalidSQL("the statement projects no columns")

    names: list[str] = []
    for position, target in enumerate(targets, start=1):
        name = _target_name(target)
        if name is None:
            raise InvalidSQL(
                f"column {position} has no name: give every projected column an "
                f"explicit alias (for example `NULL::text AS derivation`), and do "
                f"not use `SELECT *`"
            )
        names.append(name)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise InvalidSQL(f"column name(s) projected more than once: {duplicates}")

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
        raise InvalidSQL(
            f"the projection must be exactly {list(RESULT_COLUMNS)}, in any order: "
            + "; ".join(detail)
        )


def _check_relations(inspector: _Inspector) -> None:
    for schema, relation in inspector.relations:
        if schema is None and relation in inspector.cte_names:
            continue
        if relation == VIEW_NAME and schema in (None, VIEW_SCHEMA):
            continue
        named = f"{schema}.{relation}" if schema else relation
        raise InvalidSQL(
            f"{named} cannot be read: the only relation available is "
            f"{VIEW_SCHEMA}.{VIEW_NAME}, which already applies the is_latest "
            f"filter and the company/concept joins"
        )


def _check_no_denied_calls(inspector: _Inspector) -> None:
    for function in inspector.functions:
        if function in DENIED_FUNCTIONS or function.startswith(DENIED_FUNCTION_PREFIXES):
            raise InvalidSQL(
                f"{function}() is not allowed. Session settings such as "
                f"statement_timeout are USERSET, so a call that changes one is a "
                f"way around limits this layer is responsible for"
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
        raise InvalidSQL(
            "FETCH ... WITH TIES is not allowed: it returns however many rows tie "
            "at the cut-off, so its own count is not a cap"
        )
    if not isinstance(limit, ast.A_Const) or not isinstance(limit.val, ast.Integer):
        raise InvalidSQL("LIMIT must be a plain integer")
    return limit.val.ival


def _parse_one(sql: str) -> ast.SelectStmt:
    """Parse, and return the single ``SELECT`` statement, or raise."""
    try:
        statements = parse_sql(sql)
    except ParseError as error:
        raise InvalidSQL(f"the statement does not parse: {error}") from error

    if not statements:
        raise InvalidSQL("no statement was given")
    if len(statements) > 1:
        raise InvalidSQL(
            f"{len(statements)} statements were given; exactly one runs per "
            f"execution"
        )

    statement = statements[0].stmt
    if not isinstance(statement, ast.SelectStmt):
        raise InvalidSQL(
            f"only SELECT runs here, and this is {type(statement).__name__}"
        )
    return statement


def validate(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Return the statement to execute, or raise ``InvalidSQL``.

    The returned string is not always the one passed in: a statement with no
    ``LIMIT`` gets one, because the cap cannot live anywhere else. Nothing else
    is rewritten -- a statement that needs changing to be safe is refused
    instead, so what runs is what was read.
    """
    if not sql or not sql.strip():
        raise InvalidSQL("no statement was given")

    statement = _parse_one(sql)

    if statement.intoClause is not None:
        raise InvalidSQL("SELECT ... INTO creates a table; only reads run here")
    if statement.lockingClause:
        raise InvalidSQL("a locking clause (FOR UPDATE / FOR SHARE) is not a read")

    inspector = _Inspector()
    inspector(parse_sql(sql)[0])

    nested = sorted(set(inspector.statement_nodes) - ALLOWED_STATEMENT_NODES)
    if nested:
        raise InvalidSQL(
            f"the statement contains {nested}. A data-modifying statement inside a "
            f"CTE still modifies data, whatever the statement starts with"
        )

    _check_no_denied_calls(inspector)
    _check_relations(inspector)
    _check_projection(statement)

    limit = _limit_value(statement)
    if limit is not None:
        if limit > max_rows:
            raise InvalidSQL(f"LIMIT {limit} exceeds the ceiling of {max_rows}")
        return sql.strip().rstrip(";").rstrip()

    # Appended on its own line so it cannot land inside a trailing `--` comment.
    capped = f"{sql.strip().rstrip(';').rstrip()}\nLIMIT {max_rows}"
    if _limit_value(_parse_one(capped)) != max_rows:
        raise InvalidSQL(
            "a LIMIT could not be applied to this statement; it is refused rather "
            "than run uncapped"
        )
    return capped
