"""Вкладка «Лето '27» — прогноз продаж на май–сентябрь по странам.

Для встречи «утверждаем прогноз»: факт '25, факт '26, прогноз '27 по группам → цветам → листингам, с правками и комментариями.
Заказ, резерв и сток здесь сознательно не показываются — это отдельная встреча.

Источник — таблицы Нины «Sales plan +Reorder <страна>», лист «исходник».
Колонки в четырёх файлах стоят по-разному, поэтому блоки ищутся по
заголовкам, а не по буквам. Данные складываются в BigQuery по кнопке
(forecast.summer_src), правки и комментарии — в forecast.summer_review.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery

PROJECT = "reorder-497714"
SRC = f"{PROJECT}.forecast.summer_src"
REVIEW = f"{PROJECT}.forecast.summer_review"

SOURCES = {
    "US": "1b3RWGwKP3_g_bA8JCuc6-XHCTRw3KiPlW8ROIUFH9qs",
    "CA": "1rJH8IJv9gg1nhsbDuN103xAAxkoLmmie915vaCEwgTQ",
    "DE": "1mfvv2zIq7YlnsbWfHtwtajR2Ic4RCwChPODbLxqIiAE",
    "UK": "1Mv1a0sxy_TqaPHxgB1_rMu9EUQK03oCzAu8GuBg0M7g",
}
COUNTRY_LABEL = {"US": "США", "CA": "Канада", "DE": "Европа (DE)", "UK": "UK"}
SHEET = "исходник"
SEASON = (5, 6, 7, 8, 9)
SEASON_TXT = "май–сентябрь"

RU_MONTH = {"янв": 1, "февр": 2, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
            "июн": 6, "июл": 7, "авг": 8, "сент": 9, "сен": 9, "окт": 10,
            "нояб": 11, "ноя": 11, "дек": 12}
MONTH_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

C25, C26, CP26, CP27, CTR = "#9AA8B5", "#2F6FB2", "#7FB0E0", "#C8752B", "#5E9C6A"

NINA_COLS = ("nina_f25", "nina_f26", "nina_fc27", "nina_stock", "nina_order", "nina_oos", "nina_inc")

STATUSES = ["—", "согласовано", "под вопросом", "снизить", "повысить"]


# ------------------------------------------------------------------ разбор заголовков
def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h if h is not None else "")).strip()


def _key(h) -> str:
    """Заголовок без пробелов, кавычек и регистра — для поиска по смыслу."""
    s = _norm(h).lower()
    for ch in (" ", "'", "’", "`", '"', "«", "»"):
        s = s.replace(ch, "")
    return s


def _iso_or_serial(h) -> dt.date | None:
    """Месяц из заголовка вида 2027-04-01 или Excel-серийного числа (46113).
    Формат 01.04.2026 сюда намеренно не входит: так подписаны другие блоки
    (приходы), их нельзя спутать с нормированным планом."""
    s = _norm(h)
    m = re.fullmatch(r"(\d{4})-(\d{2})-01", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1)
    if re.fullmatch(r"\d{5}(\.0+)?", s):
        n = int(float(s))
        if 40000 < n < 50000:
            d = dt.date(1899, 12, 30) + dt.timedelta(days=n)
            if d.day == 1:
                return d
    return None


def _ru_month(h) -> tuple[int, int] | None:
    """(год, месяц) из заголовка вида «янв.2025», «мая2026», «сент.2025»."""
    m = re.fullmatch(r"([а-я]+)\.?\s?(\d{4})", _norm(h).lower())
    if not m or m.group(1) not in RU_MONTH:
        return None
    return int(m.group(2)), RU_MONTH[m.group(1)]


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def map_columns(header: list) -> dict:
    """Находит нужные колонки в строке заголовков «исходника».

    Возвращает {'asin': i, 'old_asin': i, 'grp': i, 'category': i, 'color': i,
    'fact': {(y, m): i}, 'plan26': {(y, m): i}, 'plan27': {(y, m): i},
    'nina_f25': i, 'nina_f26': i, 'nina_fc27': i}. Отсутствующее — None/{}."""
    h = [_norm(x) for x in header]
    keys = [_key(x) for x in header]

    def first(pred):
        for i, x in enumerate(h):
            if pred(x, keys[i]):
                return i
        return None

    out = {
        "asin": first(lambda x, k: k == "asin"),
        "old_asin": first(lambda x, k: k == "oldasin"),
        "grp": first(lambda x, k: k == "category+parent"),
        "category": first(lambda x, k: k == "категории"),
        "color": first(lambda x, k: k == "chinacolors"),
        "reorder": first(lambda x, k: k.startswith("reorder")),   # «Reorder US/DE/…»: Да / Нет
        "nina_f25": first(lambda x, k: k.startswith("фактпродаж25")),
        "nina_f26": first(lambda x, k: k.startswith("фактпродаж26")),
        "nina_fc27": first(lambda x, k: k.startswith("прогнозпродаж27")),
        # колонки сводной «Order» у Нины: остатки, заказ, OOS, приход
        "nina_stock": first(lambda x, k: k.startswith("расчетныеостаткина1числомесяцазаказа")),
        "nina_order": first(lambda x, k: k == "заказ"),
        "nina_oos": first(lambda x, k: k.startswith("oos") and "/" not in k and "ago" not in k),
        "nina_inc": first(lambda x, k: k.startswith("приходпослерасчетазаказа")),
    }

    # факт продаж: первый непрерывный блок «янв.2025 … дек.2026»
    fact: dict = {}
    start = first(lambda x, k: _ru_month(x) == (2025, 1))
    if start is not None:
        i = start
        while i < len(h) and _ru_month(h[i]) is not None:
            ym = _ru_month(h[i])
            if ym not in fact:
                fact[ym] = i
            i += 1
    out["fact"] = {k: v for k, v in fact.items() if k[0] in (2025, 2026)}

    # прогноз '27: «Нормированный план продаж…» и сразу за ним 2027-04 … 2028-03
    plan27: dict = {}
    p27 = None
    for i in range(1, len(h)):
        if _iso_or_serial(h[i]) == dt.date(2027, 4, 1) and keys[i - 1].startswith("нормированныйпланпродаж"):
            p27 = i
            break
    if p27 is not None:
        for k in range(12):
            d = _iso_or_serial(h[p27 + k]) if p27 + k < len(h) else None
            if d is not None:
                plan27[(d.year, d.month)] = p27 + k
    out["plan27"] = plan27

    # план '26: блок 2026-04 … 2027-03 после блока '27
    plan26: dict = {}
    from_i = (p27 + 12) if p27 is not None else 0
    p26 = None
    for i in range(from_i, len(h) - 1):
        if _iso_or_serial(h[i]) == dt.date(2026, 4, 1) and _iso_or_serial(h[i + 1]) == dt.date(2026, 5, 1):
            p26 = i
            break
    if p26 is not None:
        for k in range(12):
            d = _iso_or_serial(h[p26 + k]) if p26 + k < len(h) else None
            if d is not None:
                plan26[(d.year, d.month)] = p26 + k
    out["plan26"] = plan26
    return out


# ------------------------------------------------------------------ загрузка из таблиц
def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return AuthorizedSession(creds)


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def fetch_country(sess, country: str, sheet_id: str) -> tuple[pd.DataFrame, dict]:
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values"
    rng = urllib.parse.quote(f"'{SHEET}'!1:1", safe="")
    r = sess.get(f"{base}/{rng}", params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=60)
    r.raise_for_status()
    header = (r.json().get("values") or [[]])[0]
    cols = map_columns(header)
    missing = [k for k in ("asin", "grp") if cols[k] is None]
    if missing or not cols["fact"]:
        raise RuntimeError(f"{country}: не нашёл колонки {missing or 'факта продаж'} в «{SHEET}»")

    wanted: dict[str, int] = {}
    for k in ("asin", "old_asin", "grp", "category", "color", "reorder") + NINA_COLS:
        if cols[k] is not None:
            wanted[k] = cols[k]
    for kind in ("fact", "plan26", "plan27"):
        for (y, m), i in cols[kind].items():
            wanted[f"{kind}|{y}-{m:02d}"] = i

    names = list(wanted)
    ranges = [f"'{SHEET}'!{col_letter(wanted[n])}2:{col_letter(wanted[n])}" for n in names]
    r = sess.get(f"{base}:batchGet",
                 params=[("ranges", x) for x in ranges]
                 + [("valueRenderOption", "UNFORMATTED_VALUE"), ("majorDimension", "COLUMNS")],
                 timeout=180)
    r.raise_for_status()
    vranges = r.json().get("valueRanges", [])
    data = {}
    n_rows = 0
    for name, vr in zip(names, vranges):
        vals = (vr.get("values") or [[]])[0]
        data[name] = vals
        n_rows = max(n_rows, len(vals))
    for name in names:
        data[name] = list(data[name]) + [None] * (n_rows - len(data[name]))
    raw = pd.DataFrame(data)

    txt = lambda s: s.astype(str).str.strip().replace({"None": "", "nan": ""})  # noqa: E731
    ident = pd.DataFrame({
        "asin": txt(raw["asin"]),
        "old_asin": txt(raw["old_asin"]) if "old_asin" in raw else "",
        "grp": txt(raw["grp"]),
        "category": txt(raw["category"]) if "category" in raw else "",
        "color": txt(raw["color"]) if "color" in raw else "",
        "reorder": txt(raw["reorder"]) if "reorder" in raw else "",
    })
    keep = (ident["asin"] != "") & (ident["grp"] != "") & (ident["asin"].str.lower() != "asin")
    ident = ident[keep]
    raw = raw[keep]

    rows = []
    for name in names:
        if name in ("asin", "old_asin", "grp", "category", "color", "reorder"):
            continue
        vals = raw[name].map(_num)
        if "|" in name:
            kind, ym = name.split("|")
            month = dt.date(int(ym[:4]), int(ym[5:]), 1)
        else:
            kind, month = name, None
        part = ident.assign(kind=kind, month=month, units=vals.values)
        rows.append(part[part["units"] != 0])
    long = pd.concat(rows, ignore_index=True) if rows else ident.iloc[0:0]
    long.insert(0, "country", country)
    info = {
        "rows": int(len(ident)),
        "fact_months": len(cols["fact"]),
        "plan26_months": len(cols["plan26"]),
        "plan27_months": len(cols["plan27"]),
        "nina_cols": [k for k in NINA_COLS if cols[k] is not None],
    }
    return long, info


def refresh_all(client: bigquery.Client) -> dict:
    sess = _session()
    frames, report = [], {}
    for country, sid in SOURCES.items():
        try:
            df, info = fetch_country(sess, country, sid)
            frames.append(df)
            report[country] = info
        except Exception as exc:  # noqa: BLE001
            report[country] = {"error": str(exc)[:300]}
    if not frames:
        raise RuntimeError(f"Не удалось прочитать ни одну таблицу: {report}")
    df = pd.concat(frames, ignore_index=True)
    df["loaded_at"] = dt.datetime.now(dt.timezone.utc)
    schema = [
        bigquery.SchemaField("country", "STRING"),
        bigquery.SchemaField("asin", "STRING"),
        bigquery.SchemaField("old_asin", "STRING"),
        bigquery.SchemaField("grp", "STRING"),
        bigquery.SchemaField("category", "STRING"),
        bigquery.SchemaField("color", "STRING"),
        bigquery.SchemaField("reorder", "STRING"),
        bigquery.SchemaField("kind", "STRING"),
        bigquery.SchemaField("month", "DATE"),
        bigquery.SchemaField("units", "FLOAT"),
        bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    ]
    # страны, которые не прочитались, оставляем как были
    ok = [c for c in SOURCES if "error" not in report[c]]
    failed = [c for c in SOURCES if c not in ok]
    if failed:
        try:
            old = client.query(
                f"SELECT * FROM `{SRC}` WHERE country IN UNNEST(@c)",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("c", "STRING", failed)]),
            ).result().to_dataframe()
            if not old.empty:
                if "reorder" not in old:
                    old["reorder"] = ""
                df = pd.concat([df, old[df.columns]], ignore_index=True)
        except Exception:  # noqa: BLE001
            pass
    df["month"] = df["month"].map(lambda d: d if isinstance(d, dt.date) else None)
    client.load_table_from_dataframe(
        df, SRC,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE, schema=schema),
    ).result()
    return report


# ------------------------------------------------------------------ BigQuery
@st.cache_resource(show_spinner=False)
def _ensured() -> dict:
    return {}


def ensure_tables(client: bigquery.Client) -> None:
    if _ensured().get("ok"):
        return
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{REVIEW}` (
          country STRING NOT NULL,
          grp STRING NOT NULL,
          color STRING,
          new_plan27 FLOAT64,
          status STRING,
          comment STRING,
          author STRING,
          ts TIMESTAMP NOT NULL
        )""").result()
    _ensured()["ok"] = True


