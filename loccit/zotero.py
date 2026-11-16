"""Read-only access to the local Zotero API and an in-memory index of the library.

API docs: https://www.zotero.org/support/dev/web_api/v3/local_api
Only GET requests are sent. The local API returns the full result set in one
response, so the whole library is read with a single request.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from loccit.bibfile import BibEntry, parse_bib
from loccit.normalize import normalize_doi, normalize_title, normalize_url

DEFAULT_BASE_URL = "http://localhost:23119/api/users/0"

_NON_REGULAR_TYPES = {"attachment", "note", "annotation"}
_YEAR = re.compile(r"\b(\d{4})\b")
_EXTRA_CITATION_KEY = re.compile(r"^\s*citation key\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_EXTRA_DOI = re.compile(r"^\s*doi\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)

# Where a cite key -> item mapping comes from (way B).
KEY_FROM_EXPORT = "export"
KEY_FROM_FIELD = "citationKey"
KEY_FROM_EXTRA = "extra"


class ZoteroError(Exception):
    """The local Zotero API could not be used."""


class ZoteroNotRunning(ZoteroError):
    pass


class ZoteroApiDisabled(ZoteroError):
    pass


class ZoteroClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Zotero-API-Version": "3"},
            timeout=httpx.Timeout(120, connect=3),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params: str) -> httpx.Response:
        try:
            response = self._http.get(path, params=params)
        except httpx.TransportError as error:
            raise ZoteroNotRunning(str(error)) from error
        if response.status_code == 403:
            raise ZoteroApiDisabled()
        return response

    def fetch_items(self) -> list[dict]:
        """All items of the user library, each with its own BibTeX and
        BibLaTeX export."""
        response = self._get("/items", include="data,bibtex,biblatex")
        if response.status_code != 200:
            raise ZoteroError(f"GET /items returned HTTP {response.status_code}")
        return response.json()

    def attachment_file_url(self, attachment_key: str) -> str | None:
        """The `file://` URL Zotero reports for an attachment, or None."""
        response = self._get(f"/items/{attachment_key}/file/view/url")
        if response.status_code != 200:
            return None
        return response.text.strip() or None

    def load_library(self) -> "LibraryIndex":
        return LibraryIndex.from_api_items(self.fetch_items())


def file_url_to_path(url: str | None) -> Path | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


@dataclass(frozen=True)
class Attachment:
    key: str
    parent_key: str
    title: str
    filename: str | None
    date_added: str


@dataclass
class ZoteroItem:
    key: str
    item_type: str
    title: str
    authors: list[str]
    year: str | None
    date: str = ""
    date_added: str = ""
    doi: str | None = None
    url: str | None = None
    # Normalised values an entry of a .bib can be compared with.
    dois: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    # Entry types of the item in Zotero's BibTeX and BibLaTeX export.
    entry_types: set[str] = field(default_factory=set)
    # cite key -> where it comes from (export, citationKey field, Extra line)
    cite_keys: dict[str, str] = field(default_factory=dict)
    # The attachment Zotero itself treats as the item's main one.
    main_attachment_key: str | None = None
    # PDF attachments: the main one first, then in the order they were added.
    pdfs: list[Attachment] = field(default_factory=list)

    def is_duplicate_of(self, other: "ZoteroItem") -> bool:
        """Whether the two items cannot be told apart as publications: the
        same DOI, or without any DOI the same title, year, type and URL."""
        if self.dois or other.dois:
            return bool(self.dois & other.dois)
        return (
            bool(self.titles & other.titles)
            and self.year == other.year
            and self.item_type == other.item_type
            and self.urls == other.urls
        )


@dataclass
class LibraryIndex:
    items: dict[str, ZoteroItem] = field(default_factory=dict)
    by_doi: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_url: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_title: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_cite_key: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def from_api_items(cls, raw_items: list[dict]) -> "LibraryIndex":
        index = cls()
        attachments: list[Attachment] = []
        for raw in raw_items:
            data = raw.get("data", {})
            if data.get("deleted"):
                continue
            item_type = data.get("itemType")
            if item_type == "attachment":
                if _is_pdf_file(data) and data.get("parentItem"):
                    attachments.append(
                        Attachment(
                            key=data["key"],
                            parent_key=data["parentItem"],
                            title=data.get("title", ""),
                            filename=data.get("filename"),
                            date_added=data.get("dateAdded", ""),
                        )
                    )
            elif item_type not in _NON_REGULAR_TYPES:
                index._add(_parse_item(raw))
        for attachment in sorted(attachments, key=lambda a: (a.date_added, a.key)):
            parent = index.items.get(attachment.parent_key)
            if parent:
                parent.pdfs.append(attachment)
        for item in index.items.values():
            item.pdfs.sort(key=lambda a: a.key != item.main_attachment_key)
        return index

    def _add(self, item: ZoteroItem) -> None:
        self.items[item.key] = item
        for doi in item.dois:
            self.by_doi[doi].append(item.key)
        for url in item.urls:
            self.by_url[url].append(item.key)
        for title in item.titles:
            self.by_title[title].append(item.key)
        for cite_key in item.cite_keys:
            self.by_cite_key[cite_key].append(item.key)


def _is_pdf_file(data: dict) -> bool:
    return data.get("contentType") == "application/pdf" and data.get("linkMode") != "linked_url"


def _parse_item(raw: dict) -> ZoteroItem:
    data = raw["data"]
    extra = data.get("extra") or ""
    # The item as Zotero's BibTeX translator exports it, i.e. the same shape
    # the entries of an Overleaf-imported .bib have.
    exported = _first_entry(raw.get("bibtex"))
    biblatex = _first_entry(raw.get("biblatex"))

    creators = data.get("creators") or []
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    year = exported.year if exported else None
    if not year and (match := _YEAR.search(data.get("date") or "")):
        year = match.group(1)

    item = ZoteroItem(
        key=data["key"],
        item_type=data["itemType"],
        title=data.get("title") or "",
        authors=[c.get("lastName") or c.get("name") or "" for c in authors],
        year=year,
        date=data.get("date") or "",
        date_added=data.get("dateAdded") or "",
        doi=data.get("DOI") or None,
        url=data.get("url") or None,
        entry_types={entry.entry_type for entry in (exported, biblatex) if entry},
        main_attachment_key=_main_attachment_key(raw),
    )

    extra_doi = _EXTRA_DOI.search(extra)
    for doi in (data.get("DOI"), exported.doi if exported else None, extra_doi and extra_doi[1]):
        if normalized := normalize_doi(doi):
            item.dois.add(normalized)
    for url in (data.get("url"), exported.fields.get("url") if exported else None):
        if normalized := normalize_url(url):
            item.urls.add(normalized)
    for title in (data.get("title"), exported.title if exported else None):
        if normalized := normalize_title(title):
            item.titles.add(normalized)

    if exported:
        item.cite_keys[exported.key] = KEY_FROM_EXPORT
    if field_key := (data.get("citationKey") or "").strip():
        item.cite_keys.setdefault(field_key, KEY_FROM_FIELD)
    if extra_key := _EXTRA_CITATION_KEY.search(extra):
        item.cite_keys.setdefault(extra_key[1], KEY_FROM_EXTRA)
    return item


def _first_entry(exported: str | None) -> BibEntry | None:
    return next(iter(parse_bib(exported or "").entries.values()), None)


def _main_attachment_key(raw: dict) -> str | None:
    """Zotero links its "best attachment" of an item as `links.attachment`."""
    href = (raw.get("links") or {}).get("attachment", {}).get("href")
    return href.rstrip("/").rsplit("/", 1)[-1] if href else None
