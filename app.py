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


def run(sql: str, params: list | None = None) -> pd.DataFrame:
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=params or []),
    )
    return job.result().to_dataframe()


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
        SELECT month,
               SUM(IF(kind='actual',   demand_units,   NULL)) AS fact,
               SUM(IF(kind='actual',   sales_units,    NULL)) AS sales,
               SUM(IF(kind='forecast', forecast_units, NULL)) AS forecast,
               SUM(IF(kind='forecast', lo_80,          NULL)) AS lo_80,
               SUM(IF(kind='forecast', hi_80,          NULL)) AS hi_80
        FROM `{DS}.looker_fact_monthly`
        WHERE {' AND '.join(where)}
        GROUP BY month
        ORDER BY month
        """,
        params,
    )


@st.cache_data(ttl=600)
def load_breakdown(dim: str, groups: tuple, categories: tuple) -> pd.DataFrame:
    where = ["kind = 'forecast'", f"{dim} IS NOT NULL"]
    params: list = []
    if groups:
        where.append("group_key IN UNNEST(@groups)")
        params.append(bigquery.ArrayQueryParameter("groups", "STRING", list(groups)))
    if categories:
        where.append("category IN UNNEST(@cats)")
        params.append(bigquery.ArrayQueryParameter("cats", "STRING", list(categories)))

    return run(
        f"""
        SELECT {dim} AS label, SUM(forecast_units) AS units
        FROM `{DS}.looker_fact_monthly`
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
        SELECT a.group_key, a.color, a.month,
               ROUND(a.plan_auto) AS plan_auto,
               o.override_units, o.author, o.updated_at
        FROM auto_plan a
        LEFT JOIN ovr o USING (group_key, color, month)
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
def load_on_track(level: str) -> pd.DataFrame:
    table = "looker_on_track_category" if level == "category" else "looker_on_track"
    key = "category" if level == "category" else "group_key"
    return run(
        f"""
        SELECT {key} AS name, on_hand, on_order, available,
               forecast_demand, sold_season_to_date,
               proj_leftover, leftover_ratio, cover_months, status, status_rank
        FROM `{DS}.{table}`
        ORDER BY status_rank, ABS(proj_leftover) DESC
        """
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
    schema = [
        bigquery.SchemaField("group_key", "STRING"),
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
    author = st.text_input("Ваш email", placeholder="name@maximumstores.online")

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

(tab_over, tab_track, tab_needs, tab_dims,
 tab_edit, tab_cmp, tab_hist) = st.tabs(
    ["Обзор", "Сезон", "Требует плана", "Разрезы",
     "Ввод плана", "План и факт", "Правки"]
)

# ------------------------------------------------------------------ обзор
with tab_over:
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
        k3.metric("Прогноз на 12 мес", f"{fcst_12:,.0f}".replace(",", " "))
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
        st.caption("Линия факта обрывается на последнем закрытом месяце. Дальше — модель.")

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
    level = st.radio("Уровень", ["Категории", "Группы"], horizontal=True,
                     label_visibility="collapsed")
    tr = load_on_track("category" if level == "Категории" else "group")

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

# ------------------------------------------------------------------ требует плана
with tab_needs:
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
    st.caption("Прогноз на весь горизонт, по измерениям")
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
            grid = df.pivot_table(index=["group_key", "color"], columns="месяц",
                                  values="план", aggfunc="sum").reset_index()

            st.caption("Правьте цифры прямо в таблице. Пусто — считается автоматически.")
            edited = st.data_editor(
                grid, hide_index=True, use_container_width=True, height=480,
                column_config={
                    "group_key": st.column_config.TextColumn("Группа", disabled=True,
                                                             width="medium"),
                    "color": st.column_config.TextColumn("Цвет", disabled=True,
                                                         width="medium"),
                },
                key="grid",
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
                if not author:
                    st.error("Укажите email слева — правки сохраняются с автором.")
                else:
                    try:
                        n = save_overrides(changed, author, note)
                        st.cache_data.clear()
                        st.success(f"Сохранено: {n}. В отчётах появится после пересборки.")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Не сохранилось: {exc}")

# ------------------------------------------------------------------ план/факт
with tab_cmp:
    cmp_df = load_plan_compare(g)
    if cmp_df.empty:
        st.info("Сравнение недоступно.")
    else:
        cmp_df["month"] = pd.to_datetime(cmp_df["month"])
        piv = cmp_df.pivot_table(index="month", columns="series", values="units",
                                 aggfunc="sum")
        palette = {"Fact": FACT, "Our forecast": FCST,
                   "Legacy plan": MUTED, "Our forecast (growth)": "#6A9E5B"}
        fig = go.Figure()
        for col_name in piv.columns:
            fig.add_trace(go.Scatter(
                x=piv.index, y=piv[col_name], mode="lines", name=col_name,
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
        st.caption("Legacy plan — старый план из таблиц, для сверки.")

# ------------------------------------------------------------------ правки
with tab_hist:
    hist = run(
        f"""
        SELECT group_key AS `Группа`, color AS `Цвет`, month AS `Месяц`,
               override_units AS `Ручной план`, uplift_pct AS `Аплифт`,
               note AS `Комментарий`, author AS `Автор`, updated_at AS `Когда`
        FROM `{TABLE_OVERRIDE}`
        ORDER BY updated_at DESC
        LIMIT 300
        """
    )
    if hist.empty:
        st.info("Ручных правок ещё нет. Первая появится здесь сразу после сохранения.")
    else:
        st.dataframe(hist, hide_index=True, use_container_width=True)
