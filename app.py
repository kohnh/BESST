"""Trader-facing dashboard: Live plan over AEMO forecasts, and a Backtest of hindsight vs causal."""
import functools
import os
import socket
import sys
import threading
import webbrowser

import dash
import dash_mantine_components as dmc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

from besst import ingest, store
from besst.battery import Battery
from besst.optimizer import dp_plan
from besst.policy import frame, causal, hindsight

# Colour has one job each: ink = what happened, blue = what the model expects, orange = money and attention
# (selling, rising, stale), green = buying, violet = state of charge, grey = context. Violet is the one hue far
# enough from all of the above to read as its own thing. Validated with the dataviz palette script.
TH = {
    False: dict(ink="#1f2330", muted="#5c6377", blue="#4a66d2", hot="#c8552a", cool="#1f8f6e", soc="#8b3fb8", ctx="#9298aa",
                ctxfill="rgba(182,186,199,0.26)", grid="#dfe2ea", tip="#ffffff"),
    True: dict(ink="#eceef4", muted="#9aa0b2", blue="#6f89e6", hot="#d9683a", cool="#25a27a", soc="#b57ae0", ctx="#5a6070",
               ctxfill="rgba(120,126,146,0.24)", grid="#33374a", tip="#2a2e38"),
}
REGION, REGION_NAME = "NSW1", "New South Wales"   # ponytail: one region; the store schema is per-region if a second is ever wanted
DAY = pd.Timedelta("1D")
FIVE = pd.Timedelta("5min")
TAPE = 0.07  # share of a panel given to the plan tape, when the tape sits inside it (backtest)
PRICE_DOM, SOC_DOM = (0.33, 1.0), (0.0, 0.23)   # paper-fraction bands of the live chart's two panels
TAPE_PAPER = (0.265, 0.30)   # the tape's own lane in the strip between them, clear of both panels
# Drawn there rather than inside the price panel so a price dip cannot run behind it. Shapes are placed in the
# price axis's own domain units, so convert; negative is below that axis's floor.
TAPE_BAND = tuple((p - PRICE_DOM[0]) / (PRICE_DOM[1] - PRICE_DOM[0]) for p in TAPE_PAPER)


def day_of(idx):
    """Calendar day an interval belongs to. Labels are period-end, so the 00:00 row is the previous day's."""
    return (idx - FIVE).normalize()


def aud(x):
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def hm(clock):
    """Time-of-day format for both strftime and plotly's d3 formatter."""
    return "%H:%M" if clock == "24" else "%I:%M %p"


def interval_label(end, fmt):
    """The span a price covers, e.g. '02:25-02:30 AM AEST'. Labels are period-end, so a bare label reads as a
    clock running five minutes fast. The meridiem is printed once unless the interval crosses noon or midnight."""
    a, b = format(end - FIVE, fmt), format(end, fmt)
    if a[-2:] == b[-2:] and fmt.endswith("%p"):
        a = a[:-3]
    return f"{a}\u2013{b} AEST"


def act_colors(t, swap):
    """(charge, discharge) colours. The settings toggle decides which side is green."""
    return (t["hot"], t["cool"]) if swap else (t["cool"], t["hot"])


def runs(actions: pd.Series):
    """Contiguous (start, end, action) blocks of non-idle actions."""
    out, prev, start = [], "idle", None
    for t, a in actions.items():
        if a != prev:
            if prev != "idle":
                out.append((start, t, prev))
            start, prev = t, a
    if prev != "idle":
        out.append((start, actions.index[-1] + FIVE, prev))
    return out


# ---------- chart pieces ----------

def tape(fig, df, t, full=False, swap=False, band=(0, TAPE)):
    """The plan as a tape along the bottom of the price panel; `full` also washes the whole panel.

    The rects go on in one assignment rather than via add_vrect: each add_vrect revalidates every shape already on the
    figure, so a day of five-minute runs costs seconds. These are the dicts add_vrect builds for row=1, col=1.
    """
    chg, dis = act_colors(t, swap)
    rects = []
    for s, e, a in runs(df.action):
        kw = dict(type="rect", xref="x", yref="y domain", x0=s - FIVE, x1=e - FIVE,
                  fillcolor=chg if a == "charge" else dis, line=dict(width=0), layer="below")
        rects.append(dict(kw, y0=band[0], y1=band[1]))  # opaque: a translucent rect antialiases its edges
        if full:
            rects.append(dict(kw, y0=max(band[1], 0), y1=1, opacity=0.16))
    fig.layout.shapes = fig.layout.shapes + tuple(rects)


def threshold_band(fig, df, t, swap=False):
    """The two decision lines: sell above one, buy below the other, hold in between."""
    chg, dis = act_colors(t, swap)
    edge = lambda y, c: go.Scatter(x=df.index, y=y, line=dict(color=c, width=1.4, dash="dot"), showlegend=False,
                                   hovertemplate="$%{y:,.2f}/MWh<extra></extra>")
    fig.add_trace(edge(df.sell_above, dis), row=1, col=1)
    fig.add_trace(edge(df.storage_value, chg), row=1, col=1)


