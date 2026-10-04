"""Exact hypergeometric acceptance-sampling plan search.

Lot size N with D defectives; a sample of n pieces (without replacement)
contains X ~ Hypergeometric(N, D, n). The lot is accepted iff X <= c.

Producer risk  = P(reject | D = Dg) = 1 - sum_{k<=c} C(Dg,k) C(N-Dg,n-k) / C(N,n)
Consumer risk  = P(accept | D = Db) =     sum_{k<=c} C(Db,k) C(N-Db,n-k) / C(N,n)

All comparisons use exact integer combinatorics and Decimal risks; the
binomial approximation and floating-point tolerance relaxation are never used.
"""

from __future__ import annotations

from decimal import Decimal, getcontext
from math import comb
from typing import Optional, Tuple

getcontext().prec = 60


def _accept_numerator(N: int, D: int, n: int, c: int) -> int:
    """Numerator of P(X <= c): sum of exact integer hypergeometric counts."""
    good = N - D
    k_lo = max(0, n - good)
    k_hi = min(c, D, n)
    total = 0
    for k in range(k_lo, k_hi + 1):
        total += comb(D, k) * comb(good, n - k)
    return total


def accept_prob(N: int, D: int, n: int, c: int) -> Decimal:
    if D < 0 or D > N:
        raise ValueError("defectives D out of range")
    return Decimal(_accept_numerator(N, D, n, c)) / Decimal(comb(N, n))


def find_plan(
    N: int, Dg: int, Db: int, alpha: Decimal, beta: Decimal
) -> Optional[Tuple[int, int, Decimal, Decimal]]:
    """Smallest n, then smallest c, satisfying producer/consumer risk limits.

    Returns (n, c, producer_risk, consumer_risk) or None if no plan exists.
    """
    for n in range(1, N + 1):
        for c in range(0, n):
            denom = comb(N, n)
            pa_good = Decimal(_accept_numerator(N, Dg, n, c)) / Decimal(denom)
            pa_bad = Decimal(_accept_numerator(N, Db, n, c)) / Decimal(denom)
            producer_risk = Decimal(1) - pa_good
            if producer_risk <= alpha and pa_bad <= beta:
                return n, c, producer_risk, pa_bad
    return None
