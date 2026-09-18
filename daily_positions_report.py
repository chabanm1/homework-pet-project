"""
=============================================================================
DAILY / WEEKLY POSITIONS REPORT — Binance USDM Futures
=============================================================================
Локальний скрипт (НЕ для GitHub Actions — Binance геоблокує хмарні IP).
Реконструює позиції (вхід -> вихід) з фактичних fills, а не просто суму PnL.

    python daily_positions_report.py                # за вчора (Europe/Kyiv)
    python daily_positions_report.py 2026-09-17      # за конкретну дату
    python daily_positions_report.py --week          # останні 7 днів (по вчора включно) + підсумок
    python daily_positions_report.py --week 2026-09-17  # 7 днів, що закінчуються цією датою

Працює тільки в one-way режимі позицій (positionSide=BOTH). Якщо акаунт
переведено в hedge mode, групування за net-qty буде некоректним.

Потребує BINANCE_API_KEY / BINANCE_API_SECRET у .env (локально).
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
    sys.exit("Немає BINANCE_API_KEY / BINANCE_API_SECRET у .env.")


def day_start(date_str=None):
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KYIV)
    else:
        day = datetime.now(KYIV) - timedelta(days=1)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def symbols_traded(exchange, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while True:
        batch = exchange.fapiPrivateGetIncome({
            "incomeType": "REALIZED_PNL", "startTime": cursor, "endTime": end_ms, "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1]["time"]) + 1
    return sorted({r["symbol"] for r in rows})


def to_ccxt_symbol(binance_symbol):
    # "NEARUSDT" -> "NEAR/USDT:USDT"
    base = binance_symbol.replace("USDT", "")
    return f"{base}/USDT:USDT"


def reconstruct_positions(trades):
    """One-way mode: track signed net qty, split lifecycle on each return to zero."""
    positions = []
    net = 0.0
    entry_fills = []
    exit_fills = []
    open_time = None
    direction = None

    for t in trades:
        signed = t["amount"] if t["side"] == "buy" else -t["amount"]
        prev_net = net
        new_net = net + signed

        if prev_net == 0:
            open_time = t["datetime_kyiv"]
            entry_fills = []
            exit_fills = []
            direction = "LONG" if signed > 0 else "SHORT"

        same_direction = (prev_net >= 0 and signed > 0) or (prev_net <= 0 and signed < 0)

        if prev_net == 0 or same_direction:
            entry_fills.append((t["price"], abs(t["amount"])))
        else:
            realized = float(t["info"]["realizedPnl"])
            exit_fills.append((t["price"], abs(t["amount"]), realized, t["datetime_kyiv"]))

        net = new_net
        if abs(net) < 1e-9 and entry_fills:
            entry_qty = sum(q for _, q in entry_fills)
            exit_qty = sum(q for _, q, _, _ in exit_fills)
            entry_px = sum(p * q for p, q in entry_fills) / entry_qty if entry_qty else 0
            exit_px = sum(p * q for p, q, _, _ in exit_fills) / exit_qty if exit_qty else 0
            pnl = sum(pn for _, _, pn, _ in exit_fills)
            positions.append({
                "side": direction,
                "entry_qty": entry_qty,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "pnl": pnl,
                "open_time": open_time,
                "close_time": exit_fills[-1][3] if exit_fills else None,
            })
            net = 0.0
            entry_fills = []
            exit_fills = []

    return positions


def positions_for_day(exchange, start):
    """Fetch and reconstruct all positions closed within [start, start+1day)."""
    end = start + timedelta(days=1)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    symbols = symbols_traded(exchange, start_ms, end_ms)
    day_positions = []
    for binance_symbol in symbols:
        ccxt_symbol = to_ccxt_symbol(binance_symbol)
        raw = exchange.fetch_my_trades(ccxt_symbol, since=start_ms, limit=1000)
        raw = [t for t in raw if t["timestamp"] < end_ms]
        raw.sort(key=lambda t: t["timestamp"])
        for t in raw:
            t["datetime_kyiv"] = datetime.fromtimestamp(t["timestamp"] / 1000, tz=KYIV)

        for pos in reconstruct_positions(raw):
            pos["symbol"] = binance_symbol
            day_positions.append(pos)

    day_positions.sort(key=lambda p: p["open_time"])
    return day_positions


def print_positions(positions, start_index=1):
    for i, pos in enumerate(positions, start=start_index):
        move_pct = (pos["exit_px"] - pos["entry_px"]) / pos["entry_px"] * 100
        if pos["side"] == "SHORT":
            move_pct = -move_pct
        duration = pos["close_time"] - pos["open_time"]
        mark = "🟢" if pos["pnl"] >= 0 else "🔴"
        print(
            f"{mark} #{i} {pos['symbol']} {pos['side']}  "
            f"{pos['open_time'].strftime('%H:%M:%S')} -> {pos['close_time'].strftime('%H:%M:%S')} "
            f"({duration})\n"
            f"    qty={pos['entry_qty']:.4f}  entry={pos['entry_px']:.4f}  exit={pos['exit_px']:.4f}  "
            f"move={move_pct:+.2f}%  PnL={pos['pnl']:+.4f} USDT"
        )


def run_single_day(exchange, start):
    positions = positions_for_day(exchange, start)
    print(f"\n=== Позиції за {start.strftime('%Y-%m-%d')} (Europe/Kyiv) ===\n")
    if not positions:
        print("Активності за цей день немає.")
        return positions
    print_positions(positions)
    total_pnl = sum(p["pnl"] for p in positions)
    print(f"\nВсього позицій: {len(positions)}   Сумарний PnL: {total_pnl:+.4f} USDT")
    return positions


def run_week(exchange, end_day_str):
    last_day = day_start(end_day_str) if end_day_str else day_start()
    days = [last_day - timedelta(days=offset) for offset in range(6, -1, -1)]

    daily_stats = []
    all_positions = []
    for d in days:
        positions = positions_for_day(exchange, d)
        all_positions.extend(positions)
        pnl = sum(p["pnl"] for p in positions)
        wins = sum(1 for p in positions if p["pnl"] >= 0)
        daily_stats.append((d, positions, pnl, wins))

    print(f"\n=== Тиждень {days[0].strftime('%Y-%m-%d')} — {days[-1].strftime('%Y-%m-%d')} (Europe/Kyiv) ===\n")

    idx = 1
    for d, positions, pnl, wins in daily_stats:
        label = d.strftime("%a %Y-%m-%d")
        if not positions:
            print(f"{label}: без угод")
            continue
        mark = "🟢" if pnl >= 0 else "🔴"
        print(f"{mark} {label}: {len(positions)} позицій, {wins} у плюс, PnL {pnl:+.4f} USDT")
        print_positions(positions, start_index=idx)
        idx += len(positions)
        print()

    total_pnl = sum(p["pnl"] for p in all_positions)
    total_wins = sum(1 for p in all_positions if p["pnl"] >= 0)
    total_count = len(all_positions)
    win_rate = (total_wins / total_count * 100) if total_count else 0.0

    print("=== Підсумок за тиждень ===")
    print(f"Позицій: {total_count}   Win-rate: {win_rate:.1f}% ({total_wins}/{total_count})")
    print(f"Сумарний PnL: {total_pnl:+.4f} USDT")
    if total_count:
        best = max(all_positions, key=lambda p: p["pnl"])
        worst = min(all_positions, key=lambda p: p["pnl"])
        print(f"Найкраща угода: {best['symbol']} {best['side']} {best['open_time'].strftime('%d.%m %H:%M')}  PnL={best['pnl']:+.4f}")
        print(f"Найгірша угода: {worst['symbol']} {worst['side']} {worst['open_time'].strftime('%d.%m %H:%M')}  PnL={worst['pnl']:+.4f}")


def main():
    exchange = ccxt.binanceusdm({
        "apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True,
    })

    args = sys.argv[1:]
    if args and args[0] == "--week":
        run_week(exchange, args[1] if len(args) > 1 else None)
    else:
        start = day_start(args[0] if args else None)
        run_single_day(exchange, start)


if __name__ == "__main__":
    main()
