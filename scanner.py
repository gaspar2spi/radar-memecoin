#!/usr/bin/env python3
"""
Radar Memecoin — scanner automatique de signaux narratifs (focus Solana).

Philosophie : on avale un volume large de contenu (Reddit sur des dizaines de
communautés, actualité par grand thème, recherches en forte hausse), et c'est
le SCORE qui fait le tri. On ne présuppose pas d'où viendra le prochain
$CYBERLEEK — on regarde large, et on ne remonte/alerte que ce qui dépasse un
seuil.

Sources utilisées (toutes publiques, gratuites, sans clé API payante) :
  - Reddit  : ~25 communautés, listings "hot" ET "new" (fraîcheur + volume)
  - Google News : 8 grandes rubriques + ~15 recherches ciblées
  - Google Trends : recherches en forte hausse (France + États-Unis)
  - pump.fun : nouveaux tokens Solana déjà en mouvement

Conçu pour tourner via GitHub Actions (cron), avec un throttle raisonnable
pour ne pas se faire bloquer par les sources publiques.
"""

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

USER_AGENT = "radar-memecoin-scanner/2.0"
REQUEST_DELAY = 0.6  # secondes entre deux requêtes, pour rester poli

# Large éventail de communautés Reddit : crypto, mais aussi actu, pop culture,
# gaming, politique, sport — parce qu'un narratif memecoin peut naître
# n'importe où (voir $CYBERLEEK, parti d'un leak gaming, pas de la crypto).
REDDIT_SUBS = [
    "CryptoMoonShots", "SatoshiStreetBets", "solana", "CryptoCurrency",
    "SolanaMemeCoins", "dogecoin", "wallstreetbets",
    "worldnews", "news", "politics", "PoliticalHumor", "geopolitics",
    "technology", "gaming", "GamingLeaksAndRumours", "Games",
    "movies", "television", "entertainment", "popculturechat",
    "memes", "dankmemes", "PublicFreakout", "nottheonion", "out_of_the_loop",
    "formula1", "nba", "soccer",
]

# Rubriques Google News (codes officiels des sections)
NEWS_TOPICS = {
    "WORLD": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB",
    "NATION": "CAAqIggKIhxDQkFTRHdvSUwyMHZNRGRqTVhZU0FtVnVLQUFQAQ",
    "BUSINESS": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB",
    "TECHNOLOGY": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB",
    "ENTERTAINMENT": "CAAqJggKIiBDQkFTRWdvSUwyMHZNREprY0hRU0FtVnVHZ0pWVXlnQVAB",
    "SPORTS": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB",
    "SCIENCE": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FtVnVHZ0pWVXlnQVAB",
    "HEALTH": "CAAqIQgKIhtDQkFTRGdvSUwyMHZNR3QwTlRFU0FtVnVLQUFQAQ",
}

# Recherches Google News ciblées, en plus des rubriques générales
NEWS_QUERIES = [
    "memecoin launch", "crypto coin viral", "leaked footage internet",
    "celebrity crypto token", "own cryptocurrency launch", "rug pull crypto",
    "viral scandal internet", "AI controversy viral", "internet drama trending",
    "election crypto", "government shutdown", "central bank announcement",
    "billionaire announcement", "tech ceo controversy", "gaming leak",
]

# Flux "recherches en forte hausse" Google Trends
TRENDS_GEO = ["US", "FR"]

# Figures dont l'implication fait automatiquement grimper le score.
# Étoffe librement ces listes selon ce que tu observes dans le temps.
TIER_10_NAMES = [
    "trump", "elon musk", "kanye", " ye ", "melania", "javier milei",
    "vladimir putin", "xi jinping",
]
TIER_8_NAMES = [
    "mrbeast", "kim kardashian", "andrew tate", "logan paul", "jake paul",
    "sam altman", "mark zuckerberg", "jeff bezos", "taylor swift",
    "kylie jenner", "pewdiepie", "rockstar games", "nintendo", "openai",
]

VIRALITY_KEYWORDS = [
    "leak", "leaked", "hack", "hacked", "scandal", "banned", "viral",
    "breaking", "exposed", "controversy", "rage", "outrage", "meltdown",
    "fired", "resigns", "arrested", "lawsuit", "record-breaking",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "7"))
MIN_KEEP_SCORE = int(os.environ.get("MIN_KEEP_SCORE", "3"))

