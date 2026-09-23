"""
Три перевірки з огляду літератури (2026-09-24): повільний тренд, фільтр режиму BTC, сезонність 22–00 UTC.

ПРЕ-РЕЄСТРАЦІЯ. Правила, параметри і критерії нижче зафіксовані ДО першого запуску. Зміна після перегляду
результатів = підгонка, тоді результат не рахується. Дані: Binance USDM BTC/ETH 1h 2020-01 .. (data_long.py),
алерти бота — bt_data/results.pkl (engine.py, 2024-01 ..).
Половини: H1 = 2020-01-01 .. 2023-03-31, H2 = 2023-04-01 .. кінець.

ТЕСТ 1 — TSMOM на денних свічках (BTC, ETH окремо; 4 комірки × 2 монети = 8, Бонферроні 0.05/8)
  Рішення на денному закритті (00:00 UTC), позиція на наступну добу. Витрати: 0.06% за кожну одиницю зміни
  позиції (taker 0.05% + прослизання) + фактичний funding Binance (лонг платить, шорт отримує).
  TS28_LF  лонг, якщо дохідність 28 днів > 0, інакше поза ринком
  TS28_LS  +1 / −1 за знаком дохідності 28 днів
  TSQ28_5  лонг на 5 днів, якщо дохідність 28 днів >= 67-го перцентиля всіх попередніх (мін. 365 днів історії)
  ENS_VT   середнє знаків дохідностей 7/14/28/56/112 днів, лише > 0 (лонг/поза ринком), розмір =
           частка × min(40% / річна волатильність за 30 днів, 1.5)
  Бенчмарк: купити й тримати той самий перп (з funding).
  PASS  = Sharpe > Sharpe(B&H) в ОБОХ половинах І MaxDD менша за B&H І нижня межа CI Sharpe (Бонферроні,
          блоковий bootstrap 30 днів) > 0.
  maybe = перші дві умови виконані, CI не дотягує.

ТЕСТ 2 — фільтр режиму для алертів бота (BTC close vs SMA200 денних, за останнім ЗАКРИТИМ днем до алерту)
  Правило: LONG лише коли BTC > SMA200, SHORT лише коли BTC < SMA200.
  Алерти = емуляція живого бота як у report.py (best на монету → кулдаун → топ-5), метрика net R за 24h
  (SL/TP бота, після витрат), edge проти випадкової угоди того ж напрямку в тому ж місяці.
  PASS = заблоковані алерти мають 95% CI edge < 0 І залишені мають середній net R > заблокованих.

ТЕСТ 3 — сезонність BTC: лонг на закритті бару 21:00 UTC (тобто о 22:00), вихід на закритті бару 23:00 (00:00)
  Витрати: taker 0.12% кругом і maker 0.04% кругом (два сценарії). Funding не рахуємо (вихід до розрахунку).
  PASS = при maker-витратах середня чиста угода > 0 в H1 І в H2 з 95% CI > 0 у H2 (bootstrap по днях).
  Довідково (не для вердикту): середня 2-годинна дохідність за годиною входу і по роках.

    python backtest/data_long.py && python backtest/regime.py
"""
import sys, warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "backtest" / "bt_data"
sys.path.insert(0, str(ROOT))
SPLIT = pd.Timestamp("2023-04-01", tz="UTC")
B = 5000
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)


def load(sym):
    k = pd.read_pickle(CACHE / f"{sym}_1h_long.pkl").set_index("time")
    f = pd.read_pickle(CACHE / f"funding_long_{sym}.pkl")
    f["time"] = pd.to_datetime(f["ts"], unit="ms", utc=True).dt.floor("h")
    return k, f.groupby("time")["rate"].sum()


# ═══════════════ ТЕСТ 1 ═══════════════
def daily(k, fund):
    c = k["close"].resample("1D").last()                  # мітка дня d = закриття о 00:00 d+1
    fd = fund.resample("1D", offset="1h").sum()           # розрахунки в (00:00 d; 00:00 d+1] -> день d
    fd.index = fd.index.floor("D")
    return pd.DataFrame({"c": c, "fund": fd.reindex(c.index).fillna(0)}).dropna(subset=["c"])


