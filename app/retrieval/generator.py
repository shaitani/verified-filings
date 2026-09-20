"""``generate(plan)`` -- ask Qwen for SQL. No database.

NOT BUILT. Ollama serves Qwen alongside the embedding model
(``docker-compose.yml``), and ``app/embedding_client.py`` is the pattern for
talking to it.

**When the model is chosen, add its tag to the pull line in
``docker-compose.yml``** -- the ``ollama`` service pulls its models on startup
because ``docker compose down -v`` wipes the model volume along with the
database ones. A model that only ever arrives by someone running ``ollama
pull`` by hand is the step that goes missing after a wipe, and it fails as a
connection-level error that looks nothing like a missing model. See
``BOOTSTRAP.md``.

What it owes, when it is written:

* **Return the statement and nothing else.** A model that answers with prose
  around a fenced block has not failed -- extracting it is this function's
  job, not ``validate()``'s.
* **No credential, ever.** The separation this whole layer rests on is that
  Qwen takes text and returns text. It has no driver, no connection string and
  no route to PostgreSQL.
* **A refusal is an outcome.** If the model will not produce a statement, that
  is a refusal to pass on, not an exception to swallow.

Whether a rejected statement gets handed back to the model with the
``InvalidSQL`` message and one more attempt is **undecided**. It is attractive
-- the messages are written to be shown to a model -- but a retry loop is also
how a validator's error text becomes a map of what to get around. Decide it
deliberately.
"""

from __future__ import annotations

from app.schemas.query import QueryPlan


def generate(plan: QueryPlan) -> str:
    """Ask Qwen for the SQL that answers ``plan``, and return the statement."""
    raise NotImplementedError(
        "generate is not written yet; Ollama serves Qwen, and "
        "app/embedding_client.py is the pattern for reaching it"
    )
