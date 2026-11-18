"""Проверка порядка и устойчивости ночного job на заглушках (без сети и BigQuery)."""
import importlib.util, os, sys, types

def load(monkey_fail=()):
    spec = importlib.util.spec_from_file_location(
        "refresh", os.path.join(os.path.dirname(__file__), "..", "refresh_job", "refresh.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls, sent, gold = [], [], []
    m.time.sleep = lambda *_: None
    m.get_token = lambda: "t"
    m.resolve_gid = lambda doc, sheet, token: "1"
    m.with_retries = lambda fn, what, *a, **k: fn()
    def dl(doc, gid, path, token):
        return 1
    m.download_sheet = dl
    def ld(client, table, path, mode, quote, nl, width):
        calls.append(table)
        if table in monkey_fail:
            raise RuntimeError("Sheets\nexport 400")
        return 100000
    m.load_csv = ld
    m.count = lambda client, sql: 10**7
    m.save_history = lambda client, skip=None: None
    m.notify = lambda text: sent.append(text)
    class Q:
        def result(self):
            return []
    class C:
        def __init__(self, *a, **k): pass
        def query(self, sql, **k):
            if "sp_refresh_gold" in sql:
                gold.append(1)
            return Q()
    m.bigquery.Client = C
    return m, calls, sent, gold

def test_order_spr_last():
    m, *_ = load()
    names = [s[0] for s in m.SOURCES]
    spr = names.index("mt.SPR_native")
    for t in names:
        if t.startswith(("mt.hopted", "mt.order", "mt.amazon_starting", "mt.sales")):
            assert names.index(t) < spr

def test_all_ok():
    m, calls, sent, gold = load()
    assert m.main() == 0 and gold and not sent and len(calls) == len(m.SOURCES)

def test_spr_fails_others_continue_no_gold():
    m, calls, sent, gold = load({"mt.SPR_native"})
    assert m.main() == 1
    assert len(calls) == len(m.SOURCES)          # дошли до конца
    assert not gold                               # пересборка пропущена
    assert sent and "SPR_native" in sent[0] and "\n" not in sent[0].split("- ")[1].split(": ", 1)[1]

def test_hopted_fails_gold_still_runs_exit_1():
    m, calls, sent, gold = load({"mt.hopted_us_native"})
    assert m.main() == 1 and gold and sent

def test_one_line():
    m, *_ = load()
    assert "\n" not in m.one_line(RuntimeError("a\nb"))
