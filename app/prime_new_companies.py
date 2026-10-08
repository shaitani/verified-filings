"""Add companies to the corpus and prime them: a LangGraph state machine.

    uv run python -m app.prime_new_companies NFLX ADBE --as NFLX=Netflix
    uv run python -m app.prime_new_companies NFLX --dry-run   # show the plan, write nothing
    uv run python -m app.prime_new_companies --diagram        # print the graph as Mermaid

Owner's tool, development machine only. It writes ``corpus_companies.json``, the
two files get-submission keeps (``sic_numbers.json``, ``company_aliases.json``),
``data/xbrl/`` and the dev database; production has its own database and is
updated separately (docs/STARTUP.md, Production).

    preflight          dev database at head, the embedding model up, production stopped
    resolve            each ticker or CIK to one SEC company, from the SEC's ticker list
    vet                drops what is already loaded, and filers without 10-K/10-Q
    confirm            shows the plan and waits for yes -- nothing is written before it
    register           adds the new entries to corpus_companies.json
    fetch_submissions  get-submission: sic_numbers.json and company_aliases.json
    fetch_xbrl         get-xbrl: data/xbrl/<TICKER>.json
    load               the loader, for these companies only
    embed              the embedder: every concept whose text is new or changed
    validate           checks each company's data; stale embeddings go back to embed once
    report             one block per company

Every step calls the code its manual command runs and is safe to repeat, so a run
that stops is resumed by running it again: a company counts as done once it has a
``load_run``, and anything short of that goes through every step. A failure ends
at ``report``, which says what was done and where it stopped.

Names: a ticker or CIK resolves to exactly one company, so no name is guessed.
``--as TICKER=NAME`` records the name people use ("Netflix") as the entry's
``input_name``, which the company lexicon indexes; without it, what was typed is
recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import operator
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, TypedDict

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt
from sqlalchemy import func, select

from app.db import Concept, Fact, LoadRun
from app.db.embedder import build_source_text, embed_stale_concepts, source_hash
from app.db.loader import LoadBatchError, load_batch
from app.db.session import RetrievalSessionLocal, SessionLocal
from app.db.views import reported_fact
from app.embedding_client import SEARCH_DOCUMENT_PREFIX, embed_texts
from app.ingest.actions import get_submissions, get_xbrl_data
from app.ingest.corpus import CORPUS_FILE, load_corpus
from app.ingest.sec_client import SECClient, submissions_url, xbrl_data_url
from app.ingest.xbrl_store import FISCAL_YEAR_MAX, FISCAL_YEARS_KEPT, FORMS_IN_SCOPE
from app.schemas.query import QueryIn
from app.semantic.company_aliases import company_alias_index
from app.semantic.query_mapper import map_query

ROOT = CORPUS_FILE.parent
PROD_COMPOSE_FILE = ROOT / "docker-compose.prod.yml"

#: Every ticker the SEC knows, with its CIK and title: what the corpus was built from.
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

#: Forms a foreign private issuer files instead of 10-K/10-Q, under IFRS (HSBC).
FOREIGN_FORMS = ("20-F", "40-F")

#: The embed step, and one more if validate finds a concept left stale.
EMBED_RUNS = 2

#: What validate asks the mapper for: a figure every filer reports, banks included.
PROBE_METRIC = "assets"


class PrimeState(TypedDict, total=False):
    requested: list[str]  # tickers or CIKs, as typed
    names: dict[str, str]  # TICKER -> the name people use (--as)
    companies: list[dict[str, Any]]  # corpus entries this run primes
    already_loaded: list[str]  # tickers vet skipped
    dropped: Annotated[list[str], operator.add]  # "<input>: why", from resolve and vet
    approved: bool
    manifests: dict[str, dict[str, Any]]  # ticker -> get-xbrl's manifest entry
    loaded: dict[str, dict[str, int]]  # ticker -> filings, facts, dropped_unit
    embedded: dict[str, int]  # checked, embedded, skipped
    embed_runs: int
    checks: dict[str, list[dict[str, Any]]]  # ticker -> [{name, ok, detail}]
    warnings: Annotated[list[str], operator.add]
    log: Annotated[list[str], operator.add]  # one line per finished step, printed live
    summary: list[str]  # what report prints
    failed: str  # "<node>: <why>" -- set only by the node that stopped the run


# --------------------------------------------------------------------------- #
# Failure and retry
# --------------------------------------------------------------------------- #


def _transient(exc: Exception) -> bool:
    """A dropped connection, a timeout, or the SEC saying "later" (429, 5xx)."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


