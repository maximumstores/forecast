"""Вкладка «Точность плана»: насколько точно угадывали прошлые сезоны план Нины
и расчёты планера. Метрики — как у Zara / Columbia / Uniqlo:
WAPE (средняя ошибка по группам), bias (перекос вверх/вниз), FVA (помогла ли ручная правка),
OOS % (потери на аутах, отдельно в пик), sell-through.

Источники (BigQuery):
  forecast.nina_plan_hist      — архив помесячных планов Нины (закреплённые значения), US
  forecast.group_sales_monthly — факт по группам: продажи, спрос без аутов, дни без товара
  forecast.summer_review       — правки и причины правок (журнал копится с лета '27)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st
from google.cloud import bigquery

PROJECT = "reorder-497714"
HIST = f"`{PROJECT}.forecast.nina_plan_hist`"
SALES = f"`{PROJECT}.forecast.group_sales_monthly`"
REVIEW = f"`{PROJECT}.forecast.summer_review`"

MONTHS_RU = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
METHODS = {
    "nina": "План Нины",
    "calc": "Расчёт: прошлый год × рост",
    "naive": "Прошлый год как есть",
}


def _q(client, sql: str, params: list | None = None) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return client.query(sql, job_config=cfg).result().to_dataframe()


@st.cache_data(ttl=3600, show_spinner=False)
def last_fact_month(_client) -> pd.Timestamp:
    return pd.Timestamp(_q(_client, f"SELECT MAX(month) m FROM {SALES}")["m"].iloc[0])


@st.cache_data(ttl=3600, show_spinner=False)
def plan_years(_client) -> list[int]:
    df = _q(_client, f"SELECT DISTINCT EXTRACT(YEAR FROM month) y FROM {HIST} WHERE kind='month_plan' ORDER BY y")
    return [int(x) for x in df["y"]]


@st.cache_data(ttl=3600, show_spinner=False)
def load_season(_client, y: int, m_end: int) -> pd.DataFrame:
    """По группам за апрель…m_end года y: факт, спрос без аутов и три прогноза.
    Расчёт «прошлый год × рост»: продажи того же окна год назад × рост года y-1 к y-2,
    рост в рамках −20%…+60% (то, что было известно в январе года y)."""
    sql = f"""
    WITH np AS (
      SELECT grp AS group_key, SUM(units) AS nina FROM {HIST}
      WHERE kind='month_plan' AND EXTRACT(YEAR FROM month)=@y
        AND EXTRACT(MONTH FROM month) BETWEEN 4 AND @m GROUP BY 1),
    b AS (
      SELECT group_key,
        SUM(IF(EXTRACT(YEAR FROM month)=@y   AND EXTRACT(MONTH FROM month) BETWEEN 4 AND @m, sales, 0))  AS fact,
        SUM(IF(EXTRACT(YEAR FROM month)=@y   AND EXTRACT(MONTH FROM month) BETWEEN 4 AND @m, demand, 0)) AS dem,
        SUM(IF(EXTRACT(YEAR FROM month)=@y-1 AND EXTRACT(MONTH FROM month) BETWEEN 4 AND @m, sales, 0))  AS ly,
        SUM(IF(EXTRACT(YEAR FROM month)=@y-1, sales, 0)) AS yr1,
        SUM(IF(EXTRACT(YEAR FROM month)=@y-2, sales, 0)) AS yr0
      FROM {SALES} GROUP BY 1)
    SELECT b.group_key AS grp, b.fact, b.dem, np.nina, b.ly AS naive,
           IF(b.yr0 > 0, b.ly * LEAST(GREATEST(b.yr1 / b.yr0, 0.8), 1.6), NULL) AS calc
    FROM b JOIN np USING (group_key)
    WHERE b.fact > 0 OR np.nina > 0"""
    df = _q(_client, sql, [bigquery.ScalarQueryParameter("y", "INT64", y),
                           bigquery.ScalarQueryParameter("m", "INT64", m_end)])
    for c in ("fact", "dem", "nina", "naive", "calc"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_reasons(_client) -> pd.DataFrame:
    try:
        return _q(_client, f"SELECT country, grp, color, new_plan27, reason, evidence, ts FROM {REVIEW} "
                           "WHERE reason IS NOT NULL ORDER BY ts DESC")
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


PEAK = (5, 8)   # пик сезона: май–август


@st.cache_data(ttl=3600, show_spinner=False)
def load_oos_months(_client, y: int) -> pd.DataFrame:
    df = _q(_client, f"""
        SELECT EXTRACT(MONTH FROM month) AS m, SUM(sales) AS fact, SUM(demand) AS dem
        FROM {SALES} WHERE EXTRACT(YEAR FROM month)=@y GROUP BY 1 ORDER BY 1""",
            [bigquery.ScalarQueryParameter("y", "INT64", y)])
    for c in ("fact", "dem"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def oos_by_month(df: pd.DataFrame) -> pd.DataFrame:
    """OOS % по месяцам (1 − продажи/спрос без аутов) + признак пика."""
    o = df.copy()
    o["oos"] = np.where(o["dem"] > 0, 1 - o["fact"] / o["dem"], np.nan)
    o["lost"] = (o["dem"] - o["fact"]).clip(lower=0)
    o["peak"] = o["m"].between(*PEAK)
    return o


@st.cache_data(ttl=3600, show_spinner=False)
def load_sellthrough(_client, y: int, m_end: int) -> pd.DataFrame:
    """Продано за май…m_end года y (США) и остаток на Amazon сейчас — по группам и цветам."""
    df = _q(_client, f"""
        WITH sold AS (
          SELECT grp, IFNULL(NULLIF(color,''),'—') AS color, asin, SUM(units) AS sold
          FROM `{PROJECT}.forecast.summer_src`
          WHERE country='US' AND kind='fact' AND EXTRACT(YEAR FROM month)=@y
            AND EXTRACT(MONTH FROM month) BETWEEN 5 AND @m GROUP BY 1,2,3)
        SELECT s.grp, s.color, SUM(s.sold) AS sold,
               SUM(IFNULL(c.stock_us,0) + IFNULL(c.stock_in_transit_us,0)) AS onhand
        FROM sold s LEFT JOIN `{PROJECT}.forecast.stock_current` c USING (asin) GROUP BY 1,2""",
            [bigquery.ScalarQueryParameter("y", "INT64", y), bigquery.ScalarQueryParameter("m", "INT64", m_end)])
    for c in ("sold", "onhand"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def sell_through(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Sell-through = продано / (продано + остаток сейчас). Приближение: стока на начало сезона в истории нет."""
    g = df.groupby(by, as_index=False)[["sold", "onhand"]].sum()
    tot = g["sold"] + g["onhand"]
    g["st"] = np.where(tot > 0, g["sold"] / tot, np.nan)
    return g.sort_values("onhand", ascending=False)


