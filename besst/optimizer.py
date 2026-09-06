"""Dynamic program over a state-of-charge grid. Exact for a price-taker with linear costs."""
from dataclasses import dataclass

import numpy as np

from .battery import Battery


@dataclass
class Plan:
    actions: np.ndarray       # T ints: +1 charge, 0 idle, -1 discharge
    soc: np.ndarray           # T+1 MWh, soc[0] is the starting state
    storage_value: np.ndarray # T AUD/MWh: worth of one more MWh in the tank. Charge pays when price is below it.
    sell_above: np.ndarray    # T AUD/MWh: price above which discharging pays (stored value / efficiency).


def dp_plan(prices, battery: Battery, soc0_mwh: float, terminal_price: float) -> Plan:
    p = np.asarray(prices, dtype=float)
    T, step, eff = len(p), battery.step_mwh, battery.efficiency
    S = int(round(battery.capacity_mwh / step)) + 1
    s0 = min(max(int(round(soc0_mwh / step)), 0), S - 1)  # ponytail: SoC snapped to the grid
    grid = np.arange(S) * step

    # Backward induction. V[t, s] = best revenue from interval t onward starting with s steps stored.
    V = np.empty((T + 1, S))
    V[T] = grid * terminal_price * eff
    best = np.zeros((T, S), dtype=np.int8)
    for t in range(T - 1, -1, -1):
        nxt = V[t + 1]
        v = nxt.copy()                      # idle
        charge = nxt[1:] - p[t] * step      # buy step MWh at p
        m = charge > v[:-1]
        v[:-1][m] = charge[m]; best[t, :-1][m] = 1
        disch = nxt[:-1] + p[t] * step * eff  # sell step MWh, efficiency haircut
        m = disch > v[1:]
        v[1:][m] = disch[m]; best[t, 1:][m] = -1
        V[t] = v
    # ponytail: three actions only. Partial power / degradation cost would add actions here.

    # Forward pass along the chosen path, reading the decision thresholds off the value function.
    actions = np.zeros(T, dtype=int)
    soc_idx = np.zeros(T + 1, dtype=int)
    soc_idx[0] = s0
    storage_value = np.empty(T)
    sell_above = np.empty(T)
    for t in range(T):
        s = soc_idx[t]
        actions[t] = best[t, s]
        soc_idx[t + 1] = s + actions[t]
        nxt = V[t + 1]
        up = (nxt[s + 1] - nxt[s]) / step if s < S - 1 else (nxt[s] - nxt[s - 1]) / step
        down = (nxt[s] - nxt[s - 1]) / step if s > 0 else up
        storage_value[t] = up
        sell_above[t] = down / eff
    return Plan(actions, soc_idx * step, storage_value, sell_above)
