"""Fetch and parse AEMO nemweb MMS files (DispatchIS, PredispatchIS, P5MIN)."""
import csv
import io
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterator

import requests

BASE = "https://nemweb.com.au/Reports"
_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0 (besst-dashboard)"  # nemweb 404s plain clients on some folders


def get(url: str, tries: int = 5) -> bytes:
    for i in range(tries):
        try:
            r = _session.get(url, timeout=120)
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2**i)


def listing(url: str) -> list[str]:
    """Filenames in a nemweb directory page."""
    return sorted(set(re.findall(r"PUBLIC_[A-Z0-9_]+\.zip", get(url).decode(errors="ignore"))))


def csvs(blob: bytes) -> Iterator[tuple[str, str]]:
    """(name, text) for every CSV in a zip, recursing into nested daily-archive zips."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            data = z.read(n)
            if n.lower().endswith(".zip"):
                yield from csvs(data)
            elif n.lower().endswith(".csv"):
                yield n, data.decode(errors="ignore")


def parse_mms(text: str, table: tuple[str, str]) -> list[dict]:
    """Rows of one (component, table) from an MMS CSV. 'I' rows carry headers, 'D' rows data."""
    cols, out = None, []
    for p in csv.reader(io.StringIO(text)):
        if len(p) < 4 or p[1:3] != list(table):
            continue
        if p[0] == "I":
            cols = p[4:]
        elif p[0] == "D" and cols:
            out.append(dict(zip(cols, p[4:])))
    return out


def ts(s: str) -> str:
    """'2026/09/05 02:05:00' -> '2026-09-05 02:05:00'."""
    return s.replace("/", "-")


def published(fname: str) -> str:
    """Publication time from a forecast filename's second stamp (YYYYMMDDHHMMSS)."""
    s = re.findall(r"\d{12,14}", fname)[1]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14] or '00'}"


@dataclass
class Source:
    name: str
    current: str
    archive: str | None
    table: tuple[str, str]
    row: Callable[[dict, str], tuple]

    def list_files(self, start: date, end: date) -> list[str]:
        cur = listing(self.current)
        arc = listing(self.archive) if self.archive else []
        urls = []
        for i in range((end - start).days + 1):
            d = start + timedelta(days=i)
            lo, hi = d.strftime("%Y%m%d") + "0000", (d + timedelta(days=1)).strftime("%Y%m%d") + "0000"
            daily = f"PUBLIC_{self.name}_{d:%Y%m%d}.zip"
            if daily in arc:
                urls.append(self.archive + daily)
                continue
            got = [f for f in cur if lo < re.search(r"_(\d{12})", f).group(1) <= hi]
            if not got:
                raise LookupError(
                    f"{self.name}: no files for {d} in Current and no daily archive. "
                    "(Predispatch only lives in Current for ~2 weeks; its weekly ~300MB archives are out of scope.)"
                )
            urls += [self.current + f for f in got]
        return urls

    def rows(self, blob: bytes) -> list[tuple]:
        return [
            self.row(r, name)
            for name, text in csvs(blob)
            for r in parse_mms(text, self.table)
            if r["INTERVENTION"] == "0"  # AEMO settles on the no-intervention pricing run
        ]


DISPATCH = Source(
    "DISPATCHIS",
    f"{BASE}/Current/DispatchIS_Reports/",
    f"{BASE}/Archive/DispatchIS_Reports/",
    ("DISPATCH", "PRICE"),
    lambda r, f: (ts(r["SETTLEMENTDATE"]), r["REGIONID"], float(r["RRP"]), f),
)
PREDISPATCH = Source(
    "PREDISPATCHIS",
    f"{BASE}/Current/PredispatchIS_Reports/",
    None,
    ("PREDISPATCH", "REGION_PRICES"),
    lambda r, f: (published(f), r["REGIONID"], ts(r["DATETIME"]), float(r["RRP"])),
)
P5MIN = Source(
    "P5MIN",
    f"{BASE}/Current/P5_Reports/",
    f"{BASE}/Archive/P5_Reports/",
    ("P5MIN", "REGIONSOLUTION"),
    lambda r, f: (published(f), r["REGIONID"], ts(r["INTERVAL_DATETIME"]), float(r["RRP"])),
)
