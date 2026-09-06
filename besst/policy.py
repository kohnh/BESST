"""Policies: hindsight (one DP over actuals) and causal (receding-horizon DP over a forecast)."""
import numpy as np
import pandas as pd

from .battery import Battery
from .explain import reason
from .optimizer import dp_plan

ACTION = {1: "charge", 0: "idle", -1: "discharge"}
FIVE = pd.Timedelta("5min")


def frame(prices: pd.Series, battery: Battery, actions, soc, storage_value, sell_above, ref=None) -> pd.DataFrame:
    """`ref` supplies the day's other prices when `prices` is only part of a day, e.g. a live plan that starts
    mid-evening: without it every interval would be ranked against the sliver of day the horizon happens to cover."""
    a = np.asarray(actions)
    step = battery.step_mwh
    cash = np.where(a > 0, -prices.values * step,
                    np.where(a < 0, prices.values * step * battery.efficiency, 0.0))
    df = pd.DataFrame({
        "rrp": prices.values,
        "action": [ACTION[int(x)] for x in a],
        "mw": a * battery.power_mw,
        "soc_mwh": soc[1:],
        "cashflow_aud": cash,
        "storage_value": storage_value,
        "sell_above": sell_above,
    }, index=prices.index)
    # labels are period-end: the 00:00 row belongs to the previous day
    allp = prices if ref is None else pd.concat([ref, prices]).groupby(level=0).last().sort_index()
    rank = (allp.groupby((allp.index - FIVE).normalize()).rank(pct=True) * 100).reindex(prices.index)
    df["reason"] = [reason(r.rrp, r.action, r.storage_value, r.sell_above, r.soc_mwh, battery, pct)
                    for r, pct in zip(df.itertuples(), rank)]
    return df


def hindsight(prices: pd.Series, battery: Battery) -> pd.DataFrame:
    """Upper bound: every price known in advance. Leftover energy is worth nothing (terminal price 0), so the
    plan only buys what it later sells and the horizon ends empty. Revenue is realised cash, not paper stock."""
    plan = dp_plan(prices.values, battery, 0.0, 0.0)
    return frame(prices, battery, plan.actions, plan.soc, plan.storage_value, plan.sell_above)


def causal(prices: pd.Series, battery: Battery, forecast_fn) -> pd.DataFrame:
    """At each interval the battery knows the current dispatch price (AEMO publishes it as the interval
    starts) plus forecast_fn(t) for later intervals. It plans over that horizon and commits one action."""
    T = len(prices)
    actions = np.zeros(T, dtype=int)
    soc = np.empty(T + 1); soc[0] = 0.0
    storage_value = np.full(T, np.nan); sell_above = np.full(T, np.nan)
    for t in range(T):
        fc = forecast_fn(prices.index[t])
        horizon = np.concatenate([[prices.iloc[t]], fc.values]) if len(fc) else np.array([prices.iloc[t]])
        plan = dp_plan(horizon, battery, soc[t], float(horizon.mean()))
        actions[t] = plan.actions[0]
        storage_value[t], sell_above[t] = plan.storage_value[0], plan.sell_above[0]
        soc[t + 1] = plan.soc[1]
    # ponytail: one DP per interval, ~5 ms each. Cache or numba if 7-day backtests feel slow in the UI.
    return frame(prices, battery, actions, soc, storage_value, sell_above)
