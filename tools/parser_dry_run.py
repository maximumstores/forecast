"""Прогон парсера таблиц Нины БЕЗ записи в BigQuery: сравнение «было / станет».

Запуск (Cloud Shell, под своей учёткой с доступом к четырём таблицам Нины):
    gcloud auth application-default login \
        --scopes=https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/bigquery
    python tools/parser_dry_run.py

Читает листы «исходник» US/CA/DE/UK через Sheets API (только чтение), разбирает их
функцией summer27.fetch_country и сравнивает с текущей forecast.summer_src по каждой
стране и виду данных: строки, ASIN, сумма штук. Ничего не пишет.
"""
import sys

import google.auth
import pandas as pd
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

sys.path.insert(0, ".")
import summer27 as s  # noqa: E402

PROJECT = s.PROJECT


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["country", "kind"]).agg(rows=("asin", "size"), asin=("asin", "nunique"),
                                            units=("units", "sum"))
    g["units"] = g["units"].round()
    return g


def compare(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    out = old.join(new, how="outer", lsuffix="_было", rsuffix="_станет").fillna(0)
    base = out["units_было"].where(out["units_было"] != 0)
    out["units_изм_%"] = ((out["units_станет"] - out["units_было"]) / base * 100).round(2)
    return out


def must_stop(out: pd.DataFrame) -> pd.DataFrame:
    """Изменение суммы больше 1%, кроме plan26 US (его добавляет исправление парсера)."""
    idx_country = out.index.get_level_values(0)
    idx_kind = out.index.get_level_values(1)
    return out[(out["units_изм_%"].abs() > 1) & ~((idx_country == "US") & (idx_kind == "plan26"))]


def main() -> None:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                                           "https://www.googleapis.com/auth/bigquery"])
    sess = AuthorizedSession(creds)
    client = bigquery.Client(project=PROJECT, location="EU", credentials=creds)

    parts = []
    for country, sid in s.SOURCES.items():
        df, info = s.fetch_country(sess, country, sid)
        parts.append(df)
        print(f"{country}: ASIN в листе {info['rows']}, plan26 месяцев {info['plan26_months']}, "
              f"plan27 месяцев {info['plan27_months']}, plan_blocks {info.get('plan_blocks')}")
    new = summarize(pd.concat(parts, ignore_index=True))

    old = client.query(f"""
        SELECT country, kind, COUNT(*) AS rows, COUNT(DISTINCT asin) AS asin, ROUND(SUM(units)) AS units
        FROM `{s.SRC}` GROUP BY country, kind""").to_dataframe().set_index(["country", "kind"])

    out = compare(old, new)
    pd.set_option("display.width", 200, "display.max_rows", 200)
    print(out.to_string())
    flag = must_stop(out)
    if len(flag):
        print("\nСТОП: сумма изменилась больше чем на 1% (кроме plan26 US):")
        print(flag.to_string())


if __name__ == "__main__":
    main()
