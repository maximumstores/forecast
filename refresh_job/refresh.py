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
SHEET_HOPTED = "16AjBg-FziOJj7qE34y9KACWMPLjiHUv3B0BDebgaI4Q"   # сюда пишет Hopted

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
    ("mt.sales_us_fact_native",       SHEET_SALES,        "2127822292",                   "auto",   '"',   True, None),
    ("mt.sales_plus_oos_native",      SHEET_SALES,        "1547531617",                   "auto",   "",    False, None),
    # сток на руках и себестоимость — диапазон A:AM (39 колонок)
    ("mt.amazon_starting_balance_native", SHEET_ORDERS_STOCK,
                                      "Starting Amazon month Balance", "letter", '"',  True,  39),
    # сырой сток прямо из файла Hopted (Manage FBA Inventory) — ближе всего
    # к Amazon, меняется каждый день. В расчёт пока не идёт, нужен для истории.
    ("mt.hopted_us_native",           SHEET_HOPTED,       "US V2",                        "auto",   '"',   True,  None),
    ("mt.hopted_ca_native",           SHEET_HOPTED,       "CA inv",                       "auto",   '"',   True,  None),
    ("mt.hopted_de_native",           SHEET_HOPTED,       "DE inv",                       "auto",   '"',   True,  None),
    ("mt.hopted_uk_native",           SHEET_HOPTED,       "UK inv",                       "auto",   '"',   True,  None),
    # открытые заказы (в пути и в производстве) по странам — вкладки order US/CA/DE/UK
    # из Orders-Stock (ведёт Надежда). Сюда же попадают сток и приход по ASIN.
    # Колонки по буквам: две шапки (названия и даты прихода партий), данные с 4-й строки.
    ("mt.order_us_native",            SHEET_ORDERS_STOCK, "order US",                     "letter", '"',   True,  235),
    ("mt.order_ca_native",            SHEET_ORDERS_STOCK, "order CA",                     "letter", '"',   True,  80),
    ("mt.order_de_native",            SHEET_ORDERS_STOCK, "order DE",                     "letter", '"',   True,  91),
    ("mt.order_uk_native",            SHEET_ORDERS_STOCK, "order UK",                     "letter", '"',   True,  71),
    # legacy-план, приходы и старый сток — диапазон A:JC (263 колонки)
    ("mt.ishodnik_native",           SHEET_ISHODNIK,     "исходник",                     "letter", '"',   True,  263),
    # справочник стоит последним: его выгрузка чаще всего тормозит или падает,
    # и это не должно задерживать сток, заказы и Hopted
    ("mt.SPR_native",                 SHEET_SPR,          "2096733449",                   "auto",   '"',   True,  None),
]

# Сбой одного листа не останавливает остальные: задание доходит до конца,
# пишет в лог строку «ИСТОЧНИК УПАЛ», шлёт сообщение в Telegram и завершается
# с ошибкой (Cloud Run показывает его как неудачное).
# Пересборку forecast.* запускаем только если обновились эти источники:
# без продаж, стартового остатка и справочника план посчитается на старых данных.
GOLD_REQUIRES = {
    "mt.sales_us_fact_native", "mt.sales_plus_oos_native",
    "mt.amazon_starting_balance_native", "mt.SPR_native",
}

