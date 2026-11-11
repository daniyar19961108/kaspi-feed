#!/usr/bin/env python3
"""
Сборка XML-прайс-листа Kaspi для BSKZ (Baseus). Запускается GitHub Actions каждый час.

  * цены               — Google Таблица, лист «Товары» (Code, Name, RRP KZ)
  * остатки PP10       — фид поставщика Al-Style (sku = Code)
  * остатки PP7        — лист «Приход» минус заказы Kaspi с выдачей со склада PP7,
                         созданные после STOCK_START (через API Kaspi, секрет KASPI_TOKEN).
                         Без токена берётся колонка PP7 листа «Товары».
Если цены или фид Al-Style недоступны, скрипт падает и старый фид остаётся на месте.
Если недоступен только API Kaspi, остаток PP7 ставится 0 (чтобы не продать лишнее),
фид публикуется, а запуск помечается ошибкой (придёт письмо).
Только стандартная библиотека Python.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SHEET_ID = "1zladVRO6KAsDp9nxJ15ALcVHts72VlPUUFJNwwrtsJY"
GVIZ = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet="
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", GVIZ + urllib.parse.quote("Товары"))
INCOME_CSV_URL = os.environ.get("INCOME_CSV_URL", GVIZ + urllib.parse.quote("Приход"))
ALSTYLE_URL = os.environ.get("ALSTYLE_URL", "https://wsptech.mktx.io/storage/feeds/alstyle.xml")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "kaspi_feed.xml")
STOCK_REPORT = os.environ.get("STOCK_REPORT", "pp7_stock.csv")
ERROR_FLAG = "pp7_error.txt"
KASPI_TOKEN = os.environ.get("KASPI_TOKEN", "").strip()
KASPI_API = "https://kaspi.kz/shop/api/v2"

COMPANY = "BSKZ"
MERCHANT_ID = "30482122"
BRAND = "Baseus"
SKU_PREFIX = "BS"
STORE_SUPPLIER = "PP10"
STORE_MANUAL = "PP7"
# Момент, на который в листе «Приход» записан начальный остаток PP7.
# Заказы, созданные раньше, уже учтены в этом остатке.
STOCK_START = "2026-11-10 18:32"
# Заказы в этих статусах не уменьшают остаток (товар вернулся на склад).
NOT_SOLD_STATUSES = {"CANCELLED", "RETURNED"}
ORDER_STATES = ["NEW", "SIGN_REQUIRED", "PICKUP", "DELIVERY", "KASPI_DELIVERY", "ARCHIVE"]
MIN_SHEET_ROWS = 10
MIN_SUPPLIER_OFFERS = 1000
ALMATY = dt.timezone(dt.timedelta(hours=5))


def fetch(url, src, headers=None, tries=3):
    if os.path.exists(url):
        with open(url, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "bskz-kaspi-feed", **(headers or {})})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Не удалось скачать {src}: {last}")


def to_int(v):
    s = re.sub(r"[^\d\-]", "", str(v or ""))
    return int(s) if s not in ("", "-") else None


def norm_code(v):
    s = str(v or "").strip()
    if s.upper().startswith(SKU_PREFIX):
        s = s[len(SKU_PREFIX):]
    s = re.sub(r"\.0+$", "", s)
    return s if s.isdigit() else None


def read_csv(url, src):
    text = fetch(url, src).decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise RuntimeError(f"Google вернул HTML вместо листа «{src}»: проверьте доступ по ссылке.")
    return [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]


def read_sheet():
    items = {}
    for r in read_csv(SHEET_CSV_URL, "Товары"):
        code = norm_code(r.get("Code"))
        price = to_int(r.get("RRP KZ"))
        if not code or not price or price <= 0:
            continue
        items[code] = {
            "name": (r.get("Name") or "").strip(),
            "price": price,
            "pp7_manual": max(to_int(r.get("PP7")) or 0, 0),
        }
    if len(items) < MIN_SHEET_ROWS:
        raise RuntimeError(f"В таблице всего {len(items)} товаров с ценой: фид не обновлён.")
    return items


def read_income():
    income = {}
    for r in read_csv(INCOME_CSV_URL, "Приход"):
        code = norm_code(r.get("Code"))
        qty = to_int(r.get("Кол-во"))
        if code and qty:
            income[code] = income.get(code, 0) + qty
    return income


def read_alstyle():
    root = ET.fromstring(fetch(ALSTYLE_URL, "фид Al-Style"))
    stock = {}
    for offer in root.iter():
        if not offer.tag.endswith("offer"):
            continue
        qty = 0
        for a in offer.iter():
            if a.tag.endswith("availability") and a.get("available") in ("yes", "да"):
                qty += to_int(a.get("stockCount")) or 1
        stock[offer.get("sku")] = qty
    if len(stock) < MIN_SUPPLIER_OFFERS:
        raise RuntimeError(f"В фиде Al-Style всего {len(stock)} товаров: фид не обновлён.")
    return stock


# ---------- API Kaspi ----------

def kaspi_get(path, params=None):
    url = f"{KASPI_API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="[]$")
    raw = fetch(url, "API Kaspi", {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
        "X-Auth-Token": KASPI_TOKEN,
    })
    return json.loads(raw)


def kaspi_orders_since(start_ms):
    """Все заказы, созданные после start_ms (окнами по 14 дней, по всем состояниям)."""
    orders = {}
    now_ms = int(time.time() * 1000)
    window = 14 * 24 * 3600 * 1000
    for state in ORDER_STATES:
        lo = start_ms
        while lo < now_ms:
            hi = min(lo + window, now_ms)
            page = 0
            while True:
                data = kaspi_get("orders", {
                    "page[number]": page,
                    "page[size]": 100,
                    "filter[orders][state]": state,
                    "filter[orders][creationDate][$ge]": lo,
                    "filter[orders][creationDate][$le]": hi,
                })
                for o in data.get("data", []):
                    orders[o["id"]] = o
                if page + 1 >= (data.get("meta") or {}).get("pageCount", 0):
                    break
                page += 1
            lo = hi + 1
    return list(orders.values())


def entry_sku(entry):
    a = entry.get("attributes", {})
    for v in ((a.get("offer") or {}).get("code"), a.get("merchantSku"), a.get("sku")):
        if v:
            return str(v)
    return None


def kaspi_pp7_sold():
    start = dt.datetime.strptime(STOCK_START, "%Y-%m-%d %H:%M").replace(tzinfo=ALMATY)
    orders = kaspi_orders_since(int(start.timestamp() * 1000))
    sold, log = {}, []
    stores = {}
    for o in orders:
        a = o.get("attributes", {})
        pp = str(a.get("pickupPointId") or "")
        stores[pp] = stores.get(pp, 0) + 1
        if not pp.endswith("_" + STORE_MANUAL) and pp != STORE_MANUAL:
            continue
        status = a.get("status")
        counted = status not in NOT_SOLD_STATUSES
        for e in kaspi_get(f"orders/{o['id']}/entries").get("data", []):
            sku = entry_sku(e)
            code = norm_code(sku)
            if not code:
                raise RuntimeError(f"Не найден артикул в позиции заказа {a.get('code')}: "
                                   f"{json.dumps(e.get('attributes', {}), ensure_ascii=False)[:500]}")
            qty = int(e.get("attributes", {}).get("quantity") or 1)
            if counted:
                sold[code] = sold.get(code, 0) + qty
            log.append(f"  заказ {a.get('code')} {status} {SKU_PREFIX}{code} x{qty}"
                       + ("" if counted else " (не учтён)"))
    print(f"API Kaspi: заказов после {STOCK_START}: {len(orders)}, по складам: {stores}")
    print("Заказы со склада PP7:" if log else "Заказов со склада PP7 нет.")
    print("\n".join(log))
    return sold


def pp7_stock(items):
    if not KASPI_TOKEN:
        print("KASPI_TOKEN не задан: PP7 берётся из колонки PP7 листа «Товары».")
        return {c: it["pp7_manual"] for c, it in items.items()}, None
    income = read_income()
    try:
        sold = kaspi_pp7_sold()
    except Exception as e:
        msg = f"PP7 не посчитан, остаток PP7 = 0 у всех товаров. Причина: {e}"
        print("ОШИБКА:", msg, file=sys.stderr)
        with open(ERROR_FLAG, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
        return {c: 0 for c in items}, None
    stock = {c: max(income.get(c, 0) - sold.get(c, 0), 0) for c in items}
    report = [(c, items[c]["name"], income.get(c, 0), sold.get(c, 0), stock[c])
              for c in items if income.get(c) or sold.get(c)]
    return stock, report


# ---------- XML ----------

def availability(store, qty):
    if qty > 0:
        return f'<availability available="yes" storeId="{store}" stockCount="{qty}"/>'
    return f'<availability available="no" storeId="{store}"/>'


def build(items, supplier, pp7):
    now = dt.datetime.now(ALMATY).strftime("%Y-%m-%d %H:%M")
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<kaspi_catalog date="{now}" xmlns="kaspiShopping" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="kaspiShopping http://kaspi.kz/kaspishopping.xsd">',
        f"  <company>{escape(COMPANY)}</company>",
        f"  <merchantid>{escape(MERCHANT_ID)}</merchantid>",
        "  <offers>",
    ]
    for code, it in items.items():
        out += [
            f"    <offer sku={quoteattr(SKU_PREFIX + code)}>",
            f"      <model>{escape(it['name'])}</model>",
            f"      <brand>{BRAND}</brand>",
            "      <availabilities>",
            "        " + availability(STORE_SUPPLIER, supplier.get(code, 0)),
            "        " + availability(STORE_MANUAL, pp7.get(code, 0)),
            "      </availabilities>",
            f"      <price>{it['price']}</price>",
            "    </offer>",
        ]
    out += ["  </offers>", "</kaspi_catalog>", ""]
    return "\n".join(out)


def main():
    if os.path.exists(ERROR_FLAG):
        os.remove(ERROR_FLAG)
    items = read_sheet()
    supplier = read_alstyle()
    pp7, report = pp7_stock(items)
    xml = build(items, supplier, pp7)
    ET.fromstring(xml.encode("utf-8"))  # проверка валидности до записи
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
    os.replace(tmp, OUTPUT_FILE)

    if report is not None:
        with open(STOCK_REPORT, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Code", "Name", "Приход", "Продано", "Остаток PP7"])
            w.writerows(report)
        print("Остатки PP7:")
        for c, name, inc, s, left in report:
            print(f"  {SKU_PREFIX}{c}: приход {inc}, продано {s}, остаток {left}")

    missing = [c for c in items if c not in supplier]
    in_stock = sum(1 for c in items if supplier.get(c, 0) + pp7.get(c, 0) > 0)
    print(f"Товаров: {len(items)}, в наличии: {in_stock}")
    if missing:
        print("Нет в фиде Al-Style:", ", ".join(missing))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ОШИБКА:", e, file=sys.stderr)
        sys.exit(1)
