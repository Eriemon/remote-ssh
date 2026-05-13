# Erie Remote SSH Settings

Encoding canary: 编码校验：中文内容应保持 UTF-8，无乱码。

Use `config/defaults.json` to keep paths and validation choices out of scripts.

## Contents

- Loading Rules
- Path Resolution
- Fields
- Server List Creation
- Interactive Server Add
- Software Catalog and Cache
- Server List JSON
- Passwordless SSH Guidance
- Automation Entry Points
- Key Generation Guardrail
- Example
- Request, Download, and Upload Directories

## Loading Rules

- Helper commands use `erie-remote-ssh/config/defaults.json` when `--settings` is not provided.
- `--settings <json>` selects another settings file.
- `--config <server-list.json>` overrides `paths.default_server_list`.
- When neither flag is provided, the default settings point to `${skill_dir}/config/server_list.local.json`.
- `discover`, `init-config`, and `add-server --interactive` use the same resolution order.
- `validate_remote_ssh.py --server-list <json>` overrides `paths.default_server_list`.
- `validate_remote_ssh.py --skill-validator <path>` overrides `tools.skill_validator_candidates`.

## Path Resolution

Settings paths support:

- Relative paths resolved from the settings file directory.
- Environment variables supported by the operating system.
- `~` home directory expansion.
- `${project_root}` for the current skill project root. In a source checkout it resolves to the repository root; in an installed or release copy it resolves to the skill root.
- `${skill_dir}` for the `erie-remote-ssh` skill directory.
- `${settings_dir}` for the settings file directory.
- `${home}` for the current user's home directory.
- `${cwd}` for the directory where the helper process is launched.
- `${env:NAME}` for environment variables.

Empty `${env:NAME}` values are ignored when used as optional validator candidates.

## Fields

- `version`: Required integer. Use `1`.
- `paths.default_server_list`: Server list JSON used when no CLI override is provided. The bundled default points to skill-local `config/server_list.local.json`.
- `paths.validation_tmp_dir`: Temporary root directory used by validation. The bundled default is `${skill_dir}/reports/tmp/validation`; each run creates and removes its own child directory.
- `paths.requests_dir`: Directory for generated request JSON files. The bundled default is `${skill_dir}/reports/requests`; keep it git-ignored.
- `paths.downloads_dir`: Directory for `file-download` targets. The bundled default is `${skill_dir}/reports/downloads`; local download paths must stay inside this directory.
- `paths.upload_roots`: Non-empty list of local directories allowed as `request-upload --local` sources. Defaults to `${project_root}`. In a source checkout that means the repository root; in an installed or release copy it means the skill root. Use explicit workspace or data directories for uploads outside the skill project; do not configure a filesystem root or the whole user home directory.
- `tools.ssh_client`: SSH executable name or path.
- `tools.scp_client`: SCP executable name or path.
- `tools.ssh_keygen`: SSH key generator executable name or path. Defaults to `ssh-keygen`; validation may replace it with a fake helper.
- `tools.skill_validator_candidates`: Ordered `quick_validate.py` candidates.
- `ssh.config_path`: OpenSSH config path used by `ssh-config-discover` and `--ssh-alias` fallback. Defaults to `${home}/.ssh/config`.
- `ssh.default_workdir`: Default workdir prompt value for newly added servers. Defaults to `~/workspace`.
- `ssh.default_timeout`: Positive integer timeout for remote validation commands.
- `ssh.connect_timeout`: Value injected into SSH option templates.
- `ssh.safe_options`: Default SSH options. Keep host-key writes disabled here.
- `ssh.accept_new_host_key_options`: Explicit opt-in options for accepting new host keys.
- `jobs.remote_dir`: Relative directory under effective remote `workdir` for detached job state. Defaults to `.erie-remote-ssh/jobs`.
- `jobs.local_dir`: Local directory for detached job manifests. Defaults to `${skill_dir}/reports/jobs`.
- `jobs.default_tail_lines`: Positive integer default for `tail-log --lines`. Defaults to `80`.
- `projects.auto_discover`: Boolean. When true, commands automatically search upward from `${cwd}` for local project config.
- `projects.config_dir`: Project config directory name. Defaults to `.erie-remote-ssh`.
- `projects.config_names`: Ordered project config filenames. Defaults to `project.local.json`, then `project.json`.
- `projects.default_workdir_template`: Template used by `--project <id>` and `project-init` when no explicit remote workdir is supplied. Defaults to `~/workspace/${project_id}`.
- `files.default_remote_tmp_dir`: Reserved relative remote temp directory name for workflows that need one.
- `files.max_transfer_bytes`: Maximum regular file size allowed for `file-download`.
- `inventory.catalog_version`: Positive integer copied into cached software scans. Increment it when catalog behavior changes enough that old caches may be incomplete.
- `inventory.software_catalog`: Configured read-only software probes used to build the remote scan script. Each item has an `id`, optional PATH `commands`, optional `path_scan` (`first` or `all`), optional absolute `executable_globs`, optional shell `version_command` using `{path}`, optional `install_path_command`, and optional `directory_scans`.
- `validation.positive_server`: Server selector for positive local tests.
- `validation.warning_server`: Server selector expected to produce metadata warnings.
- `validation.ssh_server`: Server selector for real SSH tests.
- `validation.expected_inventory_contains`: Text snippets expected in inventory output.

