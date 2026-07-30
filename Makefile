.PHONY: build publish site list clean extract-legacy all daphne-staging daphne-validate daphne-export daphne-test production-migrate production-validate production-test

all: build publish

extract-legacy:
	python3 tools/hwdb.py extract-sources

build:
	python3 tools/hwdb.py build-db

publish:
	python3 tools/hwdb.py publish

site:
	python3 tools/hwdb.py build-db
	python3 tools/hwdb.py publish --output-dir docs

list:
	python3 tools/hwdb.py list

clean:
	rm -rf build

daphne-staging:
	python3 tools/daphne_staging.py init

daphne-validate:
	python3 tools/daphne_staging.py validate

daphne-export:
	python3 tools/daphne_staging.py export-hwdb

daphne-test:
	python3 -m unittest discover -s tests -p 'test_daphne_staging.py' -v

production-migrate:
	python3 tools/daphne_production_cli.py migrate

production-validate:
	python3 tools/daphne_production_cli.py validate

production-test:
	python3 -m unittest discover -s tests -p 'test_daphne_production.py' -v
