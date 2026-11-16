import pytest

from loccit import resolve as res
from loccit.bibfile import parse_bib
from loccit.zotero import KEY_FROM_EXTRA, KEY_FROM_FIELD, LibraryIndex
from tests.helpers import raw_item, raw_pdf


def library(*raw) -> LibraryIndex:
    return LibraryIndex.from_api_items(list(raw))


def bib_entry(key: str, title: str, *, doi: str | None = None, year: str | None = "2025") -> str:
    fields = [f"title = {{{title}}}", "author = {Heltweg, Philip and Riehle, Dirk}"]
    if doi:
        fields.append(f"doi = {{{doi}}}")
    if year:
        fields.append(f"year = {{{year}}}")
    return f"@article{{{key},\n  " + ",\n  ".join(fields) + "\n}\n"


@pytest.fixture
def stored_pdf(tmp_path):
    """A locator that reports every attachment as a file in tmp_path, and
    a function that actually creates such a file."""

    def locate(attachment_key: str) -> str:
        return (tmp_path / f"{attachment_key} paper.pdf").as_uri()

    def create(attachment_key: str):
        path = tmp_path / f"{attachment_key} paper.pdf"
        path.write_bytes(b"%PDF-1.7")
        return path

    return locate, create


def resolve(key, *, bib=None, lib, overrides=None, locate=lambda key: None):
    return res.resolve(
        key,
        bib=parse_bib(bib) if bib is not None else None,
        library=lib,
        overrides=overrides or {},
        locate_file=locate,
    )


# --- way A: via the .bib ---------------------------------------------------


def test_way_a_matches_by_doi():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="zotero_key_1", doi="10.1000/ABC"),
        raw_item("ITEM0002", "A Study", cite_key="zotero_key_2", doi="10.1000/other"),
    )
    bib = parse_bib(bib_entry("my_key", "Some other title", doi="https://doi.org/10.1000/abc"))
    assert res.resolve_way_a("my_key", bib, lib) == res.WayResult(
        res.MATCH, item_key="ITEM0001", how="doi"
    )


def test_way_a_falls_back_to_title_and_year():
    lib = library(raw_item("ITEM0001", "Collaborative editing of EMF/Ecore", cite_key="k"))
    bib = parse_bib(bib_entry("my_key", "Collaborative editing of {EMF}/{Ecore}"))
    assert res.resolve_way_a("my_key", bib, lib) == res.WayResult(
        res.MATCH, item_key="ITEM0001", how="title+year"
    )


def test_way_a_uses_title_when_the_doi_is_unknown_to_zotero():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="k"))
    bib = parse_bib(bib_entry("my_key", "A Study", doi="10.1000/abc"))
    assert res.resolve_way_a("my_key", bib, lib).item_key == "ITEM0001"


def test_way_a_requires_the_same_year():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="k", year="2024"))
    bib = parse_bib(bib_entry("my_key", "A Study", year="2025"))
    assert res.resolve_way_a("my_key", bib, lib) == res.WayResult(res.NONE, how="no_hit")


def test_way_a_does_not_match_a_similar_title():
    lib = library(raw_item("ITEM0001", "A Study of DSLs", cite_key="k"))
    bib = parse_bib(bib_entry("my_key", "A Study of DSL"))
    assert res.resolve_way_a("my_key", bib, lib).status == res.NONE


def test_way_a_rejects_same_title_with_another_doi():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="k", doi="10.1000/preprint"))
    bib = parse_bib(bib_entry("my_key", "A Study", doi="10.1000/journal"))
    assert res.resolve_way_a("my_key", bib, lib).status == res.NONE


def test_way_a_is_ambiguous_when_two_items_share_the_doi():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="k", doi="10.1000/abc"),
        raw_item("ITEM0002", "A Study", cite_key="k", doi="10.1000/ABC"),
    )
    bib = parse_bib(bib_entry("my_key", "A Study", doi="10.1000/abc"))
    assert res.resolve_way_a("my_key", bib, lib) == res.WayResult(
        res.AMBIGUOUS, how="doi", candidates=("ITEM0001", "ITEM0002")
    )


