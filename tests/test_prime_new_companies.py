"""The new-company priming graph (app/prime_new_companies.py): its pure helpers,
and the graph's routing run end to end with every step stubbed -- no SEC, no
database, no model."""

from __future__ import annotations

import difflib
import json
from types import SimpleNamespace

import httpx
import pytest
from langgraph.types import RetryPolicy

from app import prime_new_companies as prime_module
from app.ingest.corpus import CORPUS_FILE
from app.prime_new_companies import (
    filing_problem,
    prime,
    resolve_identifiers,
    ticker_clash,
    write_corpus,
)

#: The shape of https://www.sec.gov/files/company_tickers.json, primary class first.
LISTING = {
    "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "1": {"cik_str": 1065280, "ticker": "NFLX", "title": "NETFLIX INC"},
    "2": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "3": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
}


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #


def test_a_ticker_becomes_a_corpus_entry() -> None:
    (entry,), dropped = resolve_identifiers(["nflx"], {"NFLX": "Netflix"}, LISTING)
    assert dropped == []
    assert entry == {
        "input_name": "Netflix",
        "title": "NETFLIX INC",
        "ticker": "NFLX",
        "cik": 1065280,
        "cik_padded": "0001065280",
        "all_tickers": ["NFLX"],
        "submissions_url": "https://data.sec.gov/submissions/CIK0001065280.json",
        "companyfacts_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001065280.json",
    }
    assert list(entry) == list(json.loads(CORPUS_FILE.read_text("utf-8"))["companies"][0])


def test_share_classes_and_ciks_are_one_company() -> None:
    """GOOG is Alphabet, whose primary class the SEC lists first."""
    companies, dropped = resolve_identifiers(["GOOG", "1652044", "CIK0001652044"], {}, LISTING)
    assert dropped == []
    (entry,) = companies
    assert entry["ticker"] == "GOOGL" and entry["all_tickers"] == ["GOOG", "GOOGL"]
    assert entry["input_name"] == "GOOG"  # what was typed, when no --as is given


def test_a_cik_records_the_sec_s_title_as_its_name() -> None:
    """A number is no one's name for a company, and the lexicon indexes it."""
    (entry,), _ = resolve_identifiers(["0001065280"], {}, LISTING)
    assert entry["input_name"] == "NETFLIX INC"


def test_a_dotted_class_ticker_is_the_sec_s_dashed_one() -> None:
    (entry,), _ = resolve_identifiers(["BRK.B"], {}, LISTING)
    assert entry["ticker"] == "BRK-B"


def test_an_unknown_input_is_dropped_with_its_reason() -> None:
    companies, dropped = resolve_identifiers(["ZZZZ", "999"], {}, LISTING)
    assert companies == []
    assert dropped == [
        "ZZZZ: not a ticker in the SEC's list",
        "999: CIK 999 has no ticker in the SEC's list",
    ]


# --------------------------------------------------------------------------- #
# vet
# --------------------------------------------------------------------------- #


def _submissions(*forms: str) -> dict:
    return {"filings": {"recent": {"form": list(forms)}}}


def test_a_domestic_filer_is_in_scope() -> None:
    assert filing_problem(_submissions("8-K", "10-Q", "4")) is None


def test_a_foreign_issuer_is_out_and_says_why() -> None:
    """HSBC: 20-F under IFRS, the reason it is a substitute in the corpus."""
    assert filing_problem(_submissions("20-F", "6-K")) == (
        "files 20-F as a foreign issuer, not 10-K/10-Q"
    )
    assert filing_problem(_submissions("8-K")) == "no 10-K or 10-Q among its recent filings"


def test_a_ticker_another_corpus_company_answers_to_is_refused() -> None:
    corpus = [{"ticker": "GOOGL", "cik": 1652044, "all_tickers": ["GOOG", "GOOGL"]}]
    newcomer = {"ticker": "GOOG", "cik": 1, "all_tickers": ["GOOG"]}
    assert ticker_clash(newcomer, corpus) == (
        "ticker already belongs to GOOGL (CIK 1652044) in the corpus"
    )
    assert ticker_clash({"ticker": "NFLX", "cik": 2, "all_tickers": ["NFLX"]}, corpus) is None


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def test_the_corpus_is_rewritten_byte_for_byte(tmp_path) -> None:
    """In the file's own format, so an append only inserts lines: no line of
    the file as it was is removed or rewritten."""
    original = CORPUS_FILE.read_bytes()
    copy = tmp_path / "corpus_companies.json"
    write_corpus(json.loads(original), copy)
    assert copy.read_bytes() == original

    document = json.loads(original)
    (entry,), _ = resolve_identifiers(["NFLX"], {}, LISTING)
    write_corpus({**document, "companies": [*document["companies"], entry]}, copy)
    lines = original.decode().splitlines(), copy.read_text("utf-8").splitlines()
    removed = [
        line
        for line in difflib.unified_diff(*lines, lineterm="")
        if line.startswith("-") and not line.startswith("---")
    ]
    assert removed == []
    assert json.loads(copy.read_bytes())["companies"][-1] == entry
    assert b"\r\n" not in copy.read_bytes()


