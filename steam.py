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
                       max_retries=3):
    """Query Steam's priceoverview endpoint.

    Returns {'lowest_cents': int, 'median_cents': int|None, 'volume': int|None}
    for the cheapest current sell listing, or None if unavailable (rate limited,
    no listings, network error). None means "skip this cycle", not "out of range".
    """
    sess = session or requests
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": appid,
        "currency": currency,
        "market_hash_name": market_hash_name,
    }
    for attempt in range(max_retries):
        try:
            r = sess.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if not data or not data.get("success"):
                return None
            lowest = parse_price_to_cents(data.get("lowest_price"))
            if lowest is None:
                return None
            vol = None
            if data.get("volume"):
                try:
                    vol = int(re.sub(r"[^0-9]", "", data["volume"]))
                except ValueError:
                    vol = None
            return {
                "lowest_cents": lowest,
                "median_cents": parse_price_to_cents(data.get("median_price")),
                "volume": vol,
            }
        except (requests.RequestException, ValueError):
            time.sleep(3)
    return None