def test_way_a_is_ambiguous_when_two_items_share_title_and_year():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="k1"),
        raw_item("ITEM0002", "A study.", cite_key="k2"),
    )
    bib = parse_bib(bib_entry("my_key", "A Study"))
    assert res.resolve_way_a("my_key", bib, lib) == res.WayResult(
        res.AMBIGUOUS, how="title+year", candidates=("ITEM0001", "ITEM0002")
    )


def test_way_a_without_bib_entry():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="k"))
    assert res.resolve_way_a("k", None, lib) == res.WayResult(res.SKIPPED, how="no_bib")
    assert res.resolve_way_a("k", parse_bib(""), lib) == res.WayResult(res.NONE, how="not_in_bib")
    twice = parse_bib(bib_entry("k", "A Study") + bib_entry("k", "A Study"))
    assert res.resolve_way_a("k", twice, lib) == res.WayResult(res.NONE, how="duplicate_in_bib")


# --- way B: via the keys Zotero produces -----------------------------------


def test_way_b_matches_the_export_key():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    assert res.resolve_way_b("heltweg_study_2025", lib) == res.WayResult(
        res.MATCH, item_key="ITEM0001", how="export"
    )
    assert res.resolve_way_b("heltweg_study_2024", lib) == res.WayResult(
        res.NONE, how="unknown_key"
    )


def test_way_b_is_ambiguous_when_two_items_produce_the_same_key():
    lib = library(
        raw_item("ITEM0001", "Attention Is All You Need", cite_key="vaswani_attention_2023"),
        raw_item("ITEM0002", "Attention Is All You Need", cite_key="vaswani_attention_2023"),
    )
    result = res.resolve_way_b("vaswani_attention_2023", lib)
    assert result.status == res.AMBIGUOUS
    assert result.candidates == ("ITEM0001", "ITEM0002")
    # The suffixed key of a whole-library export depends on the export order.
    assert res.resolve_way_b("vaswani_attention_2023-1", lib).status == res.NONE


def test_way_b_matches_citation_key_field_and_extra_line():
    lib = library(
        raw_item("ITEM0001", "One", cite_key="one_2025", citation_key_field="Pinned2025"),
        raw_item("ITEM0002", "Two", cite_key="two_2025", extra="Citation Key: Extra2025"),
    )
    assert res.resolve_way_b("Pinned2025", lib) == res.WayResult(
        res.MATCH, item_key="ITEM0001", how=KEY_FROM_FIELD
    )
    assert res.resolve_way_b("Extra2025", lib) == res.WayResult(
        res.MATCH, item_key="ITEM0002", how=KEY_FROM_EXTRA
    )
    # No partial or case-insensitive matches.
    assert res.resolve_way_b("pinned2025", lib).status == res.NONE


# --- precedence ------------------------------------------------------------


def test_ways_agree(stored_pdf):
    locate, create = stored_pdf
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_pdf("PDF00001", "ITEM0001"),
    )
    path = create("PDF00001")
    result = resolve(
        "heltweg_study_2025",
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        lib=lib,
        locate=locate,
    )
    assert (result.status, result.source, result.item_key) == (res.OK, "a", "ITEM0001")
    assert result.ways_agree is True
    assert result.conflict is False
    assert result.pdf_path == path
    assert result.attachment_key == "PDF00001"
    assert (result.title, result.authors, result.year) == ("A Study", ("Heltweg",), "2025")


def test_way_a_wins_over_way_b_and_the_conflict_is_flagged():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="other_key", doi="10.1000/abc"),
        raw_item("ITEM0002", "Something else", cite_key="heltweg_study_2025"),
    )
    result = resolve(
        "heltweg_study_2025",
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        lib=lib,
    )
    assert (result.source, result.item_key) == ("a", "ITEM0001")
    assert result.way_b.item_key == "ITEM0002"
    assert result.ways_agree is False
    assert result.conflict is True


def test_way_b_is_used_when_way_a_finds_nothing():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = resolve(
        "heltweg_study_2025", bib=bib_entry("heltweg_study_2025", "Renamed since"), lib=lib
    )
    assert (result.source, result.item_key) == ("b", "ITEM0001")
    assert result.ways_agree is None
    assert result.conflict is False


def test_without_bib_only_way_b_runs():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = resolve("heltweg_study_2025", bib=None, lib=lib)
    assert result.way_a.status == res.SKIPPED
    assert (result.source, result.item_key) == ("b", "ITEM0001")


