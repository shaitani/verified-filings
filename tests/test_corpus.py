import pytest

from app.ingest.corpus import UnknownCompanyError, find_company, load_corpus


def test_corpus_has_exactly_twenty_companies():
    assert len(load_corpus()) == 20


def test_find_by_primary_ticker():
    company = find_company("AAPL")
    assert company["ticker"] == "AAPL"
    assert company["cik_padded"] == "0000320193"


def test_find_is_case_insensitive():
    assert find_company("aapl")["ticker"] == "AAPL"


def test_find_by_ticker_alias():
    # GOOG is an alias in all_tickers, not the primary ticker (GOOGL)
    company = find_company("GOOG")
    assert company["ticker"] == "GOOGL"


def test_find_by_cik():
    company = find_company("320193")
    assert company["ticker"] == "AAPL"


def test_unknown_identifier_raises():
    with pytest.raises(UnknownCompanyError):
        find_company("NOTACOMPANY")