# Листы, срез которых сохраняется в mt.stock_history каждый день.
# Строка хранится целиком как JSON — история не ломается, если в листе
# добавят или переставят колонки.
HISTORY_SOURCES = [
    "mt.hopted_us_native",                  # ежедневный сток из Amazon (Hopted)
    "mt.hopted_ca_native",
    "mt.hopted_de_native",
    "mt.hopted_uk_native",
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

# Заголовки ключевых колонок: в загруженном листе должна быть строка, где все
# перечисленные колонки равны указанному тексту. Если лист переставили или
# переименовали колонки, таблица в BigQuery не перезаписывается.
# Для трёх листов продаж проверка заголовков невозможна: в них нет строки с ASIN
# (заголовки #REF!), для них работают число строк и защита от резкого сжатия.
HEADER_CHECKS = {
    "mt.SPR_native": [("string_field_0", "ASIN"), ("string_field_1", "Old ASIN")],
    "mt.amazon_starting_balance_native": [("col_A", "ASIN"), ("col_B", "Old ASIN"),
                                          ("col_D", "Category+parent"), ("col_F", "Stock US")],
    "mt.hopted_us_native": [("string_field_1", "ASIN"), ("string_field_7", "FBA fulfillable quantity")],
    "mt.hopted_ca_native": [("string_field_5", "ASIN"), ("string_field_6", "FBA total quantity")],
    "mt.hopted_de_native": [("string_field_6", "ASIN"), ("string_field_7", "FBA total quantity")],
    "mt.hopted_uk_native": [("string_field_6", "ASIN"), ("string_field_7", "FBA total quantity")],
    "mt.order_us_native": [("col_A", "ASIN"), ("col_B", "Old ASIN"), ("col_E", "Category+parent")],
    "mt.order_ca_native": [("col_A", "ASIN"), ("col_B", "Old ASIN")],
    "mt.order_de_native": [("col_A", "ASIN"), ("col_B", "Old ASIN")],
    "mt.order_uk_native": [("col_A", "ASIN"), ("col_B", "Old ASIN")],
    "mt.ishodnik_native": [("col_A", "ASIN"), ("col_B", "Old ASIN"), ("col_C", "Category+parent")],
}

# Ключевая колонка с ASIN в листах продаж. В них нет строки заголовка с ASIN
# (там #REF!), поэтому проверяем сами значения: доля строк с настоящим ASIN
# (10 символов A-Z, 0-9). Пустая колонка = в таблице сломаны формулы.
KEY_COLUMNS = {
    "mt.sales_us_fact_native": ("string_field_0", "ASIN"),
    "mt.sales_plus_oos_native": ("string_field_0", "ASIN"),
}
KEY_MIN_SHARE = 0.5     # в рабочих таблицах сейчас ~95% строк с ASIN

# Новая выгрузка не может быть короче прежней таблицы больше чем на эту долю:
# так отсекается лист, который отдали обрезанным, но с числом строк выше порога.
SHRINK_LIMIT = 0.30
STAGE_PREFIX = "_stage_"

# ожидаемое число строк — если после загрузки сильно меньше, значит формат уехал
MIN_ROWS = {
    "mt.sales_us_fact_native": 5000,
    "mt.sales_plus_oos_native": 5000,
    "mt.SPR_native": 5000,
    "mt.amazon_starting_balance_native": 5000,
    "mt.ishodnik_native": 10000,
    "mt.hopted_us_native": 1000,
    "mt.hopted_ca_native": 500,
    "mt.hopted_de_native": 500,
    "mt.hopted_uk_native": 500,
    "mt.order_us_native": 5000,
    "mt.order_ca_native": 5000,
    "mt.order_de_native": 5000,
    "mt.order_uk_native": 5000,
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


def stage_name(table: str) -> str:
    """mt.order_us_native -> mt._stage_order_us_native: сюда грузим и проверяем,
    в рабочую таблицу копируем только после проверок."""
    dataset, name = table.split(".", 1)
    return f"{dataset}.{STAGE_PREFIX}{name}"


def check_csv_complete(path: str, quote_char: str = '"') -> int:
    """Файл не пустой и не оборван: строгий разбор CSV (незакрытая кавычка в конце
    файла — ошибка), последняя строка не короче половины типичной ширины."""
    from collections import Counter
    widths: Counter = Counter()
    last = None
    n = 0
    kwargs = {"quoting": csv.QUOTE_NONE} if quote_char == "" else {}
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh, strict=True, **kwargs):
                n += 1
                widths[len(row)] += 1
                last = row
    except csv.Error as exc:
        raise RuntimeError(f"выгрузка оборвана или повреждена: {exc}") from exc
    if n == 0:
        raise RuntimeError("пустая выгрузка: в файле нет ни одной строки")
    modal = widths.most_common(1)[0][0]
    if len(last) < modal / 2:
        raise RuntimeError(f"выгрузка оборвана: последняя строка {len(last)} колонок из {modal}")
    return n


def check_header(client: bigquery.Client, table: str, stage: str) -> None:
    """В загруженной таблице есть строка заголовков с ожидаемыми ключевыми колонками."""
    pairs = HEADER_CHECKS.get(table)
    if not pairs:
        return
    cond = " AND ".join(f"CAST({c} AS STRING) = '{v}'" for c, v in pairs)
    found = count(client, f"SELECT COUNT(*) FROM `{PROJECT}.{stage}` WHERE {cond}")
    if found == 0:
        raise RuntimeError("заголовки ключевых колонок не совпали: "
                           + ", ".join(f"{c}={v!r}" for c, v in pairs)
                           + " — лист изменили, рабочая таблица не тронута")


def previous_rows(client: bigquery.Client, table: str) -> int | None:
    try:
        return client.get_table(f"{PROJECT}.{table}").num_rows
    except Exception:  # noqa: BLE001
        return None


def asin_stats(client: bigquery.Client, table_ref: str, col: str) -> tuple[int, int]:
    """(строк всего, строк с настоящим ASIN в колонке col)."""
    total = count(client, f"SELECT COUNT(*) FROM `{PROJECT}.{table_ref}`")
    ok = count(client,
               f"SELECT COUNTIF(REGEXP_CONTAINS(TRIM({col}), r'^[A-Z0-9]{{10}}$')) "
               f"FROM `{PROJECT}.{table_ref}`")
    return total, ok


def ref_columns(client: bigquery.Client, table_ref: str) -> str:
    """Номера колонок (с нуля), где в ячейках есть #REF!: так видно, какие формулы сломаны.
    Любая ошибка диагностики не должна скрывать основную причину."""
    try:
        names = [f.name for f in client.get_table(f"{PROJECT}.{table_ref}").schema]
        expr = ", ".join(f"COUNTIF(CAST({n} AS STRING) = '#REF!') AS c{i}"
                         for i, n in enumerate(names))
        row = list(client.query(f"SELECT {expr} FROM `{PROJECT}.{table_ref}`",
                                location=LOCATION).result())[0]
        return ",".join(str(i) for i in range(len(names)) if row[i])
    except Exception:  # noqa: BLE001
        return ""


def key_column_message(client: bigquery.Client, table: str, table_ref: str,
                       col: str, label: str, total: int, ok: int) -> str:
    refs = ref_columns(client, table_ref)
    msg = (f"{table}: колонка {label} ({col}) пустая: настоящих значений {ok} из {total} строк")
    if refs:
        msg += f"; #REF! в колонках {refs} (номера с нуля) — в таблице сломаны формулы"
    return msg


def check_key_column(client: bigquery.Client, table: str, stage: str) -> None:
    """Сразу после загрузки листа: в ключевой колонке есть настоящие ASIN.
    Иначе рабочая таблица не перезаписывается, причина названа конкретно."""
    key = KEY_COLUMNS.get(table)
    if not key:
        return
    col, label = key
    total, ok = asin_stats(client, stage, col)
    if total == 0 or ok < total * KEY_MIN_SHARE:
        raise RuntimeError(key_column_message(client, table, stage, col, label, total, ok)
                           + " — рабочая таблица не тронута")


def validate_staged(client: bigquery.Client, table: str, stage: str, rows: int) -> None:
    """Проверки загруженного в стадию: число строк, сжатие против прежней таблицы,
    заголовки. Любая ошибка — рабочая таблица остаётся как была."""
    minimum = MIN_ROWS.get(table, 100)
    if rows < minimum:
        raise RuntimeError(f"строк {rows}, ожидалось не меньше {minimum} — "
                           "формат уехал или лист пришёл неполным")
    prev = previous_rows(client, table)
    if prev and rows < prev * (1 - SHRINK_LIMIT):
        raise RuntimeError(f"строк {rows} против {prev} в рабочей таблице: "
                           f"сжатие больше {SHRINK_LIMIT:.0%}, похоже на обрезанный лист")
    check_header(client, table, stage)
    check_key_column(client, table, stage)


def promote(client: bigquery.Client, stage: str, table: str) -> None:
    """Атомарно заменяет рабочую таблицу проверенной стадией и удаляет стадию."""
    cfg = bigquery.CopyJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    client.copy_table(f"{PROJECT}.{stage}", f"{PROJECT}.{table}",
                      job_config=cfg, location=LOCATION).result()
    drop_stage(client, stage)


def drop_stage(client: bigquery.Client, stage: str) -> None:
    try:
        client.delete_table(f"{PROJECT}.{stage}", not_found_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("стадию %s удалить не удалось: %s", stage, one_line(exc))


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


def save_history(client: bigquery.Client, skip: set[str] | None = None) -> None:
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
        if skip and table in skip:
            # источник не обновился сегодня — не записываем вчерашние данные под сегодняшней датой
            log.warning("история %s: пропущена, источник не обновился в этом запуске", table)
            continue
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
        if (src_name.startswith("hopted_") and row.n_prev > 0
                and row.changed == 0):
            log.warning("история %s: ни одна строка не изменилась со вчера — "
                        "похоже, Hopted перестал обновлять лист", src_name)


# ------------------------------------------------------------------ источники OOS
# Дни аута с ноября 2026 считаются из леджера DataDoe (datadoe_copy.fba_ledger_summary),
# для ASIN вне леджера — из ежедневных срезов Hopted US (mt.stock_history, hopted_us_native).
# Срезы Hopted — обязательный источник: пропущенный день нельзя восстановить задним числом.
HOPTED_GAP_DAYS = int(os.environ.get("HOPTED_GAP_DAYS", "3"))      # смотрим сегодня и два дня назад
LEDGER_MAX_LAG_DAYS = int(os.environ.get("LEDGER_MAX_LAG_DAYS", "8"))  # обычная задержка леджера 4–6 дней


def check_hopted_days(client: bigquery.Client) -> list[str]:
    """Даты последних HOPTED_GAP_DAYS дней (с первого среза), за которые нет среза Hopted US."""
    rows = client.query(
        f"""
        WITH have AS (
          SELECT DISTINCT snapshot_date AS d FROM `{PROJECT}.mt.stock_history`
          WHERE source = 'hopted_us_native'),
        first_day AS (SELECT MIN(d) AS d0 FROM have)
        SELECT CAST(d AS STRING) AS d
        FROM UNNEST(GENERATE_DATE_ARRAY(
               DATE_SUB(CURRENT_DATE('Europe/Kyiv'), INTERVAL {HOPTED_GAP_DAYS - 1} DAY),
               CURRENT_DATE('Europe/Kyiv'))) AS d
        WHERE d NOT IN (SELECT d FROM have) AND d >= (SELECT d0 FROM first_day)
        ORDER BY d
        """,
        location=LOCATION,
    ).result()
    return [r.d for r in rows]


def ledger_lag_days(client: bigquery.Client) -> int | None:
    """Сколько дней назад последняя дата в леджере DataDoe (None — таблица пуста)."""
    rows = list(client.query(
        f"""
        SELECT DATE_DIFF(CURRENT_DATE('Europe/Kyiv'), MAX(date), DAY) AS lag
        FROM `{PROJECT}.datadoe_copy.fba_ledger_summary`
        """,
        location=LOCATION,
    ).result())
    return rows[0].lag if rows else None


def check_oos_inputs(client: bigquery.Client) -> list[str]:
    """Проверка источников OOS. Каждая проблема — ERROR в журнале и сообщение в Telegram,
    пересборку не останавливает, но запуск завершается с ошибкой."""
    problems: list[str] = []
    try:
        missing = check_hopted_days(client)
        if missing:
            problems.append("нет среза Hopted US за " + ", ".join(missing))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"срезы Hopted не проверить: {one_line(exc)}")
    try:
        lag = ledger_lag_days(client)
        if lag is None:
            problems.append("леджер DataDoe пуст")
        elif lag > LEDGER_MAX_LAG_DAYS:
            problems.append(f"леджер DataDoe отстаёт на {lag} дн. (норма до {LEDGER_MAX_LAG_DAYS})")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"леджер DataDoe не проверить: {one_line(exc)}")
    for p in problems:
        log.error("OOS ИСТОЧНИК | %s", p)
    if problems:
        notify("Ночное обновление forecast: проблема с источниками OOS.\n"
               + "\n".join(f"- {p}" for p in problems))
    return problems


