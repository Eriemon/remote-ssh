# Erie Remote SSH Workflows

Use these workflows after the `erie-remote-ssh` skill triggers. Prefer the helper CLI because it applies consistent validation, quoting, timeouts, and output redaction.

## Contents

- Progressive Loading
- Integration Contract
- Discover Configuration
- Use SSH Config Alias Fallback
- Choose Configuration Mode
- Add a Server When None Is Available
- Use a Server List JSON
- Use a Project Workdir
- Locate and Inspect Targets
- Update or Replace the Skill
- Local Precheck
- Scan and Query Software
- File Operations
- Generate a Manual SSH Command
- Run a Remote Command
- Run and Inspect Detached Jobs
- Collect Inventory
- Troubleshooting
- Output Handling

## Progressive Loading

- Read `server-list-schema.md` when schema fields, compatibility, or validation failures matter.
- Read `configuration.md` when settings, paths, validation targets, or tool locations matter.
- Read `integration-contract.md` when another skill needs stable blocked reasons, selection gates, or output fields from this helper.
- Read `regression-scenarios.md` before changing output fields, installer behavior, governance freshness checks, or detached-job flow.
- Read `review-checklist.md` before declaring confidence, reviewing a change, or reporting that validation is complete.

## Integration Contract

When another skill depends on `erie-remote-ssh`, keep the lifecycle stable:

1. `discover`
2. `choices`
3. `check`
4. `workspace-check`
5. `request-*` / `exec` / `exec-detached`
6. `run-request` / `status` / `tail-log`

If the dependency is missing or no usable server list exists, stop early with a blocked reason and a concrete `next_action` instead of guessing SSH paths or inventing fallback shell commands.
- Keep `SKILL.md` as the checklist entrypoint; load this file only for detailed operation steps.

## Discover Configuration

1. Use the user-provided settings file when available.
2. If no settings path is provided, use `config/defaults.json`.
3. Use `--config` only to override the server list configured in settings.
4. Run:

```powershell
python <skill-dir>\scripts\remote_ssh.py discover --settings <settings>
```

Discovery outcomes:

- Exit `0`, `available`: at least one enabled SSH server is configured.
- Exit `3`, `not_configured`: the server list file is missing.
- Exit `4`, `no_enabled_ssh`: the server list exists but has no enabled SSH target.

`discover --json` gives stable machine-readable fields for automation scripts. Discovery reads configured JSON and, only when the server list is missing, reads configured OpenSSH `Host` aliases as fallback hints. It does not scan networks, probe unknown hosts, generate keys, modify SSH config, or create server records.

## Use SSH Config Alias Fallback

When `discover` exits `3` with `ssh_config_fallback_available: true`, inspect aliases without connecting:

```powershell
python <skill-dir>\scripts\remote_ssh.py ssh-config-discover --settings <settings>
```

Only simple `Host <alias>` entries are listed. Wildcard, negated, and pattern hosts are excluded. Default output shows alias names only; use `--show-sensitive` only when the user explicitly needs HostName, User, Port, or IdentityFile values.

Temporary alias targets do not write server-list JSON and do not provide cached validation, workspace, or software status:

```powershell
python <skill-dir>\scripts\remote_ssh.py command --settings <settings> --ssh-alias <alias>
python <skill-dir>\scripts\remote_ssh.py exec --settings <settings> --ssh-alias <alias> -- echo ok
```

Use alias fallback to avoid bypassing the skill when a local SSH alias already exists. If the target becomes a regular workflow dependency, ask the user whether to create a persistent server-list record through guided configuration.

## Update or Replace the Skill

Before updating or replacing an installed `erie-remote-ssh` directory from GitHub, a local directory, a release artifact, or another source, use the safe installer:

```powershell
python <skill-dir>\scripts\install_skill.py --source <release-or-skill-dir> --target <codex-home>\skills\erie-remote-ssh
```

