"""
Ансамбль трендів (trend_ens.py) з ШОРТАМИ — для ф'ючерсів (2026-09-24).

ПРЕ-РЕЄСТРАЦІЯ (до першого запуску). Шорт-підстратегії — дзеркало лонгових: вхід, коли close < min(close за L попередніх
днів), стоп = середина діапазону, лише вниз; вихід, коли close > стоп. Сигнал шорту = частка з 9 у позиції.
  TE_LS  вага = (1/N) × (сигнал_лонг − сигнал_шорт) × min(25% / rv90, 1). Витрати 0.10% за одиницю обороту.
         Funding враховано лише для BTC/ETH (для альтів історії до 2024 немає) — знак: лонг платить, шорт отримує.
  PASS = Sharpe > Sharpe(TE лонг-only, та сама обробка funding) в обох половинах (H1 < 2023-04-01 ≤ H2)
         І нижня межа 95% CI різниці Sharpe (парний блоковий bootstrap 30 днів) > 0.  maybe = краще в обох половинах.

    python backtest/trend_ls.py
"""
import contextlib, io, runpy
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(str(HERE / "trend_ens.py"))
C, R, S, elig, scale, stats, signal, SPLIT = (g[k] for k in ("C", "R", "S", "elig", "scale", "stats", "signal", "SPLIT"))
cols = list(C.columns)
SS = pd.DataFrame({s: signal(-C[s]) for s in cols})

fund = pd.DataFrame(0.0, index=C.index, columns=cols)
for s in ("BTC", "ETH"):
    f = pd.read_pickle(HERE / "bt_data" / f"funding_long_{s}.pkl")
    fs = pd.Series(f["rate"].to_numpy(), index=pd.to_datetime(f["ts"], unit="ms", utc=True))
    fd = fs.resample("1D", offset="1h").sum(); fd.index = fd.index.floor("D")
    fund[s] = fd.reindex(C.index).fillna(0)


def run(sig):
    e = elig[cols]
    W = (sig * scale[cols]).where(e).div(e.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    net = ((W * R[cols].shift(-1)).sum(axis=1) - (W * fund.shift(-1)).sum(axis=1) - 0.001 * W.diff().abs().sum(axis=1)).shift(1)
    return net[net.index >= "2021-01-02"].dropna(), W


lo_, WL = run(S[cols])
ls_, WLS = run(S[cols] - SS[cols])
sh_, WS = run(-SS[cols])
B = 5000
n = len(lo_); nb = int(np.ceil(n / 30))
ii = (np.random.default_rng(5).integers(0, n, (B, nb))[:, :, None] + np.arange(30)).reshape(B, -1)[:, :n] % n
sh = lambda a: a.mean(-1) / a.std(-1) * np.sqrt(365)
d = sh(ls_.to_numpy()[ii]) - sh(lo_.to_numpy()[ii])
dlo, dhi = np.percentile(d, [2.5, 97.5])
rows = []
for name, x, W in (("TE лонг-only", lo_, WL), ("TE_LS лонг+шорт", ls_, WLS), ("лише шорт-частина", sh_, WS)):
    f, h1, h2 = stats(x), stats(x[x.index < SPLIT]), stats(x[x.index >= SPLIT])
    rows.append(dict(стратегія=name, CAGR=f"{f['CAGR']*100:+.1f}%", vol=f"{f['vol']*100:.0f}%", Sharpe=round(f["Sharpe"], 2),
                     Sh_H1=round(h1["Sharpe"], 2), Sh_H2=round(h2["Sharpe"], 2), MaxDD=f"{f['MaxDD']*100:.0f}%",
                     брутто_експоз=f"{W.abs().sum(axis=1)[W.index >= '2021-01-02'].mean()*100:.0f}%"))
print(pd.DataFrame(rows).to_string(index=False))
better = rows[1]["Sh_H1"] > rows[0]["Sh_H1"] and rows[1]["Sh_H2"] > rows[0]["Sh_H2"]
print(f"різниця Sharpe LS − LO: 95% CI [{dlo:+.2f};{dhi:+.2f}] -> ВЕРДИКТ: {'PASS' if better and dlo > 0 else ('maybe' if better else '—')}")
print("\nПо роках, %:")
print(pd.DataFrame({r_["стратегія"]: x.groupby(x.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100)
                    for r_, x in zip(rows, (lo_, ls_, sh_))}).T.round(1).to_string())
