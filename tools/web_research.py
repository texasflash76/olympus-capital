import json
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

import feedparser


CACHE_PATH = Path("company_profile_cache.json")
CACHE_TTL_HOURS = 24


SIC_SECTOR_KEYWORDS = {
    "Technology": [
        "semiconductors",
        "computer",
        "software",
        "data processing",
        "electronic",
        "communications equipment",
        "internet",
        "information retrieval",
    ],
    "Communication Services": [
        "television",
        "radio",
        "broadcasting",
        "cable",
        "telecommunications",
        "telephone",
        "motion picture",
    ],
    "Consumer Cyclical": [
        "retail",
        "automotive",
        "motor vehicles",
        "restaurants",
        "hotels",
        "apparel",
        "furniture",
        "recreational",
    ],
    "Consumer Defensive": [
        "food",
        "beverages",
        "grocery",
        "tobacco",
        "household",
        "soap",
        "agriculture",
    ],
    "Financial Services": [
        "bank",
        "insurance",
        "investment",
        "broker",
        "credit",
        "finance",
        "security brokers",
    ],
    "Healthcare": [
        "pharmaceutical",
        "biological",
        "medical",
        "health",
        "surgical",
        "diagnostic",
    ],
    "Energy": [
        "oil",
        "gas",
        "petroleum",
        "coal",
        "drilling",
        "pipeline",
    ],
    "Industrials": [
        "machinery",
        "aerospace",
        "aircraft",
        "transportation",
        "electrical equipment",
        "construction",
        "manufacturing",
    ],
    "Materials": [
        "chemicals",
        "mining",
        "steel",
        "paper",
        "metals",
        "plastic",
        "lumber",
    ],
    "Real Estate": [
        "real estate",
        "reit",
    ],
    "Utilities": [
        "electric",
        "utility",
        "water supply",
        "natural gas transmission",
    ],
}


SECTOR_ALIASES = {
    "Information Technology": "Technology",
    "Technology": "Technology",
    "Communication Services": "Communication Services",
    "Consumer Cyclical": "Consumer Cyclical",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Defensive": "Consumer Defensive",
    "Consumer Staples": "Consumer Defensive",
    "Financial Services": "Financial Services",
    "Financials": "Financial Services",
    "Healthcare": "Healthcare",
    "Health Care": "Healthcare",
    "Energy": "Energy",
    "Industrials": "Industrials",
    "Basic Materials": "Materials",
    "Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_cache():
    if not CACHE_PATH.exists():
        return {}

    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def cache_is_fresh(cached_item):
    fetched_at = cached_item.get("fetched_at")

    if not fetched_at:
        return False

    try:
        fetched_time = datetime.fromisoformat(fetched_at)
        return datetime.now(timezone.utc) - fetched_time < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


def normalize_sector(sector):
    if not sector:
        return "Unknown"

    sector = str(sector).strip()
    return SECTOR_ALIASES.get(sector, sector)


def fetch_json(url, timeout=15, sec=False):
    headers = {
        "User-Agent": "OlympusCapital/1.0 philiptudor@gmail.com",
        "Accept": "application/json,text/plain,*/*",
    }

    if not sec:
        headers["User-Agent"] = "Mozilla/5.0 OlympusCapital/1.0"

    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")

    return json.loads(raw)


def sector_from_sic_description(sic_description):
    text = str(sic_description or "").lower()

    for sector, keywords in SIC_SECTOR_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return sector

    return "Unknown"


def get_sec_company_ticker_map():
    url = "https://www.sec.gov/files/company_tickers.json"
    data = fetch_json(url, sec=True)

    ticker_map = {}

    for _, item in data.items():
        ticker = str(item.get("ticker", "")).upper().strip()
        cik = str(item.get("cik_str", "")).strip()
        title = str(item.get("title", "")).strip()

        if ticker and cik:
            ticker_map[ticker] = {
                "cik": cik.zfill(10),
                "company_name": title,
            }

    return ticker_map


def get_sec_company_profile(ticker):
    ticker = ticker.upper().strip()

    ticker_map = get_sec_company_ticker_map()
    sec_item = ticker_map.get(ticker)

    if not sec_item:
        return None

    cik = sec_item["cik"]
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = fetch_json(submissions_url, sec=True)

    sic_description = data.get("sicDescription", "") or ""
    sic = data.get("sic", "") or ""

    sector = sector_from_sic_description(sic_description)

    return {
        "ticker": ticker,
        "company_name": data.get("name") or sec_item.get("company_name") or ticker,
        "sector": sector,
        "industry": sic_description or "Unknown",
        "website": "",
        "source": "SEC submissions API",
        "profile_url": f"https://www.sec.gov/edgar/browse/?CIK={cik}",
        "sic": sic,
        "sic_description": sic_description,
        "fetched_at": now_iso(),
        "error": None,
    }


def get_yahoo_quote_summary(ticker):
    ticker = ticker.upper().strip()
    encoded = urllib.parse.quote(ticker)

    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{encoded}"
        "?modules=assetProfile,price,summaryProfile"
    )

    data = fetch_json(url)
    result = data.get("quoteSummary", {}).get("result", [])

    if not result:
        return {}

    return result[0]


