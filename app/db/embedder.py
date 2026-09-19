"""Keep Concept.embedding in step with Concept.label / Concept.description.

    python -m app.db.embedder

Standalone entry point -- not part of app/db/loader.py. The loader writes
Concept rows scoped to one company's file; this walks the *global* Concept
table (shared by every company) and can be re-run anytime, independent of any
load happening. See app/db/models.py for the exact source-text/hash rule this
implements, and app/embedding_client.py for how the actual embedding call is
made.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Concept
from app.db.session import SessionLocal
from app.embedding_client import SEARCH_DOCUMENT_PREFIX, embed_texts

# --------------------------------------------------------------------------- #
# Pure transform: a Concept -> the text to embed and its hash. No I/O, so this
# half is unit-testable without a database or the embedding service.
# --------------------------------------------------------------------------- #

#: Matches the boundary just before an uppercase letter that follows a
#: lowercase letter or digit -- e.g. "NetIncomeLoss" -> ["Net", "Income",
#: "Loss"]. Doesn't split consecutive capitals ("EPS" stays "EPS").
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def humanize_name(name: str) -> str:
    """"NetIncomeLoss" -> "Net Income Loss".

    Used only as the last-resort fallback in build_source_text(), for
    concepts (mostly dei/srt) that have neither a label nor a description --
    so the embedding model never sees a raw camelCase identifier.
    """
    return _CAMEL_BOUNDARY.sub(" ", name)


def build_source_text(concept: Concept) -> str:
    """The exact string that gets embedded for one concept, task prefix included.

    Mirrors the rule documented on Concept.embedding_source_text in
    app/db/models.py -- keep the two in sync if this changes.
    """
    if concept.label and concept.description:
        body = f"{concept.label}. {concept.description}"
    elif concept.label or concept.description:
        body = concept.label or concept.description
    else:
        body = humanize_name(concept.name)
    return SEARCH_DOCUMENT_PREFIX + body


def source_hash(text: str) -> str:
    """sha256 hex digest of ``text`` -- what's stored in
    Concept.embedding_source_hash to detect a stale embedding cheaply."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@dataclass
class _Stale:
    id: int
    source_text: str
    source_hash: str
    vector: list[float] | None = None  # filled in after the embed call


@dataclass
class EmbedResult:
    checked: int
    embedded: int
    skipped: int  # already up to date -- hash matched, nothing sent to Ollama


async def _find_stale(session: AsyncSession) -> tuple[int, list[_Stale]]:
    """Read every Concept and decide which ones need (re-)embedding.

    A pure read -- no writes here, so this can run in a short-lived session
    that closes well before any embedding call is made.
    """
    concepts = (await session.execute(select(Concept))).scalars().all()
    stale = []
    for concept in concepts:
        text = build_source_text(concept)
        digest = source_hash(text)
        if digest != concept.embedding_source_hash:
            stale.append(_Stale(id=concept.id, source_text=text, source_hash=digest))
    return len(concepts), stale


async def embed_stale_concepts(
    *, session_factory: async_sessionmaker = SessionLocal, batch_size: int = 64
) -> EmbedResult:
    """Re-embed every Concept whose source text changed (or has no embedding
    yet); leave the rest untouched.

    Cheap to re-run on a corpus that hasn't changed: every concept costs one
    hash compare, not a re-embed, and nothing is sent to Ollama at all.

    Three phases, deliberately not one big transaction:
      1. read every Concept and figure out what's stale (session closed after)
      2. call Ollama in batches -- no Postgres transaction held open while
         waiting on the network, however long that takes
      3. write the results back in one final transaction
    """
    async with session_factory() as session:
        total, stale = await _find_stale(session)

    for start in range(0, len(stale), batch_size):
        batch = stale[start : start + batch_size]
        vectors = await embed_texts([item.source_text for item in batch])
        for item, vector in zip(batch, vectors, strict=True):
            item.vector = vector

    if stale:
        async with session_factory.begin() as session:
            for item in stale:
                await session.execute(
                    update(Concept)
                    .where(Concept.id == item.id)
                    .values(
                        embedding=item.vector,
                        embedding_source_text=item.source_text,
                        embedding_source_hash=item.source_hash,
                    )
                )

    return EmbedResult(checked=total, embedded=len(stale), skipped=total - len(stale))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.embedder")
    parser.parse_args(argv)  # no arguments yet -- always checks every concept

    result = asyncio.run(embed_stale_concepts())
    print(
        f"[embed] {result.checked} concepts checked, {result.embedded} embedded, "
        f"{result.skipped} already up to date",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
