.PHONY: help dev build up down logs migrate downgrade revision test lint fmt typecheck check shell backup

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

up:            ## Поднять всё локально
	docker compose up -d --build
down:          ## Остановить всё
	docker compose down
logs:          ## Смотреть логи бота
	docker compose logs -f bot
build:         ## Только собрать образ
	docker compose build

migrate:       ## Применить миграции
	docker compose run --rm bot alembic upgrade head
downgrade:     ## Откатить последнюю миграцию
	docker compose run --rm bot alembic downgrade -1
revision:      ## Новая миграция: make revision M="что меняем"
	docker compose run --rm bot alembic revision --autogenerate -m "$(M)"

test:          ## Тесты
	pytest
lint:          ## Линтер
	ruff check bot tests
fmt:           ## Форматирование
	black bot tests && ruff check --fix bot tests
typecheck:     ## Проверка типов
	mypy bot
check: lint typecheck test  ## Всё сразу

shell:         ## Консоль внутри контейнера бота
	docker compose exec bot bash
backup:        ## Дамп базы вручную
	docker compose exec postgres /backups/../scripts/backup.sh || ./scripts/backup.sh