def typical_band(fig, typical, t):
    fig.add_trace(go.Scatter(x=typical.index, y=typical[0.9], line=dict(width=0), hoverinfo="skip", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=typical.index, y=typical[0.1], line=dict(width=0), fill="tonexty", fillcolor=t["ctxfill"],
                             hoverinfo="skip", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=typical.index, y=typical[0.5], mode="lines", line=dict(color=t["ctx"], width=1.5, dash="dot"),
                             hovertemplate="$%{y:,.2f}/MWh<extra></extra>", showlegend=False), row=1, col=1)


def price_trace(df, name, color, dash_style=None):
    return go.Scatter(x=df.index, y=df.rrp, name=name, mode="lines", line=dict(color=color, width=2.4, dash=dash_style),
                      showlegend=False, hovertemplate="$%{y:,.2f}/MWh<extra></extra>")


def now_pill(fig, x, t, fmt):
    fig.add_shape(type="line", x0=x, x1=x, xref="x", y0=0, y1=1, yref="paper", line=dict(color=t["muted"], width=1, dash="dot"))
    fig.add_annotation(x=x, y=1.0, yref="y domain", text=format(x, fmt), showarrow=False, xanchor="center", yanchor="bottom",
                       font=dict(color=t["tip"], size=11, family="Manrope"), bgcolor=t["ink"], borderpad=6, row=1, col=1)


def base_layout(fig, height, dark, fmt="%H:%M"):
    t = TH[dark]
    fig.update_layout(
        height=height, margin=dict(l=44, r=16, t=44, b=36), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Manrope, system-ui, sans-serif", size=12, color=t["muted"]), hovermode="x unified", showlegend=False,
        hoverlabel=dict(bgcolor=t["tip"], font=dict(color=t["ink"], family="Manrope"), bordercolor=t["grid"], align="left"),
    )
    fig.update_xaxes(type="date", showgrid=False, zeroline=False, showline=False, ticks="", hoverformat=f"{fmt} · %a")
    fig.update_yaxes(gridcolor=t["grid"], gridwidth=1, zeroline=False, showline=False, ticks="", tickprefix="")
    return fig


def legend(*items, **kw):
    """Legend items for what is actually drawn; the live chart re-renders it as layer chips toggle."""
    return html.Div([html.Span([html.I(className=f"sw {kind}", style=style), label]) for kind, style, label in items], className="legend", **kw)


LEG = dict(
    actual=("", {"background": "var(--ink)"}, "Actual price"),
    forecast=("dash", {}, "AEMO forecast"),
    typical=("box", {"background": "var(--ctx)", "opacity": .55}, "Typical range · prior 7 days"),
    typmid=("dot", {}, "Typical median"),
    buy=("", {"background": "repeating-linear-gradient(90deg, var(--chg) 0 4px, transparent 4px 8px)"}, "Buy below"),
    sell=("", {"background": "repeating-linear-gradient(90deg, var(--dis) 0 4px, transparent 4px 8px)"}, "Sell above"),
    charge=("box", {"background": "var(--chg)"}, "Charging · buying"),
    discharge=("box", {"background": "var(--dis)"}, "Discharging · selling"),
    hindsight=("", {"background": "var(--ink)"}, "Hindsight"),
    causal=("", {"background": "var(--blue)"}, "Causal · AEMO forecast"),
)


# ---------- computations (cached on hashable args) ----------

def battery(power, cap, eff_pct):
    return Battery(float(power), float(cap), float(eff_pct) / 100)


def forecast_stamp(conn):
    """Newest forecast run in the store; part of the cache key so results refresh as the ingest fills forecasts in."""
    return str(conn.execute("SELECT max(run_datetime) FROM predispatch_price UNION ALL SELECT max(run_datetime) FROM p5min_price").fetchall())


@functools.lru_cache(maxsize=16)
def backtest(start, end, region, power, cap, eff_pct, _stamp):
    conn = store.connect()
    bat = battery(power, cap, eff_pct)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end) + DAY
    prices = store.load_prices(conn, t0, t1, region)
    if prices.empty:
        return None
    h = hindsight(prices, bat)
    has_fc = conn.execute("SELECT 1 FROM predispatch_price WHERE regionid=? AND run_datetime BETWEEN ? AND ? LIMIT 1",
                          (region, store.iso(t0), store.iso(t1))).fetchone()
    # ponytail: causal makes one small SQLite query per interval (~2016 for a week). Preload if it drags.
    c = causal(prices, bat, lambda at: store.load_forecast_asof(conn, at, region)) if has_fc else None
    return h, c


