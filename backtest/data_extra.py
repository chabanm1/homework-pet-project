"""
Додаткові безкоштовні дані Binance USDM з data.binance.vision (локально; Binance геоблокує GH Actions):
  - історія funding rate (8h)           -> bt_data/funding_{SYM}.pkl   [ts_ms, rate]
  - 5-хв метрики позиціонування (денні) -> bt_data/metrics_{SYM}.pkl    [ts_ms, oi_usd, top_ls, glob_ls, taker_ls]
    (sum_open_interest_value, count_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio)

    python backtest/data_extra.py [funding|metrics|all]
"""
import io, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from data import SYMBOLS, CACHE, _months, BASE   # noqa: E402

START = date(2024, 1, 1)
SES = requests.Session()
SES.mount("https://", requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32))
FAILS = []


def _fetch(url: str) -> bytes | None:
    """200 -> bytes, 404 -> None (файлу немає), інші/мережеві помилки -> 3 спроби, потім у FAILS."""
    for attempt in range(3):
        try:
            r = SES.get(url, timeout=30)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1 + attempt)
    FAILS.append(url)
    return None


def funding(sym: str) -> pd.DataFrame:
    pair, today, parts = f"{sym}USDT", date.today(), []
    for y, m in _months(START, today):
        if (y, m) >= (today.year, today.month):
            continue          # поточний місяць ще не заархівований; для ідей вистачає до кінця минулого
        b = _fetch(f"{BASE}/monthly/fundingRate/{pair}/{pair}-fundingRate-{y}-{m:02d}.zip")
        if b:
            raw = zipfile.ZipFile(io.BytesIO(b))
            parts.append(pd.read_csv(io.BytesIO(raw.read(raw.namelist()[0]))))
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df.rename(columns={"calc_time": "ts", "last_funding_rate": "rate"})[["ts", "rate"]]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def _metrics_day(pair: str, d: date) -> pd.DataFrame | None:
    b = _fetch(f"{BASE}/daily/metrics/{pair}/{pair}-metrics-{d.isoformat()}.zip")
    if not b:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df["ts"] = pd.to_datetime(df["create_time"], utc=True).astype("int64") // 10**6
    return df.rename(columns={"sum_open_interest_value": "oi_usd", "count_toptrader_long_short_ratio": "top_ls",
                              "count_long_short_ratio": "glob_ls", "sum_taker_long_short_vol_ratio": "taker_ls"})[
        ["ts", "oi_usd", "top_ls", "glob_ls", "taker_ls"]]


def metrics(sym: str) -> pd.DataFrame:
    pair, d, end, days = f"{sym}USDT", START, date.today() - timedelta(days=1), []
    while d <= end:
        days.append(d); d += timedelta(days=1)
    with ThreadPoolExecutor(12) as ex:
        parts = [p for p in ex.map(lambda x: _metrics_day(pair, x), days) if p is not None]
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def main(what: str = "all"):
    CACHE.mkdir(exist_ok=True)
    t0 = time.time()
    if what in ("funding", "all"):
        for sym in SYMBOLS:
            df = funding(sym)
            if not df.empty:
                df.to_pickle(CACHE / f"funding_{sym}.pkl")
            print(f"funding {sym:5s} {len(df):5d} рядків", flush=True)
    if what in ("metrics", "all"):
        for sym in SYMBOLS:
            df = metrics(sym)
            if not df.empty:
                df.to_pickle(CACHE / f"metrics_{sym}.pkl")
            span = f"{pd.to_datetime(df.ts.iloc[0], unit='ms'):%Y-%m-%d} -> {pd.to_datetime(df.ts.iloc[-1], unit='ms'):%Y-%m-%d}" if len(df) else "—"
            print(f"metrics {sym:5s} {len(df):7d} рядків  {span}  ({time.time()-t0:.0f}с, збоїв мережі: {len(FAILS)})", flush=True)
    if FAILS:
        print("УВАГА: не вдалось завантажити (мережа, не 404):", len(FAILS), FAILS[:3])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
