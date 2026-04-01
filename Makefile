.PHONY: setup data run dev clean

setup: data
	pip install -r requirements.txt

data:
	python scripts/download_data.py

run:
	uvicorn backend.main:app --host 0.0.0.0 --port 8000

dev:
	uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

clean:
	rm -f data/stations.csv data/connections.json
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
