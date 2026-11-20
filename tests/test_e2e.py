"""End-to-end: Docker-style launch of the ASGI app over TCP, Swagger reachable,
import -> correct -> lookup round trip, plus a cross-process writer race."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.load(r)


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    env = dict(os.environ, DB_PATH=str(tmp_path / "e2e.db"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api:app", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if _get(base, "/health")[0] == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start: " + proc.stdout.read().decode())
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_swagger_and_round_trip(server):
    with urllib.request.urlopen(server + "/docs", timeout=10) as r:
        assert r.status == 200
        assert b"swagger-ui" in r.read().lower()

    status, body = _post(
        server,
        "/import",
        {
            "works": [
                {"work_id": 1, "dois": ["10.1/journal", "10.1/preprint"]},
                {"work_id": 2, "dois": ["10.1/review"]},
                {"work_id": 3, "dois": ["10.1/method"]},
            ],
            "citations": [
                {"citing": "10.1/review", "cited": "10.1/preprint"},
                {"citing": "10.1/review", "cited": "10.1/journal"},
                {"citing": "10.1/method", "cited": "10.1/review"},
                {"citing": "10.1/journal", "cited": "10.1/preprint"},
            ],
        },
    )
    assert status == 201, body
    assert body["internal_citations_ignored"] == 1
    version = body["version"]

    # Two DOI citations from the same citing work collapse into one edge.
    status, cited_by = _get(server, "/works/1/cited-by")
    assert status == 200
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by["results"]] == [(2, 2)]
    assert cited_by["version"] == version

    # Split the preprint out; two of the citing work's two DOI links follow the
    # preprint, one stays with the journal version.
    status, rev = _post(
        server, "/revisions", {"version": version, "assignments": {"10.1/preprint": 9}}
    )
    assert status == 201, rev
    version = rev["version"]

    _, cited_by = _get(server, "/works/1/cited-by")
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by["results"]] == [(2, 1)]
    _, cited_by_9 = _get(server, "/works/9/cited-by")
    # Work 2's review cites 10.1/preprint, and the journal DOI now cites the
    # preprint across works.
    assert [(e["work"]["id"], e["doi_count"]) for e in cited_by_9["results"]] == [
        (1, 1),
        (2, 1),
    ]
    assert cited_by_9["version"] == version

    # Stale correction is refused.
    status, stale = _post(
        server, "/revisions", {"version": version - 1, "assignments": {"10.1/review": 9}}
    )
    assert status == 409
    assert stale["current_version"] == version


def test_concurrent_corrections_over_tcp(server):
    status, body = _post(
        server,
        "/import",
        {
            "works": [{"work_id": w, "dois": [f"10.7/{w}"]} for w in range(1, 6)],
        },
    )
    assert status == 201
    version = body["version"]

    barrier = threading.Barrier(5)
    outcomes: list[int] = []
    lock = threading.Lock()

    def attempt(n: int) -> None:
        barrier.wait()
        status, _ = _post(
            server,
            "/revisions",
            {"version": version, "assignments": {f"10.7/{n}": 100 + n}},
        )
        with lock:
            outcomes.append(status)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(201) == 1, outcomes
    assert outcomes.count(409) == 4, outcomes
    _, health = _get(server, "/health")
    assert health["version"] == version + 1
