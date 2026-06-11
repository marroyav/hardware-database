# Local Source Material

Place private `.docx`, `.pptx`, and `.xlsx` source files for analysis in this folder.

This directory is intentionally ignored by git except for this `README.md`, so the original documents are never pushed to GitHub from this repository.

This folder is not part of the normal build path. The repository is meant to operate from the TOML model in `specs/systems/`.

Use local source material only if you explicitly want to run the legacy extractor for provenance or one-time analysis.
EDMS conversion PDFs may also be kept here for manual review, but the extractor currently indexes only DOCX, PPTX, and XLSX files.

Optional legacy command:

```bash
python3 tools/hwdb.py extract-sources
```
