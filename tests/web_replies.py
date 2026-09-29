"""Real server output for the Web Client's tests, written under web/src/app/:

* ``conversation/fixtures/replies.json`` -- ``app.chain.ask`` over the captured runs
  in tests/fixtures/chain/: one reply of each kind;
* ``answer/fixtures/answers.json`` -- ``app.presenter.present`` over the captured
  ``ResultSet``s in tests/fixtures/presenter/: one ``AnswerView`` of each kind of view.

    uv run python -m tests.web_replies

The stages are the captures (no model, no database) and everything after them --
the chain's parts and fixed sentences, the Presenter -- is the real code, so the
client is tested against what the server sends rather than a hand-typed guess.
``tests/test_web_replies.py`` fails while either file is out of date.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import app.chain as chain
from app.chain import ask
from app.presenter import present
from tests.test_chain import _load as _load_run
from tests.test_presenter import _load as _load_result

WEB_APP = Path(__file__).resolve().parents[1] / "web/src/app"
REPLIES_FILE = WEB_APP / "conversation/fixtures/replies.json"
ANSWERS_FILE = WEB_APP / "answer/fixtures/answers.json"

#: capture -> what the client test uses it for.
CAPTURES = {
    "q001": "answered: one figure",
    "q052": "partial: five figures and a refused goodwill",
    "q046": "asked: a curated question (profit margin)",
    "ambiguous": "asked: an ambiguity (accounts payable)",
    "q028": "refused: a blocking refusal (a period out of range)",
}

#: Presenter capture -> what it shows. One of each view and display rule (presenter DESIGN).
ANSWERS = {
    "q001": "a stat card: one figure",
    "q002": "a stat card for a balance: 'as of' its date",
    "q013": "a line: one company over five years, with its growth beside it",
    "q041": "a line panel holding two metrics in one unit",
    "q009": "a ranking: bars, highest first, capped at ten",
    "q006": "comparison bars: two companies, one figure each",
    "q022": "a multiple: 0.89x, never 89%",
    "q020": "a change, 'vs' the period it is measured from",
    "q040": "an average across companies: one row naming the fourteen",
    "q039": "a filtered list, stating its filter",
}

# Fixed, so the file only changes when a reply does.
CONVERSATION = uuid.UUID(int=0xC1)


async def _reply(name: str, job: int) -> dict:
    query, plan, result = _load_run(name)

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


def _json(content: dict) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False) + "\n"


def replies() -> str:
    """replies.json's content: each capture's reply."""

    async def all_of_them() -> dict:
        return {
            name: {"about": about, "reply": await _reply(name, job)}
            for job, (name, about) in enumerate(CAPTURES.items(), start=1)
        }

    return _json(asyncio.run(all_of_them()))


def answers() -> str:
    """answers.json's content: each captured ResultSet, presented."""
    return _json(
        {
            name: {
                "about": about,
                "question": _load_result(name)[0].question,
                "answer": present(*_load_result(name)).model_dump(mode="json"),
            }
            for name, about in ANSWERS.items()
        }
    )


def main() -> None:
    for path, content in ((REPLIES_FILE, replies()), (ANSWERS_FILE, answers())):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
