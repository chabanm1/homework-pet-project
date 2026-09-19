"""
=============================================================================
CHECK SYMBOL — повний живий розбір через реальний пайплайн бота
=============================================================================
    python check_symbol.py HYPE/USD

На відміну від ad-hoc скриптів, викликає ті самі функції, що й
signal_notifier_v2.py: indicators() (RSI/MACD/BB/reversal-confirmation),
fetch_trend() (4h тренд і його штраф/бонус), signals() (усі правила разом).

ВАЖЛИВО про "% впевненості": це НЕ виміряний історичний win-rate.
Реальний win-rate (rule_win_rate) рахується ботом на GitHub Actions і
живе в crypto_cooldown.json там, не локально — тут його немає (буде
показано "н/д", поки не з'явиться реальна історія). Показаний тут "%" —
це груба якісна категорія за шкалою сили сигналу (1-10), просто
переведена у відсотки для зручності порівняння, не бектестована цифра.

Кожен виклик дописується в signal_checks_log.jsonl (локально) — щоб
згодом можна було зіставити із фактичним результатом угоди і порахувати
РЕАЛЬНИЙ % успіху власних входів.
=============================================================================
"""

import sys
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import signal_notifier_v2 as bot

KYIV = ZoneInfo("Europe/Kyiv")
LOG_FILE = "signal_checks_log.jsonl"


def strength_to_tier(strength: int, rule: str | None = None, cd: dict | None = None) -> tuple[str, str]:
    """Груба якісна категорія, НЕ виміряна ймовірність (якщо для rule вже є
    досить накопиченої історії — strength_pct() підставить реальний win-rate
    замість вигаданої вилки). % рахує та сама strength_pct(), що вставляється
    в реальні Telegram-повідомлення бота."""
    pct = bot.strength_pct(strength, rule, cd)
    if strength >= bot.STRONG_STRENGTH:
        return pct, "СИЛЬНИЙ (заходь / готовий сетап)"
    if strength >= bot.MEDIUM_STRENGTH:
        return pct, "СЕРЕДНІЙ (чекай підтвердження)"
    return pct, "СЛАБКИЙ (не заходити)"


def main():
    if len(sys.argv) < 2:
        sys.exit("Використання: python check_symbol.py HYPE/USD")
    symbol = sys.argv[1]

    df = bot.fetch_ohlcv(symbol, "1h", 100)
    ind = bot.indicators(df)
    htf_trend = bot.fetch_trend(symbol)
    fg, fg_cls = bot.fetch_fg()
    cd = bot.load_cd()

    sigs = bot.signals(ind, fg, htf_trend=htf_trend, cd=cd)

    print(f"\n=== {symbol} — {datetime.now(KYIV).strftime('%H:%M:%S')} (Europe/Kyiv) ===\n")
    print(f"Ціна: {ind['price']}   RSI(1h): {ind['rsi']:.1f}   Обсяг ×{ind['vs']:.2f}")
    print(f"Тренд 4h: {htf_trend}   F&G: {fg} ({fg_cls})")
    print(f"Відкат від 3-барного хая: {ind['off_high_pct']:+.2f}%   від лоу: {ind['off_low_pct']:+.2f}%")

    if not sigs:
        print("\nАктивних сигналів немає.")
        return

    directional = [s for s in sigs if s["type"] in ("LONG", "SHORT")]
    logged = []

    for s in sorted(sigs, key=lambda x: -x["strength"]):
        pct, tier = strength_to_tier(s["strength"], s.get("rule"), cd) if s["type"] in ("LONG", "SHORT") else (None, None)
        print(f"\n[{s['rule']}] {s['type']}  сила={s['strength']}")
        print("  " + s["text"].replace("\n", "\n  "))
        if pct:
            print(f"  Оцінка: {pct} — {tier}")

        wr = bot.rule_win_rate(cd, s.get("rule", "OTHER"))
        if wr is not None:
            print(f"  Реальний win-rate правила ({s['rule']}, 14d): {wr:.0f}%")
        else:
            print(f"  Реальний win-rate правила ({s['rule']}): н/д (недостатньо даних локально)")

        if s["type"] in ("LONG", "SHORT"):
            logged.append({
                "rule": s["rule"], "type": s["type"], "strength": s["strength"],
            })

    if logged:
        record = {
            "ts": datetime.now(KYIV).isoformat(),
            "symbol": symbol,
            "price": ind["price"],
            "rsi": round(ind["rsi"], 1),
            "htf_trend": htf_trend,
            "off_high_pct": round(ind["off_high_pct"], 2),
            "off_low_pct": round(ind["off_low_pct"], 2),
            "signals": logged,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"\n(записано в {LOG_FILE} для майбутнього зіставлення з результатом)")


if __name__ == "__main__":
    main()