## Server List Creation

Use `config/server_list.template.json` as the non-sensitive template when creating a real server list. Copy it to `config/server_list.local.json` or pass an external file with `--config`. Keep real server values out of committed files.

Use `init-config` when the configured server list does not exist:

```powershell
python <skill-dir>\scripts\remote_ssh.py init-config --settings <settings>
```

It creates this v1 structure:

```json
{
  "version": 1,
  "default_key_dir": "~/.ssh",
  "servers": []
}
```

Existing files are not overwritten unless `--force` is passed. Forced overwrites create a timestamped `.bak.*` file next to the server list.

## Configuration Gate

Use `configure --interactive` or a platform `configure_remote_ssh` wrapper before any server-list mutation:

```powershell
python <skill-dir>\scripts\remote_ssh.py configure --settings <settings> --interactive
```

The agent must ask the user in the conversation before launching this gate. The CLI then asks for one of three modes and does not provide a default:

- `script`: run guided configuration.
- `manual`: print manual setup steps and do not mutate the server list.
- `cancel`: exit without changes.

Guided mode initializes a missing list, adds a server when no server exists, or shows a redacted server summary and asks whether to add, update, or cancel when entries already exist. That action prompt also has no default. Use `--server <id-or-name>` to go directly to updating one existing entry after the configuration mode gate.

## Interactive Server Add and Update

Use `add-server --interactive` to add one configured SSH server:

```powershell
python <skill-dir>\scripts\remote_ssh.py add-server --settings <settings> --interactive
```

Use `update-server --interactive --server <id-or-name>` to modify an existing entry:

```powershell
python <skill-dir>\scripts\remote_ssh.py update-server --settings <settings> --server <id-or-name> --interactive
```

These direct commands are lower-level maintenance and validation entry points. Agent-guided user configuration should go through `configure --interactive` after the user explicitly chooses guided script mode.

Prompted fields:

- `id`: Defaults to the next `server_N` value.
- `name`: Defaults to the id.
- `host`: Required hostname or IP, stored only in the server list.
- `port`: Defaults to `22`; must be `1..65535`.
- `username`: Required SSH username.
- `key_name`: Required key filename or path. If the private key is missing for an enabled server, the helper asks whether to generate it, save the entry disabled, or cancel.
- `workdir`: Defaults to `ssh.default_workdir`, which is `~/workspace` in the bundled settings.
- `enabled`: Defaults to `true`.
- `notes`: Optional free text.

Post-add optional metadata:

- `category`: Optional non-sensitive grouping label used by `choices`; edit the server list after add when a specific category is useful.
- `functions`: Optional non-sensitive array of capabilities used by `choices`; edit the server list after add when explicit functions are useful.

The helper validates schema, rejects duplicate id/name selectors, backs up an existing server list, and writes via temporary file replacement. For enabled servers, it then runs a mandatory read-only software scan and caches the result in `software_scan`. If the scan fails, the server record is kept with `software_scan.status` set to `failed` and the add or update flow returns a failure code.

Multiple entries may use the same `host` when they represent different SSH usernames, ports, keys, or workdirs. During interactive add, the helper lists existing entries for the same host with username and port visible, asks before adding another login, and defaults to cancelling an exact `host + username + port` duplicate.

## Software Catalog and Cache

Use `scan-software` to refresh the cached software scan:

```powershell
python <skill-dir>\scripts\remote_ssh.py scan-software --settings <settings> --server <id-or-name>
```

Use `software` to answer cached availability questions without reconnecting:

```powershell
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name> --name vivado
```

The bundled catalog scans Python, Conda, CUDA/nvcc, NVIDIA driver, GCC, G++, CMake, Vivado, and Vitis. Python, CUDA, GCC/G++, and CMake include common global multi-version paths; Vivado and Vitis scan configured Xilinx install roots such as `/opt/Xilinx`, `/tools/Xilinx`, and `/usr/local/Xilinx`.

