from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import db_session, default_db_path, init_db, read_transaction
from .models import (
    CitationResponse,
    ImportRequest,
    ImportResult,
    RevisionIn,
    RevisionResult,
    WorkDetail,
)


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db_path or default_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(path)
        app.state.db_path = path
        yield

    app = FastAPI(
        title="Research Output Citation Service",
        description=(
            "Import DOI provenance (which DOIs belong to which research output) "
            "and directed DOI-level citations, then query citations per output in "
            "both directions.\n\n"
            "A research output may own several DOIs -- typically the preprint and "
            "the journal version of the same study. Citations between DOIs of the "
            "same output are internal and are not counted. Every output-level "
            "citation carries the number of distinct DOI citations behind it.\n\n"
            "Librarians correct the grouping by posting the current version plus a "
            "batch of DOI -> output assignments. A correction is accepted only if "
            "the submitted version is still current (concurrent corrections on the "
            "same version: only the first to commit wins; the loser gets 409 and "
            "changes nothing). Citation lookups read the materialised index and "
            "report the version they were read at."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(service.NotFound)
    async def _not_found(_: Request, exc: service.NotFound):
        return JSONResponse(status_code=404, content={"detail": "work not found"})

    @app.exception_handler(service.StaleVersion)
    async def _stale(_: Request, exc: service.StaleVersion):
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "stale version: another correction or import has already "
                    "advanced the dataset; re-read the current version and "
                    "resubmit"
                ),
                "current_version": exc.current,
            },
        )

    @app.exception_handler(service.InvalidRequest)
    async def _invalid(_: Request, exc: service.InvalidRequest):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy(_: Request, exc: sqlite3.OperationalError):
        # e.g. write-lock timeout while another writer holds the database.
        return JSONResponse(
            status_code=503,
            content={
                "detail": "database busy; nothing was changed, please retry",
                "error": str(exc),
            },
        )

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        with db_session(request.app.state.db_path) as conn:
            yield conn

    @app.get("/health", tags=["ops"], summary="Liveness plus current version")
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        return {"status": "ok", "version": service.current_version(conn)}

    @app.post(
        "/import",
        response_model=ImportResult,
        status_code=201,
        tags=["import"],
        summary="Import DOI provenance and directed DOI citations",
    )
    def import_data(
        payload: ImportRequest, conn: sqlite3.Connection = Depends(get_conn)
    ):
        """Import works (each with its DOIs) and DOI-level citations atomically.

        * A DOI belongs to exactly one work; a work may hold several DOIs.
        * A citation between two DOIs of the same work is internal, is not
          indexed, and is reported in ``internal_citations_ignored``.
        * Duplicate citation records are collapsed and counted in
          ``duplicate_citations_ignored``.
        * Any validation failure rolls the whole batch back.
        """
        return service.import_batch(conn, payload)

    @app.post(
        "/revisions",
        response_model=RevisionResult,
        status_code=201,
        tags=["corrections"],
        summary="Correct the DOI -> work grouping (conditional on version)",
    )
    def revise(payload: RevisionIn, conn: sqlite3.Connection = Depends(get_conn)):
        """Apply a librarian's DOI -> work regroupings.

        ``version`` is the version the librarian was looking at. The submission
        is applied only if it is still current; otherwise 409 is returned and
        the grouping and index are untouched. The index is updated from the raw
        citations of the regrouped DOIs only, evaluated under the grouping
        after the whole batch has been applied.
        """
        return service.apply_revision(conn, payload)

    def lookup(
        conn: sqlite3.Connection, work_id: int, direction: str
    ) -> CitationResponse:
        # One read snapshot so the index rows and the reported version agree.
        with read_transaction(conn):
            return service.lookup(conn, work_id, direction)

    @app.get(
        "/works/{work_id}/references",
        response_model=CitationResponse,
        tags=["lookup"],
        summary="Works cited by this work (outgoing, from the index)",
    )
    def references(work_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Deduplicated per cited work; ``doi_count`` is the number of distinct
        DOI-level citation records behind that work-level edge."""
        return lookup(conn, work_id, "references")

    @app.get(
        "/works/{work_id}/cited-by",
        response_model=CitationResponse,
        tags=["lookup"],
        summary="Works citing this work (incoming, from the index)",
    )
    def cited_by(work_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Deduplicated per citing work; ``doi_count`` is the number of distinct
        DOI-level citation records behind that work-level edge."""
        return lookup(conn, work_id, "cited_by")

    @app.get(
        "/works/{work_id}",
        response_model=WorkDetail,
        tags=["lookup"],
        summary="Get a work and its DOIs",
    )
    def work(work_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        return service.work_detail(conn, work_id)

    return app


app = create_app()
