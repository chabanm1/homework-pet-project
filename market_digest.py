"""
=============================================================================
MARKET DIGEST — погодинний дайджест ринку
=============================================================================
Щогодини надсилає в Telegram (тихо, без звуку — це інформаційний огляд,
не терміновий алерт):
  1. Стан ринку (F&G, RSI/тренд/обсяг по ETH/BTC/SOL)
  2. Потенційно вигідні сетапи (сканування сигналів по трекованих монетах,
     НЕ реальні відкриті позиції — бот не має доступу до акаунта)
  3. Що нового за останню годину (новини, Binance анонси) + майбутні
     макроподії на найближчі години
=============================================================================
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import signal_notifier_v2 as sn

KYIV = ZoneInfo("Europe/Kyiv")

FRESH_HOURS = 1     # вікно "що нового" — під інтервал запуску (раз/год)
ECON_LOOKAHEAD_HOURS = 6   # на скільки годин вперед показувати макроподії

QUIET_HOURS_START = 1   # з 01:00 до 09:00 за Києвом дайджест не турбує
QUIET_HOURS_END   = 9


def fetch_symbol_data(fg: int) -> list[dict]:
    """Одне зведення OHLCV + funding + сигналів на символ — і для
    market_state, і для opportunities, щоб не тягти дані двічі."""
    data = []
    for sym in sn.SYMBOLS:
        df = sn.fetch_ohlcv(sym, "1h", 100)
        if df.empty:
            data.append({"sym": sym, "ind": None})
            continue
        ind       = sn.indicators(df)
        funding   = sn.fetch_funding(sym)
        htf_trend = sn.fetch_trend(sym)
        sigs      = [s for s in sn.signals(ind, fg, funding, htf_trend) if s["strength"] >= sn.MIN_SIGNAL_STRENGTH]
        data.append({"sym": sym, "ind": ind, "funding": funding, "sigs": sigs})
    return data


def market_state(data: list[dict], fg: int, fg_cls: str) -> str:
    lines = ["📊 <b>СТАН РИНКУ</b>", f"F&G: {fg} ({fg_cls})", "━━━━━━━━━━━━━━━━"]
    for d in data:
        if d["ind"] is None:
            lines.append(f"{d['sym']}: немає даних")
            continue
        ind = d["ind"]
        if ind["e7"] > ind["e25"] > ind["e99"]:
            trend = "📈 висхідний"
        elif ind["e7"] < ind["e25"] < ind["e99"]:
            trend = "📉 низхідний"
        else:
            trend = "↔️ флет"
        coin = d["sym"].split("/")[0]
        funding = d["funding"]
        fund_str = f" | fund={funding*100:+.3f}%" if funding is not None else ""
        lines.append(
            f"<b>{coin}</b>: ${sn.fmt_price(ind['price'])} ({ind['mom1h']:+.1f}%/1h) "
            f"RSI={ind['rsi']:.0f} {trend} обсяг×{ind['vs']:.1f}{fund_str}"
        )
    return "\n".join(lines)


MAX_OPPORTUNITIES = 8   # топ-20 монет може дати забагато сетапів для одного Telegram-повідомлення

def opportunities(data: list[dict]) -> str:
    lines = ["🎯 <b>ПОТЕНЦІЙНО ВИГІДНІ СЕТАПИ</b>", "━━━━━━━━━━━━━━━━"]
    candidates = []
    for d in data:
        if d["ind"] is None or not d["sigs"]:
            continue
        best = max(d["sigs"], key=lambda x: x["strength"])
        candidates.append((d, best))
    candidates.sort(key=lambda pair: pair[1]["strength"], reverse=True)

    if not candidates:
        lines.append("Чітких сетапів немає — ринок без вираженого напрямку")
        return "\n".join(lines)

    for d, best in candidates[:MAX_OPPORTUNITIES]:
        coin = d["sym"].split("/")[0]
        e = {"LONG": "🟢", "SHORT": "🔴", "MOVE": "⚡", "VOL": "👀"}.get(best["type"], "📊")
        lines.append(f"{e} <b>{coin}</b> ({best['type']}, сила={best['strength']})")
        lines.append(f"   {best['text'].splitlines()[0]}")
        advice = sn.compact_advice(best, d["ind"])
        if advice:
            lines.append(advice)

    if len(candidates) > MAX_OPPORTUNITIES:
        lines.append(f"\n… ще {len(candidates) - MAX_OPPORTUNITIES} сетапів слабших за силою")

    lines.append("\n⚠️ Евристика на основі ATR, не фінансова порада")
    return "\n".join(lines)


def whats_new() -> str | None:
    news = sn.fetch_news(hours_back=FRESH_HOURS)
    ann  = sn.fetch_binance_announcements(hours_back=FRESH_HOURS)
    econ = sn.fetch_econ_calendar(hours_ahead=ECON_LOOKAHEAD_HOURS)

    if not (news or ann or econ):
        return None

    lines = ["📰 <b>ЩО НОВОГО</b>", "━━━━━━━━━━━━━━━━"]

    if econ:
        lines.append(f"📅 <b>Макроподії (наступні {ECON_LOOKAHEAD_HOURS}год):</b>")
        for e in econ[:3]:
            lines.append(f"  🕐 {e['when']} (через {e['in']}) {e['country']} — {e['event']}")
            lines.append(f"     💡 {e['explain']}")
        lines.append("")

    if ann:
        lines.append("🏦 <b>Binance анонси:</b>")
        for a in ann[:3]:
            lines.append(f"  {a['label']} [{a['pub']}] {a['title'][:70]}")
        lines.append("")

    if news:
        lines.append("📰 <b>Новини:</b>")
        for n in news[:4]:
            sent = "🟢" if n["score"] > 0 else "🔴" if n["score"] < 0 else "⚪"
            lines.append(f"  {sent} [{n['pub']}] {n['title'][:70]}")

    return "\n".join(lines)


RULE_NAMES = {
    "RSI_EXTREME": "RSI екстремум", "RSI_MILD": "RSI помірний",
    "MACD_CROSS": "MACD cross", "EMA25_BREAK": "Пробій EMA25",
    "FG_EXTREME": "F&G екстремум", "BB_LOWER": "Нижня BB",
    "FUNDING_EXTREME": "Funding екстремум", "OTHER": "Інше",
}

def performance() -> str | None:
    cd = sn.load_cd()
    stats = sn.signal_stats(cd, days=7)
    if stats["total"] == 0:
        return None
    lines = ["📈 <b>ТОЧНІСТЬ СИГНАЛІВ (7 днів)</b>", "━━━━━━━━━━━━━━━━"]
    if stats["win_rate"] is not None:
        lines.append(f"Win-rate: {stats['win_rate']:.0f}% ({stats['wins']}W/{stats['losses']}L, {stats['flats']} flat)")
    else:
        lines.append(f"{stats['flats']} flat, ще недостатньо вирішених сигналів")
    if stats["avg_pct"] is not None:
        lines.append(f"Середній рух: {stats['avg_pct']:+.2f}%")

    today = sn.signal_stats_by_rule(cd, days=1)
    if today:
        lines.append("")
        lines.append("📊 <b>За сьогодні, по типу сигналу:</b>")
        for r in today:
            name = RULE_NAMES.get(r["rule"], r["rule"])
            if r["win_rate"] is not None:
                lines.append(f"  {name}: {r['win_rate']:.0f}% ({r['wins']}W/{r['losses']}L)")
            else:
                lines.append(f"  {name}: {r['total']} сигн. — замало для % (мін. 3 вирішених)")
    return "\n".join(lines)


def main():
    now_kyiv = datetime.now(KYIV)
    print(f"\n{'='*52}\n  MARKET DIGEST | {now_kyiv.strftime('%Y-%m-%d %H:%M %Z')}\n{'='*52}")

    if QUIET_HOURS_START <= now_kyiv.hour < QUIET_HOURS_END:
        print(f"  → тихі години ({QUIET_HOURS_START}:00-{QUIET_HOURS_END}:00 Київ), виходжу")
        return

    fg, fg_cls = sn.fetch_fg()
    print(f"  F&G: {fg} ({fg_cls})")
    data = fetch_symbol_data(fg)

    parts = [
        f"🕐 <b>ДАЙДЖЕСТ РИНКУ ({now_kyiv.strftime('%d.%m %H:%M')})</b>",
        market_state(data, fg, fg_cls),
        opportunities(data),
    ]
    perf_block = performance()
    if perf_block:
        parts.append(perf_block)

    news_block = whats_new()
    if news_block:
        parts.append(news_block)

    msg = "\n\n".join(parts)
    print(f"  Довжина повідомлення: {len(msg)} символів")
    ok = sn.tg(msg, silent=True)
    print(f"  Надіслано: {'так' if ok else 'ні'}")


if __name__ == "__main__":
    main()
