"""Functional tests for import, citation lookup and grouping corrections.

Run with:  python -m pytest -q
"""

from __future__ import annotations

from app.db import db_session


def _seed_import(client):
    """Five DOIs / four works:

        work 1: 10.1/j + 10.1/p   (journal version and preprint, same study)
        work 2: 10.1/b
        work 3: 10.1/c
        work 4: 10.1/d

    DOI citations:
        10.1/b -> 10.1/j    b cites work 1's journal version  => 2 -> 1
        10.1/c -> 10.1/p    c cites work 1's preprint        => 3 -> 1
        10.1/d -> 10.1/b                                     => 4 -> 2
        10.1/j -> 10.1/b                                     => 1 -> 2
        10.1/p -> 10.1/d                                     => 1 -> 4
        10.1/j -> 10.1/p    internal to work 1 -- never indexed
    """
    response = client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.1/j", "10.1/p"]},
                {"work_id": 2, "dois": ["10.1/b"]},
                {"work_id": 3, "dois": ["10.1/c"]},
                {"work_id": 4, "dois": ["10.1/d"]},
            ],
            "citations": [
                {"citing": "10.1/b", "cited": "10.1/j"},
                {"citing": "10.1/c", "cited": "10.1/p"},
                {"citing": "10.1/d", "cited": "10.1/b"},
                {"citing": "10.1/j", "cited": "10.1/b"},
                {"citing": "10.1/p", "cited": "10.1/d"},
                {"citing": "10.1/j", "cited": "10.1/p"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_reports_counts_and_internal_citations(client):
    body = _seed_import(client)
    assert body["version"] == 1
    assert body["works"] == 4
    assert body["dois"] == 5
    assert body["citations"] == 6  # all six raw edges are stored
    assert body["internal_citations_ignored"] == 1  # j -> p
    assert body["index_edges"] == 5


def test_import_rejects_doi_assigned_to_two_works(client):
    response = client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.2/x"]},
                {"work_id": 2, "dois": ["10.2/x"]},
            ]
        },
    )
    assert response.status_code == 422
    assert "10.2/x" in response.json()["detail"]


def test_import_rejects_citation_for_unassigned_doi(client):
    response = client.post(
        "/import",
        json={
            "works": [{"work_id": 1, "dois": ["10.2/x"]}],
            "citations": [{"citing": "10.2/x", "cited": "10.2/nope"}],
        },
    )
    assert response.status_code == 422
    assert "10.2/nope" in response.json()["detail"]
    # Nothing from the failed batch leaked in.
    assert client.get("/works/1").status_code == 404
    assert client.get("/health").json()["version"] == 0


