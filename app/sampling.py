"""Exact hypergeometric sampling plans for lot acceptance.

All probabilities are computed as exact fractions of integer binomial
coefficients: no binomial approximation, no floating-point slack on the
risk constraints.

Given a lot of N items with D defectives, drawing n items without
replacement and accepting when at most c defectives are found:

    P(accept | N, D, n, c) = sum_{k=0}^{c} C(D,k) C(N-D,n-k) / C(N,n)

The plan search minimizes n first, then c, subject to:
    P(reject | D = Dg) <= alpha   (producer risk)
    P(accept | D = Db) <= beta    (consumer risk)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from math import comb


@dataclass(frozen=True)
class SamplingPlan:
    population: int
    sample_size: int  # n
    acceptance: int  # c
    producer_risk: Fraction  # actual P(reject | Dg)
    consumer_risk: Fraction  # actual P(accept | Db)


def parse_risk(value: object) -> Fraction:
    """Parse a decimal string strictly between 0 and 1 into an exact Fraction."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("risk must be a decimal string")
    try:
        dec = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"risk is not a decimal string: {value!r}") from None
    if not dec.is_finite() or dec <= 0 or dec >= 1:
        raise ValueError(f"risk must be strictly between 0 and 1: {value!r}")
    return Fraction(dec)


def acceptance_probability(population: int, defectives: int, sample_size: int, acceptance: int) -> Fraction:
    """Exact P(finding at most 'acceptance' defectives in the sample)."""
    total = comb(population, sample_size)
    good = population - defectives
    numerator = 0
    for k in range(0, min(acceptance, defectives) + 1):
        if sample_size - k <= good:
            numerator += comb(defectives, k) * comb(good, sample_size - k)
    return Fraction(numerator, total)


def find_plan(population: int, dg: int, db: int, alpha: Fraction, beta: Fraction) -> SamplingPlan:
    """Smallest n, then smallest c, satisfying both risk constraints exactly."""
    if not (0 <= dg < db <= population):
        raise ValueError(f"require 0 <= Dg < Db <= N, got Dg={dg}, Db={db}, N={population}")
    for n in range(1, population + 1):
        for c in range(0, n):
            producer = 1 - acceptance_probability(population, dg, n, c)
            if producer > alpha:
                continue
            consumer = acceptance_probability(population, db, n, c)
            if consumer <= beta:
                return SamplingPlan(population, n, c, producer, consumer)
    # Unreachable: n = N, c = N - 1 always satisfies both constraints.
    raise ValueError("no sampling plan satisfies the given risks")


def risk_str(risk: Fraction) -> str:
    """Decimal string of an exact risk fraction (40 significant digits)."""
    with localcontext() as ctx:
        ctx.prec = 40
        dec = Decimal(risk.numerator) / Decimal(risk.denominator)
    return format(dec.normalize(), "f")
