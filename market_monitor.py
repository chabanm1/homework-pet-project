"""
=============================================================================
MARKET MONITOR — прогін усіх 20 монет через реальний пайплайн бота
=============================================================================
    python market_monitor.py

Те саме, що check_symbol.py, але одразу по всьому SYMBOLS-списку бота
(indicators + 4h-тренд + reversal-confirmation + signals()) — щоб на
запит "промоніторь ринок" відповідь одразу враховувала все, що бот уже
знає (а не збиралась вручну по шматках, як раніше).

Показує тільки монети з реальним LONG/SHORT сигналом (тобто вже
пройшли через штраф за контр-тренд і підтвердження розвороту),
відсортовані за силою. Логує кожну знайдену монету в
signal_checks_log.jsonl так само, як check_symbol.py.
=============================================================================
"""

import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import signal_notifier_v2 as bot
from check_symbol import strength_to_tier, LOG_FILE

KYIV = ZoneInfo("Europe/Kyiv")


def main():
    fg, fg_cls = bot.fetch_fg()
    cd = bot.load_cd()

    rows = []
    for sym in bot.SYMBOLS:
        try:
            df = bot.fetch_ohlcv(sym, "1h", 100)
            ind = bot.indicators(df)
            htf_trend = bot.fetch_trend(sym)
            sigs = bot.signals(ind, fg, htf_trend=htf_trend, cd=cd)
            directional = [s for s in sigs if s["type"] in ("LONG", "SHORT")]
            best = max(directional, key=lambda s: s["strength"]) if directional else None
            rows.append((sym, ind, htf_trend, best))
            time.sleep(0.25)
        except Exception as e:
            print(f"{sym}: ERROR {e}")

    print(f"\n=== Огляд ринку — {datetime.now(KYIV).strftime('%H:%M:%S')} (Europe/Kyiv) ===")
    print(f"F&G: {fg} ({fg_cls})\n")

    actionable = [(sym, ind, trend, best) for sym, ind, trend, best in rows if best]
    actionable.sort(key=lambda r: -r[3]["strength"])

    if not actionable:
        print("Жодна монета зараз не дає LONG/SHORT сигналу (після штрафу за тренд і перевірки розвороту).")
    else:
        log_batch = []
        for sym, ind, trend, best in actionable:
            pct, tier = strength_to_tier(best["strength"])
            wr = bot.rule_win_rate(cd, best.get("rule", "OTHER"))
            wr_txt = f"{wr*100:.0f}% (14d)" if wr is not None else "н/д"
            mark = "🟢" if best["type"] == "LONG" else "🔴"
            print(
                f"{mark} {sym:<10} {best['type']:<6} [{best['rule']}] сила={best['strength']}  "
                f"{pct} {tier}\n"
                f"    RSI={ind['rsi']:.0f}  тренд4h={trend}  offHigh={ind['off_high_pct']:+.2f}%  "
                f"offLow={ind['off_low_pct']:+.2f}%  реальний win-rate={wr_txt}"
            )
            log_batch.append({
                "ts": datetime.now(KYIV).isoformat(), "symbol": sym, "price": ind["price"],
                "rsi": round(ind["rsi"], 1), "htf_trend": trend,
                "off_high_pct": round(ind["off_high_pct"], 2), "off_low_pct": round(ind["off_low_pct"], 2),
                "signals": [{"rule": best["rule"], "type": best["type"], "strength": best["strength"]}],
            })

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for rec in log_batch:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    quiet = [sym for sym, _, _, best in rows if not best]
    if quiet:
        print(f"\nБез сигналу: {', '.join(quiet)}")


if __name__ == "__main__":
    main()
