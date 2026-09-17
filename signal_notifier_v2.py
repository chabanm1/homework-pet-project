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
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from chart_analysis import detect_sweep, session_levels, render_chart

# ══════════════════════════════════════════════════════════════
# КОНФІГ — через GitHub Secrets (не хардкодь в коді!)
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

SYMBOLS = [   # топ-20 за обсягом на Kraken (USD-пари — ліквідніші за USDT там)
    "BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "ZEC/USD",
    "HYPE/USD", "ADA/USD", "TAO/USD", "UNI/USD", "DOGE/USD",
    "NEAR/USD", "SUI/USD", "XLM/USD", "LINK/USD", "XMR/USD",
    "LTC/USD", "AAVE/USD", "ARB/USD", "ENA/USD", "INJ/USD",
]
COINS_FOR_NEWS = [s.split("/")[0] for s in SYMBOLS]   # для фільтрації новин/анонсів

# Пороги
MIN_SIGNAL_STRENGTH  = 3    # 1-10, рекомендую 3
MAX_SIGNALS_PER_RUN  = 5    # топ-N за силою — щоб 20 монет не слали 20 окремих алертів
PRICE_MOVE_ALERT    = 2.5   # % за останню годину → сповіщення
NEWS_HOURS_BACK     = 1.5   # шукати новини за останні N годин
ANNOUNCE_HOURS_BACK = 24    # шукати анонси Binance за останні N годин
ECON_HOURS_AHEAD    = 24    # шукати макроподії на найближчі N годин
FUNDING_EXTREME     = 0.0004  # 0.04%/8h — за межею цього вважаємо funding екстремальним

TRACK_HOURS   = 4      # через скільки годин перевіряти результат сигналу
TRACK_WIN_PCT = 0.3    # % руху в потрібний бік, щоб зарахувати сигнал як "вцілив"
TRACK_CAP     = 300    # скільки записів історії сигналів зберігаємо

ATR_SL_MULT      = 1.5   # стоп-лосс = ATR * це
ATR_TP_MULT      = 2.5   # тейк-профіт = ATR * це (R:R ≈ 1.67)
ATR_TRIGGER_MULT = 0.5   # для середніх сигналів — на скільки ATR чекати підтвердження
STRONG_STRENGTH  = 8     # >= цього — "заходь зараз"
MEDIUM_STRENGTH  = 6     # >= цього (і < STRONG) — "чекай підтвердження"
                          # нижче MEDIUM — "не рухайся"

COOLDOWN_FILE = os.getenv("COOLDOWN_FILE", "crypto_cooldown.json")
COOLDOWN_MIN  = 120         # хвилин між однаковими сигналами
SEEN_CAP      = 500         # скільки id одноразових подій пам'ятаємо


def fmt_price(v: float) -> str:
    """Адаптивна точність — топ-20 тепер включає монети від $0.05 до $100k+,
    фіксовані 2 знаки після коми обнуляють дешеві (EMA=$0 для монети по $0.15)."""
    if v >= 1:
        return f"{v:,.2f}"
    if v >= 0.01:
        return f"{v:.4f}"
    return f"{v:.6f}"


# ══════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════
TELEGRAM_MAX_LEN = 4096

