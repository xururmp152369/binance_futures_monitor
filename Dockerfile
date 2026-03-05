FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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

CMD ["python", "-m", "app.main"]
