import pytest

from loccit import cli
from loccit.zotero import LibraryIndex, ZoteroApiDisabled, ZoteroNotRunning
from tests.helpers import make_pdf, raw_item, raw_pdf
from tests.test_resolve import bib_entry


class FakeZotero:
    """Stands in for ZoteroClient; papers are files in `folder`."""

    raw_items: list[dict] = []
    folder = None
    error: Exception | None = None

    def load_library(self) -> LibraryIndex:
        if self.error:
            raise self.error
        return LibraryIndex.from_api_items(self.raw_items)

    def attachment_file_url(self, attachment_key: str) -> str | None:
        return (self.folder / f"{attachment_key}.pdf").as_uri()

    def close(self) -> None:
        pass


@pytest.fixture
def zotero(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ZoteroClient", FakeZotero)
    monkeypatch.setattr(cli.paths, "overrides_file", lambda: tmp_path / "overrides.toml")
    monkeypatch.setattr(FakeZotero, "folder", tmp_path)
    monkeypatch.setattr(FakeZotero, "error", None)
    monkeypatch.setattr(
        FakeZotero,
        "raw_items",
        [
            raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
            raw_pdf("PDF00001", "ITEM0001"),
            raw_item("ITEM0002", "No PDF here", cite_key="brown_no_2020", year="2020"),
        ],
    )
    (tmp_path / "PDF00001.pdf").write_bytes(b"%PDF-1.7")
    return FakeZotero


@pytest.fixture
def thesis(tmp_path):
    pdf = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                ["Claim [", ("1", "cite.heltweg_study_2025"), ", ", ("2", "cite.brown_no_2020"), "]."],
                ["Another [", ("3", "cite.missing_key_2019"), "]."],
            ]
        ],
    )
    bib = tmp_path / "refs.bib"
    bib.write_text(
        bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc")
        + bib_entry("brown_no_2020", "No {PDF} here", year="2020")
        + bib_entry("missing_key_2019", "Never added to Zotero", year="2019")
    )
    return pdf, bib


def test_resolve_report(zotero, thesis, tmp_path, capsys):
    pdf, bib = thesis
    assert cli.main(["resolve", str(pdf), "--bib", str(bib)]) == 0
    out = capsys.readouterr().out
    assert "3 Zitate an 2 Stellen, 3 verschiedene Schlüssel" in out
    rows = {line.split()[0]: line.split()[1:] for line in out.splitlines() if line.strip()}
    assert rows["heltweg_study_2025"] == [
        "ITEM0001",
        "(DOI)",
        "ITEM0001",
        "ja",
        str(tmp_path / "PDF00001.pdf"),
    ]
    assert "S. 1: heltweg_study_2025, brown_no_2020" in out
    assert "Quellen ohne PDF: 2 von 3" in out
    assert "brown_no_2020: Eintrag ITEM0002 hat keinen PDF-Anhang – No PDF here" in out
    assert "Auswahl nötig: 0 von 3" in out
    assert "Dubletten in Zotero: 0" in out
    assert "Weg A ordnet 2 von 3 Schlüsseln zu." in out
    assert (
        "missing_key_2019: kein Zotero-Eintrag passt – Never added to Zotero "
        "(Heltweg et al., 2019)" in out
    )
    assert "Weg B allein ordnet 2 von 3 Schlüsseln zu." in out
    assert "Von den 2 zugeordneten bestätigt Weg A denselben Eintrag: 2" in out


def test_resolve_without_bib(zotero, thesis, capsys):
    pdf, _ = thesis
    assert cli.main(["resolve", str(pdf)]) == 0
    out = capsys.readouterr().out
    assert "– (keine .bib)" in out
    assert "Ohne .bib lässt sich das nicht gegen Weg A prüfen." in out


def test_pdf_without_citation_links(zotero, tmp_path, capsys):
    pdf = make_pdf(tmp_path / "plain.pdf", [[["See ", ("2.1", "section.2.1"), "."]]])
    assert cli.main(["resolve", str(pdf)]) == 1
    err = capsys.readouterr().err
    assert "enthält keine Zitat-Links" in err
    assert "section.* (1)" in err


@pytest.mark.parametrize(
    ("error", "message"),
    [(ZoteroNotRunning("refused"), "Zotero starten"), (ZoteroApiDisabled(), "Einstellungen")],
)
def test_zotero_problems_are_explained(zotero, thesis, capsys, error, message):
    zotero.error = error
    pdf, bib = thesis
    assert cli.main(["resolve", str(pdf), "--bib", str(bib)]) == 2
    assert message in capsys.readouterr().err


def test_missing_input_file(zotero, tmp_path, capsys):
    assert cli.main(["resolve", str(tmp_path / "nope.pdf")]) == 2
    assert "Datei nicht gefunden" in capsys.readouterr().err
