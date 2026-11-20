from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.db import init_db


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client
