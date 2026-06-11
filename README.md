# Hardware Database Diagram Pipeline

This repository now includes a TOML-first pipeline for turning a curated hardware model into:

- a normalized SQLite database of systems, subsystems, component types, fields, QC artifacts, and relationships
- Mermaid graphs for hierarchy views
- Mermaid graphs for dependency views
- Mermaid graphs for cable and interface views
- styled SVG and PDF publication diagrams

The design is intentionally TOML-first:

1. A curated spec layer in [`specs/systems`](specs/systems) defines the canonical model.
2. That model gets loaded into SQLite and rendered as diagrams and publication outputs.
3. Optional local source extraction exists only for provenance or one-time analysis.

This avoids treating draft prose as the database of record. The TOML specs are the source of truth.

## Repository Layout

```text
.
├── docs/               # design notes and planning documents
├── source-material/    # optional local-only private input documents, ignored by git
├── specs/systems/      # canonical system model in TOML
├── tools/              # CLI for DB build and rendering
├── build/              # generated outputs, ignored by git
├── .gitignore
├── Makefile
└── README.md
```

## What To Edit

Edit the model here:

- [`specs/systems`](specs/systems): canonical system definitions
- [`docs/hwdb-plan.md`](docs/hwdb-plan.md): design notes and extension plan

Do **not** edit generated outputs in:

- `build/`
- `build/diagrams/`
- `build/publish/`

Those files are regenerated from the specs.

Optional private reference documents belong in:

- [`source-material/`](source-material/README.md)

That folder is intentionally ignored by git, so the original `.docx`, `.pptx`, and `.xlsx` files are kept local only. They are not required for building the repo outputs.

## Operating Workflow

### 1. Normal build from the TOML model

```bash
python3 tools/hwdb.py build-db
python3 tools/hwdb.py publish
```

Equivalent convenience targets:

```bash
make build
make publish
make all
```

To generate the static HTML portfolio into `docs/`:

```bash
make site
```

This also writes the interactive database explorer:

- `docs/explorer.html`

The generated SVG diagrams are clickable: system, subsystem, component, and relation objects link into `docs/explorer.html` with stable `#object=...` deep links.

### 2. Day-to-day edit loop

When changing hierarchy, dependencies, cables, fields, or QC artifacts:

1. Edit the relevant TOML file in `specs/systems/`
2. Rebuild the SQLite database
3. Regenerate the published diagrams
4. Inspect the dense detector plate and the per-system diagrams

```bash
python3 tools/hwdb.py build-db
python3 tools/hwdb.py publish
```

### 3. Check what is loaded

```bash
python3 tools/hwdb.py list
sqlite3 build/hardware.db '.tables'
```

## Command Reference

### Quick Start

```bash
python3 tools/hwdb.py build-db
python3 tools/hwdb.py list
python3 tools/hwdb.py render-all
python3 tools/hwdb.py publish
```

No document extraction is required for the normal workflow.

### Optional legacy extraction

If you want local text snapshots from private DOCX, PPTX, and XLSX source files for provenance only:

```bash
python3 tools/hwdb.py extract-sources
make extract-legacy
```

### One-off graph commands

```bash
python3 tools/hwdb.py graph --system fd2_vd_overview --view overview
python3 tools/hwdb.py graph --system fd2_vd_top_crp --view hierarchy
python3 tools/hwdb.py graph --system fd2_vd_tde --view cabling
python3 tools/hwdb.py graph --system fd2_vd_ci --view dependency
```

### One-off publication graphics

Render one publishable SVG or PDF directly:

```bash
python3 tools/hwdb.py render-svg --system fd2_vd_tde --view cabling
python3 tools/hwdb.py render-svg --system fd2_vd_top_crp --view hierarchy --pdf-output build/publish/pdf/top_crp_hierarchy_manual.pdf
```

### Static HTML portfolio

Generate a GitHub Pages-compatible static site in `docs/`:

```bash
make site
```

This writes:

- `docs/index.html`
- `docs/explorer.html`
- `docs/assets/report.css`
- `docs/diagrams/*.svg`
- `docs/pdf/*.pdf`
- `docs/dot/*.dot`

## Output Map

Generated outputs land in `build/`:

