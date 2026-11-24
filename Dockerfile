FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 数据文件默认放 /data/ledger.db（由 compose 挂卷），密钥经 APP_SECRET_KEY 注入
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "run:app"]
