#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматическое обновление ОБЪЕДИНЁННОГО дашборда открытого интереса по
7 инструментам: MX, IMOEXF, CR, Si, BR, RI, SR (MOEX AlgoPack).
Запускается GitHub Actions каждые 5 минут (см. .github/workflows/update.yml).

Логика одного запуска, для КАЖДОГО тикера из TICKERS (moex_common.py):
1. Забираем сегодняшние данные FUTOI (from=till=сегодня, с пагинацией).
2. Если данных нет вообще (выходной, ночь, перерыв) — пропускаем тикер,
   остальные обрабатываем как обычно.
3. Если сегодня новый торговый день по сравнению с data/live_{TICKER}.csv —
   переносим последний снимок ПРЕДЫДУЩЕГО дня в data/oi_{TICKER}.json,
   начинаем live-файл заново.
4. Иначе — дописываем новые строки в data/live_{TICKER}.csv (дедуп).
5. Считаем LAST_POINT и внутридневной ряд (только для сегодня, нигде не
   хранится отдельно — пересчитывается с нуля каждый раз).

Цены (отдельно от ОИ, по уникальным price_key — IMOEX используется и
для MX, и для IMOEXF, поэтому считается один раз):
  - текущее значение (бесплатный MOEX ISS, без токена) -> LAST_POINT.price
  - при смене дня — официальное закрытие за прошедший день в
    data/price_{PRICE_KEY}.json