- `build/hardware.db`
- `build/diagrams/*.mmd`
- `build/publish/index.html`
- `build/publish/diagrams/fd2_vd_full_dense.svg`
- `build/publish/diagrams/*.svg`
- `build/publish/pdf/*.pdf`

Optional provenance outputs, if you run the extractor:

- `build/sources/index.json`
- `build/sources/*.txt`

The main report to open in a browser is:

- `build/publish/index.html`

The dense full-system publication graphic is:

- `build/publish/diagrams/fd2_vd_full_dense.svg`
- `build/publish/pdf/fd2_vd_full_dense.pdf`

## Model Shape

The database currently stores:

- `sources`: optional extracted document records
- `systems`
- `subsystems`
- `component_types`
- `fields`
- `artifacts`
- `relations`

The seeded specs cover:

- FD2-VD detector overview
- FD2-VD Top CRP
- FD2-VD TDE
- FD2-VD Cryogenic Instrumentation
- FD2-VD HV Cathode

## How To Make Changes

### Rename vs change IDs

- Prefer keeping keys stable
- Change labels and descriptions freely
- Change a key only if you intend a real structural rename

For example:

- safe: change `name = "Top Half CRP / CRU"` to a better display label
- risky: change `key = "cru"` unless you also update all relations that reference it

### Add a component type

In a system spec:

1. Add the component under `[[components]]`
2. Assign it to a subsystem if relevant
3. Add fields and artifacts if needed
4. Add at least one relation so it appears in diagrams

### Add a dependency

Use typed relations instead of free text:

- `contains`
- `connects_to`
- `interfaces_with`
- `reads_out`
- `powers`
- `timed_by`
- `depends_on`

These relation types are what drive the plotting views.

### Add a new system

1. Copy an existing TOML file in `specs/systems/`
2. Define the `[system]` block
3. Add `[[subsystems]]`
4. Add `[[components]]`
5. Add `[[relations]]`
6. Run `build-db`
7. Run `publish`

## Reliable Replotting Rules

If you want plots to stay stable and reproducible:

- treat the TOML specs as the only editable source of truth
- avoid hand-editing SVG, PDF, Mermaid, or DOT outputs
- keep relation types consistent
- keep keys stable across edits
- add structure first, then refine labels
- use `publish` after every meaningful schema change

Graph layout is automatic, so small visual shifts can happen as the graph grows. If the dense diagrams become too unstable, the next improvement should be adding explicit layout metadata to the spec layer.

## Why This Shape

The interface documents mix:

- stable structure
- open action items
- future relationship changes by installation phase
- QC intent
- cable procedures

That makes direct document parsing useful for indexing, but not reliable enough to be the canonical schema. The spec layer is the canonical model; the documents remain the evidence.

## How To Read The Publication Outputs

The published report groups outputs by system and by view:

- `Overview`: high-level framing for a system
- `Hierarchy`: parent-child assembly structure
- `Dependencies`: cross-functional links between items
- `Cabling`: cable, fiber, timing, and interface paths

The dense detector plate is the closest output to the hierarchy PowerPoint decks in this folder. It combines the modeled systems into one publication-style graphic.

## Troubleshooting

### A component does not appear in a diagram

Usually one of these is true:

- it has no relation
- it is filtered out by the selected view
- the build was not rerun after editing

### `build-db` fails

Typical causes:

- a relation references a key that does not exist
- a component references a subsystem key that does not exist
- malformed TOML syntax

### `publish` fails

The publisher depends on Graphviz `dot`. Check:

```bash
command -v dot
```

### `extract-sources` finds nothing

This is only relevant if you intentionally run the legacy extractor. Check that the private source documents are under `source-material/`:

```bash
find source-material -maxdepth 1 -type f
```

## Current Limits

- The dense full-system plate is only as complete as the seeded specs
- Some systems in the source folder are still represented only as context, not full internal models
- The current database is type-level, not PID-level
- There is not yet a subsystem DSM matrix layer in the schema

## Next Extension Points

- Add more system specs in `specs/systems`
- Add PID-level instance tables once real part inventories are available
- Add connector and cable-end tables if you want channel-level mapping
- Add schedule/task specs if you want Mermaid Gantt output from the same database