def wape(pred: pd.Series, act: pd.Series) -> float:
    m = pred.notna()
    a = act[m].sum()
    return float((pred[m] - act[m]).abs().sum() / a) if a else np.nan


def bias(pred: pd.Series, act: pd.Series) -> float:
    m = pred.notna()
    a = act[m].sum()
    return float(pred[m].sum() / a - 1) if a else np.nan


def _pct(v, signed=False) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.0%}"


def render(client, log_event, tab_intro, num) -> None:
    tab_intro(
        "Насколько точно угадывали прошлые сезоны — план Нины и расчёты планера.",
        "Метрики, по которым планирование оценивают Zara, Columbia, Uniqlo: "
        "ошибка (WAPE), перекос (bias), польза ручных правок (FVA), потери на аутах (OOS %). "
        "План Нины — закреплённые помесячные значения из её архива «Plan Sales 2025 US». Только США.",
    )
    try:
        years = plan_years(client)
        last = last_fact_month(client)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Архив планов ещё не загружен — нажмите «Обновить из таблиц Нины» во вкладке «Лето '27». ({exc})")
        return
    years = [y for y in years if pd.Timestamp(y, 4, 1) <= last]
    if not years:
        st.info("Нет сезона, по которому уже есть и план, и факт.")
        return

    c1, c2 = st.columns([2, 3])
    y = c1.radio("Сезон", years, index=len(years) - 1, horizontal=True, key="acc_year",
                 format_func=lambda v: f"Лето '{str(v)[2:]}")
    m_end = 9 if last >= pd.Timestamp(y, 9, 1) else int(last.month) if last.year == y else 9
    basis = c2.radio("Сравнивать с", ["Продажи", "Спрос без аутов"], horizontal=True, key="acc_basis",
                     help="Продажи — сколько реально продали. Спрос без аутов — сколько продали бы, "
                          "если бы товар был в наличии (оценка планера). Если ауты были большие, "
                          "честнее сравнивать со спросом.")
    st.caption(f"Окно: {MONTHS_RU[4]}–{MONTHS_RU[m_end]} {y}. "
               + ("Сентябрь ещё не загружен." if m_end < 9 else ""))

    df = load_season(client, y, m_end)
    act = df["dem"] if basis.startswith("Спрос") else df["fact"]
    has_calc = df["calc"].notna().any()
    base_key = "calc" if has_calc else "naive"

    # ---------------- карточки
    w_n, w_b = wape(df["nina"], act), wape(df[base_key], act)
    oos = 1 - df["fact"].sum() / df["dem"].sum() if df["dem"].sum() else np.nan
    k = st.columns(4)
    k[0].metric("Ошибка плана Нины (WAPE)", _pct(w_n),
                f"{METHODS[base_key].split(':')[0].lower()}: {_pct(w_b)}", delta_color="off",
                help="Сумма |план − факт| по группам / факт. Чем меньше, тем точнее.")
    k[1].metric("Перекос плана (bias)", _pct(bias(df["nina"], act), True),
                "выше факта" if bias(df["nina"], act) > 0 else "ниже факта", delta_color="off",
                help="(план − факт) / факт по сумме. Плюс каждый год = систематически завышаем.")
    fva = (w_b - w_n) * 100 if not np.isnan(w_b) else np.nan
    k[2].metric("Польза правок (FVA)", "—" if np.isnan(fva) else f"{fva:+.0f} п.п.",
                "правки улучшили" if fva > 0 else "правки ухудшили", delta_color="normal" if fva > 0 else "inverse",
                help=f"Ошибка «{METHODS[base_key]}» минус ошибка плана Нины. "
                     "Плюс — ручной план точнее расчёта, минус — расчёт был бы точнее.")
    k[3].metric("Потери на аутах (OOS %)", _pct(oos),
                help="1 − продажи / спрос без аутов: сколько продаж не случилось из-за нехватки товара.")

    # ---------------- OOS по месяцам, отдельно в пик
    try:
        om = oos_by_month(load_oos_months(client, y))
    except Exception:  # noqa: BLE001
        om = pd.DataFrame()
    if not om.empty:
        pk = om[om["peak"]]
        o_pk = 1 - pk["fact"].sum() / pk["dem"].sum() if pk["dem"].sum() else np.nan
        o_rest = om[~om["peak"]]
        o_rs = 1 - o_rest["fact"].sum() / o_rest["dem"].sum() if o_rest["dem"].sum() else np.nan
        st.markdown("#### Потери на аутах: пик и остальное")
        a, b = st.columns(2)
        a.metric(f"OOS в пик ({MONTHS_RU[PEAK[0]]}–{MONTHS_RU[PEAK[1]]})", _pct(o_pk),
                 f"потеряно ≈ {pk['lost'].sum():,.0f} шт".replace(",", " "), delta_color="off")
        b.metric("OOS в остальные месяцы", _pct(o_rs), delta_color="off")
        st.dataframe(pd.DataFrame({"Месяц": [MONTHS_RU[int(v)] for v in om["m"]], "Пик": om["peak"].map({True: "да", False: ""}),
                                   "Продажи": om["fact"].round(0), "Спрос без аутов": om["dem"].round(0),
                                   "Потеряно, шт": om["lost"].round(0), "OOS %": om["oos"]}),
                     hide_index=True, width="stretch",
                     column_config={"OOS %": st.column_config.NumberColumn(format="percent")})

    # ---------------- сравнение методов
    rows = []
    for key, label in METHODS.items():
        if df[key].notna().any():
            rows.append({"Метод": label, "Прогноз": df[key].sum(), "Факт": act[df[key].notna()].sum(),
                         "Перекос": bias(df[key], act), "Ошибка (WAPE)": wape(df[key], act)})
    st.markdown("#### Кто точнее угадал")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Прогноз": st.column_config.NumberColumn(format="localized"),
        "Факт": st.column_config.NumberColumn(format="localized"),
        "Перекос": st.column_config.NumberColumn(format="percent"),
        "Ошибка (WAPE)": st.column_config.NumberColumn(format="percent"),
    })
    if not has_calc:
        st.caption(f"«Прошлый год × рост» для лета '{str(y)[2:]} посчитать нельзя: "
                   "нужен полный год до прошлого, а история продаж начинается с января 2024.")

    # ---------------- по группам
    st.markdown("#### По группам — где ошиблись сильнее всего")
    g = df.assign(act=act)
    g["Ошибка плана, шт"] = (g["nina"] - g["act"])
    g["Ошибка плана"] = np.where(g["act"] > 0, g["nina"] / g["act"] - 1, np.nan)
    g["Ошибка расчёта"] = np.where(g["act"] > 0, g[base_key] / g["act"] - 1, np.nan)
    g["Кто точнее"] = np.where((g["nina"] - g["act"]).abs() <= (g[base_key] - g["act"]).abs(),
                               "план Нины", "расчёт")
    g.loc[g[base_key].isna(), "Кто точнее"] = ""
    g["OOS %"] = np.where(g["dem"] > 0, 1 - g["fact"] / g["dem"], np.nan)
    g = g.reindex(g["Ошибка плана, шт"].abs().sort_values(ascending=False).index)
    show = pd.DataFrame({
        "Группа": g["grp"], "Факт": g["act"], "План Нины": g["nina"],
        METHODS[base_key].split(":")[0]: g[base_key],
        "Ошибка плана": g["Ошибка плана"], "Ошибка расчёта": g["Ошибка расчёта"],
        "Кто точнее": g["Кто точнее"], "OOS %": g["OOS %"],
    })
    for c in ("Факт", "План Нины", METHODS[base_key].split(":")[0]):
        show[c] = show[c].round(0).astype("Int64")
    pc = st.column_config.NumberColumn(format="percent")
    st.dataframe(show, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(show)),
                 column_config={"Ошибка плана": pc, "Ошибка расчёта": pc, "OOS %": pc,
                                "Факт": st.column_config.NumberColumn(format="localized"),
                                "План Нины": st.column_config.NumberColumn(format="localized")})
    n_n = int((g["Кто точнее"] == "план Нины").sum())
    n_c = int((g["Кто точнее"] == "расчёт").sum())
    if n_n + n_c:
        st.caption(f"План Нины точнее в {n_n} группах, расчёт — в {n_c}.")
    st.download_button("Скачать (CSV)", show.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"accuracy_{y}.csv", mime="text/csv", key="acc_csv")

    # ---------------- sell-through
    st.markdown("#### Sell-through: какую часть продали")
    if y == pd.Timestamp.today().year:
        try:
            stdf = load_sellthrough(client, y, m_end)
        except Exception as exc:  # noqa: BLE001
            stdf = pd.DataFrame()
            st.info(f"Sell-through не посчитался: {exc}")
        if not stdf.empty:
            lvl = st.radio("Уровень", ["Группа", "Группа и цвет"], horizontal=True, key="acc_st_lvl")
            S = sell_through(stdf, ["grp"] if lvl == "Группа" else ["grp", "color"])
            S = S.rename(columns={"grp": "Группа", "color": "Цвет", "sold": "Продано",
                                  "onhand": "Остаток сейчас", "st": "Sell-through"})
            S["Продано"] = S["Продано"].round(0)
            S["Остаток сейчас"] = S["Остаток сейчас"].round(0)
            st.dataframe(S, hide_index=True, width="stretch", height=min(480, 38 + 35 * len(S)),
                         column_config={"Sell-through": st.column_config.NumberColumn(format="percent")})
            st.caption("Sell-through = продано / (продано + остаток сейчас, включая товар в пути). "
                       "Приближение: остатка на начало сезона в истории нет. У зимних групп процент низкий — на складе товар под зиму. Смотрите летние группы. Только США.")
    else:
        st.info("Sell-through считается для текущего сезона: нужен остаток сейчас, истории стока по месяцам нет.")

    # ---------------- причины правок
    st.markdown("#### Какие причины правок работают")
    rs = load_reasons(client)
    if rs.empty:
        st.info("Журнал причин правок начинается с лета '27: при правке во вкладке «Лето '27» "
                "указывается причина. После сезона здесь появится, какие причины улучшали прогноз, а какие нет.")
    else:
        st.dataframe(rs.assign(evidence=rs["evidence"].fillna("—"))
                     .groupby(["reason", "evidence"]).size().rename("Правок").reset_index()
                     .rename(columns={"reason": "Причина", "evidence": "Уровень"}), hide_index=True)
        st.caption("Точность по причинам посчитаем, когда по летним правкам '27 придёт факт.")
    log_event("accuracy", f"{y} {basis}")
