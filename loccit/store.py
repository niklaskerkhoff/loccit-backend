"""The SQLite file: uploaded documents and the user's choices."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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
"""


@dataclass(frozen=True)
class Document:
    id: int
    name: str
    pdf_sha256: str
    has_bib: bool
    uploaded_at: str
    opened_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        name=row["name"],
        pdf_sha256=row["pdf_sha256"],
        has_bib=bool(row["has_bib"]),
        uploaded_at=row["uploaded_at"],
        opened_at=row["opened_at"],
    )
