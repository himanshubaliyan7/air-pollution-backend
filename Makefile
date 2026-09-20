COMPOSE = docker compose -f docker/docker-compose.yml --env-file docker/.env

.PHONY: up down migrate logs ps test

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

migrate:
	$(COMPOSE) up --build db-migrate

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

test:
	pytest -q
