"""
Щоденне повідомлення трендової системи + паперовий рахунок (GitHub Actions, .github/workflows/trend_daily.yml).

Запуск раз на добу після денного закриття (00:05 UTC = 03:05 Київ літом). Нічого не торгує і не має ключів біржі —
лише публічні денні свічки Kraken і Telegram.

Паперовий рахунок: угоди BTC і ETH за правилами trend_signal.trade_state() (ті самі, що в backtest/trend_lev.py),
починаючи з PAPER_START. Угоду, відкриту системою раніше, рахуємо так, ніби ми приєдналися до неї за закриттям
дня перед PAPER_START (з тодішнім стопом). Стану не зберігаємо — щоразу відтворюємо з історії свічок
(Kraken дає 720 денних барів, модель потребує 360 => відтворення коректне ~рік від PAPER_START).
R = (вихід/вхід − 1 − 0.10% комісій) / (відстань до стопу на вході). Funding не враховано.

    python trend_daily.py           # локально без TELEGRAM_TOKEN — друкує повідомлення в консоль
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import signal_notifier_v2 as bot
from trend_signal import trade_state

KYIV = ZoneInfo("Europe/Kyiv")
PAPER_START = pd.Timestamp("2026-09-24", tz="UTC")
PAPER_SYMS = ["BTC/USD", "ETH/USD"]
FEES = 0.001


def fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 1000 else (f"{x:,.2f}" if x >= 10 else f"{x:.4g}")


def daily(sym: str) -> tuple[pd.DataFrame, float] | None:
    df = bot.fetch_ohlcv(sym, "1d", 720)
    if df.empty or len(df) < 400:
        return None
    d = df.set_index("time")[["open", "high", "low", "close"]]
    return d.iloc[:-1], float(d["close"].iat[-1])          # лише закриті дні + поточна ціна


def paper(sym: str, d: pd.DataFrame, st: dict, px: float) -> tuple[list[dict], dict | None]:
    """Угоди паперового рахунку з PAPER_START: закриті та відкрита (з R на поточний момент)."""
    before = d.index[d.index < PAPER_START]
    join_day = before[-1] if len(before) else None

    def rebase(p):
        if p["t0"] >= PAPER_START or join_day is None:
            return p["t0"], p["entry"], p["stop0"]
        stop_then = max(v for t, v in p["stops"].items() if t <= join_day)
        return join_day, float(d["close"].loc[join_day]), stop_then

    def r_of(entry, stop0, exit_px):
        risk = 1 - stop0 / entry
        return ((exit_px / entry - 1) - FEES) / risk if risk > 0 else float("nan")

    closed = []
    for tr in st["trades"]:
        if tr["t1"] < PAPER_START:
            continue
        t0, entry, stop0 = rebase(tr)
        if stop0 >= entry:
            continue
        closed.append(dict(sym=sym, t0=t0, entry=entry, t1=tr["t1"], exit=tr["exit"], how=tr["how"], R=r_of(entry, stop0, tr["exit"])))
    p = st["pos"]
    if p is None:
        return closed, None
    t0, entry, stop0 = rebase(p)
    if stop0 >= entry:
        return closed, None
    return closed, dict(sym=sym, t0=t0, entry=entry, stop0=stop0, R_now=r_of(entry, stop0, px))


def main():
    now = datetime.now(KYIV)
    lines = [f"📐 <b>ТРЕНДОВА СИСТЕМА</b> — {now:%d.%m %H:%M}", "━━━━━━━━━━━━━━━━"]
    closed_all, open_all, in_trend, total = [], [], 0, 0
    last_close = None
    for sym in bot.SYMBOLS:
        got = daily(sym)
        if got is None:
            continue
        d, px = got
        st = trade_state(d)
        n = int(st["n"].iat[-1]); n_prev = int(st["n"].iat[-2])
        total += 1; in_trend += n >= 5
        last_close = d.index[-1]
        if sym not in PAPER_SYMS:
            continue
        coin = sym.split("/")[0]
        p = st["pos"]
        arrow = "↑" if n > n_prev else ("↓" if n < n_prev else "=")
        head = f"<b>{coin}</b> {fmt(px)} · модель {n}/9 {arrow}"
        if p is not None:
            stop = p["stop_next"]; dist = 1 - stop / px
            fresh = p["t0"] == d.index[-1]
            act = "🟢 <b>НОВИЙ ВХІД ЛОНГ</b>" if fresh else "🟢 тримати лонг"
            lines.append(f"{head}\n{act} (з {p['t0']:%d.%m} за {fmt(p['entry'])})")
            if dist > 0:
                lines.append(f"🛑 стоп <b>{fmt(stop)}</b> ({-dist*100:.1f}%) · розмір: ризик 1% = {0.01/dist:.2f}× депо, 2% = {0.02/dist:.2f}× депо")
            else:
                lines.append(f"⚠️ ціна нижче стопу {fmt(stop)} — вихід")
        else:
            just_closed = [t for t in st["trades"] if t["t1"] == d.index[-1]]
            if just_closed:
                lines.append(f"{head}\n🔴 <b>ВИХІД</b> ({just_closed[0]['how']}, за {fmt(just_closed[0]['exit'])})")
            elif n >= 5:
                lines.append(f"{head}\n🟡 тренд є, але входу за правилом немає — чекати")
            else:
                lines.append(f"{head}\n⚪ поза ринком — вхід, коли модель ≥ 5/9")
        c, o = paper(sym, d, st, px)
        closed_all += c
        if o:
            open_all.append(o)
        lines.append("")
    if total == 0:
        print("немає даних Kraken — повідомлення не надіслано")
        return
    lines.append(f"🌡 Ринок: у тренді {in_trend}/{total} монет (модель ≥ 5/9)")

    lines += ["", f"📒 <b>Паперовий рахунок</b> (з {PAPER_START:%d.%m}, BTC+ETH, лише лонг)"]
    if not closed_all and not open_all:
        lines.append("угод ще немає")
    else:
        if closed_all:
            R = [t["R"] for t in closed_all]
            wins = sum(r > 0 for r in R)
            lines.append(f"закрито {len(R)}: {wins} в плюс, разом <b>{sum(R):+.2f}R</b> (сер. {sum(R)/len(R):+.2f}R)")
            for t in closed_all[-3:]:
                lines.append(f"· {t['sym'].split('/')[0]} {t['t0']:%d.%m}→{t['t1']:%d.%m} {t['how']}: {t['R']:+.2f}R")
        for o in open_all:
            lines.append(f"· {o['sym'].split('/')[0]} відкрита з {o['t0']:%d.%m} за {fmt(o['entry'])}: зараз {o['R_now']:+.2f}R")
        lines.append("<i>1R = втрата на стопі; при ризику 2% депо: +1R = +2% депо</i>")
    lines.append(f"\n<i>Денна свічка закрита {last_close:%d.%m}. Стоп — ордером на біржі. Не фінансова порада.</i>")
    msg = "\n".join(lines)
    if not bot.tg(msg):
        print(f"TG не надіслано\n{msg}")


if __name__ == "__main__":
    main()
