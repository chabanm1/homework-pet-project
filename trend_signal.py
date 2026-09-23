"""
Ансамбль Donchian-трендів (Zarattini, Pagani, Barbon 2025) — ті самі правила, що в backtest/trend_ens.py
і backtest/trend_lev.py, винесені окремо, щоб живі інструменти не тягнули за собою бектест.
"""
import numpy as np
import pandas as pd

LBS = (5, 10, 20, 30, 60, 90, 150, 250, 360)


def signal(c: pd.Series) -> pd.Series:
    """Частка з 9 підстратегій у позиції на кожен день (0..1, NaN до появи даних).
    Вхід: close > max(close за L попередніх днів). Стоп: max(стоп, середина діапазону), вихід, коли close < стоп."""
    a = c.to_numpy(); n = len(a)
    inpos = np.zeros((n, len(LBS)))
    for j, L in enumerate(LBS):
        mx = c.rolling(L).max().shift(1).to_numpy()
        mn = c.rolling(L).min().shift(1).to_numpy()
        on, stop = False, -np.inf
        for t in range(n):
            if np.isnan(a[t]) or np.isnan(mx[t]):
                continue
            mid = (mx[t] + mn[t]) / 2
            if on:
                stop = max(stop, mid)
                if a[t] < stop:
                    on = False
            elif a[t] > mx[t]:
                on, stop = True, mid
            inpos[t, j] = on
    return pd.Series(inpos.mean(1), index=c.index).where(c.notna())


def trade_state(d: pd.DataFrame) -> dict:
    """Стан угоди за правилами trend_lev.py (DC20) на денних свічках d[open, high, low, close] (лише закриті дні).
    Вхід: n >= 5 після n <= 4. Стоп: середина 20 попередніх закриттів, лише вгору. Вихід: low <= стоп або n <= 2."""
    n = (signal(d["close"]) * 9).round()
    mid20 = (d["close"].rolling(20).max() + d["close"].rolling(20).min()).shift(1) / 2
    pos = None
    for i in range(1, len(d)):
        t = d.index[i]
        if pos is not None:
            if d["low"].iat[i] <= pos["stop"]:
                pos = None
            elif n.iat[i] <= 2:
                pos = None
            else:
                pos["stop"] = max(pos["stop"], mid20.iat[i])
        if pos is None and n.iat[i] >= 5 and n.iat[i - 1] <= 4 and mid20.iat[i] < d["close"].iat[i]:
            pos = dict(t0=t, entry=d["close"].iat[i], stop=mid20.iat[i])
    c = d["close"]
    stop_next = (c.iloc[-20:].max() + c.iloc[-20:].min()) / 2       # середина з урахуванням останнього закриття
    if pos is not None:
        pos["stop_next"] = max(pos["stop"], stop_next)
    return dict(n=n, pos=pos, stop_next=stop_next)
