"""
Тест НОВИХ ідей сигналів на історії Binance USDM (2024-01 .. сьогодні, 20 монет, 1h).

ПРЕ-РЕЄСТРАЦІЯ. Ідеї та ВСІ параметри нижче зафіксовані ДО першого запуску; змінювати їх після перегляду
результатів = підгонка, тому результат тоді не рахується. Що не описано тут — не тестується.

Протокол
  * Рішення на закритті 1h-бару k, вхід за close[k], вихід через H годин за close[k+H] (без SL/TP —
    вимірюємо, чи несе сигнал інформацію; конструкцію виходу підбираємо окремо, тільки для тих, що пройшли).
  * Чиста угода = рух у бік сигналу − комісії/прослизання 0.12% кругом.
  * Edge = чиста угода мінус середня чиста угода ВИПАДКОВОГО входу того ж напрямку в тому ж місяці
    (знімає дрейф ринку). Головна метрика: H=24h. Для H=4h — лише довідково.
  * Повтори: кулдаун 24 год на (ідея, монета). Довірчі інтервали — bootstrap по календарних днях
    (монети рухаються разом, сигнали не незалежні).
  * РОЗВІДКА = сигнали до 2025-07-01, ХОЛДАУТ = з 2025-07-01. Холдаут дивимось один раз, для всіх комірок разом.
  * Множинні перевірки: N комірок => розвідка оцінюється за CI зі скоригованим рівнем 0.05/N (Бонферроні).
  * PASS = (розвідка: скоригований CI edge > 0) І (холдаут: 95% CI edge > 0 І чиста угода > 0).
  * Контроль: 24 ПЛАЦЕБО-комірки (випадкові сигнали тієї ж частоти). Якщо плацебо "проходить" — метод зламаний.
  * Комірка RSI_OB_LONG — це гіпотеза, побачена раніше на цих же даних (забруднена): PASS їй не присвоюється.

Ідеї (позначення комірок)
  D24/D72 (+ _L/_S)     Пробій Donchian: close > max(high) попередніх L=24/72 год -> LONG; < min(low) -> SHORT
  RS_TOP3_L, RS_BOT3_S  Відносна сила: ранг 72h-дохідності серед монет (>=12 доступних): топ-3 -> LONG, низ-3 -> SHORT
  SQZ_L / SQZ_S         Стиснення BB(20,2): ширина у нижніх 10% за 720 год у одному з попередніх 12 барів,
                        потім close вище верхньої / нижче нижньої смуги
  SHK_DN_REV_L / SHK_DN_CONT_S   1h-рух z<=-3 (std 100 барів) і обсяг >=2x: розворот LONG / продовження SHORT
  SHK_UP_REV_S / SHK_UP_CONT_L   те саме для z>=+3
  FUND_HI_S / FUND_LO_L Funding на розрахунку (00/08/16 UTC) у верхніх 5% / нижніх 5% за 90 днів (>0 / <0): контртренд
  OI_UP_UP_L / OI_UP_UP_S   ціна 24h >=+5% і OI(в монетах) 24h >=+5%: LONG (продовження) / SHORT (перегрів)
  OI_DN_UP_S / OI_DN_UP_L   ціна 24h <=-5% і OI 24h >=+5%: SHORT (продовження) / LONG (сквіз)
  GLS_HI_S / GLS_LO_L   Глобальний long/short (акаунти) у верхніх/нижніх 5% за 720 год: контртренд
  TKR_HI_L / TKR_LO_S   Taker buy/sell (сер. 4 год) у верхніх/нижніх 5% за 720 год: продовження
  BTC_CU_L / BTC_CU_S   BTC 4h >=+2% і альт 4h <=+0.5% -> LONG альта; BTC 4h <=-2% і альт 4h >=-0.5% -> SHORT альта
  RSI_OB_LONG           (контроль, забруднена) RSI(14, Wilder) > 72 -> LONG

    python backtest/ideas.py
"""
import sys, warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
import data as bdata                                                  # noqa: E402

CACHE = bdata.CACHE
COST = 0.0012                    # 0.12% кругом
COOLDOWN = 24                    # барів
SPLIT = pd.Timestamp("2025-07-01", tz="UTC")
B = 10000                        # bootstrap
HS = (4, 24)
HR = 3600_000


