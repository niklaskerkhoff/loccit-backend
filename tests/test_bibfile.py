from loccit.bibfile import parse_bib

ZOTERO_EXPORT = r"""
@comment{jabref-meta: databaseType:bibtex;}

@article{koshima_collaborative_2015,
	series = {Model {Driven} {Development} ({Selected} \& extended papers)},
	title = {Collaborative editing of {EMF}/{Ecore} meta-models and models},
	volume = {113},
	doi = {10.1016/j.scico.2015.07.004},
	journal = {Science of Computer Programming},
	author = {Koshima, Amanuel Alemayehu and Englebert, Vincent},
	month = dec,
	year = {2015},
	pages = {3--28},
}

@misc{maasch_compositional_2025,
	title = {Compositional {Causal} {Reasoning} {Evaluation} in {Language} {Models}},
	author = {Maasch, Jacqueline R. M. A. and Hüyük, Alihan},
	year = {2025},
	note = {arXiv:2503.04556 [cs.CL]},
}
"""


def test_reads_zotero_export():
    bib = parse_bib(ZOTERO_EXPORT)
    assert list(bib.entries) == ["koshima_collaborative_2015", "maasch_compositional_2025"]
    entry = bib.entries["koshima_collaborative_2015"]
    assert entry.entry_type == "article"
    assert entry.title == "Collaborative editing of {EMF}/{Ecore} meta-models and models"
    assert entry.doi == "10.1016/j.scico.2015.07.004"
    assert entry.year == "2015"
    assert entry.fields["month"] == "dec"
    assert entry.authors == ["Koshima, Amanuel Alemayehu", "Englebert, Vincent"]
    assert entry.last_names == ["Koshima", "Englebert"]
    assert bib.duplicate_keys == set()


def test_quoted_values_concatenation_and_parentheses():
    bib = parse_bib(
        '@string{acm = "ACM"}\n'
        '@book(knuth_art_1997, title = "The {Art} of " # "Programming", '
        "publisher = acm, date = {1997-07-01})"
    )
    entry = bib.entries["knuth_art_1997"]
    assert entry.title == "The {Art} of Programming"
    assert entry.fields["publisher"] == "acm"
    assert entry.year == "1997"


def test_key_defined_twice_is_reported():
    bib = parse_bib("@misc{a, title = {One}}\n@misc{a, title = {Two}}\n@misc{b, title = {B}}")
    assert bib.duplicate_keys == {"a"}
    assert set(bib.entries) == {"a", "b"}


def test_last_names():
    bib = parse_bib(
        "@misc{k, author = {{Object Management Group} and Donald E. Knuth and {van} Beek, J.}}"
    )
    assert bib.entries["k"].last_names == ["Object Management Group", "Knuth", "van Beek"]


def test_entry_without_fields():
    assert parse_bib("@misc{only_key}").entries["only_key"].fields == {}
