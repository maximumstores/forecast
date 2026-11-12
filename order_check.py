"""Проверка заказа: контракт фабрики (xlsx) против плана Нины, модели и заказа.

Логика: плановый сток на май + заказ = продажи май–сентябрь. Контракт сравниваем с «нужным заказом»
по плану Нины и по модели (коридор 0.75–1.25 от модели). Страны: в контракте разбивки нет, поэтому
делим по доле заказа Нины в каждой стране (оценка).
"""
from __future__ import annotations

import io
import re

import numpy as np
import pandas as pd
import streamlit as st

import plan_check as pc
import summer27 as s27

MIN_DIFF = 300       # меньше этой разницы (шт) вердикт не выносим
NINA_HI, NINA_LO = 1.5, 0.67   # без доверия к модели: контракт vs заказ Нины
WINTER = re.compile(r"(250|260|320|230|Beanie|Balaclava|Kids)")

# Код контракта → группа планера. Новые коды назначаются на вкладке вручную (в рамках сессии).
CODEMAP = {
"G-2P-LW":"Gaiter 150","G-LW":"Gaiter 150","M-LS-BU-LW":"Mens LS Button-Up 165","M-LS-LW":"Mens LS 165",
"M-LS-TH-HN-LW":"Mens LS 165 Thailand Henly","M-LS-ZP-LW":"Mens LS Zip 165","M-PL-ON-LW":"Mens LS Polo Open Neckline 165",
"M-PL-PL-LW":"Mens LS Polo 165 (Polo)","M-PL-ZP-LW":"Mens LS Polo Zip 165","M-PT-LW":"Mens Pants 165",
"M-SS-BU-LW":"Mens SS Button-Up 165","M-ST-LW":"Mens Sets 165","W-LS-LW":"W LS 165","W-LS-ZP-LW":"W LS Zip 165",
"W-PT-LW":"W Pants 165","W-ST-LW":"W Sets 165","M-SS-2P-LW":"Mens SS 180","M-SS-3P-LW":"Mens SS 180","M-SS-LW":"Mens SS 180",
"M-SS-SX-LW":"Mens SS+Sox 180","M-SS-TH-HN-LW":"Mens SS 180 Thailand Henly","M-SS-TH-LW":"Mens SS 180 Thailand",
"M-SS-TH-VN-LW":"Mens SS 180 Thailand V-neck","M-TS-S1-LW":"Mens Tank Top 180","M-TS-S2-LW":"Mens Tank Top 180",
"PL-LW":"Polo 180","PL-ON-LW":"Polo Open Neckline 180","PL-ZP-LW":"Polo Zip 180","W-SS-2P-LW":"W SS 180","W-SS-LW":"W SS 180",
"W-SS-SN-LW":"W SS scoopneck 180","W-SS-SX-LW":"W SS+Sox 180","W-SS-SX-VN-LW":"W SS+Sox 180 V-neck","W-SS-VN-LW":"W SS 180 V-neck",
"M-HD-CM-LW":"Mens Hoodie 190","M-HD-DRY-LW":"Mens Hoodie 190","M-HD-LW":"Mens Hoodie 190","M-LS-TH-LW":"Mens LS 190 Thailand",
"M-LS-TH-VN-LW":"Mens LS 190 Thailand V-neck","M-PT-34-LW":"Mens Pants 200 3/4 Design","M-PT-ND2-LW":"Mens Pants 200 New Design",
"M-ST-G-LW":"Mens Sets 200 + gaiter","M-ST-GBL-LW":"Mens Sets 200 + gaiter","M-UW-2P-LW":"Underwear Mens 190","M-UW-LW":"Underwear Mens 190",
"W-HD-LW":"W Hoodie 190","W-LS-SN-TH-LW":"W LS scoopneck 190","W-LS-TH-LW":"W LS 190 Thailand","W-LS-TH-VN-LW":"W LS 190 Thailand V-neck",
"W-PT-34-LW":"W Pants 200 3/4 Design","W-PT-ND2-LW":"W Pants 200 New Design","W-ST-G-LW":"W Sets 200 + gaiter","W-ST-GBL-LW":"W Sets 200 + gaiter",
"W-TS-S1-LW":"W Tank Top 190","W-TS-S2-LW":"W Tank Top 190","W-TS-S3-LW":"W Tank Top 190",
"A-B-MW":"Adult Beanie 230","A-BG-MW":"Adult Beanie+Gaiter 230","A-BSX-MW":"Adult Beanie+Sox 230","BL-MW":"Balaclava 230",
"G-2L-MW":"Gaiter 230 2layers","G-MW":"Gaiter 230","K-B-MW":"Kids Beanie 230","K-LS-MW":"Kids LS 230","K-PT-MW":"Kids Pants 230",
"K-ST-B-MW":"Kids Sets + Beanie 230","K-LS-TNL-MW":"Kids LS turtlneck low 250","K-LS-ZP-MW":"Kids LS Zip 250",
"M-LS-DRY-MW":"Mens LS 250","M-LS-MW":"Mens LS 250","M-LS-TNH-MW":"Mens LS turtlneck hight 260","M-LS-TNL-MW":"Mens LS turtlneck low 260",
"M-LS-ZP-MW":"Mens LS Zip 250","M-PT-MW":"Mens Pants 250","M-ST-B-MW":"Mens Sets + Beanie 250","M-ST-MW":"Mens Sets 250",
"M-ST-TNH-MW":"Mens Sets 250 turtlneck hight","M-ST-TNL-MW":"Mens Sets 250 turtlneck low","M-ST-ZP-MW":"Mens Sets Zip 250",
"W-LS-MW":"W LS 250","W-LS-ND-MW":"W LS 250","W-LS-TNH-MW":"W LS turtlneck hight 260","W-LS-TNL-MW":"W LS turtlneck low 260",
"W-LS-ZP-MW":"W LS Zip 250","W-LS-ZP-ND-MW":"W LS Zip 250","W-PT-MW":"W Pants 250","W-PT-ND-MW":"W Pants 250",
"W-PT-SW-SX-MW":"W Pants Smartwool design 250 + Sox","W-ST-B-MW":"W Sets + Beanie 250","W-ST-BG-MW":"W Sets 250 + Beanie + Gaiter",
"W-ST-MW":"W Sets 250","W-ST-ND-MW":"W Sets 250","W-ST-TNH-MW":"W Sets 250 turtlneck hight","W-ST-TNL-MW":"W Sets 250 turtlneck low",
"W-ST-ZP-MW":"W Sets Zip 250","W-ST-ZP-ND-MW":"W Sets Zip 250",
"M-LS-HW":"Mens LS 320","M-LS-ZP-HW":"Mens LS Zip 320","M-PT-HW":"Mens Pants 320","M-ST-BL-HW":"Mens Sets 320 + Balaclava",
"M-ST-HW":"Mens Sets 320","M-ST-ZP-HW":"Mens Sets Zip 320","W-LS-HW":"W LS 320","W-LS-ZP-HW":"W LS Zip 320","W-PT-HW":"W Pants 320","W-ST-HW":"W Sets 320",
}


