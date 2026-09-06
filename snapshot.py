"""Cut the committed demo database out of the live store: NSW1 only, vacuumed, single file.

The app only ever queries NSW1 (app.REGION), so the other four regions are dead weight -- dropping them
takes the file from ~31 MB to ~6 MB. Re-run after a backfill if the demo data should move forward.
"""
import pathlib

from besst import store

SRC = pathlib.Path("data/nem.sqlite")
OUT = pathlib.Path("data/snapshot.sqlite")
TABLES = ("dispatch_price", "predispatch_price", "p5min_price")


def cut(src=SRC, out=OUT, region="NSW1"):
    out.unlink(missing_ok=True)
    conn = store.connect(out)                       # applies the DDL
    conn.execute("PRAGMA journal_mode=DELETE")      # a committed artefact should be one file, not three
    conn.execute("ATTACH ? AS live", (str(src),))
    for t in TABLES:
        conn.execute(f"INSERT INTO {t} SELECT * FROM live.{t} WHERE regionid=?", (region,))
        print(f"{t}: {conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} rows")
    conn.commit()
    conn.execute("DETACH live")
    conn.execute("VACUUM")
    conn.close()
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    cut()
