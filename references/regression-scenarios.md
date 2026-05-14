# Erie Remote SSH Regression Scenarios

## Contents

- Governance
- Runtime Contract
- Installation And Release
- Remote Safety

## Governance

- Root `AGENTS.md` metadata matches the locally installed `agents-md-generator` version.
- `.agents/docs-governance-state.json` records the latest reviewed exact-cwd session count, review timestamp, and handoff count used for the review.
- `inspect_project.py .` no longer reports root `AGENTS.md` as trigger-required.

## Runtime Contract

- `discover` reports `message` and `next_action` in both text and JSON output.
- `check` reports `server_id`, `server_name`, `workdir_status`, `software_cache_status`, `message`, and `next_action`.
- `workspace-check` reports the same contract fields before software-cache output.
- `request-command` and other request creators report a pending-state contract plus request path and risk summary.
- `exec-detached`, `status`, and detached `run-request` output keep the same contract fields so downstream skills can resume jobs consistently.

## Installation And Release

- Source, `dist/`, and installed skill copies stay in parity for key shipped files.
- Installer preserves `config/server_list.local.json`, `config/server_list.local.json.bak.*`, and `reports/`.
- Runtime artifacts do not escape into root-level `requests/`, `downloads/`, or `tmp/` in current behavior.

## Remote Safety

- Same-host multi-account or multi-port targets remain disambiguated through `choices --host`.
- Missing private keys or passwordless failures steer the user into key-only repair instead of broad SSH mutation guidance.
- `workspace-check` failure tells the caller whether to repair auth, create a reviewed remote directory, or retry after project-workdir preparation.
- Sensitive local uploads still require explicit confirmation on both request creation and request execution.
