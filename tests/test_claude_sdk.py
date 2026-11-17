"""The strategy against the real Anthropic SDK, with only the HTTP transport
replaced: what goes over the wire, and that a streamed answer with citations
is put together correctly."""

import json

import anthropic
import httpx2

from loccit.models import load_model_settings
from loccit.strategies import SUPPORTED
from loccit.strategies.claude_citations import ClaudeCitations
from tests.test_claude_citations import BARISIC, CONTEXT, HELTWEG


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def text_block(index: int, text: str, citation: dict | None = None) -> list[dict]:
    start = {"type": "text", "text": ""}
    deltas = [{"type": "text_delta", "text": text}]
    if citation:
        start["citations"] = []
        deltas.insert(0, {"type": "citations_delta", "citation": citation})
    return [
        {"type": "content_block_start", "index": index, "content_block": start},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def location(document: int, start: int, end: int) -> dict:
    return {
        "type": "content_block_location",
        "cited_text": "quoted by the API",
        "document_index": document,
        "document_title": "t",
        "start_block_index": start,
        "end_block_index": end,
    }


STREAM = sse(
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 300,
                "output_tokens": 1,
                "cache_read_input_tokens": 18000,
                "cache_creation_input_tokens": 0,
            },
        },
    },
    *text_block(0, "VERDICT: supported\nPASSAGE: "),
    *text_block(1, "Teilnehmende waren mit Jayvee schneller.", location(0, 1, 3)),
    *text_block(2, "\nPASSAGE: "),
    *text_block(3, "Benutzbarkeit ist entscheidend.", location(1, 0, 1)),
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 61},
    },
    {"type": "message_stop"},
)


def test_request_on_the_wire_and_streamed_answer(tmp_path):
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=STREAM)

    client = anthropic.Anthropic(
        api_key="sk-ant-test-key",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    strategy = ClaudeCitations(client, load_model_settings(tmp_path / "models.toml"))
    result = strategy.find_passages(CONTEXT, [HELTWEG, BARISIC], "claude-sonnet-5-5", "off")

    (request,) = sent
    assert request.url.path == "/v1/messages"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "between_tools"}
    assert body["output_config"] == {"effort": "low"}
    assert body["stream"] is True
    documents = body["messages"][0]["content"][:2]
    assert [d["source"]["type"] for d in documents] == ["content", "content"]
    assert len(documents[0]["source"]["content"]) == len(HELTWEG.sentences)
    assert all(d["citations"] == {"enabled": True} for d in documents)
    # No sampling parameters and no structured output format are sent.
    assert not {"temperature", "top_p", "top_k", "output_format"} & set(body)

    assert result.verdict == SUPPORTED
    assert [(p.cite_key, p.start_sentence, p.end_sentence) for p in result.passages] == [
        ("heltweg_empirical_2025", 1, 3),
        ("barisic_how_2012", 0, 1),
    ]
    assert result.passages[0].reason == "Teilnehmende waren mit Jayvee schneller."
    assert (result.usage.input_tokens, result.usage.output_tokens) == (300, 61)
    assert result.usage.cache_read_tokens == 18000
