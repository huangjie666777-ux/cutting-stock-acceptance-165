"""CP-SAT integer model and staged lexicographic optimization.

Objectives, strictly prioritized in this order:
  1. total purchase cost of new stock (remnants are free),
  2. total non-reusable loss (kerf + tails below the reuse threshold),
  3. number of bars used.

The millisecond budget is split across the three stages. A stage that
exhausts its slice without proving optimality stops the pipeline: the
best complete feasible solution found so far is returned as FEASIBLE.
OPTIMAL / INFEASIBLE are only reported once actually proven.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

# Fraction of the remaining budget granted to each stage.
STAGE_BUDGET_FRACTIONS = (0.5, 0.3, 0.2)


@dataclass(frozen=True)
class Item:
    demand_id: object
    instance: int
    material: str
    length: int


@dataclass(frozen=True)
class Bar:
    source: str  # "new_stock" | "remnant"
    source_id: object
    instance: int
    material: str
    length: int
    price: int  # 0 for remnants


@dataclass
class SolveResult:
    status: str  # OPTIMAL | FEASIBLE | INFEASIBLE | NO_SOLUTION_WITHIN_BUDGET
    assignment: Optional[Dict[int, int]] = None  # item index -> bar index
    objective_values: Tuple[Optional[int], Optional[int], Optional[int]] = (None, None, None)
    proven_stages: int = 0
    message: str = ""


def expand_items(demands) -> List[Item]:
    items: List[Item] = []
    for d in demands:
        for n in range(1, d.quantity + 1):
            items.append(Item(d.id, n, d.material, d.length))
    return items


def expand_bars(new_stock, remnants) -> List[Bar]:
    bars: List[Bar] = []
    for s in new_stock:
        for n in range(1, s.available + 1):
            bars.append(Bar("new_stock", s.id, n, s.material, s.length, s.price))
    for r in remnants:
        bars.append(Bar("remnant", r.id, 1, r.material, r.length, 0))
    return bars


def solve(items: List[Item], bars: List[Bar], kerf: int, tail_threshold: int, budget_ms: int) -> SolveResult:
    start = time.monotonic()
    n_items, n_bars = len(items), len(bars)
    big_m = max(b.length for b in bars) + kerf * (n_items + 1) + 1

    model = cp_model.CpModel()
    x: Dict[Tuple[int, int], object] = {}
    for i, item in enumerate(items):
        for b, bar in enumerate(bars):
            if item.material == bar.material and item.length <= bar.length:
                x[i, b] = model.NewBoolVar(f"x_{i}_{b}")
    for i in range(n_items):
        candidates = [x[i, b] for b in range(n_bars) if (i, b) in x]
        if not candidates:
            return SolveResult(status="INFEASIBLE", message="an item fits no stock bar")
        model.AddExactlyOne(candidates)

    use_b, exact_b, tail_b, reusable_b, tail_loss_b = [], [], [], [], []
    n_b, loss_b = [], []
    for b, bar in enumerate(bars):
        assigned = [x[i, b] for i in range(n_items) if (i, b) in x]
        nb = model.NewIntVar(0, n_items, f"n_{b}")
        model.Add(nb == sum(assigned))
        use = model.NewBoolVar(f"use_{b}")
        model.Add(nb >= use)
        model.Add(nb <= n_items * use)
        used_len = sum(items[i].length * x[i, b] for i in range(n_items) if (i, b) in x)
        exact = model.NewBoolVar(f"exact_{b}")
        model.Add(exact <= use)
        # consumed = used_len + kerf*nb - kerf*exact <= bar.length
        model.Add(used_len + kerf * nb - kerf * exact <= bar.length)
        # exact == 1  <=>  the last piece consumes the bar exactly
        model.Add(used_len + kerf * nb - kerf >= bar.length - big_m * (1 - exact))
        tail = model.NewIntVar(0, bar.length, f"tail_{b}")
        model.Add(tail == bar.length * use - used_len - kerf * nb + kerf * exact)
        reusable = model.NewBoolVar(f"reusable_{b}")
        model.Add(tail >= tail_threshold * reusable)
        model.Add(tail <= (tail_threshold - 1) + big_m * reusable)
        tail_loss = model.NewIntVar(0, bar.length, f"tailloss_{b}")
        model.Add(tail_loss <= tail)
        model.Add(tail_loss <= big_m * (1 - reusable))
        model.Add(tail_loss >= tail - big_m * reusable)
        loss = model.NewIntVar(0, big_m, f"loss_{b}")
        model.Add(loss == kerf * nb - kerf * exact + tail_loss)
        use_b.append(use)
        exact_b.append(exact)
        tail_b.append(tail)
        reusable_b.append(reusable)
        tail_loss_b.append(tail_loss)
        n_b.append(nb)
        loss_b.append(loss)

    total_cost = sum(bar.price * use_b[b] for b, bar in enumerate(bars))
    total_loss = sum(loss_b)
    total_bars = sum(use_b)
    objectives = (total_cost, total_loss, total_bars)

    best_assignment: Optional[Dict[int, int]] = None
    objective_values: List[Optional[int]] = [None, None, None]
    proven_stages = 0

    for stage, fraction in enumerate(STAGE_BUDGET_FRACTIONS):
        elapsed_ms = (time.monotonic() - start) * 1000.0
        remaining_ms = budget_ms - elapsed_ms
        if remaining_ms <= 0:
            break
        stage_budget = remaining_ms if stage == len(STAGE_BUDGET_FRACTIONS) - 1 else budget_ms * fraction
        stage_budget = min(stage_budget, remaining_ms)

        model.ClearObjective()
        model.Minimize(objectives[stage])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = stage_budget / 1000.0
        solver.parameters.num_workers = 8
        status = solver.Solve(model)

        if status == cp_model.INFEASIBLE:
            return SolveResult(status="INFEASIBLE", message="proven infeasible")
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break  # budget exhausted, nothing new learned this stage

        best_assignment = {i: b for (i, b), var in x.items() if solver.Value(var) == 1}
        objective_values[stage] = int(round(solver.ObjectiveValue()))

        if status != cp_model.OPTIMAL:
            break  # feasible but unproven: cannot proceed to the next priority
        proven_stages = stage + 1
        model.Add(objectives[stage] == objective_values[stage])

    if best_assignment is None:
        return SolveResult(
            status="NO_SOLUTION_WITHIN_BUDGET",
            objective_values=tuple(objective_values),
            proven_stages=proven_stages,
            message="budget exhausted before any complete feasible solution was found",
        )
    final = "OPTIMAL" if proven_stages == len(STAGE_BUDGET_FRACTIONS) else "FEASIBLE"
    return SolveResult(
        status=final,
        assignment=best_assignment,
        objective_values=tuple(objective_values),
        proven_stages=proven_stages,
        message="" if final == "OPTIMAL" else "budget exhausted before optimality was proven",
    )
