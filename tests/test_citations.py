import pymupdf
import pytest

from loccit.citations import parse_cite_dest, scan_citations
from tests.helpers import make_pdf


@pytest.mark.parametrize(
    ("dest", "key"),
    [
        ("cite.heltweg_empirical_2025", "heltweg_empirical_2025"),
        ("cite.0@heltweg_empirical_2025", "heltweg_empirical_2025"),
        ("cite.12@brown_language_2020", "brown_language_2020"),
        ("cite.key.with.dots", "key.with.dots"),
        ("section.2.1", None),
        ("cite.", None),
        (None, None),
    ],
)
def test_parse_cite_dest(dest, key):
    assert parse_cite_dest(dest) == key


def scan(tmp_path, *pages):
    return scan_citations(make_pdf(tmp_path / "thesis.pdf", list(pages)))


def test_numeric_group(tmp_path):
    result = scan(
        tmp_path,
        [["As shown before [", ("1", "cite.a"), ", ", ("2", "cite.b"), ", ", ("3", "cite.c"), "]."]],
    )
    assert [group.keys for group in result.groups] == [["a", "b", "c"]]
    assert result.groups[0].page == 0
    assert result.keys == ["a", "b", "c"]


def test_citations_with_text_between_are_separate_groups(tmp_path):
    result = scan(
        tmp_path,
        [["First [", ("1", "cite.a"), "] and then [", ("2", "cite.b"), "]. [", ("3", "cite.c"), "]"]],
    )
    assert [group.keys for group in result.groups] == [["a"], ["b"], ["c"]]
    assert [group.id for group in result.groups] == [0, 1, 2]


def test_author_and_year_linked_separately_are_one_citation(tmp_path):
    result = scan(
        tmp_path,
        [
            [
                "(",
                ("Heltweg et al.", "cite.heltweg"),
                ", ",
                ("2025", "cite.heltweg"),
                "; ",
                ("Brown", "cite.brown"),
                ", ",
                ("2020", "cite.brown"),
                ") is a group.",
            ]
        ],
    )
    assert [group.keys for group in result.groups] == [["heltweg", "brown"]]
    assert [len(citation.rects) for citation in result.groups[0].citations] == [2, 2]


def test_textual_citations_from_one_command_are_one_group(tmp_path):
    result = scan(
        tmp_path,
        [
            [
                ("Heltweg et al.", "cite.heltweg"),
                " (",
                ("2025", "cite.heltweg"),
                "); ",
                ("Brown", "cite.brown"),
                " (",
                ("2020", "cite.brown"),
                ") agree.",
            ]
        ],
    )
    assert [group.keys for group in result.groups] == [["heltweg", "brown"]]


def test_group_continues_on_next_line(tmp_path):
    result = scan(
        tmp_path,
        [["Claim [", ("1", "cite.a"), ","], [("2", "cite.b"), "] continues."]],
    )
    assert [group.keys for group in result.groups] == [["a", "b"]]


def test_link_broken_across_lines_is_one_citation(tmp_path):
    result = scan(
        tmp_path,
        [["As ", ("Heltweg et", "cite.heltweg")], [("al. 2025", "cite.heltweg"), " wrote."]],
    )
    assert [group.keys for group in result.groups] == [["heltweg"]]
    assert len(result.groups[0].citations[0].rects) == 2


def test_biblatex_destinations(tmp_path):
    result = scan(tmp_path, [["[", ("1", "cite.0@a"), "; ", ("2", "cite.0@b"), "]"]])
    assert [group.keys for group in result.groups] == [["a", "b"]]
    assert result.groups[0].citations[0].dest == "cite.0@a"


def test_same_key_cited_twice_gives_two_groups_and_one_key(tmp_path):
    result = scan(
        tmp_path,
        [["One [", ("1", "cite.a"), "]."]],
        [["Two [", ("1", "cite.a"), ", ", ("2", "cite.b"), "]."]],
    )
    assert [(group.page, group.keys) for group in result.groups] == [(0, ["a"]), (1, ["a", "b"])]
    assert result.keys == ["a", "b"]


def test_compressed_range_is_flagged_not_grouped(tmp_path):
    # A hyphen stands in for the en dash, which the built-in test font lacks.
    result = scan(tmp_path, [["See [", ("1", "cite.a"), "-", ("3", "cite.c"), "]."]])
    assert [group.keys for group in result.groups] == [["a"], ["c"]]
    assert result.suspected_ranges == 1


def test_other_internal_links_are_ignored(tmp_path):
    result = scan(
        tmp_path,
        [["See Section ", ("2.1", "section.2.1"), " and [", ("1", "cite.a"), "]."]],
    )
    assert [group.keys for group in result.groups] == [["a"]]
    assert result.link_count == 2
    assert result.dest_prefixes == {"section": 1, "cite": 1}


def test_pdf_without_citation_links(tmp_path):
    result = scan(tmp_path, [["See Section ", ("2.1", "section.2.1"), "."]])
    assert result.groups == []
    assert result.keys == []
    assert result.dest_prefixes == {"section": 1}


def test_rects_are_in_pdf_space_and_carry_the_annotation_ids(tmp_path):
    path = make_pdf(tmp_path / "thesis.pdf", [[["See [", ("1", "cite.a"), "]."]]])
    citation = scan_citations(path).groups[0].citations[0]
    with pymupdf.open(path) as doc:
        link = doc[0].get_links()[0]
        raw_rect = doc.xref_get_key(link["xref"], "Rect")[1]
    assert citation.annotation_ids == [f"{link['xref']}R"]
    expected = [float(value) for value in raw_rect.strip("[]").split()]
    assert citation.rects == [pytest.approx(expected, abs=0.01)]
    # Origin bottom left: the first line of the page lies near its top.
    assert citation.rects[0][1] > 700


def test_rotated_page(tmp_path):
    path = make_pdf(
        tmp_path / "upright.pdf", [[["See [", ("1", "cite.a"), ", ", ("2", "cite.b"), "]."]]]
    )
    upright = scan_citations(path)
    with pymupdf.open(path) as doc:
        doc[0].set_rotation(90)
        doc.save(tmp_path / "rotated.pdf")
    rotated = scan_citations(tmp_path / "rotated.pdf")
    assert [group.keys for group in rotated.groups] == [["a", "b"]]
    assert rotated.groups[0].citations[0].rects == upright.groups[0].citations[0].rects
