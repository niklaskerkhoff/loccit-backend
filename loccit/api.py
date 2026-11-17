"""The HTTP API the web frontend talks to. Schema: `/api/openapi.json`."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from loccit import models as model_settings
from loccit import resolve as res
from loccit import workspace as ws
from loccit.store import Document as StoredDocument
from loccit.store import Run as StoredRun
from loccit.workspace import (
    CitationsView,
    DocumentNotFound,
    GroupNotFound,
    InvalidPdf,
    Workspace,
)
from loccit.zotero import LibraryIndex


class Document(BaseModel):
    id: int
    name: str
    has_bib: bool
    version: str = Field(description="Hash of the PDF; changes when a new version is uploaded.")
    uploaded_at: str
    opened_at: str


class Citation(BaseModel):
    key: str
    rects: list[tuple[float, float, float, float]] = Field(
        description="Link rectangles in PDF user space (origin bottom left)."
    )
    annotation_ids: list[str] = Field(
        description="Ids of the link annotations as PDF.js names them."
    )


class CitationGroup(BaseModel):
    id: int
    page: int = Field(description="0-based page index.")
    citations: list[Citation]


class PdfFile(BaseModel):
    attachment_key: str
    filename: str | None
    exists: bool


class Candidate(BaseModel):
    """One version of a publication the user can choose."""

    item_key: str
    item_type: str
    title: str
    authors: list[str]
    date: str
    doi: str | None
    url: str | None
    pdf_count: int
    duplicates: list[str] = Field(
        description="Indistinguishable Zotero items this one was picked from, itself included."
    )


class Way(BaseModel):
    status: Literal["match", "choice", "none", "skipped"]
    how: str
    item_key: str | None
    candidates: list[str]


class Source(BaseModel):
    """What a cite key resolves to."""

    cite_key: str
    status: Literal["ok", "choice_needed", "no_entry", "no_pdf", "file_missing"]
    detail: str
    item_key: str | None
    chosen_by: Literal["override", "choice", "a", "b"] | None
    conflict: bool = Field(description="Way A and way B found different publications.")
    duplicates: list[str]
    candidates: list[Candidate]
    pdfs: list[PdfFile]
    attachment_key: str | None = Field(description="The PDF in use; fetch it from /api/papers.")
    title: str | None
    authors: list[str]
    year: str | None
    way_a: Way
    way_b: Way


class ZoteroState(BaseModel):
    status: Literal["ok", "not_running", "api_disabled", "error"]
    detail: str | None


class DocumentCitations(BaseModel):
    document: Document
    zotero: ZoteroState
    groups: list[CitationGroup]
    sources: dict[str, Source] = Field(description="By cite key; empty if Zotero is unavailable.")
    link_count: int
    named_destinations: dict[str, int] = Field(
        description="Named link destinations by prefix, to explain a PDF without citation links."
    )
    suspected_ranges: int


class Model(BaseModel):
    id: str
    label: str
    levels: list[str] = Field(
        description='Thinking levels the model accepts, the default first: "off" or an effort level.'
    )


class Models(BaseModel):
    default_model: str
    models: list[Model]
    has_api_key: bool = Field(description="Whether an Anthropic API key is configured.")


class PageRect(BaseModel):
    page: int = Field(description="0-based page index.")
    rect: tuple[float, float, float, float] = Field(description="In PDF user space.")


class Passage(BaseModel):
    """A stretch of a paper that supports the checked text."""

    cite_key: str
    attachment_key: str = Field(description="The PDF the passage lies in.")
    page: int = Field(description="0-based index of the page the passage starts on.")
    start_sentence: int
    end_sentence: int = Field(description="Exclusive.")
    rects: list[PageRect] = Field(description="One rectangle per line of the passage.")
    reason: str


class Run(BaseModel):
    """One check of a citation group by one model at one thinking level."""

    id: int
    model: str
    thinking: str
    strategy: str
    prompt_version: str
    started_at: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float = Field(description="Estimate from the prices in models.toml.")
    verdict: Literal["supported", "partially_supported", "not_found"]
    passages: list[Passage] = Field(description="Strongest first, across all papers.")


class CheckError(BaseModel):
    code: str = Field(
        description="no_api_key, invalid_api_key, no_papers, no_context, zotero_unavailable, "
        "unknown_model, unsupported_thinking, rate_limited, refused, truncated, bad_response, "
        "permission_denied, api_error or no_connection."
    )
    detail: str


class Check(BaseModel):
    context: str | None = Field(
        description="The checked paragraph of the thesis, the citation marked with <cite_here>."
    )
    checked_keys: list[str] = Field(description="Cite keys whose paper is read.")
    missing_keys: list[str] = Field(
        description="Cite keys of the group without a readable paper; a verdict does not cover them."
    )
    run: Run | None = Field(description="The latest run for the model and thinking level.")
    error: CheckError | None


class CheckRequest(BaseModel):
    model: str
    thinking: str


class ItemChoice(BaseModel):
    item_key: str | None = Field(description="Zotero item key; null forgets the choice.")


class PdfChoice(BaseModel):
    attachment_key: str | None = Field(description="Attachment key; null forgets the choice.")


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def create_app(workspace: Workspace, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="loccit",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    # Only pages served from this machine may talk to the API: requests for
    # another host name are refused, and so are requests a page of another
    # site makes from the browser (they could upload files or start checks).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))

    @app.middleware("http")
    async def refuse_foreign_origins(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "foreign origin"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(DocumentNotFound)
    def unknown_document(request: Request, error: DocumentNotFound) -> JSONResponse:
        return JSONResponse({"detail": "unknown document"}, status_code=404)

    @app.exception_handler(GroupNotFound)
    def unknown_group(request: Request, error: GroupNotFound) -> JSONResponse:
        return JSONResponse({"detail": "unknown citation group"}, status_code=404)

    @app.exception_handler(InvalidPdf)
    def invalid_pdf(request: Request, error: InvalidPdf) -> JSONResponse:
        return JSONResponse({"detail": "not a readable PDF"}, status_code=422)

    @app.get("/api/documents")
    def list_documents() -> list[Document]:
        """Stored documents, the most recently opened first."""
        return [_document(document) for document in workspace.store.documents()]

    @app.post("/api/documents")
    def upload_document(
        pdf: Annotated[UploadFile, File(description="PDF of the thesis")],
        bib: Annotated[UploadFile | None, File(description=".bib of the thesis")] = None,
    ) -> Document:
        """Store a thesis. A PDF of the same file name replaces the stored version."""
        document = workspace.import_document(
            pdf.filename or "thesis.pdf", pdf.file.read(), bib.file.read() if bib else None
        )
        return _document(document)

    @app.delete("/api/documents/{document_id}", status_code=204)
    def delete_document(document_id: int) -> None:
        workspace.delete_document(document_id)

    @app.get("/api/documents/{document_id}/pdf", response_class=FileResponse)
    def document_pdf(document_id: int):
        return FileResponse(workspace.thesis_path(document_id), media_type="application/pdf")

    @app.get("/api/documents/{document_id}/citations")
    def document_citations(document_id: int, reload: bool = False) -> DocumentCitations:
        """Citation groups of the document and what each cite key resolves to.
        `reload` reads the Zotero library again."""
        return _citations(workspace.citations(document_id, reload=reload))

    @app.get("/api/papers/{attachment_key}/pdf", response_class=FileResponse)
    def paper_pdf(attachment_key: str):
        """A paper's PDF. Only attachments a citation was resolved to are served."""
        path = workspace.paper_path(attachment_key)
        if path is None or not path.is_file():
            raise HTTPException(404, "unknown paper")
        return FileResponse(path, media_type="application/pdf")

    @app.get("/api/models")
    def list_models() -> Models:
        """The models and thinking levels from ~/.loccit/models.toml."""
        settings = workspace.models
        return Models(
            default_model=settings.default_model,
            models=[Model(id=m.id, label=m.label, levels=list(m.levels)) for m in settings.models],
            has_api_key=model_settings.get_api_key() is not None,
        )

    @app.get("/api/documents/{document_id}/groups/{group_id}/check")
    def stored_check(document_id: int, group_id: int, model: str, thinking: str) -> Check:
        """The latest stored run for a citation group, model and thinking level."""
        return _check(workspace.check(document_id, group_id, model, thinking))

    @app.post("/api/documents/{document_id}/groups/{group_id}/check")
    def run_check(document_id: int, group_id: int, request: CheckRequest) -> Check:
        """Check a citation group now. Waits for the model's complete answer
        and stores it as a new run; earlier runs are kept."""
        return _check(
            workspace.check(document_id, group_id, request.model, request.thinking, run=True)
        )

    @app.put("/api/choices/items/{cite_key}", status_code=204)
    def choose_item(cite_key: str, choice: ItemChoice) -> None:
        """Remember which version of a publication a cite key stands for."""
        workspace.store.set_item_choice(cite_key, choice.item_key)

    @app.put("/api/choices/pdfs/{item_key}", status_code=204)
    def choose_pdf(item_key: str, choice: PdfChoice) -> None:
        """Remember which of an item's PDFs to show."""
        workspace.store.set_pdf_choice(item_key, choice.attachment_key)

    if frontend_dist:
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