OUTPUT_FILE = "signals.json"
MAX_SIGNALS_KEPT = 500


# ---------------------------------------------------------------------------
# UTILITAIRES RÉSEAU
# ---------------------------------------------------------------------------

def fetch_json(url, headers=None):
    time.sleep(REQUEST_DELAY)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_text(url, headers=None):
    time.sleep(REQUEST_DELAY)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# SCORING — c'est ce filtre qui fait tout le travail
# ---------------------------------------------------------------------------

def score_text(title, body=""):
    """
    Grille 1-10 :
      10  Figure politique/célébrité mondiale (Trump-tier) + lancement
          explicite de son propre token/crypto.
      8-9 Même figure mentionnée en contexte crypto sans lancement confirmé,
          ou scandale mondial massif avec récupération crypto déjà en cours.
      6-7 Événement viral fort (leak, scandale, actu choc) + indices de
          récupération memecoin (mots "coin", "token", "$").
      4-5 Tendance qui buzz dans des cercles ciblés mais sans portée massive.
      1-3 Signal faible ou bruit de fond — c'est la grande majorité du flux
          brut, et c'est normal : le but est de le laisser de côté.
    """
    text = f"{title} {body}".lower()
    score = 2
    reasons = []

    for name in TIER_10_NAMES:
        if name.strip() in text:
            has_launch_words = any(w in text for w in [
                "launch", "launches", "launched", "lance", "own coin",
                "own token", "unveils coin", "unveils token",
            ])
            if has_launch_words:
                score = max(score, 10)
                reasons.append(f"figure tier-10 ({name.strip()}) + lancement explicite")
            else:
                score = max(score, 8)
                reasons.append(f"figure tier-10 ({name.strip()}) mentionnée")

    for name in TIER_8_NAMES:
        if name in text:
            score = max(score, 7)
            reasons.append(f"figure/entité tier-8 ({name}) mentionnée")

    viral_hits = [w for w in VIRALITY_KEYWORDS if w in text]
    if viral_hits:
        score = max(score, 4 + min(len(viral_hits), 3))
        reasons.append(f"mots-clés viraux: {', '.join(viral_hits[:3])}")

    if re.search(r"\$[a-zA-Z]{2,10}\b", text) or "memecoin" in text or "token" in text:
        score = min(10, score + 1)
        reasons.append("référence directe à un token/ticker")

    score = max(1, min(10, score))
    return score, "; ".join(reasons) if reasons else "signal faible / bruit de fond"


# ---------------------------------------------------------------------------
# SOURCES
# ---------------------------------------------------------------------------

def scan_reddit():
    signals = []
    for sub in REDDIT_SUBS:
        for listing in ("hot", "new"):
            try:
                url = f"https://www.reddit.com/r/{sub}/{listing}.json?limit=20"
                data = fetch_json(url)
                for post in data.get("data", {}).get("children", []):
                    p = post.get("data", {})
                    title = p.get("title", "")
                    score_val, reason = score_text(title, p.get("selftext", ""))
                    if score_val < MIN_KEEP_SCORE:
                        continue
                    signals.append({
                        "id": f"reddit_{p.get('id')}",
                        "title": title,
                        "cat": "momentum" if sub.lower() in ("solana", "cryptomoonshots", "solanamemecoins") else "viral",
                        "heat": score_val,
                        "desc": f"Détecté sur r/{sub} ({p.get('ups', 0)} upvotes). {reason}.",
                        "url": f"https://reddit.com{p.get('permalink', '')}",
                        "ticker": "",
                        "source": "reddit",
                        "ts": int(time.time() * 1000),
                    })
            except Exception as e:
                print(f"[reddit] erreur sur r/{sub}/{listing}: {e}")
    return signals


def scan_pumpfun():
    signals = []
    try:
        url = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false"
        data = fetch_json(url)
        for coin in data if isinstance(data, list) else []:
            name = coin.get("name", "")
            symbol = coin.get("symbol", "")
            desc = coin.get("description", "") or ""
            score_val, reason = score_text(f"{name} {symbol}", desc)
            market_cap = coin.get("usd_market_cap", 0) or 0
            if market_cap and market_cap > 300000:
                score_val = min(10, score_val + 1)
                reason += "; market cap déjà significative"
            if score_val < MIN_KEEP_SCORE:
                continue
            signals.append({
                "id": f"pumpfun_{coin.get('mint', symbol)}",
                "title": f"{name} (${symbol})",
                "cat": "momentum",
                "heat": score_val,
                "desc": f"Repéré sur pump.fun, market cap ~${int(market_cap):,}. {reason}.",
                "url": f"https://pump.fun/{coin.get('mint', '')}",
                "ticker": f"${symbol}",
                "source": "pumpfun",
                "ts": int(time.time() * 1000),
            })
    except Exception as e:
        print(f"[pumpfun] erreur: {e}")
    return signals


