-- forecast.oos_monthly: единственный вход OOS в расчёт (читает его fact_demand_raw).
-- Выход прежний: asin, month, oos_days; добавлены oos_source (sheet|ledger|hopted) и days_observed.
--   месяцы до {CUTOVER}: замороженный лист (история не меняется ни на день);
--   с {CUTOVER}: ledger, а для ASIN вне ledger срезы Hopted. Нет источника = нет строки (спрос = продажи).
CREATE OR REPLACE VIEW `{P}.{DS}.oos_monthly` AS
WITH sheet AS (
  SELECT asin, month, oos_days, CAST(NULL AS INT64) AS days_observed, 'sheet' AS oos_source
  FROM `{P}.{DS}.oos_sheet_monthly` WHERE month < DATE '{CUTOVER}'),
led AS (SELECT * FROM `{P}.{DS}.oos_ledger_monthly` WHERE month >= DATE '{CUTOVER}'),
hop AS (
  SELECT h.* FROM `{P}.{DS}.oos_hopted_monthly` h
  LEFT JOIN led l ON l.asin = h.asin AND l.month = h.month
  WHERE h.month >= DATE '{CUTOVER}' AND l.asin IS NULL)
SELECT * FROM sheet
UNION ALL SELECT asin, month, oos_days, days_observed, oos_source FROM led
UNION ALL SELECT asin, month, oos_days, days_observed, oos_source FROM hop;
