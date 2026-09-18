"""
=============================================================================
DAILY TRADES REPORT — Binance USDM Futures
=============================================================================
Локальний скрипт (НЕ для GitHub Actions — Binance геоблокує хмарні IP).
Запускати вручну на своєму ПК наприкінці дня:

    python daily_trades_report.py            # за вчора (Europe/Kyiv)
    python daily_trades_report.py 2026-09-17  # за конкретну дату

Витягує реалізований PnL по закритих угодах з Binance Futures Income History
(/fapi/v1/income, incomeType=REALIZED_PNL) — цей ендпоінт не вимагає
заздалегідь знати символи, на відміну від userTrades.

Потребує BINANCE_API_KEY / BINANCE_API_SECRET у .env (локально, не в GH
Secrets і не в чаті). Ключ можна створити read-only (без прав на торгівлю/
вивід коштів) — цього достатньо для звіту.
=============================================================================
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ccxt
from dotenv import load_dotenv

load_dotenv()

KYIV = ZoneInfo("Europe/Kyiv")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    sys.exit(
        "Немає BINANCE_API_KEY / BINANCE_API_SECRET у .env.\n"
        "Додай у файл .env (у корені репо, він в .gitignore):\n"
        "  BINANCE_API_KEY=...\n"
        "  BINANCE_API_SECRET=...\n"
        "Ключ рекомендується створювати read-only, без прав на торгівлю/вивід коштів."
    )


def day_bounds(date_str: str | None):
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KYIV)
    else:
        day = (datetime.now(KYIV) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def fetch_income(exchange, income_type, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while True:
        batch = exchange.fapiPrivateGetIncome({
            "incomeType": income_type,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1]["time"]) + 1
    return rows


def main():
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    start, end = day_bounds(date_arg)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    exchange = ccxt.binanceusdm({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
    })

    pnl_rows = fetch_income(exchange, "REALIZED_PNL", start_ms, end_ms)
    fee_rows = fetch_income(exchange, "COMMISSION", start_ms, end_ms)
    funding_rows = fetch_income(exchange, "FUNDING_FEE", start_ms, end_ms)

    fees_by_symbol = {}
    for r in fee_rows:
        fees_by_symbol[r["symbol"]] = fees_by_symbol.get(r["symbol"], 0.0) + float(r["income"])
    funding_by_symbol = {}
    for r in funding_rows:
        funding_by_symbol[r["symbol"]] = funding_by_symbol.get(r["symbol"], 0.0) + float(r["income"])

    print(f"\n=== Звіт по угодах: {start.strftime('%Y-%m-%d')} (Europe/Kyiv) ===\n")

    if not pnl_rows:
        print("Закритих позицій (REALIZED_PNL) за цей день немає.")
    else:
        total = 0.0
        for r in pnl_rows:
            t = datetime.fromtimestamp(int(r["time"]) / 1000, tz=KYIV)
            pnl = float(r["income"])
            total += pnl
            mark = "🟢" if pnl >= 0 else "🔴"
            print(f"{mark} {t.strftime('%H:%M:%S')}  {r['symbol']:<12} PnL: {pnl:+.4f} {r['asset']}")
        print(f"\nСумарний реалізований PnL: {total:+.4f} USDT")

    if fees_by_symbol:
        print("\nКомісії за день:")
        for sym, fee in fees_by_symbol.items():
            print(f"  {sym:<12} {fee:+.4f}")

    if funding_by_symbol:
        print("\nФандинг за день:")
        for sym, f in funding_by_symbol.items():
            print(f"  {sym:<12} {f:+.4f}")


if __name__ == "__main__":
    main()