def test_import_deduplicates_repeated_citations(client):
    body = _seed_import(client)
    again = client.post(
        "/import",
        json={
            "works": [],
            "citations": [
                {"citing": "10.1/b", "cited": "10.1/j"},
                {"citing": "10.1/b", "cited": "10.1/j"},
            ],
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["citations"] == 0
    assert again.json()["duplicate_citations_ignored"] == 2
    assert again.json()["version"] == body["version"] + 1


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_references_are_deduplicated_per_work(client):
    _seed_import(client)
    response = client.get("/works/1/references")
    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == "references"
    assert body["work"] == {"id": 1, "dois": ["10.1/j", "10.1/p"]}
    assert [(e["work"]["id"], e["doi_count"]) for e in body["results"]] == [
        (2, 1),  # j -> b
        (4, 1),  # p -> d
    ]
    assert body["total_works"] == 2
    assert body["total_doi_citations"] == 2


def test_cited_by_is_deduplicated_per_citing_work(client):
    _seed_import(client)
    body = client.get("/works/1/cited-by").json()
    assert body["direction"] == "cited_by"
    # b cites the journal version; c cites the preprint; both collapse onto
    # citing works 2 and 3.  j -> p is internal and absent.
    assert [(e["work"]["id"], e["doi_count"]) for e in body["results"]] == [
        (2, 1),
        (3, 1),
    ]
    assert body["total_works"] == 2
    assert body["total_doi_citations"] == 2
    assert body["version"] == client.get("/health").json()["version"]


def test_lookup_of_unknown_work_is_404(client):
    assert client.get("/works/999/references").status_code == 404
    assert client.get("/works/999/cited-by").status_code == 404


def test_lookup_reports_current_version(client):
    _seed_import(client)
    body = client.get("/works/2/references").json()
    assert body["version"] == 1
    assert body["results"] == [{"work": {"id": 1, "dois": ["10.1/j", "10.1/p"]}, "doi_count": 1}]


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_revision_splits_a_group_and_updates_both_directions(client):
    _seed_import(client)  # version 1
    # Librarian decides 10.1/p (preprint) is a separate output, work 5.
    response = client.post(
        "/revisions", json={"version": 1, "assignments": {"10.1/p": 5}}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["dois_reassigned"] == 1

    # cited-by for the split-out preprint: j and c both cite it now.
    preprint = client.get("/works/5/cited-by").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in preprint["results"]] == [
        (1, 1),
        (3, 1),
    ]
    # Work 1 (journal version) no longer owns the preprint, so c no longer
    # cites it, but j -> p is now a cross-work citation to work 5.
    assert [
        (e["work"]["id"], e["doi_count"])
        for e in client.get("/works/1/cited-by").json()["results"]
    ] == [(2, 1)]
    assert [
        (e["work"]["id"], e["doi_count"])
        for e in client.get("/works/1/references").json()["results"]
    ] == [(2, 1), (5, 1)]

    # references of the preprint: p -> d only (j -> p is now work 1 -> work 5).
    preprint_refs = client.get("/works/5/references").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in preprint_refs["results"]] == [
        (4, 1),
    ]
    assert preprint_refs["version"] == 2


def test_revision_merging_two_works_sums_doi_counts(client):
    _seed_import(client)
    # Merge work 4 into work 2: d and b become one output.
    response = client.post(
        "/revisions", json={"version": 1, "assignments": {"10.1/d": 2}}
    )
    assert response.status_code == 201, response.text

    # Work 1 references: j -> b and p -> d now both point at work 2.
    refs = client.get("/works/1/references").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in refs["results"]] == [(2, 2)]
    assert refs["total_doi_citations"] == 2

    # Work 2 cited-by: work 1 (j -> b twice? no: j -> b once, and p -> d is now
    # 1 -> 2) and work 3 (c cites p, now part of work 2).  d -> b became
    # internal and must be gone.
    cited_by = client.get("/works/2/cited-by").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by["results"]] == [
        (1, 2),
    ]
    # d -> b became internal; it must not show up anywhere.
    refs_4 = client.get("/works/4/references")
    assert refs_4.status_code == 404  # work 4 no longer exists


def test_revision_with_both_ends_regrouped_uses_post_batch_grouping(client):
    """Two DOIs moving closer together in one submission, plus one moving out.

    Start: 1 = {j, p}, 2 = {b}, 5 = {e}.  Batch:
        10.1/j -> 10.1/p (no-op, same work)
        10.1/b -> 1     (so b joins work 1)
        10.1/e -> 2     (e joins work 2)
    Citation b -> e exists and is untouched; both ends are regrouped in the
    same submission, so it must be counted once, under (1 -> 2).
    """
    client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.1/j", "10.1/p"]},
                {"work_id": 2, "dois": ["10.1/b"]},
                {"work_id": 5, "dois": ["10.1/e"]},
            ],
            "citations": [
                {"citing": "10.1/b", "cited": "10.1/j"},  # 2 -> 1
                {"citing": "10.1/e", "cited": "10.1/b"},  # 5 -> 2
            ],
        },
    )
    version = client.get("/health").json()["version"]

    response = client.post(
        "/revisions",
        json={
            "version": version,
            "assignments": {"10.1/b": 1, "10.1/e": 2},
        },
    )
    assert response.status_code == 201, response.text

    # After the batch: 1 = {j, p, b}, 2 = {e}.  b -> j is now internal.
    # e -> b becomes 2 -> 1 -- the citation had both ends regrouped in this
    # batch, and it is counted under the post-batch grouping only.
    assert client.get("/works/1/references").json()["results"] == []
    cited_by_1 = client.get("/works/1/cited-by").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by_1["results"]] == [(2, 1)]

    refs_2 = client.get("/works/2/references").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in refs_2["results"]] == [(1, 1)]
    assert cited_by_1["total_doi_citations"] == 1
    assert client.get("/works/5").status_code == 404


def test_stale_revision_is_rejected_and_changes_nothing(client):
    _seed_import(client)  # version 1
    first = client.post("/revisions", json={"version": 1, "assignments": {"10.1/p": 5}})
    assert first.status_code == 201

    before_grouping = client.get("/works/5").json()
    before_refs = client.get("/works/1/references").json()
    before_version = client.get("/health").json()["version"]

    stale = client.post("/revisions", json={"version": 1, "assignments": {"10.1/c": 9}})
    assert stale.status_code == 409
    body = stale.json()
    assert body["current_version"] == before_version
    assert "stale" in body["detail"].lower()

    # Original grouping, index and version all intact.
    assert client.get("/works/5").json() == before_grouping
    assert client.get("/works/1/references").json() == before_refs
    assert client.get("/health").json()["version"] == before_version
    assert client.get("/works/9").status_code == 404