@functools.lru_cache(maxsize=8)
def live(newest_iso, region, power, cap, eff_pct, _stamp):
    conn = store.connect()
    bat = battery(power, cap, eff_pct)
    newest = pd.Timestamp(newest_iso)
    today = day_of(newest)
    # A day back, not just today's midnight: the chart's left half must not go blank after midnight, and the causal
    # sim has to carry SoC across the boundary. ±24 h is the widest preset, so one prior day always covers it.
    px = store.load_prices(conn, today - DAY, newest, region)
    so_far = causal(px, bat, lambda at: store.load_forecast_asof(conn, at, region)) if len(px) else None
    soc_now = float(so_far.soc_mwh.iloc[-1]) if so_far is not None else 0.0
    fc = store.load_forecast_asof(conn, newest, region)
    plan = None
    if not fc.empty:
        p = dp_plan(fc.values, bat, soc_now, float(fc.mean()))
        plan = frame(fc, bat, p.actions, p.soc, p.storage_value, p.sell_above, ref=px)
    prior = []
    for k in range(1, 8):
        d = today - k * DAY
        s = store.load_prices(conn, d - DAY, d + 2 * DAY, region)   # 72 h: a day back for the chart, two forward for the forecast span
        if len(s):
            s.index = s.index + k * DAY
            prior.append(s)
    # 30-min rolling mean on the quantiles: the band is context, not a signal, so it should not read as noise
    typical = pd.concat(prior, axis=1).quantile([0.1, 0.5, 0.9], axis=1).T.rolling(6, center=True, min_periods=1).mean() if prior else None
    return px, so_far, plan, typical, len(prior)


# ---------- layout ----------

conn0 = store.connect()
first, last = conn0.execute("SELECT min(settlementdate), max(settlementdate) FROM dispatch_price WHERE regionid=?", (REGION,)).fetchone()
MIN_DAY = pd.Timestamp(first).normalize() if first else pd.Timestamp.today().normalize()
MAX_DAY = (pd.Timestamp(last) - FIVE).normalize() if last else MIN_DAY
LAST_FULL = MAX_DAY if last and pd.Timestamp(last) == MAX_DAY + DAY else MAX_DAY - DAY
BT_START, BT_END = (max(MIN_DAY, LAST_FULL - 6 * DAY).date().isoformat(),  # ponytail: fixed last-7-days window
                    max(MIN_DAY, LAST_FULL).date().isoformat())

try:
    dash._dash_renderer._set_react_version("18.2.0")
except Exception:  # newer Dash already ships React 18
    pass
app = dash.Dash(__name__, title="BESST Dashboard")

# Paint the stored theme before React mounts. Dash restores `persistence` from localStorage only after the first
# render, and dmc mounts light until the theme callback answers, so a refresh flashed light before going dark.
# Reading Dash's own key keeps the switch the single source of truth; if that key format ever changes the try
# fails and we are back to the flash, not a broken page.
app.index_string = dash.dash._default_index.replace("{%css%}", """{%css%}
    <script>
      try {
        if (JSON.parse(localStorage.getItem("_dash_persistence.dark.checked.true"))[0]) {
          document.documentElement.dataset.mantineColorScheme = "dark";   // our CSS vars key off this
          localStorage.setItem("mantine-color-scheme-value", "dark");     // and dmc mounts from this
        }
      } catch (e) {}
    </script>""")


def seg(id_, value, options, size="sm", **kw):
    return dmc.SegmentedControl(id=id_, value=value, size=size, data=[{"label": l, "value": v} for l, v in options], **kw)


def num(id_, label, value, **kw):
    # Debounced: every battery input reruns the causal sim (~0.7 s over the 48 h window), so an undebounced
    # stepper held down queues one full recompute per click and the tiles trail the box by seconds.
    return dmc.NumberInput(id=id_, label=label, value=value, size="sm", debounce=400, **kw)


def setting(label, control):
    return html.Div([html.Span(label, className="eyebrow"), html.Span(className="spacer"), control], className="row set-row")


CLS = {"charge": "v chg", "discharge": "v dis", "idle": "v muted"}


def tile(id_, label, cls="neu-in", sub=True):
    """`sub=False` drops the caption line entirely; leaving it empty would leave the loading shimmer running."""
    kids = [html.Div(label, className="eyebrow"), html.Div(id=id_, className="v")]
    return html.Div(kids + ([html.Div(id=id_ + "-s", className="s")] if sub else []),
                    className=f"{cls} tile", style={"flex": 1})


def blank():
    """Placeholder figure: plotly's default empty axes are a white box with 1-4 on it, which reads as real data."""
    return go.Figure(layout=dict(margin=dict(l=44, r=16, t=44, b=36), paper_bgcolor="rgba(0,0,0,0)",
                                 plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(visible=False), yaxis=dict(visible=False)))


