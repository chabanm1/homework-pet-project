"""
=============================================================================
DAILY DIGEST — щоденний дайджест о 10:00 (Київ)
=============================================================================
Раз на день надсилає в Telegram:
  1. Стан ринку (F&G, RSI/тренд/обсяг по ETH/BTC/SOL)
  2. Що може вплинути сьогодні (новини, Binance анонси, макрокалендар)
  3. Потенційно вигідні сетапи (сканування сигналів по трекованих монетах,
     НЕ реальні відкриті позиції — бот не має доступу до акаунта)

Запускається через GitHub Actions двічі на день (крони під літній і
зимовий UTC-зсув Києва), але реально працює лише раз — перевіряє
поточний київський час і виходить, якщо зараз не година дайджесту.
=============================================================================
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import signal_notifier_v2 as sn

KYIV = ZoneInfo("Europe/Kyiv")
DIGEST_HOUR = 10   # київська година, о якій надсилаємо дайджест


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


def todays_news() -> str | None:
    news = sn.fetch_news(hours_back=24)
    ann  = sn.fetch_binance_announcements(hours_back=24)

    now_kyiv  = datetime.now(KYIV)
    eod_kyiv  = now_kyiv.replace(hour=23, minute=59, second=59, microsecond=0)
    hours_left = max(1.0, (eod_kyiv - now_kyiv).total_seconds() / 3600)
    econ = sn.fetch_econ_calendar(hours_ahead=hours_left)

    if not (news or ann or econ):
        return None

    lines = ["📰 <b>ЩО МОЖЕ ВПЛИНУТИ СЬОГОДНІ</b>", "━━━━━━━━━━━━━━━━"]

    if econ:
        lines.append("📅 <b>Макроподії:</b>")
        for e in econ[:5]:
            lines.append(f"  🕐 {e['when']} {e['country']} — {e['event']}")
        lines.append("")

    if ann:
        lines.append("🏦 <b>Binance анонси:</b>")
        for a in ann[:3]:
            lines.append(f"  {a['label']} [{a['pub']}] {a['title'][:70]}")
        lines.append("")

    if news:
        lines.append("📰 <b>Топ новини:</b>")
        for n in news[:4]:
            sent = "🟢" if n["score"] > 0 else "🔴" if n["score"] < 0 else "⚪"
            lines.append(f"  {sent} [{n['pub']}] {n['title'][:70]}")

    return "\n".join(lines)


def main():
    now_kyiv = datetime.now(KYIV)
    force    = os.getenv("FORCE_DIGEST", "").lower() == "true"
    print(f"Kyiv now: {now_kyiv.strftime('%Y-%m-%d %H:%M %Z')}")
    if now_kyiv.hour != DIGEST_HOUR and not force:
        print(f"  → не час дайджесту (чекаємо {DIGEST_HOUR}:00), виходжу")
        return

    print(f"\n{'='*52}\n  DAILY DIGEST | {now_kyiv.strftime('%Y-%m-%d %H:%M')}\n{'='*52}")

    fg, fg_cls = sn.fetch_fg()
    print(f"  F&G: {fg} ({fg_cls})")

    parts = [
        f"☀️ <b>ЩОДЕННИЙ ДАЙДЖЕСТ ({now_kyiv.strftime('%d.%m %H:%M')})</b>",
        market_state(fg, fg_cls),
        opportunities(fg),
    ]
    news_block = todays_news()
    if news_block:
        parts.append(news_block)

    msg = "\n\n".join(parts)
    print(f"  Довжина повідомлення: {len(msg)} символів")
    ok = sn.tg(msg)
    print(f"  Надіслано: {'так' if ok else 'ні'}")


if __name__ == "__main__":
    main()
