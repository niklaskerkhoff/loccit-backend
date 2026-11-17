"""What a passage-finding strategy gets and what it returns.

A strategy is given the checked text and the papers as lists of sentences and
answers with a verdict and pointers to sentences. It never returns text of
the paper itself. Other strategies (e.g. numbered sentences with a JSON
schema for other providers) implement the same `Strategy` protocol.
"""

from dataclasses import dataclass, field
from typing import Protocol

from loccit.context import Context
from loccit.sentences import Sentence
from loccit.textlayout import Rect

SUPPORTED = "supported"
PARTIALLY_SUPPORTED = "partially_supported"
NOT_FOUND = "not_found"
VERDICTS = (SUPPORTED, PARTIALLY_SUPPORTED, NOT_FOUND)


@dataclass(frozen=True)
class Paper:
    cite_key: str
    title: str
    authors: tuple[str, ...]
    year: str | None
    sentences: list[Sentence]


@dataclass(frozen=True)
class Passage:
    """A range of sentences of one paper that supports the checked text."""

    cite_key: str
    # Sentence indices, end exclusive.
    start_sentence: int
    end_sentence: int
    # 0-based index of the page the passage starts on.
    page: int
    # (page, rectangle in PDF user space) for every line of the passage.
    rects: tuple[tuple[int, Rect], ...]
    # One sentence saying how the passage supports the text.
    reason: str


def passage(paper: Paper, start: int, end: int, reason: str) -> Passage | None:
    """The passage for sentences [start, end) of a paper; None if the range
    lies outside the paper. Coordinates come from the stored sentences only."""
    sentences = paper.sentences[max(start, 0) : end]
    if not sentences:
        return None
    return Passage(
        cite_key=paper.cite_key,
        start_sentence=sentences[0].index,
        end_sentence=sentences[-1].index + 1,
        page=sentences[0].page,
        rects=tuple((s.page, rect) for s in sentences for rect in s.rects),
        reason=reason,
    )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Result:
    verdict: str
    # Strongest first, across all papers.
    passages: list[Passage] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # The model's answer as text, kept for later comparison of runs.
    response_text: str = ""


class StrategyError(Exception):
    """The check could not be carried out. `code` tells the UI why."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class Strategy(Protocol):
    name: str
    prompt_version: str

    def find_passages(
        self,
        context: Context,
        papers: list[Paper],
        model: str,
        thinking: str,
        *,
        missing: tuple[str, ...] = (),
    ) -> Result:
        """Find the passages in `papers` that support the citation marked in
        `context`. `missing` names cited sources whose paper is not available."""
        ...
