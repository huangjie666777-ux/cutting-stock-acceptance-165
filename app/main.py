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
