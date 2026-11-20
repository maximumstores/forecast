# Выкат и откат job `forecast-refresh`

Регион europe-north1, сервис-аккаунт streamlit-planner@reorder-497714.iam.gserviceaccount.com.

## Перед выкатом: запомнить текущий образ (для отката)
    PREV_IMAGE=$(gcloud run jobs describe forecast-refresh --region europe-north1 \
      --format='value(spec.template.spec.template.spec.containers[0].image)')
    echo "$PREV_IMAGE"        # сохранить строку: это образ с датой и хешем

## Выкат
Таймаут 3600 с, повторов 0: второй запуск читает ту же сломанную вкладку и только
тратит время; о сбое сообщает код завершения, лог и (если настроен) Telegram.

    git pull && gcloud run jobs deploy forecast-refresh --source refresh_job --region europe-north1 \
      --task-timeout 3600 --max-retries 0

Telegram необязателен. Без секретов job пишет в лог «Telegram не настроен» и работает как обычно.
Чтобы включить, когда секреты будут созданы (имена подставить свои):

    gcloud run jobs update forecast-refresh --region europe-north1 \
      --set-secrets TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest

Только поменять настройки на текущем образе, без выката кода:

    gcloud run jobs update forecast-refresh --region europe-north1 --task-timeout 3600 --max-retries 0

## Откат одной командой (на прежний образ)
    gcloud run jobs update forecast-refresh --region europe-north1 --image "$PREV_IMAGE"

Откат меняет только образ job, данные в BigQuery не трогает. Если строка PREV_IMAGE
потеряна, образы лежат в Artifact Registry (репозиторий cloud-run-source-deploy,
образ forecast-refresh): `gcloud artifacts docker images list` и подставить предыдущий.

## Проверка свежести (отдельный запуск того же образа)
    gcloud run jobs create forecast-refresh-fresh --image "$IMAGE" --region europe-north1 \
      --command python --args refresh.py,--check-fresh --task-timeout 600 --max-retries 0
    gcloud scheduler jobs create http forecast-refresh-fresh-0830 --schedule "30 8 * * *" \
      --time-zone Europe/Kiev --uri https://europe-north1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/reorder-497714/jobs/forecast-refresh-fresh:run \
      --http-method POST --oauth-service-account-email streamlit-planner@reorder-497714.iam.gserviceaccount.com

Проверка в 08:30 по Киеву смотрит, что каждая из ключевых таблиц обновлена не раньше 03:00 UTC того же дня.

## Если упало на проверке ключевой колонки
Сообщение называет таблицу, колонку, число строк и колонки с #REF!. Это ошибка в самой
таблице Google (сломаны формулы), чинит владелец файла. Рабочая таблица при этом не
перезаписывается: после правки в листе следующий запуск подхватит данные сам.
## OOS из леджера DataDoe (с ноября 2026)
Вкладка «OOS amazon» больше не загружается, `mt.sales_oos_amz_native` не читается расчётом
(таблица остаётся как архив). `forecast.oos_monthly` собирается из:
- копии листа от 17.11 (`forecast.oos_sheet_snapshot_1117`, неизменяемая) — месяцы до границы;
- леджера DataDoe (`datadoe_copy.fba_ledger_summary`) — месяцы с границы;
- ежедневных срезов Hopted US (`mt.stock_history`, `oos_source = 'hopted'`) — ASIN, которых нет в леджере.

Каждую ночь после загрузки задание проверяет и пишет ERROR в журнал и в Telegram (запуск
завершается с ошибкой, пересборка при этом идёт): нет среза Hopted US за сегодня или два
предыдущих дня; леджер пуст или отстаёт больше чем на 8 дней (`LEDGER_MAX_LAG_DAYS`).
Пропущенный день Hopted не восстановить: он виден в `forecast.hopted_snapshot_gaps`.
Доля единиц по источникам — `forecast.oos_source_report`.

Создание представлений (один раз, после «ок» на сверку): `python tools/oos_sql.py --ds forecast --cutover 2026-11-01`
и выполнить полученный SQL по файлам по порядку (10→50). Для проверки на копии: `--ds forecast_shadow --cutover 2026-09-01`.

## Расписание после перехода
Расписание (Cloud Scheduler) лежит в europe-west1, сами задания Cloud Run — в europe-north1. Запуск задания: 06:30 Киев (DataDoe приходит в 05:30–06:00). Проверку свежести сдвинуть на 08:30 Киев,
иначе она сработает до окончания загрузки. `FRESH_SINCE_UTC` оставить 03:00 (старт 06:30 Киев — это
03:30–04:30 UTC в зависимости от сезона).
