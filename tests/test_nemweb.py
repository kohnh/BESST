from pathlib import Path

import pandas as pd

from besst import store
from besst.nemweb import DISPATCH, P5MIN, PREDISPATCH

FX = Path(__file__).parent / "fixtures"


def rows(src, name):
    return src.rows((FX / name).read_bytes())


def test_parse_real_files():
    d = rows(DISPATCH, "PUBLIC_DISPATCHIS_202609050205_0000000536134527.zip")
    assert len(d) == 5 and ("2026-09-05 02:05:00", "NSW1", 55.51896) == d[0][:3]
    p = [r for r in rows(PREDISPATCH, "PUBLIC_PREDISPATCHIS_202609050300_20260905023222.zip") if r[1] == "NSW1"]
    assert len(p) == 51 and p[0][0] == "2026-09-05 02:32:22" and p[0][2] == "2026-09-05 03:00:00"
    f = [r for r in rows(P5MIN, "PUBLIC_P5MIN_202609050300_20260905025542.zip") if r[1] == "NSW1"]
    assert len(f) == 12 and f[0][0] == "2026-09-05 02:55:42" and f[-1][2] == "2026-09-05 03:55:00"


def test_store_roundtrip_and_asof_stitch():
    conn = store.connect(":memory:")
    d = rows(DISPATCH, "PUBLIC_DISPATCHIS_202609050205_0000000536134527.zip")
    assert store.upsert(conn, "dispatch_price", d) == 5
    assert store.upsert(conn, "dispatch_price", d) == 0  # idempotent
    store.upsert(conn, "predispatch_price", rows(PREDISPATCH, "PUBLIC_PREDISPATCHIS_202609050300_20260905023222.zip"))
    store.upsert(conn, "p5min_price", rows(P5MIN, "PUBLIC_P5MIN_202609050300_20260905025542.zip"))

    assert store.newest_timestamp(conn) == pd.Timestamp("2026-09-05 02:05")
    assert store.load_prices(conn, "2026-09-05 02:00", "2026-09-05 02:05").iloc[0] == 55.51896

    fc = store.load_forecast_asof(conn, "2026-09-05 03:00")
    assert fc.index[0] == pd.Timestamp("2026-09-05 03:05") and fc.index.freq is None
    assert (fc.index.to_series().diff().dropna() == pd.Timedelta(minutes=5)).all()
    assert len(fc[: "2026-09-05 03:55"]) == 11  # 5-min run covers to 03:55, predispatch beyond
    assert fc.index[-1] == pd.Timestamp("2026-09-06 04:00")  # end of next trading day
    assert store.load_forecast_asof(conn, "2026-09-05 02:00").empty  # nothing published yet