def tg(msg: str, silent: bool = False) -> bool:
    if len(msg) > TELEGRAM_MAX_LEN:
        msg = msg[:TELEGRAM_MAX_LEN - 20] + "\n… (обрізано)"
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
        if r.status_code != 200:
            print(f"TG error: HTTP {r.status_code} {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        print(f"TG error: {e}"); return False

def tg_photo(path: str, caption: str) -> bool:
    if "YOUR_BOT_TOKEN" in TELEGRAM_TOKEN:
        print(f"[DEMO TG PHOTO] {path}\n{caption}\n")
        return True
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f}, timeout=20)
        if r.status_code != 200:
            print(f"TG photo error: HTTP {r.status_code} {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        print(f"TG photo error: {e}"); return False


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
# ТРЕКІНГ ЕФЕКТИВНОСТІ СИГНАЛІВ
# ══════════════════════════════════════════════════════════════
def track_signal(sym: str, sig_type: str, price: float, cd: dict, rule: str = "OTHER"):
    """Запам'ятовує LONG/SHORT сигнал, щоб через TRACK_HOURS перевірити,
    чи ціна реально пішла в передбачений бік."""
    if sig_type not in ("LONG", "SHORT"):
        return   # MOVE/VOL не є направленою ставкою — нема що звіряти
    track = cd.setdefault("_track", [])
    track.append({
        "sym": sym, "type": sig_type, "rule": rule, "price0": price,
        "ts": time.time(), "check_at": time.time() + TRACK_HOURS * 3600,
        "resolved": False,
    })
    del track[:-TRACK_CAP]

def resolve_tracked(sym: str, price_now: float, cd: dict) -> list[dict]:
    """Дорізає результат по записах цього символу, час перевірки яких настав."""
    resolved = []
    now = time.time()
    for rec in cd.get("_track", []):
        if rec["sym"] != sym or rec.get("resolved") or rec["check_at"] > now:
            continue
        pct = (price_now - rec["price0"]) / rec["price0"] * 100
        hit = (pct >= TRACK_WIN_PCT) if rec["type"] == "LONG" else (pct <= -TRACK_WIN_PCT)
        miss = (pct <= -TRACK_WIN_PCT) if rec["type"] == "LONG" else (pct >= TRACK_WIN_PCT)
        rec["resolved"] = True
        rec["pct"]      = pct
        rec["outcome"]  = "win" if hit else ("loss" if miss else "flat")
        resolved.append(rec)
    return resolved

def signal_stats(cd: dict, days: float = 7) -> dict:
    """Win-rate по вирішених сигналах за останні `days` днів."""
    cutoff = time.time() - days * 86400
    recs = [r for r in cd.get("_track", []) if r.get("resolved") and r["ts"] >= cutoff]
    wins  = sum(1 for r in recs if r["outcome"] == "win")
    losses = sum(1 for r in recs if r["outcome"] == "loss")
    flats  = sum(1 for r in recs if r["outcome"] == "flat")
    decided = wins + losses
    return {
        "total": len(recs), "wins": wins, "losses": losses, "flats": flats,
        "win_rate": (wins / decided * 100) if decided else None,
        "avg_pct": (sum(r["pct"] for r in recs) / len(recs)) if recs else None,
    }

def signal_stats_by_rule(cd: dict, days: float = 1, min_n: int = 3) -> list[dict]:
    """Win-rate окремо по кожному типу правила (RSI_EXTREME, EMA25_BREAK, ...).
    min_n відсікає типи з надто малою вибіркою, щоб не показувати випадковий
    100%/0% на 1-2 записах як "закономірність"."""
    cutoff = time.time() - days * 86400
    recs = [r for r in cd.get("_track", []) if r.get("resolved") and r["ts"] >= cutoff]
    by_rule: dict[str, list[dict]] = {}
    for r in recs:
        by_rule.setdefault(r.get("rule", "OTHER"), []).append(r)

    out = []
    for rule, rs in by_rule.items():
        wins  = sum(1 for r in rs if r["outcome"] == "win")
        losses = sum(1 for r in rs if r["outcome"] == "loss")
        decided = wins + losses
        out.append({
            "rule": rule, "total": len(rs), "wins": wins, "losses": losses,
            "flats": len(rs) - decided,
            "win_rate": (wins / decided * 100) if decided >= min_n else None,
        })
    out.sort(key=lambda x: (x["win_rate"] is None, -(x["win_rate"] or 0)))
    return out

RULE_LEARN_DAYS  = 14   # ширше вікно, ніж денна розбивка в дайджесті — рішення "приглушити" мають спиратись на більше даних
RULE_LEARN_MIN_N = 15   # мінімум вирішених сигналів цього типу, щоб довіряти відсотку
RULE_LEARN_LOW   = 40   # win-rate нижче — тип вважаємо стабільно слабким
RULE_LEARN_HIGH  = 65   # win-rate вище — тип вважаємо стабільно сильним
RULE_LEARN_PENALTY = 3
RULE_LEARN_BONUS   = 1

def rule_win_rate(cd: dict, rule: str) -> float | None:
    """Win-rate конкретного типу сигналу за RULE_LEARN_DAYS — None, якщо
    даних замало, щоб довіряти. Використовується, щоб бот сам приглушував
    типи, які стабільно програють, замість того щоб довіряти всім однаково."""
    cutoff = time.time() - RULE_LEARN_DAYS * 86400
    recs = [r for r in cd.get("_track", [])
            if r.get("resolved") and r["ts"] >= cutoff and r.get("rule") == rule]
    wins   = sum(1 for r in recs if r["outcome"] == "win")
    losses = sum(1 for r in recs if r["outcome"] == "loss")
    decided = wins + losses
    if decided < RULE_LEARN_MIN_N:
        return None
    return wins / decided * 100


# ══════════════════════════════════════════════════════════════
# ДАНІ
# ══════════════════════════════════════════════════════════════
def _exchange():
    # Binance (HTTP 451) і Bybit (CloudFront geo-block) обидва недоступні з
    # дата-центрів GitHub Actions. Kraken — US-ліцензована біржа, звідти доступна.
    return ccxt.kraken({"enableRateLimit": True})

def _futures_exchange():
    return ccxt.krakenfutures({"enableRateLimit": True})

def fetch_ohlcv(symbol: str, tf="1h", limit=100):
    try:
        ex = _exchange()
        bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(bars, columns=["ts","open","high","low","close","vol"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df
    except Exception as e:
        print(f"  OHLCV {symbol}: {e}"); return pd.DataFrame()

def fetch_funding(symbol: str) -> float | None:
    """Funding rate безстрокового ф'ючерса (частка за 8-годинний період,
    напр. 0.0005 = 0.05%). Kraken Futures котирує в USD, не USDT."""
    try:
        base = symbol.split("/")[0]
        return _futures_exchange().fetch_funding_rate(f"{base}/USD:USD").get("fundingRate")
    except Exception as e:
        print(f"  Funding {symbol}: {e}"); return None

def fetch_trend(symbol: str, tf: str = "4h", limit: int = 100) -> str | None:
    """Напрямок старшого таймфрейму (за замовч. 4h) — 'up'/'down'/'flat',
    щоб не довіряти 1h-сигналу проти більшого тренду (як з TAO, де 1h
    RSI-перепроданість була просто епізодом у низхідному 4h-тренді)."""
    df = fetch_ohlcv(symbol, tf, limit)
    if df.empty or len(df) < 30:
        return None
    ind = indicators(df)
    if ind["e7"] > ind["e25"] > ind["e99"]:
        return "up"
    if ind["e7"] < ind["e25"] < ind["e99"]:
        return "down"
    return "flat"

def fetch_fg() -> tuple[int, str]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1",
                         timeout=8).json()
        return int(r["data"][0]["value"]), r["data"][0]["value_classification"]
    except Exception:
        return 50, "Neutral"

# CryptoPanic перейшов на платний API (мін. $50/тиждень, auth_token
# обов'язковий скрізь) — перевірено живим запитом 2026-09-16, публічний
# доступ без ключа тепер блокується Cloudflare. Замість нього — безкоштовні
# RSS-фіди, без ключа й без лімітів.
RSS_FEEDS = [
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",        "https://decrypt.co/feed"),
]

COIN_KEYWORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth"], "XRP": ["xrp", "ripple"],
    "SOL": ["solana", "sol"], "ZEC": ["zcash", "zec"], "HYPE": ["hyperliquid", "hype"],
    "ADA": ["cardano", "ada"], "TAO": ["bittensor", "tao"], "UNI": ["uniswap", "uni"],
    "DOGE": ["dogecoin", "doge"], "NEAR": ["near protocol", "near"], "SUI": ["sui"],
    "XLM": ["stellar", "xlm"], "LINK": ["chainlink", "link"], "XMR": ["monero", "xmr"],
    "LTC": ["litecoin", "ltc"], "AAVE": ["aave"], "ARB": ["arbitrum", "arb"],
    "ENA": ["ethena", "ena"], "INJ": ["injective", "inj"],
}

def _match_coins(title: str) -> list[str]:
    t = title.lower()
    return [c for c, kws in COIN_KEYWORDS.items() if any(k in t for k in kws)]

def fetch_news(hours_back: float = NEWS_HOURS_BACK) -> list[dict]:
    """Новини з безкоштовних RSS (CoinTelegraph, Decrypt). Без community-голосів
    CryptoPanic немає — score/pos/neg лишаються 0 (нейтрально), поки не
    з'явиться окремий sentiment-аналіз тексту."""
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    for source, url in RSS_FEEDS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code != 200:
                print(f"  RSS {source}: HTTP {r.status_code}")
                continue
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:20]:
                try:
                    title = (item.findtext("title") or "").strip()
                    pub = parsedate_to_datetime(item.findtext("pubDate"))
                    if pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    if pub < cutoff:
                        continue
                    results.append({
                        "title":  title,
                        "source": source,
                        "url":    (item.findtext("link") or "").strip(),
                        "pos": 0, "neg": 0, "score": 0,
                        "pub":    pub.astimezone(timezone.utc).strftime("%H:%M"),
                        "coins":  _match_coins(title),
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"  RSS {source} error: {e}")
    return sorted(results, key=lambda x: x["pub"], reverse=True)


def translate_uk(text: str) -> str:
    """Безкоштовний переклад через MyMemory (без ключа, анонімний ліміт
    ~5000 слів/день — переклад робимо лише для новин, що реально йдуть
    в повідомлення, щоб в нього вкластись). Падає тихо — повертає оригінал,
    краще англійський заголовок, ніж зламане повідомлення."""
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:500], "langpair": "en|uk"}, timeout=8)
        if r.status_code != 200:
            return text
        translated = r.json().get("responseData", {}).get("translatedText", "")
        return translated or text
    except Exception:
        return text


