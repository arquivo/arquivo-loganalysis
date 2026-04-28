FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY gunicorn.conf.py .

ENV LOG_DIR=/app/httpd
ENV DB_PATH=/app/data/analytics.duckdb
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.app:app"]
