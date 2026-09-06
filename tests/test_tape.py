"""tape() emits the shapes add_vrect would, without add_vrect's per-call revalidation of every existing shape."""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import app


def _reference(fig, df, t, full, swap):
    """The add_vrect original, kept only as the thing tape() has to keep matching."""
    chg, dis = app.act_colors(t, swap)
    for s, e, a in app.runs(df.action):
        kw = dict(x0=s - app.FIVE, x1=e - app.FIVE, fillcolor=chg if a == "charge" else dis,
                  line_width=0, layer="below", row=1, col=1)
        fig.add_vrect(y0=0, y1=app.TAPE, **kw)
        if full:
            fig.add_vrect(y0=app.TAPE, y1=1, opacity=0.16, **kw)


def _fig(rows):
    f = make_subplots(rows=rows, cols=1, shared_xaxes=True)
    f.add_trace(go.Scatter(x=[pd.Timestamp("2026-01-01")], y=[1]), row=1, col=1)
    return f


def test_tape_matches_add_vrect():
    idx = pd.date_range("2026-01-01 00:05", periods=12, freq="5min")
    df = pd.DataFrame({"action": ["idle", "charge", "charge", "idle", "discharge", "idle",
                                  "charge", "idle", "idle", "discharge", "discharge", "charge"]}, index=idx)
    t = app.TH[False]
    for rows in (1, 2):            # the live chart adds an SoC row; the shape refs must not move with it
        for full in (False, True):
            for swap in (False, True):
                a, b = _fig(rows), _fig(rows)
                app.tape(a, df, t, full=full, swap=swap)
                _reference(b, df, t, full, swap)
                assert a.layout.shapes == b.layout.shapes, (rows, full, swap)
                assert len(a.layout.shapes) == (2 if full else 1) * 5   # five non-idle runs in the fixture