В конце — пересобираем index.html из template.html, подставляя единый
__BUNDLE_JSON__ со всеми 7 инструментами, и это коммитит workflow.
"""
import json
import os
import sys
import time
from datetime import date

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import pandas as pd

from moex_common import TICKERS, futoi_fetch_day, futoi_reduce_day, futoi_reduce_intraday, price_live, price_history_one_day

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
TEMPLATE_HTML = os.path.join(ROOT, "template.html")
OUTPUT_HTML = os.path.join(ROOT, "index.html")

PRICE_LABELS = {
    "IMOEX":  "Индекс МосБиржи (IMOEX)",
    "RTSI":   "Индекс РТС (RTSI)",
    "USDRUB": "Курс USD/RUB",
    "CNYRUB": "Курс CNY/RUB",
    "SBER":   "Акции Сбербанка (SBER)",
    "BR_NEAR": "Ближайший фьючерс на нефть Brent (расчётная цена)",
}
FUTOI_NOTE = {
    "MX":     "тикер MX (квартальный фьючерс на Индекс МосБиржи)",
    "IMOEXF": "тикер IMOEXF (вечный фьючерс на Индекс МосБиржи, торгуется с 10.11.2023)",
    "CR":     "тикер CR (юань, торгуется с 2022)",
    "Si":     "тикер Si (доллар США)",
    "BR":     "тикер BR (нефть Brent)",
    "RI":     "тикер RI (индекс РТС)",
    "SR":     "тикер SR (Сбербанк, ао)",
}


def get_token():
    token = os.environ.get("MOEX_TOKEN")
    if not token:
        sys.exit("Нет переменной окружения MOEX_TOKEN (секрет репозитория). Прерываю.")
    return token.strip()


def live_csv_path(ticker):
    return os.path.join(DATA_DIR, f"live_{ticker}.csv")


def oi_json_path(ticker):
    return os.path.join(DATA_DIR, f"oi_{ticker}.json")


def price_json_path(price_key):
    return os.path.join(DATA_DIR, f"price_{price_key}.json")


def load_existing_live(ticker):
    path = live_csv_path(ticker)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, dtype=str)
            if not df.empty:
                return df
        except Exception as e:
            print(f"[{ticker}] не смог прочитать {path}: {e}")
    return pd.DataFrame()


def append_oi_history(ticker, day_str, row):
    path = oi_json_path(ticker)
    history = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            print(f"[{ticker}] не смог прочитать {path}: {e}")
    if history and history[-1][0] == day_str:
        print(f"[{ticker}] {day_str} уже есть в истории, пропускаю дублирование.")
        return
    history.append(row)
    history.sort(key=lambda r: r[0])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[{ticker}] добавил {day_str} в {path}. Всего дней: {len(history)}")


def load_price_history(price_key):
    path = price_json_path(price_key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[цена {price_key}] не смог прочитать {path}: {e}")
    return []


def append_price_history(price_key, day_str, value):
    if value is None:
        print(f"[цена {price_key}] нет значения за {day_str} — не пополняю.")
        return
    history = load_price_history(price_key)
    if history and history[-1][0] == day_str:
        return
    history.append([day_str, value])
    history.sort(key=lambda r: r[0])
    with open(price_json_path(price_key), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[цена {price_key}] добавил {day_str} ({value}). Всего дней: {len(history)}")


def compute_last_point(df):
    df = df.drop_duplicates(subset=["tradetime", "clgroup"], keep="last").copy()
    df["tradetime"] = df["tradetime"].astype(str)
    for col in ("pos_long", "pos_short"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    times = sorted(df["tradetime"].unique())
    if not times:
        return None
    last_t = times[-1]
    prev_t = times[-2] if len(times) >= 2 else times[-1]

    def snap(t):
        sub = df[df["tradetime"] == t].set_index("clgroup")
        out = {}
        for g in ("FIZ", "YUR"):
            if g not in sub.index:
                out[g] = {"long": 0, "short": 0}
                continue
            row = sub.loc[g]
            out[g] = {"long": int(abs(row["pos_long"])), "short": int(abs(row["pos_short"]))}
        return out

    last, prev = snap(last_t), snap(prev_t)
    tradedate = str(df["tradedate"].iloc[-1])
    result = {"tradedate": tradedate, "last_time": last_t, "prev_time": prev_t, "groups": {}}
    for g in ("FIZ", "YUR"):
        l, p = last[g], prev[g]
        result["groups"][g] = {
            "long": l["long"], "short": l["short"],
            "prev_long": p["long"], "prev_short": p["short"],
            "net": l["long"] - l["short"], "prev_net": p["long"] - p["short"],
        }
    return result


def process_ticker(token, ticker, today_str, price_cache):
    """Возвращает (last_point, intraday) для тикера, или (None, []) если
    сегодня по нему вообще нет данных."""
    cfg = TICKERS[ticker]
    price_key = cfg["price_key"]

    rows = futoi_fetch_day(token, ticker, today_str)
    if not rows:
        print(f"[{ticker}] данных за {today_str} нет (выходной/до открытия/перерыв).")
        # отдаём последнее, что есть, чтобы дашборд не падал в пустоту
        return None, []

    df_new = pd.DataFrame(rows)
    df_new["tradedate"] = df_new["tradedate"].astype(str)

    df_existing = load_existing_live(ticker)
    if not df_existing.empty:
        old_day = str(df_existing["tradedate"].iloc[0])
        if old_day != today_str:
            print(f"[{ticker}] новый торговый день: {old_day} -> {today_str}.")
            old_row = futoi_reduce_day(df_existing.to_dict("records"), old_day)
            if old_row is not None:
                append_oi_history(ticker, old_day, old_row)
            if price_key not in price_cache.get("_history_done", set()):
                close_val = price_history_one_day(price_key, old_day)
                append_price_history(price_key, old_day, close_val)
            df_existing = pd.DataFrame()

    df_all = df_new if df_existing.empty else pd.concat([df_existing, df_new], ignore_index=True)
    df_all = df_all.drop_duplicates(subset=list(df_all.columns), keep="last")

    os.makedirs(DATA_DIR, exist_ok=True)
    df_all.to_csv(live_csv_path(ticker), index=False)

    last_point = compute_last_point(df_all)
    intraday = futoi_reduce_intraday(df_all.to_dict("records"))
    return last_point, intraday


def rebuild_html(bundle):
    with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
        tpl = f.read()
    out = tpl.replace("__BUNDLE_JSON__", json.dumps(bundle, ensure_ascii=False))
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Пересобрал {OUTPUT_HTML}")


def main():
    token = get_token()
    today_str = date.today().isoformat()

    # отдельный "кэш" цен за этот запуск, чтобы не бить по API 2 раза ради
    # одного и того же price_key (IMOEX нужен и MX, и IMOEXF)
    price_cache = {"live": {}, "_history_done": set()}

    def get_live_price(price_key):
        if price_key not in price_cache["live"]:
            price_cache["live"][price_key] = price_live(price_key)
        return price_cache["live"][price_key]

    bundle = {}
    for ticker, cfg in TICKERS.items():
        price_key = cfg["price_key"]
        try:
            last_point, intraday = process_ticker(token, ticker, today_str, price_cache)
        except Exception as e:
            print(f"[{ticker}] ОШИБКА при обработке: {e} — пропускаю этот тикер в этом запуске.")
            last_point, intraday = None, []
        price_cache["_history_done"].add(price_key)

        if last_point is not None:
            live_price = get_live_price(price_key)
            if live_price is not None:
                last_point["price"] = live_price

        oi_history = []
        if os.path.exists(oi_json_path(ticker)):
            with open(oi_json_path(ticker), "r", encoding="utf-8") as f:
                oi_history = json.load(f)

        if last_point is None:
            # нечего показать за сегодня — используем последний известный
            # снимок из истории, чтобы страница не ломалась
            if not oi_history:
                print(f"[{ticker}] нет вообще никаких данных (ни сегодня, ни в истории) — пропускаю тикер.")
                continue
            last_row = oi_history[-1]
            last_point = {
                "tradedate": last_row[0], "last_time": "00:00:00", "prev_time": "00:00:00",
                "groups": {
                    "FIZ": {"long": last_row[1], "short": last_row[2], "prev_long": last_row[1],
                            "prev_short": last_row[2], "net": last_row[3], "prev_net": last_row[3]},
                    "YUR": {"long": last_row[4], "short": last_row[5], "prev_long": last_row[4],
                            "prev_short": last_row[5], "net": last_row[6], "prev_net": last_row[6]},
                },
                "price": get_live_price(price_key),
            }

        price_history = load_price_history(price_key)

        bundle[ticker] = {
            "label": cfg["label"],
            "priceLabel": PRICE_LABELS.get(price_key, price_key),
            "footerNote": ("Источник: MOEX AlgoPack, эндпойнт FUTOI (открытый интерес по срочному рынку), "
                           f"{FUTOI_NOTE.get(ticker, ticker)}. Цена — {PRICE_LABELS.get(price_key, price_key)}. "
                           "Обновляется автоматически, обычно раз в 5–15 минут."),
            "data": oi_history,
            "price": price_history,
            "lastPoint": last_point,
            "intraday": intraday,
        }
        time.sleep(0.1)

    if not bundle:
        print("Не удалось получить данные ни по одному тикеру в этом запуске, выхожу без изменений.")
        return

    rebuild_html(bundle)


if __name__ == "__main__":
    main()
