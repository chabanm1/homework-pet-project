"""
Сторож реальної позиції (GitHub Actions, .github/workflows/position_watch.yml, кожні 30 хв).

Нічого не торгує і не має ключів біржі — закриває позицію стоп-ордер на біржі. Сторож лише пише в Telegram:
  🔼 стоп системи піднявся вище твого — переставити стоп-ордер;
  ⚠️ ціна ближче ніж NEAR_PCT до стопу;
  🛑 ціна торкнулась стопу — перевірити, що позиція закрилась;
  🔴 система вийшла з угоди за сигналом (модель <= 2/9) — закрити вручну.
Кожне повідомлення — один раз на подію (стан у position_watch.json, кеш GitHub Actions).

Позиція задається в position.json: {"sym": "BTC/USD", "entry": 83793, "stop": 81089, "opened": "2026-09-24"}.
Немає файлу або "sym": null — сторож мовчить. Ціни з Kraken (BTC/USD), на Binance BTCUSDT відрізняються на десятки $.

    python position_watch.py        # локально без TELEGRAM_TOKEN — друкує в консоль
"""
import json
from pathlib import Path

import pandas as pd

import signal_notifier_v2 as bot
from trend_daily import daily, fmt
from trend_signal import trade_state

POS_FILE = Path("position.json")
STATE_FILE = Path("position_watch.json")
NEAR_PCT = 0.01


def main():
    if not POS_FILE.exists():
        print("position.json немає — позиції немає"); return
    pos = json.loads(POS_FILE.read_text(encoding="utf-8"))
    if not pos.get("sym"):
        print("позиції немає"); return
    sym, coin = pos["sym"], pos["sym"].split("/")[0]
    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if st.get("key") != f"{sym}@{pos['entry']}":                  # нова позиція — скинути стан
        st = {"key": f"{sym}@{pos['entry']}", "stop": pos["stop"], "sent": []}
    stop = max(float(pos["stop"]), st["stop"])                     # стоп тільки вгору

    got = daily(sym)
    h = bot.fetch_ohlcv(sym, "1h", 4)
    if got is None or h.empty:
        print("немає даних Kraken"); return
    d, px = got
    low = float(h["low"].iloc[-3:].min())                          # мінімум з попереднього запуску (з запасом)
    ts = trade_state(d)
    n = int(ts["n"].iat[-1])
    sys_pos = ts["pos"]
    r_now = (px / pos["entry"] - 1) / (1 - pos["stop"] / pos["entry"])
    head = f"<b>{coin}</b> {fmt(px)} · вхід {fmt(pos['entry'])} · {r_now:+.2f}R · модель {n}/9"
    msgs = []

    def once(event: str, text: str):
        if event not in st["sent"]:
            st["sent"].append(event); msgs.append(text)

    if low <= stop:
        once(f"hit@{stop:.2f}", f"🛑 <b>Ціна торкнулась стопу {fmt(stop)}</b> (мінімум {fmt(low)}).\n"
                                "Перевір на біржі, що позиція закрилась. Якщо ні — закрий вручну.")
    elif sys_pos is None:
        last = ts["trades"][-1] if ts["trades"] else None
        why = f"{last['how']}, {last['t1']:%d.%m}" if last else "угоди немає"
        once(f"exit@{d.index[-1]:%Y%m%d}", f"🔴 <b>Система вийшла з угоди</b> ({why}).\nЗакрий позицію вручну і зніми стоп-ордер.")
    else:
        new = float(sys_pos["stop_next"])
        if new > stop * 1.0005:
            once(f"raise@{new:.2f}", f"🔼 <b>Переставити стоп: {fmt(stop)} → {fmt(new)}</b> ({-(1 - new / px) * 100:.1f}% від ціни).\n"
                                     "Зміни стоп-ордер на біржі (Mark price, Market).")
            stop = new
        if px <= stop * (1 + NEAR_PCT):
            once(f"near@{stop:.2f}", f"⚠️ До стопу {fmt(stop)} лишилось {(px / stop - 1) * 100:.1f}%. Стоп-ордер на місці? Нічого не роби вручну.")
    st["stop"] = stop

    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    print(f"{head} · стоп {fmt(stop)} · low {fmt(low)} · нових подій {len(msgs)}")
    for m in msgs:
        if not bot.tg(f"👁 {head}\n{m}"):
            print(f"TG не надіслано\n{m}")


if __name__ == "__main__":
    main()
