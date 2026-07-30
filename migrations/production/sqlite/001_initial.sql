PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    carrier_serial TEXT,
    carrier_revision TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
        'received', 'discovered', 'allocated', 'provisioned', 'qa_running',
        'qa_passed', 'released', 'quarantined', 'service', 'retired'
    )),
    resume_state TEXT CHECK(resume_state IS NULL OR resume_state IN (
        'received', 'discovered', 'allocated', 'provisioned', 'qa_running',
        'qa_passed', 'released', 'service'
    )),
    record_revision INTEGER NOT NULL DEFAULT 1 CHECK(record_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soms (
    som_uuid TEXT PRIMARY KEY,
    som_serial TEXT NOT NULL UNIQUE,
    som_product TEXT NOT NULL,
    som_revision TEXT,
    factory_mac_id_0 TEXT NOT NULL UNIQUE,
    fru_checksum_valid INTEGER NOT NULL CHECK(fru_checksum_valid IN (0, 1)),
    eeprom_sha256 TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installations (
    installation_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    som_uuid TEXT NOT NULL REFERENCES soms(som_uuid),
    installed_at TEXT NOT NULL,
    removed_at TEXT,
    station_id TEXT NOT NULL,
    operator TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS production_one_active_som_per_asset
ON installations(asset_id) WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS production_one_active_asset_per_som
ON installations(som_uuid) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS assignments (
    asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
    assignment_revision INTEGER NOT NULL CHECK(assignment_revision >= 1),
    mac_source TEXT NOT NULL CHECK(mac_source IN (
        'som_eeprom', 'daphne_pool', 'legacy_override'
    )),
    production_mac TEXT NOT NULL UNIQUE,
    ipv4_address TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL UNIQUE,
    vlan INTEGER CHECK(vlan IS NULL OR vlan BETWEEN 1 AND 4094),
    timing_endpoint TEXT NOT NULL UNIQUE,
    firmware_release TEXT NOT NULL,
    network_authorized INTEGER NOT NULL DEFAULT 0 CHECK(network_authorized IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignment_revisions (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    assignment_revision INTEGER NOT NULL CHECK(assignment_revision >= 1),
    mac_source TEXT NOT NULL,
    production_mac TEXT NOT NULL,
    ipv4_address TEXT NOT NULL,
    hostname TEXT NOT NULL,
    vlan INTEGER,
    timing_endpoint TEXT NOT NULL,
    firmware_release TEXT NOT NULL,
    reason TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(asset_id, assignment_revision)
);

CREATE TABLE IF NOT EXISTS configuration_snapshots (
    snapshot_sha256 TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    assignment_revision INTEGER NOT NULL,
    contract_version INTEGER NOT NULL CHECK(contract_version = 1),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    som_uuid TEXT REFERENCES soms(som_uuid),
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('recorded', 'passed', 'failed')),
    from_state TEXT,
    to_state TEXT,
    station_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_uri TEXT,
    evidence_sha256 TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS production_evidence_asset_time
ON evidence_events(asset_id, observed_at);

CREATE TABLE IF NOT EXISTS qa_results (
    operation_id TEXT PRIMARY KEY REFERENCES evidence_events(operation_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL CHECK(recipe_version >= 1),
    test_id TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
    observed_at TEXT NOT NULL,
    measurements_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS production_qa_asset_recipe
ON qa_results(asset_id, recipe_id, recipe_version, test_id, observed_at);
