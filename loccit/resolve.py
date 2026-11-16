"""Deterministic mapping from a cite key to a Zotero item and its PDF.

Precedence: manual override, then way A (via the .bib), then way B (via the
cite keys Zotero itself produces). Every lookup is exact.

Several hits are handled by two fixed rules:
- Items that cannot be told apart (see `ZoteroItem.is_duplicate_of`) are
  duplicates. loccit picks one of them: an item whose PDF is on disk first,
  among those the one added last.
- Items that can be told apart are different versions of a publication.
  loccit never picks between those; the user has to choose.
"""

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loccit.bibfile import BibEntry, BibFile, strip_braces
from loccit.normalize import normalize_doi, normalize_title, normalize_url
from loccit.zotero import LibraryIndex, ZoteroItem, file_url_to_path

# WayResult.status
MATCH = "match"
CHOICE = "choice"
NONE = "none"
SKIPPED = "skipped"

# Resolution.status: a PDF is there, the user has to choose between versions,
# or one of the three reasons why a paper is not available.
OK = "ok"
CHOICE_NEEDED = "choice_needed"
NO_ENTRY = "no_entry"
NO_PDF = "no_pdf"
FILE_MISSING = "file_missing"

# A whole-library BibTeX export names items with the same key `key`,
# `key-1`, `key-2`, ...
_SUFFIXED_KEY = re.compile(r"(.+)-\d+")

# Returns the `file://` URL Zotero reports for an attachment key, or None.
FileLocator = Callable[[str], str | None]


@dataclass(frozen=True)
class PdfFile:
    attachment_key: str
    filename: str | None
    path: Path | None
    exists: bool


@dataclass(frozen=True)
class WayResult:
    status: str
    # How it matched ("doi", "url", "title+year", "export", "suffix", ...) or
    # why not ("no_bib", "not_in_bib", "duplicate_in_bib", "no_hit",
    # "contradicts_bib", "unknown_key").
    how: str = ""
    # One group per distinguishable publication. The items of a group are
    # duplicates of each other; the one loccit picks comes first.
    groups: tuple[tuple[str, ...], ...] = ()
    # Way A: (item key, field) of items dropped for contradicting the .bib.
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def item_key(self) -> str | None:
        return self.groups[0][0] if self.status == MATCH else None

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(group[0] for group in self.groups)

    @property
    def members(self) -> set[str]:
        return {key for group in self.groups for key in group}


@dataclass
class Resolution:
    cite_key: str
    way_a: WayResult
    way_b: WayResult
    status: str
    override_item_key: str | None = None
    item_key: str | None = None
    # Which rule chose the item: "override", "choice", "a" or "b".
    source: str | None = None
    # Whether A and B found the same publication; None if one found none.
    ways_agree: bool | None = None
    # A and B both found something, but not the same thing.
    conflict: bool = False
    # For NO_ENTRY: "unknown_override" or "no_match".
    detail: str = ""
    # The versions to choose from; kept after a choice so it can be changed.
    candidates: tuple[tuple[str, ...], ...] = ()
    # All items the chosen one is a duplicate of, itself included.
    duplicates: tuple[str, ...] = ()
    # The item's PDFs, Zotero's main attachment first, and the one in use.
    pdfs: list[PdfFile] = field(default_factory=list)
    pdf: PdfFile | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: str | None = None

    @property
    def pdf_path(self) -> Path | None:
        return self.pdf.path if self.pdf else None


def load_overrides(path: Path) -> dict[str, str]:
    """Read `[overrides]` (cite key = "ZOTEROITEMKEY") from overrides.toml."""
    if not path.is_file():
        return {}
    with path.open("rb") as file:
        table = tomllib.load(file).get("overrides", {})
    return {str(key): str(value) for key, value in table.items()}


