-- Откат материализации OOS: процедура как была, oos_monthly снова представление.
BEGIN
  EXECUTE IMMEDIATE (
    SELECT REPLACE(ddl, 'CREATE PROCEDURE', 'CREATE OR REPLACE PROCEDURE')
    FROM `reorder-497714.forecast.sp_refresh_gold_ddl_backup_1124`);
END;
DROP TABLE `reorder-497714.forecast.oos_monthly`;
CREATE VIEW `reorder-497714.forecast.oos_monthly` AS
SELECT * FROM `reorder-497714.forecast.oos_monthly_live`;
