"""Schemas for batch sampling acceptance.

Lengths: nominal part lengths arrive in integer millimetres inside the
nested optimize request and are converted to integer micrometres for the
frozen plan; tolerance deviations and measurements are integer
micrometres throughout. Acceptance boundaries are inclusive.
"""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from .models import EntityId, OptimizeRequest, _reject_bool_id
from .sampling import parse_risk


class Tolerance(BaseModel):
    model_config = ConfigDict(strict=True)

    lower_dev_um: StrictInt = Field(description="signed deviation below nominal, micrometres")
    upper_dev_um: StrictInt = Field(description="signed deviation above nominal, micrometres")

    @model_validator(mode="after")
    def _ordered(self):
        if self.lower_dev_um > self.upper_dev_um:
            raise ValueError(
                f"inverted tolerance: lower_dev_um {self.lower_dev_um} > upper_dev_um {self.upper_dev_um}"
            )
        return self


class BatchCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    batch_id: EntityId
    optimize: OptimizeRequest
    tolerances: Dict[str, Tolerance] = Field(
        min_length=1, description="per-demand tolerance, keyed by str(demand id)"
    )
    dg: StrictInt = Field(ge=0, description="acceptable defect count in the lot")
    db: StrictInt = Field(ge=1, description="unacceptable defect count in the lot")
    alpha: StrictStr = Field(description="producer risk, decimal string in (0, 1)")
    beta: StrictStr = Field(description="consumer risk, decimal string in (0, 1)")

    _check_id = field_validator("batch_id")(_reject_bool_id)

    @field_validator("alpha", "beta")
    @classmethod
    def _risk(cls, v):
        parse_risk(v)  # raises ValueError on non-decimal or out-of-range strings
        return v

    @model_validator(mode="after")
    def _check_against_demands(self):
        demand_keys = {str(d.id) for d in self.optimize.demands}
        unknown = sorted(set(self.tolerances) - demand_keys)
        if unknown:
            raise ValueError(f"tolerances reference unknown demands: {unknown}")
        missing = sorted(demand_keys - set(self.tolerances))
        if missing:
            raise ValueError(f"every demand needs a tolerance, missing: {missing}")
        total = sum(d.quantity for d in self.optimize.demands)
        if not (self.dg < self.db <= total):
            raise ValueError(
                f"require 0 <= Dg < Db <= N, got Dg={self.dg}, Db={self.db}, N={total}"
            )
        return self


class MeasurementRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    demand_id: EntityId
    instance: StrictInt = Field(ge=1)
    measured_um: StrictInt = Field(description="measured length, integer micrometres")

    _check_id = field_validator("demand_id")(_reject_bool_id)