def _q(client, sql: str, params: list | None = None) -> pd.DataFrame:
    return client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or [])
                        ).result().to_dataframe()


def _s(v) -> str:
    """Текст из ячейки BigQuery: None/NaN → пустая строка."""
    return v if isinstance(v, str) else ""


@st.cache_data(ttl=600, show_spinner=False)
def load_season(_client) -> pd.DataFrame:
    """Сезонные суммы (май–сентябрь) по стране × группе × цвету × ASIN."""
    return _q(_client, f"""
        SELECT country, grp, IFNULL(NULLIF(color,''),'—') AS color, asin,
               ANY_VALUE(category) AS category, ANY_VALUE(old_asin) AS old_asin,
               IFNULL(ANY_VALUE(reorder), '') AS reorder,
          SUM(IF(kind='fact'   AND EXTRACT(YEAR FROM month)=2025 AND EXTRACT(MONTH FROM month) BETWEEN 5 AND 9, units, 0)) AS f25,
          SUM(IF(kind='fact'   AND EXTRACT(YEAR FROM month)=2026 AND EXTRACT(MONTH FROM month) BETWEEN 5 AND 9, units, 0)) AS f26,
          SUM(IF(kind='plan26' AND EXTRACT(YEAR FROM month)=2026 AND EXTRACT(MONTH FROM month) BETWEEN 5 AND 9, units, 0)) AS p26,
          SUM(IF(kind='plan27' AND EXTRACT(YEAR FROM month)=2027 AND EXTRACT(MONTH FROM month) BETWEEN 5 AND 9, units, 0)) AS p27,
          SUM(IF(kind='fact'   AND EXTRACT(YEAR FROM month)=2025 AND EXTRACT(MONTH FROM month) BETWEEN 1 AND 3, units, 0)) AS w25,
          SUM(IF(kind='fact'   AND EXTRACT(YEAR FROM month)=2026 AND EXTRACT(MONTH FROM month) BETWEEN 1 AND 3, units, 0)) AS w26,
          SUM(IF(kind='nina_f25',  units, 0)) AS n25,
          SUM(IF(kind='nina_f26',  units, 0)) AS n26,
          SUM(IF(kind='nina_fc27', units, 0)) AS n27,
          SUM(IF(kind='nina_stock', units, 0)) AS n_stock,
          SUM(IF(kind='nina_order', units, 0)) AS n_order,
          SUM(IF(kind='nina_oos',   units, 0)) AS n_oos,
          SUM(IF(kind='nina_inc',   units, 0)) AS n_inc,
          SUM(IF(kind='plan27' AND EXTRACT(YEAR FROM month)=2027 AND EXTRACT(MONTH FROM month) BETWEEN 5 AND 8, units, 0)) AS p27ma,
          MAX(loaded_at) AS loaded_at,
          FORMAT_TIMESTAMP('%d.%m %H:%M', MAX(loaded_at), 'Europe/Kiev') AS loaded_txt
        FROM `{SRC}` GROUP BY 1,2,3,4""")


