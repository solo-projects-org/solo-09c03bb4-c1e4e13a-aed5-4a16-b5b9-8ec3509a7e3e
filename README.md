# Research Output Citation Service

FastAPI service for a university library. It imports DOI-level provenance
(which DOIs belong to which research output) and directed DOI-level citations
into SQLite in one batch, then answers "what does this output cite?" and "who
cites this output?" from a materialised per-output index.

Preprints and journal versions of the same study are modelled as one **work**
holding several DOIs.

## Run

```bash
docker compose up --build
```

* Swagger UI: <http://localhost:8000/docs>
* ReDoc: <http://localhost:8000/redoc>
* Data persists in the `citations-data` volume (`/data/citations.db`).
* Local (no Docker): `pip install -r requirements.txt && uvicorn app.api:app`

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/import` | Atomically import works (DOI groups) + directed DOI citations |
| `POST` | `/revisions` | Librarian correction of the grouping, conditional on version |
| `GET` | `/works/{id}/references` | Works cited by this work (outgoing) |
| `GET` | `/works/{id}/cited-by` | Works citing this work (incoming) |
| `GET` | `/works/{id}` | Work and its DOIs |
| `GET` | `/health` | Liveness + current version |

### Import

```json
{
  "works": [
    {"work_id": 1, "dois": ["10.1101/2024.01.01.123456", "10.1000/journal.2024.1"]},
    {"work_id": 2, "dois": ["10.1000/review.2023.7"]}
  ],
  "citations": [
    {"citing": "10.1000/review.2023.7", "cited": "10.1101/2024.01.01.123456"}
  ]
}
```

A batch is all-or-nothing. Citations whose two DOIs resolve to the same work
are internal: stored in the raw table but never indexed, and reported in
`internal_citations_ignored`. Repeated citation records are collapsed
(`duplicate_citations_ignored`). Every accepted mutation bumps the dataset
version.

### Correction

```json
{"version": 1, "assignments": {"10.1101/2024.01.01.123456": 4}}
```

`version` is the version the librarian was looking at. The correction is
applied only if it is still current. When two corrections race from the same
version, the one whose transaction commits first wins; the other gets `409`
with `current_version`, and neither the grouping nor the index changes.

Reindexing reads only the raw citations incident to the regrouped DOIs. Each
such citation decrements its old work-level edge and increments its new one,
where "new" is the grouping **after the whole batch**, so a citation whose two
ends are both regrouped in one submission is counted once, under its
post-submission works. Works left without DOIs are removed.

### Lookup

Answers come from `citation_index` only (no join back to raw citations), and
each response reports the version it was read at together with the work's DOIs.
Each edge is deduplicated per work and carries `doi_count` — the number of
distinct DOI-level citation records behind it.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Design notes

* SQLite in WAL mode; writers use `BEGIN IMMEDIATE`, so the version check and
  the writes are one serialised unit. Reads use one snapshot transaction.
* Raw `citations` are immutable; corrections only move DOIs between works and
  update `citation_index`.
* Tables: `meta`, `works`, `work_dois`, `citations`, `citation_index`
  (see `app/db.py`).
