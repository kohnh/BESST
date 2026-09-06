"""up_to_date() is the loop's stop condition: get it wrong and the poller either spins or gives up early."""
import pandas as pd

from besst import ingest, store


def _store(monkeypatch, now, labels):
    conn = store.connect(":memory:")
    store.upsert(conn, "dispatch_price", [(store.iso(t), "NSW1", 50.0, "f") for t in labels])
    monkeypatch.setattr(store, "now_market", lambda: pd.Timestamp(now))
    return conn


def test_holds_the_interval_in_progress(monkeypatch):
    # 02:11 sits inside the interval 02:10-02:15, which AEMO labels 02:15 (period-end)
    conn = _store(monkeypatch, "2026-09-06 02:11:02", ["2026-09-06 02:15:00"])
    assert ingest.up_to_date(conn)


def test_one_interval_behind(monkeypatch):
    # the 02:15 file was published ~02:10:11 and missed: the newest label only covers 02:05-02:10
    conn = _store(monkeypatch, "2026-09-06 02:11:02", ["2026-09-06 02:10:00"])
    assert not ingest.up_to_date(conn)


def test_empty_store(monkeypatch):
    conn = _store(monkeypatch, "2026-09-06 02:11:02", [])
    assert not ingest.up_to_date(conn)
