"""Local embedding model client (nomic-embed-text-v1.5, served by the
"ollama" docker-compose service over its HTTP API).

This is the one place that knows how to turn text into a vector. Both the
offline concept-embedding job (``app/db/embedder.py``) and any future
query-time search code (the reserved ``app/retrieval/`` -- see
``app/db/DESIGN.md`` section 5) should call ``embed_texts()`` here rather
than talking to Ollama directly.
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


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed one or more strings with the local model, in one HTTP call.

    Returned vectors are in the same order as ``texts``. Empty input returns
    an empty list without making a network call.
    """
    if not texts:
        return []
    client = AsyncClient(host=settings.embedding_url)
    response = await client.embed(model=EMBEDDING_MODEL, input=texts)
    return list(response.embeddings)


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Convenience wrapper around ``embed_texts()``."""
    (vector,) = await embed_texts([text])
    return vector
