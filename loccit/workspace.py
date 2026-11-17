"""What the server works on: the stored documents and the Zotero library."""

import hashlib
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import pymupdf

from loccit import paths
from loccit import resolve as res
from loccit.bibfile import load_bib
from loccit.citations import CitationScan, scan_citations
from loccit.context import Context, citation_context
from loccit.models import ModelSettings, get_api_key, load_model_settings
from loccit.resolve import Resolution, Resolver, load_overrides
from loccit.sentences import SENTENCE_VERSION, extract_sentences
from loccit.store import Document, Run, Store, StoredPassage, TaskPaper, task_hash
from loccit.strategies import Paper, Passage, Strategy, StrategyError, passage
from loccit.strategies.claude_citations import ClaudeCitations
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


class GroupNotFound(Exception):
    pass


@dataclass(frozen=True)
class CheckPaper:
    """A paper as it goes into a check."""

    attachment_key: str
    sha256: str
    paper: Paper


@dataclass
class Check:
    """What is known about the check of one citation group with one model
    and thinking level: the latest run, if there is one."""

    # The checked paragraph; None if the citation lies over no text.
    context: Context | None = None
    papers: list[CheckPaper] = field(default_factory=list)
    # Cited keys without a readable paper; the verdict does not cover them.
    missing: list[str] = field(default_factory=list)
    run: Run | None = None
    # The run's passages with their rectangles, and the PDF they lie in.
    passages: list[tuple[Passage, str]] = field(default_factory=list)
    # Why there is no result: a code for the UI, and detail for the user.
    error: str | None = None
    error_detail: str = ""


@dataclass
class CitationsView:
    document: Document
    scan: CitationScan
    zotero_status: str = ZOTERO_OK
    zotero_detail: str | None = None
    library: LibraryIndex | None = None
    resolutions: dict[str, Resolution] = field(default_factory=dict)


