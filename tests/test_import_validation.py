"""Import validation: DB really stays untouched on rejected batches."""
from __future__ import annotations

from app.db import db_session


def test_citation_validation_failure_rolls_back_grouping(client, db_path):
    response = client.post(
        "/import",
        json={
            "works": [{"work_id": 7, "dois": ["10.9/new"]}],
            "citations": [{"citing": "10.9/new", "cited": "10.9/ghost"}],
        },
    )
    assert response.status_code == 422
    assert "10.9/ghost" in response.json()["detail"]
    assert client.get("/works/7").status_code == 404
    with db_session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM work_dois").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM citations").fetchone()["n"] == 0


def test_duplicate_doi_within_one_work_is_idempotent(client):
    response = client.post(
        "/import",
        json={"works": [{"work_id": 1, "dois": ["10.3/a", "10.3/a"]}]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["dois"] == 1


def test_citation_may_target_doi_imported_earlier(client):
    client.post("/import", json={"works": [{"work_id": 1, "dois": ["10.4/a"]}]})
    latest = client.get("/health").json()["version"]
    response = client.post(
        "/import",
        json={
            "works": [{"work_id": 2, "dois": ["10.4/b"]}],
            "citations": [{"citing": "10.4/b", "cited": "10.4/a"}],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["version"] == latest + 1
    assert [
        (e["work"]["id"], e["doi_count"])
        for e in client.get("/works/1/cited-by").json()["results"]
    ] == [(2, 1)]


def test_reimport_moves_doi_between_work_ids(client):
    client.post("/import", json={"works": [{"work_id": 1, "dois": ["10.5/a"]}]})
    client.post("/import", json={"works": [{"work_id": 2, "dois": ["10.5/a"]}]})
    assert client.get("/works/1").status_code == 404
    assert client.get("/works/2").json()["dois"] == ["10.5/a"]
