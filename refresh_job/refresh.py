"""
Ежедневное обновление данных forecast.

Что делает:
  1. Выгружает листы Google Sheets в CSV (через export?format=csv).
  2. Перезаливает их в нативные таблицы mt.*_native.
  3. Проверяет, что silver-слой видит данные и сток не пустой.
  4. Вызывает forecast.sp_refresh_gold() — пересборка всего gold-слоя.

Почему через CSV, а не external tables: тяжёлые листы отдаются BigQuery
по 250+ секунд и падают с "Google Sheets service overloaded". Экспорт
файлом обходит это ограничение.

Два режима загрузки:
  * "auto"   — колонки string_field_N, как у старых источников;
  * "letter" — колонки col_A, col_B … ровно по диапазону листа, как было у
               external-таблицы. От этих имён зависят вьюхи stock_current,
               group_cogs, legacy_*, поэтому ширину обрезаем/добиваем точно.

Права: сервис-аккаунту нужны roles/bigquery.jobUser + dataEditor на проект,
все документы расшарены на его адрес (Viewer), включён Sheets API.
"""

from __future__ import annotations

import csv
import io
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
SHEET_ORDERS_STOCK = "1LNJGa_89vUqSUJZ6CEI2MA5iAmvPMrQ-M74kFcCA_LU"
SHEET_ISHODNIK = "1b3RWGwKP3_g_bA8JCuc6-XHCTRw3KiPlW8ROIUFH9qs"

# Источник: таблица, документ, лист (gid или имя), режим, кавычки,
#           кавычки с переносами, ширина для режима letter.
#
# Лист задаётся либо gid (строка из цифр), либо именем вкладки — тогда gid
# находится через Sheets API. По имени надёжнее: gid меняется, если вкладку
# пересоздали, а имя обычно нет.
#
# quote_char = '"' — обычный CSV
# quote_char = ""  — кавычки не обрабатываются; для листов, где формулы
#                    вернули #REF! и кавычки внутри полей стоят непарно
SOURCES = [
    # table,                          doc,                sheet,                          mode,     quote, nl,    width
    ("mt.sales_us_fact_native",       SHEET_SALES,        "2127822292",                   "auto",   '"',   False, None),
    ("mt.sales_oos_amz_native",       SHEET_SALES,        "404969167",                    "auto",   "",    False, None),
    ("mt.sales_plus_oos_native",      SHEET_SALES,        "1547531617",                   "auto",   "",    False, None),
    ("mt.SPR_native",                 SHEET_SPR,          "2096733449",                   "auto",   '"',   True,  None),
    # сток на руках и себестоимость — диапазон A:AM (39 колонок)
    ("mt.amazon_starting_balance_native", SHEET_ORDERS_STOCK,
                                      "Starting Amazon month Balance", "letter", '"',  True,  39),
    # legacy-план, приходы и старый сток — диапазон A:JC (263 колонки)
    ("mt.ishodnik_native",            SHEET_ISHODNIK,     "исходник",                     "letter", '"',   True,  263),
    # сырой сток из Hopted (Manage FBA Inventory) — меняется каждый день,
    # в расчёт пока не идёт, нужен для истории остатков
    ("mt.fba_stock_native",           SHEET_ORDERS_STOCK, "213063900",                    "auto",   '"',   True,  None),
]

# Листы, срез которых сохраняется в mt.stock_history каждый день.
# Строка хранится целиком как JSON — история не ломается, если в листе
# добавят или переставят колонки.
HISTORY_SOURCES = [
    "mt.fba_stock_native",                  # ежедневный сток из Amazon
    "mt.amazon_starting_balance_native",    # остаток на начало месяца
]

# Проверка содержимого сразу после загрузки. Если не прошла — лист
# выгружается заново: формулы в справочнике иногда не успевают досчитаться
# к моменту экспорта, и колонка приезжает пустой. Через пару минут обычно
# всё на месте.
# Последнее поле — строгая ли проверка. Строгая останавливает пересборку,
# мягкая только пишет предупреждение.
#   * стартовый остаток — главный источник стока для плана, строгая;
#   * Stock US в справочнике — запасной, план считается и без него, мягкая.
CONTENT_CHECKS = {
    "mt.SPR_native": (
        "SELECT COUNTIF(SAFE_CAST(string_field_38 AS NUMERIC) > 0) "
        "FROM `{table}`",
        1000,
        "товаров с остатком в Stock US",
        False,
    ),
    "mt.amazon_starting_balance_native": (
        "SELECT COUNTIF(SAFE_CAST(col_F AS NUMERIC) > 0) FROM `{table}`",
        1000,
        "товаров с остатком",
        True,
    ),
}
CONTENT_RETRIES = 3
CONTENT_WAIT = 240      # секунд между повторными выгрузками