def graph(id_, height, **config):
    """A graph that shimmers while its callback runs instead of showing a stale or empty canvas.

    `height` is a CSS length and is the only place a chart's height is set: the callbacks' figures carry none, and
    responsive=True sizes the plot to this box. Without it the box is height:100% of an auto-height card, i.e. 0.
    """
    return dcc.Loading(dcc.Graph(id=id_, figure=blank(), responsive=True, style={"height": height},
                                 config={"displayModeBar": False, **config}),
                       custom_spinner=html.Div(className="skel", style={"height": height}),
                       # 1.2 s, not 200 ms: a live redraw is ~0.75 s cold, and swapping a drawn chart for the
                       # skeleton every 5 minutes reads as the page reloading. Only a genuinely cold load shimmers.
                       parent_className="skel-wrap", delay_show=1200)


def card(*children, cls="", **kw):
    return html.Div(list(children), className=f"neu {cls}".strip(), **kw)


topbar = html.Div([
    html.Div([html.Span(className="dot"), "BESST Dashboard"], className="brand"),
    seg("view", "live", [("Live", "live"), ("Backtest", "bt")]),
    html.Div(className="spacer"),
    html.Div(id="age-pill", className="pill ok neu-in"),
    # native <details> for the panel, assets/settings.js for the outside click. dmc 0.14 Popover would do both,
    # but it reads _dashprivate_layout, which dash 4 no longer sets.
    html.Details([
        html.Summary("⚙", className="gear", title="Settings", **{"aria-label": "Settings"}),
        html.Div([
            setting("Dark mode", dmc.Switch(id="dark", checked=False, size="md", onLabel="☾", offLabel="☀",
                                            persistence=True, **{"aria-label": "Dark mode"})),
            setting("Clock", seg("clock", "24", [("24 h", "24"), ("12 h", "12")], size="xs", persistence=True)),
            setting("Trade colours", seg("swap", "std", [("Green buys", "std"), ("Green sells", "swap")], size="xs", persistence=True)),
        ], className="neu set-panel"),
    ], className="set"),
], className="glass topbar")

spot_card = card(
    # region name and interval share a line; the code sits under it, so the interval span never has to wrap
    html.Div([html.Span(REGION_NAME, className="eyebrow"), html.Span(className="spacer"),
              html.Span(id="spot-time", className="eyebrow")], className="row"),
    html.Div(REGION, className="eyebrow"),
    html.Div("Spot now", className="eyebrow spot-label"),
    html.Div(id="spot", className="hero"),
    html.Div(id="spot-s", className="sub"),
    html.Div([tile("f1", "Forecast +1h"), tile("nextact", "Next action"), tile("stored", "Stored"), tile("now", "Battery now", sub=False),
              tile("cost", "Est. revenue"), tile("thr", "Trade band")], className="tiles"),
    cls="spot-card",
)

battery_card = card(
    html.H3("Battery", className="card-title"),
    html.Div([html.Div([num("power", "Power (MW)", 100, min=1, max=1000, step=10),
                        html.Div(id="duration", className="sub", style={"marginTop": "6px", "fontSize": "12px"})]),
              num("cap", "Capacity (MWh)", 200, min=1, max=5000, step=10),
              num("eff", "Round-trip efficiency (%)", 90, min=50, max=100, step=1)],
             className="inputs"),
)

chart_card = card(
    html.Div([html.H3("Price and plan", className="card-title"), html.Span(className="spacer"),
              seg("win", "12", [("±6 h", "6"), ("±12 h", "12"), ("±24 h", "24"), ("Today", "all")], size="xs")], className="row"),
    graph("live-fig", "clamp(460px, 70vh, 840px)", doubleClick=False),
    html.Button(id="rst", n_clicks=0, hidden=True),
    legend(LEG["actual"], LEG["forecast"], LEG["charge"], LEG["discharge"], LEG["buy"], LEG["sell"], id="live-legend"),
    html.Div([html.Span("Layers", className="eyebrow"),
              dmc.ChipGroup(id="layers", multiple=True, value=["band"], persistence=True, children=dmc.Group([
                  dmc.Chip("Thresholds", value="band", size="sm"), dmc.Chip("Typical range", value="typical", size="sm"),
                  dmc.Chip("Shade charge/discharge", value="plan", size="sm")], gap="sm"))],
             className="row", style={"marginTop": "6px"}),
    html.Div("Drag across the chart to zoom into a window; drag along an axis to zoom that axis alone. Double-click to reset.",
             className="sub", style={"marginTop": "10px", "fontSize": "12px"}),
)

live_view = html.Div([html.Div([spot_card, battery_card], className="stack"),
                      html.Div([chart_card], className="stack")], className="grid", id="live-view")

