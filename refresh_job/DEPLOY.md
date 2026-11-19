# Выкат и откат job `forecast-refresh`

Регион europe-north1, сервис-аккаунт streamlit-planner@reorder-497714.iam.gserviceaccount.com.

## Перед выкатом: запомнить текущий образ (для отката)
    PREV_IMAGE=$(gcloud run jobs describe forecast-refresh --region europe-north1 \
      --format='value(spec.template.spec.template.spec.containers[0].image)')
    echo "$PREV_IMAGE"        # сохранить строку: это образ с датой и хешем

## Выкат
    git pull && gcloud run jobs deploy forecast-refresh --source refresh_job --region europe-north1 \
      --set-secrets TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest

## Откат одной командой (на прежний образ)
    gcloud run jobs update forecast-refresh --region europe-north1 --image "$PREV_IMAGE"

Откат меняет только образ job, данные в BigQuery не трогает. Если строка PREV_IMAGE
потеряна, образы лежат в Artifact Registry (репозиторий cloud-run-source-deploy,
образ forecast-refresh): `gcloud artifacts docker images list` и подставить предыдущий.

## Проверка свежести (отдельный запуск того же образа)
    gcloud run jobs create forecast-refresh-fresh --image "$IMAGE" --region europe-north1 \
      --command python --args refresh.py,--check-fresh --set-secrets ...те же секреты
    gcloud scheduler jobs create http forecast-refresh-fresh-0430 --schedule "30 4 * * *" \
      --time-zone UTC --uri https://europe-north1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/reorder-497714/jobs/forecast-refresh-fresh:run \
      --http-method POST --oauth-service-account-email streamlit-planner@reorder-497714.iam.gserviceaccount.com

Проверка в 04:30 UTC смотрит, что каждая из ключевых таблиц обновлена не раньше 03:00 UTC того же дня.
