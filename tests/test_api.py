import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = {
    "demands": [{"id": "D1", "material": "AL", "length": 1000, "quantity": 2}],
    "new_stock": [{"id": "N1", "material": "AL", "length": 6000, "price": 1000, "available": 2}],
    "remnants": [],
    "kerf": 5,
    "tail_threshold": 300,
    "budget_ms": 5000,
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_optimize_ok():
    r = client.post("/optimize", json=BASE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "OPTIMAL"
    assert body["proven_optimal"] is True
    assert body["metrics"]["total_cost"] == 1000
    for lay in body["layouts"]:
        assert sum(c["length"] for c in lay["cuts"]) + lay["kerf_total"] + lay["tail"] == lay["bar_length"]


def test_sample_request():
    with open("examples/sample_request.json") as f:
        payload = json.load(f)
    r = client.post("/optimize", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "OPTIMAL"
    total_parts = sum(d["quantity"] for d in payload["demands"])
    assert sum(len(l["cuts"]) for l in body["layouts"]) == total_parts


@pytest.mark.parametrize("field,value", [
    ("kerf", True),          # bool rejected
    ("kerf", -1),            # negative rejected
    ("kerf", 1.5),           # non-integer rejected
    ("budget_ms", 0),
    ("tail_threshold", -5),
])
def test_invalid_scalars(field, value):
    payload = dict(BASE, **{field: value})
    assert client.post("/optimize", json=payload).status_code == 422


def test_bool_quantity_rejected():
    payload = dict(BASE)
    payload["demands"] = [{"id": "D1", "material": "AL", "length": 1000, "quantity": True}]
    assert client.post("/optimize", json=payload).status_code == 422


def test_zero_quantity_rejected():
    payload = dict(BASE)
    payload["demands"] = [{"id": "D1", "material": "AL", "length": 1000, "quantity": 0}]
    assert client.post("/optimize", json=payload).status_code == 422


def test_duplicate_ids_rejected():
    payload = dict(BASE)
    payload["demands"] = [
        {"id": "D1", "material": "AL", "length": 1000, "quantity": 1},
        {"id": "D1", "material": "SS", "length": 500, "quantity": 1},
    ]
    assert client.post("/optimize", json=payload).status_code == 422
    payload2 = dict(BASE)
    payload2["remnants"] = [
        {"id": "R1", "material": "AL", "length": 900},
        {"id": "R1", "material": "AL", "length": 800},
    ]
    assert client.post("/optimize", json=payload2).status_code == 422


def test_limits():
    payload = dict(BASE)
    payload["demands"] = [{"id": "D1", "material": "AL", "length": 10, "quantity": 61}]
    assert client.post("/optimize", json=payload).status_code == 422
    payload = dict(BASE)
    payload["new_stock"] = [{"id": f"N{i}", "material": "AL", "length": 6000,
                             "price": 1, "available": 1} for i in range(25)]
    assert client.post("/optimize", json=payload).status_code == 422


def test_infeasible_no_partial_layout():
    payload = dict(BASE)
    payload["demands"] = [{"id": "D1", "material": "AL", "length": 9000, "quantity": 1}]
    r = client.post("/optimize", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "INFEASIBLE"
    assert body["layouts"] == []
    assert body["metrics"] is None


def test_health_not_blocked_by_solve():
    slow = dict(BASE)
    slow["demands"] = [{"id": f"D{i}", "material": "AL", "length": 100 + i, "quantity": 1}
                       for i in range(20)]
    slow["new_stock"] = [{"id": f"N{i}", "material": "AL", "length": 3000,
                          "price": 100 + i, "available": 1} for i in range(20)]
    slow["budget_ms"] = 3000
    result = {}

    def run():
        result["resp"] = client.post("/optimize", json=slow)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    start = time.monotonic()
    r = client.get("/health")
    assert r.status_code == 200
    assert time.monotonic() - start < 2.0
    t.join()
    assert result["resp"].status_code == 200
