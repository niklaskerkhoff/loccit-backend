"""The SQLite file: uploaded documents, the user's choices, and every check.

A *task* is what was checked: a paragraph with a marked citation, the cited
keys and the exact paper files. A *run* is one check of a task by one model
at one thinking level. Runs are only ever added, so that runs of different
models on the same task can be compared later.
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    -- File name of the uploaded PDF. Uploading a PDF with the same name
    -- replaces the stored version.
    name        TEXT NOT NULL UNIQUE,
    pdf_sha256  TEXT NOT NULL,
    has_bib     INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    opened_at   TEXT NOT NULL
);
-- Which Zotero item a cite key stands for, where loccit must not guess
-- between several versions of a publication.
CREATE TABLE IF NOT EXISTS item_choices (
    cite_key  TEXT PRIMARY KEY,
    item_key  TEXT NOT NULL,
    chosen_at TEXT NOT NULL
);
-- Which PDF of a Zotero item to show, if not Zotero's main attachment.
CREATE TABLE IF NOT EXISTS pdf_choices (
    item_key       TEXT PRIMARY KEY,
    attachment_key TEXT NOT NULL,
    chosen_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    -- Hash over paragraph_hash, cite_keys and papers: equal hash, same task.
    task_hash      TEXT NOT NULL UNIQUE,
    paragraph_hash TEXT NOT NULL,
    -- The checked text, the citation marked with <cite_here>.
    paragraph      TEXT NOT NULL,
    -- JSON array: the keys of the citation group, in order.
    cite_keys      TEXT NOT NULL,
    -- JSON array of {cite_key, sha256, sentences}: the papers that were
    -- read, by file hash and version of the sentence splitting.
    papers         TEXT NOT NULL,
    -- JSON array: cited keys whose paper was not available.
    missing_keys   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY,
    task_id            INTEGER NOT NULL REFERENCES tasks (id),
    model              TEXT NOT NULL,
    thinking           TEXT NOT NULL,
    strategy           TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    duration_ms        INTEGER NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_usd           REAL NOT NULL,
    verdict            TEXT NOT NULL,
    -- The model's answer as text.
    response_text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_task ON runs (task_id, model, thinking, id);
-- The passages of a run as sentence ranges per paper, strongest first.
CREATE TABLE IF NOT EXISTS passages (
    run_id         INTEGER NOT NULL REFERENCES runs (id),
    rank           INTEGER NOT NULL,
    cite_key       TEXT NOT NULL,
    start_sentence INTEGER NOT NULL,
    end_sentence   INTEGER NOT NULL,
    page           INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    PRIMARY KEY (run_id, rank)
);
"""


@dataclass(frozen=True)
class Document:
    id: int
    name: str
    pdf_sha256: str
    has_bib: bool
    uploaded_at: str
    opened_at: str


@dataclass(frozen=True)
class TaskPaper:
    cite_key: str
    sha256: str
    # Version of the sentence splitting the sentence indices refer to.
    sentences: str


@dataclass(frozen=True)
class Task:
    id: int
    task_hash: str
    paragraph_hash: str
    paragraph: str
    cite_keys: tuple[str, ...]
    papers: tuple[TaskPaper, ...]
    missing_keys: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class StoredPassage:
    cite_key: str
    start_sentence: int
    end_sentence: int
    page: int
    reason: str


@dataclass(frozen=True)
class Run:
    id: int
    task_id: int
    model: str
    thinking: str
    strategy: str
    prompt_version: str
    started_at: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    verdict: str
    response_text: str
    passages: tuple[StoredPassage, ...] = ()


