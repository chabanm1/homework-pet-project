"""
Відтворення Zarattini, Pagani, Barbon (2025) «Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins»
(SSRN 5209907) на наших 20 монетах. Денні свічки Binance USDM з 2020-01 (кеш bt_data/{SYM}_1d_long.pkl).

ПРЕ-РЕЄСТРАЦІЯ (2026-09-24, до першого запуску). Правила взяті з опису статті, параметри не підбираються.
  Для кожної монети і кожного L ∈ {5,10,20,30,60,90,150,250,360} днів — окрема «підстратегія»:
    вхід:  close[t] > max(close[t-L .. t-1])
    стоп:  stop = max(попередній stop, (max + min)/2 closes[t-L .. t-1]); вихід, коли close[t] < stop
  Сигнал монети = частка з 9 підстратегій у позиції (0..1). Лише лонг, без плеча.
  Розмір: вага монети = (1 / N доступних) × сигнал × min(25% / річна волатильність за 90 днів, 1).
  Монета доступна через 365 днів після початку її історії. Решта капіталу — кеш (0%).
  Рішення на денному закритті (00:00 UTC), позиція на наступну добу, щоденне ребалансування.
  Витрати: 0.10% (Binance spot taker) і 0.40% (Kraken spot taker, малий обсяг) за одиницю обороту.
  Funding не рахуємо — стратегія для СПОТУ.
  Комірки: PORT (усі 20 монет) і BTC_ONLY (та сама логіка лише на BTC, N=1). Бонферроні 0.05/2.
  Бенчмарки: BTC купив-і-тримай; рівновага всіх доступних монет (щомісячне ребалансування).
  PASS = Sharpe > Sharpe(BTC B&H) в обох половинах (H1 < 2023-04-01 ≤ H2) І MaxDD менша за BTC B&H
         І нижня межа CI Sharpe (блоковий bootstrap 30 днів, 97.5%) > 0 — при витратах 0.10%.
  Застереження: наші 20 монет — ТЕПЕРІШНІЙ топ (вижили й виросли) => упередження вижилих, оцінка завищена.

    python backtest/trend_ens.py            # докачує відсутні дані і рахує
"""
import sys, warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as bdata  # noqa: E402

CACHE = bdata.CACHE
LBS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
SPLIT = pd.Timestamp("2023-04-01", tz="UTC")
B = 5000
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)


def get(sym):
    p = CACHE / f"{sym}_1d_long.pkl"
    if not p.exists():
        df = bdata.download(sym, date(2020, 1, 1), tf="1d")
        if df.empty:
            return sym, None
        df.to_pickle(p)
    df = pd.read_pickle(p)
    return sym, df.set_index("time")["close"]


with ThreadPoolExecutor(6) as ex:
    C = pd.DataFrame({s: c for s, c in ex.map(get, bdata.SYMBOLS) if c is not None}).sort_index()
print(f"Дані: {C.shape[1]} монет, {C.index[0]:%Y-%m-%d} -> {C.index[-1]:%Y-%m-%d}")
print("початок історії:", {s: f"{C[s].first_valid_index():%Y-%m}" for s in C})


def signal(c):
    """Частка з 9 Donchian-підстратегій у позиції на кожен день (NaN до появи даних)."""
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


R = C.pct_change()
S = pd.DataFrame({s: signal(C[s]) for s in C})
age = C.notna().cumsum()
elig = (age > 365) & C.notna()
rv = R.rolling(90).std() * np.sqrt(365)
scale = np.minimum(0.25 / rv, 1.0)


def run(cols, cost):
    e = elig[cols]
    N = e.sum(axis=1).replace(0, np.nan)
    W = (S[cols] * scale[cols]).where(e).div(N, axis=0).fillna(0)
    gross = (W * R[cols].shift(-1)).sum(axis=1)
    turn = W.diff().abs().sum(axis=1)
    net = (gross - cost * turn).shift(1)
    start = e.any(axis=1).idxmax()
    return net[net.index > start].dropna(), W[W.index > start], turn[turn.index > start]


def ew_bh(cols):
    e = elig[cols]
    rr = R[cols].shift(-1).where(e)
    return rr.mean(axis=1).shift(1)


def stats(x):
    eq = (1 + x).cumprod(); yrs = len(x) / 365
    return dict(CAGR=eq.iloc[-1] ** (1 / yrs) - 1, vol=x.std() * np.sqrt(365),
                Sharpe=x.mean() / x.std() * np.sqrt(365), MaxDD=(eq / eq.cummax() - 1).min())


