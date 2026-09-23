"""
TREND NOW — живий стан трендової системи (єдина стратегія, що пройшла бектест: backtest/trend_ens.py, trend_lev.py).

    python trend_now.py              # BTC, ETH, SOL
    python trend_now.py all          # усі 20 монет бота

Денні свічки Kraken (закриття 00:00 UTC = 03:00 Київ літом; незакритий день ігнорується).
Правила угоди (лише ЛОНГ — шорти в бектесті значимо гірші, backtest/trend_ls.py):
  вхід   — модель переходить на >= 5/9 (після <= 4/9), на денному закритті
  стоп   — середина діапазону 20 останніх закриттів, лише вгору; перераховувати щодня після 03:00
  вихід  — спрацював стоп або модель <= 2/9
  розмір — ризик 1–2% депозиту: номінал = ризик / відстань до стопу (плече — наслідок, не мета)
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import signal_notifier_v2 as bot
from trend_signal import trade_state

KYIV = ZoneInfo("Europe/Kyiv")


def fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 1000 else (f"{x:,.2f}" if x >= 10 else f"{x:.4g}")


syms = bot.SYMBOLS if "all" in sys.argv[1:] else ["BTC/USD", "ETH/USD", "SOL/USD"]

print(f"TREND NOW  {datetime.now(KYIV):%Y-%m-%d %H:%M} Київ\n")
for sym in syms:
    df = bot.fetch_ohlcv(sym, "1d", 720)
    if df.empty or len(df) < 60:
        print(f"{sym}: немає даних"); continue
    d = df.set_index("time")[["open", "high", "low", "close"]]
    last_px = d["close"].iat[-1]
    d = d.iloc[:-1]                                              # лише закриті дні
    st = trade_state(d)
    n = st["n"].dropna().astype(int)
    hist = " ".join(str(x) for x in n.iloc[-7:])
    p = st["pos"]
    print(f"{sym:9s} ціна {fmt(last_px)}  модель {n.iat[-1]}/9  (7 днів: {hist})  закрито {d.index[-1]:%m-%d}")
    if p:
        stop = p["stop_next"]; dist = 1 - stop / last_px
        print(f"   🟢 СИСТЕМА В ЛОНГУ з {p['t0']:%m-%d} за {fmt(p['entry'])}; стоп {fmt(stop)} ({-dist*100:+.1f}% від ціни)")
        if dist > 0:
            print(f"      розмір: ризик 1% -> номінал {0.01/dist:.2f}× депозиту, 2% -> {0.02/dist:.2f}× депозиту")
        else:
            print("      ціна вже нижче стопу — вихід / не входити")
    elif n.iat[-1] >= 5:
        print(f"   🟡 модель >= 5/9, але угоди за правилом немає (вхід був раніше і закрився, або стоп вище ціни) — чекати")
    else:
        print(f"   ⚪ поза ринком — вхід, коли модель стане >= 5/9")
print("\nОновлюй після 03:00 Київ (денне закриття). Стоп — ордером на біржі, ізольована маржа.")
