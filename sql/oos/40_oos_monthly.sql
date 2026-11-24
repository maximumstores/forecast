-- forecast.oos_monthly_live: представление OOS. В расчёт идёт не оно, а таблица forecast.oos_monthly,
-- которую sp_refresh_gold пересобирает из этого представления первым шагом (читать его на каждом
-- обращении к fact_demand_raw дорого: каждый раз скан леджера и срезов Hopted, ~2,8 ГБ).
-- Выход: asin, month, oos_days; oos_source (sheet|ledger|hopted) и days_observed.
-- Выход прежний: asin, month, oos_days; добавлены oos_source (sheet|ledger|hopted) и days_observed.
--   месяцы до {CUTOVER}: замороженный лист (история не меняется ни на день);
--   с {CUTOVER}: ledger, а для ASIN вне ledger срезы Hopted. Нет источника = нет строки (спрос = продажи).
CREATE OR REPLACE VIEW `{P}.{DS}.oos_monthly_live` AS
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