def _parse_news_rss(xml_text, label):
    signals = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[news] flux illisible ({label}): {e}")
        return signals
    for item in root.findall(".//item")[:25]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        score_val, reason = score_text(title)
        if score_val < MIN_KEEP_SCORE:
            continue
        item_id = re.sub(r"\W+", "", title)[:40]
        signals.append({
            "id": f"news_{label}_{item_id}",
            "title": title,
            "cat": "macro",
            "heat": score_val,
            "desc": f"Actu détectée ({label}). {reason}.",
            "url": link,
            "ticker": "",
            "source": "news",
            "ts": int(time.time() * 1000),
        })
    return signals


def scan_news():
    signals = []
    for topic_name, topic_code in NEWS_TOPICS.items():
        try:
            url = f"https://news.google.com/rss/headlines/section/topic/{topic_name}?hl=en-US&gl=US&ceid=US:en"
            signals += _parse_news_rss(fetch_text(url), topic_name)
        except Exception as e:
            print(f"[news] erreur sur rubrique {topic_name}: {e}")

    for query in NEWS_QUERIES:
        try:
            q = query.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
            signals += _parse_news_rss(fetch_text(url), query)
        except Exception as e:
            print(f"[news] erreur sur recherche '{query}': {e}")
    return signals


def scan_trends():
    signals = []
    for geo in TRENDS_GEO:
        try:
            url = f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}"
            xml_text = fetch_text(url)
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item")[:20]:
                title = item.findtext("title") or ""
                score_val, reason = score_text(title)
                if score_val < MIN_KEEP_SCORE:
                    continue
                item_id = re.sub(r"\W+", "", title)[:40]
                signals.append({
                    "id": f"trends_{geo}_{item_id}",
                    "title": title,
                    "cat": "viral",
                    "heat": score_val,
                    "desc": f"Recherche en forte hausse sur Google ({geo}). {reason}.",
                    "url": f"https://trends.google.com/trends/explore?geo={geo}&q={title.replace(' ', '+')}",
                    "ticker": "",
                    "source": "trends",
                    "ts": int(time.time() * 1000),
                })
        except Exception as e:
            print(f"[trends] erreur sur {geo}: {e}")
    return signals


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram_alert(signal):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = (
        f"Signal {signal['heat']}/10 — {signal['title']}\n"
        f"{signal['desc']}\n"
        f"{signal.get('url', '')}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[telegram] erreur d'envoi: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    start = time.time()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Lancement du scan large...")

    all_signals = []
    all_signals += scan_reddit()
    all_signals += scan_pumpfun()
    all_signals += scan_news()
    all_signals += scan_trends()

    print(f"Volume brut retenu après premier filtre de score: {len(all_signals)} signaux "
          f"(seuil de conservation = {MIN_KEEP_SCORE}/10)")

    existing = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f).get("signals", [])
        except Exception:
            existing = []

    existing_ids = {s["id"] for s in existing}
    new_ones = [s for s in all_signals if s["id"] not in existing_ids]

    seen = set()
    deduped = []
    for s in new_ones:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        deduped.append(s)
    new_ones = deduped

    for s in new_ones:
        if s["heat"] >= ALERT_THRESHOLD:
            send_telegram_alert(s)

    merged = new_ones + existing
    merged.sort(key=lambda s: (s["heat"], s["ts"]), reverse=True)
    merged = merged[:MAX_SIGNALS_KEPT]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "signals": merged,
        }, f, ensure_ascii=False, indent=2)

    duration = round(time.time() - start, 1)
    alerted = len([s for s in new_ones if s["heat"] >= ALERT_THRESHOLD])
    print(f"Scan terminé en {duration}s : {len(new_ones)} nouveaux signaux retenus, "
          f"{alerted} ont déclenché une alerte, {len(merged)} conservés au total.")


if __name__ == "__main__":
    main()