The installer backs up the current installed skill to `${CODEX_HOME:-~/.codex}/skill_backups/erie-remote-ssh-YYYYMMDD-HHMMSS` before copying files. It must preserve the user's `config/server_list.local.json`, `config/server_list.local.json.bak.*`, and `reports/` content, then report `preserved_hash_verified: true`. If copying fails after the backup is created, the installer must restore the backup rather than leave a mixed partial installation. If a source artifact contains `config/server_list.local.json` or a server-list backup, treat that artifact as unsafe and do not install it.

`reports` is the skill-local runtime artifact root and is not managed by git. Bundled defaults write request files to `${skill_dir}/reports/requests`, downloads to `${skill_dir}/reports/downloads`, detached job manifests to `${skill_dir}/reports/jobs`, and validation temp runs to `${skill_dir}/reports/tmp/validation`; custom settings may override these paths.

Do not treat root-level `out`, `remote-validation-bundles`, `requests`, `downloads`, or `tmp` directories next to `erie-remote-ssh` as normal output from this skill. `out` and `remote-validation-bundles` usually come from other skills or explicitly supplied output paths; root-level `requests`, `downloads`, and `tmp` may be historical artifacts from older defaults. The only root-level artifact directory this skill intentionally creates is `dist/` during release builds.

Release artifact naming is canonical only in the form `dist/erie-remote-ssh-vX.Y.Z` plus `dist/erie-remote-ssh-vX.Y.Z.zip`. Older different versions remain in `dist/`. Republishing the same version may overwrite only that exact same-version directory, zip, and manifest. Noncanonical aliases without the `v` prefix are treated as legacy mistakes and should not be kept.

## Choose Configuration Mode

Before creating or changing server configuration, fixing a missing key reference, repairing failed passwordless SSH, creating an initial server list, handling a list with no enabled servers, or reworking an unusable server entry, ask the user in the conversation whether they want manual instructions, guided script configuration, or cancellation. Do not run the guided script, `configure-key --interactive`, direct add/update commands, or a platform wrapper until the user explicitly chooses guided script configuration.

Use the unified guided entry point:

```powershell
python <skill-dir>\scripts\remote_ssh.py configure --settings <settings> --interactive
```

Use `configure --interactive --server <id-or-name>` when the user already selected an existing entry to modify.

Configuration mode meanings:

- `manual`: print setup steps and leave files unchanged.
- `script`: prompt for fields, write the server list, and run required validation for enabled entries.
- `cancel`: leave files unchanged.

The CLI also asks for this mode and has no default. Pressing Enter without a mode repeats the prompt instead of choosing `script`. If servers already exist, guided mode lists a redacted server summary, then requires an explicit `add`, `update`, or `cancel` choice.

Do not respond to a missing private key by only giving a raw `ssh-keygen` command. For a new or full server edit, offer the guided configuration flow so the user can choose key generation, saving the entry disabled, or cancellation. For an existing server where only the key or passwordless login is broken, offer key-only repair with `configure-key --interactive`; it only changes `key_name` and validation caches after successful verification.

## Add a Server When None Is Available

Use the automation scripts when the user asks how to find, add, or modify remote server configuration:

```powershell
<skill-dir>\scripts\bat\config\configure_remote_ssh.bat --settings <settings>
```

```sh
sh <skill-dir>/scripts/shell/config/configure_remote_ssh.sh --settings <settings>
```