def positions(d):
    r = d["c"].pct_change()
    r28 = d["c"] / d["c"].shift(28) - 1
    P = {}
    P["TS28_LF"] = (r28 > 0).astype(float).where(r28.notna())
    P["TS28_LS"] = np.sign(r28)
    thr = r28.expanding(min_periods=365).quantile(0.667).shift(1)
    sig = (r28 >= thr).astype(float).where(thr.notna())
    P["TSQ28_5"] = sig.rolling(5, min_periods=1).max().where(thr.notna())
    ens = sum(np.sign(d["c"] / d["c"].shift(L) - 1) for L in (7, 14, 28, 56, 112)) / 5
    rv = r.rolling(30).std() * np.sqrt(365)
    P["ENS_VT"] = ens.clip(lower=0) * np.minimum(0.40 / rv, 1.5)
    P["B&H"] = pd.Series(1.0, index=d.index).where(r28.notna())
    return P


def pnl(d, p):
    """Позиція p[d] (рішення на закритті дня d) заробляє дохідність і платить funding дня d+1."""
    r_next = d["c"].pct_change().shift(-1)
    f_next = d["fund"].shift(-1)
    p = p.fillna(0)
    cost = 0.0006 * p.diff().abs().fillna(p.abs())
    out = p * r_next - p * f_next - cost
    return out.shift(1).dropna()                          # мітка = день, коли результат реалізовано


def stats(x):
    if len(x) < 30:
        return dict(CAGR=np.nan, vol=np.nan, Sharpe=np.nan, MaxDD=np.nan)
    eq = (1 + x).cumprod()
    yrs = len(x) / 365
    return dict(CAGR=eq.iloc[-1] ** (1 / yrs) - 1, vol=x.std() * np.sqrt(365),
                Sharpe=x.mean() / x.std() * np.sqrt(365) if x.std() else np.nan, MaxDD=(eq / eq.cummax() - 1).min())


