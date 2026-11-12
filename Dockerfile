# Sales Planner для Google Cloud Run.
# Секреты (.streamlit/secrets.toml с секцией [auth]) монтируются из Secret Manager
# в /root/.streamlit/secrets.toml — в образ не попадают.
# Доступ к BigQuery — через сервис-аккаунт, прикреплённый к сервису Cloud Run.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0"]