def contradiction(entry: BibEntry, item: ZoteroItem) -> str | None:
    """The field in which a Zotero item contradicts a .bib entry, if any.

    An equal DOI settles it: such an item is the cited publication even if
    its type or URL differ.
    """
    doi = normalize_doi(entry.doi)
    if doi and item.dois:
        return None if doi in item.dois else "doi"
    url = normalize_url(entry.fields.get("url"))
    if url and item.urls and url not in item.urls:
        return "url"
    if item.entry_types and entry.entry_type not in item.entry_types:
        return "type"
    return None


class Resolver:
    """Resolves cite keys against one snapshot of the Zotero library."""

    def __init__(
        self,
        library: LibraryIndex,
        locate_file: FileLocator,
        *,
        bib: BibFile | None = None,
        overrides: dict[str, str] | None = None,
        item_choices: dict[str, str] | None = None,
        pdf_choices: dict[str, str] | None = None,
    ):
        self.library = library
        self.bib = bib
        self.overrides = overrides or {}
        # cite key -> item key the user chose among several versions
        self.item_choices = item_choices or {}
        # item key -> attachment key the user chose among several PDFs
        self.pdf_choices = pdf_choices or {}
        self._locate_file = locate_file
        self._files: dict[str, list[PdfFile]] = {}

    def pdf_files(self, item_key: str) -> list[PdfFile]:
        if item_key not in self._files:
            files = []
            for attachment in self.library.items[item_key].pdfs:
                path = file_url_to_path(self._locate_file(attachment.key))
                exists = path is not None and path.is_file()
                files.append(PdfFile(attachment.key, attachment.filename, path, exists))
            self._files[item_key] = files
        return self._files[item_key]

    def has_pdf_on_disk(self, item_key: str) -> bool:
        return any(file.exists for file in self.pdf_files(item_key))

    def are_duplicates(self, key: str, other: str) -> bool:
        items = self.library.items
        return key == other or items[key].is_duplicate_of(items[other])

    # --- way A: via the .bib ------------------------------------------------

    def way_a(self, cite_key: str) -> WayResult:
        if self.bib is None:
            return WayResult(SKIPPED, how="no_bib")
        if cite_key in self.bib.duplicate_keys:
            return WayResult(NONE, how="duplicate_in_bib")
        entry = self.bib.entries.get(cite_key)
        if entry is None:
            return WayResult(NONE, how="not_in_bib")

        library = self.library
        title = normalize_title(entry.title)
        steps = [
            ("doi", library.by_doi.get(normalize_doi(entry.doi) or "", [])),
            ("url", library.by_url.get(normalize_url(entry.fields.get("url")) or "", [])),
            (
                "title+year",
                [k for k in library.by_title.get(title, []) if library.items[k].year == entry.year],
            ),
        ]
        rejected: dict[str, str] = {}
        choice: WayResult | None = None
        for how, hits in steps:
            kept = []
            for key in dict.fromkeys(hits):
                if field_name := contradiction(entry, library.items[key]):
                    rejected.setdefault(key, field_name)
                elif choice is None or key in choice.members:
                    # A later step may only narrow an earlier choice down.
                    kept.append(key)
            if not kept:
                continue
            result = self._decide(how, kept, tuple(rejected.items()))
            if result.status == MATCH:
                return result
            choice = result
        if choice:
            return choice
        how = "contradicts_bib" if rejected else "no_hit"
        return WayResult(NONE, how=how, rejected=tuple(rejected.items()))

    # --- way B: via the keys Zotero produces --------------------------------

    def way_b(self, cite_key: str) -> WayResult:
        by_key = self.library.by_cite_key
        if hits := list(dict.fromkeys(by_key.get(cite_key, []))):
            return self._decide(self.library.items[hits[0]].cite_keys[cite_key], hits)
        suffixed = _SUFFIXED_KEY.fullmatch(cite_key)
        if suffixed and (hits := list(dict.fromkeys(by_key.get(suffixed.group(1), [])))):
            # Which item an export gave which suffix is not reproducible, so
            # this only resolves if all items of the base key are duplicates.
            return self._decide("suffix", hits)
        return WayResult(NONE, how="unknown_key")

    def _decide(
        self, how: str, item_keys: list[str], rejected: tuple[tuple[str, str], ...] = ()
    ) -> WayResult:
        groups = self._group_duplicates(item_keys)
        status = MATCH if len(groups) == 1 else CHOICE
        return WayResult(status, how=how, groups=groups, rejected=rejected)

    def _group_duplicates(self, item_keys: list[str]) -> tuple[tuple[str, ...], ...]:
        groups: list[list[str]] = []
        for key in item_keys:
            joined = [group for group in groups if any(self.are_duplicates(key, k) for k in group)]
            for group in joined:
                groups.remove(group)
            groups.append([k for group in joined for k in group] + [key])
        return tuple(tuple(self._pick_first(group)) for group in groups)

    def _pick_first(self, duplicates: list[str]) -> list[str]:
        """Order duplicates: PDF on disk first, among those the one added last."""
        if len(duplicates) < 2:
            return duplicates
        items = self.library.items
        return sorted(
            duplicates,
            key=lambda k: (self.has_pdf_on_disk(k), items[k].date_added, k),
            reverse=True,
        )

    # --- both ways together -------------------------------------------------

    def resolve(self, cite_key: str) -> Resolution:
        way_a = self.way_a(cite_key)
        way_b = self.way_b(cite_key)
        resolution = Resolution(cite_key, way_a, way_b, status=NO_ENTRY)
        resolution.override_item_key = self.overrides.get(cite_key)
        b_fits_a = way_b.status == MATCH and (
            (way_a.status == MATCH and self.are_duplicates(way_a.item_key, way_b.item_key))
            or (way_a.status == CHOICE and way_b.item_key in way_a.members)
        )
        if way_a.status == MATCH and way_b.status == MATCH:
            resolution.ways_agree = b_fits_a
        resolution.conflict = (
            way_b.status == MATCH and way_a.status in (MATCH, CHOICE) and not b_fits_a
        )

        if resolution.override_item_key:
            resolution.source = "override"
            resolution.item_key = resolution.override_item_key
        elif way_a.status == MATCH:
            resolution.source = "a"
            resolution.item_key = way_a.item_key
            resolution.duplicates = way_a.groups[0]
        elif way_b.status == MATCH and (way_a.status != CHOICE or b_fits_a):
            resolution.source = "b"
            resolution.item_key = way_b.item_key
            resolution.duplicates = way_b.groups[0]
        elif CHOICE in (way_a.status, way_b.status):
            # A's candidates count; a B that names something else joins them.
            groups = way_a.groups if way_a.status == CHOICE else way_b.groups
            if way_a.status == CHOICE and way_b.status == MATCH:
                groups += way_b.groups
            resolution.candidates = groups
            chosen = self.item_choices.get(cite_key)
            group = next((group for group in groups if chosen in group), None)
            if group:
                resolution.source = "choice"
                resolution.item_key = group[0]
                resolution.duplicates = group
            else:
                resolution.status = CHOICE_NEEDED
        if len(resolution.duplicates) < 2:
            resolution.duplicates = ()

        item = self.library.items.get(resolution.item_key) if resolution.item_key else None
        entry = self.bib.entries.get(cite_key) if self.bib else None
        _fill_metadata(resolution, item, entry)
        if item is None:
            if resolution.status == NO_ENTRY:
                resolution.detail = (
                    "unknown_override" if resolution.override_item_key else "no_match"
                )
            return resolution

        resolution.pdfs = self.pdf_files(item.key)
        if not resolution.pdfs:
            resolution.status = NO_PDF
            return resolution
        on_disk = [file for file in resolution.pdfs if file.exists]
        if not on_disk:
            resolution.status = FILE_MISSING
            resolution.pdf = resolution.pdfs[0]
            return resolution
        # The user's pick, else Zotero's main attachment, else the next PDF
        # whose file is there.
        picked = self.pdf_choices.get(item.key)
        resolution.pdf = next((f for f in on_disk if f.attachment_key == picked), on_disk[0])
        resolution.status = OK
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
        resolution.authors = tuple(entry.last_names)
        resolution.year = entry.year
