import streamlit as st
from google.cloud import bigquery
import pandas as pd
import plotly.express as px

st.set_page_config(page_title="Demand Planning", layout="wide")
client = bigquery.Client(project="reorder-497714")

# 1. Загрузка данных из нашей витрины
@st.cache_data(ttl=5) # Кэш на 5 сек, чтобы сразу видеть обновленный план
def load_data():
    query = """
    SELECT group_key, month, fact_units, ml_forecast_units, manual_plan_units, final_demand_plan 
    FROM `reorder-497714.forecast.looker_demand_plan`
    ORDER BY month ASC
    """
    return client.query(query).to_dataframe()

df = load_data()

# 2. Фильтр по группе товаров
groups = df['group_key'].dropna().unique()
selected_group = st.selectbox("Выберите SKU / Группу:", groups)

df_filtered = df[df['group_key'] == selected_group].copy()

# 3. Визуализация (График прямо в Streamlit вместо Looker)
st.subheader(f"Прогноз и Факт: {selected_group}")

fig = px.line(df_filtered, x='month', y=['fact_units', 'final_demand_plan'],
              labels={'value': 'Штуки', 'month': 'Месяц', 'variable': 'Показатель'},
              color_discrete_map={'fact_units': 'blue', 'final_demand_plan': 'orange'})
st.plotly_chart(fig, use_container_width=True)

# 4. Сетка ввода плана
st.subheader("Редактирование плана продаж")
edited_df = st.data_editor(
    df_filtered[['month', 'ml_forecast_units', 'manual_plan_units']],
    column_config={
        "month": st.column_config.DateColumn("Месяц", disabled=True),
        "ml_forecast_units": st.column_config.NumberColumn("ML Прогноз", disabled=True),
        "manual_plan_units": st.column_config.NumberColumn("Ручной План (Правка)", min_value=0),
    },
    hide_index=True,
    use_container_width=True
)

# 5. Кнопка сохранения в BigQuery
if st.button("Сохранить план в BigQuery"):
    # Берем только измененные строки
    changes = edited_df[edited_df['manual_plan_units'].notnull()].copy()
    
    if not changes.empty:
        changes['group_key'] = selected_group
        changes['channel'] = 'US' # Дефолтный канал
        changes['plan_units'] = changes['manual_plan_units'].astype(int)
        changes['author'] = st.experimental_user.email if hasattr(st, 'experimental_user') else 'manager'
        changes['updated_at'] = pd.Timestamp.now(tz='UTC')
        
        # Оставляем только нужные колонки для таблицы plan_monthly
        to_bq = changes[['group_key', 'channel', 'month', 'plan_units', 'author', 'updated_at']]
        
        job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
        job = client.load_table_from_dataframe(to_bq, "reorder-497714.forecast.plan_monthly", job_config=job_config)
        job.result()
        
        st.success("План успешно сохранен!")
        st.cache_data.clear() # Сбрасываем кэш для моментального обновления графика
    else:
        st.info("Нет внесенных изменений для сохранения.")
