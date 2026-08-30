"""CLI-level tests.

The `get-submission` happy paths are exercised via `_run_action` directly
with `app.cli.SECClient` monkeypatched to a client backed by
`httpx.MockTransport` -- no real request reaches data.sec.gov. The
unknown-identifier error path is tested through `main()` itself, since it
never gets far enough to touch the network (the corpus lookup fails
first, before any client is even used).
"""

import httpx
import pytest

from app import cli
from app.ingest.sec_client import SECClient


@pytest.fixture(autouse=True)
def _isolated_rate_limiter():
    SECClient.reset_shared_rate_limiter()
    yield
    SECClient.reset_shared_rate_limiter()


@pytest.mark.asyncio
async def test_run_action_single_identifier(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cik": "0000320193"})

    def fake_sec_client(**_kwargs):
        return SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))

    # app.cli calls `SECClient()` directly; patch the name as bound in the
    # cli module so `_run_action` picks up the mocked transport.
    monkeypatch.setattr(cli, "SECClient", fake_sec_client)

    result, client = await cli._run_action("get-submission", ["AAPL"])

    assert result == {"AAPL": {"cik": "0000320193"}}
    assert client.request_count == 1
    assert client.cache_hit_count == 0


@pytest.mark.asyncio
async def test_run_action_reports_counts_across_a_batch(tmp_path, monkeypatch):
    """The '5 companies, 2 cached + 3 fetched' scenario, end to end
    through the CLI's action-running path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    shared_client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cli, "SECClient", lambda **_kwargs: shared_client)

    # Pre-warm 2 of the 5 identifiers we'll batch-request next.
    async with shared_client:
        from app.ingest.actions import get_submissions

        await get_submissions(["AAPL", "MSFT"], client=shared_client)

    result, client = await cli._run_action(
        "get-submission", ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"]
    )

    assert set(result.keys()) == {"AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"}
    assert client is shared_client
    assert client.cache_hit_count == 2  # AAPL, MSFT from the pre-warm
    assert client.request_count == 2 + 3  # the pre-warm's 2 + this batch's 3 new


def test_main_rejects_unknown_identifier(capsys):
    exit_code = cli.main(["get-submission", "NOTACOMPANY"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "did not match any of the 20 corpus companies" in captured.err


def test_main_accepts_a_single_identifier_argument():
    # nargs="+" must still accept exactly one identifier, not just 2+.
    parser = cli._build_parser()
    args = parser.parse_args(["get-submission", "AAPL"])
    assert args.identifiers == ["AAPL"]


def test_main_accepts_several_identifier_arguments():
    parser = cli._build_parser()
    args = parser.parse_args(["get-submission", "AAPL", "MSFT", "GOOGL"])
    assert args.identifiers == ["AAPL", "MSFT", "GOOGL"]


def test_build_parser_get_xbrl_accepts_one_and_many_identifiers():
    parser = cli._build_parser()
    assert parser.parse_args(["get-xbrl", "AAPL"]).identifiers == ["AAPL"]
    assert parser.parse_args(["get-xbrl", "AAPL", "MSFT"]).identifiers == ["AAPL", "MSFT"]


_XBRL_PAYLOAD = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "val": 2,
                            "accn": "x-24",
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-01",
                        }
                    ]
                },
            }
        }
    },
}


@pytest.mark.asyncio
async def test_run_action_get_xbrl_writes_the_store(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_XBRL_PAYLOAD)

    monkeypatch.setattr(
        cli,
        "SECClient",
        lambda **_kwargs: SECClient(
            cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler)
        ),
    )

    result, client = await cli._run_action("get-xbrl", ["AAPL"])

    assert result["AAPL"]["facts"] == 1
    assert client.request_count == 1

    from app.ingest.xbrl_store import load_store

    assert load_store("AAPL")["ticker"] == "AAPL"


def test_main_get_xbrl_prints_manifest_and_no_sic_line(capsys, tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_XBRL_PAYLOAD)

    monkeypatch.setattr(
        cli,
        "SECClient",
        lambda **_kwargs: SECClient(
            cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler)
        ),
    )

    exit_code = cli.main(["get-xbrl", "AAPL"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert '"AAPL"' in captured.out
    assert "network request(s) made" in captured.err
    assert "sic_numbers.json" not in captured.err
    # _XBRL_PAYLOAD's newest 10-K is fy 2024, inside the window -> no roll note.
    assert "store window is" not in captured.err


_XBRL_PAYLOAD_AHEAD = {
    "cik": 789019,
    "entityName": "MICROSOFT CORP",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "start": "2024-07-01",
                            "end": "2025-06-30",
                            "val": 1,
                            "accn": "m-25",
                            "fy": 2025,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-07-30",
                        },
                        {
                            "start": "2025-07-01",
                            "end": "2026-06-30",
                            "val": 2,
                            "accn": "m-26",
                            "fy": 2026,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2026-07-30",
                        },
                    ]
                },
            }
        }
    },
}


def test_main_get_xbrl_flags_a_company_past_the_fixed_window(capsys, tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_XBRL_PAYLOAD_AHEAD)

    monkeypatch.setattr(
        cli,
        "SECClient",
        lambda **_kwargs: SECClient(
            cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler)
        ),
    )

    assert cli.main(["get-xbrl", "MSFT"]) == 0
    err = capsys.readouterr().err
    assert "store window is FY2021-FY2025" in err
    assert "FY2026" in err
    assert "MSFT" in err
