"""A paper as a list of sentences with their place on the page.

The sentences are the units a model can point to. Their text goes to the
model; their rectangles are what gets highlighted. The model never has to
reproduce text, so highlights cannot drift from what the PDF says.
"""

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from loccit.textlayout import Rect, clean, page_paragraphs, sentence_spans, to_pdf_space

# Bump when the splitting changes: stored runs refer to sentences by index,
# and indices are only comparable within one version.
SENTENCE_VERSION = "1"

# Fragments shorter than this (table cells, headings, labels in figures) are
# collected into one unit of at most MAX_COLLECTED characters, so that a
# table does not become hundreds of one-word units.
SHORT_FRAGMENT = 30
MAX_COLLECTED = 150

log = logging.getLogger(__name__)
# MuPDF collects its messages globally; one extraction at a time keeps them
# with the paper they belong to.
_mupdf_messages = threading.Lock()


@dataclass(frozen=True)
class Sentence:
    # Position in the paper; what a model's pointer refers to.
    index: int
    text: str
    # 0-based page index
    page: int
    # One rectangle per line, in PDF user space (origin bottom left).
    rects: tuple[Rect, ...]


def extract_sentences(pdf_path: Path) -> list[Sentence]:
    """All text of the PDF, in reading order, split into sentences."""
    sentences: list[Sentence] = []
    with _mupdf_messages:
        pymupdf.TOOLS.reset_mupdf_warnings()
        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                for text, rects in _page_units(page):
                    sentences.append(Sentence(len(sentences), text, page.number, tuple(rects)))
        messages = pymupdf.TOOLS.mupdf_warnings()
    if messages:
        # Mostly damaged fonts or colour spaces that do not affect the text.
        distinct = list(dict.fromkeys(line for line in messages.splitlines() if line.strip()))
        log.debug(
            "MuPDF reported %d problem(s) reading %s: %s",
            len(distinct),
            Path(pdf_path).name,
            "; ".join(distinct[:5]) + (" …" if len(distinct) > 5 else ""),
        )
    return sentences


def _page_units(page: pymupdf.Page) -> Iterator[tuple[str, list[Rect]]]:
    """The sentences of a page as (text, rectangles), short fragments collected."""
    collected: list[tuple[str, list[Rect]]] = []

    def flush() -> Iterator[tuple[str, list[Rect]]]:
        if collected:
            text = " ".join(fragment for fragment, _ in collected)
            yield text, [rect for _, rects in collected for rect in rects]
            collected.clear()

    for paragraph in page_paragraphs(page):
        for start, end in sentence_spans(paragraph.text):
            text = clean(paragraph.text[start:end])
            rects = [to_pdf_space(page, rect) for rect in paragraph.rects(start, end)]
            if not rects or not any(char.isalnum() for char in text):
                continue
            if len(text) >= SHORT_FRAGMENT:
                yield from flush()
                yield text, rects
                continue
            collected.append((text, rects))
            if sum(len(fragment) + 1 for fragment, _ in collected) >= MAX_COLLECTED:
                yield from flush()
    yield from flush()