#: On the steps that call the SEC. Anything else will not change by asking again.
SEC_RETRY = RetryPolicy(max_attempts=3, initial_interval=2.0, retry_on=_transient)

Node = Callable[[PrimeState], Awaitable[dict[str, Any]]]


def _step(name: str, *, retried: bool = False) -> Callable[[Node], Node]:
    """Turn a failure into ``failed``, so the edge out of the node goes to report.
    On a retried step a transient error is raised instead, for ``SEC_RETRY``;
    past its last attempt it ends the run (``prime``)."""

    def decorate(fn: Node) -> Node:
        @wraps(fn)
        async def node(state: PrimeState) -> dict[str, Any]:
            try:
                return await fn(state)
            except Exception as exc:
                if retried and _transient(exc):
                    raise
                return {"failed": f"{name}: {exc}"}

        return node

    return decorate


async def _run(*command: str) -> str:
    """Run a command from the repository root; its output, or an error naming it."""
    done = await asyncio.to_thread(
        subprocess.run, command, cwd=ROOT, capture_output=True, text=True
    )
    if done.returncode:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"`{' '.join(command)}` exited {done.returncode}: {tail[0]}")
    return done.stdout + done.stderr


def _tickers(state: PrimeState) -> list[str]:
    return [company["ticker"] for company in state.get("companies", [])]


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


@_step("preflight")
async def preflight(state: PrimeState) -> dict[str, Any]:
    running = await _run(
        "docker", "compose", "-f", str(PROD_COMPOSE_FILE), "ps", "-q", "--status", "running"
    )
    if running.strip():
        raise RuntimeError("production is running; dev and production never run together")
    if "(head)" not in await _run(sys.executable, "-m", "alembic", "current"):
        raise RuntimeError("the dev database is behind: run `uv run alembic upgrade head`")
    await embed_texts([SEARCH_DOCUMENT_PREFIX + "preflight"])  # the model answers
    return {"log": ["preflight: dev database at head, embedding model up, production stopped"]}


@_step("resolve", retried=True)
async def resolve(state: PrimeState) -> dict[str, Any]:
    async with SECClient() as client:
        # Refetched each run: the disk cache never expires, and tickers move.
        listing = await client.get_json(SEC_TICKERS_URL, force_refresh=True)
    companies, dropped = resolve_identifiers(state["requested"], state.get("names", {}), listing)
    return {
        "companies": companies,
        "dropped": dropped,
        "log": [f"resolve: {len(companies)} resolved, {len(dropped)} not"],
    }


