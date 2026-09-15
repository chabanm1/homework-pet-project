"""
=============================================================================
CRYPTO SIGNAL + NEWS NOTIFIER v2.0
=============================================================================
Запускається через GitHub Actions кожні 30 хвилин.
Надсилає в Telegram:
  1. Технічні сигнали (RSI, MACD, обсяг, BB)
  2. Великі рухи ціни (±2.5% за 1 годину)
  3. Важливі новини (CryptoPanic hot/important)
  4. Лістинги/делістинги на Binance (офіційні анонси)
  5. Макроподії (CPI, FOMC, NFP тощо — ForexFactory economic calendar)

БЕЗ КОМП'ЮТЕРА: запускається на хмарі (GitHub Actions, безкоштовно)
=============================================================================
"""

import ccxt
import requests
import pandas as pd
import numpy as np
import json
import os
import time
from datetime import datetime, timezone, timedelta

# ══════════════════════════════════════════════════════════════
# КОНФІГ — через GitHub Secrets (не хардкодь в коді!)
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
CRYPTOPANIC_KEY   = os.getenv("CRYPTOPANIC_KEY", "")   # безкоштовно на cryptopanic.com

SYMBOLS = ["ETH/USDT", "BTC/USDT", "SOL/USDT"]
COINS_FOR_NEWS = ["ETH", "BTC", "SOL", "BNB"]   # для фільтрації новин

# Пороги
MIN_SIGNAL_STRENGTH = 3     # 1-10, рекомендую 3
PRICE_MOVE_ALERT    = 2.5   # % за останню годину → сповіщення
NEWS_HOURS_BACK     = 1.5   # шукати новини за останні N годин
ANNOUNCE_HOURS_BACK = 24    # шукати анонси Binance за останні N годин
ECON_HOURS_AHEAD    = 24    # шукати макроподії на найближчі N годин
FUNDING_EXTREME     = 0.0004  # 0.04%/8h — за межею цього вважаємо funding екстремальним

COOLDOWN_FILE = os.getenv("COOLDOWN_FILE", "crypto_cooldown.json")
COOLDOWN_MIN  = 120         # хвилин між однаковими сигналами
SEEN_CAP      = 500         # скільки id одноразових подій пам'ятаємо


# ══════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════
def tg(msg: str, silent: bool = False) -> bool:
    if "YOUR_BOT_TOKEN" in TELEGRAM_TOKEN:
        print(f"[DEMO TG]\n{msg}\n")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":              TELEGRAM_CHAT_ID,
                "text":                 msg,
                "parse_mode":           "HTML",
                "disable_notification": silent,
            }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"TG error: {e}"); return False


# ══════════════════════════════════════════════════════════════
# COOLDOWN
# ══════════════════════════════════════════════════════════════
def load_cd() -> dict:
    try:
        with open(COOLDOWN_FILE) as f: return json.load(f)
    except Exception: return {}

def save_cd(cd: dict):
    try:
        with open(COOLDOWN_FILE, "w") as f: json.dump(cd, f)
    except Exception: pass

def ok_to_send(key: str, cd: dict) -> bool:
    last = cd.get(key, 0)
    return (time.time() - last) / 60 >= COOLDOWN_MIN

def mark_sent(key: str, cd: dict):
    cd[key] = time.time()

def already_seen(key: str, cd: dict) -> bool:
    """Для одноразових подій (лістинг/делістинг/макроподія) — без TTL,
    бо звичайний cooldown відпустив би й почав дублювати той самий анонс."""
    return key in cd.get("_seen", [])

def mark_seen(key: str, cd: dict):
    seen = cd.setdefault("_seen", [])
    seen.append(key)
    del seen[:-SEEN_CAP]