Treat `inventory.software_catalog` as trusted local configuration. Its `version_command` and `install_path_command` templates are rendered into a POSIX shell script and executed read-only over SSH, so only reviewed skill or settings files should define them. Do not place untrusted user text into software catalog command templates.

`path_scan` defaults to `first` for compatibility. Use `all` only for simple command names when every executable on `PATH` should be reported. `executable_globs` must be absolute POSIX patterns with safe glob characters and are intended for reviewed system paths such as `/usr/bin/gcc-[0-9]*`, `/usr/bin/cmake[0-9]*`, or `/usr/local/cuda-*/bin/nvcc`.

Directory scans must use absolute POSIX `base_dirs` and relative `subdir` / `executable` values. Invalid catalog shapes, duplicate software ids, missing probes, invalid `path_scan` values, unsafe executable globs, and non-absolute scan roots should fail before SSH execution with a clear settings error.

Cached `software_scan.tools.<id>.versions` records every detected install for a tool. The top-level `path`, `version`, and `install_path` remain the first detected install for compatibility, while `software --name <tool>` and the full `software` table expose multi-version entries. Cached `software_scan.raw_summary` is a local operational artifact. It can include host inventory lines and installation paths, so keep real server lists and scan caches out of public docs and committed files.

## Server List JSON

The bundled default settings resolve `paths.default_server_list` to `${skill_dir}/config/server_list.local.json` from `erie-remote-ssh/config/defaults.json`.

Resolution priority is:

1. `--config <server-list.json>`
2. `--settings <json>` with `paths.default_server_list`
3. `erie-remote-ssh/config/defaults.json`

Commands that write validation or scan state use the same resolved path. With bundled defaults, `workspace-check`, `scan-software`, `configure`, `add-server`, and `update-server` write `erie-remote-ssh/config/server_list.local.json`. If `--config` or a custom settings file is supplied, they write the resolved override file instead.

`workspace-check` creates a timestamped backup next to the selected server list before writing `validation`, `workspace_check`, or refreshed `software_scan` data. The backup name uses the existing `<server-list-name>.bak.<timestamp>` format, appends a numeric suffix if another backup was created in the same second, and is printed as `backup: <path>`.

The helper parses JSON with UTF-8, requires a root object, rejects unsupported versions, requires `servers` to be an array of objects, and validates each selected server before connecting. Keep real hostnames, usernames, ports, and key names in the server list only.

Use optional `category` and `functions` fields to make server selection clear without exposing connection details:

```json
{
  "id": "fpga_lab",
  "name": "FPGA Lab Server",
  "category": "FPGA",
  "functions": ["Vivado synthesis", "Vitis acceleration", "workspace validation"]
}
```

When these fields are absent, `choices` displays `Uncategorized` and may infer display-only functions from `notes`, `inventory_snapshot.description`, and cached installed tools in `software_scan`.

## Project Workdir Configuration

Server-list `workdir` is the default remote directory for a login entry. Project config can override it at runtime without modifying the server list. This prevents one global server entry from forcing every task into a directory such as `~/workspace/validation`.

Commands automatically search upward from the current directory for `.erie-remote-ssh/project.local.json`, then `.erie-remote-ssh/project.json`. Use `--project-config <json>` to select one explicitly, `--project <id>` to use a temporary `~/workspace/<id>` context, or `--no-project` to force the server-list `workdir`.

Project config v1:

```json
{
  "version": 1,
  "project_id": "my_project",
  "default_server": "server_1",
  "remote_workdir": "~/workspace/my_project",
  "servers": {
    "server_1": {
      "remote_workdir": "~/workspace/my_project",
      "remote_workdir_check": {
        "status": "available",
        "checked_at": "2026-05-10T00:00:00Z"
      },
      "workspace_check": {
        "status": "ok",
        "checked_at": "2026-05-10T00:00:00Z"
      }
    }
  }
}
```

`project_id` must contain only letters, numbers, `_`, `-`, or `.`. Project config must not store host, username, port, key name, or key path.

Use `project-init --project <id> --server <id-or-name> --interactive` to create `.erie-remote-ssh/project.local.json`. It checks the candidate remote directory over BatchMode SSH before writing local config. If the directory exists or another local project config declares the same server/workdir pair, it asks for `overwrite`, `rename`, `timestamp`, or `cancel`. `overwrite` reuses the directory; it does not delete, clear, or copy over remote files.

With project context active, the effective workdir is resolved as server-specific project override, then project `remote_workdir`, then `projects.default_workdir_template`, then server-list `workdir`. Request files record `project_id`, `effective_workdir`, and `workdir_source` so `run-request` executes in the reviewed project directory even if launched from another local folder.