def resolve_identifiers(
    requested: list[str], names: dict[str, str], listing: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each ticker or CIK as a ``corpus_companies.json`` entry, or the reason not.

    The SEC lists a company once per ticker, its primary class first; the
    entry's ``ticker`` is that first one and ``all_tickers`` every class, so
    "GOOG" and "GOOGL" are one company.
    """
    rows = list(listing.values())
    by_ticker = {row["ticker"].upper(): row for row in rows}
    tickers_of: dict[int, list[str]] = {}
    title_of: dict[int, str] = {}
    for row in rows:
        tickers_of.setdefault(row["cik_str"], []).append(row["ticker"])
        title_of.setdefault(row["cik_str"], row["title"])

    companies: list[dict[str, Any]] = []
    dropped: list[str] = []
    for identifier in requested:
        key = identifier.strip().upper()
        typed_cik = key.removeprefix("CIK").isdigit()
        if typed_cik:
            cik = int(key.removeprefix("CIK"))
        elif key.replace(".", "-") in by_ticker:  # BRK.B is BRK-B to the SEC
            cik = by_ticker[key.replace(".", "-")]["cik_str"]
        else:
            dropped.append(f"{identifier}: not a ticker in the SEC's list")
            continue
        if cik not in tickers_of:
            dropped.append(f"{identifier}: CIK {cik} has no ticker in the SEC's list")
            continue
        if any(company["cik"] == cik for company in companies):
            continue  # the same company asked for twice
        ticker = tickers_of[cik][0]
        padded = f"{cik:010d}"
        # The name people use: --as, else the ticker typed. A CIK is no one's
        # name for a company, so it records the SEC's title instead.
        typed = title_of[cik] if typed_cik else identifier.strip()
        companies.append(
            {
                "input_name": names.get(ticker) or names.get(key) or typed,
                "title": title_of[cik],
                "ticker": ticker,
                "cik": cik,
                "cik_padded": padded,
                "all_tickers": sorted(tickers_of[cik]),
                "submissions_url": submissions_url(padded),
                "companyfacts_url": xbrl_data_url(padded),
            }
        )
    return companies, dropped


@_step("vet", retried=True)
async def vet(state: PrimeState) -> dict[str, Any]:
    corpus = load_corpus()
    ciks = [company["cik"] for company in state["companies"]]
    async with SessionLocal() as session:
        loaded = set(
            await session.scalars(select(LoadRun.company_cik).where(LoadRun.company_cik.in_(ciks)))
        )

    kept: list[dict[str, Any]] = []
    already: list[str] = []
    dropped: list[str] = []
    async with SECClient() as client:
        for company in state["companies"]:
            if company["cik"] in loaded:
                already.append(company["ticker"])
                continue
            listed = next((entry for entry in corpus if entry["cik"] == company["cik"]), None)
            clash = None if listed else ticker_clash(company, corpus)
            if clash:
                dropped.append(f"{company['ticker']}: {clash}")
                continue
            # Cached on disk, so fetch_submissions reads it without asking again.
            problem = filing_problem(await client.get_json(company["submissions_url"]))
            if problem:
                dropped.append(f"{company['ticker']}: {problem}")
                continue
            kept.append(listed or company)  # a listed company keeps the entry it has
    return {
        "companies": kept,
        "already_loaded": already,
        "dropped": dropped,
        "log": [
            f"vet: {len(kept)} to prime, {len(already)} already loaded, {len(dropped)} dropped"
        ],
    }


def ticker_clash(company: dict[str, Any], corpus: list[dict[str, Any]]) -> str | None:
    """A ticker some other corpus company already answers to. ``find_company``
    returns the first match, so a second owner would never be reached."""
    mine = {company["ticker"], *company["all_tickers"]}
    for entry in corpus:
        if mine & {entry["ticker"], *entry.get("all_tickers", [])}:
            return f"ticker already belongs to {entry['ticker']} (CIK {entry['cik']}) in the corpus"
    return None


def filing_problem(submissions: dict[str, Any]) -> str | None:
    """Why a company is out of scope, from the forms it filed recently -- the
    SEC's ``recent`` list holds at least a year of them. None when it is in."""
    forms = set(submissions.get("filings", {}).get("recent", {}).get("form", []))
    if forms & set(FORMS_IN_SCOPE):
        return None
    foreign = sorted(forms & set(FOREIGN_FORMS))
    if foreign:
        return f"files {' and '.join(foreign)} as a foreign issuer, not 10-K/10-Q"
    return "no 10-K or 10-Q among its recent filings"


async def confirm(state: PrimeState) -> dict[str, Any]:
    """The one pause. ``prime`` prints the plan and resumes with the answer.
    Not wrapped in ``_step``: ``interrupt`` works by raising."""
    answer = interrupt(
        {
            "companies": state["companies"],
            "already_loaded": state.get("already_loaded", []),
            "dropped": state.get("dropped", []),
        }
    )
    return {"approved": bool(answer)}


@_step("register")
async def register(state: PrimeState) -> dict[str, Any]:
    document = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    listed = {entry["cik"] for entry in document["companies"]}
    new = [company for company in state["companies"] if company["cik"] not in listed]
    if new:
        write_corpus(
            {
                **document,
                "companies": [*document["companies"], *new],
                "resolved_count": len(document["companies"]) + len(new),
            }
        )
    added = ", ".join(company["ticker"] for company in new) or "nothing new"
    return {"log": [f"register: corpus_companies.json, added {added}"]}


def write_corpus(document: dict[str, Any], path: Path = CORPUS_FILE) -> None:
    """In the file's own format: two-space indent, non-ASCII escaped, no final
    newline, LF -- so an append changes only the lines it adds."""
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=True), encoding="utf-8", newline="\n"
    )


@_step("fetch_submissions", retried=True)
async def fetch_submissions(state: PrimeState) -> dict[str, Any]:
    await get_submissions(_tickers(state))
    return {"log": ["fetch_submissions: sic_numbers.json and company_aliases.json updated"]}