# ══════════════════════════════════════════════════════════════
# ДАНІ
# ══════════════════════════════════════════════════════════════
def fetch_ohlcv(symbol: str, tf="1h", limit=100):
    try:
        ex = ccxt.binance({"options": {"defaultType": "future"},
                           "enableRateLimit": True})
        bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(bars, columns=["ts","open","high","low","close","vol"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df
    except Exception as e:
        print(f"  OHLCV {symbol}: {e}"); return pd.DataFrame()

def fetch_funding(symbol: str) -> float | None:
    """Funding rate ф'ючерса (частка за 8-годинний період, напр. 0.0005 = 0.05%)."""
    try:
        ex = ccxt.binance({"options": {"defaultType": "future"},
                           "enableRateLimit": True})
        return ex.fetch_funding_rate(symbol).get("fundingRate")
    except Exception as e:
        print(f"  Funding {symbol}: {e}"); return None

def fetch_fg() -> tuple[int, str]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1",
                         timeout=8).json()
        return int(r["data"][0]["value"]), r["data"][0]["value_classification"]
    except Exception:
        return 50, "Neutral"

def fetch_news(hours_back: float = NEWS_HOURS_BACK) -> list[dict]:
    """Новини з CryptoPanic (hot + important)."""
    results = []
    try:
        params = {
            "public":     "true",
            "filter":     "hot",
            "currencies": ",".join(COINS_FOR_NEWS),
        }
        if CRYPTOPANIC_KEY:
            params["auth_token"] = CRYPTOPANIC_KEY

        r = requests.get("https://cryptopanic.com/api/v1/posts/",
                         params=params, timeout=10)
        if r.status_code != 200:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        for item in r.json().get("results", [])[:20]:
            try:
                pub = datetime.fromisoformat(
                    item["published_at"].replace("Z", "+00:00"))
                if pub < cutoff:
                    continue
                votes = item.get("votes", {})
                pos   = votes.get("positive", 0)
                neg   = votes.get("negative", 0)
                results.append({
                    "title":  item.get("title", ""),
                    "source": item.get("source", {}).get("title", ""),
                    "url":    item.get("url", ""),
                    "pos":    pos, "neg": neg,
                    "score":  pos - neg,
                    "pub":    pub.strftime("%H:%M"),
                    "coins":  [c["code"] for c in item.get("currencies", [])],
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  News error: {e}")
    return sorted(results, key=lambda x: x["score"], reverse=True)


# Binance CMS catalog IDs (публічні, без ключа)
BINANCE_CATALOGS = {48: "🆕 Лістинг", 161: "⚠️ Делістинг"}

def fetch_binance_announcements(hours_back: float = ANNOUNCE_HOURS_BACK) -> list[dict]:
    """Нові лістинги/делістинги з офіційних анонсів Binance, що
    стосуються монет з COINS_FOR_NEWS. Без ключа, публічний CMS API."""
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    for cat_id, label in BINANCE_CATALOGS.items():
        try:
            r = requests.get(
                "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                params={"type": 1, "pageNo": 1, "pageSize": 10, "catalogId": cat_id},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code != 200:
                continue
            catalogs = r.json().get("data", {}).get("catalogs", [])
            if not catalogs:
                continue
            for art in catalogs[0].get("articles", []):
                try:
                    pub = datetime.fromtimestamp(art["releaseDate"] / 1000, tz=timezone.utc)
                    if pub < cutoff:
                        continue
                    title = art.get("title", "")
                    if not any(coin in title.upper() for coin in COINS_FOR_NEWS):
                        continue
                    results.append({
                        "id":    f"ann_{art['id']}",
                        "label": label,
                        "title": title,
                        "url":   f"https://www.binance.com/en/support/announcement/{art.get('code', '')}",
                        "pub":   pub.strftime("%d.%m %H:%M"),
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"  Binance announce error: {e}")
    return results


ECON_CURRENCIES = ("USD", "EUR")   # найбільше впливають на крипторинок

def fetch_econ_calendar(hours_ahead: float = ECON_HOURS_AHEAD) -> list[dict]:
    """High-impact макроподії (CPI, FOMC, NFP тощо) на найближчі
    hours_ahead годин. Публічний JSON-фід календаря ForexFactory
    (nfs.faireconomy.media) — без ключа, без офіційного SLA."""
    results = []
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            print(f"  Econ calendar HTTP {r.status_code}")
            return []
        now   = datetime.now(timezone.utc)
        until = now + timedelta(hours=hours_ahead)
        for ev in r.json():
            try:
                if ev.get("impact") != "High":
                    continue
                if ev.get("country") not in ECON_CURRENCIES:
                    continue
                when = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
                if not (now <= when <= until):
                    continue
                results.append({
                    "id":       f"econ_{ev.get('title')}_{ev['date']}",
                    "event":    ev.get("title", ""),
                    "country":  ev.get("country", ""),
                    "when":     when.strftime("%d.%m %H:%M"),
                    "forecast": ev.get("forecast") or None,
                    "prev":     ev.get("previous") or None,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  Econ calendar error: {e}")
    return sorted(results, key=lambda x: x["when"])


# ══════════════════════════════════════════════════════════════
# ІНДИКАТОРИ
# ══════════════════════════════════════════════════════════════
def indicators(df: pd.DataFrame) -> dict:
    c, v = df["close"], df["vol"]
    # RSI
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean()
    l = (-d.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - 100/(1 + g/l.replace(0, np.nan))).iloc[-1])
    # MACD
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    mac = e12 - e26
    sig = mac.ewm(span=9, adjust=False).mean()
    macd_xu = float(mac.iloc[-2]) <= float(sig.iloc[-2]) and float(mac.iloc[-1]) > float(sig.iloc[-1])
    macd_xd = float(mac.iloc[-2]) >= float(sig.iloc[-2]) and float(mac.iloc[-1]) < float(sig.iloc[-1])
    # EMA
    e7  = float(c.ewm(span=7,  adjust=False).mean().iloc[-1])
    e25 = float(c.ewm(span=25, adjust=False).mean().iloc[-1])
    e99 = float(c.ewm(span=99, adjust=False).mean().iloc[-1])
    # Volume
    vm  = float(v.rolling(20).mean().iloc[-1])
    vs  = float(v.iloc[-1]) / vm if vm > 0 else 1.0
    # BB
    m20 = c.rolling(20).mean()
    s20 = c.rolling(20).std()
    bb_up = float((m20 + 2*s20).iloc[-1])
    bb_lo = float((m20 - 2*s20).iloc[-1])
    bb_pct = (float(c.iloc[-1]) - bb_lo) / (bb_up - bb_lo + 1e-10)
    # Momentum
    mom1h = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
    mom3h = (float(c.iloc[-1]) - float(c.iloc[-4])) / float(c.iloc[-4]) * 100 if len(c) > 4 else 0
    return {
        "price": float(c.iloc[-1]), "prev": float(c.iloc[-2]),
        "rsi": rsi, "macd_xu": macd_xu, "macd_xd": macd_xd,
        "e7": e7, "e25": e25, "e99": e99,
        "vs": vs, "bb": bb_pct, "mom1h": mom1h, "mom3h": mom3h,
    }


# ══════════════════════════════════════════════════════════════
# СИГНАЛИ
# ══════════════════════════════════════════════════════════════
def signals(ind: dict, fg: int, funding: float | None = None) -> list[dict]:
    p, rsi, vs, bb = ind["price"], ind["rsi"], ind["vs"], ind["bb"]
    sigs = []

    # Великий ціновий рух
    if abs(ind["mom1h"]) >= PRICE_MOVE_ALERT:
        dir_ = "🟢" if ind["mom1h"] > 0 else "🔴"
        sigs.append({"type": "MOVE", "strength": 8 if abs(ind["mom1h"]) > 4 else 6,
                     "text": f"{dir_} Різкий рух: {ind['mom1h']:+.1f}% за годину\n"
                             f"Ціна: ${p:,.2f} | Обсяг ×{vs:.1f}"})

    # Великий обсяг
    if vs >= 2.8:
        d = "↑" if ind["mom1h"] > 0 else "↓"
        sigs.append({"type": "VOL", "strength": 7,
                     "text": f"👀 Обсяг ×{vs:.1f} від середнього {d}\n"
                             f"Великі гравці активні!"})

    # RSI extreme + об'єм
    if rsi < 28 and vs > 1.3:
        sigs.append({"type": "LONG", "strength": 9,
                     "text": f"🟢 RSI={rsi:.0f} — сильна перепроданість\n"
                             f"+ підвищений обсяг ×{vs:.1f}"})
    elif rsi < 35:
        sigs.append({"type": "LONG", "strength": 5,
                     "text": f"🟡 RSI={rsi:.0f} — перепроданість"})
    elif rsi > 72 and vs > 1.3:
        sigs.append({"type": "SHORT", "strength": 8,
                     "text": f"🔴 RSI={rsi:.0f} — сильна перекупленість\n"
                             f"+ обсяг ×{vs:.1f}"})
    elif rsi > 68:
        sigs.append({"type": "SHORT", "strength": 5,
                     "text": f"🟠 RSI={rsi:.0f} — перекупленість"})

    # MACD cross
    if ind["macd_xu"] and p > ind["e7"]:
        sigs.append({"type": "LONG", "strength": 7,
                     "text": f"📈 MACD Golden Cross + ціна вище EMA7"})
    if ind["macd_xd"] and p < ind["e7"]:
        sigs.append({"type": "SHORT", "strength": 6,
                     "text": f"📉 MACD Death Cross + ціна нижче EMA7"})

    # EMA25 пробій
    if ind["prev"] < ind["e25"] and p > ind["e25"] and vs > 1.2:
        sigs.append({"type": "LONG", "strength": 7,
                     "text": f"🚀 Пробій EMA25 вгору! (${ind['e25']:.0f})\n"
                             f"Обсяг ×{vs:.1f}"})
    if ind["prev"] > ind["e25"] and p < ind["e25"] and vs > 1.2:
        sigs.append({"type": "SHORT", "strength": 7,
                     "text": f"💔 Пробій EMA25 вниз (${ind['e25']:.0f})\n"
                             f"Обсяг ×{vs:.1f}"})

    # F&G extreme
    if fg < 20 and rsi < 35:
        sigs.append({"type": "LONG", "strength": 10,
                     "text": f"🔥 F&G={fg} (Extreme Fear) + RSI={rsi:.0f}\n"
                             f"Найсильніший contrarian сигнал!"})
    if fg > 82 and bb > 0.92:
        sigs.append({"type": "SHORT", "strength": 9,
                     "text": f"⚠️ F&G={fg} (Extreme Greed) + ціна у верхній BB\n"
                             f"Небезпечна ейфорія"})

    # Нижня межа BB
    if bb < 0.12 and rsi < 40:
        sigs.append({"type": "LONG", "strength": 6,
                     "text": f"🎯 Ціна біля нижньої BB + RSI={rsi:.0f}"})

    # Екстремальний funding rate (ф'ючерси) — перегріта одна сторона ринку
    if funding is not None:
        if funding >= FUNDING_EXTREME:
            sigs.append({"type": "SHORT", "strength": 6,
                         "text": f"💸 Funding={funding*100:.3f}%/8г — лонги переплачують\n"
                                 f"Перегрів, ризик long squeeze"})
        elif funding <= -FUNDING_EXTREME:
            sigs.append({"type": "LONG", "strength": 6,
                         "text": f"💰 Funding={funding*100:.3f}%/8г — шорти переплачують\n"
                                 f"Перегрів у шортах, contrarian LONG"})

    return sigs


# ══════════════════════════════════════════════════════════════
# ФОРМАТУВАННЯ ПОВІДОМЛЕНЬ
# ══════════════════════════════════════════════════════════════
def fmt_signal(symbol: str, ind: dict, fg: int, fg_cls: str,
               sig: dict) -> str:
    coin = symbol.split("/")[0]
    t    = datetime.now().strftime("%H:%M")
    e = {"LONG":"🟢","SHORT":"🔴","MOVE":"⚡","VOL":"👀"}.get(sig["type"],"📊")
    return (
        f"{e} <b>{coin}/USDT — {sig['type']} ({t})</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{sig['text']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Ціна: <b>${ind['price']:,.2f}</b> ({ind['mom1h']:+.1f}%/1h)\n"
        f"📊 RSI={ind['rsi']:.0f} | F&G={fg} {fg_cls}\n"
        f"📉 EMA7=${ind['e7']:.0f} | EMA25=${ind['e25']:.0f}\n"
        f"📦 Обсяг ×{ind['vs']:.1f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Зайди глянь!</b>"
    )

def fmt_news(news: list[dict], fg: int, fg_cls: str) -> str | None:
    if not news:
        return None
    t = datetime.now().strftime("%H:%M")
    lines = [f"📰 <b>КРИПТО НОВИНИ ({t})</b>",
             f"F&G: {fg} ({fg_cls})",
             "━━━━━━━━━━━━━━━━"]
    for n in news[:4]:
        coins = " ".join(f"#{c}" for c in n["coins"][:3])
        sent  = "🟢" if n["score"] > 0 else "🔴" if n["score"] < 0 else "⚪"
        lines.append(f"{sent} [{n['pub']}] <b>{n['source']}</b>")
        lines.append(f"   {n['title'][:80]}")
        if coins:
            lines.append(f"   {coins}")
        lines.append("")
    lines.append("📱 Більше: cryptopanic.com")
    return "\n".join(lines)

def fmt_announcement(a: dict) -> str:
    return (
        f"{a['label']} <b>Binance ({a['pub']})</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{a['title']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔗 {a['url']}"
    )

def fmt_econ(e: dict) -> str:
    ev = (
        f"📅 <b>Макроподія: {e['event']} ({e['country']})</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 {e['when']} UTC"
    )
    if e.get("forecast") is not None:
        ev += f"\n📊 Прогноз: {e['forecast']}"
    if e.get("prev") is not None:
        ev += f" | Попередній: {e['prev']}"
    return ev


# ══════════════════════════════════════════════════════════════
# ГОЛОВНА ФУНКЦІЯ
# ══════════════════════════════════════════════════════════════
def main():
    print(f"\n{'='*52}")
    print(f"  Crypto Notifier v2.0 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*52}")

    cd   = load_cd()
    fg, fg_cls = fetch_fg()
    print(f"  F&G: {fg} ({fg_cls})")

    sent = 0

    # ── 1. Технічні сигнали ─────────────────────────────────
    for sym in SYMBOLS:
        print(f"\n  {sym}...", end=" ")
        df = fetch_ohlcv(sym, "1h", 100)
        if df.empty:
            print("! немає даних"); continue

        ind     = indicators(df)
        funding = fetch_funding(sym)
        sigs    = [s for s in signals(ind, fg, funding) if s["strength"] >= MIN_SIGNAL_STRENGTH]

        if not sigs:
            print(f"сигналів немає (RSI={ind['rsi']:.0f} mov={ind['mom1h']:+.1f}%)")
            continue

        best = max(sigs, key=lambda x: x["strength"])
        key  = f"{sym}_{best['type']}"
        print(f"СИГНАЛ {best['type']} сила={best['strength']}")

        if not ok_to_send(key, cd):
            print(f"  → cooldown активний"); continue

        msg = fmt_signal(sym, ind, fg, fg_cls, best)
        if tg(msg):
            mark_sent(key, cd); sent += 1
        time.sleep(0.3)

    # ── 2. Новини ────────────────────────────────────────────
    print(f"\n  Новини...", end=" ")
    news = fetch_news()
    print(f"{len(news)} за {NEWS_HOURS_BACK}год")

    if news:
        news_key = f"news_{datetime.now().strftime('%Y%m%d_%H')}"
        if ok_to_send(news_key, cd):
            msg = fmt_news(news, fg, fg_cls)
            if msg and tg(msg, silent=True):  # silent = без звуку
                mark_sent(news_key, cd); sent += 1

    # ── 3. Лістинги/делістинги Binance ──────────────────────
    print(f"  Анонси Binance...", end=" ")
    announcements = fetch_binance_announcements()
    print(f"{len(announcements)} нових")

    for a in announcements:
        if already_seen(a["id"], cd):
            continue
        if tg(fmt_announcement(a)):
            mark_seen(a["id"], cd); sent += 1
        time.sleep(0.3)

    # ── 4. Макрокалендар (Finnhub) ──────────────────────────
    print(f"  Econ calendar...", end=" ")
    econ = fetch_econ_calendar()
    print(f"{len(econ)} high-impact подій")

    for e in econ:
        if already_seen(e["id"], cd):
            continue
        if tg(fmt_econ(e), silent=True):
            mark_seen(e["id"], cd); sent += 1
        time.sleep(0.3)

    save_cd(cd)
    print(f"\n  Надіслано: {sent} повідомлень")
    print("="*52)


if __name__ == "__main__":
    main()
