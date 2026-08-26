FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Almaty

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY swingscan ./swingscan
COPY config ./config
COPY market_update.py pyproject.toml README.md ./

RUN useradd --create-home --uid 10001 swing \
 && mkdir -p /app/state /app/reports /app/cache \
 && chown -R swing:swing /app
USER swing

VOLUME ["/app/state", "/app/reports", "/app/cache"]

# Планировщик: сканы в 09:00 и 21:00 по Астане.
# Маркет апдейт запускается отдельно (GitHub Actions или cron хоста):
#   docker compose run --rm swingscan python market_update.py
CMD ["python", "-m", "swingscan", "serve"]
