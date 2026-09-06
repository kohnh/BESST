# Domain Glossary — BESS NSW1 Arbitrage

Vocabulary used across code, UI, and docs. Implementation details do not belong here.

| Term | Meaning |
|------|---------|
| **NEM** | National Electricity Market, eastern and southern Australia, operated by AEMO. |
| **Region** | One of the five NEM price zones: QLD1, NSW1, VIC1, SA1, TAS1. This project uses NSW1. |
| **Dispatch Interval** | A five-minute period for which AEMO dispatches the market and publishes a price. 288 per day. Timestamps are AEST market time, no daylight saving. |
| **RRP** | Regional Reference Price, AUD per MWh, published per Region per Dispatch Interval. The only price signal the battery trades against. |
| **Trading Day** | The NEM day, starting 04:00 AEST. The dashboard uses calendar days 00:00–24:00 AEST for display. |
| **Battery** | The BESS being operated: a capacity (MWh), a power rating (MW), and a round-trip efficiency. It starts every Simulation empty. |
| **State of Charge (SoC)** | Energy currently stored in the Battery, in MWh, between zero and capacity. |
| **Action** | What the Battery does during one Dispatch Interval: charge, discharge, or idle, expressed as MW. |
| **Schedule** | The sequence of Actions across all Dispatch Intervals in the horizon. |
| **Policy** | A rule that produces an Action for each Dispatch Interval. A Hindsight Policy sees all prices in the horizon. A Causal Policy sees only prices up to the current interval. |
| **Simulation** | Running a Policy over a price series with a Battery to produce a Schedule, SoC trajectory, and Revenue. |
| **Revenue** | Sum over the horizon of energy sold times RRP minus energy bought times RRP, in AUD. |
| **Hindsight Gap** | Revenue of the Hindsight Policy minus Revenue of a Causal Policy over the same horizon. |
| **Reason** | A human-readable explanation attached to each Action, saying why the Battery charged, discharged, or idled at that interval. |
| **24-hour Overlay** | Display where each selected day is drawn as its own trace on a shared 00:00–24:00 axis. |
| **Actual Price** | The RRP AEMO published for a Dispatch Interval that has already been dispatched. |
| **Forecast Price** | AEMO's predicted RRP for a future Dispatch Interval. Two sources: the five-minute forecast (one hour ahead, refreshed every five minutes) and Predispatch (about 25 hours ahead at half-hour resolution, refreshed every half hour). |
| **Forecast Horizon** | The span of future Dispatch Intervals for which a Forecast Price exists. |
| **Plan** | The Schedule a Policy intends to follow over the Forecast Horizon. Only the first Action of a Plan is executed before the Plan is recomputed. |
| **Storage Value** | The marginal worth, in AUD per MWh, of one more MWh in the Battery at a given Dispatch Interval. Charging is worthwhile when RRP is below it, discharging when RRP is above it. |
| **Ingest Job** | The recurring task that fetches newly published AEMO files, parses them, and stores them. Runs every Dispatch Interval. |
| **Live View** | Dashboard view showing today's Actual Prices so far, the Forecast Price ahead, and the current Plan. |
| **Backtest View** | Dashboard view showing a past period with the Hindsight Policy Schedule, a Causal Policy Schedule, and the Hindsight Gap. |
| **Hindsight Policy** | Policy that knows every Actual Price in the horizon before choosing. Its Revenue is the upper bound a perfect trader could have earned. |
| **Causal Policy** | Policy that at each Dispatch Interval uses only Actual Prices up to that interval plus Forecast Prices. This is how a real Battery operates. |
| **Receding Horizon** | Operating pattern where a Plan is computed over the Forecast Horizon, its first Action executed, then the Plan is recomputed at the next Dispatch Interval with the new SoC and fresh forecasts. Energy carries across day boundaries. |
| **Terminal Value** | The worth assigned to energy still in the Battery at the end of a finite horizon, so the Policy does not sell it off cheaply just because the horizon ends. |
| **Forecast Run** | One publication of Forecast Prices at a given time. Predispatch produces a new Forecast Run every half hour, each covering the next ~25 hours. |
| **As-of Forecast** | For a given decision time, the most recent Forecast Run published at or before that time. This is the only forecast a Causal Policy is allowed to see. |
| **Data Age** | Time elapsed since the newest Actual Price in the store. Shown on the dashboard; a warning appears past ten minutes. |