@_step("fetch_xbrl", retried=True)
async def fetch_xbrl(state: PrimeState) -> dict[str, Any]:
    manifest = await get_xbrl_data(_tickers(state))
    window = range(FISCAL_YEAR_MAX - FISCAL_YEARS_KEPT + 1, FISCAL_YEAR_MAX + 1)
    warnings = []
    for ticker, entry in manifest.items():
        missing = sorted(set(window) - set(entry["fiscal_years"]))
        if missing:
            years = ", ".join(f"FY{year}" for year in missing)
            warnings.append(
                f"{ticker}: nothing on file for {years} (the window is "
                f"FY{window[0]}-FY{window[-1]}); its answers will say so"
            )
        newest = entry.get("latest_complete_fy") or 0
        if newest > FISCAL_YEAR_MAX:
            warnings.append(f"{ticker}: has filed a 10-K for FY{newest}, past the window")
    return {
        "manifests": manifest,
        "warnings": warnings,
        "log": [f"fetch_xbrl: {len(manifest)} curated file(s) in data/xbrl/"],
    }


@_step("load")
async def load(state: PrimeState) -> dict[str, Any]:
    try:
        results = await load_batch(_tickers(state))
    except LoadBatchError as exc:
        done = ", ".join(exc.completed) or "none"
        raise RuntimeError(
            f"{exc.failed_ticker} failed ({exc.__cause__}); loaded first: {done}"
        ) from exc
    loaded = {
        ticker: {"filings": r.filings, "facts": r.facts, "dropped_unit": r.dropped_unit}
        for ticker, r in results.items()
    }
    facts = sum(entry["facts"] for entry in loaded.values())
    return {"loaded": loaded, "log": [f"load: {len(loaded)} companies, {facts:,} facts"]}


@_step("embed")
async def embed(state: PrimeState) -> dict[str, Any]:
    result = await embed_stale_concepts()
    return {
        "embedded": {
            "checked": result.checked,
            "embedded": result.embedded,
            "skipped": result.skipped,
        },
        "embed_runs": state.get("embed_runs", 0) + 1,
        "log": [f"embed: {result.embedded} embedded, {result.skipped} already up to date"],
    }


@_step("validate")
async def validate(state: PrimeState) -> dict[str, Any]:
    company_alias_index.cache_clear()  # fetch_submissions rewrote the lexicon file
    checks = {
        company["ticker"]: await company_checks(company, state["loaded"][company["ticker"]])
        for company in state["companies"]
    }
    failing = sum(not check["ok"] for found in checks.values() for check in found)
    return {"checks": checks, "log": [f"validate: {failing} check(s) failing"]}


async def company_checks(company: dict[str, Any], loaded: dict[str, int]) -> list[dict[str, Any]]:
    """What a primed company has to show: its facts, readable the way an answer
    reads them, every concept embedded, and the mapper finding it by name."""
    cik = company["cik"]
    async with SessionLocal() as session:
        runs = await session.scalar(
            select(func.count()).select_from(LoadRun).where(LoadRun.company_cik == cik)
        )
        facts = await session.scalar(
            select(func.count()).select_from(Fact).where(Fact.company_cik == cik)
        )
        used = select(Fact.concept_id).where(Fact.company_cik == cik)
        concepts = list(await session.scalars(select(Concept).where(Concept.id.in_(used))))
    async with RetrievalSessionLocal() as session:
        visible = await session.scalar(
            select(func.count())
            .select_from(reported_fact)
            .where(reported_fact.c.company_cik == cik)
        )
    stale = [
        concept
        for concept in concepts
        if concept.embedding is None
        or concept.embedding_source_hash != source_hash(build_source_text(concept))
    ]
    checks = [
        _check(
            "loaded",
            bool(runs) and facts == loaded["facts"] and facts > 0,
            f"{facts:,} facts in the database, {loaded['facts']:,} loaded",
        ),
        _check(
            "embedded",
            not stale,
            f"{len(concepts) - len(stale):,} of the {len(concepts):,} concepts it uses",
        ),
        _check(
            "visible to retrieval",
            bool(visible),
            f"{visible:,} rows in xbrl.reported_fact, read as vf_retrieval_role",
        ),
    ]
    plans = {
        text: await map_query(_probe(text, ticker=company["ticker"]))
        for text in dict.fromkeys([company["input_name"], company["ticker"]])
    }
    for text, plan in plans.items():
        found = plan.filters.ciks == [cik]
        checks.append(_check(f"found as {text!r}", found, "the mapper's company lookup"))
    bound = any(binding.company_cik == cik for binding in plans[company["ticker"]].bindings)
    detail = "the mapper binds it for the latest year, with no model"
    checks.append(_check(f"answers {PROBE_METRIC!r}", bound, detail))
    return checks


