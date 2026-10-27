.PHONY: up down test eval

up:
	docker compose up --build

down:
	docker compose down

test:
	cd backend && pytest

eval:
	cd backend && python -m evaluation.evaluate --document "$(DOCUMENT)"
