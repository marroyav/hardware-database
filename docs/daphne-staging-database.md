# DAPHNE HWDB Staging Database

## Purpose

This is a small, local database for enrolling DAPHNE carrier assets and their
replaceable Kria K26 SOMs before the official DUNE HWDB component types and
fields are available. It is a staging and review tool, not a replacement for
the DUNE HWDB.

The design follows the official HWDB concepts:

- an HWDB architect creates component types and assigns component type IDs;
- the HWDB assigns the final Part ID when an item is created;
- item `specifications` store identifying properties;
- test types store QA/QC and other records associated with an item;
- connector functional positions link installed subcomponents to their
  parent item.

The staging database never creates a plausible-looking DUNE PID and never
writes to the development or production HWDB.

## Object model

```text
DAPHNE Board Assembly item
  specifications:
    DAPHNE asset ID
    carrier serial/revision
    schematic reference
  connector "Kria SOM"
    -> Kria K26 SOM item
       specifications:
         factory UUID
         factory serial/product/revision
         factory MAC ID 0
  tests:
    DAPHNE Production Network Enrollment

Kria K26 SOM item
  tests:
    K26 EEPROM Identity Inspection
```

This separation is deliberate. A SOM can be replaced without changing the
DAPHNE carrier asset, and the connector history can record which SOM is
currently installed. Network configuration and boot-chain verification are
test/enrollment data rather than immutable SOM identity.

## Local SQLite tables

| Table | Purpose |
|---|---|
| `component_type_proposals` | Proposed HWDB component types and unresolved official IDs. |
| `component_type_fields` | Proposed item specification fields. |
| `component_type_connectors` | Proposed `Kria SOM` functional-position connector. |
| `test_type_proposals` | Proposed EEPROM and production-enrollment test types. |
| `test_type_fields` | Proposed test-data fields. |
| `assets` | DAPHNE carrier asset rows imported before SOM discovery. |
| `soms` | Unique factory identities decoded from K26 EEPROMs. |
| `installations` | Time-aware carrier-to-SOM bindings. |
| `deployments` | Current MAC/IP/endpoint/firmware assignment and observed boot layers. |
| `qa_events` | Append-only enrollment evidence records. |

SQLite uniqueness constraints reject duplicate SOM UUIDs, SOM serials,
factory MACs, production MACs, IP addresses, hostnames, and timing endpoints.
Partial unique indexes enforce one active SOM per carrier and one active
carrier per SOM.

## Files

- `specs/staging/daphne_hwdb.toml`: proposed component types, fields,
  connectors, and test types.
- `specs/staging/daphne_observed_seed.toml`: read-only DAPHNE-015 observation,
  explicitly not production-qualified.
- `specs/staging/daphne_assets_template.csv`: authoritative carrier-list
  import template.
- `tools/daphne_staging.py`: SQLite, validation, enrollment, and export CLI.

## Initial use

From the repository root:

```bash
python3 tools/daphne_staging.py init
python3 tools/daphne_staging.py list
python3 tools/daphne_staging.py validate
python3 tools/daphne_staging.py export-hwdb
```

The default SQLite file is `build/daphne-staging.db`. Generated files remain
under `build/`, which is not the canonical source model.

The seed contains the directly observed DAPHNE-015 identities:

```text
carrier asset:   NP04-DAPHNE-015
SOM UUID:        70c5439d-de29-4263-8066-99627ad4ae5e
SOM EEPROM MAC:  00:0a:35:0e:9b:63
active MAC:      ba:be:ba:d1:cc:ff
IPv4:            10.73.137.16
timing endpoint: 0x15
```

The seed is `observed`, not `qualified`, because the current U-Boot, working
FDT, and Linux active addresses do not all carry one authoritative production
MAC.

## Loading the 192 carrier rows

Obtain the authoritative asset list rather than generating names. Populate a
CSV using `daphne_assets_template.csv`, then run:

