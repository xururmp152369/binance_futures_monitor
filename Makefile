.PHONY: help up down restart logs logs-cf run api dev build test backtest venv
.DEFAULT_GOAL := help

# ── Docker ────────────────────────────────────────────────────────────────────
up:
	docker compose up -d --build --force-recreate

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

logs-cf:
	docker compose logs cloudflared

# ── Backend ───────────────────────────────────────────────────────────────────
run:
	python -m app.main

api:
	python -m uvicorn web.api.app:create_web_app --factory --port 8000

# ── Frontend ──────────────────────────────────────────────────────────────────
dev:
	cd web/frontend && npm run dev

build:
	cd web/frontend && npm run build

# ── Virtual Environment ───────────────────────────────────────────────────────
venv:
	@test -d .venv || python -m venv .venv
	@echo "Entering venv shell..."
	@echo "  deactivate  turn off venv (stay in shell)"
	@echo "  exit        leave this shell entirely"
	@bash --rcfile .venv/Scripts/activate

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	python -m pytest tests/ -v --ignore=tests/test_ws_diag.py

# ── Backtest ──────────────────────────────────────────────────────────────────
# Variables (all optional):
#   STRATEGY  long_breakout | death_cross_short | fibonacci_long | fibonacci_short | all  (default: all)
#   DAYS      lookback days (default: 30)
#   START     start date e.g. 2025-06-04
#   END       end date   e.g. 2026-06-04
#   ACCOUNT   account name (enables P&L mode)
#   SYMBOLS   comma-separated symbols e.g. BTCUSDT,ETHUSDT
#   NO_CACHE  set to 1 to force re-download

STRATEGY ?= all
DAYS     ?=
START    ?=
END      ?=
ACCOUNT  ?=
SYMBOLS  ?=
NO_CACHE ?=

backtest:
	python backtest/run.py --strategy $(STRATEGY) \
		$(if $(DAYS),--days $(DAYS)) \
		$(if $(START),--start $(START)) \
		$(if $(END),--end $(END)) \
		$(if $(ACCOUNT),--account $(ACCOUNT)) \
		$(if $(SYMBOLS),--symbols $(SYMBOLS)) \
		$(if $(NO_CACHE),--no-cache)

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "Usage: make <target> [VARIABLE=value ...]"
	@echo ""
	@echo "Docker"
	@echo "  up          build + background start (--build --force-recreate)"
	@echo "  down        stop all containers"
	@echo "  restart     restart containers"
	@echo "  logs        tail all container logs"
	@echo "  logs-cf     tail cloudflared log (find Cloudflare Tunnel URL)"
	@echo ""
	@echo "Dev"
	@echo "  venv        create .venv (if not exists) and enter venv shell"
	@echo "  run         python -m app.main  (full bot + web :8000)"
	@echo "  api         FastAPI only, no Telegram/Binance keys needed :8000"
	@echo "  dev         frontend Vite dev server :5173 (proxy -> :8000)"
	@echo "  build       npm run build -> web/frontend/dist/"
	@echo ""
	@echo "Test & Backtest"
	@echo "  test        pytest tests/ (excludes test_ws_diag.py)"
	@echo "  backtest    run backtest  (default: STRATEGY=all, last 30 days)"
	@echo ""
	@echo "Backtest variables:"
	@echo "  STRATEGY    long_breakout | death_cross_short | fibonacci_long | fibonacci_short | all"
	@echo "  DAYS        lookback days (default 30)    e.g. DAYS=60"
	@echo "  START       start date                    e.g. START=2025-06-04"
	@echo "  END         end date (default today)      e.g. END=2026-06-04"
	@echo "  ACCOUNT     account name for P&L mode     e.g. ACCOUNT=myaccount"
	@echo "  SYMBOLS     comma-separated symbols       e.g. SYMBOLS=BTCUSDT,ETHUSDT"
	@echo "  NO_CACHE    set 1 to skip cache           e.g. NO_CACHE=1"
	@echo ""
	@echo "Examples:"
	@echo "  make backtest STRATEGY=long_breakout"
	@echo "  make backtest DAYS=60"
	@echo "  make backtest START=2025-06-04 END=2026-06-04"
	@echo "  make backtest STRATEGY=all ACCOUNT=myaccount"
	@echo "  make backtest START=2025-06-04 SYMBOLS=BTCUSDT,ETHUSDT NO_CACHE=1"
	@echo ""