# ------------------------------------------------------------------ парсер контракта
def parse_contract(data: bytes) -> pd.DataFrame:
    """Строки контракта: code, cn_color, our_color, qty. Читает лист с шапкой «CODE category»;
    код и категория заполняются вниз, строки-итоги (без цвета) пропускаются."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    rows: list[dict] = []
    for ws in wb.worksheets:
        head = None
        code = None
        for i, r in enumerate(ws.iter_rows(values_only=True), 1):
            first = str(r[0]).strip() if r and r[0] is not None else ""
            if head is None:
                if first.startswith("CODE") and "category" in first:
                    head = [str(c or "").strip() for c in r]
                continue
            if first:
                code = first
            cn = r[2] if len(r) > 2 else None
            tot_i = next((j for j, h in enumerate(head) if h.startswith("Total")), None)
            if tot_i is None or not cn or code is None:
                continue
            try:
                q = float(r[tot_i] or 0)
            except (TypeError, ValueError):
                continue
            if q > 0:
                rows.append(dict(code=code, cn_color=str(cn).strip(), our_color=str(r[3] or "").strip(), qty=q))
        if rows:
            break   # нужен только первый лист с данными (старые листы «old …» игнорируем)
    return pd.DataFrame(rows, columns=["code", "cn_color", "our_color", "qty"])


def map_groups(lines: pd.DataFrame, extra: dict[str, str] | None = None) -> pd.DataFrame:
    cm = {**CODEMAP, **(extra or {})}
    out = lines.copy()
    out["grp"] = out["code"].map(cm)
    return out


# ------------------------------------------------------------------ вердикты (чистые функции)
def norm_color(s: str) -> str:
    return re.sub(r"[\s　]+", "", str(s)).lower()


def verdict_group(C: float, nina_order: float, calc27: float, stock: float, trusted: bool) -> tuple[str, float]:
    """(вердикт, избыток/нехватка). Модель в доверии: коридор 0.75–1.25 от спроса по модели минус сток."""
    if trusted:
        low = max(s27.SCEN_LOW * calc27 - stock, 0)
        high = max(s27.SCEN_HIGH * calc27 - stock, 0)
        if C > high + MIN_DIFF:
            return "много", round(C - high)
        if C < low - MIN_DIFF:
            return "мало", round(C - low)
        return "в коридоре", 0
    N = nina_order
    if N < 200 and C < 300:
        return "в коридоре", 0
    if C > NINA_HI * N + MIN_DIFF:
        return "много", round(C - NINA_HI * N)
    if C < NINA_LO * N - MIN_DIFF:
        return "мало", round(C - NINA_LO * N)
    return "в коридоре", 0


def verdict_color(C: float, plan: float, nina_order: float, live: float | None) -> str:
    if C == 0 and nina_order >= 300:
        v = "нет в контракте — не хватит"
    elif plan < 50 and C >= 100:
        v = "нет спроса в плане — не продадим"
    elif C > 1.25 * nina_order + 100:
        v = "МНОГО"
    elif C < 0.75 * nina_order - 100:
        v = "МАЛО"
    else:
        v = "в коридоре"
    if live is not None and live >= plan > 0 and v != "нет спроса в плане — не продадим":
        v = "сток+заказы сейчас ≥ плана (проверить)" if C >= 200 else "покрыто стоком+заказами (проверить)"
    return v


def group_table(contract: pd.DataFrame, G: pd.DataFrame) -> pd.DataFrame:
    """contract: grp, qty (сумма); G: build_table(...) по группам US."""
    c = contract.groupby("grp", dropna=False)["qty"].sum()
    base = G.set_index("grp")
    rows = []
    for grp, C in c.items():
        if grp not in base.index:
            rows.append(dict(grp=grp, contract=C, plan=np.nan, stock=np.nan, nina_order=np.nan, model_order=np.nan,
                             trusted=False, verdict="нет в планере", excess=0, winter=bool(WINTER.search(str(grp)))))
            continue
        r = base.loc[grp]
        trusted = pc.model_ok(r)
        v, ex = verdict_group(C, float(r.n_order or 0), float(r.calc27 or 0), float(r.n_stock or 0), trusted)
        rows.append(dict(grp=grp, contract=C, plan=r.P27, stock=r.n_stock, nina_order=r.n_order,
                         model_order=r.model_order, trusted=trusted, verdict=v, excess=ex,
                         winter=bool(WINTER.search(str(grp)))))
    df = pd.DataFrame(rows)
    df["_a"] = df["excess"].abs()
    return df.sort_values(["winter", "_a"], ascending=[True, False]).drop(columns="_a").reset_index(drop=True)


def color_table(contract: pd.DataFrame, grp: str, Gc: pd.DataFrame, live: dict[str, float]) -> pd.DataFrame:
    """Цвета одной группы: контракт против плана/заказа Нины. live: color → сток+заказы сейчас."""
    cc = contract[contract["grp"] == grp]
    cmap: dict[str, float] = {}
    for r in cc.itertuples():
        cmap[norm_color(r.cn_color)] = cmap.get(norm_color(r.cn_color), 0) + r.qty
    rows, used = [], set()
    for r in Gc[Gc["grp"] == grp].itertuples():
        k = norm_color(r.color)
        C = cmap.get(k, 0.0)
        used.add(k)
        lv = live.get(r.color)
        rows.append(dict(color=r.color, contract=C, plan=r.P27, stock=r.n_stock, nina_order=r.n_order, live=lv,
                         verdict=verdict_color(C, r.P27, r.n_order, lv), diff=C - r.n_order))
    for r in cc.itertuples():
        k = norm_color(r.cn_color)
        if k not in used:
            used.add(k)
            rows.append(dict(color=r.cn_color, contract=cmap[k], plan=np.nan, stock=np.nan, nina_order=np.nan,
                             live=None, verdict="нет в планере — сверить цвет", diff=np.nan))
    return pd.DataFrame(rows).sort_values("contract", ascending=False)


def country_split(group_qty: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Оценка: контракт группы делим по доле заказа Нины в странах.
    group_qty: grp, qty; orders: country, grp, n_order."""
    o = orders.copy()
    tot = o.groupby("grp")["n_order"].transform("sum")
    o["share"] = np.where(tot > 0, o["n_order"] / tot, np.nan)
    m = o.merge(group_qty, on="grp")
    m["est"] = m["qty"] * m["share"]
    return m


