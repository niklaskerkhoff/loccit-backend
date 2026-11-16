"""The HTTP API the web frontend talks to. Schema: `/api/openapi.json`."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from loccit import resolve as res
from loccit.store import Document as StoredDocument
from loccit.workspace import CitationsView, DocumentNotFound, InvalidPdf, Workspace
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


class ItemChoice(BaseModel):
    item_key: str | None = Field(description="Zotero item key; null forgets the choice.")


class PdfChoice(BaseModel):
    attachment_key: str | None = Field(description="Attachment key; null forgets the choice.")


def create_app(workspace: Workspace, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="loccit",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    # Only pages served from this machine may talk to the API.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(DocumentNotFound)
    def unknown_document(request: Request, error: DocumentNotFound) -> JSONResponse:
        return JSONResponse({"detail": "unknown document"}, status_code=404)

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
        attachment_key=(
            resolution.pdf.attachment_key if resolution.status == res.OK else None
        ),
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
