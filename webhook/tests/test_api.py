"""
Tests for webhook route management API.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from webhook.server import app, init_app

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"


@pytest.fixture(autouse=True)
def setup_app():
    init_app(EXAMPLE_CONFIG)
    yield


client = TestClient(app)


class TestGetRoutes:

    def test_list_routes(self):
        r = client.get("/routes")
        assert r.status_code == 200
        routes = r.json()["routes"]
        assert len(routes) >= 1
        assert any(rt["name"] == "default" for rt in routes)

    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestPatchRoute:

    def test_update_sample(self):
        r = client.patch("/api/routes/default", json={"sample": 50})
        assert r.status_code == 200
        assert r.json()["route"]["sample"] == 50

    def test_update_agent(self):
        r = client.patch("/api/routes/default", json={"agent": "dg2"})
        assert r.status_code == 200
        assert r.json()["route"]["agent"] == "dg2"

    def test_update_not_found(self):
        r = client.patch("/api/routes/nonexistent", json={"sample": 10})
        assert r.status_code == 404


class TestCreateRoute:

    def test_create(self):
        r = client.post("/api/routes", json={
            "name": "test-route",
            "when": "project == 'TEST'",
            "agent": "dg1",
            "sample": 10,
        })
        assert r.status_code == 201
        assert r.json()["route"]["name"] == "test-route"
        # Verify it appears in list
        routes = client.get("/routes").json()["routes"]
        assert any(rt["name"] == "test-route" for rt in routes)

    def test_create_duplicate(self):
        client.post("/api/routes", json={"name": "dup-test", "agent": "dg1"})
        r = client.post("/api/routes", json={"name": "dup-test", "agent": "dg1"})
        assert r.status_code == 409

    def test_create_no_name(self):
        r = client.post("/api/routes", json={"agent": "dg1"})
        assert r.status_code == 400


class TestDeleteRoute:

    def test_delete(self):
        client.post("/api/routes", json={"name": "to-delete", "agent": "dg1"})
        r = client.delete("/api/routes/to-delete")
        assert r.status_code == 200
        routes = client.get("/routes").json()["routes"]
        assert not any(rt["name"] == "to-delete" for rt in routes)

    def test_delete_not_found(self):
        r = client.delete("/api/routes/nonexistent")
        assert r.status_code == 404
