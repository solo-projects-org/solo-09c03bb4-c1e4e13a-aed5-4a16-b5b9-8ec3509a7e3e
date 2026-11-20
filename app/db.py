from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    work_key TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS work_dois (
    doi      TEXT PRIMARY KEY,
    work_key TEXT NOT NULL REFERENCES works(work_key)
);

CREATE INDEX IF NOT EXISTS idx_work_dois_work ON work_dois(work_key);

CREATE TABLE IF NOT EXISTS citations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_doi TEXT NOT NULL,
    cited_doi  TEXT NOT NULL,
    UNIQUE (citing_doi, cited_doi)
);

CREATE INDEX IF NOT EXISTS idx_citations_citing ON citations(citing_doi);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited_doi);

-- Materialised, work-level citation index.  doi_count is the number of
-- distinct DOI-level citation records that map onto this work->work edge.
CREATE TABLE IF NOT EXISTS citation_index (
    citing_work TEXT NOT NULL,
    cited_work  TEXT NOT NULL,
    doi_count   INTEGER NOT NULL CHECK (doi_count > 0),
    PRIMARY KEY (citing_work, cited_work)
);

CREATE INDEX IF NOT EXISTS idx_citation_index_cited ON citation_index(cited_work);
"""

VERSION_KEY = "current_version"


def default_db_path() -> str:
    return os.environ.get("DB_PATH", "data/citations.db")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=10.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str) -> None:
    parent = Path(db_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def db_session(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialise writers for the whole decision+write window.

    BEGIN IMMEDIATE takes the database write lock up front, so two concurrent
    corrections both starting from the same version cannot interleave: the
    second one observes the version committed by the first.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    return int(row["value"]) if row else None


def set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (VERSION_KEY, str(version)),
    )
