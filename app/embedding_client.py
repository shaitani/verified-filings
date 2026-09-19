"""Local embedding model client (nomic-embed-text-v1.5, served by the
"ollama" docker-compose service over its HTTP API).

This is the one place that knows how to turn text into a vector. Both the
offline concept-embedding job (``app/db/embedder.py``) and any future
query-time search code (the reserved ``app/retrieval/`` -- see
``app/db/DESIGN.md`` section 5) should call in here rather than talking to
Ollama directly.
"""

from __future__ import annotations

from ollama import AsyncClient

from app.config import settings

#: The model this project has standardized on. Its output width is fixed at
#: 768 -- see app.db.models.EMBEDDING_DIM, which Concept.embedding's
#: vector(...) column is sized to. Changing EMBEDDING_MODEL to a model with a
#: different output size means changing EMBEDDING_DIM too, and a migration to
#: resize that column -- the two constants must be kept in sync by hand.
EMBEDDING_MODEL = "nomic-embed-text"

#: nomic-embed-text-v1.5 is trained to receive a task prefix on every input,
#: and the Ollama model carries no template that adds one -- whatever is sent
#: reaches the model verbatim. Without a prefix the model reads a two-word
#: query and a 1.7k-char concept description as the same kind of text, which
#: costs real accuracy on the short-query-against-long-description lookups the
#: concept search is made of: measured on this corpus, "net income" ranked
#: NetIncomeLoss #5 unprefixed and #1 prefixed.
#:
#: SEARCH_DOCUMENT_PREFIX is applied by embedder.build_source_text() rather
#: than inside embed_texts(), so Concept.embedding_source_text stays literally
#: the string that was embedded -- which also means editing this constant
#: changes every source hash and makes the next embedder run rebuild the
#: corpus by itself.
SEARCH_DOCUMENT_PREFIX = "search_document: "
SEARCH_QUERY_PREFIX = "search_query: "


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed one or more already-prefixed strings, in one HTTP call.

    Low level: adds no task prefix of its own. Callers embedding a user's
    search phrase want ``embed_query()``; callers embedding corpus text are
    expected to have applied ``SEARCH_DOCUMENT_PREFIX`` themselves.

    Returned vectors are in the same order as ``texts``. Empty input returns
    an empty list without making a network call.
    """
    if not texts:
        return []
    client = AsyncClient(host=settings.embedding_url)
    response = await client.embed(model=EMBEDDING_MODEL, input=texts)
    return list(response.embeddings)


async def embed_query(text: str) -> list[float]:
    """Embed one search phrase for comparison against ``Concept.embedding``."""
    (vector,) = await embed_texts([SEARCH_QUERY_PREFIX + text])
    return vector
