"""PirateSwap price lookups via their public web API (no login required).

A skin's autocomplete entry returns one hashcode per exterior, unlabeled. We
resolve the exact item by probing each code (cheapest listing, results=1) and
matching the full marketHashName (with exterior). Once resolved, the caller
stores that single code and each poll is one cheap request.

All prices are USD. 'price' is the exchange/trade value; 'storePrice' the cash
value. We return both in cents and let the caller choose.
"""
import requests

BASE = "https://web.pirateswap.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://pirateswap.com",
    "Referer": "https://pirateswap.com/",
}

EXTERIORS = (
    " (Factory New)", " (Minimal Wear)", " (Field-Tested)",
    " (Well-Worn)", " (Battle-Scarred)",
)


def base_name(steam_market_hash_name):
    """Strip the trailing ' (Exterior)' so it matches PirateSwap's base name."""
    for e in EXTERIORS:
        if steam_market_hash_name.endswith(e):
            return steam_market_hash_name[:-len(e)]
    return steam_market_hash_name


def _autocomplete(bn, sess):
    """Return (codes_list|None, error). [] means the skin isn't listed."""
    try:
        r = sess.get(f"{BASE}/inventory/search/v2/autocomplete",
                     params={"searchPhrase": bn}, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None, "network"
    if r.status_code == 429:
        return None, "429"
    if r.status_code != 200:
        return None, f"http{r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return None, "badjson"
    entry = next((e for e in (data or []) if e.get("marketHashName") == bn), None)
    if not entry:
        return [], None
    return [str(c) for c in (entry.get("marketNameHashCodes") or [])], None


def _cheapest(code, sess):
    """Return (item|None, error) for the cheapest listing under one hashcode."""
    try:
        r = sess.get(f"{BASE}/inventory/v2/ExchangerInventory",
                     params={"orderBy": "price", "sortOrder": "ASC",
                             "page": 1, "results": 1,
                             "marketHashNameHashCodes": code},
                     headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None, "network"
    if r.status_code == 429:
        return None, "429"
    if r.status_code != 200:
        return None, f"http{r.status_code}"
    try:
        items = r.json().get("items") or []
    except ValueError:
        return None, "badjson"
    return (items[0] if items else None), None


def resolve_code(steam_market_hash_name, session=None):
    """Find the PirateSwap hashcode for the exact item (with exterior).

    Returns (code_str, error). code_str is None on failure; error is one of:
      None       -> success (code_str set)
      'not_on_ps'-> the skin isn't listed on PirateSwap at all
      'no_stock' -> listed, but this exterior has no active listing to confirm
      '429' / 'network' / 'badjson' / 'http<N>'
    """
    sess = session or requests
    codes, err = _autocomplete(base_name(steam_market_hash_name), sess)
    if err:
        return None, err
    if not codes:
        return None, "not_on_ps"
    for code in codes:
        item, e = _cheapest(code, sess)
        if e == "429":
            return None, "429"
        if item and item.get("marketHashName") == steam_market_hash_name:
            return code, None
    return None, "no_stock"


def fetch_price(code, session=None):
    """Return {'price_cents': int, 'store_cents': int} for the cheapest current
    listing of the resolved item, or {'error': reason}."""
    sess = session or requests
    item, err = _cheapest(code, sess)
    if err:
        return {"error": err}
    if not item:
        return {"error": "no_stock"}
    return {
        "price_cents": round(float(item["price"]) * 100),
        "store_cents": round(float(item.get("storePrice") or 0) * 100),
    }