def _document(document: StoredDocument) -> Document:
    return Document(
        id=document.id,
        name=document.name,
        has_bib=document.has_bib,
        version=document.pdf_sha256[:16],
        uploaded_at=document.uploaded_at,
        opened_at=document.opened_at,
    )


def _citations(view: CitationsView) -> DocumentCitations:
    return DocumentCitations(
        document=_document(view.document),
        zotero=ZoteroState(status=view.zotero_status, detail=view.zotero_detail),
        groups=[
            CitationGroup(
                id=group.id,
                page=group.page,
                citations=[
                    Citation(key=c.key, rects=c.rects, annotation_ids=c.annotation_ids)
                    for c in group.citations
                ],
            )
            for group in view.scan.groups
        ],
        sources={
            key: _source(resolution, view.library) for key, resolution in view.resolutions.items()
        },
        link_count=view.scan.link_count,
        named_destinations=dict(view.scan.dest_prefixes),
        suspected_ranges=view.scan.suspected_ranges,
    )


def _source(resolution: res.Resolution, library: LibraryIndex) -> Source:
    return Source(
        cite_key=resolution.cite_key,
        status=resolution.status,
        detail=resolution.detail,
        item_key=resolution.item_key,
        chosen_by=resolution.source,
        conflict=resolution.conflict,
        duplicates=list(resolution.duplicates),
        candidates=[_candidate(group, library) for group in resolution.candidates],
        pdfs=[
            PdfFile(attachment_key=pdf.attachment_key, filename=pdf.filename, exists=pdf.exists)
            for pdf in resolution.pdfs
        ],
        attachment_key=(resolution.pdf.attachment_key if resolution.status == res.OK else None),
        title=resolution.title,
        authors=list(resolution.authors),
        year=resolution.year,
        way_a=_way(resolution.way_a),
        way_b=_way(resolution.way_b),
    )


