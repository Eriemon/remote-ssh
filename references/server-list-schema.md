# Server List Schema

Use this reference when reading or extending an Erie Remote SSH server list JSON. Do not copy real hostnames, usernames, ports, key names, or inventory values into documentation unless the user explicitly asks for them.

## Root Object

- `version`: Required integer. This skill supports `1`.
- `default_key_dir`: Optional string. Directory used to resolve relative `key_name` values. Defaults to `~/.ssh` when absent.
- `servers`: Required array of server objects.

## Server Object

- `id`: Stable string identifier, such as `server_alpha`.
- `legacy_server_id`: Optional string for older numeric identifiers.
- `name`: Human-readable server name.
- `category`: Optional non-sensitive grouping label for user choice prompts, such as `FPGA`, `GPU`, `CPU`, `General`, or `Testing`. Missing or empty values are displayed as `Uncategorized`.
- `functions`: Optional array of non-sensitive human-readable capabilities or intended uses, such as `Vivado synthesis`, `CUDA validation`, or `remote app testing`.
- `type`: Connection type. Version 1 supports `ssh`.
- `host`: SSH host or IP address. Treat as sensitive operational data.
- `port`: SSH port integer from `1` through `65535`.
- `username`: SSH login user. Treat as sensitive operational data.
- `key_name`: Private key file name or absolute path. When relative, resolve against `default_key_dir`.
- `workdir`: Remote working directory and the boundary for built-in file modifications. File operation paths are relative to this directory. New interactive entries default to `ssh.default_workdir` from settings, which is `~/workspace` in the bundled settings.
- `enabled`: Required boolean. Do not coerce strings or numbers. Do not connect to disabled servers unless the user explicitly overrides that safety check.
- `wsl_distro`: Optional local WSL distribution hint. The v1 helper uses Windows OpenSSH directly and does not require this field.
- `notes`: Optional free-form operator notes.
- `inventory_snapshot`: Optional previous inventory record.
- `validation`: Optional validation metadata.
- `workspace_check`: Optional workdir validation metadata.
- `software_scan`: Optional cached software scan written by `scan-software` and by successful enabled-server `add-server --interactive` flows.

## Inventory Snapshot

The helper treats this object as historical context only. It does not write back to the source JSON.

- `server_id`: Previous numeric server identifier.
- `description`: Human description of server role.
- `ssh`: Prior SSH connection metadata.
- `system`: Prior hostname, CPU model, and CPU thread metadata.
- `fpga_devices`: Prior FPGA device summary.
- `gpu_devices`: Prior GPU device summary.
- `software`: Prior software summary.

## Validation Object

- `status`: Common values are `verified`, `failed`, `skipped`, or `unknown`.
- `method`: Validation method, such as `ssh_key`.
- `verified_at`: Timestamp string when validation succeeded.
- `last_error`: Last validation error or null.

## Workspace Check Object

- `status`: Common values are `ok`, `failed`, `skipped`, or `unknown`.
- `checked_at`: Timestamp string for the last check.
- `message`: Human-readable workdir status.

## Software Scan Object

The helper writes this object after a read-only remote software scan. Treat it as cached operational state.

- `status`: Common values are `ok`, `failed`, or `skipped`.
- `scanned_at`: UTC timestamp string for the scan attempt.
- `catalog_version`: Integer copied from `inventory.catalog_version` in settings.
- `tools`: Object keyed by configured software id. Each value includes `status`, `path`, `version`, `install_path`, and optional `versions` for multi-install tools.
- `fpga_devices`: Xilinx PCIe device summaries with `device_id`, `pcie_bdf_mgmt`, and `pcie_bdf_user`.
- `raw_summary`: Raw read-only scan output for local troubleshooting.
- `last_error`: Error text when `status` is `failed` or `skipped`.

## Compatibility Rules

- Keep unknown fields when reading configs.
- Reject unsupported root `version` values instead of guessing.
- Reject non-object entries inside `servers[]`.
- Prefer adding optional fields over changing existing v1 field meaning.
- Missing `category` and `functions` fields are valid. The `choices` command may infer display-only functions from `notes`, `inventory_snapshot.description`, and cached installed tools in `software_scan`; it does not write those inferences back.
- `scan-software` updates `software_scan`; ordinary `inventory` remains a report and does not update the source JSON.
- Do not use server-list fields to widen file-operation write access beyond `workdir` in v1.
