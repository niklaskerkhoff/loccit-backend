"""Builders for synthetic thesis PDFs and Zotero API responses."""

from pathlib import Path

import pymupdf

FONT_SIZE = 11
LINE_HEIGHT = 16
LEFT = 72
TOP = 100

# A run of text; with a destination name it becomes a link.
Run = str | tuple[str, str]


def make_pdf(path: Path, pages: list[list[list[Run]]]) -> Path:
    """Write a PDF. Each page is a list of lines, each line a list of runs:
    plain strings or (text, named destination) pairs, e.g.
    ["See [", ("1", "cite.a"), ", ", ("2", "cite.b"), "]."].

    All named destinations point to an extra last page, like a bibliography.
    """
    doc = pymupdf.open()
    for _ in range(len(pages) + 1):
        doc.new_page()
    dests: set[str] = set()
    for number, lines in enumerate(pages):
        page = doc[number]
        for line_number, runs in enumerate(lines):
            x = LEFT
            baseline = TOP + line_number * LINE_HEIGHT
            for run in runs:
                text, dest = (run, None) if isinstance(run, str) else run
                width = pymupdf.get_text_length(text, fontsize=FONT_SIZE)
                page.insert_text((x, baseline), text, fontsize=FONT_SIZE)
                if dest:
                    rect = pymupdf.Rect(x, baseline - FONT_SIZE, x + width, baseline + 3)
                    page.insert_link({"kind": pymupdf.LINK_NAMED, "from": rect, "name": dest})
                    dests.add(dest)
                x += width
    target = doc[len(pages)].xref
    # Name trees must be sorted by name.
    names = "".join(f"({dest})[{target} 0 R/XYZ 72 700 0]" for dest in sorted(dests))
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</Dests<</Names[{names}]>>>>")
    doc.save(path)
    doc.close()
    return path


ENTRY_TYPES = {"journalArticle": "article", "conferencePaper": "inproceedings", "preprint": "misc"}


def raw_item(
    key: str,
    title: str,
    *,
    cite_key: str,
    doi: str | None = None,
    url: str | None = None,
    year: str | None = "2025",
    item_type: str = "journalArticle",
    authors: tuple[str, ...] = ("Heltweg",),
    extra: str = "",
    citation_key_field: str | None = None,
    date_added: str = "2024-01-01T00:00:00Z",
    main_attachment: str | None = None,
) -> dict:
    """An item as the local Zotero API returns it for `include=data,bibtex,biblatex`."""
    exported = f"\n@{ENTRY_TYPES[item_type]}{{{cite_key},\n\ttitle = {{{title}}},\n"
    data = {
        "key": key,
        "itemType": item_type,
        "title": title,
        "creators": [
            {"creatorType": "author", "firstName": "A.", "lastName": name} for name in authors
        ],
        "date": year or "",
        "extra": extra,
        "dateAdded": date_added,
    }
    links = {}
    if doi:
        data["DOI"] = doi
        exported += f"\tdoi = {{{doi}}},\n"
    if url:
        data["url"] = url
        exported += f"\turl = {{{url}}},\n"
    if year:
        exported += f"\tyear = {{{year}}},\n"
    if citation_key_field:
        data["citationKey"] = citation_key_field
    if main_attachment:
        links["attachment"] = {
            "href": f"http://localhost:23119/api/users/1/items/{main_attachment}",
            "attachmentType": "application/pdf",
        }
    exported += "}\n"
    return {"key": key, "links": links, "data": data, "bibtex": exported, "biblatex": exported}


def raw_pdf(
    key: str,
    parent: str,
    *,
    date_added: str = "2024-01-01T00:00:00Z",
    content_type: str = "application/pdf",
    link_mode: str = "imported_url",
    deleted: bool = False,
) -> dict:
    data = {
        "key": key,
        "itemType": "attachment",
        "parentItem": parent,
        "title": "Full Text PDF",
        "filename": f"{key}.pdf",
        "contentType": content_type,
        "linkMode": link_mode,
        "dateAdded": date_added,
    }
    if deleted:
        data["deleted"] = 1
    return {"key": key, "data": data, "bibtex": "\n\n"}