def _probe(text: str, *, ticker: str) -> QueryIn:
    company: dict[str, Any] = {"id": "c", "kind": "company", "text": text}
    if text == ticker:
        company["ticker"] = ticker  # as the parser marks a ticker
    return QueryIn.model_validate(
        {
            "question": f"{text} {PROBE_METRIC}",
            "intent": "lookup",
            "elements": [
                company,
                {"id": "m", "kind": "metric", "text": PROBE_METRIC},
                {"id": "p", "kind": "period", "text": "latest year", "last_n_years": 1},
            ],
        }
    )


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


async def report(state: PrimeState) -> dict[str, Any]:
    return {"summary": summary(state)}


def summary(state: PrimeState) -> list[str]:
    """What the run did, one block per company, then what stopped it if anything."""
    lines: list[str] = []
    for company in state.get("companies", []) if state.get("approved") else []:
        ticker = company["ticker"]
        lines.append(f"{ticker}  {company['title']}  CIK {company['cik']}")
        manifest, loaded = (
            state.get("manifests", {}).get(ticker),
            state.get("loaded", {}).get(ticker),
        )
        if manifest and loaded:
            years = manifest["fiscal_years"]
            span = f"FY{min(years)}-FY{max(years)}" if years else "no fiscal years"
            lines.append(f"  {span}, {loaded['filings']} filings, {loaded['facts']:,} facts")
        for check in state.get("checks", {}).get(ticker, []):
            mark = "ok  " if check["ok"] else "FAIL"
            lines.append(f"  {mark}  {check['name']}: {check['detail']}")
    if state.get("embedded"):
        e = state["embedded"]
        lines.append(f"Embedder: {e['embedded']} embedded, {e['skipped']} already up to date")
    if state.get("already_loaded"):
        lines.append(f"Already loaded, skipped: {', '.join(state['already_loaded'])}")
    lines += [f"Dropped: {reason}" for reason in state.get("dropped", [])]
    lines += [f"Warning: {warning}" for warning in state.get("warnings", [])]
    if state.get("failed"):
        lines.append(f"Stopped at {state['failed']}")
        lines.append("Fix that and run the same command again: finished steps are not redone.")
    elif state.get("approved") and state.get("companies"):
        lines.append(
            "Changed, to commit: corpus_companies.json, sic_numbers.json, company_aliases.json"
        )
    return lines or ["Nothing to do."]


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def _onward(next_node: str) -> Callable[[PrimeState], str]:
    """The edge out of a step: on to ``next_node``, or to report once anything failed."""

    def route(state: PrimeState) -> str:
        return "report" if state.get("failed") else next_node

    return route


def _has_companies(next_node: str) -> Callable[[PrimeState], str]:
    """As ``_onward``, and to report when nothing is left to prime."""

    def route(state: PrimeState) -> str:
        return "report" if state.get("failed") or not state.get("companies") else next_node

    return route


def _after_confirm(state: PrimeState) -> str:
    return "register" if state.get("approved") else END


def _after_validate(state: PrimeState) -> str:
    """Back to embed once if a concept is still stale -- Ollama dropped out
    mid-run, say. Any other failing check is for a person to look at."""
    stale = any(
        check["name"] == "embedded" and not check["ok"]
        for found in state.get("checks", {}).values()
        for check in found
    )
    if stale and not state.get("failed") and state.get("embed_runs", 0) < EMBED_RUNS:
        return "embed"
    return "report"


