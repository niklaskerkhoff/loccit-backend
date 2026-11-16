"""Find the citation links in a hyperref PDF and group them.

A citation link is a link annotation whose named destination is
`cite.<key>` (natbib/BibTeX) or `cite.<refsection>@<key>` (biblatex).

Known limitations:
- Compressed numeric ranges such as `[1-3]` only link their end points; the
  keys in between are not recovered.
- A group that continues on the next page is split at the page break.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

Rect = tuple[float, float, float, float]

_BIBLATEX_DEST = re.compile(r"\d+@(.+)", re.DOTALL)
# What may stand between two link rectangles of one citation ("Author (2020)")
# and between the citations of one group. Brackets count as separators so that
# "Author (2020); Other (2021)" from one \citet{a,b} stays together.
_SEPARATORS = re.compile(r"[\s,;()\[\]]*")
_RANGE_DASH = re.compile("\\s*[-\u2010-\u2015]\\s*")
_RECT_TOLERANCE = 0.5


@dataclass
class Citation:
    key: str
    dest: str
    # One rectangle per link annotation, in PDF user space (origin bottom
    # left, unit pt), i.e. the coordinates PDF.js works with.
    rects: list[Rect]
    # The same annotations as PDF.js names them ("<object number>R").
    annotation_ids: list[str]


@dataclass
class CitationGroup:
    id: int
    # 0-based page index
    page: int
    citations: list[Citation]

    @property
    def keys(self) -> list[str]:
        return [citation.key for citation in self.citations]


@dataclass
class CitationScan:
    groups: list[CitationGroup] = field(default_factory=list)
    link_count: int = 0
    # Prefix (text before the first dot) of every named link destination,
    # to tell what kind of links a PDF without citation links has.
    dest_prefixes: Counter = field(default_factory=Counter)
    # Neighbouring citations with only a dash in between, e.g. `[1-3]`.
    suspected_ranges: int = 0

    @property
    def keys(self) -> list[str]:
        """All cited keys in order of first appearance."""
        return list(dict.fromkeys(key for group in self.groups for key in group.keys))


def parse_cite_dest(dest: str | None) -> str | None:
    """The cite key a named destination stands for, or None."""
    if not dest or not dest.startswith("cite."):
        return None
    rest = dest[len("cite.") :]
    if biblatex := _BIBLATEX_DEST.fullmatch(rest):
        return biblatex.group(1)
    return rest or None


def scan_citations(pdf_path: Path) -> CitationScan:
    scan = CitationScan()
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            _scan_page(doc, page, scan)
    return scan


@dataclass
class _Link:
    key: str
    dest: str
    pdf_rect: Rect
    annotation_id: str
    # Range of the characters under the link in the page's text sequence.
    start: int
    end: int
    # False for a link over no text; it then forms a group of its own.
    placed: bool


def _scan_page(doc: pymupdf.Document, page: pymupdf.Page, scan: CitationScan) -> None:
    links: list[tuple[str, str, dict]] = []
    for link in page.get_links():
        scan.link_count += 1
        dest = _named_dest(doc, link)
        if dest is None:
            continue
        scan.dest_prefixes[dest.split(".", 1)[0]] += 1
        if key := parse_cite_dest(dest):
            links.append((key, dest, link))
    if not links:
        return

    chars = _page_chars(page)
    located = sorted(
        (_locate(key, dest, link, page, chars) for key, dest, link in links),
        key=lambda link: link.start,
    )
    text = "".join(char for char, _, _ in chars)

    group: CitationGroup | None = None
    previous: _Link | None = None
    for link in located:
        adjacent = previous is not None and previous.placed and link.placed
        gap = text[previous.end + 1 : link.start] if adjacent else ""
        if adjacent and _SEPARATORS.fullmatch(gap):
            if link.key != previous.key:
                group.citations.append(Citation(link.key, link.dest, [], []))
        else:
            if adjacent and _RANGE_DASH.fullmatch(gap):
                scan.suspected_ranges += 1
            group = CitationGroup(len(scan.groups), page.number, [])
            group.citations.append(Citation(link.key, link.dest, [], []))
            scan.groups.append(group)
        group.citations[-1].rects.append(link.pdf_rect)
        group.citations[-1].annotation_ids.append(link.annotation_id)
        previous = link


def _named_dest(doc: pymupdf.Document, link: dict) -> str | None:
    if name := link.get("nameddest"):
        return name
    xref = link.get("xref")
    if not xref:
        return None
    for path in ("A/D", "Dest"):
        kind, value = doc.xref_get_key(xref, path)
        if kind == "string":
            return value
        if kind == "name":
            return value.lstrip("/")
    return None


def _page_chars(page: pymupdf.Page) -> list[tuple[str, float, float]]:
    """Characters in reading order as (char, centre x, centre y); line and
    block breaks appear as a newline without position."""
    chars: list[tuple[str, float, float]] = []
    nowhere = float("nan")
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    x0, y0, x1, y1 = char["bbox"]
                    chars.append((char["c"], (x0 + x1) / 2, (y0 + y1) / 2))
            chars.append(("\n", nowhere, nowhere))
    return chars


def _locate(
    key: str, dest: str, link: dict, page: pymupdf.Page, chars: list[tuple[str, float, float]]
) -> _Link:
    # Link rectangles follow the page rotation, extracted text does not.
    rect = pymupdf.Rect(link["from"]) * page.derotation_matrix
    pdf_rect = tuple(round(v, 2) for v in (rect * ~page.transformation_matrix).normalize())
    annotation_id = f"{link['xref']}R"
    x0, y0 = rect.x0 - _RECT_TOLERANCE, rect.y0 - _RECT_TOLERANCE
    x1, y1 = rect.x1 + _RECT_TOLERANCE, rect.y1 + _RECT_TOLERANCE
    covered = [i for i, (_, x, y) in enumerate(chars) if x0 <= x <= x1 and y0 <= y <= y1]
    if not covered:
        return _Link(key, dest, pdf_rect, annotation_id, len(chars), len(chars), placed=False)
    return _Link(key, dest, pdf_rect, annotation_id, covered[0], covered[-1], placed=True)
