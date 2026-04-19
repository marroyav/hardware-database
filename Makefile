.PHONY: extract build publish list clean

extract:
	python3 tools/hwdb.py extract-sources

build:
	python3 tools/hwdb.py build-db

publish:
	python3 tools/hwdb.py publish

list:
	python3 tools/hwdb.py list

clean:
	rm -rf build
