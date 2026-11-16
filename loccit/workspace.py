"""What the server works on: the stored documents and the Zotero library."""

import hashlib
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from loccit import paths
from loccit.bibfile import load_bib
from loccit.citations import CitationScan, scan_citations
from loccit.resolve import Resolution, Resolver, load_overrides
from loccit.store import Document, Store
from loccit.zotero import (
    LibraryIndex,
    ZoteroApiDisabled,
    ZoteroClient,
    ZoteroError,
    ZoteroNotRunning,
)

THESIS_FILE = "thesis.pdf"
BIB_FILE = "refs.bib"

# Browsers name a repeated download "thesis (1).pdf".
_DOWNLOAD_COUNTER = re.compile(r" \(\d+\)$")

# CitationsView.zotero_status
ZOTERO_OK = "ok"
ZOTERO_NOT_RUNNING = "not_running"
ZOTERO_API_DISABLED = "api_disabled"
ZOTERO_ERROR = "error"


class DocumentNotFound(Exception):
    pass


class InvalidPdf(Exception):
    pass


@dataclass
class CitationsView:
    document: Document
    scan: CitationScan
    zotero_status: str = ZOTERO_OK
    zotero_detail: str | None = None
    library: LibraryIndex | None = None
    resolutions: dict[str, Resolution] = field(default_factory=dict)


class Workspace:
    def __init__(self, root: Path, zotero: ZoteroClient | None = None):
        self.root = root
        self.store = Store(paths.database_file(root))
        self.zotero = zotero or ZoteroClient()
        self._lock = threading.Lock()
        self._library: LibraryIndex | None = None
        # attachment key -> `file://` URL as reported by Zotero
        self._file_urls: dict[str, str | None] = {}
        # Paper PDFs that were resolved and may therefore be served.
        self._papers: dict[str, Path] = {}
        # document id -> (hash of the scanned PDF, its citations)
        self._scans: dict[int, tuple[str, CitationScan]] = {}

    # --- documents ----------------------------------------------------------

    def import_document(self, filename: str, pdf: bytes, bib: bytes | None = None) -> Document:
        """Store a thesis PDF and its .bib. A PDF of the same name replaces
        the stored version; without a new .bib the stored one is kept."""
        try:
            with pymupdf.open(stream=pdf, filetype="pdf") as doc:
                if doc.page_count == 0:
                    raise InvalidPdf(filename)
        except InvalidPdf:
            raise
        except Exception as error:
            raise InvalidPdf(filename) from error

        name = document_name(filename)
        existing = self.store.document_named(name)
        has_bib = bib is not None or (existing is not None and existing.has_bib)
        document = self.store.save_document(name, hashlib.sha256(pdf).hexdigest(), has_bib)
        folder = self._folder(document.id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / THESIS_FILE).write_bytes(pdf)
        if bib is not None:
            (folder / BIB_FILE).write_bytes(bib)
        return document

    def delete_document(self, document_id: int) -> None:
        self._document(document_id)
        self.store.delete_document(document_id)
        shutil.rmtree(self._folder(document_id), ignore_errors=True)
        self._scans.pop(document_id, None)

    def thesis_path(self, document_id: int) -> Path:
        return self._folder(self._document(document_id).id) / THESIS_FILE

    def _document(self, document_id: int) -> Document:
        document = self.store.document(document_id)
        if document is None:
            raise DocumentNotFound(document_id)
        return document

    def _folder(self, document_id: int) -> Path:
        return paths.documents_dir(self.root) / str(document_id)

    # --- citations ----------------------------------------------------------

    def citations(self, document_id: int, reload: bool = False) -> CitationsView:
        """The citations of a document and what each cite key resolves to.
        `reload` reads the Zotero library again instead of using the cache."""
        document = self._document(document_id)
        self.store.touch_document(document_id)
        view = CitationsView(document, self._scan(document))
        with self._lock:
            if reload:
                self._library = None
                self._file_urls.clear()
            try:
                if self._library is None:
                    self._library = self.zotero.load_library()
                resolver = Resolver(
                    self._library,
                    self._locate_file,
                    bib=load_bib(self._folder(document_id) / BIB_FILE) if document.has_bib else None,
                    overrides=load_overrides(paths.overrides_file(self.root)),
                    item_choices=self.store.item_choices(),
                    pdf_choices=self.store.pdf_choices(),
                )
                view.resolutions = {key: resolver.resolve(key) for key in view.scan.keys}
                view.library = self._library
            except ZoteroNotRunning:
                view.zotero_status = ZOTERO_NOT_RUNNING
            except ZoteroApiDisabled:
                view.zotero_status = ZOTERO_API_DISABLED
            except ZoteroError as error:
                view.zotero_status = ZOTERO_ERROR
                view.zotero_detail = str(error)
            for resolution in view.resolutions.values():
                for pdf in resolution.pdfs:
                    if pdf.exists:
                        self._papers[pdf.attachment_key] = pdf.path
        return view

    def paper_path(self, attachment_key: str) -> Path | None:
        """The file of a paper, if loccit itself resolved a citation to it."""
        return self._papers.get(attachment_key)

    def _scan(self, document: Document) -> CitationScan:
        cached = self._scans.get(document.id)
        if cached is None or cached[0] != document.pdf_sha256:
            scan = scan_citations(self._folder(document.id) / THESIS_FILE)
            cached = self._scans[document.id] = (document.pdf_sha256, scan)
        return cached[1]

    def _locate_file(self, attachment_key: str) -> str | None:
        if attachment_key not in self._file_urls:
            self._file_urls[attachment_key] = self.zotero.attachment_file_url(attachment_key)
        return self._file_urls[attachment_key]


def document_name(filename: str) -> str:
    """The name a document is listed and replaced under."""
    path = Path(filename.replace("\\", "/"))
    return _DOWNLOAD_COUNTER.sub("", path.stem).strip() + path.suffix.lower()
