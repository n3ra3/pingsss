"""Steam Community Market helpers: parse market URLs and fetch sell listings."""
import re
import time
from urllib.parse import quote, unquote

import requests

_URL_RE = re.compile(r"steamcommunity\.com/market/listings/(\d+)/([^/?#\s]+)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def parse_market_url(text):
    """Return (appid, market_hash_name, canonical_url) or None."""
    m = _URL_RE.search(text)
    if not m:
        return None
    appid = m.group(1)
    name = unquote(m.group(2))
    canonical = (
        f"https://steamcommunity.com/market/listings/{appid}/{quote(name, safe='')}"
    )
    return appid, name, canonical


def parse_price_to_cents(s):
    """Parse a Steam price string ('$12.34', '1,234.56', '12,34€') to int cents.

    The rightmost '.' or ',' is treated as the decimal separator; any other
    separators are thousands separators and dropped.
    """
    if not s:
        return None
    digits = re.sub(r"[^0-9.,]", "", s)
    if not digits:
        return None
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    dec = "." if last_dot > last_comma else ","
    other = "," if dec == "." else "."
    digits = digits.replace(other, "").replace(dec, ".")
    try:
        return round(float(digits) * 100)
    except ValueError:
        return None


def fetch_lowest_price(appid, market_hash_name, currency=1, session=None,
                       max_pages=50):
    """Query Steam's market search endpoint for the cheapest active listing.

    Uses market/search/render (a different, more lenient rate-limit bucket than
    priceoverview) and picks the result whose hash_name matches exactly.

    On success returns
      {'lowest_cents': int, 'median_cents': None, 'volume': int}
      where 'volume' is the number of active sell listings.
    On failure returns {'error': reason}:
      '429'      -> rate limited (caller should back off hard)
      'no_data'  -> item not found in results
      'no_price' -> no active sell listing right now
      'currency' -> price came back in a non-USD currency (region mismatch)
      'http<N>'  -> unexpected status code
      'network' / 'badjson' -> transport / parse problem
    Does NOT retry: retrying during a rate limit only keeps the ban alive.
    """
    sess = session or requests
    url = "https://steamcommunity.com/market/search/render/"
    # search is fuzzy (matches on tokens) and returns only ~10 per page, so we
    # page through the results and pick the one whose hash_name matches exactly.
    page = 0
    start = 0
    while page < max_pages:
        params = {
            "query": market_hash_name,
            "appid": appid,
            "search_descriptions": 0,
            "norender": 1,
            "count": 100,   # capped to ~10 by Steam, but harmless
            "start": start,
            "currency": currency,  # ignored by this endpoint; region decides
        }
        try:
            r = sess.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException:
            return {"error": "network"}
        if r.status_code == 429:
            return {"error": "429"}
        if r.status_code != 200:
            return {"error": f"http{r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            return {"error": "badjson"}
        if not data or not data.get("success"):
            return {"error": "no_data"}

        results = data.get("results") or []
        for res in results:
            names = (res.get("hash_name"),
                     (res.get("asset_description") or {}).get("market_hash_name"))
            if market_hash_name in names:
                return _parse_result(res)

        total = data.get("total_count") or 0
        start += len(results)
        page += 1
        if not results or start >= total:
            break
        time.sleep(1.5)  # be gentle between pages

    return {"error": "no_data"}


def _parse_result(res):
    sell_price = res.get("sell_price")
    listings = res.get("sell_listings") or 0
    if not sell_price or not listings:
        return {"error": "no_price"}
    # guard against a non-USD region: only trust plain "$" prices
    price_text = res.get("sell_price_text") or ""
    if "$" not in price_text or any(c in price_text for c in ("€", "£", "₽", "¥")):
        return {"error": "currency"}
    return {
        "lowest_cents": int(sell_price),  # already in cents
        "median_cents": None,
        "volume": int(listings),
    }
