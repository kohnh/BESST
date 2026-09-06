"""Plain-language Reason for each interval. Mirrors the DP rule: charge below storage value, sell above sell threshold."""
from math import ceil

from .battery import Battery


def reason(rrp: float, action: str, storage_value: float, sell_above: float,
           soc_mwh: float, battery: Battery, rank_pct: float) -> str:
    rank = (f"cheapest {max(1, ceil(rank_pct))}% of the day" if rank_pct <= 25
            else f"dearest {max(1, ceil(100 - rank_pct))}% of the day" if rank_pct >= 75
            else "mid-range for the day")  # a percentile near 50 says nothing; don't dress it up as a signal
    fill = f"Battery now {100 * soc_mwh / battery.capacity_mwh:.0f}% full."
    price = f"${rrp:,.0f}/MWh"
    if action == "charge":
        return f"Charged at {price} ({rank}), below the ${storage_value:,.0f}/MWh value of stored energy. {fill}"
    if action == "discharge":
        return f"Discharged at {price} ({rank}), above the ${sell_above:,.0f}/MWh sell threshold. {fill}"
    eps = battery.step_mwh / 2
    if soc_mwh >= battery.capacity_mwh - eps and rrp < storage_value:
        return f"Held at {price} ({rank}): would charge, but the battery is full."
    if soc_mwh <= eps and rrp > sell_above:
        return f"Held at {price} ({rank}): would discharge, but the battery is empty."
    return (f"Held at {price} ({rank}): between the ${storage_value:,.0f}/MWh buy threshold "
            f"and the ${sell_above:,.0f}/MWh sell threshold. {fill}")
