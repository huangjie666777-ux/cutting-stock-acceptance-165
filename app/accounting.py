"""Post-solve accounting: build per-bar layouts and verify conservation.

Kerf rule: every piece taken from a bar costs one kerf, except the last
piece when it exactly consumes the remaining length (tail == 0).
Loss = kerf consumed + tails shorter than the reuse threshold.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .models import BarLayout, Cut, Metrics
from .solver import Bar, Item


def build_layouts(
    items: List[Item],
    bars: List[Bar],
    assignment: Dict[int, int],
    kerf: int,
    tail_threshold: int,
) -> Tuple[List[BarLayout], Metrics]:
    per_bar: Dict[int, List[int]] = {}
    for item_idx, bar_idx in assignment.items():
        per_bar.setdefault(bar_idx, []).append(item_idx)

    layouts: List[BarLayout] = []
    total_cost = 0
    total_loss = 0
    for bar_idx in sorted(per_bar):
        bar = bars[bar_idx]
        members = sorted(per_bar[bar_idx], key=lambda i: (items[i].length, str(items[i].demand_id), items[i].instance))
        cut_len_sum = sum(items[i].length for i in members)
        n = len(members)
        # exact fit iff pieces plus one kerf per gap consume the bar exactly
        exact = cut_len_sum + kerf * (n - 1) == bar.length
        kerf_total = kerf * (n - (1 if exact else 0))
        tail = bar.length - cut_len_sum - kerf_total
        if tail < 0:
            raise ValueError(f"conservation violated on bar {bar_idx}: negative tail")
        if cut_len_sum + kerf_total + tail != bar.length:
            raise ValueError(f"conservation violated on bar {bar_idx}")
        reusable = tail >= tail_threshold
        total_loss += kerf_total + (0 if reusable else tail)
        if bar.source == "new_stock":
            total_cost += bar.price
        layouts.append(
            BarLayout(
                source=bar.source,
                source_id=bar.source_id,
                instance=bar.instance,
                material=bar.material,
                bar_length=bar.length,
                cuts=[Cut(demand_id=items[i].demand_id, instance=items[i].instance, length=items[i].length) for i in members],
                kerf_total=kerf_total,
                tail=tail,
                tail_reusable=reusable,
            )
        )
    metrics = Metrics(total_cost=total_cost, total_loss=total_loss, bars_used=len(layouts))
    return layouts, metrics