def get_yahoo_company_profile(ticker):
    ticker = ticker.upper().strip()

    data = get_yahoo_quote_summary(ticker)

    asset_profile = data.get("assetProfile", {}) or {}
    summary_profile = data.get("summaryProfile", {}) or {}
    price = data.get("price", {}) or {}

    sector = (
        asset_profile.get("sector")
        or summary_profile.get("sector")
        or "Unknown"
    )

    industry = (
        asset_profile.get("industry")
        or summary_profile.get("industry")
        or "Unknown"
    )

    company_name = (
        price.get("longName")
        or price.get("shortName")
        or ticker
    )

    website = (
        asset_profile.get("website")
        or summary_profile.get("website")
        or ""
    )

    return {
        "ticker": ticker,
        "company_name": company_name,
        "sector": normalize_sector(sector),
        "industry": industry,
        "website": website,
        "source": "Yahoo Finance quoteSummary",
        "profile_url": f"https://finance.yahoo.com/quote/{ticker}/profile",
        "fetched_at": now_iso(),
        "error": None,
    }


def get_company_profile(ticker, use_cache=True):
    """
    Returns company profile metadata.

    Priority:
    1. Local cache
    2. SEC submissions API
    3. Yahoo Finance fallback

    SEC is preferred because Yahoo quoteSummary often returns 401 Unauthorized
    from VPS/server environments.
    """
    ticker = ticker.upper().strip()

    cache = load_cache()

    if use_cache and ticker in cache and cache_is_fresh(cache[ticker]):
        cached_profile = cache[ticker].get("profile", {})
        cached_sector = cached_profile.get("sector", "Unknown")

        if cached_sector and cached_sector != "Unknown":
            return cached_profile

    errors = []

    try:
        profile = get_sec_company_profile(ticker)

        if profile and profile.get("sector") and profile.get("sector") != "Unknown":
            cache[ticker] = {
                "fetched_at": now_iso(),
                "profile": profile,
            }
            save_cache(cache)
            return profile

        errors.append("SEC returned no usable sector.")

    except Exception as e:
        errors.append(f"SEC failed: {e}")

    try:
        profile = get_yahoo_company_profile(ticker)

        if profile.get("sector") and profile.get("sector") != "Unknown":
            if errors:
                profile["error"] = " | ".join(errors)

            cache[ticker] = {
                "fetched_at": now_iso(),
                "profile": profile,
            }
            save_cache(cache)
            return profile

        errors.append("Yahoo returned Unknown sector.")

    except Exception as e:
        errors.append(f"Yahoo failed: {e}")

    profile = {
        "ticker": ticker,
        "company_name": ticker,
        "sector": "Unknown",
        "industry": "Unknown",
        "website": "",
        "source": "No profile source succeeded",
        "profile_url": "",
        "fetched_at": now_iso(),
        "error": " | ".join(errors),
    }

    cache[ticker] = {
        "fetched_at": now_iso(),
        "profile": profile,
    }
    save_cache(cache)

    return profile


def get_sector_for_ticker(ticker):
    profile = get_company_profile(ticker)
    return profile.get("sector", "Unknown") or "Unknown"


