"""
Бектест реального пайплайну бота (signal_notifier_v2.indicators/signals + chart_analysis.detect_sweep)
на історії Binance USDM Futures 1h. ЛОКАЛЬНИЙ інструмент, нічого не шле і не чіпає живий бот.

Як емулюємо живий запуск: рішення приймається в момент ЗАКРИТТЯ бару k (= open бару k+1).
Вікно для signals() = 99 закритих 1h-барів + бар k у ролі «поточної» свічки (у живому боті iloc[-1] — та, що
формується, а vs береться з iloc[-2], тобто з останньої повністю закритої — так само тут). Так перетини
(EMA25_BREAK/MACD_CROSS/MOVE) рахуються по реальному руху бару, а не по нульовому. Sweep шукається на щойно
закритому барі k. 4h-тренд: останні 100 4h-барів, останній — «поточний» з ціною close[k] (як fetch_trend).
Вхід — за close[k]; SL/TP — bot.tp_sl(price, atr, type) (ATR_SL_MULT=1.5, ATR_TP_MULT=2.5). Угода живе на
барах k+1... Якщо в одному барі зачеплені і SL, і TP — рахуємо SL (песимістично).
Обмеження: живий бот запускається всередині години (:07/:37), тут — на закритті бару; правила, що чекають
перетину всередині бару, тому наближені перетином на закритому барі.

    python backtest/engine.py            # усі 20 монет, паралельно, -> bt_data/results.pkl
"""
import sys, time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "backtest"))
import data as bdata                       # noqa: E402
import signal_notifier_v2 as bot           # noqa: E402
from chart_analysis import detect_sweep    # noqa: E402

WARM = 130                 # барів прогріву
HORIZONS = (4, 24)         # години утримання: 4h = як старий трекер, 24h = "справжня" угода
COST_RT_PCT = 0.12         # комісія 0.05%/сторона + прослизання 0.01%/сторона, кругом
H4MS = 4 * 3600 * 1000


def sim_one(hi, lo, cl, j, entry, sl, tp, d, H):
    """Повертає (exit_kind, R_gross) або None, якщо історії не вистачає."""
    n = len(hi)
    if j + H > n:
        return None
    risk = abs(entry - sl)
    for i in range(j, j + H):
        if d == "LONG":
            hs, ht = lo[i] <= sl, hi[i] >= tp
        else:
            hs, ht = hi[i] >= sl, lo[i] <= tp
        if hs and ht:
            return ("SLamb", -1.0)
        if hs:
            return ("SL", -1.0)
        if ht:
            return ("TP", abs(tp - entry) / risk)
    ex = cl[j + H - 1]
    r = (ex - entry) / risk if d == "LONG" else (entry - ex) / risk
    return ("TIME", r)


def sim_all(hi, lo, cl, j_idx, entry, sl, tp, d, H):
    """Векторна версія sim_one для baseline: R_gross для кожного j (NaN якщо не вистачає історії)."""
    n = len(hi)
    m = len(j_idx)
    res = np.full(m, np.nan)
    done = np.zeros(m, bool)
    risk = np.abs(entry - sl)
    rr = np.abs(tp - entry) / risk
    valid = j_idx + H <= n
    for h in range(H):
        ii = np.minimum(j_idx + h, n - 1)
        if d == "LONG":
            hs, ht = lo[ii] <= sl, hi[ii] >= tp
        else:
            hs, ht = hi[ii] >= sl, lo[ii] <= tp
        act = ~done & valid
        sl_now = act & hs                     # SL (в т.ч. коли обидва) — песимістично
        tp_now = act & ht & ~hs
        res[sl_now] = -1.0
        res[tp_now] = rr[tp_now]
        done |= sl_now | tp_now
    tm = ~done & valid
    exi = cl[np.minimum(j_idx + H - 1, n - 1)]
    rt = (exi - entry) / risk if d == "LONG" else (entry - exi) / risk
    res[tm] = rt[tm]
    return res


def trend_from(closes: np.ndarray) -> str:
    s = pd.Series(closes)
    e7 = float(s.ewm(span=7, adjust=False).mean().iloc[-1])
    e25 = float(s.ewm(span=25, adjust=False).mean().iloc[-1])
    e99 = float(s.ewm(span=99, adjust=False).mean().iloc[-1])
    if e7 > e25 > e99: return "up"
    if e7 < e25 < e99: return "down"
    return "flat"