# ожидаемое число строк — если после загрузки сильно меньше, значит формат уехал
MIN_ROWS = {
    "mt.sales_us_fact_native": 5000,
    "mt.sales_oos_amz_native": 5000,
    "mt.sales_plus_oos_native": 5000,
    "mt.SPR_native": 5000,
    "mt.amazon_starting_balance_native": 5000,
    "mt.ishodnik_native": 10000,
    "mt.fba_stock_native": 500,
}

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Google жёстко троттлит экспорт Sheets: несколько файлов подряд из одного
# документа почти всегда дают 400. Помогают только длинные паузы.
RETRIES = 5
RETRY_WAIT = 120        # между попытками одного листа, растёт с каждой
PAUSE_BETWEEN = 60      # между разными листами

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("refresh")


# ------------------------------------------------------------------ утилиты
def col_letters(n: int) -> list[str]:
    """Имена колонок как в Google Sheets: A … Z, AA … AZ, BA …"""
    out = []
    for i in range(n):
        s, x = "", i
        while True:
            s = chr(ord("A") + x % 26) + s
            x = x // 26 - 1
            if x < 0:
                break
        out.append(s)
    return out


def get_token() -> str:
    creds, _ = google.auth.default(scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def resolve_gid(doc_id: str, sheet: str, token: str) -> str:
    """Если лист задан именем — находит его gid через Sheets API."""
    if sheet.isdigit():
        return sheet
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{doc_id}",
        params={"fields": "sheets.properties(sheetId,title)"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    for s in resp.json().get("sheets", []):
        p = s["properties"]
        if p["title"] == sheet:
            return str(p["sheetId"])
    raise RuntimeError(f"В документе {doc_id} нет листа «{sheet}»")


def download_sheet(doc_id: str, gid: str, path: str, token: str) -> int:
    """Скачивает лист в CSV. Редирект на googleusercontent идёт без
    Authorization: ссылка уже подписана, а лишний токен даёт 400."""
    url = (f"https://docs.google.com/spreadsheets/d/{doc_id}"
           f"/export?format=csv&gid={gid}")
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                        timeout=600, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        resp = requests.get(resp.headers["Location"], timeout=600)
    resp.raise_for_status()

    head = resp.content[:200].lstrip()
    if head.startswith(b"<!DOCTYPE") or head.startswith(b"<html"):
        raise RuntimeError(
            f"Вместо CSV пришёл HTML (лист {gid}). Проверьте доступ "
            f"сервис-аккаунта к документу {doc_id}."
        )
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return len(resp.content)


def reshape_to_letters(path: str, width: int) -> str:
    """Обрезает или добивает каждую строку ровно до width колонок и
    переписывает CSV с полным экранированием — BigQuery получает ровную
    таблицу независимо от того, сколько лишних колонок было в листе."""
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    for r in rows:
        r = (r + [""] * width)[:width]
        w.writerow(r)
    out = path + ".letters.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    return out


def load_csv(client: bigquery.Client, table: str, path: str, mode: str,
             quote_char: str, quoted_newlines: bool,
             width: int | None) -> int:
    """Перезаливает CSV в нативную таблицу. Возвращает число строк."""
    if mode == "letter":
        path = reshape_to_letters(path, width)
        cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=0,
            autodetect=False,
            schema=[bigquery.SchemaField(f"col_{c}", "STRING")
                    for c in col_letters(width)],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            allow_quoted_newlines=True,
            quote_character='"',
        )
    else:
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
            fh, f"{PROJECT}.{table}", job_config=cfg, location=LOCATION)
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


def count(client: bigquery.Client, sql: str) -> int:
    return list(client.query(sql, location=LOCATION).result())[0][0]


def save_history(client: bigquery.Client) -> None:
    """Дописывает сегодняшний срез в mt.stock_history. Повторный запуск в тот
    же день заменяет срез, а не дублирует. Сравнивает со вчерашним: если
    сток не изменился ни в одной строке — это похоже на замороженный
    источник, пишем предупреждение."""
    client.query(
        f"""
        CREATE TABLE IF NOT EXISTS `{PROJECT}.mt.stock_history` (
          snapshot_date DATE      NOT NULL,
          source        STRING    NOT NULL,
          row_json      STRING,
          loaded_at     TIMESTAMP
        )
        PARTITION BY snapshot_date
        OPTIONS (description = 'Ежедневные срезы листов стока. Строка — JSON.')
        """,
        location=LOCATION,
    ).result()

    for table in HISTORY_SOURCES:
        src_name = table.split(".")[-1]
        client.query(
            f"""
            DELETE FROM `{PROJECT}.mt.stock_history`
            WHERE snapshot_date = CURRENT_DATE('Europe/Kyiv')
              AND source = '{src_name}';

            INSERT INTO `{PROJECT}.mt.stock_history`
              (snapshot_date, source, row_json, loaded_at)
            SELECT CURRENT_DATE('Europe/Kyiv'), '{src_name}',
                   TO_JSON_STRING(t), CURRENT_TIMESTAMP()
            FROM `{PROJECT}.{table}` t;
            """,
            location=LOCATION,
        ).result()

        # сравнение со вчерашним срезом
        row = list(client.query(
            f"""
            WITH today AS (
              SELECT row_json FROM `{PROJECT}.mt.stock_history`
              WHERE snapshot_date = CURRENT_DATE('Europe/Kyiv')
                AND source = '{src_name}'
            ),
            prev AS (
              SELECT row_json FROM `{PROJECT}.mt.stock_history`
              WHERE source = '{src_name}'
                AND snapshot_date = (
                  SELECT MAX(snapshot_date) FROM `{PROJECT}.mt.stock_history`
                  WHERE source = '{src_name}'
                    AND snapshot_date < CURRENT_DATE('Europe/Kyiv'))
            )
            SELECT
              (SELECT COUNT(*) FROM today)                              AS n_today,
              (SELECT COUNT(*) FROM prev)                               AS n_prev,
              (SELECT COUNT(*) FROM today
                 WHERE row_json NOT IN (SELECT row_json FROM prev))     AS changed
            """,
            location=LOCATION,
        ).result())[0]

        log.info("история %s: срез %d строк, вчера %d, изменилось %d",
                 src_name, row.n_today, row.n_prev, row.changed)
        if (src_name == "fba_stock_native" and row.n_prev > 0
                and row.changed == 0):
            log.warning("история %s: ни одна строка не изменилась со вчера — "
                        "похоже, Hopted перестал обновлять лист", src_name)


