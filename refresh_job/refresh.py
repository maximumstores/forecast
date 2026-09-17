"""
Ежедневное обновление данных forecast.

Что делает:
  1. Выгружает четыре листа Google Sheets в CSV (через export?format=csv).
  2. Перезаливает их в нативные таблицы mt.*_native.
  3. Вызывает forecast.sp_refresh_gold() — пересборка всего gold-слоя.

Почему через CSV, а не external tables: тяжёлые листы отдаются BigQuery
по 250+ секунд и падают с "Google Sheets service overloaded". Экспорт
файлом обходит это ограничение.

Права: сервис-аккаунту нужны roles/bigquery.jobUser + dataEditor на проект,
и оба документа должны быть расшарены на его адрес (Viewer).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

import google.auth
import google.auth.transport.requests
import requests
from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "reorder-497714")
LOCATION = "EU"

SHEET_SALES = "1rIUaZShBOVl7-zN_tzgJUle3vZukujrbJL40V6EaWIY"
SHEET_SPR = "1-vtLKK5KBfE7S8Ho_1xCCwg_eXUmdKd7ElWWIufj9Rs"

# (таблица назначения, id документа, gid листа, символ кавычки, кавычки с переносами)
#
# quote_char = '"'  — обычный CSV
# quote_char = ""   — кавычки не обрабатываются; нужно для листов, где формулы
#                     вернули #REF! и кавычки внутри полей стоят непарно
SOURCES = [
    ("mt.sales_us_fact_native",  SHEET_SALES, "2127822292", '"', False),
    ("mt.sales_oos_amz_native",  SHEET_SALES, "404969167",  "",  False),
    ("mt.sales_plus_oos_native", SHEET_SALES, "1547531617", "",  False),
    ("mt.SPR_native",            SHEET_SPR,   "2096733449", '"', True),
]

# ожидаемое число строк — если после загрузки сильно меньше, значит формат уехал
MIN_ROWS = {
    "mt.sales_us_fact_native": 5000,
    "mt.sales_oos_amz_native": 5000,
    "mt.sales_plus_oos_native": 5000,
    "mt.SPR_native": 5000,
}

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Google жёстко троттлит экспорт Sheets: несколько файлов подряд из одного
# документа почти всегда дают 400. Помогают только длинные паузы.
RETRIES = 5
RETRY_WAIT = 120        # между попытками одного листа
PAUSE_BETWEEN = 60      # между разными листами

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("refresh")


def get_token() -> str:
    creds, _ = google.auth.default(scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def download_sheet(sheet_id: str, gid: str, path: str, token: str) -> int:
    """Скачивает лист в CSV. Возвращает размер файла в байтах."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=600
    )
    resp.raise_for_status()

    head = resp.content[:200].lstrip()
    if head.startswith(b"<!DOCTYPE") or head.startswith(b"<html"):
        raise RuntimeError(
            f"Вместо CSV пришёл HTML (лист {gid}). Проверьте доступ "
            f"сервис-аккаунта к документу {sheet_id}."
        )

    with open(path, "wb") as fh:
        fh.write(resp.content)
    return len(resp.content)


def load_csv(client: bigquery.Client, table: str, path: str,
             quote_char: str, quoted_newlines: bool) -> int:
    """Перезаливает CSV в нативную таблицу. Возвращает число строк."""
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=0,
        autodetect=True,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        allow_quoted_newlines=quoted_newlines,
        quote_character=quote_char,
    )
    with open(path, "rb") as fh:
        job = client.load_table_from_file(
            fh, f"{PROJECT}.{table}", job_config=cfg, location=LOCATION
        )
    job.result()
    return client.get_table(f"{PROJECT}.{table}").num_rows


def with_retries(fn, what: str):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("%s — попытка %d/%d не удалась: %s",
                        what, attempt, RETRIES, exc)
            if attempt < RETRIES:
                wait = RETRY_WAIT * attempt      # 120, 240, 360, 480 с
                log.info("жду %d с перед следующей попыткой", wait)
                time.sleep(wait)
    raise RuntimeError(f"{what}: не удалось после {RETRIES} попыток") from last


def main() -> int:
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    token = get_token()
    tmp = tempfile.mkdtemp()

    for table, sheet_id, gid, quote_char, quoted in SOURCES:
        path = os.path.join(tmp, f"{table.split('.')[-1]}.csv")

        size = with_retries(
            lambda: download_sheet(sheet_id, gid, path, token),
            f"выгрузка {table}",
        )
        log.info("%s: скачано %.1f МБ", table, size / 1024 / 1024)

        rows = with_retries(
            lambda: load_csv(client, table, path, quote_char, quoted),
            f"загрузка {table}",
        )
        log.info("%s: загружено строк — %d", table, rows)

        if rows < MIN_ROWS.get(table, 100):
            raise RuntimeError(
                f"{table}: строк {rows}, ожидалось не меньше "
                f"{MIN_ROWS.get(table, 100)} — формат уехал, пересборку не запускаю"
            )

        if table != SOURCES[-1][0]:
            log.info("пауза %d с перед следующим листом", PAUSE_BETWEEN)
            time.sleep(PAUSE_BETWEEN)

    # источники загружены — проверяем, что silver-слой их видит
    checks = [
        ("fact_sales_monthly", 100000),
        ("oos_monthly", 100000),
        ("dim_product", 5000),
        ("group_demand_monthly", 1000),
    ]
    for view, minimum in checks:
        n = list(client.query(
            f"SELECT COUNT(*) AS n FROM `{PROJECT}.forecast.{view}`",
            location=LOCATION,
        ).result())[0].n
        log.info("%s: строк %d", view, n)
        if n < minimum:
            raise RuntimeError(
                f"forecast.{view}: строк {n}, ожидалось не меньше {minimum} — "
                "источник распарсился неверно, пересборку не запускаю"
            )

    log.info("Запускаю sp_refresh_gold — это несколько минут")
    client.query(
        f"CALL `{PROJECT}.forecast.sp_refresh_gold`()", location=LOCATION
    ).result()

    check = list(client.query(
        f"""
        SELECT kind, MAX(month) AS mx, COUNT(*) AS n
        FROM `{PROJECT}.forecast.looker_fact_monthly`
        GROUP BY kind
        """,
        location=LOCATION,
    ).result())
    for row in check:
        log.info("%s: до %s, строк %d", row.kind, row.mx, row.n)

    log.info("Готово")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log.error("Обновление не прошло: %s", exc)
        sys.exit(1)