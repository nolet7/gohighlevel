.PHONY: install lint test cov demo run docker probe-fp probe-ghl

install:
	pip install -e ".[dev]"

lint:
	ruff check src tests scripts

test:
	python -m pytest

cov:
	python -m pytest --cov=hvac_engine --cov-report=term-missing --cov-fail-under=90

demo:
	python -m hvac_engine.demo

run:
	uvicorn hvac_engine.main:app --reload

docker:
	docker compose up --build

probe-fp:
	python scripts/fieldpulse_probe.py

probe-ghl:
	python scripts/ghl_probe.py
