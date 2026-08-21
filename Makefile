.PHONY: help install install-dev test coverage scan serve schedule telegram-test docker-build docker-up clean

help:
	@echo "make install       — установить зависимости"
	@echo "make install-dev   — установить зависимости для разработки"
	@echo "make test          — прогнать тесты"
	@echo "make coverage      — тесты с отчётом покрытия"
	@echo "make scan          — разовое сканирование (без отправки: make scan ARGS=--dry-run)"
	@echo "make serve         — запустить планировщик 09:00 / 21:00"
	@echo "make schedule      — показать ближайшие запуски"
	@echo "make telegram-test — проверить связь с Telegram-ботом"
	@echo "make docker-up     — поднять сервис в Docker"

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

test:
	python -m pytest

coverage:
	python -m pytest --cov=swingscan --cov-report=term-missing

scan:
	python -m swingscan scan $(ARGS)

serve:
	python -m swingscan serve $(ARGS)

schedule:
	python -m swingscan schedule

telegram-test:
	python -m swingscan telegram-test

docker-build:
	docker compose build

docker-up:
	docker compose up -d

clean:
	rm -rf cache reports .pytest_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
