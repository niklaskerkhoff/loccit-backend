import pytest

from loccit import resolve as res
from loccit.bibfile import parse_bib
from loccit.zotero import KEY_FROM_EXTRA, KEY_FROM_FIELD, LibraryIndex
from tests.helpers import raw_item, raw_pdf


def bib_entry(
    key: str,
    title: str,
    *,
    doi: str | None = None,
    url: str | None = None,
    year: str | None = "2025",
    entry_type: str = "article",
) -> str:
    fields = [f"title = {{{title}}}", "author = {Heltweg, Philip and Riehle, Dirk}"]
    if doi:
        fields.append(f"doi = {{{doi}}}")
    if url:
        fields.append(f"url = {{{url}}}")
    if year:
        fields.append(f"year = {{{year}}}")
    return f"@{entry_type}{{{key},\n  " + ",\n  ".join(fields) + "\n}\n"


@pytest.fixture
def resolver(tmp_path):
    """Builds a Resolver over raw Zotero items. Zotero reports every
    attachment as a file in tmp_path; only those in `on_disk` exist."""

    def build(*raw, bib=None, on_disk=(), **options) -> res.Resolver:
        for attachment_key in on_disk:
            (tmp_path / f"{attachment_key} paper.pdf").write_bytes(b"%PDF-1.7")
        return res.Resolver(
            LibraryIndex.from_api_items(list(raw)),
            lambda attachment_key: (tmp_path / f"{attachment_key} paper.pdf").as_uri(),
            bib=parse_bib(bib) if bib is not None else None,
            **options,
        )

    return build


# --- way A: via the .bib ---------------------------------------------------


def test_way_a_matches_by_doi(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="zotero_key_1", doi="10.1000/ABC"),
        raw_item("ITEM0002", "A Study", cite_key="zotero_key_2", doi="10.1000/other"),
        bib=bib_entry("my_key", "Some other title", doi="https://doi.org/10.1000/abc"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "ITEM0001", "doi")


