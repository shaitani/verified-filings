"""Command-line entry point.

Each capability is exposed as a named subcommand ("action"), e.g.:

    sec-retriever get-submission AAPL
    sec-retriever get-submission AAPL MSFT GOOGL   # as few or as many as you like
    sec-retriever get-xbrl AAPL MSFT               # XBRL data -> data/xbrl/<TICKER>.json

Running via uv, without installing the entry point:

    uv run python -m app.cli get-submission AAPL MSFT

Every action runs against a single `SECClient` that the CLI owns for the
duration of the command. After the action completes, the CLI always
reports how many real network requests that client made versus how many
calls were served from the disk cache -- this applies to any action
following the convention documented in `app/ingest/actions.py`, not just
"get-submission", and it covers the whole batch of identifiers passed in
one call (since they all share the one client), not just one company.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.ingest import sic_index, xbrl_store
from app.ingest.actions import get_submissions, get_xbrl_data
from app.ingest.corpus import UnknownCompanyError
from app.ingest.sec_client import SECClient

# Every action must match this signature:
#   async def action(identifiers: list[str], *, client: SECClient | None = None) -> Any
ACTIONS: dict[str, Callable[..., Awaitable[Any]]] = {
    "get-submission": get_submissions,
    "get-xbrl": get_xbrl_data,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sec-retriever")
    subparsers = parser.add_subparsers(dest="action", required=True)

    get_submission_parser = subparsers.add_parser(
        "get-submission",
        help="Fetch the SEC submissions JSON for one or more corpus companies.",
    )
    get_submission_parser.add_argument(
        "identifiers",
        nargs="+",
        help="One or more company tickers (e.g. AAPL) or CIKs, from corpus_companies.json.",
    )

    get_xbrl_parser = subparsers.add_parser(
        "get-xbrl",
        help=(
            "Fetch SEC XBRL data for one or more corpus companies; writes the "
            "curated local store data/xbrl/<TICKER>.json (10-K/10-Q facts, most "
            "recent 5 fiscal years) for each and prints a manifest."
        ),
    )
    get_xbrl_parser.add_argument(
        "identifiers",
        nargs="+",
        help="One or more company tickers (e.g. AAPL) or CIKs, from corpus_companies.json.",
    )

    return parser


async def _run_action(action: str, identifiers: list[str]) -> tuple[Any, SECClient]:
    action_fn = ACTIONS[action]
    async with SECClient() as client:
        result = await action_fn(identifiers, client=client)
    return result, client


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.action not in ACTIONS:
        parser.error(f"unknown action: {args.action}")
        return 2

    try:
        result, client = asyncio.run(_run_action(args.action, args.identifiers))
    except UnknownCompanyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(
        f"[{args.action}] {client.request_count} network request(s) made, "
        f"{client.cache_hit_count} served from cache",
        file=sys.stderr,
    )

    if args.action == "get-submission":
        indexed = len(sic_index.load_sic_index())
        if indexed:
            noun = "company" if indexed == 1 else "companies"
            print(
                f"[{args.action}] sic_numbers.json now indexes {indexed} {noun}",
                file=sys.stderr,
            )

    if args.action == "get-xbrl":
        window_max = xbrl_store.FISCAL_YEAR_MAX
        window_min = window_max - xbrl_store.FISCAL_YEARS_KEPT + 1
        ahead = sorted(
            ticker
            for ticker, entry in result.items()
            if (entry.get("latest_complete_fy") or 0) > window_max
        )
        if ahead:
            newest = max(result[t]["latest_complete_fy"] for t in ahead)
            print(
                f"[{args.action}] store window is FY{window_min}-FY{window_max} for all "
                f"companies; {len(ahead)} now have a newer 10-K (to FY{newest}): "
                f"{', '.join(ahead)}. Bump xbrl_store.FISCAL_YEAR_MAX once every corpus "
                "company does, then re-run.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
