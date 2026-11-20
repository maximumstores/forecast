-- OOS по ledger DataDoe (fba_ledger_summary), магазины США из datadoe_copy.account_map.
-- День = аут, если сумма SELLABLE ending_warehouse_balance по всем местам и магазинам <= 0 (нет строки = 0).
-- ASIN наблюдаем с даты, когда подключены все магазины, где он встречался (full_from): месяц берём
-- только если он наблюдаем целиком с первого дня. Хвост месяца, до которого ledger ещё не дошёл
-- (лаг 4-6 дней), считаем по наблюдённым дням пропорционально (days_observed).
-- Открытый (текущий) месяц не пишем: как и в листе Нины, его там не было.
-- Новый магазин (MERINO-TECH US) подхватывается сам: достаточно строки в account_map и данных в ledger.
CREATE OR REPLACE VIEW `{P}.{DS}.oos_ledger_monthly` AS
WITH stores AS (
  SELECT datadoe_name FROM `{P}.datadoe_copy.account_map`
  WHERE hub_market = 'US' AND NOT IFNULL(excluded, FALSE)),
store_bal AS (
  SELECT l.child_asin AS asin, l.seller_or_vendor_name AS store, l.date, SUM(l.ending_warehouse_balance) AS qty
  FROM `{P}.datadoe_copy.fba_ledger_summary` l
  JOIN stores s ON s.datadoe_name = l.seller_or_vendor_name
  WHERE l.disposition = 'SELLABLE' AND l.child_asin IS NOT NULL AND l.date >= DATE '2026-01-01'
  GROUP BY 1, 2, 3),
store_cov AS (SELECT store, MIN(date) AS d0, MAX(date) AS d1 FROM store_bal GROUP BY store),
asin_cov AS (
  SELECT b.asin, MAX(c.d0) AS full_from
  FROM (SELECT DISTINCT asin, store FROM store_bal) b JOIN store_cov c USING (store)
  GROUP BY b.asin),
last_day AS (SELECT MIN(d1) AS d1 FROM store_cov),
daily AS (SELECT asin, date, SUM(qty) AS qty FROM store_bal GROUP BY asin, date),
days AS (
  SELECT d, DATE_TRUNC(d, MONTH) AS month
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE '{CUTOVER}', (SELECT d1 FROM last_day))) AS d),
grid AS (
  SELECT a.asin, d.d, d.month, IFNULL(y.qty, 0) AS qty
  FROM asin_cov a
  CROSS JOIN days d
  LEFT JOIN daily y ON y.asin = a.asin AND y.date = d.d
  WHERE d.d >= a.full_from)
SELECT asin, month,
       CAST(ROUND(SAFE_DIVIDE(COUNTIF(qty <= 0), COUNT(*)) * EXTRACT(DAY FROM LAST_DAY(month)), 1) AS NUMERIC) AS oos_days,
       COUNT(*) AS days_observed,
       'ledger' AS oos_source
FROM grid
WHERE month < DATE_TRUNC(CURRENT_DATE('Europe/Kiev'), MONTH)
GROUP BY asin, month
HAVING MIN(d) = month;
