"""
Довга історія BTC/ETH для regime.py: 1h-свічки Binance USDM + funding з 2019-09 (запуск перпів).
Окремі файли, щоб не чіпати кеш data.py/ideas.py:
  bt_data/{SYM}_1h_long.pkl, bt_data/funding_long_{SYM}.pkl

    python backtest/data_long.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as bdata          # noqa: E402
import data_extra as bextra   # noqa: E402

SYMS = ["BTC", "ETH"]
START = date(2019, 9, 1)


def main():
    bdata.CACHE.mkdir(exist_ok=True)
    bextra.START = START
    for s in SYMS:
        k = bdata.download(s, START)
        k.to_pickle(bdata.CACHE / f"{s}_1h_long.pkl")
        f = bextra.funding(s)
        f.to_pickle(bdata.CACHE / f"funding_long_{s}.pkl")
        gaps = (k["ts"].diff().dropna() != 3600_000).sum()
        print(f"{s}: {len(k)} барів {k.time.iloc[0]:%Y-%m-%d} -> {k.time.iloc[-1]:%Y-%m-%d %H:%M}, розривів {gaps}; "
              f"funding {len(f)} рядків від {f.ts.min() and __import__('pandas').to_datetime(f.ts.min(), unit='ms'):%Y-%m-%d}")


if __name__ == "__main__":
    main()
