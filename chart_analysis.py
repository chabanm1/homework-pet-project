"""
=============================================================================
LIQUIDITY SWEEP DETECTOR + CHART SNAPSHOT
=============================================================================
Окремий, самодостатній модуль — додається поверх signal_notifier_v2.py,
нічого в ньому не змінює. Ідея підглянута в трейдерських Telegram-каналах
(ICT/SMC стиль): ціна виносить недавній хай/лоу (забирає ліквідність
стопів), потім різко розвертається назад — свічка з довгим фітилем і
закриттям всередину діапазону. Це рахований патерн, а не "на око".

detect_sweep()   — знаходить такий патерн на щойно закритій свічці
session_levels() — Daily Open + Prev Day High/Low, з тих самих OHLCV,
                   без додаткових API-запитів
render_chart()   — малює свічки + рівні + зону ліквідності в PNG
=============================================================================
"""

import matplotlib
matplotlib.use("Agg")  # без дисплея — для GitHub Actions
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

SWEEP_LOOKBACK   = 30    # скільки попередніх свічок вважаємо "недавнім" хай/лоу
SWEEP_MIN_WICK_PCT = 0.15  # мінімальний розмір фітиля відносно ATR-подібного розмаху, щоб не ловити шум


def detect_sweep(df: pd.DataFrame, lookback: int = SWEEP_LOOKBACK) -> dict | None:
    """Перевіряє останню ЗАКРИТУ свічку (передостанню в df, бо остання може
    бути ще формуватись) на предмет liquidity sweep:
      - лоу/хай пробиває екстремум попередніх `lookback` свічок
      - закриття повертається назад всередину діапазону
      - є видимий фітиль у бік пробою (не просто закрився за межею)
    Повертає None, якщо патерну немає.
    """
    if len(df) < lookback + 3:
        return None

    idx = len(df) - 2   # передостання = остання ЗАКРИТА свічка
    window = df.iloc[idx - lookback: idx]
    if window.empty:
        return None

    row = df.iloc[idx]
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    prior_high = window["high"].max()
    prior_low  = window["low"].min()
    rng = h - l
    if rng <= 0:
        return None

    # Bearish sweep: винесли хай вище прайорного максимуму, закрились назад нижче
    if h > prior_high and c < prior_high and c < o:
        wick = h - max(o, c)
        if wick / rng >= SWEEP_MIN_WICK_PCT:
            return {
                "type": "SHORT", "level": float(prior_high),
                "candle_idx": idx, "time": row["time"], "wick_pct": wick / rng,
                "text": "винесли ліквідність вище недавнього хая і розвернулись вниз",
            }

    # Bullish sweep: винесли лоу нижче прайорного мінімуму, закрились назад вище
    if l < prior_low and c > prior_low and c > o:
        wick = min(o, c) - l
        if wick / rng >= SWEEP_MIN_WICK_PCT:
            return {
                "type": "LONG", "level": float(prior_low),
                "candle_idx": idx, "time": row["time"], "wick_pct": wick / rng,
                "text": "винесли ліквідність нижче недавнього лоу і розвернулись вгору",
            }

    return None


def session_levels(df: pd.DataFrame) -> dict:
    """Daily Open (00:00 UTC сьогодні) + хай/лоу попередньої UTC-доби.
    Рахується з уже наявних 1h OHLCV — без нових API-запитів."""
    d = df.copy()
    d["date"] = d["time"].dt.date
    today = d["date"].iloc[-1]
    dates = sorted(d["date"].unique())
    if today not in dates:
        return {}

    today_rows = d[d["date"] == today]
    levels = {"daily_open": float(today_rows.iloc[0]["open"])}

    i = dates.index(today)
    if i > 0:
        prev_rows = d[d["date"] == dates[i - 1]]
        levels["prev_day_high"] = float(prev_rows["high"].max())
        levels["prev_day_low"]  = float(prev_rows["low"].min())

    iso = d["time"].dt.isocalendar()
    latest_year, latest_week = iso["year"].iloc[-1], iso["week"].iloc[-1]
    week_rows = d[(iso["year"] == latest_year) & (iso["week"] == latest_week)]
    if not week_rows.empty:
        levels["weekly_open"] = float(week_rows.iloc[0]["open"])

    return levels


def render_chart(df: pd.DataFrame, symbol: str, sweep: dict, levels: dict,
                  out_path: str, bars: int = 200) -> str:
    """Малює останні `bars` 1h свічок + рівні сесій + затінену зону
    навколо рівня, який був виметений (liquidity zone)."""
    d = df.tail(bars).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=130)

    up_color, down_color = "#26a69a", "#ef5350"
    width = 0.6
    for i, row in d.iterrows():
        color = up_color if row["close"] >= row["open"] else down_color
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=1)
        y0, y1 = sorted([row["open"], row["close"]])
        ax.add_patch(plt.Rectangle((i - width / 2, y0), width, max(y1 - y0, 1e-9),
                                    facecolor=color, edgecolor=color))

    # Liquidity zone навколо рівня, який змели
    lvl = sweep["level"]
    zone_h = (d["high"].max() - d["low"].min()) * 0.02
    ax.axhspan(lvl - zone_h, lvl + zone_h, color="#ffb300", alpha=0.18, zorder=0)
    ax.axhline(lvl, color="#ffb300", linestyle="--", linewidth=1.2,
               label=f"Зона ліквідності ${lvl:,.4g}")

    # Sweep-свічку підсвітити — шукаємо за часом, бо `df` тут може бути
    # ширшим датафреймом (більше історії), ніж той, на якому детектили sweep
    match = d.index[d["time"] == sweep["time"]]
    if len(match):
        ax.axvline(match[0], color="#ffb300", linewidth=0.8, alpha=0.5)

    label_map = {"daily_open": ("Daily Open", "#42a5f5"),
                 "weekly_open": ("Weekly Open", "#66bb6a"),
                 "prev_day_high": ("Prev Day High", "#ab47bc"),
                 "prev_day_low": ("Prev Day Low", "#ab47bc")}
    for key, (label, color) in label_map.items():
        if key in levels:
            ax.axhline(levels[key], color=color, linestyle=":", linewidth=1)
            ax.text(len(d) - 1, levels[key], f" {label}", color=color,
                    fontsize=8, va="center")

    direction = "LONG (розворот вгору)" if sweep["type"] == "LONG" else "SHORT (розворот вниз)"
    ax.set_title(f"{symbol} — Liquidity Sweep → {direction}", fontsize=11, color="white")
    ax.set_xlim(-1, len(d))
    ax.set_facecolor("#131722")
    fig.patch.set_facecolor("#131722")
    ax.tick_params(colors="#787b86", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#2a2e39")
    ax.grid(color="#2a2e39", linewidth=0.5, alpha=0.5)
    ax.legend(loc="lower left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")
    ax.set_xticks([])

    fig.tight_layout()
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path
