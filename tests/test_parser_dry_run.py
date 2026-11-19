"""Разбор через fetch_country на поддельной сессии Sheets + сравнение «было / станет»."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import parser_dry_run as d  # noqa: E402
import summer27 as s  # noqa: E402

MONTHS_26 = [(2026, m) for m in range(4, 13)] + [(2027, m) for m in (1, 2, 3)]
RU = ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]
HEADER = (["ASIN", "Old ASIN", "Category+parent", "China Colors"]
          + [f"{RU[m - 1]}{y}" for y in (2025, 2026) for m in range(1, 13)]
          + ["Нормированный план продаж 27-28"] + [f"2027-{m:02d}-01" for m in range(4, 13)]
          + ["2028-01-01", "2028-02-01", "2028-03-01"]
          + ["Нормированный план продаж 26-27"] + [f"01.{m:02d}.{y}" for y, m in MONTHS_26])


class R:
    def __init__(self, j):
        self._j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self._j


class Sess:
    def get(self, url, params=None, timeout=None):
        if "batchGet" not in url:
            return R({"values": [HEADER]})
        out = []
        for k, v in params:
            if k != "ranges":
                continue
            col = v.split("!")[1].split(":")[0].rstrip("0123456789")
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            idx -= 1
            vals = {0: ["B000000001", "B000000002"], 1: ["", ""], 2: ["g1", "g1"], 3: ["Black", "Black"]}.get(idx, [10, 20])
            out.append({"values": [vals]})
        return R({"valueRanges": out})


def test_fetch_country_reads_both_plan_blocks():
    df, info = s.fetch_country(Sess(), "US", "x")
    sums = df.groupby("kind").units.sum().to_dict()
    assert info["plan26_months"] == 12 and info["plan27_months"] == 12
    assert sums["plan26"] == 12 * 30 and sums["plan27"] == 12 * 30
    assert set(info["plan_blocks"]) == {2026, 2027}


def test_compare_and_stop_rule():
    idx = pd.MultiIndex.from_tuples([("US", "plan26"), ("US", "plan27"), ("CA", "plan26"), ("DE", "fact")],
                                    names=["country", "kind"])
    old = pd.DataFrame({"rows": [0, 100, 100, 100], "asin": [0, 10, 10, 10], "units": [0, 1000, 1000, 1000]}, index=idx)
    new = pd.DataFrame({"rows": [120, 100, 100, 100], "asin": [10, 10, 10, 10], "units": [1756, 1005, 1050, 1000]}, index=idx)
    out = d.compare(old, new)
    stop = d.must_stop(out)
    assert list(stop.index) == [("CA", "plan26")]      # US plan26 разрешён, +0,5% US plan27 и 0% DE — нет, +5% CA — стоп