def run_symbol(sym: str):
    t0 = time.time()
    df = bdata.load(sym)
    if df.empty:
        return sym, None
    fg = pd.read_pickle(ROOT / "backtest" / "bt_data" / "fg.pkl")
    fg_day = {t.strftime("%Y-%m-%d"): int(v) for t, v in zip(fg.t, fg.v)}

    ts = df["ts"].to_numpy(np.int64)
    o, h, l, c, v = (df[x].to_numpy(float) for x in ("open", "high", "low", "close", "vol"))
    n = len(df)
    # закриті 4h-бари: останній 1h-бар бакета має відкриватись о :03:00 4h-сітки (бакет повний)
    bucket = ts // H4MS
    b_last = np.r_[np.where(np.diff(bucket) != 0)[0], n - 1]
    complete = [i for i in b_last if ts[i] % H4MS == H4MS - 3600_000]
    c4_close = c[complete]; c4_end = ts[complete] + 3600_000             # момент закриття 4h-бару
    times = df["time"].to_numpy()

    cand, raw, sweeps = [], [], []
    atr_arr = np.full(n, np.nan)
    ent_arr = np.full(n, np.nan)

    for k in range(WARM, n - 1):
        T = ts[k] + 3600_000              # момент рішення = закриття бару k
        price = c[k]
        lo_i = k - 99
        win = pd.DataFrame({              # 99 закритих + бар k як «поточна»
            "open": o[lo_i:k + 1], "high": h[lo_i:k + 1], "low": l[lo_i:k + 1],
            "close": c[lo_i:k + 1], "vol": v[lo_i:k + 1], "time": times[lo_i:k + 1],
        })
        ind = bot.indicators(win)
        nc = int(np.searchsorted(c4_end, T, side="right"))
        tr = trend_from(np.r_[c4_close[max(0, nc - 99):nc], price]) if nc >= 30 else None
        day = pd.Timestamp(T, unit="ms", tz="UTC").strftime("%Y-%m-%d")
        fgv = fg_day.get(day, 50)

        atr_arr[k] = ind["atr"]; ent_arr[k] = price
        sigs = [s for s in bot.signals(ind, fgv, None, tr, None, None) if s["strength"] >= bot.MIN_SIGNAL_STRENGTH]
        if sigs:
            best = max(sigs, key=lambda x: x["strength"])
            cand.append((sym, k, T, best["type"], best["rule"], best["strength"]))
            for s in sigs:
                if s["type"] in ("LONG", "SHORT"):
                    raw.append((sym, k, T, s["type"], s["rule"], s["strength"], tr, ind["rsi"], ind["vs"],
                                ind["atr"], price, s is best))
        win0 = pd.DataFrame({
            "open": np.r_[o[lo_i:k + 1], price], "high": np.r_[h[lo_i:k + 1], price],
            "low": np.r_[l[lo_i:k + 1], price], "close": np.r_[c[lo_i:k + 1], price],
            "vol": np.r_[v[lo_i:k + 1], 0.0], "time": np.r_[times[lo_i:k + 1], times[k:k + 1]],
        })
        sw = detect_sweep(win0, max_age=1)
        if sw:
            sweeps.append((sym, k, T, sw["type"], "LIQUIDITY_SWEEP", 0, tr, ind["rsi"], ind["vs"],
                           ind["atr"], price, False))

    cand = pd.DataFrame(cand, columns=["sym", "k", "T", "type", "rule", "strength"])
    cols = ["sym", "k", "T", "type", "rule", "strength", "trend", "rsi", "vs", "atr", "price", "is_best"]
    raw = pd.DataFrame(raw + sweeps, columns=cols)

    # ── результати угод: bot.tp_sl + симуляція на 1h-барах ──
    for H in HORIZONS:
        kind, rr_ = [], []
        for r in raw.itertuples():
            sl, tp = bot.tp_sl(r.price, r.atr, r.type)
            res = sim_one(h, l, c, r.k + 1, r.price, sl, tp, r.type, H)
            kind.append(res[0] if res else None); rr_.append(res[1] if res else np.nan)
        raw[f"kind{H}"] = kind
        raw[f"R{H}"] = rr_
    risk_pct = raw["atr"] * bot.ATR_SL_MULT / raw["price"] * 100
    raw["cost_R"] = COST_RT_PCT / risk_pct
    # «старий» результат бота: рух ціни за 4 год (вхід -> close бару k+4) з порогом ±0.3%
    raw["pct4"] = [((c[r.k + 4] / r.price - 1) * 100 if r.k + 4 < n else np.nan) for r in raw.itertuples()]

    # ── baseline: та сама угода (ATR-SL/TP), але на КОЖНОМУ барі, обидва напрямки ──
    ks = np.arange(WARM, n - 1)
    a = atr_arr[ks]; e = ent_arr[ks]; jj = ks + 1
    base = pd.DataFrame({"sym": sym, "k": ks, "T": ts[ks] + 3600_000})
    for d in ("LONG", "SHORT"):
        sgn = 1 if d == "LONG" else -1
        sl = e - sgn * bot.ATR_SL_MULT * a; tp = e + sgn * bot.ATR_TP_MULT * a
        for H in HORIZONS:
            base[f"{d}_R{H}"] = sim_all(h, l, c, jj, e, sl, tp, d, H)
    base["cost_R"] = COST_RT_PCT / (a * bot.ATR_SL_MULT / e * 100)
    # самоперевірка векторної симуляції проти покрокової
    rng = np.random.default_rng(1)
    for kk in rng.choice(len(ks), 200, replace=False):
        for d in ("LONG", "SHORT"):
            sgn = 1 if d == "LONG" else -1
            e0, a0 = e[kk], a[kk]
            slx, tpx = e0 - sgn * bot.ATR_SL_MULT * a0, e0 + sgn * bot.ATR_TP_MULT * a0
            x = sim_one(h, l, c, jj[kk], e0, slx, tpx, d, 24)
            y = base[f"{d}_R24"].iloc[kk]
            assert (x is None and np.isnan(y)) or abs(x[1] - y) < 1e-9, (sym, kk, d, x, y)
    print(f"{sym:5s} готово за {time.time()-t0:5.0f}с: {len(cand)} кандидатів, {len(raw)} направлених сигналів", flush=True)
    return sym, dict(cand=cand, raw=raw, base=base)


def main():
    syms = list(bdata.SYMBOLS)
    t0 = time.time()
    with Pool(min(8, len(syms))) as p:
        res = dict(p.map(run_symbol, syms, chunksize=1))
    res = {k: v for k, v in res.items() if v}
    out = {k: pd.concat([v[k] for v in res.values()], ignore_index=True) for k in ("cand", "raw", "base")}
    pd.to_pickle(out, ROOT / "backtest" / "bt_data" / "results.pkl")
    print(f"\nВсього {time.time()-t0:.0f}с | кандидатів {len(out['cand'])}, сигналів {len(out['raw'])}, baseline-рядків {len(out['base'])}")


if __name__ == "__main__":
    main()
