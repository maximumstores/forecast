"""
Sales Planner — ввод плана продаж поверх BigQuery (проект reorder-497714).

Уровень ввода: group_key + color (657 комбинаций).
Запись: forecast.forecast_override с size = NULL — размеры раскладываются
существующей логикой share в plan_sales.

Запуск локально:
    pip install -r requirements.txt
    gcloud auth application-default login
    streamlit run app.py
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT = "reorder-497714"
DATASET = "forecast"
TABLE_OVERRIDE = f"{PROJECT}.{DATASET}.forecast_override"

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/drive.readonly",
]

st.set_page_config(page_title="Sales Planner", layout="wide")


# ----------------------------------------------------------------- клиент
@st.cache_resource
def get_client() -> bigquery.Client:
    if "gcp_service_account" in st.secrets:
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=SCOPES
        )
        return bigquery.Client(credentials=creds, project=PROJECT, location="EU")
    return bigquery.Client(project=PROJECT, location="EU")


client = get_client()


# ----------------------------------------------------------------- данные
@st.cache_data(ttl=300)
def load_plan(months_ahead: int) -> pd.DataFrame:
    """Авто-план из plan_sales + последний ручной оверрайд, на уровне group+color."""
    sql = f"""
    WITH auto_plan AS (
      SELECT group_key,
             IFNULL(color, '') AS color,
             month,
             SUM(plan_units) AS plan_auto
      FROM `{PROJECT}.{DATASET}.plan_sales`
      WHERE month >= DATE_TRUNC(CURRENT_DATE(), MONTH)
        AND month <  DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL @ahead MONTH)
      GROUP BY 1, 2, 3
    ),
    ovr AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT group_key,
               IFNULL(color, '') AS color,
               month,
               override_units,
               author,
               updated_at,
               ROW_NUMBER() OVER (
                 PARTITION BY group_key, IFNULL(color, ''), month
                 ORDER BY updated_at DESC
               ) AS rn
        FROM `{TABLE_OVERRIDE}`
        WHERE size IS NULL
      ) WHERE rn = 1
    )
    SELECT a.group_key,
           a.color,
           a.month,
           ROUND(a.plan_auto) AS plan_auto,
           o.override_units,
           o.author,
           o.updated_at
    FROM auto_plan a
    LEFT JOIN ovr o USING (group_key, color, month)
    ORDER BY a.group_key, a.color, a.month
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("ahead", "INT64", months_ahead)
            ]
        ),
    )
    return job.result().to_dataframe()


@st.cache_data(ttl=300)
def load_fact() -> pd.DataFrame:
    """Факт продаж по месяцам для графика."""
    sql = f"""
    SELECT group_key,
           IFNULL(color, '') AS color,
           month,
           SUM(sales_units) AS fact_units
    FROM `{PROJECT}.{DATASET}.looker_fact_monthly`
    WHERE kind = 'actual'
      AND month >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 18 MONTH)
    GROUP BY 1, 2, 3
    """
    return client.query(sql).result().to_dataframe()


# ----------------------------------------------------------------- запись
def save_overrides(rows: pd.DataFrame, author: str, note: str) -> int:
    """Append-only запись в forecast_override, size = NULL."""
    if rows.empty:
        return 0

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

    job = client.load_table_from_dataframe(
        payload,
        TABLE_OVERRIDE,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=[
                bigquery.SchemaField("group_key", "STRING"),
                bigquery.SchemaField("color", "STRING"),
                bigquery.SchemaField("size", "STRING"),
                bigquery.SchemaField("month", "DATE"),
                bigquery.SchemaField("override_units", "FLOAT"),
                bigquery.SchemaField("uplift_pct", "FLOAT"),
                bigquery.SchemaField("note", "STRING"),
                bigquery.SchemaField("author", "STRING"),
                bigquery.SchemaField("updated_at", "TIMESTAMP"),
            ],
        ),
    )
    job.result()
    return len(payload)


# ----------------------------------------------------------------- UI
st.title("Sales Planner")
st.caption(
    "План на уровне группа + цвет. Пусто = используется авто-план. "
    "Размеры раскладываются автоматически."
)

