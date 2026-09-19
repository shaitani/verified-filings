"""Company name/ticker lookup: ``company_aliases.json`` and its index.

    from app.semantic.company_aliases import company_alias_index
    company_alias_index().lookup("Google")   # -> [1652044]

The companion to ``metric_aliases.py``, and the same shape of thing: derived
data read at the boundary so the resolver can speak the user's language. The
difference is provenance -- metric aliases are curated accounting judgment,
while this file is *derived from SEC data* by ``app/ingest/alias_index.py``
and refreshed whenever submissions are fetched. Nobody hand-edits it.

Read here rather than imported from ``app.ingest`` because the dependency
direction is ``semantic -> (schemas, db)``. The file path is the contract
between the two halves; neither imports the other.

**A hit is a candidate, not an answer.** The file covers the corpus, which is
not the same set as the companies actually loaded into this database, so the
resolver intersects every hit with ``Company.cik`` before using it. A cik that
resolves to no rows would produce an empty result indistinguishable from "this
company reported nothing" -- the failure mode the coverage check exists to
prevent, arriving one layer earlier.
"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from app.semantic.metric_aliases import lookup_keys

#: Beside ``sic_numbers.json`` at the project root. ``app/semantic`` ->
#: ``app`` -> repo root.
ALIAS_FILE = Path(__file__).resolve().parents[2] / "company_aliases.json"


class CompanyAliasIndex:
    """Normalized surface form -> the ciks it could mean.

    A list rather than a single cik because a short form can legitimately
    collide -- two filers both shortening to "national", say. Returning both
    lets the caller report an ambiguity instead of picking by dict order,
    which is the same rule the metric layer follows.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._by_form: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            cik = int(row["cik"].removeprefix("CIK"))
            for surface in (*row.get("names", ()), *row.get("tickers", ())):
                for key in lookup_keys(surface):
                    if cik not in self._by_form[key]:
                        self._by_form[key].append(cik)

    def lookup(self, text: str) -> list[int]:
        """Ciks whose name or ticker matches ``text`` exactly once folded.

        Exact rather than substring on purpose. The resolver already has a
        substring fallback; what this adds is the ability to say "Google" and
        "GOOG" and mean cik 1652044, and a substring rule would also say that
        "Apple" means "Apple Computer" *and* anything else containing it.
        """
        for key in lookup_keys(text):
            found = self._by_form.get(key)
            if found:
                return list(found)
        return []

    def __len__(self) -> int:
        return len(self._by_form)


def load_company_aliases(path: Path = ALIAS_FILE) -> CompanyAliasIndex:
    """Parse one alias file. A missing file yields an empty index rather than
    raising: it is a derived cache, and the resolver's own SQL lookup still
    works without it."""
    if not path.exists():
        return CompanyAliasIndex([])
    return CompanyAliasIndex(json.loads(path.read_text("utf-8")))


@lru_cache(maxsize=1)
def company_alias_index() -> CompanyAliasIndex:
    """The process-wide index, parsed once. Tests wanting a different file
    call ``load_company_aliases`` directly."""
    return load_company_aliases()
