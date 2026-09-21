"""
Завантаження історичних 1h-свічок Binance USDM Futures з data.binance.vision
(безкоштовно, без ключа). Працює ЛОКАЛЬНО — Binance геоблокує GitHub Actions.
Кеш: backtest/bt_data/{SYMBOL}_1h.parquet (gitignored).

    python backtest/data.py            # 2024-01 -> сьогодні, усі 20 монет
"""
import io, sys, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um"
CACHE = Path(__file__).parent / "bt_data"
COLS = ["ts", "open", "high", "low", "close", "vol", "close_ts", "qvol", "n", "tb", "tq", "ig"]

# Kraken USD-пара з бота -> Binance USDT-перп
SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "ZEC", "HYPE", "ADA", "TAO", "UNI", "DOGE",
           "NEAR", "SUI", "XLM", "LINK", "XMR", "LTC", "AAVE", "ARB", "ENA", "INJ"]


def _get_zip(url: str) -> pd.DataFrame | None:
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    df = pd.read_csv(io.StringIO(raw), header=0 if not first[0].isdigit() else None, names=COLS if first[0].isdigit() else None)
    if not first[0].isdigit():
        df = df.rename(columns={"open_time": "ts", "volume": "vol"})[["ts", "open", "high", "low", "close", "vol"]]
    return df[["ts", "open", "high", "low", "close", "vol"]]


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def download(sym: str, start: date, tf: str = "1h") -> pd.DataFrame:
    pair = f"{sym}USDT"
    today = date.today()
    parts = []
    for y, m in _months(start, today):
        if (y, m) < (today.year, today.month):
            df = _get_zip(f"{BASE}/monthly/klines/{pair}/{tf}/{pair}-{tf}-{y}-{m:02d}.zip")
            if df is not None:
                parts.append(df)
        else:  # поточний місяць: денні архіви до вчора
            d = date(y, m, 1)
            while d < today:
                df = _get_zip(f"{BASE}/daily/klines/{pair}/{tf}/{pair}-{tf}-{d.isoformat()}.zip")
                if df is not None:
                    parts.append(df)
                d += timedelta(days=1)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = df["ts"].astype("int64")
    df.loc[df["ts"] > 10**14, "ts"] //= 1000   # раптом мікросекунди
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def load(sym: str) -> pd.DataFrame:
    p = CACHE / f"{sym}_1h.pkl"
    return pd.read_pickle(p) if p.exists() else pd.DataFrame()


def main(start: date = date(2024, 1, 1)):
    CACHE.mkdir(exist_ok=True)
    def work(sym):
        df = download(sym, start)
        if not df.empty:
            df.to_pickle(CACHE / f"{sym}_1h.pkl")
        return sym, df
    with ThreadPoolExecutor(6) as ex:
        for sym, df in ex.map(work, SYMBOLS):
            if df.empty:
                print(f"{sym:5s} — немає даних"); continue
            gaps = (df["ts"].diff().dropna() != 3600_000).sum()
            print(f"{sym:5s} {len(df):6d} барів  {df.time.iloc[0]:%Y-%m-%d} -> {df.time.iloc[-1]:%Y-%m-%d %H:%M}  розривів: {gaps}")


if __name__ == "__main__":
    main()
