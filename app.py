"""
Sales Planner — план продаж и аналитика поверх BigQuery (reorder-497714).

Вкладки:
  Обзор       — факт, прогноз, интервал 80%, ключевые числа
  Разрезы     — размеры, цвета, категории, ABCD
  Ввод плана  — редактируемая сетка группа+цвет -> forecast_override
  План и факт — сравнение план/факт по группам
  Правки      — история ручных изменений

Запуск:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT = "reorder-497714"
DS = f"{PROJECT}.forecast"
TABLE_OVERRIDE = f"{PROJECT}.forecast.forecast_override"

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/drive.readonly",
]

INK = "#1B2A38"
FACT = "#2F6FB2"
FCST = "#C8752B"
BAND = "rgba(200,117,43,0.13)"
MUTED = "#7C8B99"

st.set_page_config(page_title="Sales Planner", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1500px;}
      @media (max-width: 820px) {
        .block-container {padding-left: .6rem; padding-right: .6rem;}
        div[data-testid="stMetricValue"] {font-size: 1.15rem;}
        div[data-testid="stMetricLabel"] {font-size: .75rem;}
        button[data-baseweb="tab"] {padding-left: .5rem; padding-right: .5rem;}
        button[data-baseweb="tab"] p {font-size: .82rem;}
      }
      div[data-testid="stMetricValue"] {font-size: 1.6rem;}
      div[data-testid="stMetricLabel"] {color: #7C8B99;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------ клиент
@st.cache_resource
def get_client() -> bigquery.Client:
    if "gcp_service_account" in st.secrets:
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=SCOPES
        )
        return bigquery.Client(credentials=creds, project=PROJECT, location="EU")
    return bigquery.Client(project=PROJECT, location="EU")


client = get_client()


# ------------------------------------------------------------------ вход
def check_auth() -> str:
    """Один общий логин на всю команду. Вход держится, пока открыта вкладка;
    с галочкой «запомнить» — 30 дней, токен уезжает в адрес страницы."""
    users = st.secrets.get("auth", {})
    if not users:
        return "guest"          # пароль не настроен — пускаем всех

    secret = str(users)

    def make_token(login: str) -> str:
        raw = f"{login}|{secret}|{dt.date.today().isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def token_valid(login: str, token: str) -> bool:
        for back in range(31):                      # токен живёт 30 дней
            day = (dt.date.today() - dt.timedelta(days=back)).isoformat()
            raw = f"{login}|{secret}|{day}"
            if hashlib.sha256(raw.encode()).hexdigest()[:32] == token:
                return True
        return False

    # уже вошли в этой сессии
    if st.session_state.get("user"):
        return st.session_state["user"]

    # пришли по ссылке с токеном
    qp = st.query_params
    if qp.get("u") and qp.get("t") and token_valid(qp["u"], qp["t"]):
        st.session_state["user"] = qp["u"]
        return qp["u"]

    # форма входа
    st.title("Sales Planner")
    with st.form("login"):
        login = st.text_input("Логин")
        pwd = st.text_input("Пароль", type="password")
        remember = st.checkbox("Запомнить на 30 дней", value=True)
        ok = st.form_submit_button("Войти", type="primary")

    if ok:
        if users.get(login) == pwd:
            st.session_state["user"] = login
            if remember:
                st.query_params["u"] = login
                st.query_params["t"] = make_token(login)
            st.rerun()
        else:
            st.error("Неверный логин или пароль")

    st.stop()


author = check_auth()


def _to_float(df: pd.DataFrame) -> pd.DataFrame:
    """BigQuery отдаёт NUMERIC как decimal.Decimal — с float он не
    складывается. Приводим такие колонки к float сразу после запроса,
    чтобы арифметика не падала в произвольном месте."""
    for col in df.columns:
        if df[col].dtype == "object":
            sample = df[col].dropna()
            if not sample.empty and isinstance(sample.iloc[0], decimal.Decimal):
                df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def run(sql: str, params: list | None = None) -> pd.DataFrame:
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=params or []),
    )
    return _to_float(job.result().to_dataframe())


# ------------------------------------------------------------------ данные
@st.cache_data(ttl=600)
def load_planned_groups() -> set:
    """Группы, для которых модель выдала план. Только их можно редактировать."""
    df = run(f"SELECT DISTINCT group_key FROM `{DS}.plan_sales`")
    return set(df["group_key"].dropna())


@st.cache_data(ttl=600)
def load_dims() -> pd.DataFrame:
    return run(
        f"""
        SELECT DISTINCT group_key, category, abcd_class
        FROM `{DS}.looker_fact_monthly`
        WHERE group_key IS NOT NULL
        """
    )


@st.cache_data(ttl=600)
def load_series(groups: tuple, categories: tuple) -> pd.DataFrame:
    where = ["group_key IS NOT NULL"]
    params: list = []
    if groups:
        where.append("group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    if categories:
        where.append("category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    return run(
        f"""
        WITH fact AS (
          SELECT month,
                 SUM(demand_units) AS fact,
                 SUM(sales_units)  AS sales
          FROM `{DS}.looker_fact_monthly`
          WHERE kind = 'actual' AND {' AND '.join(where)}
          GROUP BY month
        ),
        -- основной прогноз — тот же, на котором строится план
        plan AS (
          SELECT month, SUM(plan_units) AS forecast
          FROM `{DS}.plan_sales`
          WHERE {' AND '.join(where)}
          GROUP BY month
        ),
        -- ARIMA оставляем для интервала: он считается только там
        band AS (
          SELECT month,
                 GREATEST(SUM(lo_80), 0) AS lo_80,
                 SUM(hi_80)              AS hi_80,
                 SUM(forecast_units)     AS arima
          FROM `{DS}.looker_fact_monthly`
          WHERE kind = 'forecast' AND {' AND '.join(where)}
          GROUP BY month
        )
        SELECT month, f.fact, f.sales, p.forecast,
               b.lo_80, b.hi_80, b.arima
        FROM fact f
        FULL JOIN plan p USING (month)
        FULL JOIN band b USING (month)
        ORDER BY month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_breakdown(dim: str, groups: tuple, categories: tuple) -> pd.DataFrame:
    where = [f"{dim} IS NOT NULL"]
    params: list = []
    if groups:
        where.append("group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    if categories:
        where.append("category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    return run(
        f"""
        SELECT {dim} AS label, SUM(plan_units) AS units
        FROM `{DS}.plan_sales`
        WHERE {' AND '.join(where)}
        GROUP BY label
        ORDER BY units DESC
        LIMIT 20
        """,
        params,
    )


@st.cache_data(ttl=300)
def load_plan(months_ahead: int, groups: tuple) -> pd.DataFrame:
    where = [
        "month >= DATE_TRUNC(CURRENT_DATE(), MONTH)",
        "month < DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL @ahead MONTH)",
    ]
    params: list = [bigquery.ScalarQueryParameter("ahead", "INT64", months_ahead)]
    if groups:
        where.append("group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))

    return run(
        f"""
        WITH auto_plan AS (
          SELECT group_key, IFNULL(color,'') AS color, month,
                 SUM(plan_units) AS plan_auto
          FROM `{DS}.plan_sales`
          WHERE {' AND '.join(where)}
          GROUP BY 1,2,3
        ),
        ovr AS (
          SELECT * EXCEPT(rn) FROM (
            SELECT group_key, IFNULL(color,'') AS color, month,
                   override_units, author, updated_at,
                   ROW_NUMBER() OVER (PARTITION BY group_key, IFNULL(color,''), month
                                      ORDER BY updated_at DESC) AS rn
            FROM `{TABLE_OVERRIDE}`
            WHERE size IS NULL
          ) WHERE rn = 1
        )
        ,
        ly AS (
          SELECT group_key, IFNULL(color,'') AS color,
                 DATE_ADD(month, INTERVAL 12 MONTH) AS month,
                 SUM(demand_units) AS fact_ly
          FROM `{DS}.looker_fact_monthly`
          WHERE kind = 'actual'
          GROUP BY 1, 2, 3
        )
        SELECT a.group_key, a.color, a.month,
               ROUND(a.plan_auto) AS plan_auto,
               o.override_units, o.author, o.updated_at,
               ROUND(l.fact_ly) AS fact_ly
        FROM auto_plan a
        LEFT JOIN ovr o USING (group_key, color, month)
        LEFT JOIN ly  l USING (group_key, color, month)
        ORDER BY a.group_key, a.color, a.month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_plan_compare(groups: tuple) -> pd.DataFrame:
    where = ""
    params: list = []
    if groups:
        where = "WHERE group_key IN UNNEST(@groups)"
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    return run(
        f"""
        SELECT group_key, month, series, SUM(units) AS units
        FROM `{DS}.looker_plan_compare`
        {where}
        GROUP BY 1,2,3
        ORDER BY month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_on_track(level: str, groups: tuple, categories: tuple) -> pd.DataFrame:
    table = "looker_on_track_category" if level == "category" else "looker_on_track"
    key = "category" if level == "category" else "group_key"

    where: list[str] = []
    params: list = []
    if level == "category":
        if categories:
            where.append("category IN UNNEST(@cats)")
            params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))
    else:
        if groups:
            where.append("group_key IN UNNEST(@groups)")
            params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
        elif categories:
            where.append("category IN UNNEST(@cats)")
            params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    clause = "WHERE " + " AND ".join(where) if where else ""
    return run(
        f"""
        SELECT {key} AS name, on_hand, on_order, available,
               forecast_demand, sold_season_to_date,
               proj_leftover, leftover_ratio, cover_months,
               CASE
                 WHEN status LIKE '%no forecast%'  THEN '⚪ прогноза нет'
                 WHEN status LIKE '%overstocked%'  THEN '🔴 перезатарены'
                 WHEN status LIKE '%understocked%' THEN '🔴 не хватит'
                 WHEN status LIKE '%watch-high%'   THEN '🟡 много запаса'
                 WHEN status LIKE '%watch-tight%'  THEN '🟡 впритык'
                 ELSE '🟢 в норме'
               END AS status,
               status_rank
        FROM `{DS}.{table}`
        {clause}
        ORDER BY status_rank, ABS(proj_leftover) DESC
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_needs_plan() -> pd.DataFrame:
    return run(
        f"""
        SELECT group_key, color, size, product_key, order_us, name, reason
        FROM `{DS}.new_needs_plan`
        ORDER BY order_us DESC NULLS LAST
        """
    )


@st.cache_data(ttl=600)
def load_oos(groups: tuple, categories: tuple) -> pd.DataFrame:
    where = ["kind = 'actual'"]
    params: list = []
    if groups:
        where.append("group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    if categories:
        where.append("category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))
    return run(
        f"""
        SELECT month,
               SUM(sales_units)  AS sales,
               SUM(demand_units) AS demand,
               SAFE_DIVIDE(SUM(oos_days * IFNULL(sales_units,0)),
                           NULLIF(SUM(IFNULL(sales_units,0)),0)) AS oos_weighted
        FROM `{DS}.looker_fact_monthly`
        WHERE {' AND '.join(where)}
        GROUP BY month
        ORDER BY month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_gap(groups: tuple) -> pd.DataFrame:
    """Расхождение старого плана и нашего прогноза по группам."""
    where = ""
    params: list = []
    if groups:
        where = "WHERE group_key IN UNNEST(@groups)"
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    return run(
        f"""
        WITH scope AS (              -- только месяцы, где есть оба ряда
          SELECT month
          FROM `{DS}.looker_plan_compare`
          WHERE series IN ('Legacy plan', 'Our forecast')
          GROUP BY month
          HAVING COUNT(DISTINCT series) = 2
        ),
        p AS (
          SELECT group_key, series, SUM(units) AS units
          FROM `{DS}.looker_plan_compare`
          WHERE month IN (SELECT month FROM scope)
          {where.replace("WHERE", "AND") if where else ""}
          GROUP BY 1, 2
        )
        SELECT group_key,
               MAX(IF(series='Fact',                  units, NULL)) AS fact,
               MAX(IF(series='Legacy plan',           units, NULL)) AS legacy,
               MAX(IF(series='Our forecast',          units, NULL)) AS ours,
               MAX(IF(series='Our forecast (growth)', units, NULL)) AS growth
        FROM p
        GROUP BY group_key
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_drivers(groups: tuple, categories: tuple) -> pd.DataFrame:
    """Причины расхождения — из готовой таблицы plan_compare."""
    where = ["flag = 'REVIEW'"]
    params: list = []
    if groups:
        where.append("""asin IN (SELECT asin FROM `""" + DS +
                     """.dim_product` WHERE group_key IN UNNEST(@groups))""")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    elif categories:
        where.append("category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    return run(
        f"""
        SELECT IFNULL(category, 'без категории') AS category,
               CASE
                 WHEN driver_hint LIKE 'legacy-only%'  THEN 'была в старом плане, в новый не попала'
                 WHEN driver_hint LIKE 'new-only%'     THEN 'новая позиция, в старом плане не было'
                 WHEN driver_hint LIKE 'growth uplift%'   THEN 'рост год к году'
                 WHEN driver_hint LIKE 'growth decline%'  THEN 'падение год к году'
                 ELSE 'разница в миксе или поправка на OOS'
               END AS driver_hint,
               COUNT(DISTINCT asin) AS n_asins,
               ROUND(SUM(delta_abs)) AS delta_total
        FROM `{DS}.plan_compare`
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2
        ORDER BY ABS(SUM(delta_abs)) DESC
        LIMIT 25
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_size_curve(groups: tuple) -> pd.DataFrame:
    """Доли размеров внутри группы по календарным месяцам — та же логика,
    что внутри asin_forecast, но посчитанная отдельно для просмотра."""
    where = ["p.group_key IS NOT NULL", "p.active_us", "NOT p.is_new"]
    params: list = []
    if groups:
        where.append("p.group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))

    return run(
        f"""
        WITH cell_hist AS (
          SELECT p.group_key,
                 COALESCE(p.size, '(none)')  AS size,
                 EXTRACT(MONTH FROM d.month) AS cal_month,
                 SUM(d.demand)               AS demand
          FROM `{DS}.fact_demand_raw` d
          JOIN `{DS}.dim_product`     p ON p.asin = d.asin
          WHERE {' AND '.join(where)}
          GROUP BY 1, 2, 3
        ),
        grp AS (
          SELECT group_key, cal_month, SUM(demand) AS g_demand
          FROM cell_hist GROUP BY 1, 2
        )
        SELECT h.group_key, h.size, h.cal_month,
               ROUND(SAFE_DIVIDE(h.demand, NULLIF(g.g_demand, 0)) * 100, 1) AS share_pct,
               ROUND(h.demand) AS demand
        FROM cell_hist h
        JOIN grp g USING (group_key, cal_month)
        ORDER BY h.group_key, h.cal_month, share_pct DESC
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_alerts(groups: tuple, categories: tuple) -> pd.DataFrame:
    """Собирает проблемы из готовых gold-таблиц в один список.
    Каждая строка: что случилось, по какой позиции, на сколько единиц."""
    where_g = ""
    params: list = []
    if groups:
        where_g = "AND group_key IN UNNEST(@groups)"
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    elif categories:
        where_g = "AND category IN UNNEST(@cats)"
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    where_plain = where_g.replace("AND ", "WHERE ", 1) if where_g else ""

    return run(
        f"""
        WITH stockout AS (          -- не хватит товара до конца сезона
          SELECT 'Не хватит товара'                       AS kind,
                 group_key                                AS name,
                 category,
                 CAST(ROUND(-proj_leftover) AS INT64)     AS units,
                 CONCAT('доступно ', CAST(ROUND(available) AS STRING),
                        ', спрос ', CAST(ROUND(forecast_demand) AS STRING))  AS detail,
                 1 AS prio
          FROM `{DS}.looker_on_track`
          WHERE proj_leftover < 0 AND forecast_demand > 0 {where_g}
        ),
        overstock AS (             -- останется много после сезона
          SELECT 'Останется на складе', group_key, category,
                 CAST(ROUND(proj_leftover) AS INT64),
                 CONCAT('покрытие ', CAST(ROUND(cover_months, 1) AS STRING), ' мес'),
                 3
          FROM `{DS}.looker_on_track`
          WHERE status LIKE '%overstock%' AND proj_leftover > 0 {where_g}
                AND cover_months > 1
        ),
        noplan AS (                -- позиции без прогноза
          SELECT 'Нужен ручной план', group_key, CAST(NULL AS STRING),
                 CAST(ROUND(IFNULL(order_us, 0)) AS INT64),
                 CONCAT(IFNULL(color, ''), ' ', IFNULL(size, '')),
                 2
          FROM `{DS}.new_needs_plan`
          {where_plain.replace("category IN", "group_key IN") if "category" in where_plain else where_plain}
        ),
        model_gap AS (             -- две модели расходятся: повод перепроверить
          SELECT 'Модели расходятся' AS kind,
                 a.group_key         AS name,
                 (SELECT ANY_VALUE(category) FROM `{DS}.dim_product` d
                  WHERE d.group_key = a.group_key) AS category,
                 CAST(ROUND(a.growth_units - a.arima_units) AS INT64) AS units,
                 CONCAT('по росту ', CAST(ROUND(a.growth_units) AS STRING),
                        ', ARIMA ', CAST(ROUND(a.arima_units) AS STRING)) AS detail,
                 2 AS prio
          FROM (
            SELECT group_key,
                   SUM(IF(series = 'Our forecast (growth)', units, 0)) AS growth_units,
                   SUM(IF(series = 'Our forecast',          units, 0)) AS arima_units
            FROM `{DS}.looker_plan_compare`
            GROUP BY group_key
          ) a
          WHERE a.arima_units > 1000
            AND ABS(SAFE_DIVIDE(a.growth_units - a.arima_units, a.arima_units)) > 0.3
            {where_g.replace("group_key", "a.group_key") if where_g else ""}
        ),
        oos AS (                   -- теряем продажи из-за отсутствия товара
          SELECT 'Упущены продажи (OOS)', group_key, ANY_VALUE(category),
                 CAST(ROUND(SUM(demand_units - sales_units)) AS INT64),
                 CONCAT('за 3 мес, дней без остатка ',
                        CAST(ROUND(AVG(oos_days)) AS STRING)),
                 2
          FROM `{DS}.looker_fact_monthly`
          WHERE kind = 'actual'
            AND month >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH)
            AND demand_units > sales_units {where_g}
          GROUP BY group_key
          HAVING SUM(demand_units - sales_units) > 100
        )
        SELECT * FROM stockout
        UNION ALL SELECT * FROM overstock
        UNION ALL SELECT * FROM noplan
        UNION ALL SELECT * FROM oos
        UNION ALL SELECT * FROM model_gap
        ORDER BY prio, ABS(units) DESC
        LIMIT 200
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_stock(groups: tuple, categories: tuple) -> pd.DataFrame:
    """Остатки и движение по месяцам: что на складе, что приедет,
    сколько спланировано и что останется."""
    where: list[str] = []
    params: list = []
    if groups:
        where.append("p.group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    elif categories:
        where.append("d.category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run(
        f"""
        SELECT p.month,
               SUM(p.stock_bom)   AS stock_start,
               SUM(p.incoming)    AS incoming,
               SUM(p.plan_units)  AS plan_units,
               SUM(p.stock_eom)   AS stock_end,
               COUNTIF(p.is_stockout) AS stockout_skus
        FROM `{DS}.psi_projection` p
        LEFT JOIN (SELECT DISTINCT group_key, category
                   FROM `{DS}.dim_product` WHERE group_key IS NOT NULL) d
          ON d.group_key = p.group_key
        {clause}
        GROUP BY p.month
        ORDER BY p.month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_stockouts(groups: tuple, categories: tuple) -> pd.DataFrame:
    """Позиции, которые кончатся раньше всего."""
    where: list[str] = ["f.first_stockout_month IS NOT NULL"]
    params: list = []
    if groups:
        where.append("f.group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    elif categories:
        where.append("d.category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    return run(
        f"""
        SELECT f.group_key, f.asin, f.first_stockout_month,
               d.category,
               ROUND(SUM(p.plan_units)) AS plan_after
        FROM `{DS}.psi_first_stockout` f
        LEFT JOIN (SELECT DISTINCT group_key, category
                   FROM `{DS}.dim_product` WHERE group_key IS NOT NULL) d
          ON d.group_key = f.group_key
        LEFT JOIN `{DS}.psi_projection` p
          ON p.product_key = f.product_key AND p.month >= f.first_stockout_month
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2, 3, 4
        ORDER BY f.first_stockout_month, plan_after DESC
        LIMIT 100
        """,
        params,
    )


@st.cache_data(ttl=3600)
def load_config() -> dict:
    """Параметры планирования: границы сезона, lead time, пороги."""
    df = run(
        f"""
        SELECT season_start, season_end, lead_time_months,
               review_period_months, peak_months,
               service_level_peak, service_level_offpeak,
               growth_cap_lo, growth_cap_hi,
               overstock_red_ratio, understock_red_ratio,
               moq_default, carton_default
        FROM `{DS}.config` LIMIT 1
        """
    )
    return df.iloc[0].to_dict() if not df.empty else {}


@st.cache_data(ttl=300)
def load_target_stock(groups: tuple, months_ahead: int) -> pd.DataFrame:
    """Целевой остаток рядом с проекцией: видно, дотягиваем или нет."""
    where = ["p.month >= DATE_TRUNC(CURRENT_DATE(), MONTH)",
             "p.month < DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL @ahead MONTH)"]
    params: list = [bigquery.ScalarQueryParameter("ahead", "INT64", months_ahead)]
    if groups:
        where.append("p.group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))

    return run(
        f"""
        WITH proj AS (
          SELECT p.group_key,
                 IFNULL(d.color, '') AS color,
                 p.month,
                 SUM(p.stock_eom) AS stock_proj
          FROM `{DS}.psi_projection` p
          LEFT JOIN (SELECT DISTINCT product_key, color
                     FROM `{DS}.dim_product`) d USING (product_key)
          WHERE {' AND '.join(where)}
          GROUP BY 1, 2, 3
        )
        SELECT p.group_key, p.color, p.month,
               ROUND(p.stock_proj) AS stock_proj,
               t.target_units, t.author, t.updated_at
        FROM proj p
        LEFT JOIN `{DS}.target_stock_latest` t
          ON t.group_key = p.group_key
         AND IFNULL(t.color, '') = p.color
         AND t.month = p.month
        ORDER BY p.group_key, p.color, p.month
        """,
        params,
    )


def save_target_stock(rows: pd.DataFrame, author: str, note: str) -> int:
    payload = pd.DataFrame({
        "group_key": rows["group_key"].astype(str),
        "color": rows["color"].replace("", pd.NA),
        "month": pd.to_datetime(rows["month"]).dt.date,
        "target_units": rows["target_units"].astype(float),
        "note": note or None,
        "author": author,
        "updated_at": dt.datetime.now(dt.timezone.utc),
    })
    schema = [
        bigquery.SchemaField("group_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("color", "STRING"),
        bigquery.SchemaField("month", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("target_units", "FLOAT"),
        bigquery.SchemaField("note", "STRING"),
        bigquery.SchemaField("author", "STRING"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    client.load_table_from_dataframe(
        payload, f"{PROJECT}.forecast.target_stock",
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=schema),
    ).result()
    return len(payload)


@st.cache_data(ttl=300)
def load_curve_override(groups: tuple) -> pd.DataFrame:
    where = ""
    params: list = []
    if groups:
        where = "WHERE group_key IN UNNEST(@groups)"
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    return run(
        f"SELECT group_key, size, cal_month, share_pct, author "
        f"FROM `{DS}.size_curve_latest` {where}",
        params,
    )


def save_curve_override(rows: pd.DataFrame, author: str, note: str) -> int:
    payload = pd.DataFrame({
        "group_key": rows["group_key"].astype(str),
        "size": rows["size"].astype(str),
        "cal_month": rows["cal_month"].astype(int),
        "share_pct": rows["share_pct"].astype(float),
        "note": note or None,
        "author": author,
        "updated_at": dt.datetime.now(dt.timezone.utc),
    })
    schema = [
        bigquery.SchemaField("group_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("size", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("cal_month", "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("share_pct", "FLOAT"),
        bigquery.SchemaField("note", "STRING"),
        bigquery.SchemaField("author", "STRING"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    client.load_table_from_dataframe(
        payload, f"{PROJECT}.forecast.size_curve_override",
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=schema),
    ).result()
    return len(payload)


@st.cache_data(ttl=600)
def load_core_tail(groups: tuple, categories: tuple) -> pd.DataFrame:
    """Делит ассортимент на ядро и хвост внутри каждой группы и считает
    риск отдельно. Ядро — цвета, дающие первые 70% плана группы."""
    where: list[str] = []
    params: list = []
    if groups:
        where.append("ps.group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    if categories:
        where.append("ps.category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run(
        f"""
        WITH by_color AS (
          SELECT ps.group_key,
                 IFNULL(ps.color, '(нет)') AS color,
                 SUM(ps.plan_units) AS plan_units
          FROM `{DS}.plan_sales` ps
          {clause}
          GROUP BY 1, 2
        ),
        ranked AS (
          SELECT *,
                 SUM(plan_units) OVER (PARTITION BY group_key) AS grp_total,
                 SUM(plan_units) OVER (
                   PARTITION BY group_key ORDER BY plan_units DESC
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running,
                 ROW_NUMBER() OVER (
                   PARTITION BY group_key ORDER BY plan_units DESC) AS rn
          FROM by_color
        ),
        marked AS (
          SELECT group_key, color, plan_units, grp_total,
                 -- ядро: цвета до 70% объёма группы, но минимум один
                 IF(rn = 1 OR (running - plan_units) < grp_total * 0.7,
                    'основные', 'прочие') AS part
          FROM ranked
        ),
        risk AS (                    -- какие ячейки уходят в ноль
          SELECT dp.group_key,
                 IFNULL(dp.color, '(нет)') AS color,
                 COUNT(DISTINCT f.product_key) AS skus_total,
                 COUNT(DISTINCT IF(f.first_stockout_month IS NOT NULL,
                                   f.product_key, NULL)) AS skus_out,
                 MIN(f.first_stockout_month) AS first_out
          FROM `{DS}.psi_first_stockout` f
          JOIN (SELECT DISTINCT product_key, group_key, color
                FROM `{DS}.dim_product`) dp USING (product_key)
          GROUP BY 1, 2
        )
        SELECT m.group_key, m.color, m.part,
               ROUND(m.plan_units) AS plan_units,
               ROUND(SAFE_DIVIDE(m.plan_units, m.grp_total) * 100, 1) AS share_pct,
               IFNULL(r.skus_total, 0) AS skus_total,
               IFNULL(r.skus_out, 0)   AS skus_out,
               r.first_out
        FROM marked m
        LEFT JOIN risk r USING (group_key, color)
        ORDER BY m.group_key, m.plan_units DESC
        """,
        params,
    )


def save_overrides(rows: pd.DataFrame, author: str, note: str) -> int:
    payload = pd.DataFrame(
        {
            "group_key": rows["group_key"].astype(str),
            "color": rows["color"].replace("", pd.NA),
            "size": pd.Series([pd.NA] * len(rows), dtype="string"),
            "month": pd.to_datetime(rows["month"]).dt.date,
            "override_units": rows["override_units"].astype(float),
            "uplift_pct": pd.Series([pd.NA] * len(rows), dtype="Float64"),
            "note": note or None,
            "author": author,
            "updated_at": dt.datetime.now(dt.timezone.utc),
        }
    )
    # group_key в таблице объявлен REQUIRED — режим надо повторить,
    # иначе BigQuery считает загрузку изменением схемы и отклоняет её
    schema = [
        bigquery.SchemaField("group_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("color", "STRING"),
        bigquery.SchemaField("size", "STRING"),
        bigquery.SchemaField("month", "DATE"),
        bigquery.SchemaField("override_units", "FLOAT"),
        bigquery.SchemaField("uplift_pct", "FLOAT"),
        bigquery.SchemaField("note", "STRING"),
        bigquery.SchemaField("author", "STRING"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    client.load_table_from_dataframe(
        payload,
        TABLE_OVERRIDE,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND, schema=schema
        ),
    ).result()
    return len(payload)


def tab_intro(what: str, action: str) -> None:
    """Короткое пояснение в начале вкладки: что показано и что с этим делать."""
    st.markdown(
        f"<div style='background:#F7F8F9;border-left:3px solid #C9D2DA;"
        f"padding:10px 14px;margin-bottom:14px;border-radius:0 6px 6px 0;"
        f"font-size:0.9rem;line-height:1.5'>"
        f"<b>{what}</b><br><span style='color:#5A6B7A'>{action}</span></div>",
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ фильтры
try:
    dims = load_dims()
except Exception as exc:  # noqa: BLE001
    st.error(f"Нет доступа к данным: {exc}")
    st.stop()

with st.sidebar:
    st.markdown("### Фильтры")
    cats = st.multiselect("Категория", sorted(dims["category"].dropna().unique()))
    pool = dims if not cats else dims[dims["category"].isin(cats)]
    grps = st.multiselect("Группа", sorted(pool["group_key"].dropna().unique()))
    st.markdown("---")
    months_ahead = st.slider("Горизонт плана, месяцев", 3, 18, 12)
    st.caption(
        "План рассчитан до мая 2027 — дальше данных нет независимо "
        "от положения ползунка."
    )
    st.markdown("---")
    st.caption(f"Вы вошли как **{author}**")
    if st.button("Выйти", use_container_width=True):
        st.session_state.pop("user", None)
        st.query_params.clear()
        st.rerun()

g, c = tuple(grps), tuple(cats)

st.title("Sales Planner")
if not grps and not cats:
    scope = "все группы"
else:
    parts = []
    if cats:
        parts.append(", ".join(cats) if len(cats) <= 2 else f"{len(cats)} категорий")
    if grps:
        parts.append(", ".join(grps) if len(grps) <= 2 else f"{len(grps)} групп")
    scope = " · ".join(parts)
st.caption(f"Данные BigQuery · {scope}")

sec_act, sec_data, sec_plan, sec_check = st.tabs(
    ["Что делать", "Данные", "Планирование", "Сверка"]
)

with sec_act:
    tab_alert, tab_core, tab_needs = st.tabs(
        ["Внимание", "Основные и прочие", "Требует плана"])

with sec_data:
    tab_over, tab_track, tab_stock, tab_dims = st.tabs(
        ["Обзор", "Сезон", "Склад", "Разрезы"])

with sec_plan:
    tab_edit, tab_target, tab_curve = st.tabs(
        ["Ввод плана", "Цель по складу", "Размерные кривые"])

with sec_check:
    tab_cmp, tab_gap, tab_hist = st.tabs(
        ["План и факт", "Расхождения", "Правки"])

# ------------------------------------------------------------------ внимание
with tab_alert:
    tab_intro(
        "Что горит прямо сейчас.",
        "Сверху то, что требует решения на этой неделе: дефицит, позиции без "
        "плана, потери от отсутствия товара. Ниже — расхождение моделей и "
        "избыток запаса, если он есть. Учитывает фильтры слева."
    )
    try:
        al = load_alerts(g, c)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось собрать список: {exc}")
        al = pd.DataFrame()

    if al.empty:
        st.success("Ничего срочного не нашлось.")
    else:
        KIND_COLOR = {
            "Не хватит товара": "#B5524A",
            "Нужен ручной план": "#C8752B",
            "Упущены продажи (OOS)": "#C8752B",
            "Модели расходятся": "#7C8B99",
            "Останется на складе": "#7C8B99",
        }

        counts = al.groupby("kind")["units"].agg(["count", "sum"])
        cols = st.columns(len(counts))
        for col, (kind, row) in zip(cols, counts.iterrows()):
            col.metric(kind, int(row["count"]),
                       f"{row['sum']:+,.0f}".replace(",", " ") + " ед.")

        st.caption(
            "Сверху то, что горит. «Не хватит товара» — доступного меньше "
            "прогноза до конца сезона. «Упущены продажи» — разрыв между "
            "спросом и продажами за последние три месяца. «Модели расходятся» — "
            "растовый прогноз и ARIMA отличаются больше чем на 30%, стоит "
            "посмотреть группу глазами."
        )

        for kind in ["Не хватит товара", "Нужен ручной план",
                     "Упущены продажи (OOS)", "Модели расходятся",
                     "Останется на складе"]:
            part = al[al["kind"] == kind]
            if part.empty:
                continue

            st.markdown(
                f"<span style='color:{KIND_COLOR.get(kind, INK)};font-weight:600'>"
                f"{kind}</span> · {len(part)}",
                unsafe_allow_html=True,
            )
            show = part[["name", "category", "units", "detail"]].head(15).rename(
                columns={"name": "Позиция", "category": "Категория",
                         "units": "Единиц", "detail": "Подробности"})
            st.dataframe(show, hide_index=True, use_container_width=True,
                         height=min(400, 40 + 35 * len(show)))
            if len(part) > 15:
                st.caption(f"…и ещё {len(part) - 15}")
            st.write("")

# ------------------------------------------------------------------ обзор
with tab_over:
    tab_intro(
        "Спрос за последний год и прогноз на следующий.",
        "Быстро понять, растём или падаем и чего ждать. Ниже — сколько продаж "
        "потеряли из-за отсутствия товара на складе."
    )
    s = load_series(g, c)
    if s.empty:
        st.info("По этим фильтрам данных нет.")
    else:
        s["month"] = pd.to_datetime(s["month"])
        last_fact = s.loc[s["fact"].notna(), "month"].max()
        fact_12 = s[(s["month"] > last_fact - pd.DateOffset(months=12))
                    & s["fact"].notna()]["fact"].sum()
        fact_prev = s[(s["month"] > last_fact - pd.DateOffset(months=24))
                      & (s["month"] <= last_fact - pd.DateOffset(months=12))]["fact"].sum()
        fcst_12 = s[s["forecast"].notna()].head(12)["forecast"].sum()
        yoy = (fact_12 / fact_prev - 1) * 100 if fact_prev else None

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Спрос за 12 мес", f"{fact_12:,.0f}".replace(",", " "))
        k2.metric("Год к году", f"{yoy:+.1f}%" if yoy is not None else "—")
        fcst_n = int(s[s["forecast"].notna()].head(12).shape[0])
        k3.metric(f"Прогноз, {fcst_n} мес",
                  f"{fcst_12:,.0f}".replace(",", " "),
                  help="Столько месяцев покрыто планом")
        k4.metric("Факт по", last_fact.strftime("%b %Y"))

        fig = go.Figure()
        fc = s[s["forecast"].notna()]
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["hi_80"], mode="lines",
                                 line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["lo_80"], mode="lines", fill="tonexty",
                                 fillcolor=BAND, line=dict(width=0),
                                 name="интервал 80%", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=s["month"], y=s["fact"], mode="lines",
                                 line=dict(color=FACT, width=2.5), name="спрос (факт)",
                                 connectgaps=False))
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["forecast"], mode="lines",
                                 line=dict(color=FCST, width=2.5), name="прогноз"))
        fig.update_layout(
            height=430, margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            font=dict(color=INK, size=13),
            hovermode="x unified",
            legend=dict(orientation="h", y=1.08, x=0, bgcolor="rgba(0,0,0,0)"),
            xaxis=dict(showgrid=False, linecolor="#DDE3E8"),
            yaxis=dict(gridcolor="#EEF1F4", zeroline=False, title="единиц"),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Факт обрывается на последнем закрытом месяце. Дальше — прогноз "
            "по росту год к году: тот же, на котором строится план и заказ. "
            "Интервал показывает разброс модели ARIMA, она считается "
            "параллельно и служит для сверки (см. «План и факт»)."
        )

        st.markdown("**Продажи и упущенный спрос**")
        st.caption(
            "Разрыв между спросом и продажами — то, что не купили из-за отсутствия "
            "товара. Столбцы показывают средние дни без остатка."
        )
        o = load_oos(g, c)
        if not o.empty:
            o["month"] = pd.to_datetime(o["month"])
            o["lost"] = (o["demand"] - o["sales"]).clip(lower=0)
            f2 = go.Figure()
            f2.add_trace(go.Bar(
                x=o["month"], y=o["oos_weighted"], name="дней без остатка",
                marker_color="#E4DFD6", yaxis="y2",
                hovertemplate="%{y:.1f} дн<extra></extra>"))
            f2.add_trace(go.Scatter(
                x=o["month"], y=o["sales"], mode="lines", name="продано",
                line=dict(color=FACT, width=2.5)))
            f2.add_trace(go.Scatter(
                x=o["month"], y=o["demand"], mode="lines", name="спрос с поправкой",
                line=dict(color=FCST, width=2, dash="dot")))
            f2.update_layout(
                height=300, margin=dict(l=0, r=0, t=10, b=0),
                plot_bgcolor="white", paper_bgcolor="white", hovermode="x unified",
                font=dict(color=INK, size=12), barmode="overlay",
                legend=dict(orientation="h", y=1.12, x=0),
                xaxis=dict(showgrid=False, linecolor="#DDE3E8"),
                yaxis=dict(gridcolor="#EEF1F4", zeroline=False, title="единиц"),
                yaxis2=dict(overlaying="y", side="right", showgrid=False,
                            title="дней", range=[0, 31]),
            )
            st.plotly_chart(f2, use_container_width=True)
            lost = o["lost"].sum()
            if lost > 0:
                st.caption(
                    f"Всего упущено за период: {lost:,.0f} ед.".replace(",", " ")
                )

# ------------------------------------------------------------------ сезон
with tab_track:
    cfg = load_config()
    if cfg:
        s_start = pd.to_datetime(cfg["season_start"]).strftime("%d.%m.%Y")
        s_end = pd.to_datetime(cfg["season_end"]).strftime("%d.%m.%Y")
        months = (pd.to_datetime(cfg["season_end"]).to_period("M")
                  - pd.to_datetime(cfg["season_start"]).to_period("M")).n + 1
        season_line = (
            f"Сезон задан с {s_start} по {s_end} — это {months} мес. "
            f"Запас считается против спроса за этот период, поэтому годовой "
            f"объём товара выглядит как избыток. Границу сезона задаёт "
            f"настройка в системе, не прогноз."
        )
    else:
        season_line = "Границы сезона берутся из настроек системы."

    tab_intro(
        "Хватит ли товара до конца сезона.",
        "Считается так: на руках плюс в пути минус прогноз спроса. "
        "Красное — решать сейчас, серое — прогноза нет.<br>"
        f"<i>{season_line}</i>"
    )
    level = st.radio("Уровень", ["Категории", "Группы"], horizontal=True,
                     label_visibility="collapsed")
    # если выбрана группа, а показываем категории — фильтруем по категориям
    # этих групп, иначе фильтр молча игнорируется
    cats_eff = c
    if level == "Категории" and grps and not cats:
        cats_eff = tuple(dims[dims["group_key"].isin(grps)]["category"]
                         .dropna().unique())
    tr = load_on_track("category" if level == "Категории" else "group",
                       g, cats_eff)

    if tr.empty:
        st.info("Оценка по сезону пока не собрана.")
    else:
        red = int((tr["status_rank"] == 1).sum())
        amber = int((tr["status_rank"] == 2).sum())
        green = int((tr["status_rank"] == 3).sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Требуют решения", red)
        m2.metric("Под наблюдением", amber)
        m3.metric("В норме", green)
        m4.metric("Спрос до конца сезона",
                  f"{tr['forecast_demand'].sum():,.0f}".replace(",", " "))

        st.caption(
            "Хватит ли товара до конца сезона: на руках плюс в пути минус "
            "прогноз спроса. Красное — решать сейчас."
        )

        show = tr.rename(columns={
            "name": "Позиция", "on_hand": "На руках", "on_order": "В пути",
            "available": "Доступно", "forecast_demand": "Спрос до конца сезона",
            "sold_season_to_date": "Продано в сезоне",
            "proj_leftover": "Останется", "cover_months": "Покрытие, мес",
            "status": "Статус",
        }).drop(columns=["leftover_ratio", "status_rank"])

        st.dataframe(
            show, hide_index=True, use_container_width=True, height=520,
            column_config={
                "Останется": st.column_config.NumberColumn(
                    help="Минус — не хватит товара, плюс — останется на складе"),
                "Покрытие, мес": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        if cfg:
            with st.expander("Параметры, по которым это считается"):
                p1, p2 = st.columns(2)
                with p1:
                    st.markdown(
                        f"**Сезон:** {s_start} — {s_end} ({months} мес)  \n"
                        f"**Срок поставки:** {cfg['lead_time_months']} мес  \n"
                        f"**Периодичность заказа:** {cfg['review_period_months']} мес  \n"
                        f"**Пиковые месяцы:** {cfg['peak_months']}"
                    )
                with p2:
                    st.markdown(
                        f"**Уровень сервиса в пик:** {cfg['service_level_peak']:.0%}  \n"
                        f"**Вне пика:** {cfg['service_level_offpeak']:.0%}  \n"
                        f"**Порог «перезатарены»:** {cfg['overstock_red_ratio']:.0%} излишка  \n"
                        f"**Порог «не хватит»:** {cfg['understock_red_ratio']:.0%} дефицита"
                    )
                st.caption(
                    "Эти значения задаются в таблице настроек и меняются "
                    "бизнесом, а не разработчиком. От них напрямую зависит, "
                    "какие позиции попадут в красное."
                )

# ------------------------------------------------------------------ склад
with tab_stock:
    tab_intro(
        "Что на складе сейчас и что будет по месяцам.",
        "Начальный остаток плюс приход минус план равно остаток на конец. "
        "Если план больше доступного — товар кончится, и это видно заранее."
    )

    try:
        stk = load_stock(g, c)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось прочитать проекцию склада: {exc}")
        stk = pd.DataFrame()

    if stk.empty:
        st.info("По этим фильтрам проекции нет.")
    else:
        stk["month"] = pd.to_datetime(stk["month"])
        # в проекции бывают пустые значения — приводим к числам,
        # иначе арифметика по метрикам падает
        for col in ["stock_start", "incoming", "plan_units",
                    "stock_end", "stockout_skus"]:
            stk[col] = pd.to_numeric(stk[col], errors="coerce").fillna(0)

        first = stk.iloc[0]
        total_in = float(stk["incoming"].sum())
        total_plan = float(stk["plan_units"].sum())
        last_stock = float(stk.iloc[-1]["stock_end"])
        start_stock = float(first["stock_start"])
        lost = start_stock + total_in - total_plan - last_stock

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("На складе сейчас",
                  f"{start_stock:,.0f}".replace(",", " "))
        k2.metric("Придёт за период",
                  f"{total_in:,.0f}".replace(",", " "))
        k3.metric("Уйдёт по плану",
                  f"{total_plan:,.0f}".replace(",", " "))
        k4.metric("Останется в конце",
                  f"{last_stock:,.0f}".replace(",", " "),
                  help="Если близко к нулю — товара впритык")

        if abs(lost) > max(total_plan * 0.02, 100):
            st.caption(
                f"Простая арифметика (начало + приход − план) даёт "
                f"{start_stock + total_in - total_plan:,.0f}".replace(",", " ")
                + f", а по расчёту остаётся {last_stock:,.0f}".replace(",", " ")
                + ". Разница в том, что остаток считается по каждому SKU "
                "отдельно и не уходит в минус: на FBA неудовлетворённый спрос "
                "теряется, а не переносится на следующий месяц. "
                f"Расхождение {abs(lost):,.0f} ед.".replace(",", " ")
                + " и есть тот самый непроданный из-за дефицита объём."
            )

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=stk["month"], y=stk["incoming"], name="приход",
            marker_color="#8FAF9A",
            hovertemplate="приход %{y:,.0f}<extra></extra>"))
        fig.add_trace(go.Bar(
            x=stk["month"], y=-stk["plan_units"], name="план продаж",
            marker_color="#D8A48F",
            hovertemplate="план %{y:,.0f}<extra></extra>"))
        fig.add_trace(go.Scatter(
            x=stk["month"], y=stk["stock_end"], name="остаток на конец месяца",
            mode="lines+markers", line=dict(color=FACT, width=2.5),
            hovertemplate="остаток %{y:,.0f}<extra></extra>"))
        fig.update_layout(
            height=400, margin=dict(l=0, r=0, t=10, b=0), barmode="relative",
            plot_bgcolor="white", paper_bgcolor="white", hovermode="x unified",
            font=dict(color=INK, size=12),
            legend=dict(orientation="h", y=1.12, x=0),
            xaxis=dict(showgrid=False, linecolor="#DDE3E8"),
            yaxis=dict(gridcolor="#EEF1F4", zeroline=True,
                       zerolinecolor="#C9D2DA", title="единиц"),
        )
        st.plotly_chart(fig, use_container_width=True)

        tbl = stk.copy()
        tbl["Месяц"] = tbl["month"].dt.strftime("%Y-%m")
        tbl = tbl[["Месяц", "stock_start", "incoming", "plan_units",
                   "stock_end", "stockout_skus"]].rename(columns={
            "stock_start": "На начало", "incoming": "Приход",
            "plan_units": "План продаж", "stock_end": "На конец",
            "stockout_skus": "SKU без остатка",
        })
        for col in ["На начало", "Приход", "План продаж", "На конец"]:
            tbl[col] = tbl[col].round(0)
        st.dataframe(tbl, hide_index=True, use_container_width=True, height=330)

        st.markdown("**Что кончится раньше всего**")
        try:
            so = load_stockouts(g, c)
            if so.empty:
                st.caption("Ни одна позиция не уходит в ноль на горизонте плана.")
            else:
                so["first_stockout_month"] = pd.to_datetime(
                    so["first_stockout_month"]).dt.strftime("%Y-%m")
                show = so.head(25).rename(columns={
                    "group_key": "Группа", "asin": "ASIN",
                    "first_stockout_month": "Кончится в",
                    "category": "Категория",
                    "plan_after": "План после этого месяца",
                })
                st.dataframe(show, hide_index=True, use_container_width=True,
                             height=400)
                st.caption(
                    "«План после этого месяца» — сколько ещё планировали продать "
                    "после того, как товар кончится. Это и есть упущенные продажи, "
                    "если ничего не заказать."
                )
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Список недоступен: {exc}")

# ------------------------------------------------------------------ ядро и хвост
with tab_core:
    tab_intro(
        "Основные цвета против прочих — где на самом деле теряются деньги.",
        "По чёрному и серому запас обычно есть, а мелкие цвета и крайние "
        "размеры уходят в ноль. В штуках склад выглядит нормально, "
        "а половина ассортимента при этом недоступна покупателю."
    )

    try:
        ct = load_core_tail(g, c)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось посчитать: {exc}")
        ct = pd.DataFrame()

    if ct.empty:
        st.info("По этим фильтрам данных нет.")
    else:
        agg = ct.groupby("part").agg(
            colors=("color", "count"),
            plan=("plan_units", "sum"),
            skus=("skus_total", "sum"),
            out=("skus_out", "sum"),
        ).reindex(["основные", "прочие"]).fillna(0)

        cols = st.columns(2)
        for col, part in zip(cols, ["основные", "прочие"]):
            row = agg.loc[part]
            risk_pct = (row["out"] / row["skus"] * 100) if row["skus"] else 0
            with col:
                st.markdown(
                    f"**{part.capitalize()}** · {int(row['colors'])} цветов"
                )
                a, b = st.columns(2)
                a.metric("План, ед.",
                         f"{row['plan']:,.0f}".replace(",", " "))
                b.metric("SKU в риске",
                         f"{int(row['out'])} из {int(row['skus'])}",
                         f"{risk_pct:.0f}%",
                         delta_color="inverse")

        core_risk = (agg.loc["основные", "out"] / agg.loc["основные", "skus"] * 100
                     if agg.loc["основные", "skus"] else 0)
        tail_risk = (agg.loc["прочие", "out"] / agg.loc["прочие", "skus"] * 100
                     if agg.loc["прочие", "skus"] else 0)

        if tail_risk > core_risk * 1.3 and agg.loc["прочие", "skus"] > 0:
            st.warning(
                f"Среди прочих цветов {tail_risk:.0f}% позиций уйдут в ноль "
                f"против {core_risk:.0f}% у основных. Это та самая потеря на мелких "
                f"цветах: объём плана небольшой, но каждая недоступная "
                f"позиция — это ещё и просмотры, которые уходят конкурентам."
            )
        elif core_risk > tail_risk:
            st.info(
                f"Риск выше у основных цветов ({core_risk:.0f}% против {tail_risk:.0f}%). "
                "Необычная ситуация — стоит проверить поставки по основным цветам."
            )

        st.markdown("**По цветам**")
        show = ct.copy()
        show["Риск"] = show.apply(
            lambda r: f"{r['skus_out']} из {r['skus_total']}"
            if r["skus_total"] else "—", axis=1)
        show["first_out"] = pd.to_datetime(
            show["first_out"], errors="coerce").dt.strftime("%Y-%m")
        show = show[["group_key", "color", "part", "plan_units", "share_pct",
                     "Риск", "first_out"]].rename(columns={
            "group_key": "Группа", "color": "Цвет", "part": "Часть",
            "plan_units": "План", "share_pct": "Доля, %",
            "first_out": "Первый стокаут",
        })
        st.dataframe(
            show, hide_index=True, use_container_width=True, height=500,
            column_config={
                "План": st.column_config.NumberColumn(format="%.0f"),
                "Доля, %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(
            "Основные — цвета, дающие первые 70% плана группы. Остальные прочие. "
            "«Риск» — сколько SKU этого цвета уходят в ноль на горизонте плана."
        )

# ------------------------------------------------------------------ цель по складу
with tab_target:
    tab_intro(
        "Сколько товара хотим иметь на складе — и сколько будет по расчёту.",
        "Цель задаётся руками по группе и цвету. Рядом проекция: что получится, "
        "если ничего не менять. Разница показывает, чего не хватает в заказе."
    )

    if not grps:
        st.info("Выберите группу слева — цель задаётся по одной группе за раз.")
    else:
        try:
            tg = load_target_stock(tuple(grps[:5]), months_ahead)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Не удалось прочитать: {exc}")
            tg = pd.DataFrame()

        if tg.empty:
            st.info("Проекции по этим группам нет.")
        else:
            tg["месяц"] = pd.to_datetime(tg["month"]).dt.strftime("%Y-%m")

            set_cnt = int(tg["target_units"].notna().sum())
            gap = (tg["target_units"].fillna(0) - tg["stock_proj"]).clip(lower=0).sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Ячеек", len(tg))
            m2.metric("Цель задана", set_cnt)
            m3.metric("Не хватит до цели",
                      f"{gap:,.0f}".replace(",", " "),
                      help="Сумма недостачи там, где цель выше проекции")

            view_mode = st.radio("Показать", ["Цель", "Проекция", "Разница"],
                                 horizontal=True, label_visibility="collapsed")

            if view_mode == "Проекция":
                pv = tg.pivot_table(index=["group_key", "color"], columns="месяц",
                                    values="stock_proj", aggfunc="sum").reset_index()
                for mc in [x for x in pv.columns if x not in ("group_key", "color")]:
                    pv[mc] = pd.to_numeric(pv[mc], errors="coerce").astype("float64")
                pv = pv.rename(columns={"group_key": "Группа", "color": "Цвет"})
                st.dataframe(pv, hide_index=True, use_container_width=True,
                             height=440)
                st.caption("Остаток на конец месяца по текущему плану и приходам.")

            elif view_mode == "Разница":
                # без заданной цели разницы не существует — не показываем ноль
                tg["разница"] = tg["target_units"] - tg["stock_proj"]
                pv = tg.pivot_table(index=["group_key", "color"], columns="месяц",
                                    values="разница", aggfunc="sum",
                                    dropna=False).reset_index()
                cnt = tg.pivot_table(index=["group_key", "color"], columns="месяц",
                                     values="разница", aggfunc="count").reset_index()
                for mc in [x for x in pv.columns if x not in ("group_key", "color")]:
                    vals = pd.to_numeric(pv[mc], errors="coerce")
                    if mc in cnt.columns:
                        empty = pd.to_numeric(cnt[mc], errors="coerce").fillna(0) == 0
                        vals = vals.mask(empty)
                    pv[mc] = vals.astype("float64")
                pv = pv.rename(columns={"group_key": "Группа", "color": "Цвет"})
                st.dataframe(pv, hide_index=True, use_container_width=True,
                             height=440)
                st.caption(
                    "Плюс — цель выше расчёта, столько не хватает. "
                    "Минус — товара будет больше цели. Пусто — цель не задана."
                )

            else:
                # pivot_table по умолчанию схлопывает NaN в 0 — нам нужна
                # разница между «цель не задана» и «цель равна нулю»
                grid_t = tg.pivot_table(index=["group_key", "color"],
                                        columns="месяц", values="target_units",
                                        aggfunc="sum", dropna=False).reset_index()
                months_t = [x for x in grid_t.columns
                            if x not in ("group_key", "color")]
                has_t = tg.pivot_table(index=["group_key", "color"],
                                       columns="месяц", values="target_units",
                                       aggfunc="count").reset_index()
                for mc in months_t:
                    vals = pd.to_numeric(grid_t[mc], errors="coerce")
                    if mc in has_t.columns:
                        cnt = has_t.set_index(["group_key", "color"])[mc]
                        idx = grid_t.set_index(["group_key", "color"]).index
                        empty = [float(cnt.get(i, 0) or 0) == 0 for i in idx]
                        vals = vals.mask(pd.Series(empty, index=vals.index))
                    # float64 с NaN — Streamlit рисует пустую ячейку,
                    # в отличие от Int64/object, где появляется None
                    grid_t[mc] = vals.astype("float64")

                st.caption("Впишите целевой остаток. Пусто — цель не задана.")
                col_cfg_t = {
                    "group_key": st.column_config.TextColumn("Группа", disabled=True),
                    "color": st.column_config.TextColumn("Цвет", disabled=True),
                }
                for mc in months_t:
                    col_cfg_t[mc] = st.column_config.NumberColumn(
                        mc, format="%.0f", min_value=0, step=1)

                ed_t = st.data_editor(
                    grid_t, hide_index=True, use_container_width=True, height=440,
                    column_config=col_cfg_t, key="target_grid",
                )

                mcols_t = [x for x in grid_t.columns
                           if x not in ("group_key", "color")]
                before_t = grid_t.set_index(["group_key", "color"])[mcols_t]
                after_t = ed_t.set_index(["group_key", "color"])[mcols_t]
                diff_t = (after_t != before_t) & after_t.notna()

                changes_t = [
                    {"group_key": gk, "color": cl, "month": f"{m}-01",
                     "target_units": after_t.loc[(gk, cl), m]}
                    for (gk, cl), row in diff_t.iterrows()
                    for m in mcols_t if row[m]
                ]
                changed_t = pd.DataFrame(changes_t)

                a, b = st.columns([3, 1])
                note_t = a.text_input("Комментарий", key="target_note")
                b.metric("Изменено", len(changed_t))

                if st.button("Сохранить цель", type="primary",
                             disabled=changed_t.empty):
                    try:
                        n = save_target_stock(changed_t, author, note_t)
                        st.cache_data.clear()
                        st.success(f"Сохранено: {n}")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Не сохранилось: {exc}")

# ------------------------------------------------------------------ требует плана
with tab_needs:
    tab_intro(
        "Позиции, которым модель не смогла дать прогноз.",
        "Новая линейка без истории продаж или не проставлен Order US. "
        "План по ним ставится руками — других вариантов нет."
    )
    needs = load_needs_plan()
    if needs.empty:
        st.success("Все позиции получили прогноз. Ручное планирование не требуется.")
    else:
        st.metric("Позиций без прогноза", len(needs))
        st.caption(
            "Модели не на чем учиться: новая линейка без истории продаж или "
            "не проставлен Order US. План по ним ставится руками."
        )
        show = needs.rename(columns={
            "group_key": "Группа", "color": "Цвет", "size": "Размер",
            "product_key": "Ключ", "order_us": "Order US",
            "name": "Наименование", "reason": "Почему",
        })
        st.dataframe(show, hide_index=True, use_container_width=True, height=520)

# ------------------------------------------------------------------ разрезы
with tab_dims:
    tab_intro(
        "Из чего складывается план: размеры, цвета, категории, ABCD.",
        "Смотреть, чтобы понять структуру спроса — например, какая доля "
        "приходится на чёрный или на размер L. Цифры те же, что во "
        "«Вводе плана»."
    )
    col1, col2 = st.columns(2)
    targets = [
        (col1, "size", "Размеры"),
        (col2, "color", "Цвета"),
        (col1, "category", "Категории"),
        (col2, "abcd_class", "ABCD-класс"),
    ]
    for col, dim, title in targets:
        d = load_breakdown(dim, g, c)
        with col:
            st.markdown(f"**{title}**")
            if d.empty:
                st.caption("нет данных")
            else:
                fig = go.Figure(go.Bar(
                    x=d["units"], y=d["label"], orientation="h",
                    marker_color=FACT, hovertemplate="%{y}: %{x:,.0f}<extra></extra>"))
                fig.update_layout(
                    height=max(220, 26 * len(d)), margin=dict(l=0, r=0, t=4, b=0),
                    plot_bgcolor="white", paper_bgcolor="white",
                    font=dict(color=INK, size=12),
                    xaxis=dict(gridcolor="#EEF1F4", zeroline=False),
                    yaxis=dict(autorange="reversed"),
                )
                st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------------ ввод
with tab_edit:
    tab_intro(
        "Ручной ввод плана продаж по группе и цвету.",
        "Цифры считаются автоматически — правьте только там, где знаете "
        "больше модели. Размеры раскладываются сами. Правки переживают "
        "пересборку и попадают в расчёт на следующий день."
    )
    planned = load_planned_groups()
    editable = [x for x in grps if x in planned]
    skipped = [x for x in grps if x not in planned]

    if skipped:
        st.caption(
            "Без плана: " + ", ".join(skipped) +
            " — модель не дала прогноз (нет истории продаж или не проставлен "
            "Order US). Такие позиции смотрите во вкладке «Требует плана»."
        )

    if not editable:
        if grps:
            st.info(
                "У выбранных групп плана нет. Выберите другие — редактировать "
                f"можно {len(planned)} групп из {len(set(dims['group_key'].dropna()))}."
            )
        else:
            st.info("Выберите группы в фильтрах слева. Удобнее по одной категории за раз.")
    elif len(editable) > 8:
        st.warning(
            f"Выбрано {len(editable)} групп — в такой сетке неудобно работать. "
            "Оставьте до восьми: план ставят по одной категории за раз."
        )
    else:
        df = load_plan(months_ahead, tuple(editable))
        if df.empty:
            st.info("На этот горизонт плана нет.")
        else:
            df["месяц"] = pd.to_datetime(df["month"]).dt.strftime("%Y-%m")
            df["план"] = df["override_units"].fillna(df["plan_auto"])

            manual_cnt = int(df["override_units"].notna().sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Ячеек в плане", len(df))
            c2.metric("Правок сохранено", manual_cnt,
                      help="Сколько ячеек уже переопределено вручную и лежит в базе")
            c3.metric("План на период",
                      f"{df['план'].sum():,.0f}".replace(",", " "))

            show_all = st.toggle(
                "Показать расчёт системы и прошлый год",
                help="Сравнение: что посчитала модель, что было год назад, "
                     "и что стоит сейчас",
            )

            if show_all:
                for c_num in ["fact_ly", "plan_auto", "override_units", "план"]:
                    df[c_num] = pd.to_numeric(df[c_num],
                                              errors="coerce").astype("float64")
                cmp_tbl = df[["group_key", "color", "месяц", "fact_ly",
                              "plan_auto", "override_units", "план"]].rename(
                    columns={
                        "group_key": "Группа", "color": "Цвет", "месяц": "Месяц",
                        "fact_ly": "Год назад", "plan_auto": "Расчёт системы",
                        "override_units": "Правка руками", "план": "Итог",
                    })
                st.dataframe(cmp_tbl, hide_index=True,
                             use_container_width=True, height=360)
                st.caption(
                    "Пустая «Правка руками» — берётся расчёт системы. "
                    "Ниже — сетка для ввода."
                )

            grid = df.pivot_table(index=["group_key", "color"], columns="месяц",
                                  values="план", aggfunc="sum").reset_index()
            # float64 с NaN рисуется пустой ячейкой, а не словом None
            month_cols = [x for x in grid.columns if x not in ("group_key", "color")]
            for mc in month_cols:
                grid[mc] = pd.to_numeric(grid[mc], errors="coerce").astype("float64")

            st.caption(
                "Правьте цифры прямо в таблице, потом прокрутите вниз — "
                "под таблицей кнопка сохранения."
            )
            col_cfg = {
                "group_key": st.column_config.TextColumn("Группа", disabled=True,
                                                         width="medium"),
                "color": st.column_config.TextColumn("Цвет", disabled=True,
                                                     width="medium"),
            }
            for mc in month_cols:
                col_cfg[mc] = st.column_config.NumberColumn(
                    mc, format="%.0f", min_value=0, step=1)

            edited = st.data_editor(
                grid, hide_index=True, use_container_width=True, height=480,
                column_config=col_cfg, key="grid",
            )

            mcols = [x for x in grid.columns if x not in ("group_key", "color")]
            before = grid.set_index(["group_key", "color"])[mcols]
            after = edited.set_index(["group_key", "color"])[mcols]
            diff = (after != before) & after.notna()

            changes = [
                {"group_key": gk, "color": cl, "month": f"{m}-01",
                 "override_units": after.loc[(gk, cl), m]}
                for (gk, cl), row in diff.iterrows() for m in mcols if row[m]
            ]
            changed = pd.DataFrame(changes)

            a, b = st.columns([3, 1])
            note = a.text_input("Комментарий")
            b.metric("Изменено", len(changed))

            if st.button("Сохранить план", type="primary", disabled=changed.empty):
                try:
                    n = save_overrides(changed, author, note)
                    st.cache_data.clear()
                    st.success(f"Сохранено: {n}. В отчётах появится после пересборки.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Не сохранилось: {exc}")

# ------------------------------------------------------------------ кривые
with tab_curve:
    tab_intro(
        "Доля каждого размера внутри группы, по месяцам.",
        "По этим долям прогноз группы раскладывается на размеры. Считается "
        "из истории спроса, внизу можно поправить руками."
    )

    if not grps:
        st.info("Выберите группу слева — кривые показываются по одной группе.")
    else:
        cur = load_size_curve(tuple(grps[:3]))
        if cur.empty:
            st.info("По этим группам истории нет.")
        else:
            MONTHS = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
                      7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}
            SIZE_ORDER = ["XXS", "X-Small", "XS", "Small", "S", "Medium", "M",
                          "Large", "L", "X-Large", "XL", "XX-Large", "XXL",
                          "XXX-Large", "XXXL", "One size", "One Size"]

            def size_key(s: str) -> tuple:
                s = str(s)
                for i, name in enumerate(SIZE_ORDER):
                    if s.lower() == name.lower():
                        return (0, i, s)
                return (1, 0, s)

            for gk in cur["group_key"].unique():
                sub = cur[cur["group_key"] == gk]
                sizes = sorted(sub["size"].unique(), key=size_key)

                st.markdown(f"**{gk}**")

                if len(sizes) < 2:
                    st.caption(
                        f"У группы один размер ({sizes[0] if sizes else '—'}) — "
                        "раскладывать нечего, весь прогноз идёт на него."
                    )
                    st.divider()
                    continue

                piv = sub.pivot_table(index="size", columns="cal_month",
                                      values="share_pct", aggfunc="sum")
                dem = sub.pivot_table(index="size", columns="cal_month",
                                      values="demand", aggfunc="sum")
                months = sorted(piv.columns)
                piv = piv.reindex(index=sizes, columns=months)
                dem = dem.reindex(index=sizes, columns=months)

                # месяцы, где у группы вообще не было спроса
                month_demand = dem.sum(axis=0, min_count=1).fillna(0)
                dead = [m for m in months if month_demand.get(m, 0) == 0]
                live = [m for m in months if m not in dead]

                fig = go.Figure()
                for size in sizes:
                    fig.add_trace(go.Scatter(
                        x=[MONTHS.get(m, m) for m in live],
                        y=[piv.loc[size, m] for m in live],
                        mode="lines+markers", name=str(size), line=dict(width=2),
                        hovertemplate="%{y:.1f}%<extra>" + str(size) + "</extra>"))
                fig.update_layout(
                    height=320, margin=dict(l=0, r=0, t=6, b=0),
                    plot_bgcolor="white", paper_bgcolor="white",
                    font=dict(color=INK, size=12), hovermode="x unified",
                    legend=dict(orientation="h", y=1.15, x=0),
                    xaxis=dict(showgrid=False, linecolor="#DDE3E8"),
                    yaxis=dict(gridcolor="#EEF1F4", zeroline=False,
                               title="доля, %", rangemode="tozero"),
                )
                st.plotly_chart(fig, use_container_width=True)

                if dead:
                    st.caption(
                        "Месяцы без продаж у группы (доля не считается): " +
                        ", ".join(MONTHS.get(m, str(m)) for m in dead)
                    )

                tbl = piv[live].round(1)
                tbl.columns = [MONTHS.get(m, m) for m in live]
                tbl = tbl.reset_index().rename(columns={"size": "Размер"})
                st.dataframe(tbl, hide_index=True, use_container_width=True)

                with st.expander("Спрос, на котором посчитаны доли"):
                    dtb = dem[live].fillna(0).astype(int)
                    dtb.columns = [MONTHS.get(m, m) for m in live]
                    dtb = dtb.reset_index().rename(columns={"size": "Размер"})
                    st.dataframe(dtb, hide_index=True, use_container_width=True)
                    st.caption(
                        "Чем меньше спрос в месяце, тем случайнее доля. "
                        "Пара десятков штук — уже шум, а не сезонность."
                    )

                st.divider()

            st.divider()
            st.markdown("**Правка кривой руками**")
            st.caption(
                "Впишите долю в процентах там, где расчёт не отражает реальность. "
                "Пусто — берётся расчёт из истории. Доли нормируются "
                "автоматически, сумма по месяцу приводится к 100%."
            )

            gk_edit = st.selectbox(
                "Группа для правки",
                [x for x in cur["group_key"].unique()
                 if len(cur[cur["group_key"] == x]["size"].unique()) > 1],
                key="curve_group",
            )

            if gk_edit:
                MONTHS_ALL = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май",
                              6: "июн", 7: "июл", 8: "авг", 9: "сен", 10: "окт",
                              11: "ноя", 12: "дек"}
                sub_e = cur[cur["group_key"] == gk_edit]
                sizes_e = sorted(sub_e["size"].unique(), key=size_key)

                try:
                    ovr_c = load_curve_override((gk_edit,))
                except Exception:  # noqa: BLE001
                    ovr_c = pd.DataFrame(
                        columns=["group_key", "size", "cal_month", "share_pct"])

                base = sub_e.pivot_table(index="size", columns="cal_month",
                                         values="share_pct", aggfunc="sum")
                base = base.reindex(index=sizes_e, columns=range(1, 13))

                if not ovr_c.empty:
                    for _, r in ovr_c.iterrows():
                        if r["size"] in base.index and r["cal_month"] in base.columns:
                            base.loc[r["size"], r["cal_month"]] = r["share_pct"]

                grid_c = base.round(1).copy()
                grid_c.columns = [MONTHS_ALL[m] for m in grid_c.columns]
                grid_c = grid_c.reset_index().rename(columns={"size": "Размер"})

                ed_c = st.data_editor(
                    grid_c, hide_index=True, use_container_width=True,
                    column_config={
                        "Размер": st.column_config.TextColumn(disabled=True),
                    },
                    key=f"curve_edit_{gk_edit}",
                )

                mon_cols = [c for c in grid_c.columns if c != "Размер"]
                before_c = grid_c.set_index("Размер")[mon_cols]
                after_c = ed_c.set_index("Размер")[mon_cols]
                diff_c = (after_c != before_c) & after_c.notna()

                rev = {v: k for k, v in MONTHS_ALL.items()}
                changes_c = [
                    {"group_key": gk_edit, "size": sz, "cal_month": rev[mn],
                     "share_pct": after_c.loc[sz, mn]}
                    for sz, row in diff_c.iterrows()
                    for mn in mon_cols if row[mn]
                ]
                changed_c = pd.DataFrame(changes_c)

                sums = after_c.sum(axis=0)
                off = [m for m in mon_cols if abs(sums[m] - 100) > 5]
                if off:
                    st.caption(
                        "Сумма долей заметно отличается от 100% в месяцах: "
                        + ", ".join(off)
                        + ". При расчёте доли будут нормированы."
                    )

                a2, b2 = st.columns([3, 1])
                note_c = a2.text_input("Комментарий", key="curve_note")
                b2.metric("Изменено", len(changed_c))

                if st.button("Сохранить кривую", type="primary",
                             disabled=changed_c.empty):
                    try:
                        n = save_curve_override(changed_c, author, note_c)
                        st.cache_data.clear()
                        st.success(
                            f"Сохранено: {n}. В расчёт попадёт после того, как "
                            "правка будет подключена к прогнозу."
                        )
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Не сохранилось: {exc}")

# ------------------------------------------------------------------ план/факт
with tab_cmp:
    tab_intro(
        "Четыре ряда рядом: факт, старый план, два наших прогноза.",
        "Нужно, чтобы видеть, насколько новый расчёт отличается от того, "
        "как планировали раньше. Ряды покрывают разные периоды — "
        "сравнивать можно только на общих месяцах."
    )
    cmp_df = load_plan_compare(g)
    if cmp_df.empty:
        st.info("Сравнение недоступно.")
    else:
        cmp_df["month"] = pd.to_datetime(cmp_df["month"])
        piv = cmp_df.pivot_table(index="month", columns="series", values="units",
                                 aggfunc="sum")
        palette = {"Fact": FACT, "Our forecast": FCST,
                   "Legacy plan": MUTED, "Our forecast (growth)": "#6A9E5B"}
        names = {"Fact": "Факт", "Our forecast": "Прогноз (модель)",
                 "Legacy plan": "Старый план",
                 "Our forecast (growth)": "Прогноз (по росту)"}
        fig = go.Figure()
        for col_name in piv.columns:
            fig.add_trace(go.Scatter(
                x=piv.index, y=piv[col_name], mode="lines",
                name=names.get(col_name, col_name),
                line=dict(color=palette.get(col_name, INK), width=2,
                          dash="dot" if "Legacy" in col_name else "solid")))
        fig.update_layout(
            height=430, margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="white", paper_bgcolor="white", hovermode="x unified",
            font=dict(color=INK, size=13),
            legend=dict(orientation="h", y=1.08, x=0),
            xaxis=dict(showgrid=False, linecolor="#DDE3E8"),
            yaxis=dict(gridcolor="#EEF1F4", zeroline=False, title="единиц"),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Ряды покрывают разные периоды: старый план — с апреля 2026, "
            "прогноз по росту — с июня 2026, прогноз модели — с сентября 2026. "
            "Сравнивать их можно только на общих месяцах."
        )

# ------------------------------------------------------------------ расхождения
with tab_gap:
    tab_intro(
        "Где прогноз разошёлся со старым планом и почему.",
        "Сортировка по величине разницы — сверху самые большие деньги. "
        "Сравниваются только месяцы, покрытые обоими рядами. "
        "Внизу причины, которые система определила сама."
    )

    gap = load_gap(g)
    if gap.empty:
        st.info("Сравнение недоступно.")
    else:
        gap["delta"] = gap["ours"].fillna(0) - gap["legacy"].fillna(0)
        gap["delta_pct"] = gap["delta"] / gap["legacy"].replace(0, pd.NA) * 100
        gap = gap.reindex(gap["delta"].abs().sort_values(ascending=False).index)

        tot_legacy = gap["legacy"].sum()
        tot_ours = gap["ours"].sum()
        k1, k2, k3 = st.columns(3)
        k1.metric("Старый план", f"{tot_legacy:,.0f}".replace(",", " "))
        k2.metric("Наш прогноз", f"{tot_ours:,.0f}".replace(",", " "))
        k3.metric(
            "Разница",
            f"{tot_ours - tot_legacy:+,.0f}".replace(",", " "),
            f"{(tot_ours / tot_legacy - 1) * 100:+.1f}%" if tot_legacy else None,
        )

        show = gap.head(20).copy()
        fig = go.Figure(go.Bar(
            x=show["delta"], y=show["group_key"], orientation="h",
            marker_color=["#B5524A" if v < 0 else "#4A7C59" for v in show["delta"]],
            hovertemplate="%{y}: %{x:+,.0f}<extra></extra>"))
        fig.update_layout(
            height=max(260, 24 * len(show)), margin=dict(l=0, r=0, t=4, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            font=dict(color=INK, size=12),
            xaxis=dict(gridcolor="#EEF1F4", zeroline=True, zerolinecolor="#C9D2DA",
                       title="наш прогноз минус старый план, единиц"),
            yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Зелёное — планируем больше прежнего, красное — меньше.")

        # факта в этих месяцах ещё нет — сравниваются будущие периоды,
        # колонка была бы пустой во всех строках
        tbl = gap[["group_key", "legacy", "ours", "growth",
                   "delta", "delta_pct"]].copy()
        for col in ["legacy", "ours", "growth", "delta"]:
            tbl[col] = pd.to_numeric(tbl[col], errors="coerce").round(0)
        tbl["delta_pct"] = pd.to_numeric(tbl["delta_pct"],
                                         errors="coerce").round(1)
        tbl = tbl.rename(columns={
            "group_key": "Группа",
            "legacy": "Старый план", "ours": "Наш прогноз",
            "growth": "Прогноз по росту", "delta": "Разница",
            "delta_pct": "Разница, %",
        })
        st.dataframe(
            tbl, hide_index=True, use_container_width=True, height=420,
            column_config={
                "Старый план": st.column_config.NumberColumn(format="%.0f"),
                "Наш прогноз": st.column_config.NumberColumn(format="%.0f"),
                "Прогноз по росту": st.column_config.NumberColumn(format="%.0f"),
                "Разница": st.column_config.NumberColumn(format="%.0f"),
                "Разница, %": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

    st.markdown("**Из-за чего расходимся**")
    st.caption(
        "Причины по позициям, где разница больше 20%. Считается по парам "
        "ASIN и месяц, в колонке — число уникальных ASIN."
    )
    try:
        dr = load_drivers(g, c)
        if dr.empty:
            st.caption("Существенных расхождений нет.")
        else:
            st.dataframe(
                dr.rename(columns={
                    "category": "Категория", "driver_hint": "Причина",
                    "n_asins": "ASIN", "delta_total": "Суммарная разница",
                }),
                hide_index=True, use_container_width=True, height=380,
            )
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Причины недоступны: {exc}")

# ------------------------------------------------------------------ правки
with tab_hist:
    tab_intro(
        "Журнал ручных правок плана.",
        "Видно, кто и когда изменил цифру и с каким комментарием. "
        "Правки не перезаписываются — каждая ложится новой строкой."
    )
    hist_where = ""
    hist_params: list = []
    if grps:
        hist_where = "WHERE group_key IN UNNEST(@groups)"
        hist_params.append(
            bigquery.ArrayQueryParameter("groups", "STRING", list(grps)))

    hist = run(
        f"""
        SELECT group_key AS `Группа`, color AS `Цвет`, month AS `Месяц`,
               override_units AS `Ручной план`, uplift_pct AS `Аплифт`,
               note AS `Комментарий`, author AS `Автор`, updated_at AS `Когда`
        FROM `{TABLE_OVERRIDE}`
        {hist_where}
        ORDER BY updated_at DESC
        LIMIT 300
        """,
        hist_params,
    )
    if hist.empty:
        st.info("Ручных правок ещё нет. Первая появится здесь сразу после сохранения.")
    else:
        st.dataframe(hist, hide_index=True, use_container_width=True)