# ------------------------------------------------------------------ main
def main() -> int:
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    token = get_token()
    tmp = tempfile.mkdtemp()

    for i, (table, doc, sheet, mode, quote, nl, width) in enumerate(SOURCES):
        gid = with_retries(lambda: resolve_gid(doc, sheet, token),
                           f"поиск листа {table}")
        path = os.path.join(tmp, f"{table.split('.')[-1]}.csv")

        check = CONTENT_CHECKS.get(table)
        for content_try in range(1, CONTENT_RETRIES + 1):
            size = with_retries(
                lambda: download_sheet(doc, gid, path, token),
                f"выгрузка {table}")
            log.info("%s: скачано %.1f МБ", table, size / 1024 / 1024)

            rows = with_retries(
                lambda: load_csv(client, table, path, mode, quote, nl, width),
                f"загрузка {table}")
            log.info("%s: загружено строк — %d", table, rows)

            if not check:
                break
            sql, minimum, label, strict = check
            got = count(client, sql.format(table=f"{PROJECT}.{table}"))
            log.info("%s: %s — %d", table, label, got)
            if got >= minimum:
                break
            if content_try < CONTENT_RETRIES:
                log.warning("%s: %s %d меньше %d — формулы не досчитались, "
                            "выгружу заново через %d с",
                            table, label, got, minimum, CONTENT_WAIT)
                time.sleep(CONTENT_WAIT)
                token = get_token()
            elif strict:
                raise RuntimeError(
                    f"{table}: {label} {got} после {CONTENT_RETRIES} попыток, "
                    f"ожидалось не меньше {minimum} — источник пустой, "
                    "пересборку не запускаю")
            else:
                log.warning("%s: %s %d после %d попыток — колонка в источнике "
                            "пустая. Это запасной источник, продолжаю без него.",
                            table, label, got, CONTENT_RETRIES)

        minimum = MIN_ROWS.get(table, 100)
        if rows < minimum:
            raise RuntimeError(
                f"{table}: строк {rows}, ожидалось не меньше {minimum} — "
                "формат уехал, пересборку не запускаю")

        if i < len(SOURCES) - 1:
            log.info("пауза %d с перед следующим листом", PAUSE_BETWEEN)
            time.sleep(PAUSE_BETWEEN)

    # ---------------------------------------------- история стока
    # не критично: если срез не сохранился, пересборку не останавливаем
    try:
        save_history(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("срез истории не сохранился: %s", exc)

    # ---------------------------------------------- проверки silver-слоя
    checks = [
        ("строк в fact_sales_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.fact_sales_monthly`", 100000),
        ("строк в oos_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.oos_monthly`", 100000),
        ("строк в dim_product",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.dim_product`", 5000),
        ("строк в group_demand_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.group_demand_monthly`", 1000),
        # свежесть стока: если источник приехал пустым или колонка слетела,
        # план посчитается без остатка — такого не пропускаем
        ("ASIN с остатком в stock_current",
         f"SELECT COUNTIF(stock_us > 0) FROM `{PROJECT}.forecast.stock_current`", 1000),
        ("строк в legacy_incoming",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.legacy_incoming`", 1000),
    ]
    for label, sql, minimum in checks:
        n = count(client, sql)
        log.info("%s: %d", label, n)
        if n < minimum:
            raise RuntimeError(
                f"{label}: {n}, ожидалось не меньше {minimum} — "
                "источник пришёл неполным, пересборку не запускаю")

    log.info("Запускаю sp_refresh_gold — это несколько минут")
    client.query(f"CALL `{PROJECT}.forecast.sp_refresh_gold`()",
                 location=LOCATION).result()

    for row in client.query(
        f"""
        SELECT kind, MAX(month) AS mx, COUNT(*) AS n
        FROM `{PROJECT}.forecast.looker_fact_monthly`
        GROUP BY kind
        """,
        location=LOCATION,
    ).result():
        log.info("%s: до %s, строк %d", row.kind, row.mx, row.n)

    log.info("Готово")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log.error("Обновление не прошло: %s", exc)
        sys.exit(1)