def build_graph() -> StateGraph:
    graph = StateGraph(PrimeState)
    graph.add_node("preflight", preflight)
    graph.add_node("resolve", resolve, retry_policy=SEC_RETRY)
    graph.add_node("vet", vet, retry_policy=SEC_RETRY)
    graph.add_node("confirm", confirm)
    graph.add_node("register", register)
    graph.add_node("fetch_submissions", fetch_submissions, retry_policy=SEC_RETRY)
    graph.add_node("fetch_xbrl", fetch_xbrl, retry_policy=SEC_RETRY)
    graph.add_node("load", load)
    graph.add_node("embed", embed)
    graph.add_node("validate", validate)
    graph.add_node("report", report)

    graph.add_edge(START, "preflight")
    graph.add_conditional_edges("preflight", _onward("resolve"), ["resolve", "report"])
    graph.add_conditional_edges("resolve", _has_companies("vet"), ["vet", "report"])
    graph.add_conditional_edges("vet", _has_companies("confirm"), ["confirm", "report"])
    graph.add_conditional_edges("confirm", _after_confirm, ["register", END])
    graph.add_conditional_edges(
        "register", _onward("fetch_submissions"), ["fetch_submissions", "report"]
    )
    graph.add_conditional_edges(
        "fetch_submissions", _onward("fetch_xbrl"), ["fetch_xbrl", "report"]
    )
    graph.add_conditional_edges("fetch_xbrl", _onward("load"), ["load", "report"])
    graph.add_conditional_edges("load", _onward("embed"), ["embed", "report"])
    graph.add_conditional_edges("embed", _onward("validate"), ["validate", "report"])
    graph.add_conditional_edges("validate", _after_validate, ["embed", "report"])
    graph.add_edge("report", END)
    return graph


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def plan_text(plan: dict[str, Any]) -> list[str]:
    lines = ["To add and prime:"]
    for company in plan["companies"]:
        named = (
            f'  as "{company["input_name"]}"' if company["input_name"] != company["ticker"] else ""
        )
        tickers = ", ".join(company["all_tickers"])
        lines.append(
            f"  {company['ticker']}  {company['title']}  CIK {company['cik']}  "
            f"tickers {tickers}{named}"
        )
    if plan["already_loaded"]:
        lines.append(f"Already loaded, skipped: {', '.join(plan['already_loaded'])}")
    lines += [f"Dropped: {reason}" for reason in plan["dropped"]]
    return lines


async def prime(
    requested: list[str],
    names: dict[str, str],
    *,
    dry_run: bool = False,
    ask: Callable[[str], Awaitable[str]] | None = None,
) -> tuple[PrimeState, int]:
    """Run the graph to the end, pausing once at confirm. ``ask`` answers the
    pause (the terminal by default); ``dry_run`` stops there and writes nothing."""
    ask = ask or (lambda prompt: asyncio.to_thread(input, prompt))
    app = build_graph().compile(checkpointer=InMemorySaver())  # holds the run across the pause
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    command: Any = {"requested": requested, "names": names}
    try:
        while True:
            paused = None
            async for update in app.astream(command, config, stream_mode="updates"):
                for node, output in update.items():
                    if node == "__interrupt__":
                        paused = output[0].value  # what confirm put to the reader
                    elif output:
                        for line in output.get("log", []):
                            print(f"[prime] {line}", file=sys.stderr)
            if paused is None:
                break
            print("\n".join(plan_text(paused)))
            if dry_run:
                print("Dry run: nothing written.")
                return (await app.aget_state(config)).values, 0
            answer = await ask("Add and prime these? [y/N] ")
            command = Command(resume=answer.strip().lower() in ("y", "yes"))
    except Exception as exc:  # a retried SEC step past its last attempt
        state = {**(await app.aget_state(config)).values, "failed": f"{type(exc).__name__}: {exc}"}
        return {**state, "summary": summary(state)}, 1

    state = (await app.aget_state(config)).values
    if "approved" in state and not state["approved"]:
        return {**state, "summary": ["Nothing written."]}, 0
    failing = any(not c["ok"] for found in state.get("checks", {}).values() for c in found)
    return state, 1 if state.get("failed") or failing else 0


def _names(pairs: list[str]) -> dict[str, str]:
    names = {}
    for pair in pairs:
        ticker, sep, name = pair.partition("=")
        if not sep or not ticker.strip() or not name.strip():
            raise SystemExit(f"--as takes TICKER=NAME, got {pair!r}")
        names[ticker.strip().upper()] = name.strip()
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.prime_new_companies")
    parser.add_argument("identifiers", nargs="*", help="tickers or CIKs, e.g. NFLX 1065280")
    parser.add_argument(
        "--as",
        dest="names",
        action="append",
        default=[],
        metavar="TICKER=NAME",
        help="the name people use, e.g. --as NFLX=Netflix; repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument("--diagram", action="store_true", help="print the graph as Mermaid")
    args = parser.parse_args(argv)

    if args.diagram:
        print(build_graph().compile().get_graph().draw_mermaid())
        return 0
    if not args.identifiers:
        parser.error("give at least one ticker or CIK")
    state, code = asyncio.run(prime(args.identifiers, _names(args.names), dry_run=args.dry_run))
    if state.get("summary"):
        print("\n".join(state["summary"]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