bt_view = html.Div([
    card(html.Div([html.Div([html.Span("Range (AEST)", className="eyebrow"),
                             html.Span(f"{pd.Timestamp(BT_START):%d %b} – {pd.Timestamp(BT_END):%d %b %Y}", className="val")], className="row"),
                   html.Span(className="spacer"),
                   html.Span("Schedule shown", className="eyebrow"),
                   seg("which", "hindsight", [("Hindsight", "hindsight"), ("Causal", "causal")], size="xs")], className="row")),
    html.Div([tile("k-h", "Hindsight revenue", "neu"), tile("k-c", "Causal revenue · AEMO forecast", "neu")],
             className="row", style={"gap": "22px", "flexWrap": "nowrap"}),
    card(html.H3(id="bt-title", className="card-title"),
         legend(LEG["actual"], LEG["charge"], LEG["discharge"], LEG["buy"], LEG["sell"]),
         graph("bt-fig", "clamp(320px, 52vh, 700px)"),
         html.Div("Drag the slider under the chart to zoom into a day.", className="sub", style={"fontSize": "12px"})),
    card(html.H3("Cumulative revenue", className="card-title"), legend(LEG["hindsight"], LEG["causal"]),
         graph("cum-fig", "clamp(200px, 32vh, 420px)")),
    html.Div([card(html.H3("Revenue by day", className="card-title"), html.Div(id="day-table", style={"marginTop": "12px"})),
              card(html.H3("Worst causal decisions vs hindsight", className="card-title"), html.Div(id="worst-table", style={"marginTop": "12px"}))],
             className="grid", style={"gridTemplateColumns": "5fr 7fr"}),
], className="stack", id="bt-view", hidden=True)

app.layout = dmc.MantineProvider(
    html.Div([dcc.Interval(id="tick", interval=30_000), dcc.Interval(id="fetch-tick", interval=1_000), dcc.Store(id="seen"), dcc.Store(id="shown"), topbar,
              html.Div(className="set-backdrop"),   # outside the glass topbar, which is its own backdrop root
              live_view, bt_view], className="page", id="page"),
    id="mp", theme={"fontFamily": "Manrope, system-ui, sans-serif", "primaryColor": "indigo"},
)


def table(head, body):
    return dmc.Table(data={"head": head, "body": body}, highlightOnHover=True, fz="sm", verticalSpacing=8)


@app.callback(Output("mp", "forceColorScheme"), Output("page", "className"), Input("dark", "checked"), Input("swap", "value"))
def theme(dark, swap):
    return ("dark" if dark else "light"), "page swap" if swap == "swap" else "page"


@app.callback(Output("live-view", "hidden"), Output("bt-view", "hidden"), Input("view", "value"))
def switch_view(view):
    return view != "live", view != "bt"


# ---------- Live ----------

# ponytail: the ingest runs as a daemon thread in this process, so there is nothing to cron and nothing to babysit.
# Move it back out to `python -m besst.ingest --loop` if the dashboard ever runs multi-worker.
# BESST_DEMO (set by `make demo`) leaves it off, so the snapshot demo makes no network calls at all.
if not os.environ.get("BESST_DEMO"):
    threading.Thread(target=lambda: ingest.loop(store.connect()), daemon=True).start()


@app.callback(Output("age-pill", "children"), Output("age-pill", "className"),
              Input("fetch-tick", "n_intervals"), State("shown", "data"))
def fetch_pill(_, shown):
    """Data Age: how stale the price on screen is. It counts from the newest interval the chart has drawn, not
    from the newest row in the store, so the badge can only reset once the chart has caught up with it. A stalled
    fetcher and a silent nemweb both show up here; whether the poll itself succeeded is not what a trader needs."""
    if shown is None:
        return [html.Span(className="dot"), "Waiting for data…"], "pill neu-in warn"
    # settlementdate labels the period END, and AEMO publishes a dispatch price as its interval starts, so the
    # newest label sits up to five minutes in the future. Age counts from when the price took effect.
    s = max(0, int((store.now_market() - (pd.Timestamp(shown) - store.STEP)).total_seconds()))
    # the digits live in their own tabular-nums span, zero-padded, so the dot and the words never shift as it counts
    return ([html.Span(className="dot"), "Data ", html.Span(f"{s // 60:02d}m {s % 60:02d}s", className="num"), " old"],
            "pill neu-in " + ("ok" if s < 600 else "warn"))   # two missed dispatch intervals