# ------------------------------------------------------------------ уведомления
def one_line(exc: object, limit: int = 300) -> str:
    """Причина сбоя одной строкой: тип и текст без переносов."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:limit]


def notify(text: str) -> None:
    """Сообщение в Telegram. Нужны переменные TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
    (в Cloud Run — из Secret Manager). Сбой отправки никогда не роняет задание."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram не настроен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), "
                    "сообщение не отправлено: %s", text.replace("\n", " | "))
        return
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": text[:3500]}, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram: сообщение не отправилось: %s", type(exc).__name__)


# Таблицы, по которым видно, что ночной запуск состоялся и дошёл до конца.
FRESH_TABLES = ["mt.sales_us_fact_native", "mt.amazon_starting_balance_native",
                "mt.order_us_native", "forecast.looker_fact_monthly"]
FRESH_SINCE_UTC = os.environ.get("FRESH_SINCE_UTC", "03:00")   # время старта ночного запуска


def check_fresh() -> int:
    """Запускается отдельно (python refresh.py --check-fresh), например в 04:30 UTC:
    каждая из FRESH_TABLES должна быть обновлена не раньше сегодняшнего FRESH_SINCE_UTC.
    Иначе запуск не состоялся или не дошёл до конца — сообщение в Telegram."""
    import datetime as dt
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    now = dt.datetime.now(dt.timezone.utc)
    hh, mm = (int(x) for x in FRESH_SINCE_UTC.split(":"))
    since = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if since > now:                 # проверка раньше времени старта — смотрим на вчерашний
        since -= dt.timedelta(days=1)
    stale = []
    for t in FRESH_TABLES:
        mod = client.get_table(f"{PROJECT}.{t}").modified
        log.info("%s: обновлена %s", t, mod.strftime("%Y-%m-%d %H:%M UTC"))
        if mod < since:
            stale.append(f"{t} — обновлена {mod.strftime('%d.%m %H:%M')} UTC")
    if stale:
        log.error("ЗАПУСК НЕ СОСТОЯЛСЯ ИЛИ НЕ ДОШЁЛ ДО КОНЦА | %s", "; ".join(stale))
        notify("Ночное обновление forecast не состоялось или не дошло до конца.\n"
               + "\n".join(f"- {x}" for x in stale))
        return 1
    return 0


