from app.accounting import build_layouts
from app.solver import Bar, Item, expand_bars, expand_items, solve


class D:
    def __init__(self, id, material, length, quantity):
        self.id, self.material, self.length, self.quantity = id, material, length, quantity


class S:
    def __init__(self, id, material, length, price, available):
        self.id, self.material, self.length, self.price, self.available = (
            id, material, length, price, available)


class R:
    def __init__(self, id, material, length):
        self.id, self.material, self.length = id, material, length


def run(demands, stock, remnants, kerf, threshold, budget=10000):
    items = expand_items(demands)
    bars = expand_bars(stock, remnants)
    res = solve(items, bars, kerf, threshold, budget)
    if res.assignment is None:
        return res, None, None
    layouts, metrics = build_layouts(items, bars, res.assignment, kerf, threshold)
    return res, layouts, metrics


def test_basic_optimal_and_conservation():
    res, layouts, metrics = run(
        [D("D1", "AL", 1200, 4), D("D2", "AL", 800, 3)],
        [S("N1", "AL", 6000, 15000, 3)], [], kerf=5, threshold=300)
    assert res.status == "OPTIMAL"
    assert metrics.total_cost == 30000  # 7200mm of parts need two 6000mm bars
    for lay in layouts:
        assert sum(c.length for c in lay.cuts) + lay.kerf_total + lay.tail == lay.bar_length


def test_exact_fit_last_cut_free():
    # 2 pieces of 995 + one kerf 10 = 2000 exactly -> only 1 kerf charged
    res, layouts, metrics = run([D("D1", "AL", 995, 2)], [S("N1", "AL", 2000, 100, 1)],
                                [], kerf=10, threshold=100)
    assert res.status == "OPTIMAL"
    assert layouts[0].kerf_total == 10
    assert layouts[0].tail == 0
    assert metrics.total_loss == 10


def test_kerf_charged_when_tail_remains():
    res, layouts, metrics = run([D("D1", "AL", 500, 2)], [S("N1", "AL", 2000, 100, 1)],
                                [], kerf=10, threshold=100)
    assert layouts[0].kerf_total == 20
    assert layouts[0].tail == 980
    assert layouts[0].tail_reusable
    assert metrics.total_loss == 20  # reusable tail not counted


def test_small_tail_counts_as_loss():
    res, layouts, metrics = run([D("D1", "AL", 900, 1)], [S("N1", "AL", 1000, 100, 1)],
                                [], kerf=10, threshold=300)
    assert layouts[0].tail == 90
    assert not layouts[0].tail_reusable
    assert metrics.total_loss == 10 + 90


def test_material_isolation():
    res, layouts, metrics = run(
        [D("D1", "AL", 1000, 1), D("D2", "SS", 500, 1)],
        [S("N1", "AL", 2000, 100, 1), S("N2", "SS", 2000, 100, 1)], [],
        kerf=0, threshold=0)
    assert res.status == "OPTIMAL"
    assert metrics.bars_used == 2
    mats = {lay.material for lay in layouts}
    assert mats == {"AL", "SS"}


def test_remnant_preferred_over_new_stock():
    res, layouts, metrics = run([D("D1", "AL", 900, 1)],
                                [S("N1", "AL", 6000, 5000, 1)],
                                [R("R1", "AL", 1000)], kerf=0, threshold=100)
    assert res.status == "OPTIMAL"
    assert metrics.total_cost == 0
    assert layouts[0].source == "remnant"


def test_cost_priority_over_loss():
    # cheap long bar vs expensive short bar: cost wins even with more loss
    res, layouts, metrics = run([D("D1", "AL", 1000, 1)],
                                [S("LONG", "AL", 9000, 100, 1), S("SHORT", "AL", 1100, 500, 1)],
                                [], kerf=0, threshold=100)
    assert res.status == "OPTIMAL"
    assert metrics.total_cost == 100
    assert layouts[0].source_id == "LONG"


def test_infeasible_when_nothing_fits():
    res, _, _ = run([D("D1", "AL", 9000, 1)], [S("N1", "AL", 1000, 100, 1)], [],
                    kerf=0, threshold=0)
    assert res.status == "INFEASIBLE"


def test_no_partial_layout_on_failure():
    res, layouts, metrics = run([D("D1", "AL", 9000, 2)], [S("N1", "AL", 1000, 100, 1)],
                                [], kerf=0, threshold=0)
    assert res.status == "INFEASIBLE"
    assert layouts is None


def test_limited_availability():
    res, _, _ = run([D("D1", "AL", 900, 4)], [S("N1", "AL", 1000, 100, 1)], [],
                    kerf=0, threshold=0)
    assert res.status == "INFEASIBLE"


def test_loss_minimization_second_priority():
    # two plans with equal cost (1 bar): prefer reusable tail (less loss)
    res, layouts, metrics = run([D("D1", "AL", 1000, 1)],
                                [S("N1", "AL", 2000, 100, 1)], [], kerf=0, threshold=500)
    assert res.status == "OPTIMAL"
    assert metrics.total_loss == 0  # tail 1000 >= threshold -> reusable
