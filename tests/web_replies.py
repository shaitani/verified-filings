"""Real replies for the Web Client's tests: ``app.chain.ask`` over the captured runs in
tests/fixtures/chain/, written to web/src/app/conversation/fixtures/replies.json.

    uv run python -m tests.web_replies

The stages are the captures (no model, no database) and everything after them --
the chain's parts and fixed sentences, the Presenter -- is the real code, so the
client is tested against what the server sends rather than a hand-typed guess.
``tests/test_web_replies.py`` fails while the file is out of date.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import app.chain as chain
from app.chain import ask
from tests.test_chain import _load

REPLIES_FILE = (
    Path(__file__).resolve().parents[1] / "web/src/app/conversation/fixtures/replies.json"
)

#: capture -> what the client test uses it for.
CAPTURES = {
    "q001": "answered: one figure",
    "q052": "partial: five figures and a refused goodwill",
    "q046": "asked: a curated question (profit margin)",
    "ambiguous": "asked: an ambiguity (accounts payable)",
    "q028": "refused: a blocking refusal (a period out of range)",
}

# Fixed, so the file only changes when a reply does.
CONVERSATION = uuid.UUID(int=0xC1)


async def _reply(name: str, job: int) -> dict:
    query, plan, result = _load(name)

    async def parse(_question, *, answers=None):
        return query

    async def map_query(_query, *, pins=None):
        return plan

    async def answer(_plan):
        return result

    saved = chain.parse_question, chain.map_query, chain.answer
    chain.parse_question, chain.map_query, chain.answer = parse, map_query, answer
    try:
        outcome = await ask(plan.question)
    finally:
        chain.parse_question, chain.map_query, chain.answer = saved
    return outcome.reply(CONVERSATION, uuid.UUID(int=job)).model_dump(mode="json")


def replies() -> str:
    """The file's content: each capture's reply, indented, ending in one newline."""

    async def all_of_them() -> dict:
        return {
            name: {"about": about, "reply": await _reply(name, job)}
            for job, (name, about) in enumerate(CAPTURES.items(), start=1)
        }

    return json.dumps(asyncio.run(all_of_them()), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    REPLIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPLIES_FILE.write_text(replies(), encoding="utf-8", newline="\n")
    print(f"wrote {REPLIES_FILE}")


if __name__ == "__main__":
    main()
