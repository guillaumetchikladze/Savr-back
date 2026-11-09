# Savr-back — lint & tests
#
#   make test-db            # Postgres+pgvector éphémère (docker, port 55432)
#   make test               # suite rapide (exclut @slow) ; N=auto pour paralléliser (xdist)
#   make test-all           # tout, y compris @slow
#   make test-domain d=shopping   # un domaine : auth mealplan posts shopping dietary chat imports recipes appversion stats notifications tables
#   make test-changed       # tests liés aux fichiers modifiés (git diff vs BASE, défaut origin/main)
#   make lint / make fmt
#
# Variables : PY (python du venv), N (workers xdist), BASE (ref git pour test-changed), ARGS (args pytest).

PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)
# Workers xdist. Mesuré (118 tests, M-series 10 cœurs) : -n 0 ≈ 13 s, -n 4 ≈ 21 s, -n auto ≈ 30 s :
# le coût dominant est l'import Django+libs IA (~5-10 s) répété par worker. Série par défaut tant que
# la suite reste petite ; passer à N=auto (ou N=4) quand le temps « tests » dépasse ~30 s.
N ?= 0
BASE ?= origin/main
ARGS ?=

export TEST_DB_HOST ?= localhost
export TEST_DB_PORT ?= 55432
export TEST_DB_USER ?= postgres
export TEST_DB_PASSWORD ?= postgres
export TEST_DB_NAME ?= savr_test

PYTEST = $(PY) -m pytest -n $(N)

.PHONY: help test test-all test-domain test-changed test-fresh test-db test-db-down lint fmt check

help:
	@sed -n '2,11p' Makefile

test:
	$(PYTEST) -q $(ARGS)

test-all:
	$(PYTEST) -q -m "" $(ARGS)

test-domain:
	@test -n "$(d)" || (echo "usage: make test-domain d=<auth|mealplan|posts|shopping|dietary|chat|imports|recipes|appversion|stats|notifications|tables>" && exit 2)
	$(PYTEST) -q -m "$(d) and not slow" $(ARGS)

# Tests liés aux fichiers modifiés vs $(BASE) + working tree (best effort, cf. scripts/changed_tests.sh).
test-changed:
	@targets="$$(bash scripts/changed_tests.sh $(BASE))" || exit 1; \
	if [ -z "$$targets" ]; then echo "Aucun test lié aux changements."; exit 0; fi; \
	if [ "$$targets" = "ALL" ]; then targets=""; fi; \
	echo "pytest $${targets:-<toute la suite>}" | tr '\n' ' '; echo; \
	$(PYTEST) -q $$targets $(ARGS)

# Recrée les bases de test (après modification d'une migration existante ; les nouvelles
# migrations sont appliquées automatiquement même avec --reuse-db).
test-fresh:
	$(PYTEST) -q --create-db $(ARGS)

test-db:
	docker compose -f docker-compose.test.yml up -d --wait

test-db-down:
	docker compose -f docker-compose.test.yml down

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

check: lint test