def test_way_a_matches_by_url(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k1", url="https://example.org/a"),
        raw_item("ITEM0002", "Renamed in Zotero", cite_key="k2", url="https://example.org/b"),
        bib=bib_entry("my_key", "Old title", url="https://example.org/b"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "ITEM0002", "url")


def test_way_a_falls_back_to_title_and_year(resolver):
    r = resolver(
        raw_item("ITEM0001", "Collaborative editing of EMF/Ecore", cite_key="k"),
        bib=bib_entry("my_key", "Collaborative editing of {EMF}/{Ecore}"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "ITEM0001", "title+year")


def test_way_a_uses_title_when_the_doi_is_unknown_to_zotero(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k"),
        bib=bib_entry("my_key", "A Study", doi="10.1000/abc"),
    )
    assert r.way_a("my_key").item_key == "ITEM0001"


def test_way_a_requires_the_same_year(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k", year="2024"),
        bib=bib_entry("my_key", "A Study", year="2025"),
    )
    assert r.way_a("my_key") == res.WayResult(res.NONE, how="no_hit")


def test_way_a_does_not_match_a_similar_title(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study of DSLs", cite_key="k"),
        bib=bib_entry("my_key", "A Study of DSL"),
    )
    assert r.way_a("my_key").status == res.NONE


@pytest.mark.parametrize(
    ("item", "entry", "field"),
    [
        ({"doi": "10.1000/preprint"}, {"doi": "10.1000/journal"}, "doi"),
        ({"url": "https://example.org/a"}, {"url": "https://example.org/b"}, "url"),
        ({"item_type": "conferencePaper"}, {"entry_type": "misc"}, "type"),
    ],
)
def test_way_a_drops_candidates_that_contradict_the_bib(resolver, item, entry, field):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k", **item),
        bib=bib_entry("my_key", "A Study", **entry),
    )
    assert r.way_a("my_key") == res.WayResult(
        res.NONE, how="contradicts_bib", rejected=(("ITEM0001", field),)
    )


def test_an_equal_doi_outweighs_another_type_or_url(resolver):
    r = resolver(
        raw_item(
            "ITEM0001",
            "A Study",
            cite_key="k",
            doi="10.48550/ARXIV.2503.04556",
            url="https://arxiv.org/abs/2503.04556",
            item_type="journalArticle",
        ),
        bib=bib_entry(
            "my_key",
            "A Study",
            doi="10.48550/arXiv.2503.04556",
            url="http://arxiv.org/abs/2503.04556",
            entry_type="misc",
        ),
    )
    assert r.way_a("my_key").item_key == "ITEM0001"


def test_way_a_lets_the_bib_decide_between_versions(resolver):
    """Same title and year, told apart by the entry type."""
    r = resolver(
        raw_item("PREPRINT", "A Study", cite_key="k", item_type="preprint"),
        raw_item("PAPER001", "A Study", cite_key="k", item_type="conferencePaper"),
        bib=bib_entry("my_key", "A Study", entry_type="inproceedings"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.item_key) == (res.MATCH, "PAPER001")
    assert result.rejected == (("PREPRINT", "type"),)


def test_way_a_needs_a_choice_when_the_bib_cannot_decide(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k1", url="https://example.org/a"),
        raw_item("ITEM0002", "A study.", cite_key="k2", url="https://example.org/b"),
        bib=bib_entry("my_key", "A Study"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.how) == (res.CHOICE, "title+year")
    assert result.candidates == ("ITEM0001", "ITEM0002")
    assert result.item_key is None


def test_a_later_step_narrows_an_earlier_choice(resolver):
    r = resolver(
        raw_item("ITEM0001", "Part one", cite_key="k1", url="https://example.org/book"),
        raw_item("ITEM0002", "Part two", cite_key="k2", url="https://example.org/book"),
        bib=bib_entry("my_key", "Part two", url="https://example.org/book"),
    )
    result = r.way_a("my_key")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "ITEM0002", "title+year")


def test_way_a_without_bib_entry(resolver):
    item = raw_item("ITEM0001", "A Study", cite_key="k")
    assert resolver(item).way_a("k") == res.WayResult(res.SKIPPED, how="no_bib")
    assert resolver(item, bib="").way_a("k") == res.WayResult(res.NONE, how="not_in_bib")
    twice = bib_entry("k", "A Study") + bib_entry("k", "A Study")
    assert resolver(item, bib=twice).way_a("k") == res.WayResult(res.NONE, how="duplicate_in_bib")


# --- duplicates ------------------------------------------------------------


def vaswani(**changes):
    """Two indistinguishable entries, as in the user's library."""
    common = dict(
        cite_key="vaswani_attention_2023",
        doi="10.48550/arXiv.1706.03762",
        url="http://arxiv.org/abs/1706.03762",
        year="2023",
        item_type="preprint",
        authors=("Vaswani",),
    )
    common.update(changes)
    return [
        raw_item(
            "HA8YP9LX", "Attention Is All You Need", date_added="2024-11-28T00:18:36Z", **common
        ),
        raw_pdf("PI3XJBD5", "HA8YP9LX"),
        raw_item(
            "YMPI75PT",
            "Attention Is All You Need",
            date_added="2026-11-16T12:22:47Z",
            **common,
        ),
        raw_pdf("THDMU6JK", "YMPI75PT"),
    ]


VASWANI_BIB = bib_entry(
    "vaswani_attention_2023",
    "Attention {Is} {All} {You} {Need}",
    doi="10.48550/arXiv.1706.03762",
    url="http://arxiv.org/abs/1706.03762",
    year="2023",
    entry_type="misc",
)


def test_vaswani_duplicates_resolve_to_the_one_added_last(resolver):
    r = resolver(*vaswani(), bib=VASWANI_BIB, on_disk=("PI3XJBD5", "THDMU6JK"))
    result = r.resolve("vaswani_attention_2023")
    assert (result.status, result.source, result.item_key) == (res.OK, "a", "YMPI75PT")
    assert result.duplicates == ("YMPI75PT", "HA8YP9LX")
    assert result.pdf.attachment_key == "THDMU6JK"
    assert result.candidates == ()
    # Way B resolves the key by the same rule and agrees.
    assert (result.way_b.status, result.way_b.item_key) == (res.MATCH, "YMPI75PT")
    assert result.ways_agree is True
    assert result.conflict is False


def test_a_duplicate_with_pdf_on_disk_is_preferred_to_a_newer_one(resolver):
    r = resolver(*vaswani(), bib=VASWANI_BIB, on_disk=("PI3XJBD5",))
    result = r.resolve("vaswani_attention_2023")
    assert (result.status, result.item_key) == (res.OK, "HA8YP9LX")
    assert result.duplicates == ("HA8YP9LX", "YMPI75PT")


def test_vaswani_without_bib_is_resolved_by_way_b(resolver):
    r = resolver(*vaswani(), on_disk=("PI3XJBD5", "THDMU6JK"))
    result = r.resolve("vaswani_attention_2023")
    assert (result.status, result.source, result.item_key) == (res.OK, "b", "YMPI75PT")
    assert result.duplicates == ("YMPI75PT", "HA8YP9LX")


def test_suffixed_key_resolves_when_all_items_of_the_base_key_are_duplicates(resolver):
    r = resolver(*vaswani(), on_disk=("PI3XJBD5", "THDMU6JK"))
    result = r.way_b("vaswani_attention_2023-1")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "YMPI75PT", "suffix")
    assert r.way_b("vaswani_attention_2023-x").status == res.NONE
    assert r.way_b("vaswani_attention_2022-1").status == res.NONE


def test_items_without_doi_are_duplicates_only_if_title_year_type_and_url_agree(resolver):
    def pair(**second):
        common = dict(cite_key="k", url="https://example.org/a", item_type="conferencePaper")
        return resolver(
            raw_item("ITEM0001", "A Study", **common),
            raw_item("ITEM0002", "A study.", **{**common, **second}),
        )

    assert pair().way_b("k").status == res.MATCH
    assert pair(url="https://example.org/b").way_b("k").status == res.CHOICE
    assert pair(year="2024").way_b("k").status == res.CHOICE
    assert pair(item_type="journalArticle").way_b("k").status == res.CHOICE
    assert pair(doi="10.1000/abc").way_b("k").status == res.CHOICE


# --- different versions ----------------------------------------------------


def brown():
    """Preprint with DOI and conference version without, as in the user's library."""
    return [
        raw_item(
            "K38PK3JU",
            "Language Models are Few-Shot Learners",
            cite_key="brown_language_2020",
            doi="10.48550/arXiv.2005.14165",
            url="http://arxiv.org/abs/2005.14165",
            year="2020",
            item_type="preprint",
            authors=("Brown",),
            date_added="2026-11-16T12:23:09Z",
        ),
        raw_pdf("RR84UWN7", "K38PK3JU"),
        raw_item(
            "SFTVNYYH",
            "Language Models are Few-Shot Learners",
            cite_key="brown_language_2020",
            url="https://proceedings.neurips.cc/paper/2020/hash/1457",
            year="2020",
            item_type="conferencePaper",
            authors=("Brown",),
            date_added="2024-08-11T22:50:38Z",
        ),
        raw_pdf("MWZL9XFR", "SFTVNYYH"),
    ]


BROWN_PREPRINT_BIB = bib_entry(
    "brown_language_2020",
    "Language {Models} are {Few}-{Shot} {Learners}",
    doi="10.48550/arXiv.2005.14165",
    url="http://arxiv.org/abs/2005.14165",
    year="2020",
    entry_type="misc",
)


def test_brown_the_bib_picks_the_preprint(resolver):
    r = resolver(*brown(), bib=BROWN_PREPRINT_BIB, on_disk=("RR84UWN7", "MWZL9XFR"))
    result = r.resolve("brown_language_2020")
    assert (result.status, result.source, result.item_key) == (res.OK, "a", "K38PK3JU")
    assert result.pdf.attachment_key == "RR84UWN7"
    assert result.duplicates == ()
    # Way B alone cannot tell the versions apart; that is no conflict.
    assert result.way_b.status == res.CHOICE
    assert result.way_b.candidates == ("K38PK3JU", "SFTVNYYH")
    assert result.ways_agree is None
    assert result.conflict is False


def test_brown_the_bib_picks_the_conference_version(resolver):
    bib = bib_entry(
        "brown_language_2020",
        "Language {Models} are {Few}-{Shot} {Learners}",
        url="https://proceedings.neurips.cc/paper/2020/hash/1457",
        year="2020",
        entry_type="inproceedings",
    )
    r = resolver(*brown(), bib=bib, on_disk=("RR84UWN7", "MWZL9XFR"))
    result = r.resolve("brown_language_2020")
    assert (result.status, result.item_key, result.way_a.how) == (res.OK, "SFTVNYYH", "url")


def test_brown_without_bib_needs_a_choice(resolver):
    r = resolver(*brown(), on_disk=("RR84UWN7", "MWZL9XFR"))
    result = r.resolve("brown_language_2020")
    assert result.status == res.CHOICE_NEEDED
    assert result.candidates == (("K38PK3JU",), ("SFTVNYYH",))
    assert result.item_key is None
    assert result.pdf is None
    # The suffixed key of a whole-library export is no better.
    assert r.resolve("brown_language_2020-1").status == res.CHOICE_NEEDED


def test_a_stored_choice_is_reused(resolver):
    r = resolver(
        *brown(),
        on_disk=("RR84UWN7", "MWZL9XFR"),
        item_choices={"brown_language_2020": "SFTVNYYH"},
    )
    result = r.resolve("brown_language_2020")
    assert (result.status, result.source, result.item_key) == (res.OK, "choice", "SFTVNYYH")
    # The candidates stay, so the choice can be changed.
    assert result.candidates == (("K38PK3JU",), ("SFTVNYYH",))


def test_a_stored_choice_outside_the_candidates_is_ignored(resolver):
    r = resolver(*brown(), item_choices={"brown_language_2020": "GONE0001"})
    assert r.resolve("brown_language_2020").status == res.CHOICE_NEEDED


def test_choice_between_a_duplicate_pair_and_another_version(resolver):
    r = resolver(*brown(), *vaswani(cite_key="brown_language_2020"), on_disk=("THDMU6JK",))
    result = r.resolve("brown_language_2020")
    assert result.status == res.CHOICE_NEEDED
    assert result.candidates == (("K38PK3JU",), ("SFTVNYYH",), ("YMPI75PT", "HA8YP9LX"))
    r.item_choices["brown_language_2020"] = "HA8YP9LX"
    chosen = r.resolve("brown_language_2020")
    # Choosing any of the duplicates selects the pair; the pick stays by rule.
    assert (chosen.status, chosen.item_key) == (res.OK, "YMPI75PT")
    assert chosen.duplicates == ("YMPI75PT", "HA8YP9LX")


# --- way B: via the keys Zotero produces -----------------------------------


def test_way_b_matches_the_export_key(resolver):
    r = resolver(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = r.way_b("heltweg_study_2025")
    assert (result.status, result.item_key, result.how) == (res.MATCH, "ITEM0001", "export")
    assert r.way_b("heltweg_study_2024") == res.WayResult(res.NONE, how="unknown_key")


def test_way_b_matches_citation_key_field_and_extra_line(resolver):
    r = resolver(
        raw_item("ITEM0001", "One", cite_key="one_2025", citation_key_field="Pinned2025"),
        raw_item("ITEM0002", "Two", cite_key="two_2025", extra="Citation Key: Extra2025"),
    )
    assert (r.way_b("Pinned2025").item_key, r.way_b("Pinned2025").how) == (
        "ITEM0001",
        KEY_FROM_FIELD,
    )
    assert (r.way_b("Extra2025").item_key, r.way_b("Extra2025").how) == ("ITEM0002", KEY_FROM_EXTRA)
    # No partial or case-insensitive matches.
    assert r.way_b("pinned2025").status == res.NONE


# --- precedence ------------------------------------------------------------


def test_ways_agree(resolver, tmp_path):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_pdf("PDF00001", "ITEM0001"),
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        on_disk=("PDF00001",),
    )
    result = r.resolve("heltweg_study_2025")
    assert (result.status, result.source, result.item_key) == (res.OK, "a", "ITEM0001")
    assert result.ways_agree is True
    assert result.conflict is False
    assert result.pdf_path == tmp_path / "PDF00001 paper.pdf"
    assert (result.title, result.authors, result.year) == ("A Study", ("Heltweg",), "2025")


def test_way_a_wins_over_way_b_and_the_conflict_is_flagged(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="other_key", doi="10.1000/abc"),
        raw_item("ITEM0002", "Something else", cite_key="heltweg_study_2025"),
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
    )
    result = r.resolve("heltweg_study_2025")
    assert (result.source, result.item_key) == ("a", "ITEM0001")
    assert result.way_b.item_key == "ITEM0002"
    assert result.ways_agree is False
    assert result.conflict is True


