"""Passage finding with Claude's citations feature.

Every paper is sent as a "custom content" document with one block per
sentence. Claude answers with a verdict and one line per passage; the
citation attached to such a line names the blocks it rests on, which are
exactly our sentence indices. Citations cannot be combined with structured
outputs, hence the plain line format.

Docs: https://platform.claude.com/docs/en/build-with-claude/citations
"""

import re

import anthropic

from loccit.context import MARK_END, MARK_START, Context
from loccit.models import ModelSettings, ModelSettingsError
from loccit.strategies import (
    NOT_FOUND,
    VERDICTS,
    Paper,
    Passage,
    Result,
    StrategyError,
    Usage,
    passage,
)

# Thinking counts towards max_tokens, the answer itself is a few lines.
MAX_TOKENS = 32000
# At most this many cache breakpoints are allowed per request.
MAX_CACHE_BREAKPOINTS = 4

SYSTEM_PROMPT = f"""\
You help the author of a thesis verify their citations. You get the cited \
papers as documents, split into sentences, and one paragraph of the thesis. \
In the paragraph, one citation is marked with {MARK_START}...{MARK_END}. \
Decide whether the cited papers support what the thesis says at that \
citation, and point to the passages that show it.

What a citation backs up is the statement it is attached to: usually the \
sentence or clause that contains the marker, not the whole paragraph. Other \
citations in the paragraph belong to other sources; ignore them.

Point, do not quote. For every passage, write one short sentence that says \
how the passage supports the statement, and cite the passage in that \
sentence. The author reads the passage in the original PDF, so do not copy \
text from the papers into your answer.

A passage counts only if it states or directly shows what the thesis says: \
the same finding, number, definition or claim. A passage that merely shares \
the topic or the vocabulary is not support. The author uses this check to \
find wrong or sloppy citations, so a mistaken "supported" does more harm \
than an honest "not_found". If nothing in the papers backs the statement, \
answer not_found and list no passages; that is a valid and useful result.

The thesis may be written in German while the papers are in English. Judge \
by meaning across languages.

Answer in exactly this format and with nothing else:

VERDICT: supported | partially_supported | not_found
PASSAGE: <one sentence in the language of the thesis paragraph, citing the passage>
PASSAGE: <...>

- supported: the passages back the full statement.
- partially_supported: they back only part of it, or a weaker or narrower \
version of it.
- not_found: no passage backs it. Then there are no PASSAGE lines.
- Order the passages from strongest to weakest across all papers together. \
List at most 3 per paper and 8 in total; one precise passage is better than \
several loose ones.
- Each PASSAGE line cites exactly one passage: one sentence or a few \
consecutive sentences.
"""

_VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*([a-z_]+)", re.IGNORECASE)
_PASSAGE_LINE = re.compile(r"^\s*PASSAGE:\s*(.*\S)\s*$", re.IGNORECASE)


class ClaudeCitations:
    name = "claude-citations"
    # Bump when the prompt or the document layout changes, so that runs made
    # with different prompts can be told apart.
    prompt_version = "1"

    def __init__(self, client: anthropic.Anthropic, models: ModelSettings):
        self._client = client
        self._models = models

    def find_passages(
        self,
        context: Context,
        papers: list[Paper],
        model: str,
        thinking: str,
        *,
        missing: tuple[str, ...] = (),
    ) -> Result:
        config = self._models.get(model)
        if config is None:
            raise StrategyError("unknown_model", model)
        try:
            options = config.request_options(thinking)
        except ModelSettingsError as error:
            raise StrategyError("unsupported_thinking", str(error)) from error

        try:
            # Streamed only to stay clear of HTTP timeouts on long inputs;
            # nothing is passed on before the response is complete.
            with self._client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_content(context, papers, missing)}],
                **options,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as error:
            raise StrategyError("invalid_api_key") from error
        except anthropic.PermissionDeniedError as error:
            raise StrategyError("permission_denied", error.message) from error
        except anthropic.NotFoundError as error:
            raise StrategyError("unknown_model", model) from error
        except anthropic.RateLimitError as error:
            raise StrategyError("rate_limited") from error
        except anthropic.APIStatusError as error:
            raise StrategyError(
                "api_error", f"HTTP {error.status_code}: {error.message}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise StrategyError("no_connection") from error

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) or ""
            raise StrategyError("refused", category)
        if message.stop_reason == "max_tokens":
            raise StrategyError("truncated")
        return parse_message(message, papers)


def build_content(context: Context, papers: list[Paper], missing: tuple[str, ...]) -> list[dict]:
    """The user message: one document per paper, then the paragraph."""
    content: list[dict] = []
    first_cached = len(papers) - MAX_CACHE_BREAKPOINTS
    for number, paper in enumerate(papers):
        document = {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": s.text} for s in paper.sentences],
            },
            "title": _title(paper),
            "context": f"cite key: {paper.cite_key}",
            "citations": {"enabled": True},
        }
        # The same paper is often cited several times in a thesis; a cached
        # document makes the next check of it cheaper and faster.
        if number >= first_cached:
            document["cache_control"] = {"type": "ephemeral"}
        content.append(document)

    lines = ["Paragraph of the thesis:", "", context.text, ""]
    lines.append("The marked citation refers to: " + ", ".join(p.cite_key for p in papers) + ".")
    if missing:
        lines.append(
            "It also refers to sources that are not available as documents: "
            + ", ".join(missing)
            + ". Judge only on the documents provided."
        )
    content.append({"type": "text", "text": "\n".join(lines)})
    return content


def _title(paper: Paper) -> str:
    authors = paper.authors[0] if paper.authors else ""
    if len(paper.authors) > 1:
        authors += " et al."
    byline = " ".join(part for part in (authors, f"({paper.year})" if paper.year else "") if part)
    return f"{byline}: {paper.title}" if byline else paper.title


def parse_message(message: anthropic.types.Message, papers: list[Paper]) -> Result:
    """Read verdict and passages from the answer. Passages are built from
    the citations' block indices alone, never from text the model wrote."""
    text = ""
    # (start, end, citation) for every cited stretch of the answer text.
    cited: list[tuple[int, int, object]] = []
    for block in message.content:
        if block.type != "text":
            continue
        for citation in block.citations or []:
            cited.append((len(text), len(text) + len(block.text), citation))
        text += block.text

    verdict = None
    passages: list[Passage] = []
    seen: set[tuple[int, int, int]] = set()
    offset = 0
    for line in text.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if verdict is None and (match := _VERDICT_LINE.match(line)):
            verdict = match.group(1).lower()
            continue
        if not (match := _PASSAGE_LINE.match(line)):
            continue
        for begin, end, citation in cited:
            if begin >= offset or end <= start or citation.type != "content_block_location":
                continue
            key = (citation.document_index, citation.start_block_index, citation.end_block_index)
            if key in seen or not 0 <= citation.document_index < len(papers):
                continue
            seen.add(key)
            found = passage(
                papers[citation.document_index],
                citation.start_block_index,
                citation.end_block_index,
                match.group(1),
            )
            if found:
                passages.append(found)

    if verdict not in VERDICTS:
        raise StrategyError("bad_response", text[:500])
    # A verdict is only as good as the passages that can be shown for it.
    if verdict == NOT_FOUND or not passages:
        verdict, passages = NOT_FOUND, []
    usage = message.usage
    return Result(
        verdict=verdict,
        passages=passages,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        ),
        response_text=text,
    )