class Workspace:
    def __init__(
        self,
        root: Path,
        zotero: ZoteroClient | None = None,
        strategy_factory: Callable[[], Strategy] | None = None,
    ):
        self.root = root
        self.store = Store(paths.database_file(root))
        self.zotero = zotero or ZoteroClient()
        self._strategy_factory = strategy_factory or self._claude_citations
        self._models: ModelSettings | None = None
        # (path, mtime, size) of a paper file -> its hash and sentences
        self._paper_files: dict[tuple[str, int, int], tuple[str, list]] = {}
        # (document id, PDF hash, group id) -> checked paragraph
        self._contexts: dict[tuple[int, str, int], Context | None] = {}
        # (task hash, model, thinking level) -> lock held while that check runs
        self._run_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._run_locks_guard = threading.Lock()
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
                    bib=load_bib(self._folder(document_id) / BIB_FILE)
                    if document.has_bib
                    else None,
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

    # --- checks -------------------------------------------------------------

    @property
    def models(self) -> ModelSettings:
        if self._models is None:
            self._models = load_model_settings(paths.models_file(self.root))
        return self._models

    def check(
        self, document_id: int, group_id: int, model: str, thinking: str, *, run: bool = False
    ) -> Check:
        """The latest stored run for a citation group, model and thinking
        level. With `run`, the papers are checked now and the result is
        stored as a new run first."""
        view = self.citations(document_id)
        if not 0 <= group_id < len(view.scan.groups):
            raise GroupNotFound(group_id)
        group = view.scan.groups[group_id]
        check = Check()
        if view.zotero_status != ZOTERO_OK:
            check.error = "zotero_unavailable"
            return check

        context_key = (document_id, view.document.pdf_sha256, group_id)
        if context_key not in self._contexts:
            self._contexts[context_key] = citation_context(self.thesis_path(document_id), group)
        check.context = self._contexts[context_key]
        for citation in group.citations:
            paper = self._check_paper(view.resolutions[citation.key])
            if paper:
                check.papers.append(paper)
            else:
                check.missing.append(citation.key)
        if check.context is None:
            check.error = "no_context"
            return check
        if not check.papers:
            # A missing paper is no verdict, and no reason to ask a model.
            check.error = "no_papers"
            return check

        task_papers = [
            TaskPaper(p.paper.cite_key, p.sha256, SENTENCE_VERSION) for p in check.papers
        ]
        key = task_hash(check.context.hash, group.keys, task_papers)
        if run:
            try:
                check.run = self._run_once(check, group.keys, task_papers, model, thinking)
            except StrategyError as error:
                check.error, check.error_detail = error.code, error.detail
                return check
        else:
            check.run = self._latest_run(key, model, thinking)

        if check.run:
            by_key = {p.paper.cite_key: p for p in check.papers}
            for stored in check.run.passages:
                paper = by_key[stored.cite_key]
                found = passage(
                    paper.paper, stored.start_sentence, stored.end_sentence, stored.reason
                )
                if found:
                    check.passages.append((found, paper.attachment_key))
        return check

    def _latest_run(self, key: str, model: str, thinking: str) -> Run | None:
        task = self.store.find_task(key)
        return self.store.latest_run(task.id, model, thinking) if task else None

    def _run_once(
        self,
        check: Check,
        cite_keys: list[str],
        task_papers: list[TaskPaper],
        model: str,
        thinking: str,
    ) -> Run:
        """Run the check, unless the same check is already running (say,
        from a second browser tab): then wait for it and return its run
        instead of paying for another one."""
        key = task_hash(check.context.hash, cite_keys, task_papers)
        before = self._latest_run(key, model, thinking)
        with self._run_locks_guard:
            lock = self._run_locks.setdefault((key, model, thinking), threading.Lock())
        with lock:
            latest = self._latest_run(key, model, thinking)
            if latest and (before is None or latest.id != before.id):
                return latest
            return self._run_check(check, cite_keys, task_papers, model, thinking)

    def _run_check(
        self,
        check: Check,
        cite_keys: list[str],
        task_papers: list[TaskPaper],
        model: str,
        thinking: str,
    ) -> Run:
        config = self.models.get(model)
        if config is None:
            raise StrategyError("unknown_model", model)
        strategy = self._strategy_factory()
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        started = time.perf_counter()
        result = strategy.find_passages(
            check.context,
            [p.paper for p in check.papers],
            model,
            thinking,
            missing=tuple(check.missing),
        )
        duration_ms = round((time.perf_counter() - started) * 1000)
        task = self.store.get_or_create_task(
            check.context.hash, check.context.text, cite_keys, task_papers, check.missing
        )
        return self.store.add_run(
            task.id,
            model=model,
            thinking=thinking,
            strategy=strategy.name,
            prompt_version=strategy.prompt_version,
            started_at=started_at,
            duration_ms=duration_ms,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_write_tokens=result.usage.cache_write_tokens,
            cost_usd=config.cost(result.usage),
            verdict=result.verdict,
            response_text=result.response_text,
            passages=[
                StoredPassage(p.cite_key, p.start_sentence, p.end_sentence, p.page, p.reason)
                for p in result.passages
            ],
        )

    def _check_paper(self, resolution: Resolution) -> CheckPaper | None:
        """The paper a cite key resolves to, read into sentences; None if
        there is no PDF or it has no text (a scan)."""
        if resolution.status != res.OK:
            return None
        path = resolution.pdf.path
        stat = path.stat()
        file_key = (str(path), stat.st_mtime_ns, stat.st_size)
        if file_key not in self._paper_files:
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            self._paper_files[file_key] = (sha256, extract_sentences(path))
        sha256, sentences = self._paper_files[file_key]
        if not sentences:
            return None
        paper = Paper(
            cite_key=resolution.cite_key,
            title=resolution.title or resolution.cite_key,
            authors=resolution.authors,
            year=resolution.year,
            sentences=sentences,
        )
        return CheckPaper(resolution.pdf.attachment_key, sha256, paper)

    def _claude_citations(self) -> Strategy:
        api_key = get_api_key()
        if not api_key:
            raise StrategyError("no_api_key")
        return ClaudeCitations(anthropic.Anthropic(api_key=api_key), self.models)


def document_name(filename: str) -> str:
    """The name a document is listed and replaced under."""
    path = Path(filename.replace("\\", "/"))
    return _DOWNLOAD_COUNTER.sub("", path.stem).strip() + path.suffix.lower()