def test_way_b_is_used_when_way_a_finds_nothing(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        bib=bib_entry("heltweg_study_2025", "Renamed since"),
    )
    result = r.resolve("heltweg_study_2025")
    assert (result.source, result.item_key) == ("b", "ITEM0001")
    assert result.ways_agree is None
    assert result.conflict is False


def test_without_bib_only_way_b_runs(resolver):
    r = resolver(raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"))
    result = r.resolve("heltweg_study_2025")
    assert result.way_a.status == res.SKIPPED
    assert (result.source, result.item_key) == ("b", "ITEM0001")


def test_way_b_decides_a_choice_of_way_a_if_it_names_one_of_the_candidates(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", url="https://a.org"),
        raw_item("ITEM0002", "A Study", cite_key="heltweg_study_2025a", url="https://b.org"),
        bib=bib_entry("heltweg_study_2025", "A Study"),
    )
    result = r.resolve("heltweg_study_2025")
    assert result.way_a.status == res.CHOICE
    assert (result.source, result.item_key, result.conflict) == ("b", "ITEM0001", False)


def test_way_b_outside_the_candidates_of_way_a_joins_the_choice(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="k1", url="https://a.org"),
        raw_item("ITEM0002", "A Study", cite_key="k2", url="https://b.org"),
        raw_item("ITEM0003", "Something else", cite_key="heltweg_study_2025"),
        bib=bib_entry("heltweg_study_2025", "A Study"),
    )
    result = r.resolve("heltweg_study_2025")
    assert result.status == res.CHOICE_NEEDED
    assert result.candidates == (("ITEM0001",), ("ITEM0002",), ("ITEM0003",))
    assert result.conflict is True


