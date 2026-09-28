"""/openapi.json -- what the Web Client's TypeScript types are generated from (DESIGN §9)."""

from __future__ import annotations

import pytest

from app.api.server import create_app
from tests.test_auth import CONFIG


@pytest.fixture(scope="module")
def spec() -> dict:
    return create_app(CONFIG).openapi()  # no lifespan: nothing touches the database


def _refs(node) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _refs(value)]
    return []


def test_the_event_stream_is_typed(spec: dict) -> None:
    schemas = spec["components"]["schemas"]
    job_event = schemas["JobEvent"]
    assert job_event["discriminator"]["propertyName"] == "kind"
    assert set(job_event["discriminator"]["mapping"]) == {"stage", "done", "failed"}
    stream = spec["paths"]["/api/jobs/{job_id}/events"]["get"]["responses"]["200"]["content"]
    assert stream["text/event-stream"]["schema"] == {"$ref": "#/components/schemas/JobEvent"}


def test_every_reference_resolves(spec: dict) -> None:
    schemas = spec["components"]["schemas"]
    prefix = "#/components/schemas/"
    missing = {ref for ref in _refs(spec) if ref.removeprefix(prefix) not in schemas}
    assert not missing


UNIONS = ["StageEvent", "DoneEvent", "FailedEvent", "StatView", "LineView", "BarView"]


@pytest.mark.parametrize("model", UNIONS)
def test_a_sent_discriminator_is_required(spec: dict, model: str) -> None:
    # Optional, TypeScript could not narrow a union on it.
    assert "kind" in spec["components"]["schemas"][model]["required"]


def test_a_sent_optional_field_is_required_but_nullable(spec: dict) -> None:
    reply = spec["components"]["schemas"]["Reply"]
    assert {"blocking", "parts", "answer"} <= set(reply["required"])  # always sent, maybe null


def test_a_request_keeps_its_defaults_optional(spec: dict) -> None:
    # The browser may leave "kind" out of an answer; only responses are always complete.
    assert "kind" not in spec["components"]["schemas"]["OptionAnswerIn"]["required"]


def test_the_clients_copy_of_the_contract_is_current() -> None:
    """web/openapi.json is what the client's types are generated from: a model
    changed without it would leave the client compiling against the old shape."""
    from app.api.openapi import SPEC_FILE, contract

    assert SPEC_FILE.read_text(encoding="utf-8") == contract(), (
        "web/openapi.json is out of date: run `uv run python -m app.api.openapi`, "
        "then `npm run api:types` in web/"
    )
