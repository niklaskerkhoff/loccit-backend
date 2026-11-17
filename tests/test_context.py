from loccit.citations import scan_citations
from loccit.context import citation_context
from tests.helpers import make_pdf


def contexts(path):
    return [citation_context(path, group) for group in scan_citations(path).groups]


def test_context_is_the_paragraph_with_the_citation_marked(tmp_path):
    path = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                ["Domain-specific languages help non-programmers build data pipe-"],
                [
                    "lines [",
                    ("1", "cite.a"),
                    ", ",
                    ("2", "cite.b"),
                    "]. Transformers changed NLP [",
                    ("3", "cite.c"),
                    "].",
                ],
            ]
        ],
    )
    first, second = contexts(path)
    assert first.text == (
        "Domain-specific languages help non-programmers build data pipelines "
        "[<cite_here>1, 2</cite_here>]. Transformers changed NLP [3]."
    )
    assert second.text == (
        "Domain-specific languages help non-programmers build data pipelines "
        "[1, 2]. Transformers changed NLP [<cite_here>3</cite_here>]."
    )
    assert first.page == 0
    assert first.hash != second.hash


def test_only_the_paragraph_of_the_citation_is_used(tmp_path):
    path = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                ["The first paragraph ends here."],
                [
                    "    A second paragraph, indented, cites a paper [",
                    ("1", "cite.a"),
                    "] and goes on",
                ],
                ["for another line."],
                ["    The third paragraph is not part of it."],
            ]
        ],
    )
    (context,) = contexts(path)
    assert context.text == (
        "A second paragraph, indented, cites a paper [<cite_here>1</cite_here>] and goes on "
        "for another line."
    )


def test_hash_follows_the_paragraph_not_the_rest_of_the_document(tmp_path):
    paragraph = ["The claim under test is cited here [", ("1", "cite.a"), "]."]
    before = make_pdf(tmp_path / "v1.pdf", [[["An introduction."], paragraph]])
    after = make_pdf(
        tmp_path / "v2.pdf", [[["A rewritten, longer introduction."]], [["More text."], paragraph]]
    )
    changed = make_pdf(
        tmp_path / "v3.pdf",
        [[["The claim under test, reworded, is cited [", ("1", "cite.a"), "]."]]],
    )
    (old,), (moved,), (reworded,) = contexts(before), contexts(after), contexts(changed)
    assert old.hash == moved.hash
    assert moved.page == 1
    assert old.hash != reworded.hash
