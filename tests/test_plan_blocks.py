"""Поиск блоков «Нормированный план продаж» по подписи, в обоих форматах дат."""
import datetime as dt
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# summer27 тянет streamlit/pandas/bigquery на импорте; для разбора заголовков они не нужны
for name in ("streamlit",):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.SimpleNamespace(cache_data=lambda *a, **k: (lambda f: f), secrets={})
import summer27 as s  # noqa: E402


def months(start_year, fmt):
    out = []
    y, m = start_year, 4
    for _ in range(12):
        out.append(f"{y}-{m:02d}-01" if fmt == "iso" else f"01.{m:02d}.{y}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def header(title27, fmt27, title26, fmt26, with_arrivals=True):
    h = ["ASIN", "Old ASIN", "Category+parent"]
    if with_arrivals:   # блок приходов 01.04.2026 … без подписи плана: читаться не должен
        h += months(2026, "dmy") + ["01.04.2027", "01.05.2027"]
    h += [title27] + months(2027, fmt27) + ["20,00%"] + [title26] + months(2026, fmt26) + ["статистика для заказа"]
    return h


def check(h, expect26_first, expect27_first):
    c = s.map_columns(h)
    assert min(c["plan26"]) == expect26_first and len(c["plan26"]) == 12
    assert min(c["plan27"]) == expect27_first and len(c["plan27"]) == 12
    assert max(c["plan26"]) == (2027, 3) and max(c["plan27"]) == (2028, 3)
    return c


def test_us_layout_dmy_for_26_27_iso_for_27_28():
    h = header("Нормированный план продаж 27-28", "iso", "Нормированный план продаж 26-27", "dmy")
    c = check(h, (2026, 4), (2027, 4))
    # колонки плана '26 идут сразу за подписью, а не в блоке приходов
    assert h[c["plan26"][(2026, 4)] - 1].startswith("Нормированный план продаж 26-27")
    assert c["plan_blocks"] == {2026: s.col_letter(h.index("Нормированный план продаж 26-27")),
                                2027: s.col_letter(h.index("Нормированный план продаж 27-28"))}


def test_ca_uk_layout_typo_in_title_and_no_year():
    h = header("Нормированный план продаж", "iso", "Нормированный план продаж 26-25 ", "iso")
    check(h, (2026, 4), (2027, 4))


def test_both_formats_everywhere():
    for f27 in ("iso", "dmy"):
        for f26 in ("iso", "dmy"):
            check(header("Нормированный план продаж 27-28", f27, "Нормированный план продаж 26-27", f26),
                  (2026, 4), (2027, 4))


def test_dates_without_title_are_not_read():
    h = ["ASIN", "Category+parent"] + months(2026, "dmy") + ["x"] + months(2027, "iso")
    c = s.map_columns(h)
    assert c["plan26"] == {} and c["plan27"] == {}


def test_serial_dates():
    ser = [str(46113 + 31 * k) for k in range(12)]   # 46113 = 2026-04-01; остальные не с 1-го числа -> не блок
    h = ["Нормированный план продаж 26-27"] + ser
    assert s.find_plan_blocks(h, [s._key(x) for x in h]) == {}
    assert s._plan_month("46113") == dt.date(2026, 4, 1)