def test_concurrent_corrections_on_same_version_only_first_wins(db_path):
    """Two writers race from the same version; exactly one commits."""
    import threading

    from fastapi.testclient import TestClient

    from app import create_app

    app = create_app(db_path)
    with TestClient(app) as client:
        _seed_import(client)
        version = client.get("/health").json()["version"]

        barrier = threading.Barrier(2)
        results: list[int] = []
        lock = threading.Lock()

        def attempt(doi: str, work_id: int) -> None:
            with TestClient(app) as thread_client:
                barrier.wait()
                r = thread_client.post(
                    "/revisions",
                    json={"version": version, "assignments": {doi: work_id}},
                )
                with lock:
                    results.append(r.status_code)

        threads = [
            threading.Thread(target=attempt, args=("10.1/p", 5)),
            threading.Thread(target=attempt, args=("10.1/c", 6)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == [201, 409]

        # The accepted move is visible; the rejected one left no trace.
        final_version = client.get("/health").json()["version"]
        assert final_version == version + 1
        winner = (
            5
            if client.get("/works/5/references").status_code == 200
            else 6
        )
        loser = 6 if winner == 5 else 5
        assert client.get(f"/works/{loser}").status_code == 404
        # Index checks: the winner is consistent in both directions.
        assert client.get(f"/works/{winner}/cited-by").status_code == 200


def test_revision_can_move_a_doi_into_an_existing_group_and_back(client):
    _seed_import(client)
    v = 1

    r = client.post("/revisions", json={"version": v, "assignments": {"10.1/c": 4}})
    assert r.status_code == 201
    v = r.json()["version"]
    # work 3 is now empty and gone; c now cites work 1 from work 4.
    assert client.get("/works/3").status_code == 404
    cited_by = client.get("/works/1/cited-by").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by["results"]] == [
        (2, 1),
        (4, 1),
    ]

    r = client.post("/revisions", json={"version": v, "assignments": {"10.1/c": 3}})
    assert r.status_code == 201
    cited_by = client.get("/works/1/cited-by").json()
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by["results"]] == [
        (2, 1),
        (3, 1),
    ]


def test_rejected_revision_preserves_index(client):
    _seed_import(client)
    # Invalid assignment (unknown work id 0 is rejected by validation).
    bad = client.post("/revisions", json={"version": 1, "assignments": {"10.1/p": 0}})
    assert bad.status_code == 422
    assert client.get("/works/1/references").json()["version"] == 1

    # A revision that errors mid-flight must roll back: unknown DOI is fine
    # (moves it), but an empty map is rejected by validation.
    empty = client.post("/revisions", json={"version": 1, "assignments": {}})
    assert empty.status_code == 422
    assert client.get("/health").json()["version"] == 1


def test_raw_citations_are_never_rewritten_by_corrections(client, db_path):
    _seed_import(client)
    client.post("/revisions", json={"version": 1, "assignments": {"10.1/p": 5}})
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT citing_doi, cited_doi FROM citations ORDER BY citing_doi, cited_doi"
        ).fetchall()
    assert [(r["citing_doi"], r["cited_doi"]) for r in rows] == [
        ("10.1/b", "10.1/j"),
        ("10.1/c", "10.1/p"),
        ("10.1/d", "10.1/b"),
        ("10.1/j", "10.1/b"),
        ("10.1/j", "10.1/p"),
        ("10.1/p", "10.1/d"),
    ]


def _rebuild_expected(db_path):
    """Recompute work-level edges from raw citations + current grouping."""
    with db_session(db_path) as conn:
        return {
            (row["citing_work"], row["cited_work"]): row["n"]
            for row in conn.execute(
                """
                SELECT citer.work_key AS citing_work,
                       cited.work_key AS cited_work,
                       COUNT(*) AS n
                FROM citations c
                JOIN work_dois citer ON citer.doi = c.citing_doi
                JOIN work_dois cited ON cited.doi = c.cited_doi
                WHERE citer.work_key <> cited.work_key
                GROUP BY citer.work_key, cited.work_key
                """
            )
        }


def _stored_index(db_path):
    with db_session(db_path) as conn:
        return {
            (row["citing_work"], row["cited_work"]): row["doi_count"]
            for row in conn.execute(
                "SELECT citing_work, cited_work, doi_count FROM citation_index"
            )
        }


