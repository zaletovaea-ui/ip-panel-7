#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматическое обновление дашборда открытого интереса MX (MOEX AlgoPack).
Запускается GitHub Actions каждые 5 минут (см. .github/workflows/update.yml).

Логика одного запуска:
1. Забираем сегодняшние данные FUTOI по тикеру MX (from=till=сегодня, с пагинацией,
   тот же метод, что и в рабочем скрипте fetch_futoi_history.py).
2. Если сегодня новый торговый день по сравнению с тем, что лежит в
   data/futoi_MX_live.csv — переносим последний снимок ПРЕДЫДУЩЕГО дня в
   data/data.json (историю по дням) и начинаем live-файл заново.
3. Иначе — дописываем новые строки в data/futoi_MX_live.csv (дедуп по всем полям,
   как в живом сборщике на компьютере).
4. Если данных нет вообще (выходной, ночь, перерыв) — тихо выходим без изменений.
5. Считаем LAST_POINT (две последние 5-минутные точки) и пересобираем index.html
   из template.html + data/data.json + last_point.
6. Дальше это в workflow: git add/commit/push, если что-то изменилось.
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

import requests
import pandas as pd

TICKER = "MX"
BASE_URL = f"https://apim.moex.com/iss/analyticalproducts/futoi/securities/{TICKER}.json"
PAGE_SIZE = 1000
MAX_RETRIES = 5

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
LIVE_CSV = os.path.join(DATA_DIR, f"futoi_{TICKER}_live.csv")
DATA_JSON = os.path.join(DATA_DIR, "data.json")
TEMPLATE_HTML = os.path.join(ROOT, "template.html")
OUTPUT_HTML = os.path.join(ROOT, "index.html")


def get_token():
    token = os.environ.get("MOEX_TOKEN")
    if not token:
        sys.exit("Нет переменной окружения MOEX_TOKEN (секрет репозитория). Прерываю.")
    return token.strip()


