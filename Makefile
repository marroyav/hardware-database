.PHONY: build publish site list clean extract-legacy all

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
