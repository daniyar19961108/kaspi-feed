#!/usr/bin/env python3
"""
Сборка XML-прайс-листа Kaspi для BSKZ (Baseus). Запускается GitHub Actions каждый час.

  * цены и остатки PP7 — Google Таблица (лист «Товары»: Code, Name, RRP KZ, PP7)
  * остатки PP10       — фид поставщика Al-Style (sku = Code)
В фид попадают только товары из таблицы. Если источник недоступен или пришёл
подозрительно пустым, скрипт падает и старый kaspi_feed.xml остаётся на месте.
Только стандартная библиотека Python.
"""
import csv
import datetime as dt
import io
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SHEET_ID = "1zladVRO6KAsDp9nxJ15ALcVHts72VlPUUFJNwwrtsJY"
SHEET_CSV_URL = os.environ.get(
    "SHEET_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet=%D0%A2%D0%BE%D0%B2%D0%B0%D1%80%D1%8B",
)
ALSTYLE_URL = os.environ.get("ALSTYLE_URL", "https://wsptech.mktx.io/storage/feeds/alstyle.xml")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "kaspi_feed.xml")

COMPANY = "BSKZ"
MERCHANT_ID = "30037772"
BRAND = "Baseus"
SKU_PREFIX = "BS"
STORE_SUPPLIER = "PP10"
STORE_MANUAL = "PP7"
MIN_SHEET_ROWS = 10
MIN_SUPPLIER_OFFERS = 1000
ALMATY = dt.timezone(dt.timedelta(hours=5))


def fetch(url, src):
    if os.path.exists(url):
        with open(url, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "bskz-kaspi-feed"})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # повтор при сетевом сбое
            last = e
    raise RuntimeError(f"Не удалось скачать {src}: {last}")


def to_int(v):
    s = re.sub(r"[^\d\-]", "", str(v or ""))
    return int(s) if s not in ("", "-") else None


def read_sheet():
    text = fetch(SHEET_CSV_URL, "Google Таблицу").decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise RuntimeError("Google вернул HTML вместо таблицы: проверьте доступ по ссылке.")
    rows = list(csv.DictReader(io.StringIO(text)))
    items = {}
    for r in rows:
        r = {k.strip(): v for k, v in r.items() if k}
        code = re.sub(r"\D", "", str(r.get("Code") or ""))
        price = to_int(r.get("RRP KZ"))
        if not code or not price or price <= 0:
            continue
        items[code] = {
            "name": (r.get("Name") or "").strip(),
            "price": price,
            "pp7": max(to_int(r.get("PP7")) or 0, 0),
        }
    if len(items) < MIN_SHEET_ROWS:
        raise RuntimeError(f"В таблице всего {len(items)} товаров с ценой: фид не обновлён.")
    return items


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


def availability(store, qty):
    if qty > 0:
        return f'<availability available="yes" storeId="{store}" stockCount="{qty}"/>'
    return f'<availability available="no" storeId="{store}"/>'


def build(items, supplier):
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
            "        " + availability(STORE_MANUAL, it["pp7"]),
            "      </availabilities>",
            f"      <price>{it['price']}</price>",
            "    </offer>",
        ]
    out += ["  </offers>", "</kaspi_catalog>", ""]
    return "\n".join(out)


def main():
    items = read_sheet()
    supplier = read_alstyle()
    xml = build(items, supplier)
    ET.fromstring(xml.encode("utf-8"))  # проверка валидности до записи
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
    os.replace(tmp, OUTPUT_FILE)

    missing = [c for c in items if c not in supplier]
    in_stock = sum(1 for c, it in items.items() if supplier.get(c, 0) + it["pp7"] > 0)
    print(f"Товаров: {len(items)}, в наличии: {in_stock}")
    if missing:
        print("Нет в фиде Al-Style:", ", ".join(missing))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ОШИБКА:", e, file=sys.stderr)
        sys.exit(1)
