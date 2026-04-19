# Local Source Material

Place private `.docx` and `.pptx` source files for analysis in this folder.

This directory is intentionally ignored by git except for this `README.md`, so the original documents are never pushed to GitHub from this repository.

Typical workflow:

```bash
python3 tools/hwdb.py extract-sources
python3 tools/hwdb.py build-db
python3 tools/hwdb.py publish
```
