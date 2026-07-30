# DAPHNE Production Enrollment Contract

## Scope

This workflow deploys and QA/QCs a DAPHNE carrier plus replaceable Kria K26
SOM without depending on the final DUNE HWDB types or API. The production
database is authoritative during the campaign. HWDB integration is a later
adapter that consumes the same versioned records.

The production system separates:

1. one immutable release bundle shared by the batch;
2. one database assignment and configuration snapshot per carrier;
3. one append-only evidence history per operation;
4. a versioned QA recipe that determines qualification.

## Versioned contracts

The JSON Schema documents are under `schemas/daphne-production/v1/`:

- `inventory-record.schema.json` describes carrier lifecycle state;
- `assignment.schema.json` describes network, timing, and release allocation;
- `evidence.schema.json` describes append-only station evidence;
- `qa-recipe.schema.json` describes qualification requirements;
- `board-config.schema.json` is the database-to-firmware deployment boundary.

Contract version 1 is immutable. A breaking field or semantic change requires
a version 2 schema and an explicit migration.

## Lifecycle

```text
received
  -> discovered
  -> allocated
  -> provisioned
  -> qa_running
  -> qa_passed
  -> released
  -> service
  -> received              # after an approved SOM removal
```

Any non-retired working state can enter `quarantined`. The database stores the
previous state, and `resume` restores only that state. It never skips a gate.
A SOM replacement closes the active installation instead of editing or
deleting its history.

Quarantine always clears network authorization. A quarantined `released` unit
resumes at `qa_passed`, so an authorized operator must run `release` again.

Only `release` sets `network_authorized=true`. Qualification and release are
separate operations so a QA station cannot authorize production networking.

## Database and migrations

SQLite is the default for development and a single-station pilot:

```bash
python3 tools/daphne_production_cli.py migrate
```

The default file is `build/daphne-production.db`. Multiple stations use a
PostgreSQL URL and the optional psycopg 3 driver:

```bash
python3 tools/daphne_production_cli.py \
  --database-url postgresql://USER@HOST/DATABASE migrate
```

Ordered backend migrations live under `migrations/production/`. Both backends
enforce unique SOM serials, factory MACs, production MACs, IPv4 addresses,
hostnames, timing endpoints, active carrier-to-SOM installations, and operation
IDs. Mutating station operations use database transactions. Reusing an
operation ID with the same payload is a safe retry; reusing it with different
data is rejected.

## Operator sequence

Every station-supplied `--operation-id` must be stable across retries. A UUID
or station run ID is appropriate.

### 1. Import carrier assets

```bash
python3 tools/daphne_production_cli.py asset-import \
  /controlled/daphne-assets.csv \
  --station-id intake-01 --operator OPERATOR
```

The CSV columns are `asset_id`, `carrier_serial`, and `carrier_revision`.

### 2. Discover and bind the K26 SOM

```bash
python3 tools/daphne_production_cli.py discover \
  --asset-id DAPHNE-ASSET \
  --som-uuid UUID --som-serial SERIAL \
  --som-product SM-K26-XCL2GC-ED --som-revision REVISION \
  --factory-mac MAC --fru-checksum-valid \
  --eeprom-sha256 SHA256 \
  --operation-id RUN:discover --station-id station-01 --operator OPERATOR
```

Invalid FRU data is recorded with `quarantine`; the discovery operation never
creates an identity from a malformed EEPROM.

### 3. Allocate immutable deployment values

```bash
python3 tools/daphne_production_cli.py allocate \
  --asset-id DAPHNE-ASSET --mac-source som_eeprom \
  --production-mac MAC --ipv4-address 192.0.2.10 \
  --hostname daphne-001.example --vlan 100 \
  --timing-endpoint 0x001 --firmware-release daphne-RELEASE \
  --operation-id RUN:allocate --station-id station-01 --operator OPERATOR
```

An existing assignment can be retried unchanged. It cannot be silently
rewritten. After approved rework, `reassign --reason ...` creates an explicit,
append-only assignment revision. Under `som_eeprom` policy, allocation and
reassignment reject a production MAC that differs from the active SOM.

### 4. Render the board snapshot

```bash
python3 tools/daphne_production_cli.py render \
  --asset-id DAPHNE-ASSET --output-dir /controlled/RUN/config \
  --operation-id RUN:render --station-id station-01 --operator OPERATOR
```

This writes canonical `board-config-v1.json` and `SHA256SUMS`. The firmware
repository renderer consumes that file without knowing which database created
it.

### 5. Provision and QA

Record the immutable release-manifest hash and snapshot hash:

```bash
python3 tools/daphne_production_cli.py provision \
  --asset-id DAPHNE-ASSET --snapshot-sha256 SHA256 \
  --artifact-manifest-sha256 SHA256 --pass \
  --operation-id RUN:provision --station-id station-01 --operator OPERATOR

python3 tools/daphne_production_cli.py qa-start \
  --asset-id DAPHNE-ASSET \
  --operation-id RUN:qa-start --station-id station-01 --operator OPERATOR
```

Each test appends one result. Measurements can be inline JSON or `@FILE`:

```bash
python3 tools/daphne_production_cli.py test \
  --asset-id DAPHNE-ASSET --test-id identity_chain --pass \
  --measurements @identity-result.json \
  --operation-id RUN:test:identity \
  --station-id station-01 --operator OPERATOR
```

The released recipe is `specs/production/daphne-production-qa-v1.json`.
Qualification fails until every required result and its minimum pass count are
present. In particular, the v1 recipe requires three independent cold-boot
passes.

### 6. Qualify and separately release

```bash
python3 tools/daphne_production_cli.py qualify \
  --asset-id DAPHNE-ASSET \
  --operation-id RUN:qualify --station-id qa-review --operator REVIEWER

python3 tools/daphne_production_cli.py release \
  --asset-id DAPHNE-ASSET \
  --operation-id RUN:release --station-id release-control --operator APPROVER
```

Network automation may consume only records whose lifecycle is `released` and
whose assignment has `network_authorized=true`.

`status`, `list`, and `history` export versioned inventory, assignment, and
append-only evidence records for dashboards, campaign review, and later HWDB
adapters.

## Failure and rework

Use `quarantine --reason ...` for identity, fixture, provisioning, or test
failures. `resume` returns to the saved state after the problem is reviewed.
Use `service` before planned work on a released unit. `replace-som` closes the
old active installation, clears network authorization, and returns the carrier
to `received` for rediscovery. After discovering the replacement SOM, use
`reassign` when its EEPROM MAC
changes. The prior assignment remains in `assignment_revisions`.

## HWDB boundary

`tools/daphne_staging.py` is now the legacy HWDB handoff adapter. It remains
available for proposed component types and HWDB-shaped review exports, but its
one-step `enroll` command is not the production workflow. A future adapter
should read released production records and append the assigned HWDB IDs; it
must not become an enrollment-station dependency.
