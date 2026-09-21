"""
Звіт по результатах backtest/engine.py (bt_data/results.pkl). Локальний, нічого не змінює.

Метрика — очікуваний результат угоди в R (1R = відстань до SL) ПІСЛЯ комісій/прослизання.
"Edge" = R сигналу мінус середній R випадкової угоди ТОГО Ж напрямку в ТОМУ Ж місяці (знімає дрейф ринку:
у бичачому місяці лонги виглядають розумними самі по собі). 95% CI — bootstrap по календарних днях
(монети рухаються разом, тому окремі сигнали НЕ незалежні).

    python backtest/report.py [H]        # H = 24 (за замовч.) або 4 — горизонт утримання, годин
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import signal_notifier_v2 as bot  # noqa: E402

H = int(sys.argv[1]) if len(sys.argv) > 1 else 24
RES = pd.read_pickle(ROOT / "backtest" / "bt_data" / "results.pkl")
raw, cand, base = RES["raw"].copy(), RES["cand"].copy(), RES["base"].copy()
SYM_ORDER = {s.split("/")[0]: i for i, s in enumerate(bot.SYMBOLS)}
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)

for d in (raw, cand, base):
    d["dt"] = pd.to_datetime(d["T"], unit="ms", utc=True)
    d["day"] = d["dt"].dt.strftime("%Y-%m-%d")
    d["month"] = d["dt"].dt.strftime("%Y-%m")

# ── baseline: середній net R випадкової угоди по (напрямок, місяць) ──
for dr in ("LONG", "SHORT"):
    base[f"{dr}_net"] = base[f"{dr}_R{H}"] - base["cost_R"]
bm = {dr: base.groupby("month")[f"{dr}_net"].mean() for dr in ("LONG", "SHORT")}
raw = raw[raw[f"R{H}"].notna()].copy()
raw["net"] = raw[f"R{H}"] - raw["cost_R"]
raw["bl"] = [bm[t].get(m, np.nan) for t, m in zip(raw["type"], raw["month"])]
raw["edge"] = raw["net"] - raw["bl"]
raw["aligned"] = np.where(raw["trend"].isna(), "n/a",
                 np.where(((raw["type"] == "LONG") & (raw["trend"] == "up")) | ((raw["type"] == "SHORT") & (raw["trend"] == "down")),
                          "за трендом", np.where(raw["trend"] == "flat", "flat", "проти тренду")))
raw["kind"] = raw[f"kind{H}"]


def dedupe(df, key_cols, hours):
    """Залишає перший сигнал у кожному ключі, а наступні — не раніше ніж через `hours` годин (як кулдаун бота)."""
    df = df.sort_values("T")
    keep, last = [], {}
    for idx, key, t in zip(df.index, zip(*[df[c] for c in key_cols]), df["T"]):
        if key not in last or t - last[key] >= hours * 3600_000:
            keep.append(idx); last[key] = t
    return df.loc[keep]


def boot_ci(df, col, B=1500, seed=7):
    g = df.groupby("day")[col].agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), (B, len(g)))
    m = s[idx].sum(1) / c[idx].sum(1)
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


def summ(df, label):
    if len(df) == 0:
        return dict(група=label, n=0)
    lo, hi = boot_ci(df, "edge") if len(df) >= 30 else (np.nan, np.nan)
    tp = (df["kind"] == "TP").mean() * 100
    sl = df["kind"].isin(["SL", "SLamb"]).mean() * 100
    return {"група": label, "n": len(df), "днів": df["day"].nunique(), "TP%": round(tp), "SL%": round(sl),
            "час%": round(100 - tp - sl), "R_сиг": round(df["net"].mean(), 3), "R_випадк": round(df["bl"].mean(), 3),
            "edge": round(df["edge"].mean(), 3), "CI95": f"[{lo:+.2f};{hi:+.2f}]" if not np.isnan(lo) else "мало даних",
            "значущо": "ТАК" if (not np.isnan(lo) and (lo > 0 or hi < 0)) else ""}


def table(df, by, title, minn=1):
    rows = [summ(g, str(k)) for k, g in df.groupby(by)]
    out = pd.DataFrame([r for r in rows if r["n"] >= minn])
    print(f"\n=== {title} ===")
    print(out.to_string(index=False))


print(f"\n#### ГОРИЗОНТ {H}h | комісії+прослизання {0.12}% кругом | період {raw.dt.min():%Y-%m-%d} -> {raw.dt.max():%Y-%m-%d} ####")
print(f"Baseline (випадкова угода, ATR-SL/TP бота), середній net R: LONG {base['LONG_net'].mean():+.3f}, SHORT {base['SHORT_net'].mean():+.3f}")
print(f"(беззбиткова точка: R=0; SL=−1R, TP=+{bot.ATR_TP_MULT/bot.ATR_SL_MULT:.2f}R)")

# ═══ 1. Кожне правило окремо (вибірка з кулдауном 4 год на sym+rule+type — щоб сигнали не дублювались) ═══
rd = dedupe(raw, ["sym", "rule", "type"], 4)
table(rd, ["rule", "type"], "ПРАВИЛА (кожен сигнал, кулдаун 4h на монету+правило+напрямок)")

# ═══ 2. «Як бачить користувач»: best на монету → кулдаун 120хв → топ-5 за прогін ═══
cand = cand.sort_values(["T", "sym"], key=lambda s: s.map(SYM_ORDER) if s.name == "sym" else s)
sent, last = [], {}
for T, g in cand.groupby("T", sort=True):
    ok = []
    for r in g.itertuples():
        key = f"{r.sym}_{r.type}"
        if key in last and T - last[key] < bot.COOLDOWN_MIN * 60_000:
            continue
        ok.append(r)
    ok.sort(key=lambda r: r.strength, reverse=True)
    for r in ok[:bot.MAX_SIGNALS_PER_RUN]:
        sent.append((r.sym, r.k, r.type, r.rule)); last[f"{r.sym}_{r.type}"] = T
sent = pd.DataFrame(sent, columns=["sym", "k", "type", "rule"])
al = raw[raw["is_best"]].merge(sent, on=["sym", "k", "type", "rule"])
print(f"\n--- Емуляція живого бота: {len(sent)} алертів, з них направлених LONG/SHORT: {len(al)} "
      f"(≈{len(al)/ ((raw.dt.max()-raw.dt.min()).days/7):.0f} на тиждень) ---")
al["tier"] = pd.cut(al["strength"], [0, 5, 7, 10], labels=["слабкий <6", "середній 6-7", "СИЛЬНИЙ 8+"])
table(al.assign(all="усі алерти"), "all", "УСІ АЛЕРТИ БОТА (як їх отримує користувач)")
table(al, "tier", "АЛЕРТИ ЗА СИЛОЮ (СИЛЬНИЙ = «заходь зараз»)")
table(al, "type", "АЛЕРТИ ЗА НАПРЯМКОМ")
table(al, "aligned", "АЛЕРТИ: за/проти тренду 4h")
table(al, ["rule"], "АЛЕРТИ ПО ПРАВИЛАХ")

# ═══ 3. Стабільність у часі (кварталах) для алертів ═══
al["q"] = al["dt"].dt.to_period("Q").astype(str)
table(al, "q", "АЛЕРТИ ПО КВАРТАЛАХ (чи стабільна поведінка)")

# ═══ 4. Перевірка достовірності: «старий» win-rate бота (±0.3% за 4h) vs живі 48% ═══
a4 = al.dropna(subset=["pct4"]).copy()
sg = np.where(a4["type"] == "LONG", a4["pct4"], -a4["pct4"])
a4["old"] = np.where(sg >= 0.3, "win", np.where(sg <= -0.3, "loss", "flat"))
def wr(x):
    w, l_, f = (x == "win").sum(), (x == "loss").sum(), (x == "flat").sum()
    return f"{w}W/{l_}L/{f}flat WR={w/(w+l_)*100:.0f}%" if w + l_ else "-"
print(f"\n=== СТАРА МЕТРИКА БОТА (±0.3% за 4h) на алертах бектесту ===")
print("весь період:        ", wr(a4["old"]))
last7 = a4[a4["dt"] >= a4["dt"].max() - pd.Timedelta(days=7)]
print("останні 7 днів:      ", wr(last7["old"]), f"| живий трекер бота за 7 днів: 63W/67L/19flat WR=48%")
print("по кварталах:        ", {q: wr(g["old"]) for q, g in a4.groupby("q")})
