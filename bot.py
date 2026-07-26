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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
CURRENCY = int(os.environ.get("CURRENCY", "1"))          # 1 = USD
COUNTRY = os.environ.get("COUNTRY", "US")
DEFAULT_MARGIN_PCT = float(os.environ.get("DEFAULT_MARGIN_PCT", "10"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "90"))     # seconds per full cycle
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "4"))    # seconds between items
PORT = int(os.environ.get("PORT", "10000"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_session = requests.Session()

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


HELP = (
    "<b>Steam buy-order watcher</b>\n\n"
    "Кидай сообщение вида:\n"
    "<code>&lt;ссылка на предмет&gt; &lt;твоя цена ордера&gt; [+N%] [макс. цена]</code>\n\n"
    "Примеры:\n"
    "<code>https://steamcommunity.com/market/listings/730/... 12.50</code>\n"
    "<code>https://steamcommunity.com/market/listings/730/... 12.50 8%</code>\n"
    "<code>https://steamcommunity.com/market/listings/730/... 12.50 14.00</code>\n\n"
    "Первое число — цена твоего ордера. <code>N%</code> — потолок выгоды над "
    "ордером. Второе число (без %) — абсолютный максимум.\n"
    f"По умолчанию потолок +{DEFAULT_MARGIN_PCT:g}%.\n\n"
    "Команды:\n"
    "/list — список отслеживаемого\n"
    "/check — проверить цены прямо сейчас\n"
    "/remove N — убрать предмет №N из /list\n"
    "/help — эта справка"
)

_NUM = r"\d+(?:[.,]\d+)?"


def _num(s):
    return float(s.replace(",", "."))


def handle_add(chat_id, text):
    parsed = steam.parse_market_url(text)
    if not parsed:
        send_message(chat_id, "Не вижу ссылку на предмет Steam Market. /help")
        return
    appid, name, url = parsed

    rest = re.sub(r"\S*steamcommunity\.com/market/listings/\S+", "", text)
    pct_m = re.search(rf"({_NUM})\s*%", rest)
    margin_pct = _num(pct_m.group(1)) if pct_m else None
    if pct_m:
        rest = rest[:pct_m.start()] + rest[pct_m.end():]

    nums = re.findall(_NUM, rest)
    if not nums:
        send_message(chat_id, "Укажи цену своего ордера после ссылки. /help")
        return
    order_price = _num(nums[0])
    order_cents = round(order_price * 100)

    max_price = _num(nums[1]) if len(nums) > 1 else None
    if margin_pct is None and max_price is not None and order_price > 0:
        margin_pct = (max_price / order_price - 1) * 100
    if margin_pct is None:
        margin_pct = DEFAULT_MARGIN_PCT

    item_id = storage.add_item(chat_id, appid, name, name, url, order_cents, margin_pct)
    ceiling = order_price * (1 + margin_pct / 100)
    send_message(
        chat_id,
        f"✅ Отслеживаю <b>#{item_id}</b>: {html.escape(name)}\n"
        f"Твой ордер: <b>${order_price:.2f}</b>\n"
        f"Потолок: <b>${ceiling:.2f}</b> (+{margin_pct:.1f}%)\n"
        f"Пингну, когда появится лот на продажу в этом диапазоне.",
    )


def handle_list(chat_id):
    items = storage.list_items(chat_id)
    if not items:
        send_message(chat_id, "Список пуст. Кинь ссылку + цену, чтобы добавить.")
        return
    lines = ["<b>Отслеживаемые предметы:</b>"]
    for i, it in enumerate(items, 1):
        order = it["order_price_cents"] / 100
        ceiling = order * (1 + (it["margin_pct"] or 0) / 100)
        lines.append(
            f"{i}. {html.escape(it['name'])}\n"
            f"    ордер ${order:.2f} · потолок ${ceiling:.2f} "
            f"(+{it['margin_pct']:.1f}%)"
        )
    lines.append("\nУбрать: /remove N")
    send_message(chat_id, "\n".join(lines))


def handle_check(chat_id):
    """On-demand: fetch current prices for all tracked items and report."""
    items = storage.list_items(chat_id)
    if not items:
        send_message(chat_id, "Список пуст. Добавь предмет: ссылка + цена.")
        return
    send_message(chat_id, f"Проверяю {len(items)} предмет(ов) сейчас...")
    lines = []
    for it in items:
        order = it["order_price_cents"]
        ceil = round(order * (1 + (it["margin_pct"] or 0) / 100))
        data = steam.fetch_lowest_price(
            it["appid"], it["market_hash_name"], currency=CURRENCY, session=_session)
        name = html.escape(it["name"])
        if not data:
            lines.append(f"• {name}\n   ⚠️ нет данных (рейт-лимит Steam или нет лотов)")
            print(f"[check] {it['name']}: нет данных", flush=True)
        else:
            low = data["lowest_cents"]
            in_range = low <= ceil
            mark = "✅ в диапазоне" if in_range else "— выше потолка"
            extra = []
            if data.get("median_cents"):
                extra.append(f"медиана ${data['median_cents']/100:.2f}")
            if data.get("volume"):
                extra.append(f"объём {data['volume']}")
            extra_s = ("\n   " + " · ".join(extra)) if extra else ""
            lines.append(
                f"• {name}\n"
                f"   лоу <b>${low/100:.2f}</b> · ордер ${order/100:.2f} · "
                f"потолок ${ceil/100:.2f}{extra_s}\n   {mark}")
            print(f"[check] {it['name']}: lowest=${low/100:.2f} "
                  f"order=${order/100:.2f} ceiling=${ceil/100:.2f} "
                  f"in_range={in_range}", flush=True)
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
        send_message(chat_id, "Не понял. Кинь ссылку + цену или /help.")


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
def check_item(it):
    order_cents = it["order_price_cents"]
    ceiling_cents = round(order_cents * (1 + (it["margin_pct"] or 0) / 100))
    data = steam.fetch_lowest_price(
        it["appid"], it["market_hash_name"],
        currency=CURRENCY, session=_session)
    if not data:
        print(f"[steam] {it['name']}: нет данных (рейт-лимит/нет лотов) — пропуск",
              flush=True)
        return  # rate limited / no listings — skip this cycle, don't reset state

    low = data["lowest_cents"]
    last = it["last_alert_cents"]

    # ping on ANY lot at or below the ceiling — including below your order
    if low <= ceiling_cents:
        if last != low:  # new lot / new price -> ping once
            over = (low / order_cents - 1) * 100 if order_cents else 0
            vol = f"\nОбъём за сутки: {data['volume']}" if data.get("volume") else ""
            send_message(it["chat_id"],
                         f"🔔 <b>{html.escape(it['name'])}</b>\n"
                         f"Самый дешёвый лот: <b>${low/100:.2f}</b> "
                         f"({over:+.1f}% к ордеру ${order_cents/100:.2f})\n"
                         f"Потолок: ${ceiling_cents/100:.2f}{vol}\n"
                         f"{it['url']}")
            storage.update_last_alert(it["id"], low)
            decision = "🔔 АЛЕРТ отправлен"
        else:
            decision = "в диапазоне, уже уведомлял"
    else:
        # above ceiling: reset so a later drop re-alerts
        if last is not None:
            storage.update_last_alert(it["id"], None)
        decision = "выше потолка"

    med = data.get("median_cents")
    med_s = f" median=${med/100:.2f}" if med else ""
    vol_s = f" vol={data['volume']}" if data.get("volume") else ""
    print(f"[steam] {it['name']}: lowest=${low/100:.2f}{med_s}{vol_s} "
          f"order=${order_cents/100:.2f} ceiling=${ceiling_cents/100:.2f} "
          f"-> {decision}", flush=True)


def poller_loop():
    print("[steam] poller started", flush=True)
    while True:
        items = storage.all_items()
        if not items:
            print("[steam] watchlist пуст — отправь боту ссылку + цену, чтобы добавить",
                  flush=True)
        else:
            print(f"[steam] --- цикл: проверяю {len(items)} предмет(ов) ---", flush=True)
        for it in items:
            try:
                check_item(it)
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
          f"currency={CURRENCY} margin={DEFAULT_MARGIN_PCT}%", flush=True)
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=poller_loop, daemon=True).start()
    print(f"[init] serving /health on :{PORT}", flush=True)
    serve(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
