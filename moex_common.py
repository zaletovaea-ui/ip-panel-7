#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общие настройки и функции для объединённого дашборда открытого интереса
(MX, IMOEXF, CR, Si, BR, RI, SR). Используется и разовым backfill-скриптом
(на компьютере), и update_dashboard.py (в GitHub Actions) — чтобы список
инструментов и логика цены не расходились между ними.
"""
import json
import re
import time
import urllib.request

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

HEADERS = {"User-Agent": "Mozilla/5.0"}

# -----------------------------------------------------------------------
# Инструменты: тикер FUTOI -> с какой даты есть открытый интерес, и какой
# ценовой ряд к нему привязать (price_key -> см. PRICE_SOURCES ниже).
# MX и IMOEXF оба показывают цену индекса IMOEX — он один на двоих.
# -----------------------------------------------------------------------
TICKERS = {
    "MX":     {"label": "Индекс МосБиржи, квартальный фьючерс (MX)",      "oi_start": "2020-01-03", "price_key": "IMOEX"},
    "IMOEXF": {"label": "Индекс МосБиржи, вечный фьючерс (IMOEXF)",        "oi_start": "2023-11-10", "price_key": "IMOEX"},
    "CR":     {"label": "Юань (CR)",                                       "oi_start": "2022-04-21", "price_key": "CNYRUB"},
    "Si":     {"label": "Доллар США (Si)",                                 "oi_start": "2020-01-03", "price_key": "USDRUB"},
    "BR":     {"label": "Нефть Brent (BR)",                                "oi_start": "2020-01-03", "price_key": "BR_NEAR"},
    "RI":     {"label": "Индекс РТС (RI)",                                 "oi_start": "2020-01-03", "price_key": "RTSI"},
    "SR":     {"label": "Сбербанк, ао (SR)",                                "oi_start": "2020-01-03", "price_key": "SBER"},
}

FUTOI_URL = "https://apim.moex.com/iss/analyticalproducts/futoi/securities/{ticker}.json"
PAGE_SIZE = 1000
MAX_RETRIES = 5

# Прямые ряды котировок самого базового актива — бесплатный MOEX ISS,
# токен AlgoPack не нужен.
INDEX_SOURCES = {
    "IMOEX": {"secid": "IMOEX", "board": "SNDX"},
    "RTSI":  {"secid": "RTSI",  "board": "RTSI"},
}
CURRENCY_SOURCES = {
    "USDRUB": {"secid": "USD000UTSTOM", "board": "CETS"},
    "CNYRUB": {"secid": "CNYRUB_TOM",   "board": "CETS"},
}
SHARES_SOURCES = {
    "SBER": {"secid": "SBER", "board": "TQBR"},
}
# BR_NEAR обрабатывается отдельными функциями (нет фиксированного secid —
# это самый ликвидный из торгуемых контрактов BR* на данный момент).
BR_SECID_RE = re.compile(r"^BR[A-Z]\d$")


# ------------------------- базовые HTTP-помощники -----------------------

def http_get_json(url, headers=None, params=None, timeout=20, retries=MAX_RETRIES):
    if params:
        from urllib.parse import urlencode
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 20))
    print(f"  сетевая ошибка на {url}: {last_err}")
    return None


def iss_blocks(payload):
    """Возвращает список (имя_блока, columns, data) для блоков вида {columns, data}."""
    out = []
    if not isinstance(payload, dict):
        return out
    for name, block in payload.items():
        if isinstance(block, dict) and "columns" in block and "data" in block:
            out.append((name, block["columns"], block["data"]))
    return out


def rows_as_dicts(payload, block_name):
    for name, cols, data in iss_blocks(payload):
        if name == block_name:
            return [dict(zip(cols, r)) for r in data]
    return []


# ------------------------------ FUTOI (ОИ) -------------------------------

def futoi_fetch_day(token, ticker, day_str):
    """Один торговый день FUTOI по тикеру (с пагинацией). Список dict-строк."""
    url = FUTOI_URL.format(ticker=ticker)
    headers = {"Authorization": f"Bearer {token}"}
    all_rows = []
    start = 0
    while True:
        payload = http_get_json(url, headers=headers, params={"from": day_str, "till": day_str, "start": start})
        if payload is None:
            break
        rows = rows_as_dicts(payload, "futoi")
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        start += len(rows)
        if start > 100000:
            break
    return all_rows


def futoi_reduce_day(rows, day_str):
    """[day, fiz_long, fiz_short, net_fiz, yur_long, yur_short, net_yur] — последний
    снимок дня по каждой группе клиентов (FIZ/YUR)."""
    if not rows:
        return None
    latest = {}
    for r in rows:
        g = r.get("clgroup")
        if g not in ("FIZ", "YUR"):
            continue
        if g not in latest or str(r.get("tradetime", "")) > str(latest[g].get("tradetime", "")):
            latest[g] = r

    def side(g, col):
        if g not in latest:
            return 0
        v = latest[g].get(col)
        try:
            return int(abs(float(v))) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    fiz_long, fiz_short = side("FIZ", "pos_long"), side("FIZ", "pos_short")
    yur_long, yur_short = side("YUR", "pos_long"), side("YUR", "pos_short")
    return [day_str, fiz_long, fiz_short, fiz_long - fiz_short,
            yur_long, yur_short, yur_long - yur_short]


def futoi_reduce_intraday(rows):
    """[[tradetime, net_fiz, net_yur], ...] — весь сегодняшний день, по каждой
    уникальной 5-минутной точке. Не хранится отдельно — считается с нуля."""
    by_time = {}
    for r in rows:
        g = r.get("clgroup")
        if g not in ("FIZ", "YUR"):
            continue
        t = str(r.get("tradetime", ""))
        by_time.setdefault(t, {})[g] = r

    out = []
    for t in sorted(by_time.keys()):
        snap = by_time[t]

        def net(g):
            if g not in snap:
                return None
            row = snap[g]
            try:
                pl = float(row.get("pos_long"))
                ps = float(row.get("pos_short"))
            except (TypeError, ValueError):
                return None
            return int(abs(pl)) - int(abs(ps))

        out.append([t, net("FIZ"), net("YUR")])
    return out


# ------------------------------ ЦЕНЫ -------------------------------------

def price_live(price_key):
    """Текущее значение цены (float) или None."""
    if price_key in INDEX_SOURCES:
        src = INDEX_SOURCES[price_key]
        url = f"https://iss.moex.com/iss/engines/stock/markets/index/securities/{src['secid']}.json"
        payload = http_get_json(url, retries=2)
        rows = rows_as_dicts(payload, "marketdata") if payload else []
        if rows and rows[0].get("LASTVALUE") is not None:
            return float(rows[0]["LASTVALUE"])
        return None

    if price_key in CURRENCY_SOURCES:
        src = CURRENCY_SOURCES[price_key]
        url = (f"https://iss.moex.com/iss/engines/currency/markets/selt/boards/"
               f"{src['board']}/securities/{src['secid']}.json")
        payload = http_get_json(url, retries=2)
        rows = rows_as_dicts(payload, "marketdata") if payload else []
        if rows:
            v = rows[0].get("LAST") or rows[0].get("WAPRICE") or rows[0].get("CLOSEPRICE")
            return float(v) if v is not None else None
        return None

    if price_key in SHARES_SOURCES:
        src = SHARES_SOURCES[price_key]
        url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
               f"{src['board']}/securities/{src['secid']}.json")
        payload = http_get_json(url, retries=2)
        rows = rows_as_dicts(payload, "marketdata") if payload else []
        if rows:
            v = rows[0].get("LAST") or rows[0].get("WAPRICE")
            return float(v) if v is not None else None
        return None

    if price_key == "BR_NEAR":
        return _br_near_live()

    return None


def price_history_one_day(price_key, day_str):
    """Официальное закрытие за конкретный прошедший день (float) или None."""
    if price_key in INDEX_SOURCES:
        src = INDEX_SOURCES[price_key]
        url = (f"https://iss.moex.com/iss/history/engines/stock/markets/index/boards/"
               f"{src['board']}/securities/{src['secid']}.json")
        payload = http_get_json(url, params={"from": day_str, "till": day_str}, retries=2)
        rows = rows_as_dicts(payload, "history") if payload else []
        if rows and rows[0].get("CLOSE") is not None:
            return float(rows[0]["CLOSE"])
        return None

    if price_key in CURRENCY_SOURCES:
        src = CURRENCY_SOURCES[price_key]
        url = (f"https://iss.moex.com/iss/history/engines/currency/markets/selt/boards/"
               f"{src['board']}/securities/{src['secid']}.json")
        payload = http_get_json(url, params={"from": day_str, "till": day_str}, retries=2)
        rows = rows_as_dicts(payload, "history") if payload else []
        if rows and rows[0].get("CLOSE"):  # 0/None -> нет данных (например, блокировка торгов)
            return float(rows[0]["CLOSE"])
        return None

    if price_key in SHARES_SOURCES:
        src = SHARES_SOURCES[price_key]
        url = (f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/"
               f"{src['board']}/securities/{src['secid']}.json")
        payload = http_get_json(url, params={"from": day_str, "till": day_str}, retries=2)
        rows = rows_as_dicts(payload, "history") if payload else []
        if rows and rows[0].get("CLOSE") is not None:
            return float(rows[0]["CLOSE"])
        return None

    if price_key == "BR_NEAR":
        return _br_near_history_one_day(day_str)

    return None


def _br_front_month_row(rows):
    """Среди строк BR* (без календарных спредов) берёт самый ликвидный
    контракт по открытой позиции — это и есть фактически ближний месяц."""
    candidates = [r for r in rows if BR_SECID_RE.match(str(r.get("SECID", "")))]
    if not candidates:
        return None

    def oi(r):
        for k in ("OPENPOSITION", "PREVOPENPOSITION"):
            v = r.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0

    return max(candidates, key=oi)


def _br_near_live():
    payload = http_get_json("https://iss.moex.com/iss/engines/futures/markets/forts/securities.json", retries=2)
    if payload is None:
        return None
    rows = rows_as_dicts(payload, "marketdata")
    row = _br_front_month_row(rows)
    if row is None:
        return None
    v = row.get("SETTLEPRICE") or row.get("LAST")
    return float(v) if v is not None else None


def _br_near_history_one_day(day_str):
    all_rows = []
    start = 0
    while True:
        payload = http_get_json(
            "https://iss.moex.com/iss/history/engines/futures/markets/forts/securities.json",
            params={"date": day_str, "start": start}, retries=2)
        if payload is None:
            break
        rows = rows_as_dicts(payload, "history")
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 100:
            break
        start += len(rows)
        if start > 5000:
            break
    row = _br_front_month_row(all_rows)
    if row is None:
        return None
    v = row.get("SETTLEPRICE") or row.get("CLOSE")
    return float(v) if v is not None else None