@app.callback(
    Output("live-fig", "figure"), Output("live-legend", "children"), Output("seen", "data"), Output("shown", "data"),
    Output("spot-time", "children"), Output("spot", "children"), Output("spot-s", "children"),
    Output("f1", "children"), Output("f1-s", "children"), Output("nextact", "children"), Output("nextact-s", "children"),
    Output("nextact", "className"), Output("stored", "children"), Output("stored-s", "children"),
    Output("now", "children"), Output("now", "className"),
    Output("cost", "children"), Output("cost-s", "children"), Output("thr", "children"), Output("thr-s", "children"),
    Input("tick", "n_intervals"),
    Input("power", "value"), Input("cap", "value"), Input("eff", "value"),
    Input("win", "value"), Input("dark", "checked"), Input("layers", "value"),
    Input("clock", "value"), Input("swap", "value"),
    Input("rst", "n_clicks"),
    State("seen", "data"), State("live-fig", "relayoutData"),
)
def live_view_cb(_, power, cap, eff, win, dark, layers, clock, swap_v, _rst, seen, zoom):
    region = REGION
    if not all(v is not None for v in (power, cap, eff)):
        raise PreventUpdate
    t, layers = TH[bool(dark)], layers or []
    fmt, swap = hm(clock), swap_v == "swap"
    conn = store.connect()
    newest = store.newest_timestamp(conn, region)
    if newest is None:
        raise PreventUpdate
    key = f"{newest}|{forecast_stamp(conn)}|{region}|{power}|{cap}|{eff}|{win}|{dark}|{layers}|{clock}|{swap_v}"
    if dash.ctx.triggered_id == "tick" and key == seen:
        raise PreventUpdate
    px, so_far, plan, typical, nprior = live(str(newest), region, power, cap, eff, forecast_stamp(conn))
    today = day_of(newest)

    # One x axis for both panels (SoC sits on y2 with its own domain) so hoversubplots="axis" lists the SoC row in the
    # same tooltip. make_subplots' stacked rows give the SoC panel a *matched* xaxis2, which that option does not join.
    fig = make_subplots(rows=1, cols=1)
    if typical is not None and "typical" in layers:
        typical_band(fig, typical, t)
    for df in (so_far, plan):
        if df is not None:
            tape(fig, df, t, full="plan" in layers, swap=swap, band=TAPE_BAND)
    if "band" in layers and (so_far is not None or plan is not None):  # one trace per line, else each side adds a hover row at NOW
        threshold_band(fig, pd.concat([x for x in (so_far, plan) if x is not None]), t, swap)
    # Real traces hover so the swatch carries the line style (solid vs dashed); a merged hover trace can only draw one.
    # Plotly reports every trace with a point within `hoverdistance` px of the cursor, so near the actual/forecast seam both
    # rows show. The default 20px reaches ~50 min at ±12h; 1px keeps that to the one gap between the sides.
    # ponytail: 1px is gap-free while 5-min points sit <6px apart, i.e. the ±6h preset on a plot up to ~850px wide.
    if so_far is not None:
        fig.add_trace(price_trace(so_far, "Actual", t["ink"]), row=1, col=1)
    if plan is not None:
        fig.add_trace(price_trace(plan, "AEMO forecast", t["blue"], dash_style="dash"), row=1, col=1)
    soc = pd.concat([x.soc_mwh for x in (so_far, plan) if x is not None])
    fig.add_trace(go.Scatter(x=soc.index, y=soc.values, mode="lines", line=dict(color=t["soc"], width=2.4, shape="hv"), showlegend=False,
                             hovertemplate="%{y:,.0f} MWh<extra></extra>", yaxis="y2"))
    now_pill(fig, newest, t, fmt)
    x0, x1 = (today, today + DAY) if win == "all" else (newest - pd.Timedelta(hours=int(win)), newest + pd.Timedelta(hours=int(win)))
    # uirevision alone cannot hold a zoom: it only protects attributes the new figure leaves unset, and we set
    # xaxis.range on every redraw. So carry the trader's own range back in, and let the window preset be the one
    # inputs that override it are the window preset and #rst, the double-click reset (assets/settings.js).
    zoom = zoom or {}
    if dash.ctx.triggered_id not in ("win", "rst") and "xaxis.range[0]" in zoom:
        x0, x1 = pd.Timestamp(zoom["xaxis.range[0]"]), pd.Timestamp(zoom["xaxis.range[1]"])
    # y2 is declared before base_layout: update_yaxes only reaches axes the layout already has, so a y2 created
    # afterwards would keep plotly's default white gridlines instead of the price panel's.
    fig.update_layout(
        # uirevision keyed on the window preset only: toggling layers, theme or clock redraws without throwing away
        # a zoom the trader set. Picking a different preset is a deliberate view change, so that one does reset.
        uirevision=win,
        hoverdistance=1, hoversubplots="axis", xaxis=dict(anchor="y2", range=[x0, x1], tickformat=f"{fmt}<br>%a"),
        yaxis=dict(domain=list(PRICE_DOM), title_text="A$/MWh"),
        yaxis2=dict(domain=list(SOC_DOM), anchor="x", title_text="MWh"))
    base_layout(fig, 680, dark, fmt)

    # stat tiles
    spot = float(px.iloc[-1]) if len(px) else float("nan")
    delta = float(px.iloc[-1] - px.iloc[-2]) if len(px) > 1 else 0.0
    # chg/dis, not hot/cool: a falling price is a buying signal and a rising one a selling signal, so the delta
    # follows the "Trade colours" setting through the same CSS vars the tape and thresholds use.
    spot_s = [html.Span("A$/MWh · "), html.B(f"{delta:+.2f}", className="dis" if delta > 0 else "chg" if delta < 0 else "muted"), html.Span(" vs 5 min ago")]
    if plan is not None:
        hour = plan.rrp.iloc[:12]
        f1, f1_s = f"{hour.iloc[-1]:,.2f}", f"{hour.min():,.0f}–{hour.max():,.0f} next hour"
        nxt = plan[plan.action != "idle"]
        if len(nxt):
            r = nxt.iloc[0]
            na, na_cls, na_s = r.action.capitalize(), CLS[r.action], f"{nxt.index[0]:{fmt}} at ${r.rrp:,.0f}"
        else:
            na, na_cls, na_s = "Hold", CLS["idle"], "no trades in the horizon"
    else:
        f1 = na = "–"; f1_s = na_s = "no forecast in store"; na_cls = CLS["idle"]
    # what the battery is doing on this interval, as opposed to the plan's next trade
    now_a = so_far.action.iloc[-1] if so_far is not None else "idle"
    nx, nx_cls = {"charge": "Charging", "discharge": "Discharging", "idle": "Idle"}[now_a], CLS[now_a]
    soc_now = float(so_far.soc_mwh.iloc[-1]) if so_far is not None else 0.0
    stored, stored_s = f"{100 * soc_now / float(cap):.0f}%", f"{soc_now:,.0f} / {float(cap):,.0f} MWh"
    # Cash the plan moves over its horizon: sales at forecast prices, less what the charges cost. The MWh already
    # in the tank are carried in free -- what they cost is whatever the real fills were, which the dashboard does
    # not know (so_far is a simulation, not a fill history), so it is left out rather than guessed at. Energy still
    # stored when the horizon ends is worth nothing here, the same terminal rule hindsight() uses.
    if plan is not None:
        rev = float(plan.cashflow_aud.sum())
        # contiguous runs, not non-idle intervals: a two-hour charge is one decision a trader acts on, not 24
        act = plan.action
        runs = int(((act != "idle") & (act != act.shift())).sum())
        span = (plan.index[-1] - plan.index[0]).total_seconds() / 3600
        cost_v, cost_s = f"${rev:,.0f}", f"{runs} runs over next {span:,.0f} h"
    else:
        cost_v, cost_s = "–", "no forecast in store"
    # The DP's own thresholds for this interval: charge pays below storage_value, discharge pays above
    # sell_above (= storage_value / efficiency). chg/dis classes, so the pair follows the "Trade colours" setting.
    if so_far is not None and so_far.storage_value.iloc[-1] == so_far.storage_value.iloc[-1]:
        buy, sell = float(so_far.storage_value.iloc[-1]), float(so_far.sell_above.iloc[-1])
        thr = [html.Span(f"${buy:,.0f}", className="chg"), html.Span(" / ", className="muted"),
               html.Span(f"${sell:,.0f}", className="dis")]
        thr_s = "buy below / sell above"
    else:
        thr, thr_s = "–", "no plan yet"
    shown = ["actual", "forecast", "charge", "discharge"]
    shown += ["buy", "sell"] * ("band" in layers) + ["typical", "typmid"] * ("typical" in layers)
    leg = legend(*(LEG[k] for k in shown)).children
    return (fig, leg, key, str(newest),
            interval_label(newest, fmt), f"{spot:,.2f}", spot_s,
            f1, f1_s, na, na_s, na_cls, stored, stored_s, nx, nx_cls, cost_v, cost_s, thr, thr_s)