def test_override_wins_over_both_ways(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
        raw_item("ITEM0009", "The right one", cite_key="unrelated"),
        raw_pdf("PDF00009", "ITEM0009"),
        bib=bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc"),
        overrides={"heltweg_study_2025": "ITEM0009"},
        on_disk=("PDF00009",),
    )
    result = r.resolve("heltweg_study_2025")
    assert (result.status, result.source, result.item_key) == (res.OK, "override", "ITEM0009")
    assert result.title == "The right one"


def test_override_to_unknown_item_is_not_resolved(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        overrides={"heltweg_study_2025": "NOSUCHIT"},
    )
    result = r.resolve("heltweg_study_2025")
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


def test_reason_1_no_item_matches(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        bib=bib_entry("unknown_key_2020", "Not in Zotero"),
    )
    result = r.resolve("unknown_key_2020")
    assert (result.status, result.detail) == (res.NO_ENTRY, "no_match")
    assert result.item_key is None
    # What is known comes from the .bib, so the PDF can be added to Zotero.
    assert (result.title, result.year) == ("Not in Zotero", "2025")
    assert result.authors == ("Heltweg, Philip", "Riehle, Dirk")


def test_reason_2_item_without_pdf(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("SNAPSHOT", "ITEM0001", content_type="text/html"),
    )
    result = r.resolve("heltweg_study_2025")
    assert result.status == res.NO_PDF
    assert result.item_key == "ITEM0001"
    assert result.title == "A Study"