def task_hash(paragraph_hash: str, cite_keys: list[str], papers: list[TaskPaper]) -> str:
    identity = [paragraph_hash, cite_keys, [[p.cite_key, p.sha256, p.sentences] for p in papers]]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    def _execute(self, sql: str, *args) -> list[sqlite3.Row]:
        with closing(self._connect()) as db, db:
            return db.execute(sql, args).fetchall()

    # --- documents ----------------------------------------------------------

    def documents(self) -> list[Document]:
        rows = self._execute("SELECT * FROM documents ORDER BY opened_at DESC, id DESC")
        return [_document(row) for row in rows]

    def document(self, document_id: int) -> Document | None:
        rows = self._execute("SELECT * FROM documents WHERE id = ?", document_id)
        return _document(rows[0]) if rows else None

    def document_named(self, name: str) -> Document | None:
        rows = self._execute("SELECT * FROM documents WHERE name = ?", name)
        return _document(rows[0]) if rows else None

    def save_document(self, name: str, pdf_sha256: str, has_bib: bool) -> Document:
        """Insert the document, or mark the one of that name as replaced."""
        now = _now()
        self._execute(
            "INSERT INTO documents (name, pdf_sha256, has_bib, uploaded_at, opened_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET pdf_sha256 = excluded.pdf_sha256, "
            "has_bib = excluded.has_bib, uploaded_at = excluded.uploaded_at, "
            "opened_at = excluded.opened_at",
            name,
            pdf_sha256,
            int(has_bib),
            now,
            now,
        )
        return self.document_named(name)

    def touch_document(self, document_id: int) -> None:
        self._execute("UPDATE documents SET opened_at = ? WHERE id = ?", _now(), document_id)

    def delete_document(self, document_id: int) -> None:
        self._execute("DELETE FROM documents WHERE id = ?", document_id)

    # --- choices ------------------------------------------------------------

    def item_choices(self) -> dict[str, str]:
        rows = self._execute("SELECT cite_key, item_key FROM item_choices")
        return {row["cite_key"]: row["item_key"] for row in rows}

    def set_item_choice(self, cite_key: str, item_key: str | None) -> None:
        if item_key is None:
            self._execute("DELETE FROM item_choices WHERE cite_key = ?", cite_key)
        else:
            self._execute(
                "INSERT OR REPLACE INTO item_choices VALUES (?, ?, ?)", cite_key, item_key, _now()
            )

    def pdf_choices(self) -> dict[str, str]:
        rows = self._execute("SELECT item_key, attachment_key FROM pdf_choices")
        return {row["item_key"]: row["attachment_key"] for row in rows}

    def set_pdf_choice(self, item_key: str, attachment_key: str | None) -> None:
        if attachment_key is None:
            self._execute("DELETE FROM pdf_choices WHERE item_key = ?", item_key)
        else:
            self._execute(
                "INSERT OR REPLACE INTO pdf_choices VALUES (?, ?, ?)",
                item_key,
                attachment_key,
                _now(),
            )

    # --- tasks and runs -----------------------------------------------------

    def find_task(self, task_hash: str) -> Task | None:
        rows = self._execute("SELECT * FROM tasks WHERE task_hash = ?", task_hash)
        return _task(rows[0]) if rows else None

    def get_or_create_task(
        self,
        paragraph_hash: str,
        paragraph: str,
        cite_keys: list[str],
        papers: list[TaskPaper],
        missing_keys: list[str],
    ) -> Task:
        key = task_hash(paragraph_hash, cite_keys, papers)
        self._execute(
            "INSERT OR IGNORE INTO tasks (task_hash, paragraph_hash, paragraph, cite_keys, "
            "papers, missing_keys, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            key,
            paragraph_hash,
            paragraph,
            json.dumps(cite_keys),
            json.dumps([vars(paper) for paper in papers]),
            json.dumps(missing_keys),
            _now(),
        )
        return self.find_task(key)

    def add_run(
        self,
        task_id: int,
        *,
        model: str,
        thinking: str,
        strategy: str,
        prompt_version: str,
        started_at: str,
        duration_ms: int,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        verdict: str,
        response_text: str,
        passages: list[StoredPassage],
    ) -> Run:
        """Store a finished check. Earlier runs are never touched."""
        with closing(self._connect()) as db, db:
            run_id = db.execute(
                "INSERT INTO runs (task_id, model, thinking, strategy, prompt_version, "
                "started_at, duration_ms, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, cost_usd, verdict, response_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    model,
                    thinking,
                    strategy,
                    prompt_version,
                    started_at,
                    duration_ms,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                    verdict,
                    response_text,
                ),
            ).lastrowid
            db.executemany(
                "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, rank, p.cite_key, p.start_sentence, p.end_sentence, p.page, p.reason)
                    for rank, p in enumerate(passages)
                ],
            )
        return self.run(run_id)

    def run(self, run_id: int) -> Run | None:
        rows = self._execute("SELECT * FROM runs WHERE id = ?", run_id)
        return self._run(rows[0]) if rows else None

    def runs(self, task_id: int) -> list[Run]:
        """All runs of a task, oldest first."""
        rows = self._execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", task_id)
        return [self._run(row) for row in rows]

    def latest_run(self, task_id: int, model: str, thinking: str) -> Run | None:
        rows = self._execute(
            "SELECT * FROM runs WHERE task_id = ? AND model = ? AND thinking = ? "
            "ORDER BY id DESC LIMIT 1",
            task_id,
            model,
            thinking,
        )
        return self._run(rows[0]) if rows else None

    def _run(self, row: sqlite3.Row) -> Run:
        passages = self._execute(
            "SELECT cite_key, start_sentence, end_sentence, page, reason FROM passages "
            "WHERE run_id = ? ORDER BY rank",
            row["id"],
        )
        return Run(**dict(row), passages=tuple(StoredPassage(**dict(p)) for p in passages))


def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        name=row["name"],
        pdf_sha256=row["pdf_sha256"],
        has_bib=bool(row["has_bib"]),
        uploaded_at=row["uploaded_at"],
        opened_at=row["opened_at"],
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        task_hash=row["task_hash"],
        paragraph_hash=row["paragraph_hash"],
        paragraph=row["paragraph"],
        cite_keys=tuple(json.loads(row["cite_keys"])),
        papers=tuple(TaskPaper(**paper) for paper in json.loads(row["papers"])),
        missing_keys=tuple(json.loads(row["missing_keys"])),
        created_at=row["created_at"],
    )
