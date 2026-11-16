import pytest

from loccit.normalize import normalize_doi, normalize_title


@pytest.mark.parametrize(
    "raw",
    [
        "10.1145/3193954.3193961",
        "https://doi.org/10.1145/3193954.3193961",
        "http://dx.doi.org/10.1145/3193954.3193961",
        "doi:10.1145/3193954.3193961",
        "  DOI: 10.1145/3193954.3193961 ",
    ],
)
def test_doi_prefixes_are_removed(raw):
    assert normalize_doi(raw) == "10.1145/3193954.3193961"


def test_doi_is_lowercased_and_unescaped():
    assert normalize_doi("10.48550/ARXIV.2503.04556") == "10.48550/arxiv.2503.04556"
    assert normalize_doi(r"10.1007/978-3-642-04425-0\_32") == "10.1007/978-3-642-04425-0_32"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_doi(raw):
    assert normalize_doi(raw) is None


def test_title_drops_braces_case_and_punctuation():
    bib = "Collaborative editing of {EMF}/{Ecore} meta-models and models: {Conflict} detection"
    zotero = "Collaborative editing of EMF/Ecore meta-models and models: Conflict detection"
    assert normalize_title(bib) == normalize_title(zotero)
    assert normalize_title(bib) == (
        "collaborative editing of emf ecore meta models and models conflict detection"
    )


def test_title_drops_latex_commands_and_folds_accents():
    assert normalize_title(r"Selected \& extended {H{\"u}y{\"u}k}") == normalize_title(
        "Selected & extended Hüyük"
    )
    assert normalize_title(r"A \textit{very} good   idea") == "a very good idea"


def test_different_titles_stay_different():
    assert normalize_title("Attention Is All You Need") != normalize_title(
        "Attention Is Not All You Need"
    )
