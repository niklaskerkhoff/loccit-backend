"""Deterministic mapping from a cite key to a Zotero item and its PDF.

Precedence: manual override, then way A (via the .bib: DOI, else title and
year), then way B (via the cite keys Zotero itself produces). Every lookup is
exact; anything that is not a unique hit counts as not resolved.
"""

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loccit.bibfile import BibEntry, BibFile, strip_braces
from loccit.normalize import normalize_doi, normalize_title
from loccit.zotero import LibraryIndex, ZoteroItem, file_url_to_path

# WayResult.status
MATCH = "match"
NONE = "none"
AMBIGUOUS = "ambiguous"
SKIPPED = "skipped"

# Resolution.status: a PDF is there, or one of the three reasons why not.
OK = "ok"
NO_ENTRY = "no_entry"
NO_PDF = "no_pdf"
FILE_MISSING = "file_missing"

# Returns the `file://` URL Zotero reports for an attachment key, or None.
FileLocator = Callable[[str], str | None]


@dataclass(frozen=True)
class WayResult:
    status: str
    item_key: str | None = None
    # How it matched ("doi", "title+year", "export", ...) or why it did not
    # ("no_bib", "not_in_bib", "duplicate_in_bib", "no_hit", "unknown_key").
    how: str = ""
    candidates: tuple[str, ...] = ()


@dataclass
class Resolution:
    cite_key: str
    way_a: WayResult
    way_b: WayResult
    status: str
    override_item_key: str | None = None
    item_key: str | None = None
    # Which rule chose the item: "override", "a" or "b".
    source: str | None = None
    # A and B both found something, but not the same thing.
    conflict: bool = False
    # For NO_ENTRY: "unknown_override", "ambiguous" or "no_match".
    detail: str = ""
    attachment_key: str | None = None
    pdf_path: Path | None = None
    other_pdf_count: int = 0
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: str | None = None

    @property
    def ways_agree(self) -> bool | None:
        if self.way_a.status != MATCH or self.way_b.status != MATCH:
            return None
        return self.way_a.item_key == self.way_b.item_key


def load_overrides(path: Path) -> dict[str, str]:
    """Read `[overrides]` (cite key = "ZOTEROITEMKEY") from overrides.toml."""
    if not path.is_file():
        return {}
    with path.open("rb") as file:
        table = tomllib.load(file).get("overrides", {})
    return {str(key): str(value) for key, value in table.items()}


def resolve_way_a(cite_key: str, bib: BibFile | None, library: LibraryIndex) -> WayResult:
    if bib is None:
        return WayResult(SKIPPED, how="no_bib")
    if cite_key in bib.duplicate_keys:
        return WayResult(NONE, how="duplicate_in_bib")
    entry = bib.entries.get(cite_key)
    if entry is None:
        return WayResult(NONE, how="not_in_bib")

    doi = normalize_doi(entry.doi)
    if doi and (hits := _unique(library.by_doi.get(doi, []))):
        return _hit("doi", hits)

    title = normalize_title(entry.title)
    hits = [
        key
        for key in _unique(library.by_title.get(title, []))
        if library.items[key].year == entry.year
        # Same title and year but another DOI is another publication.
        and not (doi and library.items[key].dois and doi not in library.items[key].dois)
    ]
    if title and hits:
        return _hit("title+year", hits)
    return WayResult(NONE, how="no_hit")


def resolve_way_b(cite_key: str, library: LibraryIndex) -> WayResult:
    hits = _unique(library.by_cite_key.get(cite_key, []))
    if not hits:
        return WayResult(NONE, how="unknown_key")
    return _hit(library.items[hits[0]].cite_keys[cite_key], hits)


def _unique(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


def _hit(how: str, hits: list[str]) -> WayResult:
    if len(hits) == 1:
        return WayResult(MATCH, item_key=hits[0], how=how)
    return WayResult(AMBIGUOUS, how=how, candidates=tuple(hits))


def resolve(
    cite_key: str,
    *,
    bib: BibFile | None,
    library: LibraryIndex,
    overrides: dict[str, str],
    locate_file: FileLocator,
) -> Resolution:
    way_a = resolve_way_a(cite_key, bib, library)
    way_b = resolve_way_b(cite_key, library)
    entry = bib.entries.get(cite_key) if bib else None
    resolution = Resolution(cite_key, way_a, way_b, status=NO_ENTRY)
    resolution.override_item_key = overrides.get(cite_key)
    resolution.conflict = (
        way_a.status == MATCH and way_b.status == MATCH and way_a.item_key != way_b.item_key
    ) or (
        # A cannot decide between several items and B names yet another one.
        way_a.status == AMBIGUOUS
        and way_b.status == MATCH
        and way_b.item_key not in way_a.candidates
    )

    if resolution.override_item_key:
        resolution.source = "override"
        resolution.item_key = resolution.override_item_key
    elif way_a.status == MATCH:
        resolution.source = "a"
        resolution.item_key = way_a.item_key
    elif way_b.status == MATCH:
        resolution.source = "b"
        resolution.item_key = way_b.item_key

    item = library.items.get(resolution.item_key) if resolution.item_key else None
    _fill_metadata(resolution, item, entry)
    if item is None:
        if resolution.override_item_key:
            resolution.detail = "unknown_override"
        elif AMBIGUOUS in (way_a.status, way_b.status):
            resolution.detail = "ambiguous"
        else:
            resolution.detail = "no_match"
        return resolution

    if not item.pdfs:
        resolution.status = NO_PDF
        return resolution

    # Fixed rule: the PDF that was added first.
    attachment = item.pdfs[0]
    resolution.attachment_key = attachment.key
    resolution.other_pdf_count = len(item.pdfs) - 1
    path = file_url_to_path(locate_file(attachment.key))
    if path is None or not path.is_file():
        resolution.status = FILE_MISSING
        resolution.pdf_path = path
        return resolution
    resolution.status = OK
    resolution.pdf_path = path
    return resolution


def _fill_metadata(
    resolution: Resolution, item: ZoteroItem | None, entry: BibEntry | None
) -> None:
    if item:
        resolution.title = item.title
        resolution.authors = tuple(item.authors)
        resolution.year = item.year
    elif entry:
        resolution.title = strip_braces(entry.title or "") or None
        resolution.authors = tuple(entry.authors)
        resolution.year = entry.year