def boot_sharpe(x, alpha, block=30, seed=5):
    a = x.to_numpy(); n = len(a); nb = int(np.ceil(n / block))
    st = np.random.default_rng(seed).integers(0, n, (B, nb))
    idx = (st[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n] % n
    s = a[idx]
    sh = s.mean(1) / s.std(1) * np.sqrt(365)
    return np.percentile(sh, 100 * alpha / 2), np.percentile(sh, 100 * (1 - alpha / 2))


ALL = list(C.columns)
cells = {"PORT": ALL, "BTC_ONLY": ["BTC"]}
res, rows = {}, []
for name, cols in cells.items():
    for cost in (0.001, 0.004):
        x, W, turn = run(cols, cost)
        res[(name, cost)] = x
        rows.append((name, cost, x, W, turn))
start = res[("PORT", 0.001)].index[0]
btc = R["BTC"].shift(-1).shift(1)[lambda s: s.index >= start].dropna()
ew = ew_bh(ALL)[lambda s: s.index >= start].dropna()

out = []
b1, b2, bf = stats(btc[btc.index < SPLIT]), stats(btc[btc.index >= SPLIT]), stats(btc)
for name, cost, x, W, turn in rows:
    x = x[x.index >= start]
    f, h1, h2 = stats(x), stats(x[x.index < SPLIT]), stats(x[x.index >= SPLIT])
    lo, hi = boot_sharpe(x, 0.05 / 2)
    ok = h1["Sharpe"] > b1["Sharpe"] and h2["Sharpe"] > b2["Sharpe"] and f["MaxDD"] > bf["MaxDD"]
    v = ("PASS" if ok and lo > 0 else ("maybe" if ok else "—")) if cost == 0.001 else "(довідково)"
    out.append(dict(комірка=name, витрати=f"{cost*100:.2f}%", CAGR=f"{f['CAGR']*100:+.0f}%", vol=f"{f['vol']*100:.0f}%",
                    Sharpe=round(f["Sharpe"], 2), CI_97_5=f"[{lo:+.2f};{hi:+.2f}]", Sh_H1=round(h1["Sharpe"], 2),
                    Sh_H2=round(h2["Sharpe"], 2), MaxDD=f"{f['MaxDD']*100:.0f}%",
                    експозиція=f"{W.sum(axis=1).mean()*100:.0f}%", оборот_на_рік=f"{turn.sum()/(len(turn)/365)*100:.0f}%",
                    вердикт=v))
for nm, x in (("BTC B&H", btc), ("EW B&H (усі монети)", ew)):
    f, h1, h2 = stats(x), stats(x[x.index < SPLIT]), stats(x[x.index >= SPLIT])
    lo, hi = boot_sharpe(x, 0.05 / 2)
    out.append(dict(комірка=nm, витрати="—", CAGR=f"{f['CAGR']*100:+.0f}%", vol=f"{f['vol']*100:.0f}%", Sharpe=round(f["Sharpe"], 2),
                    CI_97_5=f"[{lo:+.2f};{hi:+.2f}]", Sh_H1=round(h1["Sharpe"], 2), Sh_H2=round(h2["Sharpe"], 2),
                    MaxDD=f"{f['MaxDD']*100:.0f}%", експозиція="100%", оборот_на_рік="—", вердикт="бенчмарк"))
print(f"\nПеріод: {start:%Y-%m-%d} -> {C.index[-1]:%Y-%m-%d}")
print(pd.DataFrame(out).to_string(index=False))

print("\nПо роках, %:")
yr = {}
for key, lab in ((("PORT", 0.001), "PORT 0.10%"), (("PORT", 0.004), "PORT 0.40%"), (("BTC_ONLY", 0.001), "BTC_ONLY 0.10%")):
    x = res[key][lambda s: s.index >= start]
    yr[lab] = x.groupby(x.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100)
yr["BTC B&H"] = btc.groupby(btc.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100)
yr["EW B&H"] = ew.groupby(ew.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100)
print(pd.DataFrame(yr).T.round(0).to_string())

W = run(ALL, 0.001)[1]
print("\nПоточні ваги (останній день), % капіталу:")
last = W.iloc[-1]
print({s: round(v * 100, 1) for s, v in last.sort_values(ascending=False).items() if v > 0}, f"| разом {last.sum()*100:.0f}%")