# ------------------------------------------------------------------ данные
def load_live(client) -> dict[tuple[str, str], float]:
    """Сток+заказы сейчас (US) по группе×цвету; если таблицы нет — пусто."""
    try:
        df = s27._q(client, f"""
            SELECT s.grp, IFNULL(NULLIF(s.color,''),'—') AS color, SUM(c.stock_plus_orders_us) AS spo
            FROM (SELECT DISTINCT asin, grp, color FROM `{s27.SRC}` WHERE country='US') s
            JOIN `{s27.PROJECT}.forecast.stock_current` c USING (asin) GROUP BY 1,2""")
    except Exception:  # noqa: BLE001
        return {}
    return {(r.grp, r.color): float(r.spo or 0) for r in df.itertuples()}


# ------------------------------------------------------------------ интерфейс
def render(client, author, log_event, tab_intro, num) -> None:
    tab_intro(
        "Проверка заказа фабрике: контракт против плана Нины и модели.",
        "Загрузите xlsx контракта (лист с шапкой «CODE category»). Логика: плановый сток на май + заказ = продажи "
        "май–сентябрь. Вердикт по группе: «много» / «мало» / «в коридоре» (модель ±25%, без модели — против заказа Нины). "
        "Страны — оценка по доле заказа Нины: в контракте разбивки нет.",
    )
    up = st.file_uploader("Контракт (xlsx)", type=["xlsx"], key="oc_file")
    if up is None:
        st.info("Загрузите файл контракта — таблицы построятся сразу.")
        return
    try:
        lines = parse_contract(up.getvalue())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не прочитал файл: {exc}")
        return
    if lines.empty:
        st.error("Не нашёл строк: нужен лист с шапкой «CODE category» и колонкой «Total».")
        return

    ov = st.session_state.setdefault("oc_codemap", {})
    lines = map_groups(lines, ov)
    miss = sorted(lines.loc[lines["grp"].isna(), "code"].unique())
    try:
        season = s27.load_season(client)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Нет доступа к BigQuery: {exc}")
        return
    if season.empty:
        st.info("Данных Нины ещё нет — сначала «Обновить из таблиц Нины» на вкладке «Лето '27».")
        return
    all_grps = sorted(season["grp"].dropna().unique())
    if miss:
        st.warning(f"Нет группы для {len(miss)} кодов ({int(lines.loc[lines['grp'].isna(), 'qty'].sum()):,} шт). Назначьте:")
        for code in miss:
            q = int(lines.loc[lines["code"] == code, "qty"].sum())
            pick = st.selectbox(f"{code} — {q:,} шт", ["—"] + all_grps, key=f"oc_map_{code}")
            if pick != "—":
                ov[code] = pick
        lines = map_groups(lines, ov)
    st.caption(f"Строк в контракте: {len(lines)} · всего {int(lines['qty'].sum()):,} шт.")

    sdf = season[season["country"] == "US"].copy()
    sdf_all = sdf.copy()
    sdf = sdf[sdf["reorder"].str.lower() == "да"] if (sdf["reorder"].str.lower() == "да").any() else sdf
    has_nina = (sdf[["n25", "n26", "n27"]].abs().sum() > 0).all()
    G = pc.build_table(sdf, sdf_all, has_nina, False)
    GT = group_table(lines.dropna(subset=["grp"]), G)

    st.subheader("По группам (US-план Нины)")
    show = st.radio("Показать", ["Все", "много", "мало"], horizontal=True, key="oc_show")
    seasonal = st.toggle("Только лето (без зимних групп)", value=True, key="oc_summer")
    t = GT[~GT["winter"]] if seasonal else GT
    if show != "Все":
        t = t[t["verdict"] == show]
    st.dataframe(
        t.rename(columns=dict(grp="Группа", contract="Контракт", plan="План Нины май–сен", stock="Сток на 1 мая",
                              nina_order="Заказ Нины", model_order="Заказ по модели", trusted="Модели верим",
                              verdict="Вывод", excess="Избыток(+)/нехватка(−)")).drop(columns=["winter"]),
        hide_index=True, use_container_width=True)

    st.subheader("По цветам")
    pick_grp = st.selectbox("Группа", list(GT["grp"]), key="oc_grp")
    Gc = pc.build_table(sdf, sdf_all, has_nina, True)
    live_all = load_live(client)
    live = {c: v for (g, c), v in live_all.items() if g == pick_grp}
    CT = color_table(lines.dropna(subset=["grp"]), pick_grp, Gc, live)
    st.dataframe(CT.rename(columns=dict(color="Цвет", contract="Контракт", plan="План", stock="Сток 1 мая",
                                        nina_order="Заказ Нины", live="Сток+заказы сейчас", verdict="Вывод",
                                        diff="Контракт − заказ")), hide_index=True, use_container_width=True)
    if not live_all:
        st.caption("Сток+заказы сейчас недоступны (нет forecast.stock_current) — вывод только по плану Нины.")

    st.subheader("Оценка по странам")
    ords = season[season["reorder"].str.lower() == "да"].groupby(["country", "grp"], as_index=False)["n_order"].sum()
    gq = lines.dropna(subset=["grp"]).groupby("grp", as_index=False)["qty"].sum()
    CS = country_split(gq, ords)
    piv = CS.pivot_table(index="grp", columns="country", values="est", aggfunc="sum").round()
    piv["Контракт"] = gq.set_index("grp")["qty"]
    st.dataframe(piv.reset_index().rename(columns={"grp": "Группа"}), hide_index=True, use_container_width=True)
    st.caption("Оценка: контракт группы × доля заказа Нины по стране. Реальную разбивку даёт только Нина/Сергей.")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        GT.drop(columns=["winter"]).to_excel(xw, "Группы", index=False)
        CT.to_excel(xw, "Цвета", index=False)
        piv.reset_index().to_excel(xw, "Страны (оценка)", index=False)
    st.download_button("Скачать Excel", buf.getvalue(), "Проверка_заказа.xlsx", key="oc_dl")