@app.callback(Output("duration", "children"), Input("power", "value"), Input("cap", "value"))
def duration_hint(power, cap):
    if not power or not cap:
        raise PreventUpdate
    return f"{float(cap) / float(power):.1f} h duration · {float(power) / float(cap):.2f}C"


# ---------- Backtest ----------

@app.callback(
    Output("k-h", "children"), Output("k-h-s", "children"), Output("k-c", "children"), Output("k-c-s", "children"),
    Output("bt-title", "children"), Output("bt-fig", "figure"), Output("cum-fig", "figure"),
    Output("day-table", "children"), Output("worst-table", "children"),
    Input("power", "value"), Input("cap", "value"), Input("eff", "value"),
    Input("which", "value"), Input("dark", "checked"),
    Input("clock", "value"), Input("swap", "value"),
)
def backtest_view(power, cap, eff, which, dark, clock, swap_v):
    region = REGION
    if not all(v is not None for v in (power, cap, eff)):
        raise PreventUpdate
    t = TH[bool(dark)]
    fmt, swap = hm(clock), swap_v == "swap"
    res = backtest(BT_START, BT_END, region, power, cap, eff, forecast_stamp(store.connect()))
    if res is None:
        raise PreventUpdate
    h, c = res
    runs_ = {"hindsight": h, "causal": c}
    rev = {k: float(v.cashflow_aud.sum()) if v is not None else None for k, v in runs_.items()}
    pct = lambda k: f"captured {100 * rev[k] / rev['hindsight']:.0f}% of hindsight" if rev[k] is not None and rev["hindsight"] else ""

    df = runs_[which] if runs_[which] is not None else h
    fig = make_subplots(rows=1, cols=1)
    tape(fig, df, t, swap=swap); threshold_band(fig, df, t, swap)
    fig.add_trace(price_trace(df, which, t["ink"]), row=1, col=1)
    base_layout(fig, 480, dark, fmt).update_yaxes(title_text="A$/MWh")
    fig.update_xaxes(tickformat=f"{fmt}<br>%a %d", rangeslider=dict(visible=True, thickness=0.07))

    cum = go.Figure()
    for k, v in runs_.items():
        if v is not None:
            cum.add_trace(go.Scatter(x=v.index, y=v.cashflow_aud.cumsum(), mode="lines", line=dict(color=t[{"hindsight": "ink", "causal": "blue"}[k]], width=2.2),
                                     showlegend=False, hovertemplate="%{x|%a " + fmt + "} · $%{y:,.0f}<extra>" + k + "</extra>"))
    base_layout(cum, 300, dark, fmt).update_yaxes(title_text="A$")

    days = pd.DataFrame({k: v.cashflow_aud.groupby(day_of(v.index)).sum() for k, v in runs_.items() if v is not None})
    day_rows = [[f"{d:%a %d %b}"] + [aud(r[k]) for k in days.columns] +
                ([f"{100 * r['causal'] / r['hindsight']:.0f}%" if "causal" in days and r["hindsight"] else "–"]) for d, r in days.iterrows()]
    day_head = ["Day"] + [k.capitalize() for k in days.columns] + ["Captured"]

    if c is not None:
        lost = (h.cashflow_aud - c.cashflow_aud).sort_values(ascending=False).head(10)
        worst = table(["Interval", "Lost", "Hindsight", "Causal", "Causal reasoning"],
                      [[f"{tt:%a %d %b {fmt}}", aud(lost[tt]), h.action[tt], c.action[tt], c.reason[tt]] for tt in lost.index])
    else:
        worst = dmc.Alert("No AEMO forecast rows for this range yet. Run the ingest to add Predispatch and 5-minute forecasts.", color="orange", variant="light")
    return (aud(rev["hindsight"]), f"{len(h) // 288} days · perfect foresight",
            aud(rev["causal"]) if rev["causal"] is not None else "–", pct("causal") or "awaiting forecast data",
            f"{region} · {which} schedule", fig, cum, table(day_head, day_rows), worst)