# Ключові слова в заголовку (англ.) -> пояснення, як це типово рухає ціну.
# Той самий підхід, що й ECON_EXPLANATIONS для макроподій: грубе правило,
# не аналіз конкретної новини, перший збіг виграє.
NEWS_IMPACT_RULES = [
    (("sec ", "cftc", "regulat", "lawsuit", " sue", "sued", "ban ", "crackdown", "investigat"),
     "Регуляторна невизначеність — часто тисне на ціну, поки не з'ясуються деталі"),
    (("hack", "exploit", "stolen", "breach", "drain", "malware", "vulnerabilit", "scam"),
     "Злом/вразливість — негативно для довіри, тиск на постраждалий токен чи протокол"),
    (("etf", "institutional", "custody", "blackrock", "fidelity", "inflow"),
     "Інституційний інтерес/приплив капіталу — зазвичай бичачий сигнал"),
    (("partnership", "integrat", "listing", "adopt", "launch", "mainnet", "upgrade"),
     "Розширення використання чи технологічний прогрес — помірно бичачий сигнал"),
    (("delist", "fraud", "outflow", "sell-off", "sell off", "dump", "liquidat", "collapse"),
     "Негативний/продажний сигнал — тиск на ціну активу"),
    (("rate cut", "dovish", "stimulus"),
     "М'якша монетарна політика — бичачо для ризикових активів, включно з криптою"),
    (("rate hike", "hawkish", "inflation"),
     "Жорсткіша монетарна політика — тиск на ризикові активи"),
]

