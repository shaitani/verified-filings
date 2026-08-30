"""Pydantic schemas -- the app-wide single source of truth for data shapes
(sec-retriever.md section 3).

One module per domain. Consumers import from the submodule, e.g.::

    from app.schemas.xbrl import CompanyFactsFile

Currently:

* ``xbrl`` -- validates one curated XBRL-data file (``data/xbrl/<TICKER>.json``)
  on the way in, before the load step turns it into ``app.db`` rows. See
  ``app/schemas/DESIGN.md``.
"""