# ═══════════════ панель ═══════════════
def load_panel():
    dfs = {}
    for s in bdata.SYMBOLS:
        d = bdata.load(s)
        if not d.empty:
            dfs[s] = d.set_index("ts")
    idx = pd.Index(sorted(set().union(*[d.index for d in dfs.values()])), name="ts")
    P = {c: pd.DataFrame({s: d[c].reindex(idx) for s, d in dfs.items()}) for c in ("open", "high", "low", "close", "vol")}
    return idx, P


idx, P = load_panel()
O, Hh, L, C, V = P["open"], P["high"], P["low"], P["close"], P["vol"]
SYMS = list(C.columns)
dt = pd.to_datetime(idx, unit="ms", utc=True)
T_close = idx.to_numpy() + HR                                        # момент рішення
n = len(idx)
print(f"Панель: {n} барів × {len(SYMS)} монет, {dt[0]:%Y-%m-%d} -> {dt[-1]:%Y-%m-%d}")


def bc(series):
    """Серія по часу -> DataFrame T×S (однакове значення для всіх монет), щоб pandas не сварився на форму."""
    return pd.DataFrame(np.repeat(np.asarray(series)[:, None], len(SYMS), 1), index=C.index, columns=SYMS)


def exp_series(ts_ms, values, decision_ms, lag_ms=0):
    """Значення «як відомо на момент рішення»: останнє з ts <= decision - lag (merge_asof, без заглядання вперед)."""
    left = pd.DataFrame({"t": decision_ms - lag_ms})
    right = pd.DataFrame({"t": ts_ms, "v": values}).sort_values("t")
    return pd.merge_asof(left, right, on="t")["v"].to_numpy()