def test_way_b_outside_the_ambiguous_candidates_of_way_a_is_a_conflict():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="dup", doi="10.1000/abc"),
        raw_item("ITEM0002", "A Study", cite_key="dup", doi="10.1000/abc"),
        raw_item("ITEM0003", "Something else", cite_key="heltweg_study_2025"),
    )
    result = resolve(
        "heltweg_study_2025",
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        lib=lib,
    )
    assert (result.source, result.item_key) == ("b", "ITEM0003")
    assert result.conflict is True


def test_override_wins_over_both_ways(stored_pdf):
    locate, create = stored_pdf
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_item("ITEM0009", "The right one", cite_key="unrelated"),
        raw_pdf("PDF00009", "ITEM0009"),
    )
    create("PDF00009")
    result = resolve(
        "heltweg_study_2025",
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        lib=lib,
        overrides={"heltweg_study_2025": "ITEM0009"},
        locate=locate,
    )
    assert (result.status, result.source, result.item_key) == (res.OK, "override", "ITEM0009")
    assert result.title == "The right one"


def test_override_to_unknown_item_is_not_resolved():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = resolve(
        "heltweg_study_2025", bib=None, lib=lib, overrides={"heltweg_study_2025": "NOSUCHIT"}
    )
    assert (result.status, result.detail) == (res.NO_ENTRY, "unknown_override")


def test_load_overrides(tmp_path):
    assert res.load_overrides(tmp_path / "overrides.toml") == {}
    file = tmp_path / "overrides.toml"
    file.write_text('[overrides]\nheltweg_study_2025 = "ITEM0009"\n"key:with.dots" = "ITEM0010"\n')
    assert res.load_overrides(file) == {
        "heltweg_study_2025": "ITEM0009",
        "key:with.dots": "ITEM0010",
    }


# --- the three reasons for a missing paper ---------------------------------


def test_reason_1_no_item_matches():
    lib = library(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = resolve("unknown_key_2020", bib=bib_entry("unknown_key_2020", "Not in Zotero"), lib=lib)
    assert (result.status, result.detail) == (res.NO_ENTRY, "no_match")
    assert result.item_key is None
    # What is known comes from the .bib, so the PDF can be added to Zotero.
    assert (result.title, result.year) == ("Not in Zotero", "2025")
    assert result.authors == ("Heltweg, Philip", "Riehle, Dirk")


def test_reason_1_several_items_match():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_item("ITEM0002", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_pdf("PDF00001", "ITEM0001"),
    )
    result = resolve(
        "heltweg_study_2025",
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        lib=lib,
    )
    assert (result.status, result.detail) == (res.NO_ENTRY, "ambiguous")
    assert result.item_key is None
    assert result.pdf_path is None


def test_reason_2_item_without_pdf():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("SNAPSHOT", "ITEM0001", content_type="text/html"),
    )
    result = resolve("heltweg_study_2025", lib=lib)
    assert result.status == res.NO_PDF
    assert result.item_key == "ITEM0001"
    assert result.title == "A Study"


def test_reason_3_zotero_reports_no_file():
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("PDF00001", "ITEM0001"),
    )
    result = resolve("heltweg_study_2025", lib=lib, locate=lambda key: None)
    assert result.status == res.FILE_MISSING
    assert (result.attachment_key, result.pdf_path) == ("PDF00001", None)


def test_reason_3_file_is_not_on_disk(stored_pdf):
    locate, _ = stored_pdf
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("PDF00001", "ITEM0001"),
    )
    result = resolve("heltweg_study_2025", lib=lib, locate=locate)
    assert result.status == res.FILE_MISSING
    assert result.pdf_path.name == "PDF00001 paper.pdf"


def test_first_added_pdf_is_used_and_the_others_are_counted(stored_pdf):
    locate, create = stored_pdf
    lib = library(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("PDFLATER", "ITEM0001", date_added="2025-05-01T10:00:00Z"),
        raw_pdf("PDFFIRST", "ITEM0001", date_added="2024-08-12T00:00:08Z"),
    )
    create("PDFFIRST")
    create("PDFLATER")
    result = resolve("heltweg_study_2025", lib=lib, locate=locate)
    assert result.status == res.OK
    assert result.attachment_key == "PDFFIRST"
    assert result.other_pdf_count == 1