# ------------------------------------------------------------------ main
def main() -> int:
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    token = get_token()
    tmp = tempfile.mkdtemp()
    failed: list[str] = []
    errors: dict[str, str] = {}

    for i, (table, doc, sheet, mode, quote, nl, width) in enumerate(SOURCES):
        try:
            gid = with_retries(lambda: resolve_gid(doc, sheet, token),
                               f"поиск листа {table}")
            path = os.path.join(tmp, f"{table.split('.')[-1]}.csv")

            check = CONTENT_CHECKS.get(table)
            stage = stage_name(table)
            for content_try in range(1, CONTENT_RETRIES + 1):
                size = with_retries(
                    lambda: download_sheet(doc, gid, path, token),
                    f"выгрузка {table}")
                log.info("%s: скачано %.1f МБ", table, size / 1024 / 1024)
                check_csv_complete(path, quote)

                rows = with_retries(
                    lambda: load_csv(client, stage, path, mode, quote, nl, width),
                    f"загрузка {table}")
                log.info("%s: в стадию загружено строк — %d", table, rows)
                validate_staged(client, table, stage, rows)

                if not check:
                    break
                sql, minimum, label, strict = check
                got = count(client, sql.format(table=f"{PROJECT}.{stage}"))
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
                        "рабочая таблица не тронута")
                else:
                    log.warning("%s: %s %d после %d попыток — колонка в источнике "
                                "пустая. Это запасной источник, продолжаю без него.",
                                table, label, got, CONTENT_RETRIES)

            promote(client, stage, table)
            log.info("%s: рабочая таблица обновлена (%d строк)", table, rows)

        except Exception as exc:  # noqa: BLE001
            log.error("ИСТОЧНИК УПАЛ | %s | %s", table, one_line(exc))
            drop_stage(client, stage_name(table))
            failed.append(table)
            errors[table] = one_line(exc)
            if table in GOLD_REQUIRES:
                # пересборка из-за этого не состоится, сообщаем сразу, а не в конце запуска;
                # остальные листы (сток, заказы, Hopted) продолжают грузиться
                notify(f"Ночное обновление forecast: источник упал, пересборка не состоится.\n"
                       f"- {table}: {errors[table]}")

        if i < len(SOURCES) - 1:
            log.info("пауза %d с перед следующим листом", PAUSE_BETWEEN)
            time.sleep(PAUSE_BETWEEN)

    # ---------------------------------------------- история стока
    # не критично: если срез не сохранился, пересборку не останавливаем
    try:
        save_history(client, skip=set(failed))
    except Exception as exc:  # noqa: BLE001
        log.warning("срез истории не сохранился: %s", exc)

    # ---------------------------------------------- источники OOS (Hopted и леджер)
    oos_problems = check_oos_inputs(client)

    # ---------------------------------------------- пересобирать ли forecast.*
    blocked = sorted(set(failed) & GOLD_REQUIRES)
    if blocked:
        log.error("ПЕРЕСБОРКА ПРОПУЩЕНА | не обновились: %s. forecast.* остаётся от прошлого запуска.",
                  ", ".join(blocked))
        notify("Ночное обновление forecast: пересборка пропущена.\n"
               + "\n".join(f"- {t}: {errors.get(t, '?')}" for t in failed))
        return 1

    # ---------------------------------------------- проверки silver-слоя
    # (название, запрос, минимум, таблица-источник, колонка ASIN в ней для диагностики)
    checks = [
        ("forecast.fact_sales_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.fact_sales_monthly`", 100000,
         "mt.sales_us_fact_native"),
        ("forecast.oos_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.oos_monthly`", 100000,
         "forecast.oos_source_report"),
        ("forecast.dim_product",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.dim_product`", 5000, "mt.SPR_native"),
        ("forecast.group_demand_monthly",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.group_demand_monthly`", 1000, None),
        # свежесть стока: если источник приехал пустым или колонка слетела,
        # план посчитается без остатка — такого не пропускаем
        ("forecast.stock_current (ASIN с остатком)",
         f"SELECT COUNTIF(stock_us > 0) FROM `{PROJECT}.forecast.stock_current`", 1000,
         "mt.amazon_starting_balance_native"),
        ("forecast.legacy_incoming",
         f"SELECT COUNT(*) FROM `{PROJECT}.forecast.legacy_incoming`", 1000,
         "mt.ishodnik_native"),
    ]
    for label, sql, minimum, src in checks:
        n = count(client, sql)
        log.info("%s: %d", label, n)
        if n < minimum:
            detail = ""
            if src in KEY_COLUMNS:
                col, key_label = KEY_COLUMNS[src]
                total, ok = asin_stats(client, src, col)
                detail = " | " + key_column_message(client, src, src, col, key_label, total, ok)
            elif src:
                detail = f" | проверь {src}"
            raise RuntimeError(f"{label}: строк {n}, ожидалось не меньше {minimum}{detail}")

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

    if failed:
        log.error("Готово, но не обновились: %s. Данные из этих листов старые.", ", ".join(failed))
        notify("Ночное обновление forecast завершено с ошибкой.\n"
               + "\n".join(f"- {t}: {errors.get(t, '?')}" for t in failed))
        return 1
    if oos_problems:
        log.error("Готово, но есть проблемы с источниками OOS: %s", "; ".join(oos_problems))
        return 1
    log.info("Готово")
    return 0


if __name__ == "__main__":
    if "--check-fresh" in sys.argv:
        sys.exit(check_fresh())
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log.error("Обновление не прошло: %s", one_line(exc))
        notify(f"Ночное обновление forecast упало: {one_line(exc)}")
        sys.exit(1) 
