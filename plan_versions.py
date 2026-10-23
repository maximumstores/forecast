"""Версии плана: кнопка «Утвердить» и сравнение утверждённых версий.

Зачем: без сохранённых версий нельзя измерить, насколько точен план (какой именно план
угадывали и кто его утвердил). Снимок = точная копия forecast.summer_src на момент
утверждения плюс последние правки из forecast.summer_review (с причинами и уровнем A–D).

Таблицы:
  forecast.plan_snapshots      — уже существует: строки summer_src с меткой snap_ts и label.
                                 Структуру НЕ меняем, этот модуль только дописывает в неё.
  forecast.plan_versions       — создаётся здесь: одна строка на версию (кто, когда, комментарий, итоги).
  forecast.plan_version_edits  — создаётся здесь: правки группа × цвет, действовавшие на момент утверждения.

Версию нельзя перезаписать: повторная метка отклоняется. Версия без автора не сохраняется.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from google.cloud import bigquery

PROJECT = "reorder-497714"
SRC = f"{PROJECT}.forecast.summer_src"
REVIEW = f"{PROJECT}.forecast.summer_review"
SNAPS = f"{PROJECT}.forecast.plan_snapshots"
VERSIONS = f"{PROJECT}.forecast.plan_versions"
EDITS = f"{PROJECT}.forecast.plan_version_edits"

COUNTRY_LABEL = {"US": "США", "CA": "Канада", "DE": "Европа (DE)", "UK": "UK"}
MIN_LABEL = 5


def _q(client, sql: str, params: list | None = None) -> pd.DataFrame:
    return client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or [])
                        ).result().to_dataframe()


def ensure_tables(client: bigquery.Client) -> None:
    # Колонки причины и уровня доказанности добавляет и summer27.ensure_tables; здесь то же самое
    # (IF NOT EXISTS — безопасно повторять), чтобы утверждение не упало, если вкладка «Лето '27»
    # ещё не запускалась после обновления.
    client.query(f"ALTER TABLE `{REVIEW}` ADD COLUMN IF NOT EXISTS reason STRING, "
                 "ADD COLUMN IF NOT EXISTS evidence STRING").result()
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{VERSIONS}` (
          snap_ts TIMESTAMP NOT NULL,
          label STRING NOT NULL,
          author STRING NOT NULL,
          note STRING,
          src_rows INT64,
          edits INT64,
          fc27_us FLOAT64,
          fc27_all FLOAT64,
          created_at TIMESTAMP NOT NULL
        )""").result()
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{EDITS}` (
          snap_ts TIMESTAMP NOT NULL,
          country STRING NOT NULL,
          grp STRING NOT NULL,
          color STRING,
          new_plan27 FLOAT64,
          status STRING,
          reason STRING,
          evidence STRING,
          comment STRING,
          author STRING,
          edit_ts TIMESTAMP
        )""").result()


# ---------------------------------------------------------------------------
# Утверждение
# ---------------------------------------------------------------------------
def label_exists(client, label: str) -> bool:
    df = _q(client, f"SELECT COUNT(*) n FROM `{VERSIONS}` WHERE label=@l",
            [bigquery.ScalarQueryParameter("l", "STRING", label)])
    return int(df["n"].iloc[0]) > 0


def approve(client, label: str, author: str, note: str = "") -> dict:
    """Сохраняет снимок. Возвращает {'snap_ts', 'src_rows', 'edits'}.

    Всё в одном скрипте BigQuery: один и тот же snap_ts у всех трёх вставок.
    """
    label, author, note = label.strip(), author.strip(), (note or "").strip()
    if len(label) < MIN_LABEL:
        raise ValueError(f"Название версии короче {MIN_LABEL} символов")
    if not author:
        raise ValueError("Не указан автор")
    ensure_tables(client)
    if label_exists(client, label):
        raise ValueError("Версия с таким названием уже есть — задайте другое")
    script = f"""
    DECLARE ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
    INSERT INTO `{SNAPS}`
      (snap_ts, label, country, asin, old_asin, grp, category, color, reorder, kind, month, units, loaded_at)
    SELECT ts, @label, country, asin, old_asin, grp, category, color, reorder, kind, month, units, loaded_at
    FROM `{SRC}`;
    INSERT INTO `{EDITS}`
    SELECT ts, country, grp, color, new_plan27, status, reason, evidence, comment, author, ts_edit
    FROM (
      SELECT country, grp, color, new_plan27, status, reason, evidence, comment, author, ts AS ts_edit,
             ROW_NUMBER() OVER (PARTITION BY country, grp, IFNULL(color,'') ORDER BY ts DESC) AS rn
      FROM `{REVIEW}`
      WHERE new_plan27 IS NOT NULL)
    WHERE rn = 1;
    INSERT INTO `{VERSIONS}` (snap_ts, label, author, note, src_rows, edits, fc27_us, fc27_all, created_at)
    SELECT ts, @label, @author, @note,
      (SELECT COUNT(*) FROM `{SRC}`),
      (SELECT COUNT(*) FROM `{EDITS}` WHERE snap_ts = ts),
      (SELECT IFNULL(SUM(units),0) FROM `{SRC}` WHERE kind='nina_fc27' AND country='US'),
      (SELECT IFNULL(SUM(units),0) FROM `{SRC}` WHERE kind='nina_fc27'),
      CURRENT_TIMESTAMP();
    """
    client.query(script, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("label", "STRING", label),
        bigquery.ScalarQueryParameter("author", "STRING", author),
        bigquery.ScalarQueryParameter("note", "STRING", note),
    ])).result()
    row = _q(client, f"SELECT snap_ts, src_rows, edits FROM `{VERSIONS}` WHERE label=@l",
             [bigquery.ScalarQueryParameter("l", "STRING", label)]).iloc[0]
    return {"snap_ts": row["snap_ts"], "src_rows": int(row["src_rows"]), "edits": int(row["edits"])}


# ---------------------------------------------------------------------------
# Чтение и сравнение
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def list_versions(_client) -> pd.DataFrame:
    try:
        return _q(_client, f"SELECT snap_ts, label, author, note, src_rows, edits, fc27_us, fc27_all "
                           f"FROM `{VERSIONS}` ORDER BY snap_ts DESC")
    except Exception:  # noqa: BLE001  — таблицы ещё нет
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def legacy_snapshots(_client) -> pd.DataFrame:
    """Снимки, сделанные до этого модуля (есть в plan_snapshots, нет в plan_versions)."""
    return _q(_client, f"SELECT snap_ts, ANY_VALUE(label) label, COUNT(*) n FROM `{SNAPS}` GROUP BY snap_ts "
                       "ORDER BY snap_ts DESC")


def _fc27_by_group(client, snap_ts) -> pd.DataFrame:
    """Прогноз '27 по стране × группе из снимка (snap_ts=None → текущее состояние summer_src)."""
    if snap_ts is None:
        return _q(client, f"SELECT country, grp, SUM(units) fc27 FROM `{SRC}` WHERE kind='nina_fc27' GROUP BY 1,2")
    return _q(client, f"SELECT country, grp, SUM(units) fc27 FROM `{SNAPS}` "
                      "WHERE snap_ts=@t AND kind='nina_fc27' GROUP BY 1,2",
              [bigquery.ScalarQueryParameter("t", "TIMESTAMP", snap_ts)])


def compare(client, snap_a, snap_b) -> pd.DataFrame:
    """Изменение прогноза '27 по группам между двумя версиями (b минус a). None = сейчас."""
    a = _fc27_by_group(client, snap_a).rename(columns={"fc27": "A"})
    b = _fc27_by_group(client, snap_b).rename(columns={"fc27": "B"})
    m = a.merge(b, on=["country", "grp"], how="outer").fillna({"A": 0.0, "B": 0.0})
    m["Δ"] = m["B"] - m["A"]
    m["Δ %"] = m.apply(lambda r: r["Δ"] / r["A"] if r["A"] else float("nan"), axis=1)
    return m.sort_values("Δ", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------
def render(client, author, log_event, tab_intro, num) -> None:
    tab_intro(
        "Версии плана: что утвердили, когда и кто.",
        "Кнопка «Утвердить» сохраняет копию прогноза и всех правок на сегодня. Через сезон по версии "
        "считаются точность и перекос — без сохранённых версий проверять нечего.",
    )
    vers = list_versions(client)

    st.subheader("Утвердить текущий план")
    with st.form("approve_plan", clear_on_submit=False):
        label = st.text_input("Название версии", placeholder="Лето '27, встреча 1, 28.10.2026")
        who = st.text_input("Кто утверждает", value=author or "")
        note = st.text_area("Комментарий (что решили, что осталось под вопросом)", height=80)
        ok = st.form_submit_button("Утвердить")
    if ok:
        try:
            res = approve(client, label, who, note)
            list_versions.clear()
            st.success(f"Версия сохранена: {res['src_rows']} строк плана, {res['edits']} правок.")
            log_event("plan_approved", f"{label} · правок: {res['edits']}")
            vers = list_versions(client)
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Не удалось сохранить: {exc}")

    st.subheader("Сохранённые версии")
    if vers.empty:
        st.caption("Утверждённых версий пока нет.")
    else:
        v = vers.copy()
        v["snap_ts"] = pd.to_datetime(v["snap_ts"]).dt.strftime("%d.%m.%Y %H:%M")
        v["fc27_us"] = v["fc27_us"].map(num)
        v["fc27_all"] = v["fc27_all"].map(num)
        st.dataframe(v.rename(columns={
            "snap_ts": "Когда (UTC)", "label": "Название", "author": "Автор", "note": "Комментарий",
            "src_rows": "Строк", "edits": "Правок", "fc27_us": "Прогноз '27 US", "fc27_all": "Прогноз '27 все"}),
            use_container_width=True, hide_index=True)

    legacy = legacy_snapshots(client)
    if not legacy.empty:
        known = set(pd.to_datetime(vers["snap_ts"]).astype("int64")) if not vers.empty else set()
        old = legacy[~pd.to_datetime(legacy["snap_ts"]).astype("int64").isin(known)]
        if not old.empty:
            st.caption("Снимки без автора (сделаны до появления кнопки): " +
                       "; ".join(f"{r.label} ({pd.Timestamp(r.snap_ts):%d.%m.%Y})" for r in old.itertuples()))

    st.subheader("Что изменилось между версиями")
    options = {"Сейчас (текущее состояние)": None}
    for r in legacy.itertuples():
        options[f"{r.label} — {pd.Timestamp(r.snap_ts):%d.%m.%Y %H:%M}"] = r.snap_ts
    if len(options) < 2:
        st.caption("Нужна хотя бы одна сохранённая версия.")
        return
    names = list(options)
    c1, c2 = st.columns(2)
    a_name = c1.selectbox("Было (версия)", names[1:] or names, index=0)
    b_name = c2.selectbox("Стало", names, index=0)
    diff = compare(client, options[a_name], options[b_name])
    diff = diff[diff["Δ"].abs() > 0.5]
    if diff.empty:
        st.info("Прогноз '27 между этими версиями не отличается.")
        return
    d = diff.copy()
    d["country"] = d["country"].map(COUNTRY_LABEL).fillna(d["country"])
    for c in ("A", "B", "Δ"):
        d[c] = d[c].map(num)
    d["Δ %"] = d["Δ %"].map(lambda x: "—" if pd.isna(x) else f"{x:+.0%}")
    st.dataframe(d.rename(columns={"country": "Страна", "grp": "Группа", "A": "Было", "B": "Стало"}),
                 use_container_width=True, hide_index=True)
