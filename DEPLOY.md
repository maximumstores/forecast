# Деплой Sales Planner в Google Cloud Run

Приложение то же, что на Streamlit Cloud, меняется только место запуска.
Проект GCP: `reorder-497714`, регион: `europe-west1`.

## Один раз: настройка

1. **APIs** (Console → APIs & Services → Enable): Cloud Run, Cloud Build, Artifact Registry, Secret Manager.

2. **Секреты** (Security → Secret Manager → Create secret):
   - имя `forecast-streamlit-secrets`;
   - значение — содержимое текущих Secrets из Streamlit Cloud,
     **но без секции `[gcp_service_account]`** (на Cloud Run ключ не нужен);
   - в секции `[auth]` заменить `redirect_uri` на новый адрес:
     `https://forecast.maximumstores.online/oauth2callback`
     (пока нет домена — адрес сервиса `https://forecast-…run.app/oauth2callback`).

3. **Права сервис-аккаунта** `streamlit-planner@reorder-497714.iam.gserviceaccount.com`
   (IAM): уже есть `BigQuery Job User` + `BigQuery Data Editor`; добавить
   `Secret Manager Secret Accessor`.

4. **OAuth-клиент** (APIs & Services → Credentials → клиент из `[auth]`):
   в Authorized redirect URIs добавить новый `redirect_uri` из шага 2.
   Старый (streamlit.app) не удалять до отключения старого адреса.

## Создание сервиса

Cloud Run → Create service → **Continuously deploy from a repository** →
Set up with Cloud Build → GitHub `maximumstores/forecast`, ветка `main`,
Build type: **Dockerfile** (`/Dockerfile`).

Настройки:
- Service name: `forecast`, Region: `europe-west1`
- Authentication: **Allow unauthenticated** (вход проверяет само приложение —
  Google-логин, только `@maximumstores.online`)
- Container → Port `8080`; Memory `1 GiB`; CPU `1`
- Request timeout `3600` (Streamlit держит websocket)
- Minimum instances `1` (не засыпает; можно `0` — дешевле, первый вход 5–10 с)
- **Session affinity: On**
- Security → Service account: `streamlit-planner@reorder-497714.iam.gserviceaccount.com`
- Volumes → Add volume → Secret `forecast-streamlit-secrets`, version `latest`;
  Volume mounts → mount path `/root/.streamlit`, файл `secrets.toml`.

После этого каждый push в `main` сам пересобирает и выкладывает приложение.

## Домен

Cloud Run → сервис → Integrations / Manage custom domains →
`forecast.maximumstores.online` → Google покажет DNS-запись (CNAME
`ghs.googlehosted.com.`), её прописывает Макс. Сертификат выдаётся сам.

## Переезд

1. Неделю работают оба адреса.
2. Проверить: вход, все вкладки, сохранение правок (override, target stock,
   кривые) — в `forecast.login_log` и таблицах правок появляются строки.
3. Отключить приложение на Streamlit Cloud, удалить старый redirect URI.
