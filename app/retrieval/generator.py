"""``generate(plan)`` -- ask Qwen for SQL. No database.

NOT BUILT. Ollama serves Qwen alongside the embedding model
(``docker-compose.yml``), and ``app/embedding_client.py`` is the pattern for
talking to it.

``GENERATION_MODEL`` below is authoritative. **It is also named in
``docker-compose.yml``'s pull line and health check, and the two have to be
kept in step** -- the ``ollama`` service pulls its models on startup because
``docker compose down -v`` wipes the model volume along with the database
ones, and a model that only ever arrives by someone running ``ollama pull`` by
hand is the step that goes missing after a wipe. See ``BOOTSTRAP.md``.

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

#: The model that writes the SQL. Mirrored in ``docker-compose.yml``'s pull
#: line and health check -- this constant is authoritative, that is the copy.
#:
#: qwen2.5-coder:7b rather than something larger, because the task is narrow:
#: one relation, the plan's coordinates handed over as a literal VALUES list,
#: and a fixed twelve-column projection that ``validate()`` refuses any
#: deviation from. A weaker model therefore degrades into refusals with
#: specific messages, not into plausible wrong numbers -- which is what makes
#: starting small cheap to be wrong about. Measured on a GTX 1080 Ti: loads
#: 100% onto the card at 4.7GB and generates ~48 tok/s.
GENERATION_MODEL = "qwen2.5-coder:7b"


def generate(plan: QueryPlan) -> str:
    """Ask Qwen for the SQL that answers ``plan``, and return the statement."""
    raise NotImplementedError(
        "generate is not written yet; Ollama serves Qwen, and "
        "app/embedding_client.py is the pattern for reaching it"
    )