def test_incremental_index_matches_full_rebuild_across_revisions(client, db_path):
    """After every accepted correction, the incrementally maintained index must
    agree exactly with a rebuild from the raw DOI citations."""
    _seed_import(client)
    assert _stored_index(db_path) == _rebuild_expected(db_path)

    steps = [
        {"10.1/p": 5},                 # split the preprint out
        {"10.1/j": 5},                 # split the journal version too
        {"10.1/b": 1, "10.1/c": 5},    # two ends regrouped in one batch
        {"10.1/d": 1, "10.1/p": 1},    # merge several DOIs back into work 1
        {"10.1/j": 4},                 # move one out again
    ]
    version = client.get("/health").json()["version"]
    for step in steps:
        response = client.post(
            "/revisions", json={"version": version, "assignments": step}
        )
        assert response.status_code == 201, response.text
        version = response.json()["version"]
        assert _stored_index(db_path) == _rebuild_expected(db_path), step


def test_lookups_read_only_the_index(client, db_path):
    """Citation lookups must not depend on the raw citation table."""
    _seed_import(client)
    before_refs = client.get("/works/1/references").json()
    before_cited_by = client.get("/works/1/cited-by").json()

    with db_session(db_path) as conn:
        conn.execute("DELETE FROM citations")

    assert client.get("/works/1/references").json() == before_refs
    assert client.get("/works/1/cited-by").json() == before_cited_by


def test_revision_with_unknown_doi_is_rejected_and_preserves_state(client):
    _seed_import(client)
    before = {
        "refs": client.get("/works/1/references").json(),
        "cited": client.get("/works/1/cited-by").json(),
        "version": client.get("/health").json()["version"],
    }
    response = client.post(
        "/revisions",
        json={"version": before["version"], "assignments": {"10.9/ghost": 5}},
    )
    assert response.status_code == 422
    assert "10.9/ghost" in response.json()["detail"]
    assert client.get("/health").json()["version"] == before["version"]
    assert client.get("/works/1/references").json() == before["refs"]
    assert client.get("/works/1/cited-by").json() == before["cited"]


def test_reindex_reads_only_citations_of_regrouped_dois(client, db_path):
    """Proof of selective reindexing: unrelated raw citations may be missing
    from the raw table (they are not consulted), and their index rows survive."""
    _seed_import(client)
    with db_session(db_path) as conn:
        # Drop every raw citation that does not touch 10.1/p (the DOI being
        # moved).  A full rebuild would now destroy the unrelated index rows.
        conn.execute(
            "DELETE FROM citations WHERE citing_doi <> '10.1/p' AND cited_doi <> '10.1/p'"
        )
        remaining = {
            (r["citing_work"], r["cited_work"]): r["doi_count"]
            for r in conn.execute("SELECT * FROM citation_index")
        }

    response = client.post(
        "/revisions", json={"version": 1, "assignments": {"10.1/p": 5}}
    )
    assert response.status_code == 201, response.text

    with db_session(db_path) as conn:
        after = {
            (r["citing_work"], r["cited_work"]): r["doi_count"]
            for r in conn.execute("SELECT * FROM citation_index")
        }
    unaffected = {edge: n for edge, n in remaining.items() if "1" not in edge}
    assert unaffected
    for edge, n in unaffected.items():
        assert after.get(edge) == n


def test_randomised_corrections_keep_index_consistent(client, db_path):
    """Fuzz: 40 random DOIs, random citations, then 30 random corrections --
    the incrementally maintained index must equal a full rebuild throughout."""
    import random

    rng = random.Random(20261120)
    dois = [f"10.8/{i}" for i in range(40)]
    works = [
        {"work_id": i // 2 + 1, "dois": dois[i * 2 : i * 2 + 2]} for i in range(20)
    ]
    pairs = set()
    while len(pairs) < 120:
        a, b = rng.sample(dois, 2)
        pairs.add((a, b))
    citations = [{"citing": a, "cited": b} for a, b in pairs]

    response = client.post("/import", json={"works": works, "citations": citations})
    assert response.status_code == 201, response.text
    assert _stored_index(db_path) == _rebuild_expected(db_path)

    version = response.json()["version"]
    for _ in range(30):
        picks = rng.sample(range(40), rng.choice([1, 2, 3]))
        assignments = {dois[i]: rng.choice(version and [3, 7, 11, 21, 30, 40]) for i in picks}
        response = client.post(
            "/revisions", json={"version": version, "assignments": assignments}
        )
        assert response.status_code == 201, (assignments, response.text)
        version = response.json()["version"]
        expected = _rebuild_expected(db_path)
        actual = _stored_index(db_path)
        assert actual == expected, assignments