@st.cache_data(ttl=600, show_spinner=False)
def load_planner(_client) -> pd.DataFrame:
    """Прогноз нашего планера (только US) на май–август '27 по ASIN.
    Горизонт планера — 12 месяцев, сентябрь '27 в него пока не входит."""
    return _q(_client, f"""
        SELECT asin, SUM(plan_units) AS pl27ma
        FROM `{PROJECT}.forecast.plan_sales`
        WHERE month BETWEEN '2027-05-01' AND '2027-08-01' AND asin IS NOT NULL
        GROUP BY asin""")


@st.cache_data(ttl=600, show_spinner=False)
def load_arima(_client) -> pd.DataFrame:
    """ARIMA по группам (только US) на май–сентябрь '27 с 80% коридором.
    Считается каждую ночь в sp_refresh_gold (forecast.group_forecast_long, горизонт 18 мес)."""
    return _q(_client, f"""
        SELECT group_key AS grp, SUM(units_forecast) AS a27, SUM(lo_80) AS a_lo, SUM(hi_80) AS a_hi
        FROM `{PROJECT}.forecast.group_forecast_long`
        WHERE month BETWEEN '2027-05-01' AND '2027-09-01'
        GROUP BY 1""")


@st.cache_data(ttl=600, show_spinner=False)
def load_arima_monthly(_client, grp: str) -> pd.DataFrame:
    return _q(_client, f"""
        SELECT month, units_forecast, lo_80, hi_80
        FROM `{PROJECT}.forecast.group_forecast_long`
        WHERE group_key = @g ORDER BY month""",
              [bigquery.ScalarQueryParameter("g", "STRING", grp)])