def test_reason_3_zotero_reports_no_file():
    library = LibraryIndex.from_api_items(
        [
            raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
            raw_pdf("PDF00001", "ITEM0001"),
        ]
    )
    result = res.Resolver(library, lambda key: None).resolve("heltweg_study_2025")
    assert result.status == res.FILE_MISSING
    assert (result.pdf.attachment_key, result.pdf_path) == ("PDF00001", None)


def test_reason_3_file_is_not_on_disk(resolver):
    r = resolver(
        raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025"),
        raw_pdf("PDF00001", "ITEM0001"),
    )
    result = r.resolve("heltweg_study_2025")
    assert result.status == res.FILE_MISSING
    assert result.pdf_path.name == "PDF00001 paper.pdf"


# --- several PDFs ----------------------------------------------------------


def several_pdfs():
    return [
        raw_item(
            "ITEM0001", "A Study", cite_key="heltweg_study_2025", main_attachment="PDFMAIN1"
        ),
        raw_pdf("PDFFIRST", "ITEM0001", date_added="2024-08-12T00:00:08Z"),
        raw_pdf("PDFMAIN1", "ITEM0001", date_added="2025-05-01T10:00:00Z"),
        raw_pdf("PDFLAST1", "ITEM0001", date_added="2026-01-01T10:00:00Z"),
    ]


def test_zoteros_main_attachment_is_used(resolver):
    r = resolver(*several_pdfs(), on_disk=("PDFFIRST", "PDFMAIN1", "PDFLAST1"))
    result = r.resolve("heltweg_study_2025")
    assert result.status == res.OK
    assert result.pdf.attachment_key == "PDFMAIN1"
    assert [pdf.attachment_key for pdf in result.pdfs] == ["PDFMAIN1", "PDFFIRST", "PDFLAST1"]


def test_next_pdf_on_disk_is_used_when_the_main_file_is_missing(resolver):
    r = resolver(*several_pdfs(), on_disk=("PDFLAST1",))
    result = r.resolve("heltweg_study_2025")
    assert result.status == res.OK
    assert result.pdf.attachment_key == "PDFLAST1"
    assert [pdf.exists for pdf in result.pdfs] == [False, False, True]


def test_the_users_pdf_choice_is_used(resolver):
    r = resolver(
        *several_pdfs(),
        on_disk=("PDFFIRST", "PDFMAIN1"),
        pdf_choices={"ITEM0001": "PDFFIRST"},
    )
    assert r.resolve("heltweg_study_2025").pdf.attachment_key == "PDFFIRST"
    r.pdf_choices["ITEM0001"] = "PDFLAST1"  # not on disk
    assert r.resolve("heltweg_study_2025").pdf.attachment_key == "PDFMAIN1"