def _request_page(session, headers, params, day_str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(BASE_URL, headers=headers, params=params, timeout=20)
        except requests.RequestException as e:
            wait = min(2 ** attempt, 20)
            print(f"[{day_str}] сетевая ошибка: {e}. Повтор через {wait}с...")
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            sys.exit(f"[{day_str}] 401 Unauthorized — токен MOEX недействителен/просрочен. "
                      "Нужно взять новый в личном кабинете AlgoPack и обновить секрет MOEX_TOKEN.")
        if resp.status_code == 429:
            wait = min(5 * attempt, 40)
            print(f"[{day_str}] 429 Too Many Requests. Пауза {wait}с...")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = min(2 ** attempt, 20)
            print(f"[{day_str}] {resp.status_code} ошибка сервера. Повтор через {wait}с...")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            print(f"[{day_str}] неожиданный статус {resp.status_code}: {resp.text[:300]}")
            return None
        try:
            return resp.json()
        except ValueError:
            print(f"[{day_str}] ответ не в формате JSON: {resp.text[:300]}")
            return None
    print(f"[{day_str}] не удалось получить данные после {MAX_RETRIES} попыток.")
    return None


def extract_rows(payload):
    rows = []
    if not isinstance(payload, dict):
        return rows
    for _, block in payload.items():
        if isinstance(block, dict) and "columns" in block and "data" in block:
            cols = block["columns"]
            if "tradedate" not in cols:
                continue
            for raw in block["data"]:
                rows.append(dict(zip(cols, raw)))
    return rows


def fetch_today(session, token, day_str):
    headers = {"Authorization": f"Bearer {token}"}
    all_rows = []
    start = 0
    while True:
        params = {"from": day_str, "till": day_str, "start": start}
        payload = _request_page(session, headers, params, day_str)
        if payload is None:
            break
        rows = extract_rows(payload)
        all_rows.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        start += len(rows)
        if start > 100000:
            break
    return all_rows


def load_existing_live():
    if os.path.exists(LIVE_CSV):
        try:
            df = pd.read_csv(LIVE_CSV, dtype=str)
            if not df.empty:
                return df
        except Exception as e:
            print(f"Не смог прочитать {LIVE_CSV}: {e}")
    return pd.DataFrame()


def finalize_day_into_history(df_old_day):
    """Берёт последний снимок дня (по каждой группе) из df_old_day и
    добавляет строку в data/data.json, если такой даты там ещё нет."""
    if df_old_day.empty:
        return
    df = df_old_day.copy()
    for col in ("pos_long", "pos_short"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["tradetime"] = df["tradetime"].astype(str)
    old_day = str(df["tradedate"].iloc[0])

    last = df.sort_values("tradetime").groupby("clgroup", as_index=False).last()
    last = last.set_index("clgroup")

    def side(group, col):
        if group not in last.index:
            return 0
        v = last.loc[group, col]
        return int(abs(v)) if pd.notna(v) else 0

    fiz_long = side("FIZ", "pos_long")
    fiz_short = side("FIZ", "pos_short")
    yur_long = side("YUR", "pos_long")
    yur_short = side("YUR", "pos_short")
    net_fiz = fiz_long - fiz_short
    net_yur = yur_long - yur_short

    with open(DATA_JSON, "r", encoding="utf-8") as f:
        history = json.load(f)

    if history and history[-1][0] == old_day:
        print(f"{old_day} уже есть в истории, пропускаю дублирование.")
        return

    history.append([old_day, fiz_long, fiz_short, net_fiz, yur_long, yur_short, net_yur])
    history.sort(key=lambda row: row[0])

    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Добавил {old_day} в историю (data/data.json). Всего дней: {len(history)}")


def compute_last_point(df):
    """Две последние 5-минутные точки (дедуп по tradetime+clgroup) -> LAST_POINT dict."""
    df = df.drop_duplicates(subset=["tradetime", "clgroup"], keep="last").copy()
    df["tradetime"] = df["tradetime"].astype(str)
    for col in ("pos_long", "pos_short"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    times = sorted(df["tradetime"].unique())
    if len(times) < 1:
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

    last = snap(last_t)
    prev = snap(prev_t)
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


def rebuild_html(last_point):
    with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(DATA_JSON, "r", encoding="utf-8") as f:
        data_json = f.read()
    last_point_json = json.dumps(last_point, ensure_ascii=False)
    out = tpl.replace("__DATA_JSON__", data_json.strip()).replace("__LAST_POINT_JSON__", last_point_json)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Пересобрал {OUTPUT_HTML}")


def main():
    token = get_token()
    today_str = date.today().isoformat()

    session = requests.Session()
    new_rows = fetch_today(session, token, today_str)

    if not new_rows:
        print(f"[{today_str}] данных нет (выходной/до открытия/перерыв) — выхожу без изменений.")
        return

    df_new = pd.DataFrame(new_rows)
    df_new["tradedate"] = df_new["tradedate"].astype(str)

    df_existing = load_existing_live()

    if not df_existing.empty:
        old_day = str(df_existing["tradedate"].iloc[0])
        if old_day != today_str:
            print(f"Новый торговый день: {old_day} -> {today_str}. Переношу {old_day} в историю.")
            finalize_day_into_history(df_existing)
            df_existing = pd.DataFrame()  # начинаем live-файл заново

    df_all = df_new if df_existing.empty else pd.concat([df_existing, df_new], ignore_index=True)
    df_all = df_all.drop_duplicates(subset=list(df_all.columns), keep="last")

    os.makedirs(DATA_DIR, exist_ok=True)
    df_all.to_csv(LIVE_CSV, index=False)
    print(f"Сохранил {LIVE_CSV}: {len(df_all)} строк.")

    last_point = compute_last_point(df_all)
    if last_point is None:
        print("Не удалось посчитать LAST_POINT (пустые данные), выхожу.")
        return

    rebuild_html(last_point)


if __name__ == "__main__":
    main()
