"""Steam Community Market helpers: parse market URLs and fetch sell listings."""
import re
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


def fetch_lowest_price(appid, market_hash_name, currency=1, session=None):
    """Query Steam's priceoverview endpoint.

    On success returns
      {'lowest_cents': int, 'median_cents': int|None, 'volume': int|None}
    On failure returns {'error': reason}, where reason is:
      '429'      -> rate limited (caller should back off hard)
      'no_data'  -> success=false (bad name / no listings)
      'no_price' -> no lowest_price in the payload
      'http<N>'  -> unexpected status code
      'network' / 'badjson' -> transport / parse problem
    Does NOT retry: retrying during a rate limit only keeps the ban alive.
    """
    sess = session or requests
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": appid,
        "currency": currency,
        "market_hash_name": market_hash_name,
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
    lowest = parse_price_to_cents(data.get("lowest_price"))
    if lowest is None:
        return {"error": "no_price"}
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
