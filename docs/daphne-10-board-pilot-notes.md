# DAPHNE 10-Board Pilot Notes

Status: planning note captured 2026-07-30

## Scope

Exercise 10 boards before the full production campaign. This pilot is intended
to validate the station workflow, database lifecycle, firmware deployment,
identity readback, provisional runtime configuration, and QA/QC evidence path.

## Provisional Runtime Assumptions

These values are acceptable for the 10-board pilot only. They must become a
versioned runtime/register contract before the 200-board campaign.

- Endpoint address: allocate from a controlled range for the 10 pilot boards.
- Clock source: use local clocking for the pilot.
- Hermes/DAQ identifiers: allocate from a controlled range for the 10 pilot
  boards.
- Frontend configuration: examples will be supplied from the external repo.

## Open Inputs

- Select the 10 physical board asset IDs from the authoritative inventory.
- Define the exact endpoint-address range and allocation rule.
- Define the exact Hermes/DAQ ID range and allocation rule.
- Identify the frontend-config example repo, commit, and files.
- Decide whether pilot IP/hostname/VLAN values come from the production pool or
  an isolated pilot pool.
- Define the minimum QA/QC acceptance set for the pilot, including identity,
  artifact integrity, register readback, timing/local-clock status, frontend
  config application, 1024-sample capture, self-trigger behavior, and counters.

## Resume Point

Next session should turn these provisional assumptions into a concrete
`daphne.runtime-config`/register-plan draft for 10 boards, then run one dry-run
record end to end before touching hardware.