PowerShell equivalent:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <skill-dir>\scripts\powershell\config\configure_remote_ssh.ps1 --settings <settings>
```

Manual equivalent:

```powershell
python <skill-dir>\scripts\remote_ssh.py configure --settings <settings> --interactive
python <skill-dir>\scripts\remote_ssh.py discover --settings <settings>
```

Adding an enabled server, enabling a server, or changing host, port, username, key_name, or workdir writes the configured server list, creates a backup before replacing an existing file, validates schema, then runs a mandatory read-only software scan over SSH. The scan must complete before the add flow reports `added:` or connection-changing update reports `updated:`; failures are cached as `software_scan.status: failed`. Name/category/functions/notes-only updates show a redacted summary, require `save_server_record`, preserve validation caches, and do not force a key check or scan.

`add-server --interactive` prompts in three groups: connection fields, key/workdir fields, and metadata. Metadata includes optional `category`, `functions`, and `notes`; enter functions as comma- or semicolon-separated labels. The helper prints a redacted summary and requires `save_server_record` confirmation before key handling or JSON write-back.

`update-server --interactive` opens a field menu. Use `show` to inspect the candidate record, a field name to edit one value, `all` to walk every editable field, `done` to review a redacted summary and confirm `save_server_record`, or `cancel` to leave the server list unchanged. `configure --interactive` shows numbered server choices before `update`; the selector accepts a number, id, or name. Only host, port, username, key_name, and workdir edits clear validation/workspace/software caches; name/category/functions/notes-only edits preserve them.

When an enabled entry points to a missing private key, guided configuration prompts for:

- `generate`: create a local Ed25519 key after showing the target path and getting confirmation.
- `disable`: save the server disabled so it cannot be used accidentally.
- `cancel`: leave the server list unchanged.

Generated keys are local only. The helper prints public-key installation guidance for the remote `authorized_keys` file; it does not edit the remote account, run `ssh-copy-id`, or bypass the first manual login. The user must log in once using a password, console, existing jump host, or administrator path, append the public key to `~/.ssh/authorized_keys`, then return to run `setup-key`, `check`, and `exec -- echo ok`.

## Repair an Existing Key Only

Use this when an existing server entry has the right host, port, username, and workdir, but `check` reports `key file not found` or `workspace-check` / `exec -- echo ok` fails with `Permission denied`, `publickey`, or another key-based authentication error.

First ask the user whether to run guided key-only repair. If they choose it:

```powershell
python <skill-dir>\scripts\remote_ssh.py configure-key --settings <settings> --server <id-or-name> --interactive
```

This flow can confirm or update `key_name`, generate a local Ed25519 key after explicit confirmation, print `authorized_keys` guidance, and ask the user to confirm that the public key was installed remotely. It verifies the configured `workdir` with the candidate key before any JSON write. On success it backs up the server list and writes only `key_name`, `validation`, `workspace_check`, and refreshed `software_scan`. On cancellation or verification failure, it leaves the server list unchanged.

Do not use this flow to correct host, port, username, workdir, enabled state, category, functions, or notes. Use full guided configuration for those fields.

The default workdir prompt is controlled by `ssh.default_workdir` and is `~/workspace` in the bundled settings. The written server entry still stores an explicit `workdir`.

## Use a Project Workdir

The server-list `workdir` is the server default. When a local task belongs to a specific project, use project config so the effective remote directory becomes project-specific without changing the shared server entry.

Project config discovery is automatic: from the current directory upward, the helper looks for `.erie-remote-ssh/project.local.json` and then `.erie-remote-ssh/project.json`. Use `project-show` before remote work when the current directory might already contain project config:

```powershell
python <skill-dir>\scripts\remote_ssh.py project-show --settings <settings> --server <id-or-name>
```

Use `project-init` to create project config:

```powershell
python <skill-dir>\scripts\remote_ssh.py project-init --settings <settings> --server <id-or-name> --project <project_id> --interactive
```

`project-init` checks the candidate remote directory using BatchMode SSH before writing local JSON. If the directory already exists, is declared by another local project config for the same server, or is otherwise a collision, the user must choose:

- `overwrite`: reuse the existing directory without deleting or clearing anything.
- `rename`: enter a new safe directory name or full `~/workspace/<name>` path, then recheck.
- `timestamp`: use `~/workspace/<project_id>-YYYYMMDD-HHMMSS`, then recheck.
- `cancel`: leave local config unchanged.

Use `--project-config <json>` to choose a specific project file, `--project <id>` for a temporary project context, or `--no-project` to force the server-list `workdir`.

## Use a Server List JSON

The default settings read skill-local `config/server_list.local.json`. Create it from `config/server_list.template.json`, use `init-config`, or pass an external JSON file with `--config`.

```powershell
python <skill-dir>\scripts\remote_ssh.py discover
python <skill-dir>\scripts\remote_ssh.py list
python <skill-dir>\scripts\remote_ssh.py check --server <id-or-name>
```

Use `--config <server-list.json>` to select a different server list for one command. Use `--settings <settings.json>` when path defaults, request directories, downloads, tools, or validation targets need to change together. Do not rely on a repository-level `ref` directory.

## Locate and Inspect Targets

After discovery reports `available`, run:

```powershell
python <skill-dir>\scripts\remote_ssh.py choices --settings <settings>
```

`choices` is the required server choice gate before connecting when the user has not already selected a server. It reads only the configured JSON, groups enabled SSH servers by category, lists explicit or inferred functions, includes cached software availability, and keeps host, username, port, and key details hidden by default.

Show the grouped choices to the user and wait for a server id or name before running `check`, `workspace-check`, `command`, `exec`, `inventory`, file operations, or request execution. If exactly one enabled server is present, still show it and confirm unless the user already named it. Use `choices --all` only when disabled servers matter; disabled entries are informational and require explicit enablement or override before remote access.

When the user gives only a host or IP, first narrow the local list:

```powershell
python <skill-dir>\scripts\remote_ssh.py choices --settings <settings> --host <host-or-ip>
```

If more than one login entry exists for that host, ask the user to choose an id/name or the intended port before any SSH command. Use `--show-sensitive` only in that explicit selection conversation, because it reveals username, port, and key path. Commands may also accept `host`, `user@host`, `host:port`, or `user@host:port`; ambiguous matches fail before connecting.

Use `list` when a compact table is useful:

```powershell
python <skill-dir>\scripts\remote_ssh.py list --settings <settings>
```

Keep the default redacted output unless the user explicitly needs full connection details.

## Local Precheck

Run this before connection, execution, file operations, or inventory:

```powershell
python <skill-dir>\scripts\remote_ssh.py check --settings <settings> --server <id-or-name>
```

Resolve these failures before connecting:

- Unsupported config version.
- Missing or empty required server fields.
- Unsupported `type`.
- Invalid port.
- Disabled server.
- Missing local private key file.
- Warnings from failed validation metadata or skipped/failed workspace checks.

If key-based login is not ready, run:

```powershell
python <skill-dir>\scripts\remote_ssh.py setup-key --settings <settings> --server <id-or-name>
```

`setup-key` only checks local private/public key file presence and gives passwordless SSH guidance. It does not generate keys, copy public keys, run `ssh-copy-id`, edit `authorized_keys`, or rewrite SSH client configuration. Passwordless SSH is not ready until the user completes one manual remote login path and installs the public key.

If `setup-key` reports a missing private key, offer `configure --interactive --server <id-or-name>` rather than only giving manual key-generation commands.

If the target is an existing entry and only the key or passwordless login needs repair, offer `configure-key --interactive --server <id-or-name>` instead. It only changes `key_name` and validation caches after successful verification.

Then verify the remote working directory:

```powershell
python <skill-dir>\scripts\remote_ssh.py workspace-check --settings <settings> --server <id-or-name>
```

`workspace-check` is the configuration validation write-back point. Without project context, it verifies the configured server `workdir`, backs up the selected server-list JSON, writes `validation.status: verified` and `workspace_check.status: ok` on success, then refreshes cached `software_scan`. With project context, it verifies the project effective workdir, writes project workspace status to the project config, updates global SSH validation, and refreshes server-level `software_scan` without overwriting global server `workspace_check`. On workspace failure, it does not create the remote project directory automatically.

`workdir` is the boundary for built-in file modifications. The helper does not claim to sandbox arbitrary shell commands. Internal request execution still performs a non-mutating workdir probe before acting.

## Scan and Query Software

Use `scan-software` after key changes, tool installs, or any time the user asks for a fresh software inventory:

```powershell
python <skill-dir>\scripts\remote_ssh.py scan-software --settings <settings> --server <id-or-name> --timeout 30
```

The command is read-only on the remote host and writes the cached `software_scan` object to the local server list. It scans the configured catalog from `config/defaults.json`, including Python, Conda, CUDA/nvcc, NVIDIA driver, GCC, G++, CMake, Vivado, Vitis, and Xilinx FPGA PCIe devices. The bundled catalog records multi-version installs from reviewed global paths such as `/usr/bin/gcc-[0-9]*`, `/usr/bin/cmake[0-9]*`, `/usr/local/cuda-*/bin/nvcc`, and Xilinx install roots. To avoid Windows command-length failures, the helper sends the full scan script over SSH stdin using a short remote shell command instead of embedding the entire script in a single SSH argument. `workspace-check` also runs this scan automatically after successful workdir validation.

The software catalog is trusted local configuration because its command templates are rendered into the remote scan script. Use only reviewed settings, and treat cached `raw_summary` as local operational data rather than public documentation.

Use `software` to answer cached availability questions:

```powershell
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name> --name vivado
```

If the cache is missing or a named tool was not scanned, refresh with `scan-software`.

When a tool has multiple detected installs, `software` prints one table row per install and `software --name <tool>` prints `version_entry` lines. Old caches may contain only one row; refresh with `scan-software` after upgrading to a catalog with a newer `catalog_version` or after any remote software install changes.

## File Operations

Read-only operations run directly after `check` and `workspace-check`:

```powershell
python <skill-dir>\scripts\remote_ssh.py file-list --settings <settings> --server <id-or-name> --path .
python <skill-dir>\scripts\remote_ssh.py file-stat --settings <settings> --server <id-or-name> --path src/main.py
python <skill-dir>\scripts\remote_ssh.py file-download --settings <settings> --server <id-or-name> --remote src/main.py --local copied/main.py
```

Remote paths must be relative to `workdir`. Absolute paths, drive paths, backslashes, empty paths, and `..` are rejected. Downloads write only inside `paths.downloads_dir`, which defaults to `${skill_dir}/reports/downloads`.

Upload sources must stay inside configured `paths.upload_roots`, which defaults to `${project_root}`. In a source checkout that resolves to the repository root; in an installed or release copy it resolves to the skill root. To upload from the current workspace or a data directory outside the skill project, use a custom settings file with explicit roots, for example `"upload_roots": ["${cwd}", "F:/work/data"]`. Do not use a filesystem root or the whole user home directory as an upload root.

Write operations create request files first:

```powershell
python <skill-dir>\scripts\remote_ssh.py request-upload --settings <settings> --server <id-or-name> --local tmp/file.txt --remote tmp/file.txt --reason "sync validation file"
python <skill-dir>\scripts\remote_ssh.py request-mkdir --settings <settings> --server <id-or-name> --path tmp/new-dir --reason "prepare workspace"
python <skill-dir>\scripts\remote_ssh.py request-delete --settings <settings> --server <id-or-name> --path tmp/file.txt --reason "cleanup"
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings> --request <request.json> --execute
```

With bundled defaults, these request files are written under `${skill_dir}/reports/requests`.

For sensitive local sources such as `.codex`, `.ssh`, private-key-like files, `.env`, `known_hosts`, `authorized_keys`, or system directories, both steps require explicit acknowledgement:

```powershell
python <skill-dir>\scripts\remote_ssh.py request-upload --settings <settings> --server <id-or-name> --local .codex/example.txt --remote tmp/example.txt --reason "user-approved sensitive upload" --confirm-sensitive-local-upload
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings> --request <request.json> --execute --confirm-sensitive-local-upload
```

Deletion is non-recursive unless `request-delete --recursive` is used. Recursive deletion is recorded in the request file and must still pass workdir boundary checks before execution.

## Generate a Manual SSH Command

Use this when the user wants to connect manually or inspect the exact command:

```powershell
python <skill-dir>\scripts\remote_ssh.py command --settings <settings> --server <id-or-name>
```

This command does not connect to the remote server. It is redacted by default; add `--show-sensitive` only when the user explicitly needs a runnable command.

## Run a Remote Command

Use `exec` for short explicit validation commands. Prefer request files for reviewed engineering commands:

```powershell
python <skill-dir>\scripts\remote_ssh.py exec --settings <settings> --server <id-or-name> --timeout 20 -- echo ok
python <skill-dir>\scripts\remote_ssh.py request-command --settings <settings> --server <id-or-name> --reason "check current directory" -- pwd
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings> --request <request.json> --execute
```

Guidelines:

- Keep validation commands short, such as `echo ok`, `pwd`, or `uname -a`.
- Avoid destructive commands unless the user clearly requested them.
- The helper enters the effective `workdir` before running the command. Project config overrides the server-list `workdir`; `--no-project` disables that override.
- The helper does not make arbitrary remote commands safe; it only passes the requested command to the remote shell.
- Request files include a lightweight risk summary for obvious shell risks such as `sudo`, `rm`, redirection, pipes, backgrounding, and absolute paths.
- Increase `--timeout` only when the remote command is expected to take longer.
- Do not use `--accept-new-host-key` unless the user accepts that OpenSSH may update `known_hosts`.
- If a user writes `exec --cmd "..."` or `request-command --cmd "..."`, retry with the delimiter form: `exec ... -- <remote command>`.

## Run and Inspect Detached Jobs

Use detached jobs for long Vitis, Vivado, Vitis HLS, build, emulation, board-run, or other commands where local SSH timeouts should not be treated as remote failure:

```powershell
python <skill-dir>\scripts\remote_ssh.py exec-detached --settings <settings> --server <id-or-name> --reason "run hw emulation" -- make hw_emu
python <skill-dir>\scripts\remote_ssh.py status --settings <settings> --server <id-or-name> --job <job-id>
python <skill-dir>\scripts\remote_ssh.py tail-log --settings <settings> --server <id-or-name> --job <job-id>
```

For reviewed commands, create a detached request and execute it explicitly:

```powershell
python <skill-dir>\scripts\remote_ssh.py request-command --settings <settings> --server <id-or-name> --reason "run hardware link" --detached -- make hw
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings> --request <request.json> --execute
```

Detached startup creates `workdir/.erie-remote-ssh/jobs/<job-id>/` on the remote host with `runner.sh`, `pid`, `command.txt`, `reason.txt`, `stdout.log`, `exit_code`, `started_at`, and `finished_at` files. The local manifest is written under `${skill_dir}/reports/jobs` by default. `tail-log` accepts only a job id and line count, not arbitrary remote paths.

Interpret local SSH timeout as a transport boundary. Use `status` and `tail-log` to decide whether the remote job is still running, succeeded, failed, or needs manual inspection.

## Collect Inventory

Use inventory for CPU/GPU/FPGA/software environment discovery:

```powershell
python <skill-dir>\scripts\remote_ssh.py inventory --settings <settings> --server <id-or-name> --timeout 30
```

The report includes hostname, kernel, CPU model, CPU thread count, NVIDIA GPU summary, Xilinx FPGA summary, Python, Conda, CUDA, GCC, G++, CMake, Vivado, and Vitis where available.

Missing tools are reported as `not detected`; this is not necessarily an error.

Inventory output is a report only. Use `scan-software` when a software result should be written back to the server list cache.

## Troubleshooting

- `OpenSSH client 'ssh' was not found on PATH`: Install or enable OpenSSH Client locally, then retry.
- `generated command line was too long on Windows`: The helper tried to start SSH with an oversized command. Keep long remote scripts on stdin transport paths such as the bundled software scan flow instead of embedding them directly in the SSH argument list.
- `key file not found`: Confirm `default_key_dir` and `key_name`; do not create or move keys unless the user asks.
- `Permission denied`: Confirm username, key, and server-side authorized keys.
- `Connection timed out`: Confirm VPN/network reachability, host, port, and server state.
- `Host key verification failed`: Ask the user before changing known-hosts behavior.
- `Server is disabled`: Do not override unless the user explicitly says to use that disabled entry.
- `Timeout must be a positive integer`: Retry with a positive `--timeout` value.
- `software_scan_status: failed`: Resolve SSH/key/connectivity errors, then run `scan-software` again.

## Output Handling

- Summarize connection failures without repeating sensitive values.
- Do not paste full generated commands into public-facing documents.
- Treat inventory output as a report; use cached `software` output for install-status questions unless the user asks for a fresh scan.
