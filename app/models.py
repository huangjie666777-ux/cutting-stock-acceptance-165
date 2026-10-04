"""Request/response schemas and input validation.

All lengths are integer millimetres, prices are integer cents.
Booleans, negative values, duplicate IDs and illegal quantities are rejected.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_ITEMS = 60  # max expanded demand pieces
MAX_BARS = 24  # max expanded stock/remnant bars

EntityId = Union[str, int]


def _reject_bool_id(v):
    if isinstance(v, bool):
        raise ValueError("id must not be a boolean")
    if not isinstance(v, (str, int)):
        raise ValueError("id must be a string or integer")
    if isinstance(v, str) and not v:
        raise ValueError("id must not be empty")
    return v


class Demand(BaseModel):
    model_config = ConfigDict(strict=True)

    id: EntityId
    material: str = Field(min_length=1)
    length: StrictInt = Field(gt=0, description="finished part length, mm")
    quantity: StrictInt = Field(ge=1, le=MAX_ITEMS)

    _check_id = field_validator("id")(_reject_bool_id)


class NewStock(BaseModel):
    model_config = ConfigDict(strict=True)

    id: EntityId
    material: str = Field(min_length=1)
    length: StrictInt = Field(gt=0, description="stock bar length, mm")
    price: StrictInt = Field(ge=0, description="price per bar, cents")
    available: StrictInt = Field(ge=1, le=MAX_BARS, description="usable bar count")

    _check_id = field_validator("id")(_reject_bool_id)


class Remnant(BaseModel):
    model_config = ConfigDict(strict=True)

    id: EntityId
    material: str = Field(min_length=1)
    length: StrictInt = Field(gt=0, description="existing remnant length, mm")

    _check_id = field_validator("id")(_reject_bool_id)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    demands: List[Demand] = Field(min_length=1)
    new_stock: List[NewStock] = Field(default_factory=list)
    remnants: List[Remnant] = Field(default_factory=list)
    kerf: StrictInt = Field(ge=0, description="saw kerf width, mm")
    tail_threshold: StrictInt = Field(ge=0, description="min reusable tail length, mm")
    budget_ms: StrictInt = Field(gt=0, le=600_000, description="solver time budget, ms")

    @model_validator(mode="after")
    def _check_global(self):
        groups = (
            ("demands", self.demands),
            ("new_stock", self.new_stock),
            ("remnants", self.remnants),
        )
        # The same id may not be reused across the stock lists either.
        cross_owner: dict = {}
        for group_name, group in groups:
            seen = set()
            for entry in group:
                key = entry.id
                if key in seen:
                    raise ValueError(f"duplicate id in {group_name}: {entry.id!r}")
                seen.add(key)
                if group_name != "demands":
                    if key in cross_owner:
                        raise ValueError(
                            f"duplicate stock id {entry.id!r} in both {cross_owner[key]} and {group_name}"
                        )
                    cross_owner[key] = group_name
        total_items = sum(d.quantity for d in self.demands)
        if total_items > MAX_ITEMS:
            raise ValueError(f"too many demand pieces: {total_items} > {MAX_ITEMS}")
        total_bars = sum(s.available for s in self.new_stock) + len(self.remnants)
        if total_bars > MAX_BARS:
            raise ValueError(f"too many stock bars: {total_bars} > {MAX_BARS}")
        if total_bars == 0:
            raise ValueError("no stock available: new_stock and remnants are both empty")
        return self


class Cut(BaseModel):
    demand_id: EntityId
    instance: int = Field(ge=1, description="1-based instance number within the demand")
    length: int


class BarLayout(BaseModel):
    source: Literal["new_stock", "remnant"]
    source_id: EntityId
    instance: int = Field(ge=1, description="1-based instance number within the source")
    material: str
    bar_length: int
    cuts: List[Cut]
    kerf_total: int
    tail: int
    tail_reusable: bool


class Metrics(BaseModel):
    total_cost: int
    total_loss: int
    bars_used: int


class OptimizeResponse(BaseModel):
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "NO_SOLUTION_WITHIN_BUDGET"]
    proven_optimal: bool
    layouts: List[BarLayout]
    metrics: Optional[Metrics]
    elapsed_ms: int
    message: str = ""


def _decimal_fraction(v):
    """Accept only decimal strings strictly inside [0, 1]; reject bools and numbers."""
    if isinstance(v, bool) or not isinstance(v, str):
        raise ValueError("risk must be a decimal string")
    s = v.strip()
    try:
        d = Decimal(s)
    except Exception as exc:
        raise ValueError("risk must be a decimal string") from exc
    if not (d >= 0 and d <= 1):
        raise ValueError("risk must be between 0 and 1 inclusive")
    if not d.is_finite():
        raise ValueError("risk must be finite")
    return v


class ToleranceSpec(BaseModel):
    model_config = ConfigDict(strict=True)

    lower: StrictInt = Field(description="lower deviation, micrometres")
    upper: StrictInt = Field(description="upper deviation, micrometres")

    @model_validator(mode="after")
    def _check_order(self):
        if self.lower > self.upper:
            raise ValueError("tolerance inverted: lower deviation must be <= upper deviation")
        return self


class BatchCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    batch_id: EntityId
    request: OptimizeRequest
    tolerances: Dict[EntityId, ToleranceSpec]
    dg: StrictInt = Field(ge=0, description="acceptable number of defectives")
    db: StrictInt = Field(ge=1, description="unacceptable number of defectives")
    alpha: str = Field(description="producer risk, decimal string in [0,1]")
    beta: str = Field(description="consumer risk, decimal string in [0,1]")

    _check_alpha = field_validator("alpha")(_decimal_fraction)
    _check_beta = field_validator("beta")(_decimal_fraction)
    _check_batch_id = field_validator("batch_id")(_reject_bool_id)

    @model_validator(mode="after")
    def _check_batch(self):
        demand_ids = {d.id for d in self.request.demands}
        # JSON object keys are strings; map numeric demand ids to their string form.
        normalized = {}
        for key, spec in self.tolerances.items():
            norm_key = key
            if isinstance(key, str):
                try:
                    norm_key = int(key)
                except ValueError:
                    norm_key = key
            normalized[norm_key] = spec
        tol_ids = set(normalized)
        missing = demand_ids - tol_ids
        if missing:
            raise ValueError(f"missing tolerance for demand id(s): {sorted(map(str, missing))}")
        unknown = tol_ids - demand_ids
        if unknown:
            raise ValueError(f"tolerance for unknown demand id(s): {sorted(map(str, unknown))}")
        object.__setattr__(self, "tolerances", normalized)
        lot_n = sum(d.quantity for d in self.request.demands)
        if not (0 <= self.dg < self.db <= lot_n):
            raise ValueError("require 0 <= dg < db <= lot size N")
        return self


class MeasurementRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    measured_length: StrictInt = Field(description="measured length, micrometres")