with st.sidebar:
    st.header("Настройки")
    months_ahead = st.slider("Горизонт, месяцев", 3, 18, 12)
    author = st.text_input("Ваш email", placeholder="name@maximumstores.online")
    st.divider()
    st.caption(
        "Правки пишутся в forecast_override и переживают пересборку gold. "
        "Чтобы они попали в дашборд, нужен рефреш."
    )

try:
    df = load_plan(months_ahead)
except Exception as exc:  # noqa: BLE001
    st.error(f"Не удалось прочитать данные: {exc}")
    st.stop()

if df.empty:
    st.warning("plan_sales не вернул строк на этот горизонт.")
    st.stop()

groups = sorted(df["group_key"].unique())
sel_groups = st.multiselect("Группы", groups, default=groups[:5])
view = df[df["group_key"].isin(sel_groups)].copy()

if view.empty:
    st.info("Выберите хотя бы одну группу.")
    st.stop()

view["месяц"] = pd.to_datetime(view["month"]).dt.strftime("%Y-%m")
view["план"] = view["override_units"].fillna(view["plan_auto"])

tab_edit, tab_chart, tab_hist = st.tabs(["Ввод плана", "График", "История правок"])

with tab_edit:
    grid = view.pivot_table(
        index=["group_key", "color"],
        columns="месяц",
        values="план",
        aggfunc="sum",
    ).reset_index()

    edited = st.data_editor(
        grid,
        hide_index=True,
        use_container_width=True,
        height=560,
        column_config={
            "group_key": st.column_config.TextColumn("Группа", disabled=True),
            "color": st.column_config.TextColumn("Цвет", disabled=True),
        },
        key="grid",
    )

    # что изменилось
    month_cols = [c for c in grid.columns if c not in ("group_key", "color")]
    before = grid.set_index(["group_key", "color"])[month_cols]
    after = edited.set_index(["group_key", "color"])[month_cols]
    diff = (after != before) & after.notna()

    changes = []
    for (gk, color), row in diff.iterrows():
        for m in month_cols:
            if row[m]:
                changes.append(
                    {
                        "group_key": gk,
                        "color": color,
                        "month": f"{m}-01",
                        "override_units": after.loc[(gk, color), m],
                    }
                )
    changed = pd.DataFrame(changes)

    col_a, col_b = st.columns([3, 1])
    with col_a:
        note = st.text_input("Комментарий к правке")
    with col_b:
        st.metric("Изменено ячеек", len(changed))

    if st.button("Сохранить в BigQuery", type="primary", disabled=changed.empty):
        if not author:
            st.error("Укажите email в боковой панели.")
        else:
            try:
                n = save_overrides(changed, author, note)
                st.cache_data.clear()
                st.success(f"Записано строк: {n}")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ошибка записи: {exc}")

with tab_chart:
    try:
        fact = load_fact()
        fact = fact[fact["group_key"].isin(sel_groups)]
        chart = (
            pd.concat(
                [
                    fact.groupby("month")["fact_units"].sum().rename("Факт"),
                    view.groupby("month")["plan_auto"].sum().rename("Авто-план"),
                    view.groupby("month")["план"].sum().rename("План (итог)"),
                ],
                axis=1,
            )
            .sort_index()
        )
        st.line_chart(chart)
        st.caption("Факт обрывается на последнем закрытом месяце — это ожидаемо.")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"График недоступен: {exc}")

with tab_hist:
    hist = view[view["override_units"].notna()][
        ["group_key", "color", "месяц", "plan_auto", "override_units", "author", "updated_at"]
    ].rename(
        columns={
            "group_key": "Группа",
            "color": "Цвет",
            "plan_auto": "Авто",
            "override_units": "Ручной",
            "author": "Автор",
            "updated_at": "Когда",
        }
    )
    if hist.empty:
        st.info("Ручных правок пока нет.")
    else:
        st.dataframe(hist, hide_index=True, use_container_width=True)
