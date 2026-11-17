import anthropic
import httpx2
import pytest
from anthropic.types import Message

from loccit.context import Context
from loccit.models import load_model_settings
from loccit.sentences import Sentence
from loccit.strategies import NOT_FOUND, PARTIALLY_SUPPORTED, SUPPORTED, Paper, StrategyError
from loccit.strategies.claude_citations import ClaudeCitations

CONTEXT = Context(
    "DSLs helfen Nicht-Programmierern [<cite_here>1, 2</cite_here>]. Mehr Text [3].", page=4
)


def paper(cite_key: str, *texts: str, first_page: int = 0) -> Paper:
    """A paper with one sentence per page, each with one rectangle."""
    sentences = [
        Sentence(index, text, first_page + index, ((72.0, 700.0 - index, 300.0, 712.0 - index),))
        for index, text in enumerate(texts)
    ]
    return Paper(cite_key, f"Title of {cite_key}", ("Heltweg", "Riehle"), "2025", sentences)


HELTWEG = paper(
    "heltweg_empirical_2025",
    "Data engineering is hard.",
    "Participants solved tasks faster with Jayvee.",
    "They also made fewer errors.",
    "Future work remains.",
)
BARISIC = paper("barisic_how_2012", "Usability matters for DSLs.", "We propose a process.")


def cited(text: str, document: int, start: int, end: int) -> dict:
    return {
        "type": "text",
        "text": text,
        "citations": [
            {
                "type": "content_block_location",
                # What the API quotes is ignored; only the indices are used.
                "cited_text": "text the API quoted",
                "document_index": document,
                "document_title": "t",
                "start_block_index": start,
                "end_block_index": end,
            }
        ],
    }


