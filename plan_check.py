"""Вкладка «Проверка плана '27» — построчная проверка плана на лето '27 перед заказом.

По каждой группе (и цвету) рядом: план Нины, модель («Расчёт '27»), факт '26, сток, приход и заказ
с флагами риска. Рядом с каждой строкой можно отметить «ок / поправить / вопрос» и вписать новую
цифру. Отметки пишутся в ту же таблицу forecast.summer_review, что и вкладка «Лето '27», —
поэтому «Версии плана» и «Лето '27» видят их без переноса.

Все расчёты берут готовые функции summer27 (summarize, apply_ytd), чтобы цифры совпадали с «Летом '27».
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

import summer27 as s27

# --- пороги флагов. Ширина коридора модели общая со сценариями «Лета '27» (±25%).
OVER_HI = 1.5                  # план больше модели в 1.5+ раза — главный риск перезаказа
OVER = s27.SCEN_HIGH           # 1.25: план выше верхнего сценария
UNDER = s27.SCEN_LOW           # 0.75: план ниже нижнего сценария
ORDER_TOL = 0.25               # заказ отличается от «заказа по модели» больше чем на 25% …
ORDER_MIN = 200                # … и больше чем на 200 шт (мелочь не флагуем)

DECISIONS = ["—", "ок", "поправить", "вопрос"]
EVIDENCE_SHORT = ["—", "A", "B", "C", "D"]
EDIT_COLS = ["Решение", "Новая цифра", "Причина", "Основание", "Комментарий"]


# ------------------------------------------------------------------ расчёты (без Streamlit — тестируются отдельно)
def model_ok(r) -> bool:
    """Модели можно верить: есть лето '25 и лето '26 и это не зимний товар
    (зимой янв–мар продали больше, чем всё лето). Тот же критерий, что у ARIMA в «Лете '27»."""
    return bool(r.F25 > 0 and r.F26 > 0 and r.w26 <= r.f26 and pd.notna(r.calc27) and r.calc27 > 0)


def model_order(r) -> float:
    """Оценка заказа по модели: спрос по модели минус сток и приход. Оценка, а не расчёт Нины:
    она считает по месяцам, мы — по сезону целиком."""
    if not model_ok(r):
        return np.nan
    return max(r.calc27 - (r.n_stock or 0) - (r.n_inc or 0), 0.0)


def plan_flags(r) -> str:
    out: list[str] = []
    ok = model_ok(r)
    if not ok:
        if r.P27 > 0 or r.F26 > 0:
            if r.F25 == 0 or r.F26 == 0:
                out.append("🆕 мало истории — модели не верим")
            else:
                out.append("❄️ зимний товар — модель занижена/завышена, смотрим план")
    else:
        ratio = r.P27 / r.calc27
        if ratio > OVER_HI:
            out.append("🔴 план > модели в 1.5+ раза")
        elif ratio > OVER:
            out.append("🟠 план выше модели")
        elif ratio < UNDER and r.P27 > 0:
            out.append("🔵 план ниже модели")
    if r.P27 > 0 and (r.n_stock or 0) >= r.P27:
        out.append("📦 сток уже ≥ плана")
    mo = r.model_order
    if pd.notna(mo):
        diff = (r.n_order or 0) - mo
        if diff > max(ORDER_MIN, ORDER_TOL * mo):
            out.append("💸 заказ больше, чем по модели")
        elif -diff > max(ORDER_MIN, ORDER_TOL * (r.n_order or 0)):
            out.append("🛒 возможен недозаказ")
    return " · ".join(out)


def build_table(sdf: pd.DataFrame, sdf_all: pd.DataFrame, has_nina: bool, by_color: bool) -> pd.DataFrame:
    """Строки проверки: группа (или группа × цвет) с планом, моделью, фактом, стоком, заказом и флагами."""
    by = ["grp", "color"] if by_color else ["grp"]
    G = s27.summarize(sdf, by, has_nina)
    if by_color:
        G["_k"] = G["grp"] + "\x1f" + G["color"]
        G = s27.apply_ytd(G, sdf_all.assign(_k=sdf_all["grp"] + "\x1f" + sdf_all["color"]), "_k")
    else:
        G = s27.apply_ytd(G, sdf_all, "grp")
    for c in ("n_stock", "n_inc", "n_order"):
        G[c] = G[c].fillna(0.0)
    G["model_order"] = [model_order(r) for r in G.itertuples()]
    G["ratio"] = np.where(G["calc27"] > 0, G["P27"] / G["calc27"], np.nan)
    G["pflag"] = [plan_flags(r) for r in G.itertuples()]
    G["over_order"] = G["n_order"] - G["model_order"]
    if "color" not in G:
        G["color"] = ""
    return G


