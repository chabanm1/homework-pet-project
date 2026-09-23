"""
Надбудови над ансамблем трендів (trend_ens.py) з другого огляду літератури (2026-09-24).

ПРЕ-РЕЄСТРАЦІЯ (до першого запуску; параметри не підбираються). База TE = trend_ens.py як є (витрати 0.10%).
  A  PVT   — таргетування волатильності на рівні ПОРТФЕЛЯ: TE-ваги × k, k = 25% / (річна волатильність TE-дохідності
             за попередні 60 днів), загальна експозиція ≤ 100% (без плеча).
  B  XSM   — нахил на моментум між монетами (Liu–Tsyvinski–Wu 2022): TE-ваги лише для топ-10 доступних монет
             за дохідністю 28 днів, ×2 (решта 0).
  C  STBL  — режим ліквідності: TE-ваги × 0, якщо сукупна пропозиція стейблкоїнів (DefiLlama) за 30 днів
             впала, інакше × 1. Дані з лагом 1 день.
  E  MOM5  — окрема стратегія: щопонеділка купуємо рівними частками топ-5 доступних монет за дохідністю 21 дня,
             100% капіталу, тримаємо тиждень.
  Бонферроні 0.05/4. Половини H1 < 2023-04-01 ≤ H2.
  Для A/B/C: PASS = Sharpe > Sharpe(TE) в обох половинах І нижня межа CI різниці Sharpe (парний блоковий
             bootstrap 30 днів) > 0. maybe = краще в обох половинах.
  Для E:     PASS = Sharpe > Sharpe(BTC B&H) в обох половинах І MaxDD менша І нижня межа CI Sharpe > 0.

    python backtest/trend_plus.py
"""
import contextlib, io, runpy
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(str(HERE / "trend_ens.py"))
C, R, S, elig, scale, stats, SPLIT = g["C"], g["R"], g["S"], g["elig"], g["scale"], g["stats"], g["SPLIT"]
COST, B, ALPHA = 0.001, 5000, 0.05 / 4
cols = list(C.columns)
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)


def te_weights():
    e = elig[cols]
    return (S[cols] * scale[cols]).where(e).div(e.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)


def ret(W):
    W = W.fillna(0)
    net = ((W * R[cols].shift(-1)).sum(axis=1) - COST * W.diff().abs().sum(axis=1)).shift(1)
    return net


START = pd.Timestamp("2021-01-02", tz="UTC")
W0 = te_weights()
r0 = ret(W0)

# A — портфельне таргетування волатильності
rv = r0.rolling(60).std().shift(0) * np.sqrt(365)          # відомо на закритті дня (r0 вже зсунута на день реалізації)
k = (0.25 / rv).replace(np.inf, np.nan)
WA = W0.mul(k, axis=0)
WA = WA.div(np.maximum(WA.sum(axis=1), 1.0), axis=0)

# B — топ-10 за 28 днями
r28 = (C / C.shift(28) - 1).where(elig)
WB = (W0 * (r28.rank(axis=1, ascending=False) <= 10)) * 2

# C — стейблкоїни
js = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=30).json()
st = pd.Series({pd.Timestamp(int(x["date"]), unit="s", tz="UTC"): x["totalCirculatingUSD"].get("peggedUSD", np.nan) for x in js})
st = st.sort_index().reindex(C.index).ffill().shift(1)
up = (st / st.shift(30) - 1) > 0
WC = W0.mul(up.astype(float), axis=0)

# E — топ-5 за 21 днем, ребаланс щопонеділка
r21 = (C / C.shift(21) - 1).where(elig)
top5 = (r21.rank(axis=1, ascending=False) <= 5).astype(float)
top5 = top5.div(top5.sum(axis=1).replace(0, np.nan), axis=0)
WE = top5.where(pd.Series(C.index.dayofweek == 0, index=C.index), np.nan).ffill().fillna(0)

btc = R["BTC"].shift(-1).shift(1)
series = {"TE (база)": r0, "A PVT": ret(WA), "B XSM": ret(WB), "C STBL": ret(WC), "E MOM5": ret(WE), "BTC B&H": btc}
series = {n: s[s.index >= START].dropna() for n, s in series.items()}
expo = {"TE (база)": W0, "A PVT": WA, "B XSM": WB, "C STBL": WC, "E MOM5": WE}


def blocks(n, block=30, seed=5):
    nb = int(np.ceil(n / block))
    st_ = np.random.default_rng(seed).integers(0, n, (B, nb))
    return (st_[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n] % n


def sh(a):
    return a.mean(-1) / a.std(-1) * np.sqrt(365)


base = series["TE (база)"]
ii = blocks(len(base))
b0 = base.to_numpy()[ii]
rows = []
bt = series["BTC B&H"]
for name, x in series.items():
    x = x.reindex(base.index).fillna(0)
    f, h1, h2 = stats(x), stats(x[x.index < SPLIT]), stats(x[x.index >= SPLIT])
    bx = x.to_numpy()[ii]
    lo, hi = np.percentile(sh(bx), [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    dlo, dhi = np.percentile(sh(bx) - sh(b0), [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    if name in ("A PVT", "B XSM", "C STBL"):
        better = h1["Sharpe"] > stats(base[base.index < SPLIT])["Sharpe"] and h2["Sharpe"] > stats(base[base.index >= SPLIT])["Sharpe"]
        v = "PASS" if better and dlo > 0 else ("maybe" if better else "—")
    elif name == "E MOM5":
        ok = (h1["Sharpe"] > stats(bt[bt.index < SPLIT])["Sharpe"] and h2["Sharpe"] > stats(bt[bt.index >= SPLIT])["Sharpe"]
              and f["MaxDD"] > stats(bt)["MaxDD"])
        v = "PASS" if ok and lo > 0 else ("maybe" if ok else "—")
    else:
        v = "база" if name.startswith("TE") else "бенчмарк"
    e = expo.get(name)
    rows.append(dict(стратегія=name, CAGR=f"{f['CAGR']*100:+.0f}%", vol=f"{f['vol']*100:.0f}%", Sharpe=round(f["Sharpe"], 2),
                     CI_Sharpe=f"[{lo:+.2f};{hi:+.2f}]", dSharpe_vs_TE=f"[{dlo:+.2f};{dhi:+.2f}]",
                     Sh_H1=round(h1["Sharpe"], 2), Sh_H2=round(h2["Sharpe"], 2), MaxDD=f"{f['MaxDD']*100:.0f}%",
                     експоз=f"{e.sum(axis=1)[e.index >= START].mean()*100:.0f}%" if e is not None else "100%", вердикт=v))
print(f"Період {START:%Y-%m-%d} -> {C.index[-1]:%Y-%m-%d}, витрати {COST*100:.2f}%, CI {100*(1-ALPHA):.1f}% (Бонферроні 0.05/4)")
print(pd.DataFrame(rows).to_string(index=False))
print("\nПо роках, %:")
print(pd.DataFrame({n: x.groupby(x.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100) for n, x in series.items()}).T.round(0).to_string())
print(f"\nСтейблкоїни: частка днів з ростом за 30 днів = {up[up.index >= START].mean()*100:.0f}%; останнє значення: "
      f"{'ріст' if up.iloc[-1] else 'падіння'} ({(st.iloc[-1]/st.iloc[-31]-1)*100:+.1f}% за 30 днів)")
