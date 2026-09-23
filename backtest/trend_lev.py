"""
Трендові входи з плечем на Binance USDM (BTC, ETH), розмір від ризику на угоду. Дані data_long.py (1h + funding з 2020).

ПРЕ-РЕЄСТРАЦІЯ (2026-09-24, до першого запуску; параметри не підбираються).
  Сигнал n[t] = скільки з 9 Donchian-підстратегій trend_ens.py «в позиції» на денному закритті (00:00 UTC).
  Вхід:   на денному закритті, коли n[t] >= 5 і n[t-1] <= 4 і позиції немає. Ціна = close + прослизання 0.02%.
  Стоп (дві комірки):
    DC20  стоп = середина діапазону закриттів за 20 попередніх днів, лише вгору
    ATR3  стоп = найвище денне закриття з моменту входу − 3 × ATR(14, денний), лише вгору
    Якщо початковий стоп >= ціни входу — вхід пропускаємо.
  Вихід:  (а) годинний low <= стоп -> вихід за стопом − 0.10% прослизання (або за open, якщо гепнуло нижче);
          (б) на денному закритті n[t] <= 2 -> вихід за close.
  Розмір: номінал = ризик × капітал / (відстань до стопу у %), плече не більше 10× капіталу на угоду,
          сумарно по двох монетах не більше 10×. Комісія 0.05% на кожен бік (taker). Funding — фактичний Binance
          (лонг платить при кожному розрахунку, поки позиція відкрита). Капітал спільний для BTC і ETH.
  Ризик на угоду: 2% (основний), 1% і 5% — довідково.
  PASS (для кожної комірки, Бонферроні 0.05/2): середній R угоди > 0 з CI (bootstrap по угодах) > 0 і середній R > 0
        в обох половинах (H1 < 2023-04-01 ≤ H2). R = результат угоди / ризик (1R = втрата на стопі).

    python backtest/trend_lev.py
"""
import contextlib, io, runpy, warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
CACHE = HERE / "bt_data"
with contextlib.redirect_stdout(io.StringIO()):
    TE = runpy.run_path(str(HERE / "trend_ens.py"))
signal = TE["signal"]
SPLIT = pd.Timestamp("2023-04-01", tz="UTC")
FEE, SLIP_IN, SLIP_STOP, MAXLEV = 0.0005, 0.0002, 0.001, 10.0
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)

H, D, F = {}, {}, {}
for s in ("BTC", "ETH"):
    k = pd.read_pickle(CACHE / f"{s}_1h_long.pkl").set_index("time")
    H[s] = k[["open", "high", "low", "close"]]
    d = k.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d["n"] = (signal(d["close"]) * 9).round()
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["mid20"] = (d.close.rolling(20).max() + d.close.rolling(20).min()).shift(1) / 2
    D[s] = d
    f = pd.read_pickle(CACHE / f"funding_long_{s}.pkl")
    F[s] = pd.Series(f["rate"].to_numpy(), index=pd.to_datetime(f["ts"], unit="ms", utc=True).dt.floor("h")).groupby(level=0).sum()

T = H["BTC"].index.intersection(H["ETH"].index)


def simulate(stop_kind, risk):
    eq = 1.0
    pos = {}                     # sym -> dict(entry, stop, notional, risk_amt, t0, hi)
    trades, curve = [], []
    for t in T:
        # 1) годинна перевірка стопів
        for s in list(pos):
            p, bar = pos[s], H[s].loc[t]
            if t in F[s].index:
                eq -= p["notional"] * F[s][t] * (bar.open / p["entry"])
                p["fund"] += p["notional"] * F[s][t]
            if bar.low <= p["stop"]:
                px = min(bar.open, p["stop"]) * (1 - SLIP_STOP)
                pnl = p["notional"] * (px / p["entry"] - 1) - p["notional"] * (px / p["entry"]) * FEE
                eq += pnl
                trades.append(dict(sym=s, t0=p["t0"], t1=t, R=(pnl - p["fee_in"] - p["fund"]) / p["risk_amt"], how="стоп",
                                   ret=pnl, lev=p["notional"] / p["eq0"]))
                del pos[s]
        # 2) денне закриття (бар 23:00 закривається о 00:00 наступного дня)
        if t.hour == 23:
            day = t.floor("D")
            for s in ("BTC", "ETH"):
                if day not in D[s].index:
                    continue
                r = D[s].loc[day]
                prev = D[s]["n"].shift(1).get(day, np.nan)
                if s in pos:
                    p = pos[s]
                    if r.n <= 2:
                        px = r.close
                        pnl = p["notional"] * (px / p["entry"] - 1) - p["notional"] * (px / p["entry"]) * FEE
                        eq += pnl
                        trades.append(dict(sym=s, t0=p["t0"], t1=t, R=(pnl - p["fee_in"] - p["fund"]) / p["risk_amt"], how="сигнал",
                                           ret=pnl, lev=p["notional"] / p["eq0"]))
                        del pos[s]
                        continue
                    p["hi"] = max(p["hi"], r.close)
                    new = r.mid20 if stop_kind == "DC20" else p["hi"] - 3 * r.atr
                    if not np.isnan(new):
                        p["stop"] = max(p["stop"], new)
                elif r.n >= 5 and prev <= 4:
                    entry = r.close * (1 + SLIP_IN)
                    stop = r.mid20 if stop_kind == "DC20" else r.close - 3 * r.atr
                    if np.isnan(stop) or stop >= entry:
                        continue
                    dist = 1 - stop / entry
                    used = sum(q["notional"] for q in pos.values())
                    notional = min(risk * eq / dist, MAXLEV * eq, max(0.0, MAXLEV * eq - used))
                    if notional <= 0:
                        continue
                    fee_in = notional * FEE
                    eq -= fee_in
                    pos[s] = dict(entry=entry, stop=stop, notional=notional, risk_amt=risk * eq, t0=t, hi=r.close,
                                  fee_in=0.0, fund=0.0, eq0=eq)
            # mark-to-market для кривої капіталу
            mtm = eq + sum(p["notional"] * (H[s].loc[t].close / p["entry"] - 1) for s, p in pos.items())
            curve.append((t.floor("D"), mtm))
    c = pd.Series(dict(curve))
    simulate.open = pos
    return pd.DataFrame(trades), c


