-- OOS из замороженного снимка листа Нины (17.11.2026). Месяцы до {CUTOVER} берём только отсюда.
-- Логика разбора та же, что была в forecast.oos_monthly: колонка string_field_N = месяц от апр 2023.
CREATE OR REPLACE VIEW `{P}.{DS}.oos_sheet_monthly` AS
WITH long AS (
  SELECT TRIM(string_field_0) AS asin, col, oos_str
  FROM `{P}.forecast.oos_sheet_snapshot_1117`
  UNPIVOT(oos_str FOR col IN (string_field_7,string_field_8,string_field_9,string_field_10,string_field_11,string_field_12,string_field_13,string_field_14,string_field_15,string_field_16,string_field_17,string_field_18,string_field_19,string_field_20,string_field_21,string_field_22,string_field_23,string_field_24,string_field_25,string_field_26,string_field_27,string_field_28,string_field_29,string_field_30,string_field_31,string_field_32,string_field_33,string_field_34,string_field_35,string_field_36,string_field_37,string_field_38,string_field_39,string_field_40,string_field_41,string_field_42,string_field_43,string_field_44,string_field_45,string_field_46,string_field_47,string_field_48,string_field_49,string_field_50,string_field_51,string_field_52,string_field_53,string_field_54))
  WHERE string_field_0 IS NOT NULL AND TRIM(string_field_0) NOT IN ("","ASIN") AND LOWER(TRIM(string_field_0)) != "new")
SELECT asin,
       DATE_ADD(DATE "2023-04-01", INTERVAL (CAST(REGEXP_EXTRACT(col, r"(\d+)$") AS INT64) - 7) MONTH) AS month,
       SAFE_CAST(NULLIF(TRIM(oos_str), "") AS NUMERIC) AS oos_days
FROM long
WHERE SAFE_CAST(NULLIF(TRIM(oos_str), "") AS NUMERIC) IS NOT NULL;
