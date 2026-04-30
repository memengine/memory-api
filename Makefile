.DEFAULT_GOAL := dev

.PHONY: dev test build clean migrate migrate-current

dev:
	docker compose up --build

test:
	docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm api

build:
	docker compose build

migrate:
	docker compose exec api alembic -c /app/alembic.ini upgrade head

migrate-current:
	docker compose exec api alembic -c /app/alembic.ini current

clean:
	docker compose -f docker-compose.yml -f docker-compose.test.yml down -v --remove-orphans