@st.cache_data(ttl=600, show_spinner=False)
def load_monthly(_client, country: str, grp: str, color: str | None) -> pd.DataFrame:
    params = [bigquery.ScalarQueryParameter("c", "STRING", country),
              bigquery.ScalarQueryParameter("g", "STRING", grp),
              bigquery.ScalarQueryParameter("col", "STRING", color or "")]
    return _q(_client, f"""
        SELECT kind, month, SUM(units) AS units FROM `{SRC}`
        WHERE country=@c AND grp=@g AND (@col='' OR IFNULL(NULLIF(color,''),'—')=@col)
          AND kind IN ('fact','plan26','plan27') AND month IS NOT NULL
        GROUP BY 1,2 ORDER BY 2""", params)


@st.cache_data(ttl=60, show_spinner=False)
def load_reviews(_client) -> pd.DataFrame:
    return _q(_client, f"SELECT *, FORMAT_TIMESTAMP('%d.%m %H:%M', ts, 'Europe/Kiev') AS ts_txt "
                       f"FROM `{REVIEW}` ORDER BY ts DESC")


def save_review(client, country, grp, color, new_plan27, status, comment, author) -> None:
    client.query(
        f"INSERT INTO `{REVIEW}` (country, grp, color, new_plan27, status, comment, author, ts) "
        "VALUES (@c, @g, @col, @p, @s, @t, @a, CURRENT_TIMESTAMP())",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("c", "STRING", country),
            bigquery.ScalarQueryParameter("g", "STRING", grp),
            bigquery.ScalarQueryParameter("col", "STRING", color or None),
            bigquery.ScalarQueryParameter("p", "FLOAT64", new_plan27),
            bigquery.ScalarQueryParameter("s", "STRING", None if status == "—" else status),
            bigquery.ScalarQueryParameter("t", "STRING", comment or None),
            bigquery.ScalarQueryParameter("a", "STRING", author)]),
    ).result()