def news_impact_explain(title: str) -> str:
    t = title.lower()
    for keywords, note in NEWS_IMPACT_RULES:
        if any(k in t for k in keywords):
            return note
    return "Прямого патерну не знайдено — новина не завжди напряму рухає ціну"


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
KYIV = ZoneInfo("Europe/Kyiv")

# (ключові слова в назві події, регістр не важливий) -> коротке пояснення
# від чого залежить напрямок руху. Перевіряються по порядку, перший збіг виграє.
ECON_EXPLANATIONS = [
    (("cpi", "pce", "inflation"),
     "Вище прогнозу → інфляція гаряча → ринок чекає жорсткішого Fed → крипта/ризик вниз. Нижче прогнозу → навпаки, вгору."),
    (("federal funds rate", "rate decision", "interest rate"),
     "Зниження ставки / м'якший тон → бичачо для крипти. Підвищення / яструбиний тон → ведмежо."),
    (("fomc",),
     "Рух залежить не від цифр, а від тону (hawkish/dovish) заяви чи прес-конференції Пауелла."),
    (("non-farm", "nfp", "employment change"),
     "Сильні дані по зайнятості → Fed може не поспішати зі зниженням ставки → тиск на крипту. Слабкі → навпаки."),
    (("unemployment rate",),
     "Вище прогнозу (гірший ринок праці) → очікування пом'якшення Fed → зазвичай бичачо для крипти."),
    (("ppi",),
     "Схоже на CPI, але про інфляцію на рівні виробників — сигнал слабший, та напрямок той самий."),
    (("retail sales",),
     "Сильні продажі → економіка гаряча → менше шансів на пом'якшення Fed → тиск на крипту."),
    (("gdp",),
     "Значно вище прогнозу → менше приводів для Fed пом'якшувати політику → тиск на ризикові активи."),
    (("pmi",),
     "Вище 50 і вище прогнозу → економіка розширюється → сильніший USD → часто тиск на крипту."),
    (("powell", "speaks", "press conference"),
     "Рух залежить від тону виступу (hawkish/dovish), а не від конкретної цифри."),
]

