import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


BASE_REQUEST = {
    "demands": [{"id": "D1", "material": "AL", "length": 1000, "quantity": 8}],
    "new_stock": [{"id": "N1", "material": "AL", "length": 6000, "price": 1000, "available": 2}],
    "remnants": [],
    "kerf": 5,
    "tail_threshold": 300,
    "budget_ms": 5000,
}


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage.reset_for_tests(path)
    with TestClient(app) as c:
        yield c


def _batch_payload(**overrides):
    payload = {
        "batch_id": "B1",
        "request": BASE_REQUEST,
        "tolerances": {"D1": {"lower": -50, "upper": 100}},
        "dg": 1,
        "db": 5,
        "alpha": "0.05",
        "beta": "0.10",
    }
    payload.update(overrides)
    return payload


def test_create_and_get_batch(client):
    r = client.post("/batches", json=_batch_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["lot_size"] == 8
    plan = body["sampling_plan"]
    assert 1 <= plan["n"] <= 8 and 0 <= plan["c"] < plan["n"]
    assert len(body["sample"]) == plan["n"]
    for piece in body["sample"]:
        assert piece["nominal_length_um"] == 1_000_000
    r2 = client.get("/batches/B1")
    assert r2.status_code == 200
    assert r2.json()["sampling_plan"] == plan


def test_same_content_idempotent_different_conflicts(client):
    assert client.post("/batches", json=_batch_payload()).status_code == 200
    again = client.post("/batches", json=_batch_payload())
    assert again.status_code == 200 and again.json()["created"] is False
    changed = _batch_payload(alpha="0.01")
    assert client.post("/batches", json=changed).status_code == 409


def test_validation_rules(client):
    assert client.post("/batches", json=_batch_payload(dg=True)).status_code == 422
    assert client.post("/batches", json=_batch_payload(dg=5, db=1)).status_code == 422
    assert client.post("/batches", json=_batch_payload(alpha=0.05)).status_code == 422
    assert client.post("/batches", json=_batch_payload(alpha="1.5")).status_code == 422
    bad_tol = _batch_payload(tolerances={"D1": {"lower": 10, "upper": -10}})
    assert client.post("/batches", json=bad_tol).status_code == 422
    assert client.post("/batches", json=_batch_payload(tolerances={})).status_code == 422
    unknown = _batch_payload(tolerances={"D1": {"lower": 0, "upper": 1}, "X": {"lower": 0, "upper": 1}})
    assert client.post("/batches", json=unknown).status_code == 422


def test_cross_list_duplicate_id_rejected(client):
    req = dict(BASE_REQUEST)
    req["remnants"] = [{"id": "N1", "material": "AL", "length": 1200}]
    assert client.post("/batches", json=_batch_payload(request=req)).status_code == 422


def test_no_batch_without_complete_solution(client):
    req = dict(BASE_REQUEST)
    req["demands"] = [{"id": "D1", "material": "AL", "length": 9000, "quantity": 1}]
    r = client.post("/batches", json=_batch_payload(request=req))
    assert r.status_code == 422
    assert client.get("/batches").json()["batches"] == []


def test_measurement_flow_boundary_idempotent_conflict(client):
    body = client.post("/batches", json=_batch_payload()).json()
    n, c = body["sampling_plan"]["n"], body["sampling_plan"]["c"]
    sample = body["sample"]
    first = sample[0]
    url = f"/batches/B1/pieces/{first['demand_id']}/{first['instance']}"
    # exact lower boundary is conforming
    boundary = first["nominal_length_um"] + first["lower_deviation_um"]
    r = client.put(url, json={"measured_length": boundary})
    assert r.status_code == 200
    assert r.json()["measured_count"] == 1
    # same value resent -> idempotent
    assert client.put(url, json={"measured_length": boundary}).status_code == 200
    # different value -> conflict, no overwrite
    assert client.put(url, json={"measured_length": boundary + 1}).status_code == 409
    # only sampled pieces accepted
    sampled_keys = {(p["demand_id"], p["instance"]) for p in sample}
    all_inst = [(d["id"], i) for d in BASE_REQUEST["demands"] for i in range(1, d["quantity"] + 1)]
    unsampled = next(k for k in all_inst if k not in sampled_keys)
    r = client.put(f"/batches/B1/pieces/{unsampled[0]}/{unsampled[1]}",
                   json={"measured_length": 1_000_000})
    assert r.status_code == 404
    # incomplete -> pending
    assert client.get("/batches/B1").json()["verdict"] is None

    # fill the rest; defectives outside [-50, +100] um
    defects = 0
    for i, piece in enumerate(sample[1:], start=1):
        val = piece["nominal_length_um"] + piece["upper_deviation_um"]
        if i % 2 == 0:
            val += 1  # defective
            defects += 1
        rr = client.put(
            f"/batches/B1/pieces/{piece['demand_id']}/{piece['instance']}",
            json={"measured_length": val},
        )
        assert rr.status_code == 200
    final = client.get("/batches/B1").json()
    assert final["verdict"] in ("ACCEPTED", "REJECTED")
    assert (final["defect_count"] <= c) == (final["verdict"] == "ACCEPTED")
    # verdict frozen
    again = client.get("/batches/B1").json()
    assert again["verdict"] == final["verdict"]
    assert again["measured_count"] == n


def test_restart_resumes_inspection(client):
    body = client.post("/batches", json=_batch_payload()).json()
    piece = body["sample"][0]
    client.put(f"/batches/B1/pieces/{piece['demand_id']}/{piece['instance']}",
               json={"measured_length": piece["nominal_length_um"]})
    # simulate restart by reopening the same DB file
    path = storage.DB_PATH
    storage.reset_connection_for_restart(path)
    got = client.get("/batches/B1").json()
    assert got["measured_count"] == 1
    assert got["sample"][0]["measured"] is True


def test_numeric_demand_id_tolerance_keys(client):
    req = json.loads(json.dumps(BASE_REQUEST))
    req["demands"][0]["id"] = 7
    payload = _batch_payload(request=req, tolerances={"7": {"lower": 0, "upper": 10}})
    r = client.post("/batches", json=payload)
    assert r.status_code == 200, r.text


def test_concurrent_creates_single_effect(client):
    import threading

    from decimal import Decimal

    from app.models import OptimizeRequest
    from app.accounting import build_layouts
    from app.solver import expand_bars, expand_items, solve

    req = OptimizeRequest.model_validate(BASE_REQUEST)
    items = expand_items(req.demands)
    bars = expand_bars(req.new_stock, req.remnants)
    res = solve(items, bars, req.kerf, req.tail_threshold, req.budget_ms)
    layouts_obj, metrics_obj = build_layouts(items, bars, res.assignment, req.kerf, req.tail_threshold)
    from fastapi.encoders import jsonable_encoder
    layouts = jsonable_encoder(layouts_obj)
    metrics = jsonable_encoder(metrics_obj)
    created_flag = []
    errors = []

    def fire():
        try:
            _, created = storage.create_batch(
                "B1",
                json.loads(json.dumps(BASE_REQUEST)),
                {"D1": {"lower": -50, "upper": 100}},
                layouts, metrics,
                1, 5, Decimal("0.05"), Decimal("0.10"),
                8, 1, Decimal("0"), Decimal("0"),
            )
            created_flag.append(created)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert created_flag.count(True) == 1 and created_flag.count(False) == 5
    listing = client.get("/batches").json()["batches"]
    assert len(listing) == 1
    b = client.get("/batches/B1").json()
    assert len(b["sample"]) == b["sampling_plan"]["n"]
