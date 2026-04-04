.DEFAULT_GOAL := dev

.PHONY: dev test build clean

dev:
	docker compose up --build

test:
	docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm api

build:
	docker compose build

clean:
	docker compose -f docker-compose.yml -f docker-compose.test.yml down -v --remove-orphans
