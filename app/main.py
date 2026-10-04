"""FastAPI entrypoint: HTTP layer, concurrency limiting, non-blocking solve."""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .accounting import build_layouts
from .models import OptimizeRequest, OptimizeResponse
from .solver import expand_bars, expand_items, solve

MAX_CONCURRENT_SOLVES = int(os.environ.get("MAX_CONCURRENT_SOLVES", "2"))

app = FastAPI(title="Profile Cut-to-Length Optimizer", version="1.0.0")

# Guards solver capacity across requests; always released via context manager.
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SOLVES)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/health")
async def health():
    return {"status": "ok", "solver_slots_available": _semaphore._value}


@app.post("/optimize", response_model=OptimizeResponse)
async def optimize(req: OptimizeRequest):
    if _semaphore.locked():
        raise HTTPException(status_code=429, detail="solver capacity exhausted, retry later")
    async with _semaphore:
        try:
            started = time.monotonic()
            items = expand_items(req.demands)
            bars = expand_bars(req.new_stock, req.remnants)
            loop = asyncio.get_running_loop()
            # Run CP-SAT in a worker thread so health checks stay responsive.
            result = await loop.run_in_executor(
                None, solve, items, bars, req.kerf, req.tail_threshold, req.budget_ms
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if result.status in ("INFEASIBLE", "NO_SOLUTION_WITHIN_BUDGET"):
                return OptimizeResponse(
                    status=result.status,
                    proven_optimal=False,
                    layouts=[],
                    metrics=None,
                    elapsed_ms=elapsed_ms,
                    message=result.message,
                )
            layouts, metrics = build_layouts(
                items, bars, result.assignment, req.kerf, req.tail_threshold
            )
            return OptimizeResponse(
                status=result.status,
                proven_optimal=result.status == "OPTIMAL",
                layouts=layouts,
                metrics=metrics,
                elapsed_ms=elapsed_ms,
                message=result.message,
            )
        except HTTPException:
            raise
        except Exception as exc:  # capacity released by context manager either way
            raise HTTPException(status_code=500, detail=f"solve failed: {exc}") from exc


# --- Batch sampling acceptance -------------------------------------------------

import hashlib
import json
import random
import secrets
import threading

from fastapi.encoders import jsonable_encoder as _jsonable

from .batch_models import BatchCreateRequest, MeasurementRequest
from .sampling import find_plan, parse_risk, risk_str
from .store import BatchStore

_store = None
_store_lock = threading.Lock()


def get_store() -> BatchStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = BatchStore(os.environ.get("BATCH_DB_PATH", "batches.db"))
        return _store


def _demand_key(demand_id) -> str:
    return json.dumps(demand_id)


@app.post("/batches")
async def create_batch(req: BatchCreateRequest):
    """Solve, then freeze the plan plus a fixed random sample as a batch."""
    if _semaphore.locked():
        raise HTTPException(status_code=429, detail="solver capacity exhausted, retry later")
    async with _semaphore:
        try:
            items = expand_items(req.optimize.demands)
            bars = expand_bars(req.optimize.new_stock, req.optimize.remnants)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                solve,
                items,
                bars,
                req.optimize.kerf,
                req.optimize.tail_threshold,
                req.optimize.budget_ms,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"solve failed: {exc}") from exc

    batch_id = str(req.batch_id)
    if result.status in ("INFEASIBLE", "NO_SOLUTION_WITHIN_BUDGET"):
        # No complete solution -> no batch is created.
        return JSONResponse(
            status_code=200,
            content={
                "batch_id": batch_id,
                "batch_created": False,
                "status": result.status,
                "message": result.message,
            },
        )

    layouts, metrics = build_layouts(
        items, bars, result.assignment, req.optimize.kerf, req.optimize.tail_threshold
    )

    population = len(items)
    plan = find_plan(population, req.dg, req.db, parse_risk(req.alpha), parse_risk(req.beta))

    # Uniform sample without replacement, fixed forever once persisted.
    instances = [(it.demand_id, it.instance) for it in items]
    rng = random.Random(secrets.randbits(64))
    picked = rng.sample(instances, plan.sample_size)

    demand_by_key = {str(d.id): d for d in req.optimize.demands}
    samples = []
    for demand_id, instance in sorted(picked, key=lambda p: (str(p[0]), p[1])):
        demand = demand_by_key[str(demand_id)]
        tol = req.tolerances[str(demand_id)]
        samples.append(
            {
                "demand_id": demand_id,
                "demand_key": _demand_key(demand_id),
                "instance": instance,
                "nominal_um": demand.length * 1000,
                "lower_dev_um": tol.lower_dev_um,
                "upper_dev_um": tol.upper_dev_um,
            }
        )

    response = {
        "batch_id": batch_id,
        "batch_created": True,
        "status": result.status,
        "proven_optimal": result.status == "OPTIMAL",
        "layouts": _jsonable(layouts),
        "metrics": _jsonable(metrics),
        "sampling": {
            "population": population,
            "dg": req.dg,
            "db": req.db,
            "alpha": req.alpha,
            "beta": req.beta,
            "sample_size": plan.sample_size,
            "acceptance": plan.acceptance,
            "producer_risk": risk_str(plan.producer_risk),
            "consumer_risk": risk_str(plan.consumer_risk),
            "producer_risk_exact": f"{plan.producer_risk.numerator}/{plan.producer_risk.denominator}",
            "consumer_risk_exact": f"{plan.consumer_risk.numerator}/{plan.consumer_risk.denominator}",
        },
        "samples": [
            {
                "demand_id": s["demand_id"],
                "instance": s["instance"],
                "nominal_um": s["nominal_um"],
                "lower_dev_um": s["lower_dev_um"],
                "upper_dev_um": s["upper_dev_um"],
                "measured_um": None,
                "defective": None,
            }
            for s in samples
        ],
        "conclusion": None,
    }
    content_hash = hashlib.sha256(
        json.dumps(_jsonable(req), sort_keys=True).encode()
    ).hexdigest()
    outcome, stored = get_store().insert_batch(
        batch_id, content_hash, response, plan.sample_size, plan.acceptance, samples
    )
    if outcome == "conflict":
        raise HTTPException(
            status_code=409, detail=f"batch {batch_id!r} already exists with different content"
        )
    return JSONResponse(
        status_code=201 if outcome == "created" else 200, content=stored
    )


@app.get("/batches/{batch_id}")
async def get_batch(batch_id: str):
    view = get_store().get_batch(batch_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id!r}")
    return view


@app.post("/batches/{batch_id}/measurements")
async def submit_measurement(batch_id: str, req: MeasurementRequest):
    store = get_store()
    outcome, _ = store.record_measurement(
        batch_id, _demand_key(req.demand_id), req.instance, req.measured_um
    )
    if outcome == "batch_missing":
        raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id!r}")
    if outcome == "not_sample":
        raise HTTPException(
            status_code=422,
            detail=f"instance not in the fixed sample: {req.demand_id!r}#{req.instance}",
        )
    if outcome == "conflict":
        raise HTTPException(
            status_code=409,
            detail=f"measurement already recorded with a different value: {req.demand_id!r}#{req.instance}",
        )
    return store.get_batch(batch_id)
