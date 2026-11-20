-- Резерв для ASIN вне ledger (магазины, которых нет в DataDoe, пока MERINO-TECH US не подключён):
-- ежедневные срезы листа Hopted US из mt.stock_history (source = hopted_us_native).
-- День = аут, если FBA fulfillable (string_field_7) по ASIN <= 0. Месяц пишем, только если в нём
-- есть срезы не менее чем за HOPTED_MIN_DAYS дней, оставшиеся дни считаем пропорционально.
-- Пропущенный день срезов виден в forecast.hopted_snapshot_gaps и в логе ночного job (ERROR).
CREATE OR REPLACE VIEW `{P}.{DS}.oos_hopted_monthly` AS
WITH snap AS (
  SELECT snapshot_date AS d,
         UPPER(TRIM(JSON_VALUE(row_json, '$.string_field_1'))) AS asin,
         SUM(IFNULL(SAFE_CAST(JSON_VALUE(row_json, '$.string_field_7') AS INT64), 0)) AS ful
  FROM `{P}.mt.stock_history`
  WHERE source = 'hopted_us_native'
  GROUP BY 1, 2),
snap_valid AS (SELECT *, DATE_TRUNC(d, MONTH) AS month FROM snap WHERE REGEXP_CONTAINS(asin, r'^[A-Z0-9]{{10}}$')),
month_days AS (
  SELECT DATE_TRUNC(d, MONTH) AS month, COUNT(DISTINCT d) AS n_snap_days FROM snap GROUP BY 1)
SELECT s.asin, s.month,
       CAST(ROUND(SAFE_DIVIDE(COUNTIF(s.ful <= 0), COUNT(*)) * EXTRACT(DAY FROM LAST_DAY(s.month)), 1) AS NUMERIC) AS oos_days,
       COUNT(*) AS days_observed,
       'hopted' AS oos_source
FROM snap_valid s
JOIN month_days m ON m.month = s.month
WHERE m.n_snap_days >= {HOPTED_MIN_DAYS}
  AND s.month < DATE_TRUNC(CURRENT_DATE('Europe/Kiev'), MONTH)
GROUP BY s.asin, s.month;
