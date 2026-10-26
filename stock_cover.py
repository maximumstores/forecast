"""Вкладка «Покрытие и старый сток»: на сколько недель хватит товара и сколько лежит давно.

Только США. Источники (BigQuery):
  mt.stock_history               — ежедневные снимки Hopted (source='hopted_us_native'):
                                   остаток, приход, отгрузки за 7/30 дней, возраст остатка
  forecast.summer_src            — соответствие ASIN → группа (country='US')
  forecast.group_forecast_long   — прогноз спроса по группам и месяцам с коридором 80%

Покрытие считается не по темпу последних 30 дней (он врёт в межсезонье), а прогоном остатка
по прогнозу спроса вперёд: сначала остаток текущего месяца, потом следующие месяцы.
  base — по прогнозу, high — по верхней границе коридора (спрос выше ожидаемого).

Ограничения данных (показываем в интерфейсе, не скрываем):
  * в снимке только корзины возраста 271–365 и 365+ дней, корзины 181–270 нет —
    это не «старше 6 месяцев», а старше 9 месяцев;
  * прогноз только по группам, не по ASIN, поэтому покрытие считается по группе целиком.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import numpy as np
import pandas as pd
import streamlit as st
from google.cloud import bigquery

PROJECT = "reorder-497714"
HIST = f"`{PROJECT}.mt.stock_history`"
SRC = f"`{PROJECT}.forecast.summer_src`"
FC = f"`{PROJECT}.forecast.group_forecast_long`"

SOURCE = "hopted_us_native"
MAX_WEEKS = 104          # дальше прогноз не смотрим
LOW_WEEKS = 12           # меньше — красный флаг: заказ в пути не успеет (морем ~12 недель)
CEILING_WEEKS = 39       # потолок покрытия из документа 12 окт: 9 месяцев
OLD_SHARE_FLAG = 0.05    # доля остатка старше 271 дня, с которой подсвечиваем группу


def _q(client, sql: str, params: list | None = None) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return client.query(sql, job_config=cfg).result().to_dataframe()


# ---------------------------------------------------------------------------
# Чистая функция покрытия — без BigQuery, чтобы её можно было проверить отдельно
# ---------------------------------------------------------------------------
def weeks_of_cover(stock: float, monthly: dict[date, float], today: date,
                   max_weeks: int = MAX_WEEKS) -> float:
    """Через сколько недель закончится остаток, если спрос идёт по помесячному прогнозу.

    monthly: {первое число месяца: прогноз штук за месяц}. Внутри месяца спрос равномерный.
    Нет прогноза на месяц, в котором остаток ещё есть → считаем только по известным месяцам
    и возвращаем max_weeks (честнее показать «больше горизонта», чем выдумать спрос).
    """
    if stock <= 0:
        return 0.0
    left = float(stock)
    day = today
    end = today + timedelta(weeks=max_weeks)
    while day < end:
        m0 = day.replace(day=1)
        dim = calendar.monthrange(day.year, day.month)[1]
        per_day = monthly.get(m0)
        if per_day is None:
            return float(max_weeks)
        per_day = per_day / dim
        days_left_in_month = dim - day.day + 1
        need = per_day * days_left_in_month
        if per_day <= 0:
            day += timedelta(days=days_left_in_month)
            continue
        if need >= left:
            return (day - today).days / 7 + left / per_day / 7
        left -= need
        day += timedelta(days=days_left_in_month)
    return float(max_weeks)


def demand_in_weeks(monthly: dict[date, float], today: date, weeks: int = 13) -> float:
    """Сколько штук по прогнозу уйдёт за ближайшие weeks недель (равномерно внутри месяца)."""
    total, day, end = 0.0, today, today + timedelta(weeks=weeks)
    while day < end:
        dim = calendar.monthrange(day.year, day.month)[1]
        per_day = monthly.get(day.replace(day=1), 0.0) or 0.0
        total += per_day / dim
        day += timedelta(days=1)
    return total


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def snapshot_dates(_client) -> list[date]:
    df = _q(_client, f"SELECT DISTINCT snapshot_date d FROM {HIST} WHERE source=@s "
                     "ORDER BY d DESC LIMIT 60",
            [bigquery.ScalarQueryParameter("s", "STRING", SOURCE)])
    return [pd.Timestamp(x).date() for x in df["d"]]


@st.cache_data(ttl=3600, show_spinner=False)
def load_stock(_client, snap: date) -> pd.DataFrame:
    """Остаток по ASIN из снимка Hopted US за дату snap, с группой из summer_src.

    Колонки снимка US (шапка в первой строке): 1 ASIN, 7 fulfillable, 8 inbound receiving,
    9 inbound shipped, 12 отгрузки за 7 дней, 13 за 30 дней, 15 возраст 271–365, 16 возраст 365+,
    19 всего на FBA. Строки с нечисловым ASIN (шапка, служебные формулы) отбрасываем.
    """
    sql = f"""
    WITH st AS (
      SELECT JSON_VALUE(row_json,'$.string_field_1') AS asin,
        SAFE_CAST(JSON_VALUE(row_json,'$.string_field_7')  AS FLOAT64) AS fulf,
        IFNULL(SAFE_CAST(JSON_VALUE(row_json,'$.string_field_8')  AS FLOAT64),0)
          + IFNULL(SAFE_CAST(JSON_VALUE(row_json,'$.string_field_9')  AS FLOAT64),0) AS inbound,
        SAFE_CAST(JSON_VALUE(row_json,'$.string_field_13') AS FLOAT64) AS u30,
        IFNULL(SAFE_CAST(JSON_VALUE(row_json,'$.string_field_15') AS FLOAT64),0) AS a271,
        IFNULL(SAFE_CAST(JSON_VALUE(row_json,'$.string_field_16') AS FLOAT64),0) AS a365,
        SAFE_CAST(JSON_VALUE(row_json,'$.string_field_19') AS FLOAT64) AS total
      FROM {HIST} WHERE source=@s AND snapshot_date=@d),
    st2 AS (
      SELECT asin, SUM(fulf) fulf, SUM(inbound) inbound, SUM(u30) u30,
             SUM(a271) a271, SUM(a365) a365, SUM(total) total
      FROM st WHERE REGEXP_CONTAINS(asin, r'^B0[0-9A-Z]{{8}}$') GROUP BY asin),
    g AS (SELECT asin, ANY_VALUE(grp) grp FROM {SRC} WHERE country='US' GROUP BY asin)
    SELECT st2.asin, g.grp, st2.fulf, st2.inbound, st2.u30, st2.a271, st2.a365, st2.total
    FROM st2 LEFT JOIN g USING (asin)"""
    df = _q(_client, sql, [bigquery.ScalarQueryParameter("s", "STRING", SOURCE),
                           bigquery.ScalarQueryParameter("d", "DATE", snap)])
    for c in ("fulf", "inbound", "u30", "a271", "a365", "total"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_forecast(_client) -> pd.DataFrame:
    df = _q(_client, f"SELECT group_key AS grp, month, units_forecast AS base, hi_80 AS high FROM {FC}")
    df["month"] = pd.to_datetime(df["month"]).dt.date
    return df


# ---------------------------------------------------------------------------
# Расчёт по группам
# ---------------------------------------------------------------------------
def build_table(stock: pd.DataFrame, fc: pd.DataFrame, today: date,
                with_inbound: bool) -> tuple[pd.DataFrame, dict]:
    """Таблица по группам + контрольные суммы."""
    mapped = stock[stock["grp"].notna()]
    check = {
        "asin_total": int(stock["asin"].nunique()),
        "asin_mapped": int(mapped["asin"].nunique()),
        "units_total": float(stock["fulf"].sum()),
        "units_mapped": float(mapped["fulf"].sum()),
    }
    agg = mapped.groupby("grp").agg(
        asins=("asin", "nunique"), fulf=("fulf", "sum"), inbound=("inbound", "sum"),
        u30=("u30", "sum"), a271=("a271", "sum"), a365=("a365", "sum")).reset_index()
    rows = []
    for r in agg.itertuples(index=False):
        sub = fc[fc["grp"] == r.grp]
        base = dict(zip(sub["month"], sub["base"]))
        high = dict(zip(sub["month"], sub["high"]))
        have = r.fulf + (r.inbound if with_inbound else 0.0)
        old = r.a271 + r.a365
        no_demand = bool(base) and demand_in_weeks(base, today) <= 0.5
        rows.append({
            "Группа": r.grp, "ASIN": r.asins, "Остаток, шт": r.fulf, "В пути, шт": r.inbound,
            "Отгрузка 30 дн.": r.u30,
            # без остатка и без спроса покрытие не определено; с остатком и без спроса — залежь (104+)
            "Нед. покрытия (прогноз)": (np.nan if (no_demand and have <= 0) or not base
                                        else weeks_of_cover(have, base, today)),
            "Нед. покрытия (верх коридора)": (np.nan if (no_demand and have <= 0) or not high
                                              else weeks_of_cover(have, high, today)),
            "Нет спроса": no_demand,
            "Старше 271 дн., шт": old,
            "Доля старого": old / r.fulf if r.fulf else np.nan,
        })
    out = pd.DataFrame(rows)

    def flag(row) -> str:
        w, share = row["Нед. покрытия (верх коридора)"], row["Доля старого"]
        marks = []
        if row["Нет спроса"] and row["Остаток, шт"] <= 0:
            return "нет спроса"
        if pd.notna(w) and w < LOW_WEEKS:
            marks.append("🔴 мало")
        if pd.notna(row["Нед. покрытия (прогноз)"]) and row["Нед. покрытия (прогноз)"] > CEILING_WEEKS:
            marks.append("🟠 больше 9 мес.")
        if pd.notna(share) and share > OLD_SHARE_FLAG:
            marks.append("🟠 старый сток")
        return ", ".join(marks) or "—"

    if not out.empty:
        out["Флаг"] = out.apply(flag, axis=1)
        out = out.sort_values("Остаток, шт", ascending=False).reset_index(drop=True)
    return out, check


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------
def render(client, log_event, tab_intro, num) -> None:
    tab_intro(
        "На сколько недель хватит товара и сколько лежит давно.",
        "Остаток из ежедневного снимка Hopted (США), спрос — прогноз по группам с коридором 80%. "
        "Покрытие — прогон остатка по прогнозу вперёд, а не деление на темп последних 30 дней: "
        "в межсезонье темп занижает или завышает срок. Данные о возрасте — только корзины 271+ дней.",
    )
    try:
        dates = snapshot_dates(client)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Не удалось прочитать снимки стока: {exc}")
        return
    if not dates:
        st.info("В истории стока нет снимков US.")
        return

    c1, c2 = st.columns([1, 1])
    snap = c1.selectbox("Снимок стока", dates, format_func=lambda d: d.strftime("%d.%m.%Y"))
    with_inbound = c2.checkbox(
        "Считать товар в пути (приёмка и отгружено)", value=True,
        help="Учитывается товар, который уже на складах Amazon в приёмке или отгружен. "
             "Заказы на фабрике и в море не учитываются: их нет в этих данных.")

    stock = load_stock(client, snap)
    if stock.empty:
        st.warning(f"В снимке за {snap:%d.%m.%Y} не нашлось ни одного ASIN с остатком. "
                   "Проверьте снимок в mt.stock_history (source = hopted_us_native) или выберите другую дату.")
        return
    fc = load_forecast(client)
    table, check = build_table(stock, fc, snap, with_inbound)
    if table.empty:
        st.info("Нет данных для таблицы.")
        return

    # Контроль: сколько остатка попало в группы
    cover_units = check["units_mapped"] / check["units_total"] if check["units_total"] else 0
    st.caption(
        f"Контроль: в группы попало {check['asin_mapped']} из {check['asin_total']} ASIN, "
        f"{cover_units:.1%} остатка ({num(check['units_mapped'])} из {num(check['units_total'])} шт). "
        "Остальное — ASIN без группы в таблицах Нины."
    )

    k1, k2, k3 = st.columns(3)
    k1.metric("Остаток, шт", num(table["Остаток, шт"].sum()))
    k2.metric("Групп с красным флагом", int(table["Флаг"].str.contains("🔴").sum()))
    old_total = table["Старше 271 дн., шт"].sum()
    k3.metric("Старше 271 дня, шт", num(old_total),
              f"{old_total / table['Остаток, шт'].sum():.1%} остатка")

    table = table.drop(columns=["Нет спроса"])
    view = table.copy()
    for c in ("Остаток, шт", "В пути, шт", "Отгрузка 30 дн.", "Старше 271 дн., шт"):
        view[c] = view[c].map(num)
    for c in ("Нед. покрытия (прогноз)", "Нед. покрытия (верх коридора)"):
        view[c] = view[c].map(lambda v: "—" if pd.isna(v) else (f"{v:.0f}+" if v >= MAX_WEEKS else f"{v:.0f}"))
    view["Доля старого"] = view["Доля старого"].map(lambda v: "—" if pd.isna(v) else f"{v:.1%}")
    st.dataframe(view, use_container_width=True, hide_index=True)

    st.download_button("Скачать CSV", table.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"coverage_us_{snap:%Y%m%d}.csv", mime="text/csv")
    with st.expander("Как читать"):
        st.markdown(
            f"- **Нед. покрытия (прогноз)** — через сколько недель кончится остаток при ожидаемом спросе. "
            f"Значение со знаком «+» значит, что остатка хватает дольше {MAX_WEEKS} недель.\n"
            f"- **Верх коридора** — то же при спросе на верхней границе 80%-коридора; "
            f"меньше {LOW_WEEKS} недель — 🔴.\n"
            f"- 🟠 больше 9 мес. — выше потолка покрытия из правила «не больше {CEILING_WEEKS} недель».\n"
            f"- Старый сток: корзин 181–270 дней в данных нет, поэтому это строго 271+ дней.\n"
            "- Прогноз по группам, не по ASIN: группа может быть в норме, а отдельный цвет или размер — на нуле."
        )
    log_event("coverage_view", str(snap))
