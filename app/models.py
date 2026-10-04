"""Request/response schemas and input validation.

All lengths are integer millimetres, prices are integer cents.
Booleans, negative values, duplicate IDs and illegal quantities are rejected.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Union

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
        for group_name, group in (
            ("demands", self.demands),
            ("new_stock", self.new_stock),
            ("remnants", self.remnants),
        ):
            seen = set()
            for entry in group:
                key = entry.id
                if key in seen:
                    raise ValueError(f"duplicate id in {group_name}: {entry.id!r}")
                seen.add(key)
        stock_ids = {s.id for s in self.new_stock}
        remnant_ids = {r.id for r in self.remnants}
        overlap = stock_ids & remnant_ids
        if overlap:
            raise ValueError(f"duplicate id across new_stock and remnants: {sorted(overlap, key=repr)!r}")
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
