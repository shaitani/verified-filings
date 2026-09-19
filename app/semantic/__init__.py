"""Semantic layer: turning a user's question into something the database can
answer -- intent, metric resolution, sector detection (`sec-retriever.md` §3).

Import from the submodule (``from app.semantic.query_mapper import map_query``);
this file stays a docstring only, same convention as ``app/schemas``.

Design notes: ``app/semantic/DESIGN.md``.
"""
