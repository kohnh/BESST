"""Ingest CLI: backfill a date range, fetch what's new once, or loop every dispatch interval."""
import argparse
import re
import time
from datetime import date, timedelta

from besst import nemweb, store
from besst.nemweb import DISPATCH, P5MIN, PREDISPATCH, listing

SOURCES = {"dispatch_price": DISPATCH, "predispatch_price": PREDISPATCH, "p5min_price": P5MIN}


def _ingest(conn, table, src, urls):
    for i, u in enumerate(urls, 1):
        n = store.upsert(conn, table, src.rows(nemweb.get(u)))
        print(f"[{src.name}] {i}/{len(urls)} {u.rsplit('/', 1)[1]} +{n}", flush=True)


def backfill(conn, start: date, end: date):
    for table, src in SOURCES.items():
        _ingest(conn, table, src, src.list_files(start, end))


def once(conn):
    """Fetch Current files stamped after the newest stored price (or the last 24 h on an empty store)."""
    newest = store.newest_timestamp(conn) or store.now_market() - timedelta(days=1)
    since = newest.strftime("%Y%m%d%H%M")
    for table, src in SOURCES.items():
        new = [src.current + f for f in listing(src.current) if re.search(r"_(\d{12})", f).group(1) > since]
        _ingest(conn, table, src, new)


def up_to_date(conn) -> bool:
    """True once the store holds the dispatch price for the interval now in progress. Labels are period-end,
    so the interval running at 02:11 is labelled 02:15 -- the newest label rounds the clock up, never down."""
    newest = store.newest_timestamp(conn)
    return newest is not None and newest >= store.now_market().ceil("5min")


def loop(conn, offset=15, retry=15, window=105):
    """Fetch now, then just after each 5-minute boundary, retrying until this interval's price actually lands.

    AEMO's publication time within an interval varies from ~10 s to over a minute, and a poll that finds nothing
    new raises nothing -- so a single attempt at a fixed offset leaves the store a whole interval stale whenever
    AEMO is slow. Retrying on the store's own state covers a late publication and a network failure alike.
    """
    while True:
        deadline = time.time() + window
        while True:
            try:
                once(conn)
            except Exception as e:  # network or parse hiccup: same retry as a late publication
                print(f"poll failed: {e}", flush=True)
            if up_to_date(conn) or time.time() + retry >= deadline:
                break
            time.sleep(retry)
        boundary = (time.time() // 300 + 1) * 300
        time.sleep(max(0.0, boundary + offset - time.time()))


def main():
    ap = argparse.ArgumentParser(description="Ingest AEMO nemweb data into SQLite")
    ap.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--to", dest="end", type=date.fromisoformat)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--db", default=store.DB_PATH)
    a = ap.parse_args()
    conn = store.connect(a.db)
    if a.start:
        backfill(conn, a.start, a.end or a.start)
    elif a.loop:
        loop(conn)
    else:
        once(conn)


if __name__ == "__main__":
    main()