def severity(flag: str) -> int:
    for i, mark in enumerate(("🔴", "💸", "📦", "🟠", "🛒", "🔵", "❄️", "🆕")):
        if mark in flag:
            return i
    return 9


# ------------------------------------------------------------------ отметки
def to_status(decision: str, new, plan: float) -> str | None:
    if decision == "ок":
        return "согласовано"
    if decision == "вопрос":
        return "под вопросом"
    if decision == "поправить":
        if new < plan:
            return "снизить"
        if new > plan:
            return "повысить"
        return "согласовано"
    return None


def decision_from_status(status) -> str:
    return {"согласовано": "ок", "снизить": "поправить", "повысить": "поправить",
            "под вопросом": "вопрос"}.get(status if isinstance(status, str) else "", "—")


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v == "" or v == "—"


def collect_changes(base: pd.DataFrame, edited: pd.DataFrame, meta: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """Сравнивает таблицу до и после правки. Возвращает (что сохранить, ошибки).
    meta — строки в том же порядке: grp, color, P27 (план), calc27 (модель)."""
    rows: list[dict] = []
    errors: list[str] = []
    for idx in base.index:
        b, e = base.loc[idx], edited.loc[idx]
        if all((_blank(b[c]) and _blank(e[c])) or (not _blank(b[c]) and not _blank(e[c]) and b[c] == e[c])
               for c in EDIT_COLS):
            continue
        m = meta.loc[idx]
        name = m["grp"] + (f" / {m['color']}" if m["color"] else "")
        dec = "—" if _blank(e["Решение"]) else e["Решение"]
        new = None if _blank(e["Новая цифра"]) else float(e["Новая цифра"])
        reason = None if _blank(e["Причина"]) else e["Причина"]
        evid = None if _blank(e["Основание"]) else e["Основание"]
        text = "" if _blank(e["Комментарий"]) else str(e["Комментарий"]).strip()
        if dec == "поправить" and new is None:
            errors.append(f"{name}: «поправить» — впишите новую цифру")
            continue
        if new is not None and dec != "поправить":
            errors.append(f"{name}: есть новая цифра — поставьте «поправить»")
            continue
        calc = m["calc27"]
        if new is not None and pd.notna(calc) and calc > 0:
            dev = new / calc - 1
            if abs(dev) > s27.REASON_LIMIT and (reason is None or evid is None):
                errors.append(f"{name}: новая цифра отличается от модели на {dev:+.0%} — "
                              "выберите причину и основание")
                continue
        rows.append({"grp": m["grp"], "color": m["color"] or None, "new": new,
                     "status": to_status(dec, new, m["P27"]), "comment": text,
                     "reason": reason, "evidence": evid})
    return rows, errors


# ------------------------------------------------------------------ интерфейс
def render(client, author, log_event, tab_intro, num) -> None:
    tab_intro(
        f"Построчная проверка плана на лето '27 ({s27.SEASON_TXT}) перед заказом.",
        "По каждой группе и цвету рядом: план Нины, модель, факт '26, сток, приход и заказ. "
        "Флаги: 🔴 план больше модели в 1.5+ раза · 🟠 выше модели · 📦 сток уже больше плана · "
        "💸 заказ больше, чем по модели · 🛒 возможен недозаказ · 🆕/❄️ у модели мало данных — смотрим на план. "
        "В правых колонках отметьте «ок / поправить / вопрос» и впишите новую цифру — сохранится с автором и временем.",
    )
    try:
        s27.ensure_tables(client)
        season = s27.load_season(client)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Нет доступа к BigQuery: {exc}")
        return
    if season.empty:
        st.info("Данных Нины ещё нет — сначала нажмите «Обновить из таблиц Нины» на вкладке «Лето '27».")
        return
    ts = season.sort_values("loaded_at")["loaded_txt"].iloc[-1]
    st.caption(f"Данные из таблиц Нины на {ts} (Киев). Обновляются кнопкой на вкладке «Лето '27».")

    countries = [c for c in s27.SOURCES if c in set(season["country"])]
    c1, c2 = st.columns([3, 2])
    country = c1.radio("Страна", countries, horizontal=True, key="pc_country",
                       format_func=lambda c: s27.COUNTRY_LABEL.get(c, c))
    level = c2.radio("Уровень", ["Группа", "Группа и цвет"], horizontal=True, key="pc_level")
    by_color = level == "Группа и цвет"

    sdf = season[season["country"] == country].copy()
    has_nina = (sdf[["n25", "n26", "n27"]].abs().sum() > 0).all()
    f1, f2 = st.columns([2, 3])
    cats = f1.multiselect("Категория", sorted(x for x in sdf["category"].dropna().unique() if x), key="pc_cat")
    if cats:
        sdf = sdf[sdf["category"].isin(cats)]
    grps = f2.multiselect("Группа", sorted(sdf["grp"].unique()), key="pc_grp")
    if grps:
        sdf = sdf[sdf["grp"].isin(grps)]
    sdf_all = sdf.copy()
    t1, t2, t3 = st.columns(3)
    only_reorder = t1.toggle("Только «Reorder = Да»", value=True, key="pc_reorder",
                             help="Как в сводной order у Нины: строки «Нет» — снятые с заказа и старые ASIN.")
    only_flag = t2.toggle("Только с флагами", key="pc_onlyflag")
    only_open = t3.toggle("Только без отметки", key="pc_onlyopen")
    if only_reorder and (sdf["reorder"].str.lower() == "да").any():
        sdf = sdf[sdf["reorder"].str.lower() == "да"]

    G = build_table(sdf, sdf_all, has_nina, by_color)

    # ---------------- отметки
    rev = s27.load_reviews(client)
    last = s27.latest_reviews(rev)
    last = last[last["country"] == country] if not last.empty else last
    lk = {(r.grp, r.color or ""): r for r in last.itertuples()} if not last.empty else {}

    def rv(row, attr, default=None):
        r = lk.get((row.grp, (row.color if by_color else "") or ""))
        v = getattr(r, attr, default) if r is not None else default
        return default if v is None or (isinstance(v, float) and np.isnan(v)) else v

    G["Решение"] = [decision_from_status(rv(r, "status")) for r in G.itertuples()]
    G["Новая цифра"] = [rv(r, "new_plan27", np.nan) for r in G.itertuples()]
    G["Причина"] = [rv(r, "reason", "—") for r in G.itertuples()]
    G["Основание"] = [rv(r, "evidence", "—") for r in G.itertuples()]
    G["Комментарий"] = [rv(r, "comment", "") for r in G.itertuples()]

    # ---------------- итоги страны
    ok = G[G["calc27"].notna()]
    m = st.columns(6)
    m[0].metric("Факт '26", num(G["F26"].sum()))
    m[1].metric("План Нины '27", num(G["P27"].sum()), s27._pct(G["P27"].sum() / G["F26"].sum() - 1
                if G["F26"].sum() else np.nan) + " к '26")
    m[2].metric(f"Модель '27 · {len(ok)} групп", num(ok["calc27"].sum()),
                s27._pct(ok["P27"].sum() / ok["calc27"].sum() - 1 if ok["calc27"].sum() else np.nan) + " план к модели",
                help="«Расчёт '27» из вкладки «Лето '27»: факт '26 без аутов × рост янв–авг. Процент — "
                     "на сколько план Нины выше модели по тем же группам.")
    m[3].metric("Сток (расч.) + приход", num(G["n_stock"].sum() + G["n_inc"].sum()))
    m[4].metric("Заказ (Нина)", num(G["n_order"].sum()),
                help="Колонка «Заказ» из сводной Нины.")
    mo = G["model_order"].sum(skipna=True)
    m[5].metric("Заказ по модели (оценка)", num(mo),
                help="Спрос по модели минус сток и приход, по группам, где модели можно верить. "
                     "Оценка по сезону целиком, не расчёт по месяцам — для сравнения порядка величин.")
    done = int((G["Решение"] != "—").sum())
    st.progress(done / len(G) if len(G) else 0.0, text=f"Проверено {done} из {len(G)} строк")

    # ---------------- главные риски
    over = G[(G["over_order"] > max(ORDER_MIN, 0)) & G["pflag"].str.contains("💸")].sort_values("over_order", ascending=False)
    under = G[G["pflag"].str.contains("🛒")].sort_values("over_order").head(8)
    r1, r2 = st.columns(2)
    risk_cols = lambda d: pd.DataFrame({  # noqa: E731
        "Группа" + (" / цвет" if by_color else ""): d["grp"] + (" / " + d["color"] if by_color else ""),
        "План": d["P27"].round(0).astype("Int64"), "Модель": d["calc27"].round(0).astype("Int64"),
        "Заказ": d["n_order"].round(0).astype("Int64"), "По модели": d["model_order"].round(0).astype("Int64")})
    with r1:
        st.markdown("**Риск перезаказа (топ-8)**")
        if len(over):
            st.dataframe(risk_cols(over.head(8)), hide_index=True, width="stretch")
        else:
            st.caption("Нет.")
    with r2:
        st.markdown("**Возможный недозаказ (топ-8)**")
        if len(under):
            st.dataframe(risk_cols(under), hide_index=True, width="stretch")
        else:
            st.caption("Нет.")

    # ---------------- таблица проверки
    view = G.copy()
    if only_flag:
        view = view[view["pflag"] != ""]
    if only_open:
        view = view[view["Решение"] == "—"]
    view["_ord"] = view["pflag"].map(severity)
    view = view.sort_values(["_ord", "P27"], ascending=[True, False]).reset_index(drop=True)
    if view.empty:
        st.info("Нет строк под выбранные фильтры.")
        return

    show = pd.DataFrame({
        "Группа": view["grp"],
        **({"Цвет": view["color"]} if by_color else {}),
        "Флаги": view["pflag"],
        "Факт '26": view["F26"],
        "Факт '26 без аутов": view["F26oos"],
        "План Нины '27": view["P27"],
        "Модель '27": view["calc27"],
        "План / модель": view["ratio"],
        "Сток (расч.)": view["n_stock"],
        "Приход": view["n_inc"],
        "Заказ (Нина)": view["n_order"],
        "Заказ по модели": view["model_order"],
        "Решение": view["Решение"],
        "Новая цифра": view["Новая цифра"],
        "Причина": view["Причина"],
        "Основание": view["Основание"],
        "Комментарий": view["Комментарий"],
    })
    for c in show.columns:
        if pd.api.types.is_float_dtype(show[c]) and c not in ("План / модель", "Новая цифра"):
            show[c] = show[c].round(0).astype("Int64")
    show["Новая цифра"] = show["Новая цифра"].round(0).astype("Float64")
    meta = view[["grp", "color", "P27", "calc27"]].copy()
    meta["color"] = meta["color"].fillna("") if by_color else ""

    nc = st.column_config.NumberColumn(format="localized")
    sig = f"{country}|{level}|{cats}|{grps}|{only_reorder}|{only_flag}|{only_open}|{st.session_state.get('pc_ver', 0)}"
    edited = st.data_editor(
        show, hide_index=True, width="stretch", height=min(640, 38 + 35 * len(show)), num_rows="fixed",
        key=f"pc_editor_{abs(hash(sig))}",
        disabled=[c for c in show.columns if c not in EDIT_COLS],
        column_config={
            **{c: nc for c in ("Факт '26", "Факт '26 без аутов", "План Нины '27", "Модель '27", "Сток (расч.)",
                               "Приход", "Заказ (Нина)", "Заказ по модели")},
            "План / модель": st.column_config.NumberColumn(format="%.2f×"),
            "Решение": st.column_config.SelectboxColumn(options=DECISIONS, required=True),
            "Новая цифра": st.column_config.NumberColumn(min_value=0, step=10, format="localized",
                                                         help="Новый прогноз на май–сентябрь '27, шт."),
            "Причина": st.column_config.SelectboxColumn(options=s27.REASONS, width="medium",
                                                        help="Обязательна, если новая цифра отличается от модели больше чем на 15%."),
            "Основание": st.column_config.SelectboxColumn(options=EVIDENCE_SHORT, required=True, width="small",
                                                          help="A — факт из наших данных · B — проверено на истории '24–'26 · "
                                                               "C — аналог · D — оценка без данных."),
            "Комментарий": st.column_config.TextColumn(width="medium"),
        },
    )

    b1, b2 = st.columns([1, 3])
    save = b1.button("Сохранить отметки", type="primary", key="pc_save")
    b2.download_button("Скачать таблицу (CSV)", show.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"plan_check_{country}.csv", mime="text/csv", key="pc_csv")
    if save:
        rows, errors = collect_changes(show, edited, meta)
        if errors:
            st.warning("Не сохранено, поправьте:\n\n" + "\n".join(f"- {e}" for e in errors))
        elif not rows:
            st.info("Изменений нет.")
        else:
            for r in rows:
                s27.save_review(client, country, r["grp"], r["color"], r["new"], r["status"] or "—",
                                r["comment"], author, r["reason"], r["evidence"])
            s27.load_reviews.clear()
            log_event("plan_check", f"{country} {level}: {len(rows)}")
            st.session_state["pc_ver"] = st.session_state.get("pc_ver", 0) + 1
            st.success(f"Сохранено отметок: {len(rows)}.")
            st.rerun()
