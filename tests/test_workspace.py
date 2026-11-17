import pytest

from loccit import resolve as res
from loccit.workspace import (
    ZOTERO_API_DISABLED,
    ZOTERO_NOT_RUNNING,
    ZOTERO_OK,
    DocumentNotFound,
    InvalidPdf,
    Workspace,
    document_name,
)
from loccit.zotero import ZoteroApiDisabled, ZoteroNotRunning
from tests.helpers import FakeZotero, make_pdf, raw_item, raw_pdf
from tests.test_resolve import BROWN_PREPRINT_BIB, brown


@pytest.fixture
def zotero(tmp_path):
    papers = tmp_path / "zotero"
    papers.mkdir()
    fake = FakeZotero(
        papers,
        [
            raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
            raw_pdf("PDF00001", "ITEM0001"),
            *brown(),
        ],
    )
    fake.add_paper("PDF00001")
    fake.add_paper("RR84UWN7")
    fake.add_paper("MWZL9XFR")
    return fake


@pytest.fixture
def workspace(tmp_path, zotero):
    return Workspace(tmp_path / "home", zotero)


@pytest.fixture
def thesis_pdf(tmp_path):
    path = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                ["A [", ("1", "cite.heltweg_study_2025"), ", ", ("2", "cite.brown_language_2020")],
                ["] and B [", ("3", "cite.nobody_nothing_1999"), "]."],
            ]
        ],
    )
    return path.read_bytes()


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("thesis.pdf", "thesis.pdf"),
        ("Master Thesis (2).PDF", "Master Thesis.pdf"),
        ("C:\\Users\\me\\Downloads\\thesis (1).pdf", "thesis.pdf"),
        ("/tmp/x/proposal.pdf", "proposal.pdf"),
        ("report (draft).pdf", "report (draft).pdf"),
    ],
)
def test_document_name(filename, name):
    assert document_name(filename) == name


def test_import_stores_the_files_under_the_data_folder(workspace, thesis_pdf, tmp_path):
    document = workspace.import_document("thesis.pdf", thesis_pdf, b"@misc{a}")
    folder = tmp_path / "home" / "documents" / str(document.id)
    assert (folder / "thesis.pdf").read_bytes() == thesis_pdf
    assert (folder / "refs.bib").read_bytes() == b"@misc{a}"
    assert workspace.thesis_path(document.id) == folder / "thesis.pdf"
    assert document.has_bib is True
    # Nothing is written anywhere else.
    assert {p.name for p in (tmp_path / "home").iterdir()} == {"loccit.sqlite", "documents"}


def test_a_newer_version_replaces_the_document_and_keeps_its_bib(workspace, thesis_pdf, tmp_path):
    first = workspace.import_document("thesis.pdf", thesis_pdf, b"@misc{a}")
    newer = make_pdf(tmp_path / "v2.pdf", [[["Only [", ("1", "cite.other_key_2024"), "]."]]])
    second = workspace.import_document("thesis (1).pdf", newer.read_bytes())
    assert second.id == first.id
    assert second.pdf_sha256 != first.pdf_sha256
    assert second.has_bib is True
    assert len(workspace.store.documents()) == 1
    assert workspace.citations(second.id).scan.keys == ["other_key_2024"]


def test_import_rejects_what_is_not_a_pdf(workspace):
    with pytest.raises(InvalidPdf):
        workspace.import_document("thesis.pdf", b"this is not a PDF")
    assert workspace.store.documents() == []


def test_delete_document(workspace, thesis_pdf):
    document = workspace.import_document("thesis.pdf", thesis_pdf)
    folder = workspace.thesis_path(document.id).parent
    workspace.delete_document(document.id)
    assert not folder.exists()
    with pytest.raises(DocumentNotFound):
        workspace.citations(document.id)
    with pytest.raises(DocumentNotFound):
        workspace.delete_document(document.id)


def test_citations_resolve_every_key(workspace, thesis_pdf):
    document = workspace.import_document("thesis.pdf", thesis_pdf, BROWN_PREPRINT_BIB.encode())
    view = workspace.citations(document.id)
    assert view.zotero_status == ZOTERO_OK
    assert [group.keys for group in view.scan.groups] == [
        ["heltweg_study_2025", "brown_language_2020"],
        ["nobody_nothing_1999"],
    ]
    assert {key: r.status for key, r in view.resolutions.items()} == {
        "heltweg_study_2025": res.OK,
        "brown_language_2020": res.OK,
        "nobody_nothing_1999": res.NO_ENTRY,
    }
    assert view.resolutions["brown_language_2020"].item_key == "K38PK3JU"


def test_only_resolved_papers_can_be_served(workspace, zotero, thesis_pdf):
    document = workspace.import_document("thesis.pdf", thesis_pdf, BROWN_PREPRINT_BIB.encode())
    assert workspace.paper_path("PDF00001") is None
    workspace.citations(document.id)
    assert workspace.paper_path("PDF00001") == zotero.folder / "PDF00001.pdf"
    assert workspace.paper_path("RR84UWN7") == zotero.folder / "RR84UWN7.pdf"
    # The conference version exists in Zotero, but no citation resolved to it.
    assert workspace.paper_path("MWZL9XFR") is None
    assert workspace.paper_path("../../etc/passwd") is None


def test_a_choice_is_stored_and_reused(workspace, thesis_pdf, tmp_path, zotero):
    document = workspace.import_document("thesis.pdf", thesis_pdf)
    brown_source = workspace.citations(document.id).resolutions["brown_language_2020"]
    assert brown_source.status == res.CHOICE_NEEDED

    workspace.store.set_item_choice("brown_language_2020", "SFTVNYYH")
    chosen = workspace.citations(document.id).resolutions["brown_language_2020"]
    assert (chosen.status, chosen.source, chosen.item_key) == (res.OK, "choice", "SFTVNYYH")

    # A new server process finds the choice in the SQLite file.
    restarted = Workspace(tmp_path / "home", zotero)
    again = restarted.citations(document.id).resolutions["brown_language_2020"]
    assert (again.status, again.item_key) == (res.OK, "SFTVNYYH")


def test_library_is_cached_until_reload(workspace, zotero, thesis_pdf):
    document = workspace.import_document("thesis.pdf", thesis_pdf)
    workspace.citations(document.id)
    workspace.citations(document.id)
    assert zotero.library_loads == 1

    # The missing paper is added to Zotero while loccit is running.
    zotero.raw_items += [
        raw_item("ITEM0009", "Nothing", cite_key="nobody_nothing_1999", year="1999"),
        raw_pdf("PDF00009", "ITEM0009"),
    ]
    zotero.add_paper("PDF00009")
    assert (
        workspace.citations(document.id).resolutions["nobody_nothing_1999"].status == res.NO_ENTRY
    )
    reloaded = workspace.citations(document.id, reload=True)
    assert zotero.library_loads == 2
    assert reloaded.resolutions["nobody_nothing_1999"].status == res.OK


@pytest.mark.parametrize(
    ("error", "status"),
    [(ZoteroNotRunning("refused"), ZOTERO_NOT_RUNNING), (ZoteroApiDisabled(), ZOTERO_API_DISABLED)],
)
def test_citations_are_listed_even_without_zotero(workspace, zotero, thesis_pdf, error, status):
    zotero.error = error
    document = workspace.import_document("thesis.pdf", thesis_pdf)
    view = workspace.citations(document.id)
    assert view.zotero_status == status
    assert len(view.scan.groups) == 2
    assert view.resolutions == {}
    # Once Zotero is back, a reload is enough.
    zotero.error = None
    assert workspace.citations(document.id, reload=True).zotero_status == ZOTERO_OK
