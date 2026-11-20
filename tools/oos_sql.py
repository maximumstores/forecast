"""Собирает SQL нового OOS из sql/oos/*.sql с подстановкой параметров.

    python tools/oos_sql.py --ds forecast_shadow --cutover 2026-09-01 > /tmp/oos_shadow.sql
    python tools/oos_sql.py --ds forecast --cutover 2026-11-01        > /tmp/oos_prod.sql

Файл содержит несколько CREATE OR REPLACE VIEW; выполнять по порядку (по одному стейтменту).
"""
import argparse
import glob
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SQL_DIR = os.path.join(HERE, "..", "sql", "oos")


def render(ds: str, cutover: str, project: str = "reorder-497714", hopted_min_days: int = 15) -> list[str]:
    if not re.fullmatch(r"\d{4}-\d{2}-01", cutover):
        raise ValueError("cutover должен быть первым числом месяца: YYYY-MM-01")
    out = []
    for path in sorted(glob.glob(os.path.join(SQL_DIR, "*.sql"))):
        text = open(path, encoding="utf-8").read()
        text = (text.replace("{P}", project).replace("{DS}", ds)
                .replace("{CUTOVER}", cutover).replace("{HOPTED_MIN_DAYS}", str(hopted_min_days)))
        # '{{' и '}}' в файлах нужны только для экранирования; здесь шаблон не format(), но
        # регулярки в SQL пишем с одинарными скобками, поэтому схлопываем двойные.
        text = text.replace("{{", "{").replace("}}", "}")
        left = re.findall(r"\{[A-Z_]+\}", text)
        if left:
            raise ValueError(f"{path}: не подставлено {left}")
        for stmt in text.split(";\n"):
            body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if body:
                out.append(stmt.strip() + ";")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ds", required=True)
    ap.add_argument("--cutover", required=True)
    ap.add_argument("--hopted-min-days", type=int, default=15)
    a = ap.parse_args()
    print("\n\n".join(render(a.ds, a.cutover, hopted_min_days=a.hopted_min_days)))