# ═══════════════ ознаки ═══════════════
ret1 = C.pct_change()
z = ret1 / ret1.rolling(100).std().shift(1)
volr = V / V.rolling(20).mean().shift(1)
ret4 = C / C.shift(4) - 1
ret24 = C / C.shift(24) - 1
ret72 = C / C.shift(72) - 1
d_ = C.diff()
g_ = d_.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
l_ = (-d_.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
rsi = 100 - 100 / (1 + g_ / l_.replace(0, np.nan))
m20, s20 = C.rolling(20).mean(), C.rolling(20).std()
bbw = 4 * s20 / m20
squeeze_before = (bbw <= bbw.rolling(720).quantile(0.10)).shift(1).rolling(12).max().fillna(0).astype(bool)

E = {}   # ім'я комірки -> (булева панель, напрямок)
for Lb in (24, 72):
    E[f"D{Lb}_L"] = (C > Hh.rolling(Lb).max().shift(1), "LONG")
    E[f"D{Lb}_S"] = (C < L.rolling(Lb).min().shift(1), "SHORT")
cnt = ret72.notna().sum(axis=1)
rk = ret72.rank(axis=1, ascending=False)
enough = bc((cnt >= 12).to_numpy())
E["RS_TOP3_L"] = (rk.le(3) & enough, "LONG")
E["RS_BOT3_S"] = (rk.gt(bc((cnt - 3).to_numpy())) & enough, "SHORT")
E["SQZ_L"] = (squeeze_before & (C > m20 + 2 * s20), "LONG")
E["SQZ_S"] = (squeeze_before & (C < m20 - 2 * s20), "SHORT")
dn, up = (z <= -3) & (volr >= 2), (z >= 3) & (volr >= 2)
E["SHK_DN_REV_L"], E["SHK_DN_CONT_S"] = (dn, "LONG"), (dn, "SHORT")
E["SHK_UP_REV_S"], E["SHK_UP_CONT_L"] = (up, "SHORT"), (up, "LONG")
btc4 = bc(ret4["BTC"].to_numpy())
notbtc = pd.DataFrame({c: c != "BTC" for c in SYMS}, index=C.index)
cu_l = (btc4 >= 0.02) & (ret4 <= 0.005) & notbtc
cu_s = (btc4 <= -0.02) & (ret4 >= -0.005) & notbtc
E["BTC_CU_L"], E["BTC_CU_S"] = (cu_l, "LONG"), (cu_s, "SHORT")
E["RSI_OB_LONG"] = (rsi > 72, "LONG")

# funding: подія на барі, що закривається рівно в момент розрахунку
fh, fl = pd.DataFrame(False, index=C.index, columns=SYMS), pd.DataFrame(False, index=C.index, columns=SYMS)
pos_by_close = pd.Series(np.arange(n), index=T_close)
for s in SYMS:
    p = CACHE / f"funding_{s}.pkl"
    if not p.exists():
        continue
    f = pd.read_pickle(p)
    r = pd.Series(f["rate"].to_numpy(), index=(f["ts"] // HR * HR).to_numpy())
    hi = (r >= r.rolling(270, min_periods=180).quantile(0.95)) & (r > 0)
    lo = (r <= r.rolling(270, min_periods=180).quantile(0.05)) & (r < 0)
    for flag, tgt in ((hi, fh), (lo, fl)):
        ks = pos_by_close.reindex(flag[flag].index).dropna().astype(int).to_numpy()
        tgt.iloc[ks, tgt.columns.get_loc(s)] = True
E["FUND_HI_S"], E["FUND_LO_L"] = (fh, "SHORT"), (fl, "LONG")

# метрики позиціонування (5-хв -> як відомо на момент рішення, з лагом 10 хв)
oi = C * np.nan; gls = oi.copy(); tkr = oi.copy()
for s in SYMS:
    p = CACHE / f"metrics_{s}.pkl"
    if not p.exists():
        continue
    m = pd.read_pickle(p)
    oi[s] = exp_series(m.ts.to_numpy(), m.oi_usd.to_numpy(), T_close, 600_000)
    gls[s] = exp_series(m.ts.to_numpy(), m.glob_ls.to_numpy(), T_close, 600_000)
    tkr[s] = exp_series(m.ts.to_numpy(), m.taker_ls.to_numpy(), T_close, 600_000)
oi_coins = oi / C
oi24 = oi_coins / oi_coins.shift(24) - 1
E["OI_UP_UP_L"] = ((ret24 >= 0.05) & (oi24 >= 0.05), "LONG")
E["OI_UP_UP_S"] = ((ret24 >= 0.05) & (oi24 >= 0.05), "SHORT")
E["OI_DN_UP_S"] = ((ret24 <= -0.05) & (oi24 >= 0.05), "SHORT")
E["OI_DN_UP_L"] = ((ret24 <= -0.05) & (oi24 >= 0.05), "LONG")
E["GLS_HI_S"] = (gls >= gls.rolling(720, min_periods=500).quantile(0.95), "SHORT")
E["GLS_LO_L"] = (gls <= gls.rolling(720, min_periods=500).quantile(0.05), "LONG")
tk4 = tkr.rolling(4).mean()
E["TKR_HI_L"] = (tk4 >= tk4.rolling(720, min_periods=500).quantile(0.95), "LONG")
E["TKR_LO_S"] = (tk4 <= tk4.rolling(720, min_periods=500).quantile(0.05), "SHORT")

# плацебо: випадкові сигнали, ~1% барів (24 комірки з різним seed)
rng_seeds = range(100, 112)
for sd in rng_seeds:
    rr = np.random.default_rng(sd).random(C.shape) < 0.01
    E[f"PLACEBO{sd}_L"] = (pd.DataFrame(rr, index=C.index, columns=SYMS) & C.notna(), "LONG")
    rr = np.random.default_rng(sd + 1000).random(C.shape) < 0.01
    E[f"PLACEBO{sd}_S"] = (pd.DataFrame(rr, index=C.index, columns=SYMS) & C.notna(), "SHORT")

# ═══════════════ результати та baseline ═══════════════
FWD = {h: (C.shift(-h) / C - 1) for h in HS}
month = dt.strftime("%Y-%m").to_numpy()
day = dt.strftime("%Y-%m-%d").to_numpy()
BASE = {}
for d in ("LONG", "SHORT"):
    sgn = 1 if d == "LONG" else -1
    for h in HS:
        net = sgn * FWD[h] - COST
        BASE[(d, h)] = net.mean(axis=1).groupby(month).mean()      # середнє по місяцю (усі монети, усі бари)


def events(name):
    panel, d = E[name]
    sgn = 1 if d == "LONG" else -1
    A = panel.fillna(False).to_numpy()
    rows = []
    for j, s in enumerate(SYMS):
        last = -10**9
        for k in np.flatnonzero(A[:, j]):
            if k - last < COOLDOWN or k + 24 >= n:
                continue
            last = k
            rows.append((name, s, k, d, sgn * FWD[4].iat[k, j] - COST, sgn * FWD[24].iat[k, j] - COST))
    df = pd.DataFrame(rows, columns=["cell", "sym", "k", "dir", "net4", "net24"])
    if df.empty:
        return df
    df["month"], df["day"] = month[df.k], day[df.k]
    df["dt"] = dt[df.k]
    for h in HS:
        df[f"edge{h}"] = df[f"net{h}"] - df.month.map(BASE[(d, h)])
    return df.dropna(subset=["net24"])


def boot(df, col, alpha):
    if len(df) < 30:
        return np.nan, np.nan
    g = df.groupby("day")[col].agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    ii = np.random.default_rng(11).integers(0, len(g), (B, len(g)))
    m = s[ii].sum(1) / c[ii].sum(1)
    return np.percentile(m, 100 * alpha / 2), np.percentile(m, 100 * (1 - alpha / 2))


real = [k for k in E if not k.startswith("PLACEBO") and k != "RSI_OB_LONG"]
N = len(real)
ALPHA_ADJ = 0.05 / N
print(f"Комірок реальних ідей: {N} -> скоригований рівень {ALPHA_ADJ:.4f} (CI {100*(1-ALPHA_ADJ):.2f}%)\n")

rows = []
for name in list(E):
    ev = events(name)
    if ev.empty:
        rows.append(dict(cell=name, n=0)); continue
    disc, hold = ev[ev.dt < SPLIT], ev[ev.dt >= SPLIT]
    dlo_adj, dhi_adj = boot(disc, "edge24", ALPHA_ADJ)
    dlo, dhi = boot(disc, "edge24", 0.05)
    hlo, hhi = boot(hold, "edge24", 0.05)
    tlo, thi = boot(ev, "edge24", 0.05)
    disc_sig = dlo_adj > 0
    hold_ok = (hlo > 0) and (hold.net24.mean() > 0) if len(hold) >= 30 else False
    kind = "плацебо" if name.startswith("PLACEBO") else ("контроль (забруднена)" if name == "RSI_OB_LONG" else "ідея")
    if kind == "ідея" and disc_sig and hold_ok:
        verdict = "PASS"
    elif kind == "ідея" and dlo > 0 and len(hold) >= 30 and hold.edge24.mean() > 0:
        verdict = "maybe"
    elif dhi_adj < 0 or thi < 0:
        verdict = "знач.НЕГАТИВ"
    else:
        verdict = "—"
    rows.append(dict(cell=name, n=len(ev), днів=ev.day.nunique(), net24=ev.net24.mean() * 100, edge24=ev.edge24.mean() * 100,
                     edge4=ev.edge4.mean() * 100, розв_n=len(disc), розв_edge=disc.edge24.mean() * 100,
                     розв_CIadj=f"[{dlo_adj*100:+.2f};{dhi_adj*100:+.2f}]", холд_n=len(hold), холд_edge=hold.edge24.mean() * 100,
                     холд_CI95=f"[{hlo*100:+.2f};{hhi*100:+.2f}]", холд_net=hold.net24.mean() * 100, вердикт=verdict, тип=kind,
                     _dsig95=(dlo > 0) or (dhi < 0), _tsig95=(tlo > 0) or (thi < 0)))
R = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 200)
show = ["cell", "n", "днів", "net24", "edge24", "edge4", "розв_n", "розв_edge", "розв_CIadj", "холд_n", "холд_edge", "холд_CI95", "холд_net", "вердикт"]
print("=== РЕАЛЬНІ ІДЕЇ + КОНТРОЛЬ (усі значення у % за 24 год після комісій; edge = проти випадкового входу) ===")
print(R[R.тип != "плацебо"][show].round(3).to_string(index=False))
pl = R[R.тип == "плацебо"]
print(f"\n=== ПЛАЦЕБО ({len(pl)} комірок) ===")
print(f"edge24: середнє {pl.edge24.mean():+.4f}%  розкид [{pl.edge24.min():+.3f}; {pl.edge24.max():+.3f}]")
print(f"'значущих' при 95% (на всьому періоді): {int(pl._tsig95.sum())} з {len(pl)} (очікуємо ~{0.05*len(pl):.1f})")
print(f"PASS-плацебо: {int((pl.вердикт=='PASS').sum())} (має бути 0)   'maybe'-плацебо: {int((pl.вердикт=='maybe').sum())}")
R.to_pickle(CACHE / "ideas_results.pkl")
