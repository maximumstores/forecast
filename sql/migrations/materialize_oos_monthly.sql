-- Материализация OOS: forecast.oos_monthly становится таблицей, которую sp_refresh_gold пересобирает
-- первым шагом из представления forecast.oos_monthly_live.
-- Порядок в проде (не в 06:30–07:00 Киев, когда идёт ночной запуск):
--   1) python tools/oos_sql.py --ds forecast --cutover 2026-11-01   -> выполнить (создаст oos_monthly_live)
--   2) выполнить этот файл целиком (скрипт BigQuery)
--   3) один ручной запуск CALL forecast.sp_refresh_gold() и сверка байт по INFORMATION_SCHEMA.JOBS
-- Откат: sql/migrations/rollback_materialize_oos_monthly.sql

-- копия текущего определения процедуры (для отката)
CREATE OR REPLACE TABLE `reorder-497714.forecast.sp_refresh_gold_ddl_backup_1124` AS
SELECT CURRENT_TIMESTAMP() AS saved_at, ddl
FROM `reorder-497714.forecast.INFORMATION_SCHEMA.ROUTINES` WHERE routine_name = 'sp_refresh_gold';

-- представление -> таблица (то же имя, читатели не меняются)
DROP VIEW `reorder-497714.forecast.oos_monthly`;
CREATE TABLE `reorder-497714.forecast.oos_monthly` AS
SELECT * FROM `reorder-497714.forecast.oos_monthly_live`;

-- первым шагом процедуры: пересобрать таблицу
BEGIN
  DECLARE d STRING;
  DECLARE p INT64;
  SET d = (SELECT ddl FROM `reorder-497714.forecast.sp_refresh_gold_ddl_backup_1124`);
  SET p = STRPOS(d, '\nBEGIN\n');
  IF p = 0 THEN RAISE USING MESSAGE = 'sp_refresh_gold: не нашёл BEGIN, процедура не изменена'; END IF;
  IF STRPOS(d, 'oos_monthly_live') = 0 THEN
    SET d = CONCAT(
      REPLACE(SUBSTR(d, 1, p + 6), 'CREATE PROCEDURE', 'CREATE OR REPLACE PROCEDURE'),
      '-- ---- 00_oos_materialize: OOS читают много раз за прогон, считаем один раз ----\n',
      'CREATE OR REPLACE TABLE `reorder-497714.forecast.oos_monthly` AS\n',
      'SELECT * FROM `reorder-497714.forecast.oos_monthly_live`;\n\n',
      SUBSTR(d, p + 7));
    EXECUTE IMMEDIATE d;
  END IF;
END;
