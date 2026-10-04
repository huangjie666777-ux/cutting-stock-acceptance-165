import json
from fractions import Fraction
from math import comb

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.sampling import acceptance_probability, find_plan, parse_risk


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_DB_PATH", str(tmp_path / "batches.db"))
    main._store = None
    yield TestClient(main.app)
    main._store = None


def batch_payload(**overrides):
    payload = {
        "batch_id": "B1",
        "optimize": {
            "demands": [{"id": "D1", "material": "AL", "length": 1000, "quantity": 4}],
            "new_stock": [{"id": "N1", "material": "AL", "length": 6000, "price": 100, "available": 2}],
            "remnants": [],
            "kerf": 5,
            "tail_threshold": 300,
            "budget_ms": 5000,
        },
        "tolerances": {"D1": {"lower_dev_um": -2000, "upper_dev_um": 3000}},
        "dg": 0,
        "db": 2,
        "alpha": "0.05",
        "beta": "0.10",
    }
    payload.update(overrides)
    return payload


# --- sampling math -------------------------------------------------------------

def test_acceptance_probability_exact():
    # N=10, D=3, n=4, c=1: (C(3,0)C(7,4) + C(3,1)C(7,3)) / C(10,4)
    expected = Fraction(comb(7, 4) + 3 * comb(7, 3), comb(10, 4))
    assert acceptance_probability(10, 3, 4, 1) == expected


def test_find_plan_minimizes_n_then_c():
    alpha, beta = parse_risk("0.05"), parse_risk("0.10")
    plan = find_plan(20, 2, 8, alpha, beta)
    assert 1 - acceptance_probability(20, 2, plan.sample_size, plan.acceptance) <= alpha
    assert acceptance_probability(20, 8, plan.sample_size, plan.acceptance) <= beta
    # minimality: no smaller n works, and no smaller c at this n
    for n in range(1, plan.sample_size):
        assert not any(
            1 - acceptance_probability(20, 2, n, c) <= alpha
            and acceptance_probability(20, 8, n, c) <= beta
            for c in range(n)
        )
    for c in range(plan.acceptance):
        assert not (
            1 - acceptance_probability(20, 2, plan.sample_size, c) <= alpha
            and acceptance_probability(20, 8, plan.sample_size, c) <= beta
        )


@pytest.mark.parametrize("bad", ["0", "1", "1.0", "-0.1", "abc", "", True, 0.5, None])
def test_parse_risk_rejects(bad):
    with pytest.raises(ValueError):
        parse_risk(bad)


# --- batch creation ------------------------------------------------------------

def test_create_batch_freezes_plan_and_sample(client):
    r = client.post("/batches", json=batch_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["batch_created"] is True
    assert body["status"] == "OPTIMAL"
    assert body["layouts"]  # frozen plan present
    s = body["sampling"]
    assert s["population"] == 4 and s["sample_size"] == 3 and s["acceptance"] == 0
    assert s["producer_risk"] == "0" and s["consumer_risk"] == "0"
    assert len(body["samples"]) == 3
    for sample in body["samples"]:
        assert sample["nominal_um"] == 1_000_000  # 1000 mm -> um
        assert sample["lower_dev_um"] == -2000 and sample["upper_dev_um"] == 3000
        assert sample["measured_um"] is None
    assert body["conclusion"] is None


def test_create_batch_idempotent_same_content(client):
    first = client.post("/batches", json=batch_payload()).json()
    r = client.post("/batches", json=batch_payload())
    assert r.status_code == 200
    assert r.json()["samples"] == first["samples"]  # original sample returned


def test_create_batch_conflict_different_content(client):
    client.post("/batches", json=batch_payload())
    other = batch_payload()
    other["tolerances"] = {"D1": {"lower_dev_um": -1000, "upper_dev_um": 1000}}
    r = client.post("/batches", json=other)
    assert r.status_code == 409


def test_create_batch_infeasible_creates_nothing(client):
    payload = batch_payload()
    payload["optimize"]["demands"] = [{"id": "D1", "material": "AL", "length": 9000, "quantity": 1}]
    payload["db"] = 1
    r = client.post("/batches", json=payload)
    assert r.status_code == 200
    assert r.json()["batch_created"] is False
    assert client.get("/batches/B1").status_code == 404


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(dg=True),                       # boolean rejected
    lambda p: p.update(dg=2, db=2),                    # Dg < Db violated
    lambda p: p.update(dg=-1),
    lambda p: p.update(db=5),                          # Db > N
    lambda p: p.update(alpha=0.05),                    # must be a string
    lambda p: p.update(alpha="1.5"),
    lambda p: p.update(beta="0"),
    lambda p: p["tolerances"].update(DX={"lower_dev_um": 0, "upper_dev_um": 1}),  # unknown demand
    lambda p: p.update(tolerances={}),                 # missing tolerance
    lambda p: p["tolerances"].update(D1={"lower_dev_um": 5, "upper_dev_um": -5}),  # inverted
    lambda p: p["optimize"].update(kerf=True),
])
def test_create_batch_validation(client, mutate):
    payload = batch_payload()
    mutate(payload)
    assert client.post("/batches", json=payload).status_code == 422