def metrics(c):
    r = c.pct_change().dropna(); yrs = len(r) / 365
    return dict(CAGR=c.iloc[-1] ** (1 / yrs) - 1, Sharpe=r.mean() / r.std() * np.sqrt(365), MaxDD=(c / c.cummax() - 1).min())


def boot_mean(x, alpha, B=10000, seed=3):
    a = np.asarray(x); ii = np.random.default_rng(seed).integers(0, len(a), (B, len(a)))
    m = a[ii].mean(1)
    return np.percentile(m, 100 * alpha / 2), np.percentile(m, 100 * (1 - alpha / 2))


def streak(R):
    best = cur = 0
    for x in R:
        cur = cur + 1 if x < 0 else 0; best = max(best, cur)
    return best


rows, keep = [], {}
for stop_kind in ("DC20", "ATR3"):
    for risk in (0.02, 0.01, 0.05):
        tr, c = simulate(stop_kind, risk)
        keep[(stop_kind, risk)] = (tr, c)
        m = metrics(c)
        lo, hi = boot_mean(tr.R, 0.05 / 2)
        h1, h2 = tr[tr.t0 < SPLIT].R.mean(), tr[tr.t0 >= SPLIT].R.mean()
        v = ("PASS" if lo > 0 and h1 > 0 and h2 > 0 else "—") if risk == 0.02 else "(довідково)"
        rows.append(dict(стоп=stop_kind, ризик=f"{risk*100:.0f}%", угод=len(tr), на_рік=round(len(tr) / (len(c) / 365), 1),
                         WR=f"{(tr.R > 0).mean()*100:.0f}%", серR=round(tr.R.mean(), 2), CI_R=f"[{lo:+.2f};{hi:+.2f}]",
                         R_H1=round(h1, 2), R_H2=round(h2, 2), найкращаR=round(tr.R.max(), 1), серія_збитків=streak(tr.R),
                         сер_плече=round(tr.lev.mean(), 1), макс_плече=round(tr.lev.max(), 1),
                         CAGR=f"{m['CAGR']*100:+.0f}%", Sharpe=round(m["Sharpe"], 2), MaxDD=f"{m['MaxDD']*100:.0f}%", вердикт=v))
print(f"Період {T[0]:%Y-%m-%d} -> {T[-1]:%Y-%m-%d}, BTC+ETH, спільний капітал")
print(pd.DataFrame(rows).to_string(index=False))

for key in (("DC20", 0.02), ("ATR3", 0.02)):
    tr, c = keep[key]
    print(f"\n{key[0]} ризик 2% — дохідність по роках, %:",
          c.groupby(c.index.year).apply(lambda s: round((s.iloc[-1] / s.iloc[0] - 1) * 100)).to_dict())
    print("останні 8 угод:")
    t = tr.tail(8).copy(); t["t0"] = t.t0.dt.strftime("%Y-%m-%d"); t["t1"] = t.t1.dt.strftime("%Y-%m-%d")
    print(t[["sym", "t0", "t1", "how", "R", "lev"]].round(2).to_string(index=False))

simulate("DC20", 0.02)
for s_, p in simulate.open.items():
    print(f"\nВІДКРИТА угода DC20 2%: {s_} вхід {p['t0']:%Y-%m-%d} за {p['entry']:.0f}, стоп зараз {p['stop']:.0f}, "
          f"плече {p['notional']/p['eq0']:.2f}")
print("\nПоточний стан (останній день даних):")
for s in ("BTC", "ETH"):
    d = D[s].iloc[-1]; d0 = D[s].iloc[-2]
    print(f"{s}: close {d.close:.0f}  n={int(d.n)}/9 (вчора {int(d0.n)})  стоп DC20 {d.mid20:.0f} ({(d.mid20/d.close-1)*100:+.1f}%)"
          f"  ATR3 {d.close-3*d.atr:.0f} ({-3*d.atr/d.close*100:.1f}%)")
