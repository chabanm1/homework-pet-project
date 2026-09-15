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


def market_state(fg: int, fg_cls: str) -> str:
    lines = ["📊 <b>СТАН РИНКУ</b>", f"F&G: {fg} ({fg_cls})", "━━━━━━━━━━━━━━━━"]
    for sym in sn.SYMBOLS:
        df = sn.fetch_ohlcv(sym, "1h", 100)
        if df.empty:
            lines.append(f"{sym}: немає даних")
            continue
        ind = sn.indicators(df)
        if ind["e7"] > ind["e25"] > ind["e99"]:
            trend = "📈 висхідний"
        elif ind["e7"] < ind["e25"] < ind["e99"]:
            trend = "📉 низхідний"
        else:
            trend = "↔️ флет"
        coin = sym.split("/")[0]
        lines.append(
            f"<b>{coin}</b>: ${ind['price']:,.2f} ({ind['mom1h']:+.1f}%/1h) "
            f"RSI={ind['rsi']:.0f} {trend} обсяг×{ind['vs']:.1f}"
        )
    return "\n".join(lines)


def opportunities(fg: int) -> str:
    lines = ["🎯 <b>ПОТЕНЦІЙНО ВИГІДНІ СЕТАПИ</b>", "━━━━━━━━━━━━━━━━"]
    found = False
    for sym in sn.SYMBOLS:
        df = sn.fetch_ohlcv(sym, "1h", 100)
        if df.empty:
            continue
        ind  = sn.indicators(df)
        sigs = [s for s in sn.signals(ind, fg) if s["strength"] >= sn.MIN_SIGNAL_STRENGTH]
        if not sigs:
            continue
        found = True
        coin = sym.split("/")[0]
        best = max(sigs, key=lambda x: x["strength"])
        e = {"LONG": "🟢", "SHORT": "🔴", "MOVE": "⚡", "VOL": "👀"}.get(best["type"], "📊")
        lines.append(f"{e} <b>{coin}</b> ({best['type']}, сила={best['strength']})")
        lines.append(f"   {best['text'].splitlines()[0]}")
    if not found:
        lines.append("Чітких сетапів немає — ринок без вираженого напрямку")
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
        for e in econ[:5]:
            lines.append(f"  🕐 {e['when']} {e['country']} — {e['event']}")
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


def main():
    now_kyiv = datetime.now(KYIV)
    print(f"\n{'='*52}\n  MARKET DIGEST | {now_kyiv.strftime('%Y-%m-%d %H:%M %Z')}\n{'='*52}")

    fg, fg_cls = sn.fetch_fg()
    print(f"  F&G: {fg} ({fg_cls})")

    parts = [
        f"🕐 <b>ДАЙДЖЕСТ РИНКУ ({now_kyiv.strftime('%d.%m %H:%M')})</b>",
        market_state(fg, fg_cls),
        opportunities(fg),
    ]
    news_block = whats_new()
    if news_block:
        parts.append(news_block)

    msg = "\n\n".join(parts)
    print(f"  Довжина повідомлення: {len(msg)} символів")
    ok = sn.tg(msg, silent=True)
    print(f"  Надіслано: {'так' if ok else 'ні'}")


if __name__ == "__main__":
    main()
