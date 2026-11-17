import pymupdf
import pytest

from loccit.sentences import extract_sentences
from loccit.textlayout import clean, sentence_spans
from tests.helpers import make_pdf


def split(text: str) -> list[str]:
    return [text[start:end] for start, end in sentence_spans(text)]


def test_sentences_end_at_punctuation_before_a_capital():
    assert split("Data is the foundation. It must be usable! Is it? Yes.") == [
        "Data is the foundation.",
        "It must be usable!",
        "Is it?",
        "Yes.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Heltweg et al. (2025) show that Jayvee helps.",
        "As shown in Fig. 3 and Sec. 4, the effect holds.",
        "This holds, e.g. for J. Smith and A. B. Jones.",
        "The value is 3.5 on average, i.e. Rather low.",
        "Siehe z.B. Abb. 2 bzw. Nr. 7 im Anhang.",
    ],
)
def test_abbreviations_numbers_and_initials_do_not_end_a_sentence(text):
    assert split(text) == [text]


def test_quotes_and_brackets_stay_with_their_sentence():
    assert split('He said "stop." Then (as before) it ended [12]. Next one.') == [
        'He said "stop."',
        "Then (as before) it ended [12].",
        "Next one.",
    ]


def test_clean_expands_ligatures_and_collapses_whitespace():
    assert clean("eﬃcient  \n workﬂow") == "efficient workflow"


def text_in(page: pymupdf.Page, rect) -> str:
    """The text under a rectangle given in PDF user space."""
    return " ".join(page.get_textbox(pymupdf.Rect(rect) * page.transformation_matrix).split())


def test_every_sentence_knows_its_page_and_rectangles(tmp_path):
    path = make_pdf(
        tmp_path / "paper.pdf",
        [
            [
                ["Jayvee is a domain-specific language for data pipelines. Participants"],
                ["solved the tasks faster with it. The error rate stayed the same in all groups."],
            ],
            [["A second page starts here with one more sentence for the test."]],
        ],
    )
    sentences = extract_sentences(path)
    assert [(s.index, s.page, s.text) for s in sentences] == [
        (0, 0, "Jayvee is a domain-specific language for data pipelines."),
        (1, 0, "Participants solved the tasks faster with it."),
        (2, 0, "The error rate stayed the same in all groups."),
        (3, 1, "A second page starts here with one more sentence for the test."),
    ]
    # A sentence that runs over a line break has one rectangle per line.
    assert [len(s.rects) for s in sentences] == [1, 2, 1, 1]

    with pymupdf.open(path) as doc:
        first, second = sentences[1].rects
        assert text_in(doc[0], first) == "Participants"
        assert text_in(doc[0], second) == "solved the tasks faster with it."
        assert text_in(doc[0], sentences[2].rects[0]) == sentences[2].text
        assert text_in(doc[1], sentences[3].rects[0]) == sentences[3].text
        # Rectangles are in PDF user space: the first line is near the top.
        assert first[1] > second[1] > 600


def test_hyphenation_at_a_line_end_is_resolved_but_both_lines_are_marked(tmp_path):
    path = make_pdf(
        tmp_path / "paper.pdf",
        [[["The participants found the language more approach-"], ["able than the alternative."]]],
    )
    (sentence,) = extract_sentences(path)
    assert (
        sentence.text
        == "The participants found the language more approachable than the alternative."
    )
    assert len(sentence.rects) == 2
    with pymupdf.open(path) as doc:
        assert text_in(doc[0], sentence.rects[0]).endswith("approach-")
        assert text_in(doc[0], sentence.rects[1]) == "able than the alternative."


def test_a_hyphen_before_a_capital_is_kept(tmp_path):
    path = make_pdf(
        tmp_path / "paper.pdf",
        [[["The model outperforms the Berkeley-"], ["Parser on this task by a wide margin."]]],
    )
    assert extract_sentences(path)[0].text.startswith("The model outperforms the Berkeley- Parser")


def test_short_fragments_are_collected_into_one_unit(tmp_path):
    path = make_pdf(
        tmp_path / "paper.pdf",
        [
            [
                # Empty lines make each fragment a block of its own, like table cells.
                ["Table 1"],
                [],
                ["Group A"],
                [],
                ["12.5"],
                [],
                ["-----"],
                [],
                ["The table above lists the results of all groups in the study."],
                [],
                ["Group B"],
            ]
        ],
    )
    sentences = extract_sentences(path)
    assert [s.text for s in sentences] == [
        "Table 1 Group A 12.5",
        "The table above lists the results of all groups in the study.",
        "Group B",
    ]
    assert len(sentences[0].rects) == 3


def test_pdf_without_text_has_no_sentences(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    doc.save(tmp_path / "scan.pdf")
    assert extract_sentences(tmp_path / "scan.pdf") == []
