import numpy as np
import pandas as pd

from besst.battery import Battery
from besst.optimizer import dp_plan
from besst.policy import causal, hindsight


def sine_prices(days=2, seed=0):
    idx = pd.date_range("2026-09-01 00:05", periods=288 * days, freq="5min")
    t = np.arange(len(idx))
    rng = np.random.default_rng(seed)
    p = 100 + 80 * np.cos(2 * np.pi * (t - 216) / 288) + rng.normal(0, 5, len(idx))  # trough ~06:00, peak ~18:00
    return pd.Series(p, index=idx, name="rrp")


def test_toy_series_obvious_optimum():
    b = Battery(power_mw=100, capacity_mwh=2 * 100 * 5 / 60, efficiency=1.0)
    plan = dp_plan([10, 10, 100, 100], b, 0.0, terminal_price=55)
    assert list(plan.actions) == [1, 1, -1, -1]
    revenue = sum(-p * b.step_mwh if a > 0 else p * b.step_mwh for p, a in zip([10, 10, 100, 100], plan.actions))
    assert abs(revenue - 2 * b.step_mwh * 90) < 1e-9
    assert plan.soc[-1] == 0


def test_hindsight_beats_causal_and_soc_in_bounds():
    prices, b = sine_prices(), Battery()
    h = hindsight(prices, b)
    # perfect 24 h forecast: hindsight still wins, because it optimises the whole series at once
    c = causal(prices, b, lambda at: prices.loc[at + pd.Timedelta("5min"):at + pd.Timedelta("24h")])
    assert h.cashflow_aud.sum() >= c.cashflow_aud.sum()
    for df in (h, c):
        assert df.soc_mwh.between(0, b.capacity_mwh).all()
        assert (df.mw.abs() <= b.power_mw).all()
    assert h.cashflow_aud.sum() > 0
