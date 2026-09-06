"""SQLite store for prices and forecasts. Primary keys make every ingest idempotent."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

DB_PATH = Path(os.environ.get("BESST_DB", "data/nem.sqlite"))   # `make demo` points this at a copy of the snapshot
STEP = pd.Timedelta(minutes=5)

DDL = """
CREATE TABLE IF NOT EXISTS dispatch_price(
    settlementdate TEXT, regionid TEXT, rrp REAL, source_file TEXT,
    PRIMARY KEY(settlementdate, regionid));
CREATE TABLE IF NOT EXISTS predispatch_price(
    run_datetime TEXT, regionid TEXT, interval_datetime TEXT, rrp REAL,
    PRIMARY KEY(run_datetime, regionid, interval_datetime));
CREATE TABLE IF NOT EXISTS p5min_price(
    run_datetime TEXT, regionid TEXT, interval_datetime TEXT, rrp REAL,
    PRIMARY KEY(run_datetime, regionid, interval_datetime));
"""


AEST = timezone(timedelta(hours=10))   # NEM market time: fixed offset, no daylight saving


def now_market() -> pd.Timestamp:
    """Wall clock in NEM market time, naive to match the stored timestamps. Every timestamp in the store is
    AEST, so a host in any other zone must convert rather than call datetime.now()."""
    return pd.Timestamp(datetime.now(AEST).replace(tzinfo=None))


def iso(t) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def connect(path=DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def upsert(conn, table: str, rows: Iterable[tuple]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
    conn.commit()
    return conn.total_changes - before


def load_prices(conn, start, end, region="NSW1") -> pd.Series:
    df = pd.read_sql(
        "SELECT settlementdate, rrp FROM dispatch_price WHERE regionid=? AND settlementdate>? AND settlementdate<=? ORDER BY 1",
        conn, params=(region, iso(start), iso(end)), parse_dates=["settlementdate"],
    )
    return df.set_index("settlementdate")["rrp"].rename_axis(None)


def _latest_run(conn, table, at, region) -> pd.Series:
    run = conn.execute(f"SELECT max(run_datetime) FROM {table} WHERE regionid=? AND run_datetime<=?", (region, iso(at))).fetchone()[0]
    if run is None:
        return pd.Series(dtype=float)
    df = pd.read_sql(
        f"SELECT interval_datetime, rrp FROM {table} WHERE regionid=? AND run_datetime=? AND interval_datetime>? ORDER BY 1",
        conn, params=(region, run, iso(at)), parse_dates=["interval_datetime"],
    )
    return df.set_index("interval_datetime")["rrp"].rename_axis(None)


def load_forecast_asof(conn, at, region="NSW1") -> pd.Series:
    """As-of Forecast: newest 5-min run for its hour, then newest predispatch run beyond it, on a 5-min grid."""
    at = pd.Timestamp(at)
    p5 = _latest_run(conn, "p5min_price", at, region)
    pre = _latest_run(conn, "predispatch_price", at, region)
    if not pre.empty:
        grid = pd.date_range(at.floor("5min") + STEP, pre.index.max(), freq="5min")
        # a half-hour value is labelled by period end and covers the six intervals ending there -> bfill
        pre = pre.reindex(grid.union(pre.index)).bfill().reindex(grid)
        if not p5.empty:
            pre = pre[pre.index > p5.index.max()]
    return pd.concat([p5, pre]).rename("rrp")


def newest_timestamp(conn, region="NSW1"):
    t = conn.execute("SELECT max(settlementdate) FROM dispatch_price WHERE regionid=?", (region,)).fetchone()[0]
    return pd.Timestamp(t) if t else None
