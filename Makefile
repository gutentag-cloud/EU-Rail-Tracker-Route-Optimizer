.PHONY: setup data run dev docker-up docker-down \
        migrate clean test lint

# ── Local Development ─────────────────────────────
setup: data
	pip install -r requirements.txt

data:
	python scripts/download_data.py

run:
	uvicorn backend.main:app --host 0.0.0.0 --port 8000

dev:
	uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# ── Docker ────────────────────────────────────────
docker-up:
	docker-compose up -d --build

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f app

# ── Database ──────────────────────────────────────
migrate:
	docker-compose exec db \
		psql -U rail -d railtracker \
		-f /migrations/001_initial.sql

# ── Quality ───────────────────────────────────────
test:
	pytest tests/ -v --tb=short

lint:
	ruff check backend/
	ruff format --check backend/

format:
	ruff format backend/

# ── Cleanup ───────────────────────────────────────
clean:
	rm -f data/stations.csv data/connections.json
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	docker-compose down -v 2>/dev/null || true
