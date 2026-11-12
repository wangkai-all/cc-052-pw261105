.PHONY: install test backend frontend build dev demo

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
	cd frontend && npm ci

test:
	cd backend && .venv/bin/python -m pytest -q
	cd frontend && npm test

build:
	cd frontend && npm run build

backend:
	cd backend && .venv/bin/python run.py

frontend:
	cd frontend && npm run dev

demo:
	./scripts/crash_demo.sh after_move