def message(*content, stop_reason: str = "end_turn", **usage) -> Message:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80, **usage},
            "content": [{"type": "text", "text": c} if isinstance(c, str) else c for c in content],
        }
    )


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns `answer`."""

    def __init__(self, answer: Message | Exception):
        self.answer = answer
        self.requests: list[dict] = []
        self.messages = self

    def stream(self, **request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get_final_message(self) -> Message:
        return self.answer


@pytest.fixture
def run(tmp_path):
    settings = load_model_settings(tmp_path / "models.toml")

    def run(answer, papers=(HELTWEG, BARISIC), model="claude-sonnet-5-5", thinking="off", **kw):
        client = FakeClient(answer)
        strategy = ClaudeCitations(client, settings)
        result = strategy.find_passages(CONTEXT, list(papers), model, thinking, **kw)
        return result, client.requests[0]

    return run


SUPPORTED_ANSWER = message(
    "VERDICT: supported\nPASSAGE: ",
    cited("Teilnehmende lösten Aufgaben mit Jayvee schneller und fehlerärmer.", 0, 1, 3),
    "\nPASSAGE: ",
    cited("Benutzbarkeit ist für DSLs entscheidend.", 1, 0, 1),
    "\n",
    cache_read_input_tokens=900,
    cache_creation_input_tokens=300,
)


def test_each_paper_is_a_custom_content_document_with_one_block_per_sentence(run):
    _, request = run(SUPPORTED_ANSWER)
    first, second, question = request["messages"][0]["content"]
    assert first["type"] == "document"
    assert first["source"] == {
        "type": "content",
        "content": [{"type": "text", "text": s.text} for s in HELTWEG.sentences],
    }
    assert first["citations"] == {"enabled": True}
    assert second["citations"] == {"enabled": True}
    assert first["title"] == "Heltweg et al. (2025): Title of heltweg_empirical_2025"
    assert first["context"] == "cite key: heltweg_empirical_2025"
    assert first["cache_control"] == second["cache_control"] == {"type": "ephemeral"}
    assert question["type"] == "text"
    assert CONTEXT.text in question["text"]
    assert "heltweg_empirical_2025, barisic_how_2012" in question["text"]
    # Citations cannot be combined with structured outputs.
    assert "output_format" not in request
    assert "format" not in request["output_config"]


def test_the_prompt_asks_for_not_found_rather_than_loose_matches(run):
    _, request = run(SUPPORTED_ANSWER)
    system = request["system"]
    assert "not_found" in system and "valid and useful result" in system
    assert "merely shares the topic" in system
    assert "do not copy text from the papers" in system
    assert "German" in system


def test_model_and_thinking_level_become_request_parameters(run):
    _, off = run(SUPPORTED_ANSWER)
    assert off["model"] == "claude-sonnet-5-5"
    assert off["thinking"] == {"type": "between_tools"}
    assert off["output_config"] == {"effort": "low"}
    _, high = run(SUPPORTED_ANSWER, model="claude-opus-5-5", thinking="high")
    assert high["model"] == "claude-opus-5-5"
    assert high["thinking"] == {"type": "adaptive"}
    assert high["output_config"] == {"effort": "high"}


def test_passages_come_from_the_cited_block_indices(run):
    result, _ = run(SUPPORTED_ANSWER)
    assert result.verdict == SUPPORTED
    strongest, second = result.passages
    assert (strongest.cite_key, strongest.start_sentence, strongest.end_sentence) == (
        "heltweg_empirical_2025",
        1,
        3,
    )
    assert strongest.reason == "Teilnehmende lösten Aufgaben mit Jayvee schneller und fehlerärmer."
    # Page and rectangles are those stored for sentences 1 and 2.
    assert strongest.page == 1
    assert strongest.rects == (
        (1, HELTWEG.sentences[1].rects[0]),
        (2, HELTWEG.sentences[2].rects[0]),
    )
    assert (second.cite_key, second.start_sentence, second.end_sentence, second.page) == (
        "barisic_how_2012",
        0,
        1,
        0,
    )
    assert "text the API quoted" not in repr(result.passages)


def test_usage_is_reported_with_cached_tokens(run):
    result, _ = run(SUPPORTED_ANSWER)
    usage = result.usage
    assert (usage.input_tokens, usage.output_tokens) == (1200, 80)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (900, 300)
    assert result.response_text.startswith("VERDICT: supported\nPASSAGE: Teilnehmende")


def test_not_found_is_a_valid_result(run):
    result, _ = run(message("VERDICT: not_found\n"))
    assert (result.verdict, result.passages) == (NOT_FOUND, [])


def test_a_verdict_without_any_cited_passage_counts_as_not_found(run):
    result, _ = run(message("VERDICT: supported\nPASSAGE: Das Paper sagt das irgendwo.\n"))
    assert (result.verdict, result.passages) == (NOT_FOUND, [])


def test_passages_listed_under_not_found_are_dropped(run):
    answer = message("VERDICT: not_found\nPASSAGE: ", cited("Nur lose verwandt.", 0, 0, 1))
    result, _ = run(answer)
    assert (result.verdict, result.passages) == (NOT_FOUND, [])


def test_partial_support_with_thinking_blocks_in_the_answer(run):
    answer = message(
        {"type": "thinking", "thinking": "", "signature": "sig"},
        "VERDICT: partially_supported\nPASSAGE: ",
        cited("Nur die Geschwindigkeit ist belegt.", 0, 1, 2),
    )
    result, _ = run(answer, model="claude-opus-5-5", thinking="medium")
    assert result.verdict == PARTIALLY_SUPPORTED
    assert [(p.start_sentence, p.end_sentence) for p in result.passages] == [(1, 2)]


def test_citations_outside_the_papers_are_ignored(run):
    answer = message(
        "VERDICT: supported\nPASSAGE: ",
        cited("Dokument gibt es nicht.", 7, 0, 1),
        "\nPASSAGE: ",
        cited("Satzbereich gibt es nicht.", 1, 40, 41),
        "\nPASSAGE: ",
        cited("Gültig, am Ende gekappt.", 1, 1, 9),
        "\nPASSAGE: ",
        cited("Dieselbe Stelle noch einmal.", 1, 1, 9),
    )
    result, _ = run(answer)
    assert [(p.cite_key, p.start_sentence, p.end_sentence) for p in result.passages] == [
        ("barisic_how_2012", 1, 2)
    ]


def test_missing_sources_are_named_in_the_request(run):
    _, request = run(SUPPORTED_ANSWER, papers=[HELTWEG], missing=("nobody_nothing_1999",))
    question = request["messages"][0]["content"][-1]["text"]
    assert "not available as documents: nobody_nothing_1999" in question
    assert len(request["messages"][0]["content"]) == 2


def test_only_the_last_four_documents_get_a_cache_breakpoint(run):
    papers = [paper(f"key_{n}", "One sentence.") for n in range(6)]
    _, request = run(message("VERDICT: not_found"), papers=papers)
    documents = request["messages"][0]["content"][:-1]
    assert ["cache_control" in document for document in documents] == [False, False] + [True] * 4


def test_an_answer_without_verdict_is_an_error(run):
    with pytest.raises(StrategyError) as error:
        run(message("I could not decide."))
    assert error.value.code == "bad_response"


@pytest.mark.parametrize(
    ("stop_reason", "code"), [("refusal", "refused"), ("max_tokens", "truncated")]
)
def test_refusal_and_truncation_are_errors_not_verdicts(run, stop_reason, code):
    with pytest.raises(StrategyError) as error:
        run(message("VERDICT: supported", stop_reason=stop_reason))
    assert error.value.code == code


def api_error(kind, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return kind("failed", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (api_error(anthropic.AuthenticationError, 401), "invalid_api_key"),
        (api_error(anthropic.PermissionDeniedError, 403), "permission_denied"),
        (api_error(anthropic.NotFoundError, 404), "unknown_model"),
        (api_error(anthropic.RateLimitError, 429), "rate_limited"),
        (api_error(anthropic.BadRequestError, 400), "api_error"),
        (api_error(anthropic.InternalServerError, 500), "api_error"),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            "no_connection",
        ),
    ],
)
def test_api_failures_become_error_codes(run, error, code):
    with pytest.raises(StrategyError) as raised:
        run(error)
    assert raised.value.code == code


def test_unknown_model_or_level_fails_before_any_request(tmp_path):
    client = FakeClient(SUPPORTED_ANSWER)
    strategy = ClaudeCitations(client, load_model_settings(tmp_path / "models.toml"))
    with pytest.raises(StrategyError) as error:
        strategy.find_passages(CONTEXT, [HELTWEG], "claude-opus-5-5", "off")
    assert error.value.code == "unsupported_thinking"
    with pytest.raises(StrategyError) as error:
        strategy.find_passages(CONTEXT, [HELTWEG], "gpt-x", "off")
    assert error.value.code == "unknown_model"
    assert client.requests == []