# ------------------------------------------------------------------ расчёты
def summarize(df: pd.DataFrame, by: list[str], use_nina: bool) -> pd.DataFrame:
    cols = ["f25", "f26", "p26", "p27", "w25", "w26", "n25", "n26", "n27",
            "n_stock", "n_order", "n_oos", "n_inc", "p27ma", "pl27ma"]
    df = df.copy()
    for c in cols:
        if c not in df:
            df[c] = 0.0
    g = df.groupby(by, dropna=False)[cols].sum().reset_index()
    if use_nina:
        # итоги как в сводной «Order» у Нины, если в файле есть её колонки
        g["F25"] = np.where(g["n25"] != 0, g["n25"], g["f25"])
        g["F26"] = np.where(g["n26"] != 0, g["n26"], g["f26"])
        g["P27"] = np.where(g["n27"] != 0, g["n27"], g["p27"])
    else:
        g["F25"], g["F26"], g["P27"] = g["f25"], g["f26"], g["p27"]
    g["yoy"] = np.where(g["F25"] > 0, g["F26"] / g["F25"] - 1, np.nan)
    g["g27"] = np.where(g["F26"] > 0, g["P27"] / g["F26"] - 1, np.nan)
    g["exec26"] = np.where(g["p26"] > 0, g["f26"] / g["p26"], np.nan)
    g["wyoy"] = np.where(g["w25"] > 0, g["w26"] / g["w25"] - 1, np.nan)
    trend = g["yoy"].clip(lower=-0.3, upper=1.0).fillna(0)
    g["trend27"] = g["F26"] * (1 + trend)
    g["flag"] = [flag(r) for r in g.itertuples()]
    return g


def flag(r) -> str:
    if r.F26 == 0 and r.P27 > 0:
        return "🆕 новинка"
    if r.P27 == 0:
        return ""
    out = []
    yoy = r.yoy if not np.isnan(r.yoy) else None
    if r.F25 == 0 and not np.isnan(r.g27):
        # запущена в '26: прошлогоднего роста нет, сравнивать можно только с фактом '26
        out.append("🟠 запуск '26, прогноз ×2+" if r.g27 > 1.0 else "🆕 запуск '26")
    elif not np.isnan(r.g27):
        limit = max(yoy or 0, 0) * 1.5 + 0.2
        if r.g27 > 0.5 and r.g27 > limit:
            out.append("🔴 рост выше тренда")
        elif r.g27 > 0.3:
            out.append("🟡 рост > 30%")
        elif r.g27 < -0.2:
            out.append("🔵 ниже факта")
    if (not np.isnan(r.wyoy) and not np.isnan(r.g27) and r.g27 > 0.3
            and r.wyoy < r.g27 - 0.5):
        out.append("❄️ зимой рост слабее")
    return " · ".join(out)


def latest_reviews(rev: pd.DataFrame) -> pd.DataFrame:
    if rev.empty:
        return rev
    rev = rev.copy()
    rev["color"] = rev["color"].fillna("")
    # последняя запись целиком (а не последнее непустое значение каждого поля)
    return rev.sort_values("ts").drop_duplicates(["country", "grp", "color"], keep="last")


def _pct(v) -> str:
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.0%}"