def boot_sharpe(x, alpha, block=30, seed=5):
    a = x.to_numpy(); n = len(a); nb = int(np.ceil(n / block))
    rng = np.random.default_rng(seed)
    st = rng.integers(0, n, (B, nb))
    idx = (st[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n] % n
    s = a[idx]
    sh = s.mean(1) / s.std(1) * np.sqrt(365)
    return np.percentile(sh, 100 * alpha / 2), np.percentile(sh, 100 * (1 - alpha / 2))


print("═" * 100 + "\nТЕСТ 1 — TSMOM на денних свічках (після витрат і funding)\n" + "═" * 100)
CELLS = ["TS28_LF", "TS28_LS", "TSQ28_5", "ENS_VT"]
A1 = 0.05 / (len(CELLS) * 2)
rows, curves = [], {}
for sym in ("BTC", "ETH"):
    k, fund = load(sym)
    d = daily(k, fund)
    P = positions(d)
    start = P["TSQ28_5"].first_valid_index()                 # спільний старт для всіх (після 365 днів історії)
    R = {name: pnl(d, P[name])[lambda s: s.index > start] for name in P}
    bh = R["B&H"]
    for name in CELLS + ["B&H"]:
        x = R[name]
        full, h1, h2 = stats(x), stats(x[x.index < SPLIT]), stats(x[x.index >= SPLIT])
        bh1, bh2, bhf = stats(bh[bh.index < SPLIT]), stats(bh[bh.index >= SPLIT]), stats(bh)
        lo, hi = boot_sharpe(x, A1)
        pos = P[name][P[name].index > start].fillna(0)
        beats = h1["Sharpe"] > bh1["Sharpe"] and h2["Sharpe"] > bh2["Sharpe"] and full["MaxDD"] > bhf["MaxDD"]
        v = "бенчмарк" if name == "B&H" else ("PASS" if beats and lo > 0 else ("maybe" if beats else "—"))
        rows.append(dict(монета=sym, комірка=name, CAGR=f"{full['CAGR']*100:+.0f}%", vol=f"{full['vol']*100:.0f}%",
                         Sharpe=round(full["Sharpe"], 2), CI_adj=f"[{lo:+.2f};{hi:+.2f}]",
                         Sh_H1=round(h1["Sharpe"], 2), Sh_H2=round(h2["Sharpe"], 2), MaxDD=f"{full['MaxDD']*100:.0f}%",
                         в_ринку=f"{(pos.abs() > 0).mean()*100:.0f}%", змін_на_рік=round((pos.diff().abs() > 0).sum() / (len(pos) / 365)),
                         вердикт=v))
        curves[(sym, name)] = x
print(f"період: {start:%Y-%m-%d} -> {d.index[-1]:%Y-%m-%d}, CI Sharpe {100*(1-A1):.1f}% (Бонферроні 0.05/{len(CELLS)*2})")
print(pd.DataFrame(rows).to_string(index=False))

print("\nПо роках (дохідність за рік, %):")
yr = {}
for (sym, name), x in curves.items():
    yr[f"{sym} {name}"] = x.groupby(x.index.year).apply(lambda s: ((1 + s).prod() - 1) * 100).round(0)
print(pd.DataFrame(yr).T.to_string())


# ═══════════════ ТЕСТ 2 ═══════════════
print("\n" + "═" * 100 + "\nТЕСТ 2 — фільтр режиму BTC SMA200 для алертів бота (24h, R після витрат)\n" + "═" * 100)
import signal_notifier_v2 as bot  # noqa: E402

k, _ = load("BTC")
dc = k["close"].resample("1D").last()
bull = (dc > dc.rolling(200).mean()).where(dc.rolling(200).mean().notna())
bull_known = bull.copy(); bull_known.index = bull_known.index + pd.Timedelta(days=1)   # відомо з 00:00 наступного дня

RES = pd.read_pickle(CACHE / "results.pkl")
raw, cand, base = RES["raw"].copy(), RES["cand"].copy(), RES["base"].copy()
for x in (raw, cand, base):
    x["dt"] = pd.to_datetime(x["T"], unit="ms", utc=True)
    x["day"] = x["dt"].dt.strftime("%Y-%m-%d"); x["month"] = x["dt"].dt.strftime("%Y-%m")
for dr in ("LONG", "SHORT"):
    base[f"{dr}_net"] = base[f"{dr}_R24"] - base["cost_R"]
bm = {dr: base.groupby("month")[f"{dr}_net"].mean() for dr in ("LONG", "SHORT")}
raw = raw[raw["R24"].notna()].copy()
raw["net"] = raw["R24"] - raw["cost_R"]
raw["edge"] = raw["net"] - [bm[t].get(m, np.nan) for t, m in zip(raw["type"], raw["month"])]

SYM_ORDER = {s.split("/")[0]: i for i, s in enumerate(bot.SYMBOLS)}
cand = cand.sort_values(["T", "sym"], key=lambda s: s.map(SYM_ORDER) if s.name == "sym" else s)
sent, last = [], {}
for T, g in cand.groupby("T", sort=True):
    ok = [r for r in g.itertuples() if not (f"{r.sym}_{r.type}" in last and T - last[f"{r.sym}_{r.type}"] < bot.COOLDOWN_MIN * 60_000)]
    ok.sort(key=lambda r: r.strength, reverse=True)
    for r in ok[:bot.MAX_SIGNALS_PER_RUN]:
        sent.append((r.sym, r.k, r.type, r.rule)); last[f"{r.sym}_{r.type}"] = T
al = raw[raw["is_best"]].merge(pd.DataFrame(sent, columns=["sym", "k", "type", "rule"]), on=["sym", "k", "type", "rule"])
al["bull"] = pd.merge_asof(al[["dt"]].sort_values("dt").reset_index(),
                           bull_known.rename("b").dropna().reset_index().rename(columns={"time": "dt"}),
                           on="dt").set_index("index")["b"].reindex(al.index)
al["regime"] = np.where(al["bull"] == 1, "BTC>SMA200", "BTC<SMA200")
al["keep"] = ((al["type"] == "LONG") & (al["bull"] == 1)) | ((al["type"] == "SHORT") & (al["bull"] == 0))


def boot_mean(df, col, alpha=0.05, seed=7):
    g = df.groupby("day")[col].agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    ii = np.random.default_rng(seed).integers(0, len(g), (B, len(g)))
    m = s[ii].sum(1) / c[ii].sum(1)
    return np.percentile(m, 100 * alpha / 2), np.percentile(m, 100 * (1 - alpha / 2))


def summ(df, label):
    if len(df) < 30:
        return dict(група=label, n=len(df))
    lo, hi = boot_mean(df, "edge")
    return dict(група=label, n=len(df), днів=df.day.nunique(), R_net=round(df.net.mean(), 3), edge=round(df.edge.mean(), 3),
                CI95=f"[{lo:+.3f};{hi:+.3f}]", TP=f"{(df.kind24=='TP').mean()*100:.0f}%")


print(f"днів у режимі BTC>SMA200 серед алертів: {(al.bull==1).mean()*100:.0f}%  "
      f"(період {al.dt.min():%Y-%m-%d} -> {al.dt.max():%Y-%m-%d})")
t = [summ(al, "усі алерти")]
for (ty, rg), g in al.groupby(["type", "regime"]):
    t.append(summ(g, f"{ty} при {rg}"))
t += [summ(al[al.keep], "ЗАЛИШЕНІ фільтром"), summ(al[~al.keep], "ЗАБЛОКОВАНІ фільтром")]
print(pd.DataFrame(t).to_string(index=False))
blk, kp = al[~al.keep], al[al.keep]
blo, bhi = boot_mean(blk, "edge")
v2 = "PASS" if bhi < 0 and kp.net.mean() > blk.net.mean() else "—"
print(f"ВЕРДИКТ ТЕСТ 2: {v2}  (заблоковані edge CI [{blo:+.3f};{bhi:+.3f}], net R залишені {kp.net.mean():+.3f} vs заблоковані {blk.net.mean():+.3f})")


# ═══════════════ ТЕСТ 3 ═══════════════
print("\n" + "═" * 100 + "\nТЕСТ 3 — сезонність BTC: лонг 22:00 -> 00:00 UTC (01:00 -> 03:00 Київ літом)\n" + "═" * 100)
c = k["close"]
r2 = c.shift(-2) / c - 1                                   # вхід на закритті бару h, вихід через 2 бари
hour_in = (r2.index.hour + 1) % 24                         # година входу = закриття бару
trade = r2[hour_in == 22].dropna()
tr = pd.DataFrame({"g": trade})
tr["day"] = tr.index.strftime("%Y-%m-%d")
out = []
for costname, cost in (("taker 0.12%", 0.0012), ("maker 0.04%", 0.0004)):
    for half, sub in (("H1", tr[tr.index < SPLIT]), ("H2", tr[tr.index >= SPLIT]), ("усе", tr)):
        sub = sub.assign(net=sub.g - cost)
        lo, hi = boot_mean(sub, "net")
        out.append(dict(витрати=costname, період=half, n=len(sub), брутто=f"{sub.g.mean()*100:+.3f}%",
                        нетто=f"{sub.net.mean()*100:+.3f}%", CI95=f"[{lo*100:+.3f};{hi*100:+.3f}]",
                        WR=f"{(sub.net > 0).mean()*100:.0f}%", сума_за_рік=f"{sub.net.mean()*365*100:+.0f}%"))
O3 = pd.DataFrame(out)
print(O3.to_string(index=False))
mk = tr.assign(net=tr.g - 0.0004)
h1m, h2 = mk[mk.index < SPLIT].net.mean(), mk[mk.index >= SPLIT]
lo2, _ = boot_mean(h2, "net")
print(f"ВЕРДИКТ ТЕСТ 3: {'PASS' if h1m > 0 and h2.net.mean() > 0 and lo2 > 0 else '—'}")
print("\nДовідково — брутто 2h за роком (%):", (tr.g.groupby(tr.index.year).mean() * 100).round(3).to_dict())
allh = pd.DataFrame({"g": r2, "h": hour_in}).dropna()
print("Довідково — середня брутто 2h-дохідність за годиною входу UTC (усі роки, %):")
print((allh.groupby("h").g.mean() * 100).round(3).to_string())
