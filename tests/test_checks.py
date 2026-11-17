"""Checking a citation group end to end, with the model replaced by a fake."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from loccit.api import create_app
from loccit.strategies import NOT_FOUND, SUPPORTED, Result, StrategyError, Usage, passage
from loccit.workspace import GroupNotFound, Workspace
from loccit.zotero import ZoteroNotRunning
from tests.helpers import FakeZotero, make_pdf, raw_item, raw_pdf

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


class FakeStrategy:
    name = "fake-strategy"
    prompt_version = "t1"

    def __init__(self):
        self.calls: list[dict] = []
        self.verdict = SUPPORTED
        self.error: StrategyError | None = None

    def find_passages(self, context, papers, model, thinking, *, missing=()):
        self.calls.append(
            {
                "context": context,
                "papers": papers,
                "model": model,
                "thinking": thinking,
                "missing": missing,
            }
        )
        if self.error:
            raise self.error
        passages = []
        if self.verdict != NOT_FOUND:
            # The last sentence of the last paper first, then the first of the first.
            last = len(papers[-1].sentences)
            passages = [
                passage(papers[-1], last - 1, last, "Stärkste Stelle."),
                passage(papers[0], 0, 1, "Zweite Stelle."),
            ]
        return Result(self.verdict, passages, Usage(1000, 50, 400, 100), "VERDICT: ...")


@pytest.fixture
def zotero(tmp_path):
    papers = tmp_path / "zotero"
    papers.mkdir()
    fake = FakeZotero(
        papers,
        [
            raw_item("ITEM0001", "Jayvee Study", cite_key="heltweg_empirical_2025"),
            raw_pdf("PDF00001", "ITEM0001"),
            raw_item("ITEM0002", "Usable DSLs", cite_key="barisic_how_2012", year="2012"),
            raw_pdf("PDF00002", "ITEM0002"),
            raw_item("ITEM0003", "No PDF", cite_key="nopdf_paper_2020", year="2020"),
        ],
    )
    make_pdf(
        papers / "PDF00001.pdf",
        [
            [
                [
                    "Data engineering is hard work for most people. Participants were faster with Jayvee."
                ]
            ]
        ],
    )
    make_pdf(
        papers / "PDF00002.pdf",
        [
            [["Usability matters a lot for every DSL."]],
            [["We propose an evaluation process for it."]],
        ],
    )
    return fake


@pytest.fixture
def strategy():
    return FakeStrategy()


@pytest.fixture
def workspace(tmp_path, zotero, strategy):
    return Workspace(tmp_path / "home", zotero, strategy_factory=lambda: strategy)


def thesis(tmp_path, name="thesis.pdf", intro="An introduction.") -> bytes:
    path = make_pdf(
        tmp_path / name,
        [
            [
                [intro],
                [],
                [
                    "DSLs help [",
                    ("1", "cite.heltweg_empirical_2025"),
                    ", ",
                    ("2", "cite.barisic_how_2012"),
                    "].",
                ],
                [],
                [
                    "With a gap [",
                    ("3", "cite.heltweg_empirical_2025"),
                    ", ",
                    ("4", "cite.nopdf_paper_2020"),
                    "].",
                ],
                [],
                ["Nothing to read [", ("5", "cite.nopdf_paper_2020"), "]."],
            ]
        ],
    )
    return path.read_bytes()


@pytest.fixture
def document(workspace, tmp_path):
    return workspace.import_document("thesis.pdf", thesis(tmp_path))


def only_task(workspace):
    ((hash_,),) = workspace.store._execute("SELECT task_hash FROM tasks")
    return workspace.store.find_task(hash_)


def test_nothing_is_checked_until_asked(workspace, document, strategy):
    check = workspace.check(document.id, 0, SONNET, "off")
    assert check.context.text == "DSLs help [<cite_here>1, 2</cite_here>]."
    assert [p.paper.cite_key for p in check.papers] == [
        "heltweg_empirical_2025",
        "barisic_how_2012",
    ]
    assert (check.run, check.error, check.missing) == (None, None, [])
    assert strategy.calls == []


def test_a_check_is_stored_as_a_run_with_everything_about_it(workspace, document, strategy):
    check = workspace.check(document.id, 0, SONNET, "off", run=True)
    (call,) = strategy.calls
    assert (call["model"], call["thinking"], call["missing"]) == (SONNET, "off", ())
    # The whole paper goes to the model, sentence by sentence.
    assert [s.text for s in call["papers"][0].sentences] == [
        "Data engineering is hard work for most people.",
        "Participants were faster with Jayvee.",
    ]

    run = check.run
    assert (run.model, run.thinking, run.verdict) == (SONNET, "off", SUPPORTED)
    assert (run.strategy, run.prompt_version) == ("fake-strategy", "t1")
    assert (run.input_tokens, run.output_tokens) == (1000, 50)
    assert (run.cache_read_tokens, run.cache_write_tokens) == (400, 100)
    assert run.cost_usd == pytest.approx((1000 * 2 + 50 * 10 + 400 * 0.1 + 100 * 2.5) / 1e6)
    assert run.duration_ms >= 0 and run.started_at
    assert [(p.cite_key, p.start_sentence, p.end_sentence, p.page) for p in run.passages] == [
        ("barisic_how_2012", 1, 2, 1),
        ("heltweg_empirical_2025", 0, 1, 0),
    ]
    # For display, the passages get their rectangles and their PDF back.
    (strongest, attachment), (second, _) = check.passages
    assert (strongest.page, attachment) == (1, "PDF00002")
    assert strongest.rects and strongest.rects[0][0] == 1
    assert second.reason == "Zweite Stelle."

    task = only_task(workspace)
    assert task.paragraph == "DSLs help [<cite_here>1, 2</cite_here>]."
    assert task.cite_keys == ("heltweg_empirical_2025", "barisic_how_2012")
    assert [paper.cite_key for paper in task.papers] == list(task.cite_keys)
    assert all(len(paper.sha256) == 64 for paper in task.papers)


def test_the_stored_run_is_shown_per_model_and_level_and_rechecking_adds_a_run(
    workspace, document, strategy
):
    first = workspace.check(document.id, 0, SONNET, "off", run=True).run
    assert workspace.check(document.id, 0, SONNET, "off").run == first
    # Another combination has not been checked yet.
    assert workspace.check(document.id, 0, SONNET, "high").run is None
    assert workspace.check(document.id, 0, OPUS, "low").run is None
    assert len(strategy.calls) == 1

    strategy.verdict = NOT_FOUND
    again = workspace.check(document.id, 0, SONNET, "off", run=True).run
    other = workspace.check(document.id, 0, OPUS, "low", run=True).run
    assert again.id != first.id and again.task_id == first.task_id == other.task_id
    assert workspace.check(document.id, 0, SONNET, "off").run == again
    assert workspace.check(document.id, 0, SONNET, "off").passages == []
    # The earlier run is still there.
    assert [run.id for run in workspace.store.runs(first.task_id)] == [first.id, again.id, other.id]
    assert workspace.store.run(first.id).verdict == SUPPORTED


def test_a_group_with_a_missing_paper_is_checked_on_the_others(workspace, document, strategy):
    check = workspace.check(document.id, 1, SONNET, "off", run=True)
    assert check.missing == ["nopdf_paper_2020"]
    assert [p.paper.cite_key for p in check.papers] == ["heltweg_empirical_2025"]
    assert strategy.calls[0]["missing"] == ("nopdf_paper_2020",)
    assert check.run.verdict == SUPPORTED
    task = only_task(workspace)
    assert task.missing_keys == ("nopdf_paper_2020",)


def test_no_model_is_asked_about_a_missing_paper(workspace, document, strategy):
    check = workspace.check(document.id, 2, SONNET, "off", run=True)
    assert (check.error, check.run) == ("no_papers", None)
    assert check.missing == ["nopdf_paper_2020"]
    assert strategy.calls == []
    assert workspace.store._execute("SELECT count(*) FROM runs")[0][0] == 0


def test_adding_the_missing_paper_makes_a_new_task(workspace, document, strategy, zotero):
    before = workspace.check(document.id, 1, SONNET, "off", run=True).run
    zotero.raw_items.append(raw_pdf("PDF00003", "ITEM0003"))
    make_pdf(zotero.folder / "PDF00003.pdf", [[["The paper that was missing is here now."]]])
    workspace.citations(document.id, reload=True)

    check = workspace.check(document.id, 1, SONNET, "off")
    assert (check.missing, check.run) == ([], None)
    after = workspace.check(document.id, 1, SONNET, "off", run=True).run
    assert after.task_id != before.task_id
    assert len(strategy.calls[-1]["papers"]) == 2


def test_runs_stay_valid_for_a_new_version_with_the_same_paragraph(
    workspace, document, strategy, tmp_path
):
    run = workspace.check(document.id, 0, SONNET, "off", run=True).run
    newer = workspace.import_document(
        "thesis.pdf", thesis(tmp_path, "v2.pdf", intro="A rewritten introduction.")
    )
    assert newer.id == document.id and newer.pdf_sha256 != document.pdf_sha256
    assert workspace.check(document.id, 0, SONNET, "off").run == run
    assert len(strategy.calls) == 1


def test_a_failed_check_is_reported_and_not_stored(workspace, document, strategy):
    strategy.error = StrategyError("rate_limited")
    check = workspace.check(document.id, 0, SONNET, "off", run=True)
    assert (check.error, check.run) == ("rate_limited", None)
    assert workspace.store._execute("SELECT count(*) FROM runs")[0][0] == 0


def test_without_api_key_the_check_says_so(tmp_path, zotero, monkeypatch):
    monkeypatch.setattr("loccit.workspace.get_api_key", lambda: None)
    workspace = Workspace(tmp_path / "home", zotero)
    document = workspace.import_document("thesis.pdf", thesis(tmp_path))
    assert workspace.check(document.id, 0, SONNET, "off", run=True).error == "no_api_key"


def test_unknown_model_group_and_zotero_outage(workspace, document, strategy, zotero):
    assert workspace.check(document.id, 0, "gpt-x", "off", run=True).error == "unknown_model"
    with pytest.raises(GroupNotFound):
        workspace.check(document.id, 9, SONNET, "off")
    zotero.error = ZoteroNotRunning("refused")
    workspace.citations(document.id, reload=True)
    assert workspace.check(document.id, 0, SONNET, "off", run=True).error == "zotero_unavailable"
    assert strategy.calls == []


# --- over HTTP -------------------------------------------------------------


@pytest.fixture
def client(workspace, document):
    return TestClient(create_app(workspace), base_url="http://localhost")


def test_models_endpoint(client, monkeypatch):
    monkeypatch.setattr("loccit.api.model_settings.get_api_key", lambda: None)
    body = client.get("/api/models").json()
    assert body["default_model"] == SONNET
    assert body["has_api_key"] is False
    assert body["models"][0] == {
        "id": SONNET,
        "label": "Sonnet 5.5",
        "levels": ["off", "low", "medium", "high", "xhigh", "max"],
    }
    assert [model["id"] for model in body["models"]][1:] == [
        OPUS,
        "claude-haiku-5-5",
        "claude-fable-5-2",
    ]


def test_check_endpoints(client, document):
    url = f"/api/documents/{document.id}/groups/0/check"
    empty = client.get(url, params={"model": SONNET, "thinking": "off"}).json()
    assert empty["run"] is None and empty["error"] is None
    assert empty["context"] == "DSLs help [<cite_here>1, 2</cite_here>]."
    assert empty["checked_keys"] == ["heltweg_empirical_2025", "barisic_how_2012"]

    body = client.post(url, json={"model": SONNET, "thinking": "off"}).json()
    run = body["run"]
    assert (run["verdict"], run["model"], run["thinking"]) == ("supported", SONNET, "off")
    strongest = run["passages"][0]
    assert (strongest["cite_key"], strongest["attachment_key"], strongest["page"]) == (
        "barisic_how_2012",
        "PDF00002",
        1,
    )
    assert (strongest["start_sentence"], strongest["end_sentence"]) == (1, 2)
    assert strongest["rects"][0]["page"] == 1 and len(strongest["rects"][0]["rect"]) == 4
    assert strongest["reason"] == "Stärkste Stelle."
    # A later click finds the stored run without another model call.
    assert (
        client.get(url, params={"model": SONNET, "thinking": "off"}).json()["run"]["id"]
        == run["id"]
    )
    # The papers of the checked group can be fetched.
    assert client.get("/api/papers/PDF00002/pdf").status_code == 200


def test_check_endpoint_errors(client, document):
    base = f"/api/documents/{document.id}/groups"
    missing = client.post(f"{base}/2/check", json={"model": SONNET, "thinking": "off"}).json()
    assert missing["error"] == {"code": "no_papers", "detail": ""}
    assert missing["missing_keys"] == ["nopdf_paper_2020"]
    assert (
        client.get(f"{base}/9/check", params={"model": SONNET, "thinking": "off"}).status_code
        == 404
    )
    assert (
        client.get(
            "/api/documents/99/groups/0/check", params={"model": SONNET, "thinking": "off"}
        ).status_code
        == 404
    )


def test_a_check_that_is_already_running_is_joined_not_repeated(tmp_path, zotero):
    """Two tabs press "Neu prüfen" for the same citation at the same time."""
    started, release = threading.Event(), threading.Event()

    class SlowStrategy(FakeStrategy):
        def find_passages(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            return super().find_passages(*args, **kwargs)

    strategy = SlowStrategy()
    workspace = Workspace(tmp_path / "home", zotero, strategy_factory=lambda: strategy)
    document = workspace.import_document("thesis.pdf", thesis(tmp_path))
    results = {}

    def press(name: str, model: str = SONNET) -> None:
        results[name] = workspace.check(document.id, 0, model, "off", run=True).run

    first = threading.Thread(target=press, args=("first",))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=press, args=("second",))
    second.start()
    other_model = threading.Thread(target=press, args=("haiku", "claude-haiku-5-5"))
    other_model.start()
    time.sleep(0.2)
    release.set()
    for thread in (first, second, other_model):
        thread.join(5)

    assert results["second"] == results["first"]
    # Another model is another check and runs on its own.
    assert results["haiku"].model == "claude-haiku-5-5"
    assert len(strategy.calls) == 2
    assert len(workspace.store.runs(results["first"].task_id)) == 2

    # Pressing again later is a deliberate new check.
    again = workspace.check(document.id, 0, SONNET, "off", run=True).run
    assert again.id != results["first"].id
    assert len(strategy.calls) == 3