# ------------------------------------------------------------------ интерфейс
def render(client, author, log_event, tab_intro, num) -> None:
    tab_intro(
        f"Прогноз продаж на лето '27 ({SEASON_TXT}) по странам — для встречи «утверждаем прогноз».",
        "Сначала смотрим только прогноз против факта: заказ, резерв и сток — на второй встрече. "
        "Строки с флагом 🔴 — прогноз растёт сильнее, чем росли продажи; их обсуждаем первыми. "
        "Правку и комментарий можно оставить по группе или по цвету — сохраняется с автором и временем.",
    )
    try:
        ensure_tables(client)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Нет доступа к BigQuery: {exc}")
        return

    try:
        season = load_season(client)
    except Exception:  # noqa: BLE001
        season = pd.DataFrame()

    top = st.columns([3, 1])
    with top[1]:
        do_refresh = st.button("Обновить из таблиц Нины", key="s27_refresh", width="stretch")
    if do_refresh or season.empty:
        with st.spinner("Читаю таблицы Нины (US, CA, DE, UK)… около минуты"):
            try:
                report = refresh_all(client)
                load_season.clear()
                load_monthly.clear()
                season = load_season(client)
                errs = {c: v["error"] for c, v in report.items() if "error" in v}
                if errs:
                    st.warning("Не прочитались: " + "; ".join(f"{c}: {e}" for c, e in errs.items()))
                else:
                    st.success("Данные обновлены из всех 4 таблиц.")
                log_event("summer27", "refresh")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Не удалось прочитать таблицы: {exc}")
                if season.empty:
                    return
    with top[0]:
        if not season.empty:
            ts = season.sort_values("loaded_at")["loaded_txt"].iloc[-1]
            st.caption(f"Данные из таблиц Нины на {ts} (Киев). "
                       "Таблицы не меняются автоматически — после правок в них нажмите «Обновить».")

    countries = [c for c in SOURCES if c in set(season["country"])]
    country = st.radio("Страна", countries, horizontal=True, key="s27_country",
                       format_func=lambda c: COUNTRY_LABEL.get(c, c))
    sdf = season[season["country"] == country].copy()
    has_nina = (sdf[["n25", "n26", "n27"]].abs().sum() > 0).all()
    has_order = bool(sdf[["n_stock", "n_order"]].abs().sum().sum() > 0) if "n_order" in sdf else False
    arima = pd.DataFrame()
    if country == "US":
        try:
            arima = load_arima(client)
        except Exception:  # noqa: BLE001
            arima = pd.DataFrame()
    has_arima = not arima.empty

    f1, f2, f3 = st.columns([2, 3, 1.3])
    cats = f1.multiselect("Категория", sorted(x for x in sdf["category"].dropna().unique() if x), key="s27_cat")
    if cats:
        sdf = sdf[sdf["category"].isin(cats)]
    grps_pick = f2.multiselect("Группа", sorted(sdf["grp"].unique()), key="s27_grp")
    if grps_pick:
        sdf = sdf[sdf["grp"].isin(grps_pick)]
    only_flag = f3.toggle("Только с флагами", key="s27_onlyflag")
    if "reorder" in sdf and (sdf["reorder"].str.lower() == "да").any():
        only_reorder = st.toggle("Только «Reorder = Да» — как в сводной order у Нины", value=True,
                                 key="s27_reorder",
                                 help="Строки «Нет» — снятые с заказа и старые ASIN. Нина их в сводную не берёт.")
        if only_reorder:
            sdf = sdf[sdf["reorder"].str.lower() == "да"]

    rev = load_reviews(client)
    last = latest_reviews(rev)
    last_c = last[last["country"] == country] if not last.empty else last

    G = summarize(sdf, ["grp"], has_nina)
    if not last_c.empty:
        grp_rev = last_c[last_c["color"] == ""].set_index("grp")
        G["Правка '27"] = G["grp"].map(grp_rev["new_plan27"])
        G["Статус"] = G["grp"].map(grp_rev["status"]).fillna("")
        G["Комментарий"] = G["grp"].map(grp_rev["comment"]).fillna("")
        n_com = rev[rev["country"] == country].groupby("grp").size()
        G["💬"] = G["grp"].map(n_com).fillna(0).astype(int)
    else:
        G["Правка '27"], G["Статус"], G["Комментарий"], G["💬"] = np.nan, "", "", 0
    G["Итог '27"] = G["Правка '27"].fillna(G["P27"])
    if has_arima:
        a = arima.set_index("grp")
        G["a27"] = G["grp"].map(a["a27"])
        G["a_lo"] = G["grp"].map(a["a_lo"])
        G["a_hi"] = G["grp"].map(a["a_hi"])
        above = G["a_hi"].notna() & (G["P27"] > G["a_hi"])
        G.loc[above, "flag"] = G.loc[above, "flag"].map(
            lambda f: (f + " · " if f else "") + "📈 выше коридора ARIMA")

    # ---------------- итоги страны
    T = G[["F25", "F26", "P27", "Итог '27"]].sum()
    m = st.columns(5)
    m[0].metric(f"Факт '25, {SEASON_TXT}", num(T["F25"]))
    m[1].metric("Факт '26", num(T["F26"]), _pct(T["F26"] / T["F25"] - 1 if T["F25"] else np.nan) + " к '25")
    if has_arima:
        a_sum = G["a27"].sum()
        m[2].metric("ARIMA '27", num(a_sum), _pct(a_sum / T["F26"] - 1 if T["F26"] else np.nan) + " к '26",
                    help="Статистический прогноз BigQuery ARIMA_PLUS по истории продаж каждой группы "
                         "(спрос с поправкой на OOS), май–сентябрь '27.")
    else:
        m[2].metric("Ориентир '27 по тренду", num(G["trend27"].sum()),
                    _pct(G["trend27"].sum() / T["F26"] - 1 if T["F26"] else np.nan) + " к '26",
                    help="Факт '26 × рост '26/'25 по каждой группе (рост ограничен −30%…+100%). "
                         "Нейтральная точка для разговора, не план.")
    m[3].metric("Прогноз '27", num(T["P27"]), _pct(T["P27"] / T["F26"] - 1 if T["F26"] else np.nan) + " к '26")
    m[4].metric("С правками", num(T["Итог '27"]),
                _pct(T["Итог '27"] / T["P27"] - 1 if T["P27"] else np.nan) + " к прогнозу")

    view = G.copy()
    if only_flag:
        view = view[view["flag"] != ""]
    view["_ord"] = np.select([view["flag"].str.startswith("🔴"), view["flag"].str.startswith("🟠")], [0, 1], 2)
    view = view.sort_values(["_ord", "P27"], ascending=[True, False])

    st.markdown(f"#### Группы — {COUNTRY_LABEL.get(country, country)}")
    st.caption(
        "Факт и прогноз — сумма за май–сентябрь. «Ориентир '27» = факт '26 × рост '26/'25 "
        "(рост ограничен −30%…+100%) — нейтральная точка для разговора, а не план. "
        "«ARIMA '27» — статистический прогноз по истории продаж группы (только US), с 80% коридором; "
        "📈 — план Нины выше верхней границы коридора. "
        "«Зима '26/'25» — рост продаж за янв–мар: если он слабый, а прогноз лета сильный, это повод спросить почему."
    )
    # порядок колонок — как в сводной «order» у Нины, плюс наши колонки для сверки
    cols = {"Группа": view["grp"], "Флаг": view["flag"],
            "Факт '26": view["F26"], "Прогноз '27 (Нина)": view["P27"]}
    if has_arima:
        cols["ARIMA '27"] = view["a27"]
        cols["ARIMA 80%"] = [f"{num(lo)}–{num(hi)}" if pd.notna(lo) else "" for lo, hi in zip(view["a_lo"], view["a_hi"])]
        cols["Нина к ARIMA"] = np.where(view["a27"] > 0, view["P27"] / view["a27"] - 1, np.nan)
    if has_order:
        cols["Расч. остатки"] = view["n_stock"]
        cols["Заказ"] = view["n_order"]
        cols["OOS '25"] = view["n_oos"]
        cols["Приход после заказа"] = view["n_inc"]
    cols.update({
        "Факт '25": view["F25"],
        "Рост факт '26/'25": view["yoy"],
        "Рост прогноз/факт '26": view["g27"],
        "Ориентир '27": view["trend27"],
        "Зима '26/'25": view["wyoy"],
        "Правка '27": view["Правка '27"],
        "Статус": view["Статус"],
        "💬": view["💬"],
        "Комментарий": view["Комментарий"],
    })
    show = pd.DataFrame(cols)
    pc = st.column_config.NumberColumn(format="percent")
    nc = st.column_config.NumberColumn(format="localized")
    st.dataframe(
        show, hide_index=True, width="stretch", height=min(620, 38 + 35 * len(show)),
        column_config={
            **{c: nc for c in ("Факт '25", "Факт '26", "Прогноз '27 (Нина)", "ARIMA '27",
                               "Расч. остатки", "Заказ", "OOS '25",
                               "Приход после заказа", "Ориентир '27", "Правка '27")},
            **{c: pc for c in ("Нина к ARIMA", "Рост факт '26/'25", "Рост прогноз/факт '26",
                               "Зима '26/'25")},
            "Комментарий": st.column_config.TextColumn(width="medium"),
        },
    )
    st.download_button(
        "Скачать таблицу (CSV)", show.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"summer27_{country}.csv", mime="text/csv", key="s27_csv")

    # ---------------- разбор группы
    st.markdown("#### Разбор группы")
    opts = view["grp"].tolist()
    if not opts:
        st.info("Нет групп под выбранные фильтры.")
        return
    gsel = st.selectbox("Группа", opts, key=f"s27_g_{country}",
                        format_func=lambda x: f"{x}  {G.set_index('grp').at[x, 'flag']}".strip())
    gd = sdf[sdf["grp"] == gsel]
    C = summarize(gd, ["color"], has_nina).sort_values("P27", ascending=False)
    colors = ["вся группа"] + C["color"].tolist()
    csel = st.selectbox("Цвет", colors, key=f"s27_c_{country}_{gsel}")
    color = None if csel == "вся группа" else csel

    mon = load_monthly(client, country, gsel, color)
    ar_m = pd.DataFrame()
    if has_arima and color is None:
        try:
            ar_m = load_arima_monthly(client, gsel)
        except Exception:  # noqa: BLE001
            ar_m = pd.DataFrame()
    st.plotly_chart(monthly_chart(mon, gsel, color, ar_m), width="stretch", key=f"s27_chart_{country}")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**По цветам**")
        st.dataframe(pd.DataFrame({
            "Цвет": C["color"], "Флаг": C["flag"], "Факт '25": C["F25"], "Факт '26": C["F26"],
            "Рост '26": C["yoy"], "Прогноз '27": C["P27"], "Рост '27": C["g27"],
        }), hide_index=True, width="stretch",
            column_config={"Факт '25": nc, "Факт '26": nc, "Прогноз '27": nc,
                           "Рост '26": pc, "Рост '27": pc})
        with st.expander("По листингам (ASIN)"):
            A = summarize(gd if color is None else gd[gd["color"] == color], ["asin", "color", "old_asin"], has_nina)
            A = A.sort_values("P27", ascending=False)
            st.dataframe(pd.DataFrame({
                "ASIN": A["asin"], "Старый ASIN": A["old_asin"], "Цвет": A["color"], "Флаг": A["flag"],
                "Факт '25": A["F25"], "Факт '26": A["F26"], "Прогноз '27": A["P27"], "Рост '27": A["g27"],
            }), hide_index=True, width="stretch",
                column_config={"Факт '25": nc, "Факт '26": nc, "Прогноз '27": nc, "Рост '27": pc})

    with right:
        cur = (G.set_index("grp").loc[gsel] if color is None else C.set_index("color").loc[color])
        st.markdown(f"**Правка и комментарий** — {gsel}" + (f" / {color}" if color else ""))
        with st.form(key=f"s27_form_{country}_{gsel}_{csel}", clear_on_submit=True):
            st.caption(f"Прогноз '27 сейчас: **{num(cur['P27'])}** · факт '26: {num(cur['F26'])} · "
                       f"ориентир: {num(cur['trend27'])}")
            new_val = st.number_input("Новый прогноз '27, шт (пусто — без правки)", min_value=0.0,
                                      value=None, step=10.0, format="%.0f")
            status = st.selectbox("Статус", STATUSES)
            text = st.text_area("Комментарий", placeholder="Почему так: промо, новый цвет, отзывы, OOS…")
            ok = st.form_submit_button("Сохранить", type="primary")
        if ok:
            if new_val is None and status == "—" and not text.strip():
                st.warning("Нечего сохранять — укажите правку, статус или комментарий.")
            else:
                save_review(client, country, gsel, color, new_val, status, text.strip(), author)
                load_reviews.clear()
                log_event("summer27", f"{country} {gsel}" + (f" / {color}" if color else ""))
                st.success("Сохранено.")
                st.rerun()

        hist = rev[(rev["country"] == country) & (rev["grp"] == gsel)] if not rev.empty else rev
        if not hist.empty:
            st.markdown("**История**")
            for r in hist.itertuples():
                when = _s(r.ts_txt)
                bits = []
                if _s(r.color):
                    bits.append(f"цвет {r.color}")
                if pd.notna(r.new_plan27):
                    bits.append(f"прогноз → {num(r.new_plan27)}")
                if _s(r.status):
                    bits.append(r.status)
                head = f"{when} · {str(r.author).split('@')[0]}" + (" · " + ", ".join(bits) if bits else "")
                st.markdown(f"<div style='font-size:.85rem;color:#5A6B7A'>{head}</div>"
                            f"<div style='margin-bottom:8px'>{_s(r.comment)}</div>",
                            unsafe_allow_html=True)


