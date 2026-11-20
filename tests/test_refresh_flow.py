"""Ночной job на заглушках (без сети и BigQuery): порядок, устойчивость к сбою листа,
загрузка через стадию с проверками (пустой, короткий, оборванный лист, заголовки)."""
import importlib.util
import os

GOOD_CSV = "ASIN,x\nB000000001,1\nB000000002,2\n"


def load(fail=(), csv_by_table=None, rows_by_table=None, prev_rows=None, header_ok=True,
         asin_ok=None, silver_zero=None):
    spec = importlib.util.spec_from_file_location(
        "refresh", os.path.join(os.path.dirname(__file__), "..", "refresh_job", "refresh.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    ev = {"loaded": [], "copied": [], "dropped": [], "sent": [], "gold": [], "cur": None}
    m.time.sleep = lambda *_: None
    m.get_token = lambda: "t"
    m.resolve_gid = lambda doc, sheet, token: "1"
    m.with_retries = lambda fn, what, *a, **k: fn()

    def dl(doc, gid, path, token):
        # текущую таблицу подскажет порядок вызовов: берём её из SOURCES по счётчику
        table = m.SOURCES[ev["i"]][0]
        ev["cur"] = table
        ev["i"] += 1
        data = (csv_by_table or {}).get(table, GOOD_CSV)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        return len(data)
    ev["i"] = 0
    m.download_sheet = dl

    def ld(client, dest, path, mode, quote, nl, width):
        if ev["cur"] in fail:
            raise RuntimeError("Sheets\nexport 400")
        ev["loaded"].append(dest)
        return (rows_by_table or {}).get(ev["cur"], 100000)
    m.load_csv = ld

    def cnt(client, sql):
        if "CAST(" in sql and "WHERE" in sql:       # проверка заголовков
            return 1 if header_ok else 0
        if "REGEXP_CONTAINS" in sql:                 # настоящие ASIN в ключевой колонке
            for t, v in (asin_ok or {}).items():
                if t.split(".")[-1] in sql:
                    return v
            return 10**7
        if silver_zero and silver_zero in sql:
            return 0
        return 10**7
    m.count = cnt
    m.save_history = lambda client, skip=None: None
    m.notify = lambda text: ev["sent"].append(text)

    class Job:
        def result(self):
            return []

    class T:
        def __init__(self, n):
            self.num_rows = n

    class C:
        def __init__(self, *a, **k):
            pass

        def query(self, sql, **k):
            if "sp_refresh_gold" in sql:
                ev["gold"].append(1)
            return Job()

        def get_table(self, name):
            short = name.split(".", 1)[1]
            for tbl, _ in ((s[0], 0) for s in m.SOURCES):
                if tbl.split(".")[-1] == short:
                    return T((prev_rows or {}).get(tbl, 100000))
            return T(100000)

        def copy_table(self, src, dst, **k):
            ev["copied"].append(dst.split(".", 1)[0].split("reorder-497714")[0] + dst.split("reorder-497714.")[1])
            return Job()

        def delete_table(self, name, **k):
            ev["dropped"].append(name.split(".", 1)[1])
    m.bigquery.Client = C
    return m, ev


def test_order_spr_last():
    m, _ = load()
    names = [s[0] for s in m.SOURCES]
    spr = names.index("mt.SPR_native")
    for t in names:
        if t.startswith(("mt.hopted", "mt.order", "mt.amazon_starting", "mt.sales")):
            assert names.index(t) < spr


def test_all_ok_loads_stage_then_copies():
    m, ev = load()
    assert m.main() == 0
    n = len(m.SOURCES)
    assert len(ev["copied"]) == n and ev["gold"] and not ev["sent"]
    assert all(x.startswith("mt._stage_") for x in ev["loaded"])
    assert len(ev["dropped"]) == n


def test_spr_fails_others_continue_no_gold():
    m, ev = load(fail={"mt.SPR_native"})
    assert m.main() == 1
    assert len(ev["copied"]) == len(m.SOURCES) - 1 and "mt.SPR_native" not in ev["copied"]
    assert not ev["gold"]
    assert ev["sent"] and "SPR_native" in ev["sent"][0]


def test_hopted_fails_gold_still_runs_exit_1():
    m, ev = load(fail={"mt.hopted_us_native"})
    assert m.main() == 1 and ev["gold"] and ev["sent"]


def test_empty_sheet_does_not_touch_working_table():
    m, ev = load(csv_by_table={"mt.order_us_native": ""})
    assert m.main() == 1
    assert "mt.order_us_native" not in ev["copied"]
    assert "пуст" in ev["sent"][0]


def test_short_sheet_below_min_rows():
    m, ev = load(rows_by_table={"mt.order_uk_native": 120})
    assert m.main() == 1
    assert "mt.order_uk_native" not in ev["copied"]
    assert "ожидалось не меньше" in ev["sent"][0]


def test_truncated_sheet_unclosed_quote():
    cut = 'ASIN,x\nB000000001,"оборвано на середине поля'
    m, ev = load(csv_by_table={"mt.hopted_ca_native": cut})
    assert m.main() == 1
    assert "mt.hopted_ca_native" not in ev["copied"]
    assert "оборван" in ev["sent"][0]


def test_truncated_sheet_last_row_cut():
    cut = "a,b,c,d,e,f\n1,2,3,4,5,6\n1,2,3,4,5,6\n7"
    m, ev = load(csv_by_table={"mt.hopted_de_native": cut})
    assert m.main() == 1 and "hopted_de_native" not in ev["copied"]


def test_sudden_shrink_vs_previous_table():
    m, ev = load(rows_by_table={"mt.order_ca_native": 6000}, prev_rows={"mt.order_ca_native": 11292})
    assert m.main() == 1
    assert "mt.order_ca_native" not in ev["copied"]
    assert "сжатие" in ev["sent"][0]


def test_header_mismatch_keeps_working_table():
    m, ev = load(header_ok=False)
    assert m.main() == 1
    checked = set(m.HEADER_CHECKS)
    # таблицы с проверкой заголовков не скопированы, три листа продаж без неё — скопированы
    for t in checked:
        assert t not in ev["copied"]
    assert {"mt.sales_us_fact_native", "mt.sales_oos_amz_native", "mt.sales_plus_oos_native"} <= set(ev["copied"])


def test_check_csv_complete_ok_and_no_quote_mode():
    m, _ = load()
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        f.write('a,"b\nc,d\n')          # непарная кавычка допустима в режиме без кавычек
    assert m.check_csv_complete(f.name, "") == 2


def test_one_line():
    m, _ = load()
    assert "\n" not in m.one_line(RuntimeError("a\nb"))


def test_sales_key_column_empty_fails_fast_and_names_cause():
    m, ev = load(asin_ok={"mt.sales_oos_amz_native": 0})
    assert m.main() == 1
    assert "mt.sales_oos_amz_native" not in ev["copied"]
    assert "mt.sales_us_fact_native" in ev["copied"] and "mt.order_us_native" in ev["copied"]
    assert not ev["gold"]
    first = ev["sent"][0]            # сообщение сразу при сбое, не в конце запуска
    assert "mt.sales_oos_amz_native" in first and "string_field_0" in first
    assert "пустая" in first and "ASIN" in first


def test_silver_check_names_table_and_source():
    import pytest
    m, ev = load(silver_zero="oos_monthly")
    with pytest.raises(RuntimeError) as e:
        m.main()
    msg = str(e.value)
    assert "forecast.oos_monthly" in msg and "mt.sales_oos_amz_native" in msg
    assert "неполным" not in msg


def test_telegram_optional(monkeypatch):
    m, ev = load()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    import importlib
    spec = importlib.util.spec_from_file_location(
        "refresh2", os.path.join(os.path.dirname(__file__), "..", "refresh_job", "refresh.py"))
    r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
    r.notify("тест")      # без секретов только предупреждение в лог, без исключения
