"""Storing checks: every run is kept, and runs of the same task belong together."""

from loccit.store import Store, StoredPassage, TaskPaper, task_hash

HELTWEG = TaskPaper("heltweg_empirical_2025", "sha-heltweg", "1")
BARISIC = TaskPaper("barisic_how_2012", "sha-barisic", "1")
KEYS = ["heltweg_empirical_2025", "barisic_how_2012"]


def add_run(store: Store, task_id: int, model: str, thinking: str, verdict: str, passages=()):
    return store.add_run(
        task_id,
        model=model,
        thinking=thinking,
        strategy="claude-citations",
        prompt_version="1",
        started_at="2026-11-17T10:00:00+00:00",
        duration_ms=4200,
        input_tokens=18000,
        output_tokens=150,
        cache_read_tokens=0,
        cache_write_tokens=17500,
        cost_usd=0.045,
        verdict=verdict,
        response_text=f"VERDICT: {verdict}",
        passages=list(passages),
    )


def test_runs_of_all_models_hang_on_the_same_task(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    task = store.get_or_create_task(
        "para-hash", "A claim [<cite_here>1</cite_here>].", KEYS, [HELTWEG, BARISIC], []
    )
    again = store.get_or_create_task(
        "para-hash", "A claim [<cite_here>1</cite_here>].", KEYS, [HELTWEG, BARISIC], []
    )
    assert again == task
    assert (task.cite_keys, task.papers) == (tuple(KEYS), (HELTWEG, BARISIC))

    passages = [
        StoredPassage("heltweg_empirical_2025", 12, 14, 6, "Stärkste Stelle."),
        StoredPassage("barisic_how_2012", 3, 4, 1, "Schwächere Stelle."),
    ]
    first = add_run(store, task.id, "claude-sonnet-5-5", "off", "supported", passages)
    second = add_run(store, task.id, "claude-opus-5-5", "high", "partially_supported", passages[:1])
    third = add_run(store, task.id, "claude-sonnet-5-5", "off", "not_found")

    # Nothing is overwritten: all three runs are there, oldest first.
    assert [run.id for run in store.runs(task.id)] == [first.id, second.id, third.id]
    assert store.run(first.id).verdict == "supported"
    # Passages are stored as sentence ranges per paper, in order of strength.
    assert store.run(first.id).passages == tuple(passages)
    assert store.run(third.id).passages == ()
    assert (first.duration_ms, first.input_tokens, first.cost_usd) == (4200, 18000, 0.045)
    assert (first.strategy, first.prompt_version) == ("claude-citations", "1")


def test_latest_run_per_model_and_thinking_level(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    task = store.get_or_create_task("para-hash", "text", KEYS, [HELTWEG], [])
    assert store.latest_run(task.id, "claude-sonnet-5-5", "off") is None
    add_run(store, task.id, "claude-sonnet-5-5", "off", "supported")
    newest = add_run(store, task.id, "claude-sonnet-5-5", "off", "not_found")
    other = add_run(store, task.id, "claude-sonnet-5-5", "high", "supported")
    assert store.latest_run(task.id, "claude-sonnet-5-5", "off") == newest
    assert store.latest_run(task.id, "claude-sonnet-5-5", "high") == other
    assert store.latest_run(task.id, "claude-opus-5-5", "high") is None


def test_what_makes_a_new_task(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    base = store.get_or_create_task("para-hash", "text", KEYS, [HELTWEG], ["barisic_how_2012"])

    def task(
        paragraph_hash="para-hash", keys=KEYS, papers=(HELTWEG,), missing=("barisic_how_2012",)
    ):
        return store.get_or_create_task(
            paragraph_hash, "text", list(keys), list(papers), list(missing)
        )

    assert task() == base
    # The paragraph changed.
    assert task(paragraph_hash="other").id != base.id
    # The missing paper was added to Zotero.
    assert task(papers=(HELTWEG, BARISIC), missing=()).id != base.id
    # Zotero now holds another file for the paper.
    assert task(papers=(TaskPaper("heltweg_empirical_2025", "sha-new", "1"),)).id != base.id
    # The sentence splitting changed, so sentence indices mean something else.
    assert task(papers=(TaskPaper("heltweg_empirical_2025", "sha-heltweg", "2"),)).id != base.id
    # Another citation group in the same paragraph.
    assert task(keys=["heltweg_empirical_2025"]).id != base.id
    assert task_hash("para-hash", KEYS, [HELTWEG]) == base.task_hash


def test_runs_survive_reopening_the_file(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    task = store.get_or_create_task("para-hash", "text", KEYS, [HELTWEG], [])
    run = add_run(store, task.id, "claude-sonnet-5-5", "off", "supported")
    reopened = Store(tmp_path / "loccit.sqlite")
    assert reopened.find_task(task.task_hash) == task
    assert reopened.latest_run(task.id, "claude-sonnet-5-5", "off") == run