def test_cross_list_duplicate_id_rejected(client):
    payload = {
        "demands": [{"id": "D1", "material": "AL", "length": 100, "quantity": 1}],
        "new_stock": [{"id": "X", "material": "AL", "length": 6000, "price": 1, "available": 1}],
        "remnants": [{"id": "X", "material": "AL", "length": 500}],
        "kerf": 0,
        "tail_threshold": 0,
        "budget_ms": 1000,
    }
    assert client.post("/optimize", json=payload).status_code == 422


# --- measurements and conclusion ------------------------------------------------

def _create(client):
    return client.post("/batches", json=batch_payload()).json()


def test_measurement_flow_to_reject(client):
    body = _create(client)
    samples = body["samples"]
    # non-sample instance rejected (population 4, sample 3 -> exactly one outsider)
    outsider = ({1, 2, 3, 4} - {s["instance"] for s in samples}).pop()
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": "D1", "instance": outsider, "measured_um": 1_000_000})
    assert r.status_code == 422
    # unknown batch
    assert client.post("/batches/NOPE/measurements",
                       json={"demand_id": "D1", "instance": 1, "measured_um": 1}).status_code == 404
    # in-tolerance measurement (boundary inclusive: nominal + upper_dev)
    first = samples[0]
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": first["demand_id"], "instance": first["instance"],
                          "measured_um": 1_003_000})
    assert r.status_code == 200
    view = r.json()
    assert view["conclusion"] is None  # incomplete -> no conclusion
    recorded = [s for s in view["samples"] if s["instance"] == first["instance"]][0]
    assert recorded["measured_um"] == 1_003_000 and recorded["defective"] is False
    # idempotent resend of the same value
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": first["demand_id"], "instance": first["instance"],
                          "measured_um": 1_003_000})
    assert r.status_code == 200
    # different value must not overwrite
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": first["demand_id"], "instance": first["instance"],
                          "measured_um": 1_000_000})
    assert r.status_code == 409
    # remaining two: one defective (beyond lower boundary), one ok
    rest = samples[1:]
    client.post("/batches/B1/measurements",
                json={"demand_id": rest[0]["demand_id"], "instance": rest[0]["instance"],
                      "measured_um": 997_999})  # nominal - 2001 < lower bound
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": rest[1]["demand_id"], "instance": rest[1]["instance"],
                          "measured_um": 998_000})  # exactly on lower boundary -> ok
    view = r.json()
    assert view["conclusion"] == "REJECT"  # 1 defect > c=0
    # conclusion immutable; resubmission stays idempotent/conflict
    assert client.get("/batches/B1").json()["conclusion"] == "REJECT"
    r = client.post("/batches/B1/measurements",
                    json={"demand_id": rest[0]["demand_id"], "instance": rest[0]["instance"],
                          "measured_um": 1_000_000})
    assert r.status_code == 409
    assert client.get("/batches/B1").json()["conclusion"] == "REJECT"


def test_measurement_flow_to_accept(client):
    body = _create(client)
    for s in body["samples"]:
        client.post("/batches/B1/measurements",
                    json={"demand_id": s["demand_id"], "instance": s["instance"],
                          "measured_um": s["nominal_um"]})
    assert client.get("/batches/B1").json()["conclusion"] == "ACCEPT"


def test_restart_resumes_state(client, tmp_path, monkeypatch):
    body = _create(client)
    s = body["samples"][0]
    client.post("/batches/B1/measurements",
                json={"demand_id": s["demand_id"], "instance": s["instance"], "measured_um": 999_000})
    # simulate restart: drop the in-memory store, reopen the same database file
    main._store = None
    view = client.get("/batches/B1").json()
    assert view["samples"][0]["measured_um"] == 999_000
    # replayed creation returns the original frozen sample
    again = client.post("/batches", json=batch_payload())
    assert again.status_code == 200
    assert [s["instance"] for s in again.json()["samples"]] == [s["instance"] for s in body["samples"]]


def test_concurrent_creation_single_winner(client):
    import threading

    results = []

    def post():
        for _ in range(20):
            code = client.post("/batches", json=batch_payload()).status_code
            if code != 429:  # solver capacity is bounded; retry like a real client
                results.append(code)
                return
        results.append(429)

    threads = [threading.Thread(target=post) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 200, 200, 200, 201]
    assert len(client.get("/batches/B1").json()["samples"]) == 3
