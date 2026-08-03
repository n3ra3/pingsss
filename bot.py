"""Steam buy-order watcher Telegram bot.

You place a buy order at price X. When a sell listing appears ABOVE X but still
within your profit ceiling (X * (1 + margin%)), the bot pings you so you can grab
it manually. Each individual lot (listingid) is alerted only once.

Runs three things in one process:
  * a tiny HTTP server exposing /health  (for Render + UptimeRobot keep-alive)
  * a Telegram long-polling loop          (add / list / remove items)
  * a Steam polling loop                  (check listings, send alerts)
"""
import html
import os
import re
import sys
import threading
import time
import traceback

from dotenv import load_dotenv
load_dotenv()  # read .env before importing modules that read env vars at import time

import requests
from flask import Flask, jsonify
from waitress import serve

import steam
import storage
import pirateswap

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
CURRENCY = int(os.environ.get("CURRENCY", "1"))          # 1 = USD
COUNTRY = os.environ.get("COUNTRY", "US")
DEFAULT_PROFIT_PCT = float(os.environ.get("DEFAULT_PROFIT_PCT", "20"))
PS_FEE_PCT = float(os.environ.get("PS_FEE_PCT", "10"))   # haircut on PirateSwap price
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))    # seconds per full cycle
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "8"))    # seconds between items
STEAM_COOLDOWN = int(os.environ.get("STEAM_COOLDOWN", "600"))  # pause after a 429, seconds
MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))            # search pages to scan per item
STEAM_COOKIE = os.environ.get("STEAM_COOKIE", "").strip()      # optional steamLoginSecure=...
PORT = int(os.environ.get("PORT", "10000"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_session = requests.Session()          # Steam (may carry steamLoginSecure cookie)
_ps_session = requests.Session()       # PirateSwap (kept separate — no Steam cookie)
if STEAM_COOKIE:
    # accept either "steamLoginSecure=VALUE" or just "VALUE"
    _cookie = STEAM_COOKIE if "=" in STEAM_COOKIE else f"steamLoginSecure={STEAM_COOKIE}"
    _session.headers.update({"Cookie": _cookie})

_cooldown_until = 0  # epoch seconds; while now < this, skip all Steam requests

app = Flask(__name__)


@app.route("/")
@app.route("/health")
def health():
    last = storage.get_meta("last_poll", "never")
    return jsonify({
        "status": "ok",
        "items": storage.count_items(),
        "last_poll": last,
        "now": int(time.time()),
    })


# ---------------------------------------------------------------- telegram i/o
def send_message(chat_id, text):
    try:
        _session.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
    except requests.RequestException as e:
        print(f"[tg] send failed: {e}", flush=True)


def _authorized(chat_id):
    if not OWNER_CHAT_ID:
        return True  # not locked down yet
    return str(chat_id) == OWNER_CHAT_ID


def notify_owner(text, fallback_chat=None):
    """Send an out-of-band notice to the owner (or the given chat as fallback)."""
    chat = OWNER_CHAT_ID or fallback_chat
    if chat:
        send_message(chat, text)


HELP = (
    "<b>Steam → PirateSwap арбитраж</b>\n\n"
    "Кидай <b>только ссылку</b> на предмет Steam Market (можно несколько за раз). "
    "Бот сам берёт нижнюю цену Steam (закуп) и цену на PirateSwap за вычетом "
    f"{PS_FEE_PCT:g}% (продажа) и пингует, когда профит ≥ <b>{DEFAULT_PROFIT_PCT:g}%</b>.\n\n"
    "Можно задать свой порог: добавь число после ссылки —\n"
    "<code>https://steamcommunity.com/market/listings/730/... 25</code> "
    "(порог 25%).\n\n"
    f"Профит = (цена PirateSwap × {1 - PS_FEE_PCT/100:g} − нижняя цена Steam) / "
    "нижняя цена Steam.\n\n"
    "Команды:\n"
    "/list — список отслеживаемого\n"
    "/check — проверить прямо сейчас\n"
    "/remove N — убрать предмет №N из /list\n"
    "/help — эта справка"
)

_NUM = r"\d+(?:[.,]\d+)?"


def _num(s):
    return float(s.replace(",", "."))


def _profit_pct(steam_cents, ps_cents):
    return (ps_cents - steam_cents) / steam_cents * 100 if steam_cents else 0


def handle_add(chat_id, text):
    parsed = steam.parse_market_url(text)
    if not parsed:
        send_message(chat_id, "Не вижу ссылку на предмет Steam Market. /help")
        return
    appid, name, url = parsed

    rest = re.sub(r"\S*steamcommunity\.com/market/listings/\S+", "", text)
    nums = re.findall(_NUM, rest)
    threshold = _num(nums[0]) if nums else DEFAULT_PROFIT_PCT

    # resolve the PirateSwap code up front so we can confirm coverage
    code, ps_err = pirateswap.resolve_code(name, session=_ps_session)
    item_id = storage.add_item(chat_id, appid, name, name, url, threshold, code)

    lines = [f"✅ Отслеживаю <b>#{item_id}</b>: {html.escape(name)}",
             f"Порог профита: <b>{threshold:g}%</b>"]
    if code:
        lines.append("PirateSwap: ✔ найден")
    elif ps_err == "not_on_ps":
        lines.append("PirateSwap: ⚠️ предмета нет на площадке — профит не посчитать")
    else:
        lines.append(f"PirateSwap: пока не подтверждён ({reason_text(ps_err or 'no_stock')}), "
                     "попробую в цикле")
    lines.append("Пингну, когда профит достигнет порога.")
    send_message(chat_id, "\n".join(lines))


def handle_list(chat_id):
    items = storage.list_items(chat_id)
    if not items:
        send_message(chat_id, "Список пуст. Кинь ссылку на предмет Steam Market, чтобы добавить.")
        return
    lines = ["<b>Отслеживаемые предметы:</b>"]
    for i, it in enumerate(items, 1):
        thr = (it["profit_threshold"] if it["profit_threshold"] is not None
               else DEFAULT_PROFIT_PCT)
        ps = "✔ PS" if it["ps_code"] else "— PS?"
        lines.append(f"{i}. {html.escape(it['name'])}\n"
                     f"    порог {thr:g}% · {ps}")
    lines.append("\nУбрать: /remove N")
    send_message(chat_id, "\n".join(lines))


def handle_check(chat_id):
    """On-demand: compute Steam→PirateSwap profit for all tracked items."""
    items = storage.list_items(chat_id)
    if not items:
        send_message(chat_id, "Список пуст. Кинь ссылку на предмет Steam Market.")
        return
    send_message(chat_id, f"Проверяю {len(items)} предмет(ов) сейчас...")
    lines = []
    for it in items:
        ev = evaluate_item(it)
        name = html.escape(it["name"])
        if ev.get("error"):
            side = ev.get("side")
            lines.append(f"• {name}\n   ⚠️ {side}: {reason_text(ev['error'])}")
            print(f"[check] {it['name']}: {side}:{ev['error']}", flush=True)
        else:
            mark = ("✅ ВЫГОДНО" if ev["profit"] >= ev["threshold"] else "—")
            lines.append(
                f"• {name}\n"
                f"   Steam ${ev['steam_cents']/100:.2f} → "
                f"PS ${ev['ps_cents']/100:.2f} −{PS_FEE_PCT:g}% = "
                f"${ev['ps_net_cents']/100:.2f}\n"
                f"   профит <b>+{ev['profit']:.1f}%</b> · "
                f"чистыми <b>${ev['net_profit_cents']/100:+.2f}</b> "
                f"(порог {ev['threshold']:g}%) {mark}")
            print(f"[check] {it['name']}: steam=${ev['steam_cents']/100:.2f} "
                  f"ps_net=${ev['ps_net_cents']/100:.2f} profit=+{ev['profit']:.1f}%",
                  flush=True)
        time.sleep(REQUEST_DELAY)
    send_message(chat_id, "\n\n".join(lines))


def handle_remove(chat_id, arg):
    items = storage.list_items(chat_id)
    try:
        idx = int(arg)
    except (ValueError, TypeError):
        send_message(chat_id, "Использование: /remove N (номер из /list)")
        return
    if idx < 1 or idx > len(items):
        send_message(chat_id, f"Нет предмета №{idx}. Смотри /list.")
        return
    it = items[idx - 1]
    storage.remove_item(it["id"], chat_id)
    send_message(chat_id, f"🗑 Убрал: {html.escape(it['name'])}")


def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    print(f"[tg] сообщение от {chat_id}: {text[:80]}", flush=True)

    if not _authorized(chat_id):
        send_message(chat_id,
                     f"Доступ только для владельца. Твой chat_id: <code>{chat_id}</code>")
        return

    low = text.lower()
    if low in ("/start", "/help") or low.startswith("/help"):
        send_message(chat_id, HELP + (f"\n\nТвой chat_id: <code>{chat_id}</code>"
                                      if not OWNER_CHAT_ID else ""))
    elif low.startswith("/list"):
        handle_list(chat_id)
    elif low.startswith("/check"):
        handle_check(chat_id)
    elif low.startswith("/remove") or low.startswith("/del"):
        parts = text.split()
        handle_remove(chat_id, parts[1] if len(parts) > 1 else None)
    elif "steamcommunity.com/market/listings/" in low:
        handle_add(chat_id, text)
    else:
        send_message(chat_id, "Не понял. Кинь ссылку на предмет Steam Market или /help.")


def telegram_loop():
    offset = None
    print("[tg] polling started", flush=True)
    while True:
        try:
            r = _session.get(f"{API}/getUpdates",
                             params={"timeout": 30, "offset": offset},
                             timeout=40)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    traceback.print_exc()
        except requests.RequestException as e:
            print(f"[tg] poll error: {e}", flush=True)
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


# ---------------------------------------------------------------- steam poller
REASON_RU = {
    "429": "рейт-лимит (429)",
    "no_data": "предмет не найден на Steam (проверь ссылку/имя)",
    "no_price": "нет активных лотов на продажу сейчас",
    "currency": "цена пришла не в USD (регион IP)",
    "not_on_ps": "нет на PirateSwap",
    "no_stock": "нет активных лотов на PirateSwap",
    "network": "ошибка сети",
    "badjson": "некорректный ответ",
}


def reason_text(r):
    return REASON_RU.get(r, r)


def evaluate_item(it):
    """Fetch Steam (buy cost) + PirateSwap (sell price) and compute profit %.

    Returns on success:
      {'steam_cents', 'ps_cents', 'profit', 'threshold', 'steam_data'}
    On failure:
      {'error': reason, 'side': 'steam'|'ps', 'steam_cents'?, 'steam_data'?}
    """
    threshold = (it["profit_threshold"] if it["profit_threshold"] is not None
                 else DEFAULT_PROFIT_PCT)
    sd = steam.fetch_lowest_price(
        it["appid"], it["market_hash_name"],
        currency=CURRENCY, session=_session, max_pages=MAX_PAGES)
    if sd.get("error"):
        return {"error": sd["error"], "side": "steam", "steam_data": sd}
    steam_cents = sd["lowest_cents"]

    code = it["ps_code"]
    if not code:
        code, perr = pirateswap.resolve_code(it["market_hash_name"],
                                             session=_ps_session)
        if not code:
            return {"error": perr, "side": "ps", "steam_cents": steam_cents}
        storage.update_ps_code(it["id"], code)
    pd = pirateswap.fetch_price(code, session=_ps_session)
    if pd.get("error"):
        return {"error": pd["error"], "side": "ps", "steam_cents": steam_cents}

    ps_cents = pd["price_cents"]
    ps_net_cents = round(ps_cents * (1 - PS_FEE_PCT / 100))  # what you actually get
    return {"steam_cents": steam_cents, "ps_cents": ps_cents,
            "ps_net_cents": ps_net_cents,
            "net_profit_cents": ps_net_cents - steam_cents,  # clean profit in $
            "profit": _profit_pct(steam_cents, ps_net_cents),
            "threshold": threshold, "steam_data": sd}


def check_item(it):
    ev = evaluate_item(it)
    if ev.get("error"):
        reason, side = ev["error"], ev.get("side")
        diag = ""
        sd = ev.get("steam_data") or {}
        if reason == "no_data" and "scanned" in sd:
            diag = (f" [просмотрено {sd['scanned']}/{sd['total']}, "
                    f"напр.: {sd.get('sample')}]")
        print(f"[calc] {it['name']}: {side}: {reason_text(reason)}{diag} — пропуск",
              flush=True)
        # only a Steam 429 warrants the global cooldown
        return "rate_limited" if (reason == "429" and side == "steam") else None

    steam_c, ps_c, ps_net = ev["steam_cents"], ev["ps_cents"], ev["ps_net_cents"]
    net = ev["net_profit_cents"]
    profit, thr = ev["profit"], ev["threshold"]
    last = it["last_alert_cents"]

    if profit >= thr:
        if last != steam_c:  # new opportunity (Steam price moved) -> ping once
            send_message(it["chat_id"],
                         f"🔔 <b>{html.escape(it['name'])}</b>\n"
                         f"Профит <b>+{profit:.1f}%</b> (порог {thr:g}%)\n"
                         f"Чистыми: <b>${net/100:+.2f}</b>\n"
                         f"Steam (закуп): <b>${steam_c/100:.2f}</b>\n"
                         f"PirateSwap: ${ps_c/100:.2f} −{PS_FEE_PCT:g}% = "
                         f"<b>${ps_net/100:.2f}</b>\n"
                         f"{it['url']}")
            storage.update_last_alert(it["id"], steam_c)
            decision = f"🔔 АЛЕРТ +{profit:.1f}%"
        else:
            decision = f"+{profit:.1f}% ≥ порога, уже уведомлял"
    else:
        if last is not None:
            storage.update_last_alert(it["id"], None)
        decision = f"+{profit:.1f}% < {thr:g}%"

    print(f"[calc] {it['name']}: steam=${steam_c/100:.2f} "
          f"ps=${ps_c/100:.2f}(-{PS_FEE_PCT:g}%=${ps_net/100:.2f}) "
          f"-> {decision}", flush=True)


def poller_loop():
    global _cooldown_until
    print("[steam] poller started", flush=True)
    while True:
        wait = _cooldown_until - time.time()
        if wait > 0:
            print(f"[steam] пауза после лимита Steam (429): ещё {int(wait)}s",
                  flush=True)
            time.sleep(min(wait, POLL_INTERVAL))
            continue

        items = storage.all_items()
        if not items:
            print("[steam] watchlist пуст — пришли боту ссылку на предмет Steam Market",
                  flush=True)
        else:
            print(f"[steam] --- цикл: проверяю {len(items)} предмет(ов) ---", flush=True)
        for it in items:
            try:
                if check_item(it) == "rate_limited":
                    _cooldown_until = time.time() + STEAM_COOLDOWN
                    print(f"[steam] ⚠️ Steam лимитирует (429). Пауза {STEAM_COOLDOWN}s, "
                          f"чтобы лимит сбросился.", flush=True)
                    notify_owner(
                        f"⚠️ Steam вернул <b>429</b> (рейт-лимит).\n"
                        f"Похоже, пора обновить куку <code>STEAM_COOKIE</code> "
                        f"(свежий <code>steamLoginSecure</code>) на Render.\n"
                        f"Пауза {STEAM_COOLDOWN // 60} мин, потом попробую снова.",
                        fallback_chat=it["chat_id"])
                    break
            except Exception as e:
                print(f"[steam] {it['name']}: ошибка: {e}", flush=True)
            time.sleep(REQUEST_DELAY)
        storage.set_meta("last_poll", int(time.time()))
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------- main
def main():
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN is required (set it as an environment variable).")
    storage.init_db()
    print(f"[init] storage={'postgres' if storage.IS_PG else 'sqlite'} "
          f"owner={'set' if OWNER_CHAT_ID else 'OPEN'} "
          f"currency={CURRENCY} profit>={DEFAULT_PROFIT_PCT}% "
          f"interval={POLL_INTERVAL}s cookie={'yes' if STEAM_COOKIE else 'no'}",
          flush=True)
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=poller_loop, daemon=True).start()
    print(f"[init] serving /health on :{PORT}", flush=True)
    serve(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