def _way(way: res.WayResult) -> Way:
    return Way(
        status=way.status, how=way.how, item_key=way.item_key, candidates=list(way.candidates)
    )


def _candidate(group: tuple[str, ...], library: LibraryIndex) -> Candidate:
    item = library.items[group[0]]
    return Candidate(
        item_key=item.key,
        item_type=item.item_type,
        title=item.title,
        authors=item.authors,
        date=item.date,
        doi=item.doi,
        url=item.url,
        pdf_count=len(item.pdfs),
        duplicates=list(group),
    )


def _check(check: ws.Check) -> Check:
    return Check(
        context=check.context.text if check.context else None,
        checked_keys=[paper.paper.cite_key for paper in check.papers],
        missing_keys=check.missing,
        run=_run(check.run, check.passages) if check.run else None,
        error=CheckError(code=check.error, detail=check.error_detail) if check.error else None,
    )


def _run(run: StoredRun, passages: list) -> Run:
    return Run(
        id=run.id,
        model=run.model,
        thinking=run.thinking,
        strategy=run.strategy,
        prompt_version=run.prompt_version,
        started_at=run.started_at,
        duration_ms=run.duration_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cache_write_tokens=run.cache_write_tokens,
        cost_usd=run.cost_usd,
        verdict=run.verdict,
        passages=[
            Passage(
                cite_key=found.cite_key,
                attachment_key=attachment_key,
                page=found.page,
                start_sentence=found.start_sentence,
                end_sentence=found.end_sentence,
                rects=[PageRect(page=page, rect=rect) for page, rect in found.rects],
                reason=found.reason,
            )
            for found, attachment_key in passages
        ],
    )