```bash
python3 tools/daphne_staging.py import-assets daphne-assets.csv
```

The carrier rows can exist in `pending` state before any SOM box is opened.
UUID, factory serial, and factory MAC remain empty until automated discovery.

## Automated enrollment

The station should obtain the EEPROM arguments from a checksum-valid FRU
decoder and the runtime arguments from the controlled provisioning and boot
verification workflow. A representative command is:

```bash
python3 tools/daphne_staging.py enroll \
  --asset-id DAPHNE-ASSET-ID \
  --som-uuid UUID-FROM-EEPROM \
  --som-serial SERIAL-FROM-EEPROM \
  --som-product SM-K26-XCL2GC-ED \
  --som-revision REVISION-FROM-EEPROM \
  --factory-mac MAC-FROM-EEPROM \
  --fru-checksum-valid \
  --eeprom-sha256 RAW-DUMP-SHA256 \
  --mac-source daphne_pool \
  --production-mac APPROVED-MAC \
  --ipv4-address APPROVED-IP \
  --hostname APPROVED-HOSTNAME \
  --timing-endpoint APPROVED-ENDPOINT \
  --firmware-release APPROVED-RELEASE \
  --uboot-ethaddr APPROVED-MAC \
  --fdt-mac APPROVED-MAC \
  --linux-active-mac APPROVED-MAC \
  --network-admission-approved \
  --boot-chain-passed \
  --station-id STATION-ID \
  --operator OPERATOR
```

The command is one transaction: identity conflicts, duplicate network values,
or an already-installed different SOM reject the entire enrollment.

## HWDB handoff export

```bash
python3 tools/daphne_staging.py export-hwdb
```

The review package contains:

- `component-type-proposals.json`
- `test-type-proposals.json`
- `item-payloads.json`
- `subcomponent-links.json`
- `test-result-payloads.json`
- `inventory.csv`
- `manifest.json`

The JSON uses the official REST shapes for component-type PATCHes, item POSTs,
test-type POSTs, test-result POSTs, and subcomponent PATCHes. Payloads remain
`ready_for_submission: false` while required HWDB IDs are unresolved.

After the HWDB team assigns IDs, record them locally:

```bash
python3 tools/daphne_staging.py set-hwdb-id type daphne_board Dxxxxxxxxxxx
python3 tools/daphne_staging.py set-hwdb-id type kria_k26_som Dxxxxxxxxxxx
python3 tools/daphne_staging.py set-hwdb-id asset NP04-DAPHNE-015 Dxxxxxxxxxxx-xxxxx
python3 tools/daphne_staging.py set-hwdb-id som 70c5439d-de29-4263-8066-99627ad4ae5e Dxxxxxxxxxxx-xxxxx
```

These commands record IDs assigned by HWDB; they do not allocate or submit
them.

## Decisions required from the HWDB team

The export intentionally leaves these unresolved:

1. Official DUNE PDS system ID and DAPHNE subsystem ID.
2. Component type IDs for the DAPHNE assembly and K26 SOM.
3. Whether the K26 SOM should be its own reusable component type or represented
   through an existing computing-hardware type.
4. Institution, manufacturer, and role IDs.
5. Final specification field names and YAML datasheet types.
6. Final test type names and schemas.
7. Whether network enrollment belongs in HWDB test data or in an external
   configuration authority linked from HWDB.

No live HWDB change should be attempted until these points are reviewed.

## Official HWDB references

- [DUNE HWDB conceptual overview and PID](https://dune.github.io/computing-HWDB/02-Introduction-HWDB/)
- [DUNE component types, specifications, connectors, and test types](https://dune.github.io/computing-HWDB/03-Setting-up-Types/)
- [DUNE HWDB REST and bulk-upload training](https://dune.github.io/computing-HWDB/aio/)

The production API hierarchy requires authenticated access. The public
read-only request made while preparing this staging model returned HTTP 403,
so the official PDS IDs were not inferred from an unauthenticated response.