## Passwordless SSH Guidance

Use `setup-key` to inspect local key readiness without modifying local or remote SSH state:

```powershell
python <skill-dir>\scripts\remote_ssh.py setup-key --settings <settings> --server <id-or-name>
```

It reports whether the private key and matching `.pub` file exist, reminds the operator that only the public key belongs in the remote account's `~/.ssh/authorized_keys`, and keeps default verification aligned with `BatchMode=yes`. Passwordless SSH still requires the user to log in to the remote account once using a password, console, existing jump host, or administrator path and append the public key before Codex can verify with `check`.

## Key Generation Guardrail

Guided configuration can generate a local Ed25519 key when an enabled server entry references a missing private key.

- It uses `tools.ssh_keygen`, defaulting to `ssh-keygen`.
- It resolves relative `key_name` values against `default_key_dir`, normally `~/.ssh`.
- It prints the local private-key target path before writing so the user can confirm the exact destination; public-key content remains hidden unless `--show-sensitive` is explicitly used.
- It refuses to overwrite existing private keys.
- It asks the user to confirm before writing.
- It asks whether the passphrase should be empty or custom. Custom passphrases are read with hidden input and are not written to config or logs.
- It prints public-key installation guidance for the remote `~/.ssh/authorized_keys`; it does not modify the remote account, run `ssh-copy-id`, or skip the first manual login.

## Automation Entry Points

- Windows batch: `scripts/bat/config/configure_remote_ssh.bat`
- Windows PowerShell: `scripts/powershell/config/configure_remote_ssh.ps1`
- POSIX shell: `scripts/shell/config/configure_remote_ssh.sh`

These scripts now call `configure --interactive`. The agent must ask the user before launching them, and the CLI requires an explicit manual/script/cancel choice before mutating any server list. Guided configuration may generate a local key only after explicit confirmation, does not install public keys remotely, does not run `ssh-copy-id`, and does not scan networks. Enabled server additions and updates connect only for the mandatory read-only software scan after passwordless SSH is ready.

## Example

```json
{
  "version": 1,
  "paths": {
    "default_server_list": "${skill_dir}/config/server_list.local.json",
    "validation_tmp_dir": "${skill_dir}/reports/tmp/validation",
    "requests_dir": "${skill_dir}/reports/requests",
    "downloads_dir": "${skill_dir}/reports/downloads",
    "upload_roots": ["${project_root}", "${cwd}"]
  },
  "tools": {
    "ssh_client": "ssh",
    "scp_client": "scp",
    "ssh_keygen": "ssh-keygen",
    "skill_validator_candidates": [
      "${env:REMOTE_SSH_SKILL_VALIDATOR}",
      "${home}/.codex/skills/.system/skill-creator/scripts/quick_validate.py"
    ]
  },
  "ssh": {
    "default_workdir": "~/workspace"
  },
  "projects": {
    "auto_discover": true,
    "config_dir": ".erie-remote-ssh",
    "config_names": ["project.local.json", "project.json"],
    "default_workdir_template": "~/workspace/${project_id}"
  }
}
```

Keep sensitive server values in the server list, not in settings documentation.

## Request, Download, and Upload Directories

The bundled runtime artifact root is `erie-remote-ssh/reports`. It is intentionally git-ignored and may contain request audit JSON, downloaded files, and validation temporary files. Before updating or replacing an installed skill directory from GitHub, a local directory, a release artifact, or another source, inspect the target `reports` directory. If it exists, ask the user whether to clear it or preserve it; preserve it unless the user explicitly confirms cleanup.

Bundled defaults must not create root-level runtime directories next to `erie-remote-ssh`. In particular, default `requests`, `downloads`, validation `tmp`, `out`, and `remote-validation-bundles` paths do not belong at the repository root. `dist/` is the only root-level artifact directory produced by the release build helper.

Request files are local audit artifacts. They record operation type, server id, relative paths, reason, risks, and timestamp; they do not store real hostnames, usernames, key names, key paths, or ports.

Downloads are constrained to `paths.downloads_dir`. Use a custom settings file when a workflow needs a different project-local download root.

Uploads are constrained to `paths.upload_roots`. `request-upload` records the matched upload root and a relative path, then `run-request --execute` resolves and validates them again before SCP. This prevents a modified request JSON from pointing outside the configured local roots. Sensitive local sources, including `.codex`, `.ssh`, private-key-like names, `.env`, `known_hosts`, `authorized_keys`, Windows system directories, and common POSIX system directories, require `--confirm-sensitive-local-upload` and a non-empty `--reason` when the request is created and again when it is executed.
