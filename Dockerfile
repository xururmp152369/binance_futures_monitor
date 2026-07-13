# ── Stage 1: Build Vue 3 frontend ────────────────────────────────────────────
FROM node:18-slim AS frontend-builder

WORKDIR /frontend

COPY web/frontend/package*.json ./
RUN npm ci

COPY web/frontend/ ./
RUN npm run build

# ── Stage 2: Python app ───────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# Install TA-Lib C library (required by Python package TA-Lib)
RUN wget -q https://sourceforge.net/projects/ta-lib/files/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib \
    && ./configure --prefix=/usr/local \
    && make \
    && make install \
    && cd /app \
    && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 10001 appuser

# Make sure runtime user can write logs under /app
RUN chown -R appuser:appuser /app
USER appuser

COPY --chown=appuser:appuser . .

# 覆蓋 Stage 1 build 出來的前端靜態檔（優先於 COPY . . 帶進來的任何舊 dist）
COPY --from=frontend-builder --chown=appuser:appuser /frontend/dist /app/web/frontend/dist

CMD ["python", "-m", "app.main"]
