"""The text of the thesis that a citation is checked against: the paragraph
around the citation, with the citation's position marked."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from loccit.citations import CitationGroup
from loccit.textlayout import clean, page_paragraphs

MARK_START = "<cite_here>"
MARK_END = "</cite_here>"
_RECT_TOLERANCE = 0.5


@dataclass(frozen=True)
class Context:
    # The paragraph, with MARK_START and MARK_END around the citation.
    text: str
    # 0-based page index
    page: int

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def citation_context(pdf_path: Path, group: CitationGroup) -> Context | None:
    """The paragraph that holds the citation group, or None if the citation
    links lie over no text."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[group.page]
        rects = [
            pymupdf.Rect(rect) * page.transformation_matrix
            for citation in group.citations
            for rect in citation.rects
        ]
        best: tuple[int, int, str] | None = None
        best_count = 0
        for paragraph in page_paragraphs(page):
            covered = [
                index
                for index, box in enumerate(paragraph.boxes)
                if box is not None and any(_inside(box[1], rect) for rect in rects)
            ]
            if len(covered) > best_count:
                best_count = len(covered)
                best = (covered[0], covered[-1] + 1, paragraph.text)
    if best is None:
        return None
    start, end, text = best
    marked = text[:start] + MARK_START + text[start:end] + MARK_END + text[end:]
    return Context(clean(marked), group.page)


def _inside(box: tuple[float, float, float, float], rect: pymupdf.Rect) -> bool:
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    return (
        rect.x0 - _RECT_TOLERANCE <= x <= rect.x1 + _RECT_TOLERANCE
        and rect.y0 - _RECT_TOLERANCE <= y <= rect.y1 + _RECT_TOLERANCE
    )
