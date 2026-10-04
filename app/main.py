"""FastAPI entrypoint: HTTP layer, concurrency limiting, non-blocking solve."""

from __future__ import annotations

import asyncio
import json
import os
import time

from decimal import Decimal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .accounting import build_layouts
from .models import BatchCreateRequest, MeasurementRequest, OptimizeRequest, OptimizeResponse
from .sampling import find_plan
from .solver import expand_bars, expand_items, solve
from . import storage

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


def _run_solve(req: OptimizeRequest):
    items = expand_items(req.demands)
    bars = expand_bars(req.new_stock, req.remnants)
    result = solve(items, bars, req.kerf, req.tail_threshold, req.budget_ms)
    if result.status in ("INFEASIBLE", "NO_SOLUTION_WITHIN_BUDGET"):
        return result, None, None
    layouts, metrics = build_layouts(items, bars, result.assignment, req.kerf, req.tail_threshold)
    return result, layouts, metrics


def _solve_to_completion(req: OptimizeRequest):
    """Reuse solve + accounting; return complete layouts only."""
    result, layouts, metrics = _run_solve(req)
    if result.status in ("INFEASIBLE", "NO_SOLUTION_WITHIN_BUDGET"):
        raise HTTPException(
            status_code=422,
            detail={"solver_status": result.status, "message": result.message},
        )
    return result, layouts, metrics


@app.post("/batches")
async def create_batch(payload: BatchCreateRequest):
    """Submit the original cutting request plus per-demand tolerances; freeze a batch."""
    if _semaphore.locked():
        raise HTTPException(status_code=429, detail="solver capacity exhausted, retry later")
    async with _semaphore:
        loop = asyncio.get_running_loop()
        try:
            result, layouts, metrics = await loop.run_in_executor(
                None, _solve_to_completion, payload.request
            )
        except HTTPException:
            raise

    req = payload.request
    lot_n = sum(d.quantity for d in req.demands)
    plan = find_plan(
        lot_n, payload.dg, payload.db, Decimal(payload.alpha), Decimal(payload.beta)
    )
    if plan is None:
        raise HTTPException(
            status_code=422,
            detail="no sampling plan (n, c) satisfies the given risk limits",
        )
    sample_n, accept_c, producer_risk, consumer_risk = plan

    request_dict = jsonable_encoder(req)
    tolerances_dict = {k: jsonable_encoder(v) for k, v in payload.tolerances.items()}
    layouts_dict = [jsonable_encoder(l) for l in layouts]
    metrics_dict = jsonable_encoder(metrics)
    try:
        row, created = await loop.run_in_executor(
            None,
            storage.create_batch,
            payload.batch_id,
            request_dict,
            tolerances_dict,
            layouts_dict,
            metrics_dict,
            payload.dg,
            payload.db,
            Decimal(payload.alpha),
            Decimal(payload.beta),
            sample_n,
            accept_c,
            producer_risk,
            consumer_risk,
        )
    except storage.ContentMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    batch = storage.get_batch(payload.batch_id)
    return {
        "batch_id": payload.batch_id,
        "created": created,
        "solver_status": result.status,
        "proven_optimal": result.status == "OPTIMAL",
        "layouts": layouts_dict,
        "metrics": metrics_dict,
        "tolerances_um": tolerances_dict,
        "lot_size": lot_n,
        "sampling_plan": {
            "n": sample_n,
            "c": accept_c,
            "actual_producer_risk": str(producer_risk),
            "actual_consumer_risk": str(consumer_risk),
        },
        "status": batch["status"],
        "verdict": batch["verdict"],
        "sample": batch["samples"],
    }


@app.get("/batches")
async def list_batches():
    return {"batches": storage.list_batches()}


@app.get("/batches/{batch_id:path}")
async def get_batch(batch_id: str):
    key = jsonable_encoder(batch_id)
    batch = storage.get_batch(key)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"unknown batch {batch_id!r}")
    return _batch_view(batch)


def _batch_view(batch: dict):
    return {
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "verdict": batch["verdict"],
        "lot_size": batch["n_total"],
        "dg": batch["dg"],
        "db": batch["dbad"],
        "alpha": batch["alpha"],
        "beta": batch["beta"],
        "sampling_plan": {
            "n": batch["sample_n"],
            "c": batch["accept_c"],
            "actual_producer_risk": batch["producer_risk"],
            "actual_consumer_risk": batch["consumer_risk"],
        },
        "measured_count": batch["measured_count"],
        "defect_count": batch["defect_count"],
        "sample": batch["samples"],
        "layouts": json.loads(batch["layouts_json"]),
        "metrics": json.loads(batch["metrics_json"]) if batch["metrics_json"] else None,
    }


@app.put("/batches/{batch_id:path}/pieces/{demand_id:path}/{instance}")
async def submit_measurement(batch_id: str, demand_id: str, instance: int, payload: MeasurementRequest):
    if instance < 1:
        raise HTTPException(status_code=422, detail="instance must be >= 1")
    def _maybe(v):
        try:
            return json.loads(v)
        except Exception:
            return v

    bid, did = _maybe(batch_id), _maybe(demand_id)
    try:
        batch = storage.record_measurement(bid, did, instance, payload.measured_length)
    except storage.NotSampled as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except storage.MeasurementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _batch_view(batch)