def monthly_chart(mon: pd.DataFrame, grp: str, color: str | None,
                  ar: pd.DataFrame | None = None) -> go.Figure:
    fig = go.Figure()
    if mon.empty:
        return fig
    mon = mon.copy()
    mon["month"] = pd.to_datetime(mon["month"])
    mon["y"], mon["m"] = mon["month"].dt.year, mon["month"].dt.month

    def series(kind, year):
        s = mon[(mon["kind"] == kind) & (mon["y"] == year)].set_index("m")["units"]
        return [float(s.get(i, np.nan)) if i in s.index else None for i in range(1, 13)]

    x = MONTH_SHORT
    fig.add_vrect(x0=3.5, x1=8.5, fillcolor="rgba(200,117,43,0.07)", line_width=0)
    fig.add_trace(go.Scatter(x=x, y=series("fact", 2025), name="Факт '25", mode="lines+markers",
                             line=dict(color=C25, width=2)))
    fig.add_trace(go.Scatter(x=x, y=series("fact", 2026), name="Факт '26", mode="lines+markers",
                             line=dict(color=C26, width=3)))
    fig.add_trace(go.Scatter(x=x, y=series("plan27", 2027), name="План Нины '27", mode="lines+markers",
                             line=dict(color=CP27, width=3, dash="dash")))
    if ar is not None and not ar.empty:
        a = ar.copy()
        a["month"] = pd.to_datetime(a["month"])
        a = a[a["month"].dt.year == 2027]
        a = a.set_index(a["month"].dt.month)
        pick = lambda col: [float(a[col].get(i)) if i in a.index else None for i in range(1, 13)]  # noqa: E731
        fig.add_trace(go.Scatter(x=x, y=pick("hi_80"), mode="lines", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=x, y=pick("lo_80"), mode="lines", line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(94,156,106,0.15)", name="ARIMA 80%", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=x, y=pick("units_forecast"), name="ARIMA '27", mode="lines+markers",
                                 line=dict(color=CTR, width=2)))
    fig.update_layout(
        height=340, margin=dict(l=10, r=10, t=40, b=10),
        title=dict(text=f"{grp}" + (f" / {color}" if color else "") + " — по месяцам, шт", font=dict(size=14)),
        legend=dict(orientation="h", y=-0.15), hovermode="x unified",
        xaxis=dict(type="category"), plot_bgcolor="white",
        yaxis=dict(gridcolor="#EEF1F4", rangemode="tozero"),
    )
    return fig
