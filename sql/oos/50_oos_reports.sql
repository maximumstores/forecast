-- Контроль источников OOS: какая доля продаж США покрыта каждым источником, и пропущенные дни срезов Hopted.
CREATE OR REPLACE VIEW `{P}.{DS}.oos_source_report` AS
WITH s AS (SELECT TRIM(asin) AS asin, month, units FROM `{P}.forecast.fact_sales_monthly` WHERE units > 0),
o AS (SELECT asin, month, oos_source FROM `{P}.{DS}.oos_monthly`)
SELECT s.month,
       IFNULL(o.oos_source, 'нет источника') AS oos_source,
       COUNT(*) AS asins,
       ROUND(SUM(s.units)) AS units,
       ROUND(SAFE_DIVIDE(SUM(s.units), SUM(SUM(s.units)) OVER (PARTITION BY s.month)) * 100, 1) AS units_pct
FROM s LEFT JOIN o ON o.asin = s.asin AND o.month = s.month
GROUP BY s.month, oos_source;

CREATE OR REPLACE VIEW `{P}.{DS}.hopted_snapshot_gaps` AS
WITH snap_days AS (SELECT DISTINCT snapshot_date AS dt FROM `{P}.mt.stock_history` WHERE source = 'hopted_us_native'),
rng AS (SELECT day FROM UNNEST(GENERATE_DATE_ARRAY((SELECT MIN(dt) FROM snap_days), CURRENT_DATE('Europe/Kiev'))) AS day)
SELECT day AS missing_day FROM rng WHERE day NOT IN (SELECT dt FROM snap_days);