def fetch_rss(url, limit=5):
    try:
        feed = feedparser.parse(url)
        results = []

        for entry in feed.entries[:limit]:
            results.append({
                "title": getattr(entry, "title", ""),
                "link": getattr(entry, "link", ""),
                "published": getattr(entry, "published", ""),
                "source": url,
            })

        return results

    except Exception as e:
        return [{
            "title": f"RSS fetch failed: {e}",
            "link": "",
            "published": "",
            "source": url,
        }]


def get_yahoo_news(ticker, limit=5):
    ticker = ticker.upper().strip()
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    return fetch_rss(url, limit)


def get_google_news(query, limit=5):
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    return fetch_rss(url, limit)


def dedupe_news(items):
    seen = set()
    clean = []

    for item in items:
        title = str(item.get("title", "")).strip()

        if not title:
            continue

        key = title.lower()

        if key in seen:
            continue

        seen.add(key)
        clean.append(item)

    return clean


def estimate_headline_sentiment(items):
    positive_words = [
        "rally", "gains", "surge", "beats", "upgrade", "optimism",
        "growth", "strong", "record", "bullish", "rebound", "jump",
        "tops", "outperform",
    ]

    negative_words = [
        "falls", "drop", "plunge", "miss", "downgrade", "fear",
        "risk", "weak", "selloff", "bearish", "slump", "cuts",
        "warning", "lawsuit", "probe", "recession",
    ]

    positive = 0
    negative = 0

    for item in items:
        title = str(item.get("title", "")).lower()

        if any(word in title for word in positive_words):
            positive += 1

        if any(word in title for word in negative_words):
            negative += 1

    if positive > negative + 1:
        label = "positive"
    elif negative > positive + 1:
        label = "negative"
    else:
        label = "mixed"

    return {
        "label": label,
        "positive_headline_count": positive,
        "negative_headline_count": negative,
        "total_headlines_checked": len(items),
    }


def get_market_context(ticker, sector="Unknown", limit=5):
    ticker = ticker.upper().strip()

    broad_market_queries = [
        "stock market today S&P 500 Nasdaq Dow inflation Fed yields",
        "US market sentiment today stocks investors",
        "Wall Street today market rally selloff rates earnings",
    ]

    sector_queries = []

    if sector and sector != "Unknown":
        sector_queries.append(f"{sector} sector stocks news today")
        sector_queries.append(f"{sector} earnings guidance stocks")

    broad_market_news = []

    for query in broad_market_queries:
        broad_market_news.extend(get_google_news(query, limit=limit))

    sector_news = []

    for query in sector_queries:
        sector_news.extend(get_google_news(query, limit=limit))

    broad_market_news = dedupe_news(broad_market_news)[:limit * 2]
    sector_news = dedupe_news(sector_news)[:limit * 2]

    all_context_news = broad_market_news + sector_news

    return {
        "generated_at": now_iso(),
        "market_sentiment_heuristic": estimate_headline_sentiment(all_context_news),
        "broad_market_news": broad_market_news,
        "sector_news": sector_news,
    }


def get_web_research(ticker, limit=5):
    ticker = ticker.upper().strip()

    company_profile = get_company_profile(ticker)
    sector = company_profile.get("sector", "Unknown")

    yahoo_news = get_yahoo_news(ticker, limit)
    google_news = get_google_news(f"{ticker} stock news", limit)
    company_news = dedupe_news(yahoo_news + google_news)[:limit * 2]

    market_context = get_market_context(
        ticker=ticker,
        sector=sector,
        limit=limit,
    )

    return {
        "ticker": ticker,
        "generated_at": now_iso(),
        "company_profile": company_profile,
        "sector": sector,
        "industry": company_profile.get("industry", "Unknown"),
        "company_news": company_news,
        "yahoo_news": yahoo_news,
        "google_news": google_news,
        "market_context": market_context,
        "sec_search": f"https://www.sec.gov/edgar/search/#/q={ticker}",
        "finance_profile": f"https://finance.yahoo.com/quote/{ticker}/profile",
    }


if __name__ == "__main__":
    result = get_web_research("NVDA")
    print(json.dumps(result, indent=2))
