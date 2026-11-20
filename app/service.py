"""Core domain logic: import, correction (regrouping) and citation lookup.

Data model
----------
* ``works``          -- one row per research output (integer id).
* ``work_dois``      -- DOI -> work mapping.  A DOI belongs to exactly one work;
                        a work may own several DOIs (preprint + journal version).
* ``citations``      -- raw DOI-level directed citation edges, unique on
                        (citing_doi, cited_doi).  Corrections never rewrite these.
* ``citation_index`` -- materialised work->work citation edges with ``doi_count``
                        = number of distinct raw DOI edges that collapse onto
                        that work-level edge.  Internal citations (both ends in
                        the same work) never appear here.

Both citation directions read the same index table in opposite directions.

Versioning
----------
``meta.current_version`` is a monotonically increasing integer.  Each accepted
mutation (import or correction) bumps it and commits together with its writes.
A correction carries the version the librarian was looking at; if a concurrent
correction (or import) committed first, the stale submission is rejected with
HTTP 409 and leaves both the grouping and the index untouched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .db import get_version, set_version, write_transaction
from .models import (
    CitationEdge,
    CitationResponse,
    ImportRequest,
    ImportResult,
    RevisionIn,
    RevisionResult,
    WorkDetail,
    WorkRef,
)

# SQLite's default limit is 999 bound parameters; stay well below it.
CHUNK = 500


class NotFound(Exception):
    """Requested work does not exist."""


class StaleVersion(Exception):
    """The submitted version is no longer current."""

    def __init__(self, current: int):
        super().__init__(f"current version is {current}")
        self.current = current


class InvalidRequest(Exception):
    """The request cannot be applied as submitted."""


@dataclass(frozen=True)
class _Move:
    doi: str
    old_work: str | None
    new_work: str


def _norm(doi: str) -> str:
    return doi.strip()


def _chunks(items: list, size: int = CHUNK) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_batch(conn: sqlite3.Connection, payload: ImportRequest) -> ImportResult:
    """Import works and directed DOI citations atomically.

    A DOI may be assigned to only one work in a batch; re-importing a DOI
    updates its work.  Citations may reference DOIs from this batch or ones
    already imported.  Citations whose two DOIs fall in the same work are
    internal: stored but never indexed.  Repeating an existing citation is a
    no-op.
    """
    doi_to_work: dict[str, int] = {}
    for work in payload.works:
        for raw_doi in work.dois:
            doi = _norm(raw_doi)
            if not doi:
                raise InvalidRequest("DOI must not be blank")
            previous = doi_to_work.get(doi)
            if previous is not None and previous != work.work_id:
                raise InvalidRequest(
                    f"DOI {doi!r} is assigned to both work {previous} "
                    f"and work {work.work_id} in this import"
                )
            doi_to_work[doi] = work.work_id

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    raw_duplicates = 0
    for citation in payload.citations:
        citing, cited = _norm(citation.citing), _norm(citation.cited)
        if not citing or not cited:
            raise InvalidRequest("citation DOIs must not be blank")
        key = (citing, cited)
        if key in seen:
            raw_duplicates += 1
            continue
        seen.add(key)
        edges.append(key)

    with write_transaction(conn):
        work_keys = {str(w.work_id) for w in payload.works}
        conn.executemany(
            "INSERT OR IGNORE INTO works(work_key) VALUES (?)",
            [(key,) for key in work_keys],
        )
        conn.executemany(
            "INSERT INTO work_dois(doi, work_key) VALUES (?, ?) "
            "ON CONFLICT(doi) DO UPDATE SET work_key = excluded.work_key",
            [(doi, str(work_id)) for doi, work_id in doi_to_work.items()],
        )

        # Effective mapping after this import's upserts.
        effective = _load_doi_map(conn, [d for pair in edges for d in pair])
        for citing, cited in edges:
            if citing not in effective:
                raise InvalidRequest(
                    f"citation DOI {citing!r} has no work assignment; "
                    "import its provenance first"
                )
            if cited not in effective:
                raise InvalidRequest(
                    f"citation DOI {cited!r} has no work assignment; "
                    "import its provenance first"
                )

        inserted = 0
        for part in _chunks(edges):
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO citations(citing_doi, cited_doi) VALUES (?, ?)",
                part,
            )
            inserted += conn.total_changes - before

        internal = sum(
            1 for citing, cited in edges if effective[citing] == effective[cited]
        )

        _rebuild_index(conn)

        # A re-import may have moved the last DOI out of a work.
        conn.execute(
            "DELETE FROM works WHERE NOT EXISTS "
            "(SELECT 1 FROM work_dois WHERE work_dois.work_key = works.work_key)"
        )

        new_version = (get_version(conn) or 0) + 1
        set_version(conn, new_version)
        index_edges = conn.execute(
            "SELECT COUNT(*) AS n FROM citation_index"
        ).fetchone()["n"]

    return ImportResult(
        version=new_version,
        works=len(work_keys),
        dois=len(doi_to_work),
        citations=inserted,
        internal_citations_ignored=internal,
        duplicate_citations_ignored=raw_duplicates + (len(edges) - inserted),
        index_edges=index_edges,
    )


def _rebuild_index(conn: sqlite3.Connection) -> None:
    """Full rebuild -- used by import only."""
    conn.execute("DELETE FROM citation_index")
    conn.execute(
        """
        INSERT INTO citation_index(citing_work, cited_work, doi_count)
        SELECT citer.work_key, cited.work_key, COUNT(*)
        FROM citations c
        JOIN work_dois citer ON citer.doi = c.citing_doi
        JOIN work_dois cited ON cited.doi = c.cited_doi
        WHERE citer.work_key <> cited.work_key
        GROUP BY citer.work_key, cited.work_key
        """
    )


def _load_doi_map(conn: sqlite3.Connection, dois: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    unique = sorted(set(dois))
    for part in _chunks(unique):
        marks = ",".join("?" * len(part))
        for row in conn.execute(
            f"SELECT doi, work_key FROM work_dois WHERE doi IN ({marks})", part
        ):
            mapping[row["doi"]] = row["work_key"]
    return mapping


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


def apply_revision(conn: sqlite3.Connection, payload: RevisionIn) -> RevisionResult:
    """Correct the DOI -> work grouping and update the index incrementally.

    Raw citations are read only for the DOIs being regrouped (the only edges
    whose work-level grouping can change).  For each such citation the old
    work-level edge (pre-submission grouping) is decremented and the new edge
    (grouping after the whole submission) is incremented, so a citation whose
    two ends are both regrouped in this batch is counted once, under the new
    works.
    """
    assignments: dict[str, int] = {}
    for raw_doi, work_id in payload.assignments.items():
        doi = _norm(raw_doi)
        if not doi:
            raise InvalidRequest("DOI must not be blank")
        if doi in assignments and assignments[doi] != work_id:
            raise InvalidRequest(f"DOI {doi!r} assigned twice in one submission")
        assignments[doi] = work_id

    with write_transaction(conn):
        current = get_version(conn) or 0
        if payload.version != current:
            # Rejected submissions change nothing, including the version.
            raise StaleVersion(current)

        old_map = _load_doi_map(conn, assignments.keys())
        unknown = sorted(doi for doi in assignments if doi not in old_map)
        if unknown:
            raise InvalidRequest(
                "cannot regroup DOI(s) with no imported provenance: "
                + ", ".join(repr(doi) for doi in unknown)
                + "; import the DOI's ownership first"
            )

        moves = [
            _Move(doi=doi, old_work=old_map.get(doi), new_work=str(work_id))
            for doi, work_id in assignments.items()
            if old_map.get(doi) != str(work_id)
        ]

        if moves:
            conn.executemany(
                "INSERT OR IGNORE INTO works(work_key) VALUES (?)",
                [(move.new_work,) for move in moves],
            )
            conn.executemany(
                "INSERT INTO work_dois(doi, work_key) VALUES (?, ?) "
                "ON CONFLICT(doi) DO UPDATE SET work_key = excluded.work_key",
                [(move.doi, move.new_work) for move in moves],
            )

            deltas = _edge_deltas(conn, moves, {m.doi: m.old_work for m in moves})
            _apply_deltas(conn, deltas)

            # Works that no longer own a DOI are gone.
            conn.execute(
                "DELETE FROM works WHERE NOT EXISTS "
                "(SELECT 1 FROM work_dois WHERE work_dois.work_key = works.work_key)"
            )
        else:
            deltas = {}

        new_version = current + 1
        set_version(conn, new_version)

    added = sum(delta for delta in deltas.values() if delta > 0)
    removed = -sum(delta for delta in deltas.values() if delta < 0)
    affected = {m.old_work for m in moves if m.old_work} | {m.new_work for m in moves}
    return RevisionResult(
        version=new_version,
        dois_reassigned=len(moves),
        works_affected=len(affected),
        citations_added=added,
        citations_removed=removed,
        reindexed=bool(moves),
    )


def _edge_deltas(
    conn: sqlite3.Connection,
    moves: list[_Move],
    old_by_doi: dict[str, str | None],
) -> dict[tuple[str, str], int]:
    """Net index changes per work-level edge, from citations of moved DOIs.

    Must be called after ``work_dois`` has been updated, so the join yields the
    post-submission grouping.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS moved_dois(doi TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM moved_dois")
    conn.executemany(
        "INSERT OR IGNORE INTO moved_dois(doi) VALUES (?)", [(m.doi,) for m in moves]
    )

    rows = conn.execute(
        """
        SELECT c.citing_doi, c.cited_doi,
               citer.work_key AS citing_work, cited.work_key AS cited_work
        FROM citations c
        JOIN work_dois citer ON citer.doi = c.citing_doi
        JOIN work_dois cited ON cited.doi = c.cited_doi
        WHERE c.citing_doi IN (SELECT doi FROM moved_dois)
           OR c.cited_doi  IN (SELECT doi FROM moved_dois)
        """
    ).fetchall()

    deltas: dict[tuple[str, str], int] = {}

    def bump(edge: tuple[str, str] | None, by: int) -> None:
        if edge is None:
            return
        deltas[edge] = deltas.get(edge, 0) + by

    for row in rows:
        citing, cited = row["citing_doi"], row["cited_doi"]
        # Old grouping: moved DOIs take their pre-submission work; any other
        # DOI was not touched by this submission, so its current work is old.
        old_citing = old_by_doi.get(citing, row["citing_work"])
        old_cited = old_by_doi.get(cited, row["cited_work"])
        new_citing, new_cited = row["citing_work"], row["cited_work"]

        if old_citing is not None and old_cited is not None and old_citing != old_cited:
            bump((old_citing, old_cited), -1)
        if new_citing != new_cited:
            bump((new_citing, new_cited), 1)

    return {edge: delta for edge, delta in deltas.items() if delta != 0}


def _apply_deltas(conn: sqlite3.Connection, deltas: dict[tuple[str, str], int]) -> None:
    for (citing, cited), delta in deltas.items():
        row = conn.execute(
            "SELECT doi_count FROM citation_index "
            "WHERE citing_work = ? AND cited_work = ?",
            (citing, cited),
        ).fetchone()
        if row is None:
            if delta > 0:
                conn.execute(
                    "INSERT INTO citation_index(citing_work, cited_work, doi_count) "
                    "VALUES (?, ?, ?)",
                    (citing, cited, delta),
                )
            continue
        count = row["doi_count"] + delta
        if count < 0:
            raise RuntimeError(
                f"citation index underflow for {citing}->{cited}: {count}"
            )
        if count == 0:
            conn.execute(
                "DELETE FROM citation_index WHERE citing_work = ? AND cited_work = ?",
                (citing, cited),
            )
        else:
            conn.execute(
                "UPDATE citation_index SET doi_count = ? "
                "WHERE citing_work = ? AND cited_work = ?",
                (count, citing, cited),
            )


# ---------------------------------------------------------------------------
# Lookup (index only)
# ---------------------------------------------------------------------------


def _work_dois(conn: sqlite3.Connection, work_id: int) -> list[str]:
    return [
        row["doi"]
        for row in conn.execute(
            "SELECT doi FROM work_dois WHERE work_key = ? ORDER BY doi", (str(work_id),)
        )
    ]


def lookup(conn: sqlite3.Connection, work_id: int, direction: str) -> CitationResponse:
    """Read one direction of the citation index for ``work_id``.

    The caller must hold a read transaction so the index rows and the reported
    version come from the same committed snapshot.
    """
    dois = _work_dois(conn, work_id)
    if not dois:
        raise NotFound(str(work_id))

    if direction == "references":
        column, other, label = "citing_work", "cited_work", "references"
    else:
        column, other, label = "cited_work", "citing_work", "cited_by"

    rows = conn.execute(
        f"SELECT {other} AS other_work, doi_count FROM citation_index "
        f"WHERE {column} = ? ORDER BY doi_count DESC, other_work",
        (str(work_id),),
    ).fetchall()

    results = [
        CitationEdge(
            work=WorkRef(
                id=int(row["other_work"]),
                dois=_work_dois(conn, int(row["other_work"])),
            ),
            doi_count=row["doi_count"],
        )
        for row in rows
    ]

    return CitationResponse(
        version=get_version(conn) or 0,
        direction=label,
        work=WorkRef(id=work_id, dois=dois),
        total_works=len(results),
        total_doi_citations=sum(edge.doi_count for edge in results),
        results=results,
    )


def work_detail(conn: sqlite3.Connection, work_id: int) -> WorkDetail:
    dois = _work_dois(conn, work_id)
    if not dois:
        raise NotFound(str(work_id))
    return WorkDetail(id=work_id, dois=dois)


def current_version(conn: sqlite3.Connection) -> int:
    return get_version(conn) or 0