def econ_explain(title: str) -> str:
    t = title.lower()
    for keywords, note in ECON_EXPLANATIONS:
        if any(k in t for k in keywords):
            return note
    return "Сильне відхилення факту від прогнозу (в будь-який бік) зазвичай підсилює волатильність."

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
                title = ev.get("title", "")
                hrs_left = (when - now).total_seconds() / 3600
                in_str = f"{int(hrs_left)}г {int((hrs_left % 1) * 60)}хв" if hrs_left >= 1 else f"{int(hrs_left * 60)}хв"
                results.append({
                    "id":       f"econ_{title}_{ev['date']}",
                    "event":    title,
                    "country":  ev.get("country", ""),
                    "when":     when.astimezone(KYIV).strftime("%d.%m %H:%M") + " Київ",
                    "in":       in_str,
                    "forecast": ev.get("forecast") or None,
                    "prev":     ev.get("previous") or None,
                    "explain":  econ_explain(title),
                    "_ts":      when.timestamp(),
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  Econ calendar error: {e}")
    return sorted(results, key=lambda x: x["_ts"])


# ══════════════════════════════════════════════════════════════
# ІНДИКАТОРИ
# ══════════════════════════════════════════════════════════════
def indicators(df: pd.DataFrame) -> dict:
    c, v = df["close"], df["vol"]
    # RSI — Wilder's згладжування (EMA, alpha=1/14), стандарт TradingView/бірж.
    # Проста ковзна середня (rolling mean) давала б значення "гарячіші" за
    # реальний ринок — сильніше реагує на недавні різкі рухи, завищуючи
    # екстремуми перекупленості/перепроданості.
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
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
    # ATR(14) — для TP/SL порад
    h, l = df["high"], df["low"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    return {
        "price": float(c.iloc[-1]), "prev": float(c.iloc[-2]),
        "rsi": rsi, "macd_xu": macd_xu, "macd_xd": macd_xd,
        "e7": e7, "e25": e25, "e99": e99,
        "vs": vs, "bb": bb_pct, "mom1h": mom1h, "mom3h": mom3h, "atr": atr,
    }


# ══════════════════════════════════════════════════════════════
# СИГНАЛИ
# ══════════════════════════════════════════════════════════════
TREND_BONUS   = 1   # сигнал за трендом 4h — сила +1 (макс 10)
TREND_PENALTY = 2   # сигнал проти тренду 4h — сила -2 (мін 1), бо контр-трендові статистично слабші

BTC_CORRELATION_MOM = 1.0   # |BTC mom1h| вище цього — вважаємо рух ринковим, не монето-специфічним

def signals(ind: dict, fg: int, funding: float | None = None, htf_trend: str | None = None,
            cd: dict | None = None, btc_mom1h: float | None = None) -> list[dict]:
    p, rsi, vs, bb = ind["price"], ind["rsi"], ind["vs"], ind["bb"]
    sigs = []

    # Великий ціновий рух
    if abs(ind["mom1h"]) >= PRICE_MOVE_ALERT:
        dir_ = "🟢" if ind["mom1h"] > 0 else "🔴"
        sigs.append({"type": "MOVE", "rule": "MOVE", "strength": 8 if abs(ind["mom1h"]) > 4 else 6,
                     "text": f"{dir_} Різкий рух: {ind['mom1h']:+.1f}% за годину\n"
                             f"Ціна: ${fmt_price(p)} | Обсяг ×{vs:.1f}"})

    # Великий обсяг
    if vs >= 2.8:
        d = "↑" if ind["mom1h"] > 0 else "↓"
        sigs.append({"type": "VOL", "rule": "VOL", "strength": 7,
                     "text": f"👀 Обсяг ×{vs:.1f} від середнього {d}\n"
                             f"Великі гравці активні!"})

    # RSI extreme + об'єм
    if rsi < 28 and vs > 1.3:
        sigs.append({"type": "LONG", "rule": "RSI_EXTREME", "strength": 9,
                     "text": f"🟢 RSI={rsi:.0f} — сильна перепроданість\n"
                             f"+ підвищений обсяг ×{vs:.1f}"})
    elif rsi < 35:
        sigs.append({"type": "LONG", "rule": "RSI_MILD", "strength": 5,
                     "text": f"🟡 RSI={rsi:.0f} — перепроданість"})
    elif rsi > 72 and vs > 1.3:
        sigs.append({"type": "SHORT", "rule": "RSI_EXTREME", "strength": 8,
                     "text": f"🔴 RSI={rsi:.0f} — сильна перекупленість\n"
                             f"+ обсяг ×{vs:.1f}"})
    elif rsi > 68:
        sigs.append({"type": "SHORT", "rule": "RSI_MILD", "strength": 5,
                     "text": f"🟠 RSI={rsi:.0f} — перекупленість"})

    # MACD cross — вимагаємо підтвердження обсягом і забороняємо вхід,
    # якщо рух уже видихався (RSI близько до протилежної межі або ціна
    # вже пройшла велику відстань за останню годину) — інакше заходимо
    # на вершку/дні хвилі замість її початку.
    if ind["macd_xu"] and p > ind["e7"] and vs > 1.0 and rsi < 65 and ind["mom1h"] < 2.0:
        sigs.append({"type": "LONG", "rule": "MACD_CROSS", "strength": 7,
                     "text": f"📈 MACD Golden Cross + ціна вище EMA7\n"
                             f"Обсяг ×{vs:.1f}"})
    if ind["macd_xd"] and p < ind["e7"] and vs > 1.0 and rsi > 35 and ind["mom1h"] > -2.0:
        sigs.append({"type": "SHORT", "rule": "MACD_CROSS", "strength": 6,
                     "text": f"📉 MACD Death Cross + ціна нижче EMA7\n"
                             f"Обсяг ×{vs:.1f}"})

    # EMA25 пробій
    if ind["prev"] < ind["e25"] and p > ind["e25"] and vs > 1.2:
        sigs.append({"type": "LONG", "rule": "EMA25_BREAK", "strength": 7,
                     "text": f"🚀 Пробій EMA25 вгору! (${fmt_price(ind['e25'])})\n"
                             f"Обсяг ×{vs:.1f}"})
    if ind["prev"] > ind["e25"] and p < ind["e25"] and vs > 1.2:
        sigs.append({"type": "SHORT", "rule": "EMA25_BREAK", "strength": 7,
                     "text": f"💔 Пробій EMA25 вниз (${fmt_price(ind['e25'])})\n"
                             f"Обсяг ×{vs:.1f}"})

    # F&G extreme
    if fg < 20 and rsi < 35:
        sigs.append({"type": "LONG", "rule": "FG_EXTREME", "strength": 10,
                     "text": f"🔥 F&G={fg} (Extreme Fear) + RSI={rsi:.0f}\n"
                             f"Найсильніший contrarian сигнал!"})
    if fg > 82 and bb > 0.92:
        sigs.append({"type": "SHORT", "rule": "FG_EXTREME", "strength": 9,
                     "text": f"⚠️ F&G={fg} (Extreme Greed) + ціна у верхній BB\n"
                             f"Небезпечна ейфорія"})

    # Нижня межа BB
    if bb < 0.12 and rsi < 40:
        sigs.append({"type": "LONG", "rule": "BB_LOWER", "strength": 6,
                     "text": f"🎯 Ціна біля нижньої BB + RSI={rsi:.0f}"})

    # Екстремальний funding rate (ф'ючерси) — перегріта одна сторона ринку
    if funding is not None:
        if funding >= FUNDING_EXTREME:
            sigs.append({"type": "SHORT", "rule": "FUNDING_EXTREME", "strength": 6,
                         "text": f"💸 Funding={funding*100:.3f}%/8г — лонги переплачують\n"
                                 f"Перегрів, ризик long squeeze"})
        elif funding <= -FUNDING_EXTREME:
            sigs.append({"type": "LONG", "rule": "FUNDING_EXTREME", "strength": 6,
                         "text": f"💰 Funding={funding*100:.3f}%/8г — шорти переплачують\n"
                                 f"Перегрів у шортах, contrarian LONG"})

    # Поправка на тренд старшого таймфрейму (4h) — TAO-урок: 1h RSI-відскок
    # проти більшого тренду частіше програє, ніж вигравав у нашому трекінгу
    if htf_trend in ("up", "down"):
        for s in sigs:
            if s["type"] not in ("LONG", "SHORT"):
                continue
            aligned = (s["type"] == "LONG" and htf_trend == "up") or \
                      (s["type"] == "SHORT" and htf_trend == "down")
            if aligned:
                s["strength"] = min(10, s["strength"] + TREND_BONUS)
                s["text"] += "\n✅ За трендом 4h"
            else:
                s["strength"] = max(1, s["strength"] - TREND_PENALTY)
                s["text"] += "\n⚠️ Проти тренду 4h — обережно"

    # Навчання на власній історії: типи сигналів, що стабільно програють
    # (RULE_LEARN_MIN_N+ вирішених за RULE_LEARN_DAYS днів) — приглушуємо;
    # стабільно виграшні — трохи підсилюємо. Це і є "бот вчиться на помилках".
    if cd is not None:
        for s in sigs:
            if s["type"] not in ("LONG", "SHORT"):
                continue
            wr = rule_win_rate(cd, s.get("rule", "OTHER"))
            if wr is None:
                continue
            if wr < RULE_LEARN_LOW:
                s["strength"] = max(1, s["strength"] - RULE_LEARN_PENALTY)
                s["text"] += f"\n📉 Цей тип історично слабкий (win-rate {wr:.0f}% за {RULE_LEARN_DAYS}д)"
            elif wr > RULE_LEARN_HIGH:
                s["strength"] = min(10, s["strength"] + RULE_LEARN_BONUS)
                s["text"] += f"\n📈 Цей тип історично сильний (win-rate {wr:.0f}% за {RULE_LEARN_DAYS}д)"

    # Кореляція з BTC — попереджаємо, що це може бути загальноринковий рух,
    # а не унікальна можливість саме в цій монеті
    if btc_mom1h is not None and abs(btc_mom1h) >= BTC_CORRELATION_MOM:
        for s in sigs:
            if s["type"] not in ("LONG", "SHORT"):
                continue
            same_dir = (s["type"] == "LONG" and btc_mom1h > 0) or (s["type"] == "SHORT" and btc_mom1h < 0)
            if same_dir:
                s["text"] += f"\n🌊 BTC теж рухається так само ({btc_mom1h:+.1f}%/1h) — можливо, це ринковий рух, не унікальний сетап"

    return sigs


# ══════════════════════════════════════════════════════════════
# ФОРМАТУВАННЯ ПОВІДОМЛЕНЬ
# ══════════════════════════════════════════════════════════════
def tp_sl(price: float, atr: float, sig_type: str) -> tuple[float, float]:
    """SL/TP на основі ATR(14). Груба евристика, не фінансова порада."""
    if sig_type == "LONG":
        return price - ATR_SL_MULT * atr, price + ATR_TP_MULT * atr
    return price + ATR_SL_MULT * atr, price - ATR_TP_MULT * atr

def advice_block(sig: dict, ind: dict) -> str:
    """Порада за силою сигналу: сильний → заходь зараз з TP/SL,
    середній → чекай підтвердження на рівні, слабкий → не рухайся."""
    if sig["type"] not in ("LONG", "SHORT") or not ind.get("atr"):
        return ""
    price, atr = ind["price"], ind["atr"]
    action = "BUY / LONG" if sig["type"] == "LONG" else "SELL / SHORT"
    strength = sig["strength"]

    if strength >= STRONG_STRENGTH:
        sl, tp = tp_sl(price, atr, sig["type"])
        return (
            f"\n🎯 <b>ПОРАДА: СИЛЬНИЙ — заходь зараз {action}</b>\n"
            f"SL: ${fmt_price(sl)} | TP: ${fmt_price(tp)}"
        )
    if strength >= MEDIUM_STRENGTH:
        trig = price + ATR_TRIGGER_MULT * atr if sig["type"] == "LONG" else price - ATR_TRIGGER_MULT * atr
        sl, tp = tp_sl(trig, atr, sig["type"])
        move = "підніметься" if sig["type"] == "LONG" else "опуститься"
        return (
            f"\n🟡 <b>ПОРАДА: СЕРЕДНІЙ — чекай підтвердження</b>\n"
            f"Якщо ціна {move} до ${fmt_price(trig)} → {action}\n"
            f"SL: ${fmt_price(sl)} | TP: ${fmt_price(tp)}"
        )
    return "\n⚪ <b>ПОРАДА: СЛАБКИЙ — краще не рухатись, просто тримай на радарі</b>"

def compact_advice(sig: dict, ind: dict) -> str:
    """Однорядкова версія advice_block — для дайджесту з багатьма монетами."""
    if sig["type"] not in ("LONG", "SHORT") or not ind.get("atr"):
        return ""
    price, atr = ind["price"], ind["atr"]
    action = "BUY" if sig["type"] == "LONG" else "SELL"
    strength = sig["strength"]
    if strength >= STRONG_STRENGTH:
        sl, tp = tp_sl(price, atr, sig["type"])
        return f"   → {action} зараз | SL ${fmt_price(sl)} TP ${fmt_price(tp)}"
    if strength >= MEDIUM_STRENGTH:
        trig = price + ATR_TRIGGER_MULT * atr if sig["type"] == "LONG" else price - ATR_TRIGGER_MULT * atr
        return f"   → чекай ${fmt_price(trig)} → {action}"
    return "   → краще не рухайся"

def nearby_econ_note(econ: list[dict], hours: float = 8) -> str:
    """Коротке нагадування про макроподію, якщо вона зовсім скоро —
    щоб не ставити SL/TP прямо перед FOMC чи NFP наосліп."""
    if not econ:
        return ""
    soon = econ[:2]   # econ вже відсортований за часом
    lines = ["\n📅 <b>Скоро:</b>"] + [f"  🕐 {e['when']} (через {e['in']}) {e['country']} — {e['event']}" for e in soon]
    return "\n".join(lines)

def fmt_signal(symbol: str, ind: dict, fg: int, fg_cls: str,
               sig: dict, econ: list[dict] | None = None) -> str:
    t = datetime.now().strftime("%H:%M")
    e = {"LONG":"🟢","SHORT":"🔴","MOVE":"⚡","VOL":"👀"}.get(sig["type"],"📊")
    return (
        f"{e} <b>{symbol} — {sig['type']} ({t})</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{sig['text']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Ціна: <b>${fmt_price(ind['price'])}</b> ({ind['mom1h']:+.1f}%/1h)\n"
        f"📊 RSI={ind['rsi']:.0f} | F&G={fg} {fg_cls}\n"
        f"📉 EMA7=${fmt_price(ind['e7'])} | EMA25=${fmt_price(ind['e25'])}\n"
        f"📦 Обсяг ×{ind['vs']:.1f}\n"
        f"━━━━━━━━━━━━━━━━"
        f"{advice_block(sig, ind)}"
        f"{nearby_econ_note(econ or [])}\n"
        f"⚠️ Евристика на основі ATR, не фінансова порада"
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
        title_uk = translate_uk(n["title"])
        lines.append(f"{sent} [{n['pub']}] <b>{n['source']}</b>")
        lines.append(f"   {title_uk[:140]}")
        if coins:
            lines.append(f"   {coins}")
        lines.append(f"   💡 {news_impact_explain(n['title'])}")
        lines.append("")
    lines.append("📱 Джерела: CoinTelegraph, Decrypt")
    return "\n".join(lines)

def fmt_sweep_caption(symbol: str, sweep: dict, ind: dict) -> str:
    t = datetime.now().strftime("%H:%M")
    e = "🟢" if sweep["type"] == "LONG" else "🔴"
    return (
        f"{e} <b>{symbol} — LIQUIDITY SWEEP ({t})</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{sweep['text'].capitalize()}\n"
        f"Рівень: ${fmt_price(sweep['level'])} | Фітиль {sweep['wick_pct']*100:.0f}% свічки\n"
        f"💰 Зараз: ${fmt_price(ind['price'])}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ Патерн, не гарантія — перевіряємо на реальних даних, чи дає edge"
    )

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
        f"🕐 {e['when']} (через {e['in']})"
    )
    if e.get("forecast") is not None:
        ev += f"\n📊 Прогноз: {e['forecast']}"
    if e.get("prev") is not None:
        ev += f" | Попередній: {e['prev']}"
    ev += f"\n💡 {e['explain']}"
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

    econ = fetch_econ_calendar()   # фетчимо раз — і для алертів (4), і для нотаток у сигналах
    print(f"  Econ calendar: {len(econ)} high-impact подій")

    sent = 0

    # ── 1. Технічні сигнали — збираємо кандидатів з усіх монет,───
    #      потім шлемо тільки топ-N найсильніших одним прогоном,
    #      а не окреме повідомлення на кожну з 20 монет.
    candidates = []
    sweep_hits = []   # окремо від candidates — не змагаються за топ-5, свій ліміт/cooldown
    btc_mom1h  = None   # BTC/USD іде першим у SYMBOLS — заповнюється в першій ітерації
    for sym in SYMBOLS:
        print(f"\n  {sym}...", end=" ")
        df = fetch_ohlcv(sym, "1h", 100)
        if df.empty:
            print("! немає даних"); continue

        ind       = indicators(df)
        funding   = fetch_funding(sym)
        htf_trend = fetch_trend(sym)
        if sym == "BTC/USD":
            btc_mom1h = ind["mom1h"]

        sweep = detect_sweep(df)
        if sweep:
            sweep_hits.append({"sym": sym, "df": df, "ind": ind, "sweep": sweep})

        resolved = resolve_tracked(sym, ind["price"], cd)
        for r in resolved:
            e = {"win": "✅", "loss": "❌", "flat": "➖"}[r["outcome"]]
            print(f"  [track] {r['type']} {sym} {r['pct']:+.2f}% {e}")

        sigs = [s for s in signals(ind, fg, funding, htf_trend, cd, None if sym == "BTC/USD" else btc_mom1h)
                if s["strength"] >= MIN_SIGNAL_STRENGTH]

        if not sigs:
            print(f"сигналів немає (RSI={ind['rsi']:.0f} mov={ind['mom1h']:+.1f}%)")
            continue

        best = max(sigs, key=lambda x: x["strength"])
        key  = f"{sym}_{best['type']}"
        print(f"СИГНАЛ {best['type']} сила={best['strength']}")

        if not ok_to_send(key, cd):
            print(f"  → cooldown активний"); continue

        candidates.append({"sym": sym, "ind": ind, "best": best, "key": key})

    candidates.sort(key=lambda c: c["best"]["strength"], reverse=True)
    top = candidates[:MAX_SIGNALS_PER_RUN]
    if len(candidates) > len(top):
        print(f"\n  {len(candidates) - len(top)} сигналів не потрапили в топ-{MAX_SIGNALS_PER_RUN}, пропущено цей прогін")

    for c in top:
        msg = fmt_signal(c["sym"], c["ind"], fg, fg_cls, c["best"], econ)
        if tg(msg):
            mark_sent(c["key"], cd)
            track_signal(c["sym"], c["best"]["type"], c["ind"]["price"], cd, c["best"].get("rule", "OTHER"))
            sent += 1
        time.sleep(0.3)

    # ── 1.5 Liquidity sweep — окремі повідомлення з картинкою ──
    print(f"\n  Liquidity sweeps: {len(sweep_hits)} знайдено")
    for hit in sweep_hits[:MAX_SIGNALS_PER_RUN]:
        sym, sweep, ind = hit["sym"], hit["sweep"], hit["ind"]
        key = f"{sym}_SWEEP"
        if not ok_to_send(key, cd):
            print(f"  {sym} sweep → cooldown активний"); continue
        try:
            chart_df = fetch_ohlcv(sym, "1h", 200)   # ширший контекст лише для монет зі sweep
            if chart_df.empty:
                chart_df = hit["df"]
            levels = session_levels(chart_df)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                render_chart(chart_df, sym, sweep, levels, tf.name)
                if tg_photo(tf.name, fmt_sweep_caption(sym, sweep, ind)):
                    mark_sent(key, cd)
                    track_signal(sym, sweep["type"], ind["price"], cd, "LIQUIDITY_SWEEP")
                    sent += 1
            os.unlink(tf.name)
        except Exception as e:
            print(f"  {sym} sweep chart error: {e}")
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

    # ── 4. Макрокалендар — окремі одноразові алерти ─────────
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
