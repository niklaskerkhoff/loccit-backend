"""Text of a PDF page as paragraphs that remember where every character is.

PyMuPDF yields blocks of lines of characters with coordinates. Here the lines
of a block are split into paragraphs and joined into running text: a line
break becomes a space, and a word hyphenated at a line end is put back
together. Every character of the running text keeps its line and its box,
so any span of the text can be turned back into rectangles on the page.
"""

import itertools
import re
import unicodedata
from dataclasses import dataclass, field

import pymupdf

Rect = tuple[float, float, float, float]

_HYPHENS = "-\u00ad\u2010"
# A first-line indent or a short last line, relative to the block's width.
_INDENT = 4.0
_SHORT_LINE = 0.12
_SENTENCE_END = ".!?:"

# A sentence ends at ., ! or ? (plus closing quotes or brackets) when
# whitespace and then a capital letter, a digit or an opening quote follow.
_SENTENCE_BREAK = re.compile(
    r"[.!?][\"'\u201d\u2019)\]]*(\s+)(?=[A-Z\u00c0-\u00de0-9\"'\u201c\u2018(\[])"
)
_WORD_BEFORE = re.compile(r"([\w.]+)$")
# Abbreviations after which a capital letter does not start a sentence.
_ABBREVIATIONS = {
    "al", "e.g", "i.e", "fig", "figs", "eq", "eqs", "sec", "cf", "vs", "no", "nos", "dr",
    "prof", "mr", "mrs", "ms", "st", "vol", "pp", "ch", "ref", "refs", "tab", "resp",
    "approx", "ca", "z.b", "bzw", "u.a", "d.h", "vgl", "abb", "bspw", "ggf", "nr", "s",
}  # fmt: skip


@dataclass
class Paragraph:
    # 0-based page index
    page: int
    text: str = ""
    # For every character of `text`: (line number on the page, box in PyMuPDF
    # page coordinates), or None for a space that stands for a line break.
    boxes: list[tuple[int, Rect] | None] = field(default_factory=list)

    def rects(self, start: int, end: int) -> list[Rect]:
        """One rectangle per line for text[start:end], in PyMuPDF coordinates."""
        lines: dict[int, pymupdf.Rect] = {}
        for box in self.boxes[start:end]:
            if box is not None:
                line, rect = box
                lines[line] = lines[line] | rect if line in lines else pymupdf.Rect(rect)
        return [tuple(rect) for rect in lines.values()]


@dataclass
class _Line:
    number: int
    x0: float
    x1: float
    chars: list[tuple[str, Rect]]

    @property
    def text(self) -> str:
        return "".join(char for char, _ in self.chars)


def page_paragraphs(page: pymupdf.Page) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    number = 0
    for block in page.get_text("rawdict")["blocks"]:
        lines: list[_Line] = []
        for raw_line in block.get("lines", []):
            chars = [
                (c["c"], tuple(c["bbox"])) for span in raw_line["spans"] for c in span["chars"]
            ]
            if "".join(char for char, _ in chars).strip():
                x0, _, x1, _ = raw_line["bbox"]
                lines.append(_Line(number, x0, x1, chars))
                number += 1
        for group in _split_paragraphs(lines):
            paragraphs.append(_join_lines(page.number, group))
    return paragraphs


def _split_paragraphs(lines: list[_Line]) -> list[list[_Line]]:
    """Split the lines of a block where a new paragraph starts: at a line
    indented against the one before it, or after a line that ends a sentence
    well short of the right margin."""
    if not lines:
        return []
    right = max(line.x1 for line in lines)
    width = right - min(line.x0 for line in lines)
    groups = [[lines[0]]]
    for previous, line in itertools.pairwise(lines):
        indented = line.x0 > previous.x0 + _INDENT
        previous_ends_short = (
            previous.x1 < right - _SHORT_LINE * width
            and previous.text.rstrip()[-1:] in _SENTENCE_END
        )
        if indented or previous_ends_short:
            groups.append([])
        groups[-1].append(line)
    return groups


def _join_lines(page: int, lines: list[_Line]) -> Paragraph:
    paragraph = Paragraph(page)
    text: list[str] = []
    for index, line in enumerate(lines):
        chars = list(line.chars)
        while chars and chars[-1][0].isspace():
            chars.pop()
        following = lines[index + 1].text.lstrip() if index + 1 < len(lines) else ""
        hyphenated = (
            len(chars) > 1
            and chars[-1][0] in _HYPHENS
            and chars[-2][0].isalpha()
            and following[:1].islower()
        )
        if hyphenated:
            chars.pop()
        for char, rect in chars:
            text.append(char)
            paragraph.boxes.append((line.number, rect))
        if following and not hyphenated:
            text.append(" ")
            paragraph.boxes.append(None)
    paragraph.text = "".join(text)
    return paragraph


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split text into sentences, as (start, end) offsets without the
    whitespace between them."""
    spans = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        if match.group()[0] == "." and _is_abbreviation(text[start : match.start()]):
            continue
        spans.append((start, match.start(1)))
        start = match.end(1)
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _is_abbreviation(before_period: str) -> bool:
    word = _WORD_BEFORE.search(before_period)
    if not word:
        return False
    token = word.group(1)
    # "et al.", "Fig.", and initials as in "J. Smith".
    return token.lower() in _ABBREVIATIONS or (len(token) == 1 and token.isupper())


def clean(text: str) -> str:
    """Text as it is sent to a model: ligatures expanded, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def to_pdf_space(page: pymupdf.Page, rect: Rect) -> Rect:
    """PyMuPDF page coordinates (origin top left) to PDF user space."""
    converted = (pymupdf.Rect(rect) * ~page.transformation_matrix).normalize()
    return tuple(round(value, 2) for value in converted)
