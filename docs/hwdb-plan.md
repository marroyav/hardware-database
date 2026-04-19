# HWDB Database And Diagram Plan

## Goal

Build one maintainable source of truth that can answer three different needs from the same model:

- hierarchy graphs for assemblies and subassemblies
- dependency and interface graphs across systems
- cable and readout path graphs for installation and mapping work

The interface documents in this folder already define the right raw ingredients:

- system and subsystem names
- component types
- QC-bearing fields and files
- parent-child structure
- cable procedures
- lifecycle changes during production, shipment, assembly, and installation

## Recommended Architecture

### 1. Source layer

Keep the original EDMS-derived `.docx` and hierarchy `.pptx` files untouched.

Use the extractor to generate:

- plain text snapshots for review
- a source index with titles, timestamps, and extracted text paths

This gives traceability and makes later audits possible.

### 2. Curated spec layer

Maintain one TOML file per system in `specs/systems`.

Each spec defines:

- the system
- its subsystems
- component types
- tracked fields
- QC artifacts
- typed relations

This is the canonical layer because the documents are still partly draft-like and contain open questions.

### 3. SQLite model

Load the curated specs into SQLite so the same structured data can drive:

- reports
- exports
- graph generation
- later dashboard work

The current schema is enough for design-level structure:

- `systems`
- `subsystems`
- `component_types`
- `fields`
- `artifacts`
- `relations`

The next schema expansion should add instance-level operational data:

- `parts`
- `part_fields`
- `qc_results`
- `files`
- `connectors`
- `cables`
- `cable_ends`
- `shipments`
- `locations`
- `tasks`

## Diagram Strategy

### Hierarchy

Use `contains` edges to render assembly trees such as:

- Top CRP -> CRU -> composite frame -> spacers
- uTCA crate -> AMC / MCH / WR-MCH / PSU
- Cathode plane -> half cathode plane -> frame / meshes / LAD

### Dependency

Use typed non-hierarchical edges such as:

- `interfaces_with`
- `reads_out`
- `powers`
- `timed_by`
- `depends_on`

This supports system-level reasoning and installation planning.

### Cabling

Use `connects_to` edges and cable/fiber component categories to render:

- signal paths
- power paths
- timing paths
- flange breakouts

This is where channel-level detail should later be added through connector tables rather than more prose-driven boxes.

## Why Not Parse The Documents Directly Into The Database

The documents are good sources, but they are not stable machine schemas:

- some sections are incomplete
- some names are inconsistent across documents and decks
- some hierarchies differ by lifecycle phase
- some fields are narrative rather than enumerated

Direct parsing would create brittle data and constant cleanup work. The curated spec layer is the correct boundary.

## Extension Plan

1. Expand the seeded system specs until every consortium document in scope has a first-pass model.
2. Add connector-level entities for cable endpoints, flange ports, and readout mapping.
3. Add instance-level PID imports from real HWDB exports or spreadsheets.
4. Add schedule specs and task dependencies if you want Mermaid Gantt output.
5. Add validation rules for duplicate names, broken references, and missing source links.

## Immediate Usage

Use this pipeline as the design-time modeling tool first.

Once the structure is stable, the same schema can become the staging area for:

- spreadsheet imports
- EDMS cross-links
- dashboard summaries
- installation readiness tracking
