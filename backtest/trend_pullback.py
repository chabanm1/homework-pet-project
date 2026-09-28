"""
Входи НА ВІДКАТІ всередині активного денного тренду — чи дають вони більше угод без втрати переваги?
Дані: денні свічки Binance USDM 20 монет (bt_data/{SYM}_1d_long.pkl, trend_ens.py).

ПРЕ-РЕЄСТРАЦІЯ (2026-09-28, до першого запуску; параметри не підбираються).
  Тренд активний у день t: n[t] >= 5 (n = скільки з 9 Donchian-підстратегій «в позиції», trend_signal.py)
                           і close[t] > mid20[t] (mid20 = середина 20 попередніх закриттів = стоп DC20).
  Комірки входу (на денному закритті, одна відкрита угода на монету на комірку):
    FRESH   базова система trend_lev/trend_signal: n[t] >= 5 і n[t-1] <= 4 (для порівняння, не тестується)
    ANY     контроль: будь-який день активного тренду, щойно немає відкритої угоди (вхід «коли завгодно»)
    PB3     close[t] — найнижче закриття з 3 останніх днів (3-денний відкат)
    PBATR   close[t] <= max(close за 20 днів) − 1.5 × ATR14
    PBHALF  (close − mid20) / (max(close за 20 днів) − mid20) <= 0.5 (ціна в нижній половині шляху від стопу до хаю)
  Стоп: mid20 на вході, далі лише вгору (mid20 кожного дня). Вихід: денний low <= стоп -> за min(open, стоп) − 0.10%;
        або n <= 2 на закритті -> за close. Вхід за close + 0.02%.
  Витрати: 0.05% комісія на кожен бік + funding фіксовано 0.03%/день (≈0.01%/8год) поки угода відкрита.
  R = (exit/entry − 1 − витрати) / (1 − stop0/entry).
  Період: входи з 2021-01-01 (перші 360 днів — розгін Donchian), H1 < 2023-04-01 <= H2.
  CI: bootstrap кластерами по тижню входу (угоди різних монет в один тиждень залежні), Бонферроні 0.05/3.
  PASS (для PB3, PBATR, PBHALF): середній R > 0 з CI > 0 і середній R > 0 в обох половинах.
  Додатково (довідково): різниця сер.R з ANY — чи відкат кращий за вхід «будь-коли в тренді».
  Застереження: survivorship (поточний топ-20), денний low замість годинного (стоп-гепи всередині дня не видно).

    python backtest/trend_pullback.py
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from trend_signal import signal   # noqa: E402  (те саме, що trend_ens.py)

CACHE = HERE / "bt_data"
SYMS = ["BTC", "ETH", "XRP", "SOL", "ZEC", "HYPE", "ADA", "TAO", "UNI", "DOGE",
        "NEAR", "SUI", "XLM", "LINK", "XMR", "LTC", "AAVE", "ARB", "ENA", "INJ"]
START, SPLIT = pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2023-04-01", tz="UTC")
FEE, SLIP_IN, SLIP_STOP, FUND_DAY = 0.0005, 0.0002, 0.001, 0.0003
CELLS = ["FRESH", "ANY", "PB3", "PBATR", "PBHALF"]
TESTED = ["PB3", "PBATR", "PBHALF"]
ALPHA = 0.05 / len(TESTED)
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)


def load(sym):
    k = pd.read_pickle(CACHE / f"{sym}_1d_long.pkl")
    d = k.set_index("time")[["open", "high", "low", "close"]].astype(float)
    d.index = pd.to_datetime(d.index, utc=True)
    d = d.iloc[:-1]                                                     # останній день може бути незакритим
    d["n"] = (signal(d["close"]) * 9).round()
    d["mid20"] = (d.close.rolling(20).max() + d.close.rolling(20).min()).shift(1) / 2
    d["hi20"] = d.close.rolling(20).max()
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["low3"] = d.close.rolling(3).min()
    return d


def entry_ok(cell, d, i):
    r = d.iloc[i]
    active = r.n >= 5 and r.close > r.mid20
    if cell == "FRESH":
        return r.n >= 5 and d.n.iat[i - 1] <= 4 and r.close > r.mid20
    if not active:
        return False
    if cell == "ANY":
        return True
    if cell == "PB3":
        return r.close <= r.low3
    if cell == "PBATR":
        return r.close <= r.hi20 - 1.5 * r.atr
    if cell == "PBHALF":
        span = r.hi20 - r.mid20
        return span > 0 and (r.close - r.mid20) / span <= 0.5
    raise ValueError(cell)


def trades_for(sym, d, cell):
    out, pos = [], None
    for i in range(1, len(d)):
        t, r = d.index[i], d.iloc[i]
        if pos is not None:
            pos["days"] += 1
            if r.low <= pos["stop"]:
                px, how = min(r.open, pos["stop"]) * (1 - SLIP_STOP), "стоп"
            elif r.n <= 2:
                px, how = r.close, "сигнал"
            else:
                if not np.isnan(r.mid20):
                    pos["stop"] = max(pos["stop"], r.mid20)
                continue
            cost = 2 * FEE + FUND_DAY * pos["days"]
            R = (px / pos["entry"] - 1 - cost) / pos["risk"]
            out.append(dict(sym=sym, cell=cell, t0=pos["t0"], t1=t, R=R, days=pos["days"], how=how, risk=pos["risk"]))
            pos = None
            continue                                                    # у день виходу не входимо знову
        if t < START or np.isnan(r.mid20) or np.isnan(r.n) or np.isnan(r.atr):
            continue
        if entry_ok(cell, d, i):
            entry = r.close * (1 + SLIP_IN)
            risk = 1 - r.mid20 / entry
            if risk > 0:
                pos = dict(t0=t, entry=entry, stop=r.mid20, risk=risk, days=0)
    return out


def cluster_ci(tr, alpha, B=10000, seed=7):
    wk = tr.t0.dt.to_period("W").astype(str)
    g = tr.groupby(wk).R.agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    ii = np.random.default_rng(seed).integers(0, len(g), (B, len(g)))
    m = s[ii].sum(1) / c[ii].sum(1)
    return np.percentile(m, 100 * alpha / 2), np.percentile(m, 100 * (1 - alpha / 2))


data = {s: load(s) for s in SYMS if (CACHE / f"{s}_1d_long.pkl").exists()}
end = max(d.index[-1] for d in data.values())
yrs = (end - START).days / 365.25
all_tr = pd.DataFrame([t for cell in CELLS for s, d in data.items() for t in trades_for(s, d, cell)])

rows = []
for cell in CELLS:
    tr = all_tr[all_tr.cell == cell]
    lo, hi = cluster_ci(tr, ALPHA)
    h1, h2 = tr[tr.t0 < SPLIT].R.mean(), tr[tr.t0 >= SPLIT].R.mean()
    last12 = tr[tr.t0 >= end - pd.Timedelta(days=365)]
    verdict = ("PASS" if lo > 0 and h1 > 0 and h2 > 0 else "—") if cell in TESTED else "(база)"
    rows.append(dict(комірка=cell, угод=len(tr), на_місяць=round(len(tr) / yrs / 12, 1),
                     за_12міс=len(last12), R_12міс=round(last12.R.mean(), 2),
                     WR=f"{(tr.R > 0).mean()*100:.0f}%", серR=round(tr.R.mean(), 3),
                     CI=f"[{lo:+.2f};{hi:+.2f}]", R_H1=round(h1, 2), R_H2=round(h2, 2),
                     медіана_стоп=f"{tr.risk.median()*100:.1f}%", днів=round(tr.days.median()),
                     сума_R_на_рік=round(tr.R.sum() / yrs, 1), вердикт=verdict))
print(f"Входи {START:%Y-%m-%d} -> {end:%Y-%m-%d} ({yrs:.1f} р), {len(data)} монет, CI {100*(1-ALPHA):.1f}% кластерами по тижнях")
print(pd.DataFrame(rows).to_string(index=False))

print("\nРізниця сер.R з ANY (чи відкат кращий за вхід будь-коли в тренді):")
anyR = all_tr[all_tr.cell == "ANY"]
for cell in TESTED:
    tr = all_tr[all_tr.cell == cell]
    rng = np.random.default_rng(11); diffs = []
    wa, wb = anyR.t0.dt.to_period("W").astype(str), tr.t0.dt.to_period("W").astype(str)
    weeks = np.array(sorted(set(wa) | set(wb)))
    ga, gb = anyR.groupby(wa).R.agg(["sum", "count"]).reindex(weeks, fill_value=0), tr.groupby(wb).R.agg(["sum", "count"]).reindex(weeks, fill_value=0)
    for _ in range(5000):
        ii = rng.integers(0, len(weeks), len(weeks))
        diffs.append(gb["sum"].to_numpy()[ii].sum() / max(gb["count"].to_numpy()[ii].sum(), 1)
                     - ga["sum"].to_numpy()[ii].sum() / max(ga["count"].to_numpy()[ii].sum(), 1))
    lo, hi = np.percentile(diffs, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    print(f"  {cell}: {tr.R.mean() - anyR.R.mean():+.3f}R  CI [{lo:+.2f};{hi:+.2f}]")

print("\nПо роках, сер.R (кількість):")
all_tr["рік"] = all_tr.t0.dt.year
print(all_tr.pivot_table(index="cell", columns="рік", values="R", aggfunc=lambda x: f"{x.mean():+.2f} ({len(x)})").reindex(CELLS).to_string())

print("\nПо монетах, сер.R:")
print(all_tr.pivot_table(index="sym", columns="cell", values="R", aggfunc="mean").reindex(columns=CELLS).round(2).to_string())
all_tr.to_pickle(CACHE / "pullback_trades.pkl")
