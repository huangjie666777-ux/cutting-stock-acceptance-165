from decimal import Decimal

import pytest

from app.sampling import accept_prob, find_plan


def test_accept_prob_boundaries():
    # accept with c = n - 1 means rejection only when the whole sample is defective
    assert accept_prob(100, 100, 5, 4) == Decimal(0)
    assert accept_prob(100, 0, 5, 4) == Decimal(1)


def test_find_plan_known_case():
    # Classic-style lot N=100, AQL Dg=1, RQL Db=10, risks 0.05 / 0.10
    plan = find_plan(100, 1, 10, Decimal("0.05"), Decimal("0.10"))
    assert plan is not None
    n, c, prod, cons = plan
    assert 1 <= n <= 100 and 0 <= c < n
    assert prod <= Decimal("0.05")
    assert cons <= Decimal("0.10")


def test_minimal_n_then_minimal_c():
    plan_a = find_plan(50, 1, 5, Decimal("0.1"), Decimal("0.2"))
    n, c, _, _ = plan_a
    # No smaller sample size can satisfy both risks
    smaller_ok = False
    from math import comb
    for n2 in range(1, n):
        for c2 in range(n2):
            pa_good = accept_prob(50, 1, n2, c2)
            pa_bad = accept_prob(50, 5, n2, c2)
            if Decimal(1) - pa_good <= Decimal("0.1") and pa_bad <= Decimal("0.2"):
                smaller_ok = True
    assert not smaller_ok


def test_impossible_plan_returns_none():
    # Producer alpha=0 and consumer beta=0 with Dg=0, Db=1: c must be 0
    # (producer risk stays 0) and any defective in the sample rejects;
    # this is always achievable, so a plan must exist (never silently relaxed).
    plan = find_plan(10, 0, 1, Decimal("0"), Decimal("0"))
    assert plan is not None
    n, c, prod, cons = plan
    assert c == 0 and prod == 0 and cons == 0


def test_tight_risks_full_inspection():
    # alpha=0 forces c >= Dg; beta=0 with Db defects requires the sample to
    # guarantee detection (n + c >= N - Db + 1 + c ... smallest n found first).
    n, c, prod, cons = find_plan(20, 2, 5, Decimal("0"), Decimal("0"))
    assert c == 2 and n == 18  # 18 sampled, >2 defectives found with certainty
    assert prod == 0 and cons == 0