def free_port(start, tries=20):
    """First port at or after `start` that nothing else is bound to.

    SO_REUSEADDR mirrors what werkzeug's own socket does, so a port left in TIME_WAIT by the last run
    reads as free here exactly as it will there.
    """
    for p in range(start, start + tries):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start   # nothing free in the range: hand `start` back and let app.run() report the real error


if __name__ == "__main__":
    want = int(os.environ.get("PORT", 8050))
    # Probe once, in the launching process. The reloader child inherits PORT and must reuse the same
    # number, or an edit would move the server out from under the tab that is already open.
    # ponytail: racy by construction, something could take the port between probe and bind. A dev server
    # on loopback, so the retry is the human. Bind-and-hand-the-socket-over if that ever stops being true.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        port = want
    else:
        port = free_port(want)
        os.environ["PORT"] = str(port)
        if port != want:
            print(f"port {want} is in use, serving on {port} instead", flush=True)
    # Only where a GUI plausibly exists. On a headless unix box webbrowser registers lynx/w3m/links
    # whenever TERM is set (webbrowser.py, "Also try console browsers"), and those are GenericBrowser,
    # whose open() inherits this terminal and blocks on p.wait() -- so an SSH session would get a text
    # browser drawn over the server's own console. The test is the display, not the OS: a Linux desktop
    # should open a tab and a mac over SSH should not.
    gui = (sys.platform[:3] == "win" or sys.platform == "darwin"
           or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    # Open the tab from the launching process only. Under BESST_DEBUG the reloader re-execs a child with
    # WERKZEUG_RUN_MAIN=true on every edit, so opening there would mean a fresh tab per save; the launching
    # process runs once either way. Delayed so the server is accepting connections before the browser asks,
    # and daemon so a Ctrl-C in the first second does not wait on the timer.
    if gui and os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not os.environ.get("BESST_NO_BROWSER"):
        t = threading.Timer(1.5, webbrowser.open, (f"http://127.0.0.1:{port}",))
        t.daemon = True
        t.start()
    # Serving is the default; debug is opt-in. Its reloader forks a second process, which means a second
    # ingest thread polling nemweb -- fine while editing, wrong for anything left running.
    app.run(debug=bool(os.environ.get("BESST_DEBUG")), port=port)