# --------------------------------------------------------------------------- #
# The graph, with every step stubbed
# --------------------------------------------------------------------------- #

NFLX, _ = resolve_identifiers(["NFLX"], {}, LISTING)


@pytest.fixture
def steps(monkeypatch):
    """Every node replaced; ``calls`` records which ran. A test swaps one out."""
    calls: list[str] = []

    def stub(name, result, *, retried=False):
        @prime_module._step(name, retried=retried)
        async def node(state):
            calls.append(name)
            return result(state) if callable(result) else result

        monkeypatch.setattr(prime_module, name, node)

    stub("preflight", {})
    stub("resolve", {"companies": NFLX})
    stub("vet", {"companies": NFLX})
    stub("register", {})
    stub("fetch_submissions", {})
    stub("fetch_xbrl", {"manifests": {"NFLX": {"fiscal_years": [2021, 2025]}}})
    stub("load", {"loaded": {"NFLX": {"filings": 20, "facts": 5000, "dropped_unit": 0}}})
    stub("embed", lambda s: {"embedded": {"embedded": 9, "skipped": 1}, "embed_runs": 1})
    good = [{"name": "embedded", "ok": True, "detail": "all"}]
    stub("validate", {"checks": {"NFLX": good}})
    monkeypatch.setattr(
        prime_module,
        "SEC_RETRY",
        RetryPolicy(initial_interval=0.01, retry_on=prime_module._transient),
    )
    return SimpleNamespace(calls=calls, stub=stub)


async def _yes(_prompt: str) -> str:
    return "y"


async def test_approved_runs_every_step_and_reports(steps) -> None:
    state, code = await prime(["NFLX"], {}, ask=_yes)
    assert code == 0
    assert steps.calls == [
        "preflight", "resolve", "vet", "register", "fetch_submissions",
        "fetch_xbrl", "load", "embed", "validate",
    ]  # fmt: skip
    assert state["summary"][0] == "NFLX  NETFLIX INC  CIK 1065280"
    assert "  FY2021-FY2025, 20 filings, 5,000 facts" in state["summary"]


async def test_declining_writes_nothing(steps) -> None:
    async def no(_prompt: str) -> str:
        return "n"

    state, code = await prime(["NFLX"], {}, ask=no)
    assert (code, state["summary"]) == (0, ["Nothing written."])
    assert "register" not in steps.calls


async def test_a_dry_run_stops_at_the_plan(steps) -> None:
    async def never(_prompt: str) -> str:
        raise AssertionError("a dry run asks nothing")

    _, code = await prime(["NFLX"], {}, dry_run=True, ask=never)
    assert code == 0 and steps.calls == ["preflight", "resolve", "vet"]


async def test_everything_already_loaded_goes_straight_to_report(steps) -> None:
    steps.stub("vet", {"companies": [], "already_loaded": ["NFLX"]})

    async def never(_prompt: str) -> str:
        raise AssertionError("nothing to confirm")

    state, code = await prime(["NFLX"], {}, ask=never)
    assert code == 0 and steps.calls[-1] == "vet"
    assert state["summary"] == ["Already loaded, skipped: NFLX"]


async def test_stale_embeddings_go_back_to_embed_once(steps) -> None:
    stale = [{"name": "embedded", "ok": False, "detail": "3 of 5"}]
    steps.stub("embed", lambda s: {"embed_runs": s.get("embed_runs", 0) + 1})
    steps.stub("validate", {"checks": {"NFLX": stale}})

    state, code = await prime(["NFLX"], {}, ask=_yes)
    assert steps.calls[-4:] == ["embed", "validate", "embed", "validate"]
    assert code == 1 and "  FAIL  embedded: 3 of 5" in state["summary"]


async def test_a_dropped_connection_to_the_sec_is_retried(steps) -> None:
    attempts = []

    def flaky(state):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("reset")
        return {"manifests": {"NFLX": {"fiscal_years": [2021, 2025]}}}

    steps.stub("fetch_xbrl", flaky, retried=True)
    _, code = await prime(["NFLX"], {}, ask=_yes)
    assert code == 0 and len(attempts) == 2


async def test_a_failure_stops_at_report_and_says_how_to_resume(steps) -> None:
    def broken(state):
        raise RuntimeError("NFLX failed (bad file); loaded first: none")

    steps.stub("load", broken)
    state, code = await prime(["NFLX"], {}, ask=_yes)
    assert code == 1 and "embed" not in steps.calls
    assert "Stopped at load: NFLX failed (bad file); loaded first: none" in state["summary"]
