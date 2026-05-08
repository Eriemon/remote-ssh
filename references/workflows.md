# Erie Remote SSH Workflows

Use these workflows after the `erie-remote-ssh` skill triggers. Prefer the helper CLI because it applies consistent validation, quoting, timeouts, and output redaction.

## Contents

- Progressive Loading
- Discover Configuration
- Add a Server When None Is Available
- Use a Server List JSON
- Locate and Inspect Targets
- Local Precheck
- Scan and Query Software
- File Operations
- Generate a Manual SSH Command
- Run a Remote Command
- Collect Inventory
- Troubleshooting
- Output Handling

## Progressive Loading

- Read `server-list-schema.md` when schema fields, compatibility, or validation failures matter.
- Read `configuration.md` when settings, paths, validation targets, or tool locations matter.
- Read `review-checklist.md` before declaring confidence, reviewing a change, or reporting that validation is complete.
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

`discover --json` gives stable machine-readable fields for automation scripts. Discovery reads configured JSON only; it does not scan networks or probe unknown hosts.

## Add a Server When None Is Available

Use the automation scripts when the user asks how to find or add remote server configuration:

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
python <skill-dir>\scripts\remote_ssh.py init-config --settings <settings>
python <skill-dir>\scripts\remote_ssh.py add-server --settings <settings> --interactive
python <skill-dir>\scripts\remote_ssh.py discover --settings <settings>
```

Adding an enabled server writes the configured server list, creates a backup before replacing an existing file, validates schema, then runs a mandatory read-only software scan over SSH. The scan must complete before the add flow reports `added:`; failures are cached as `software_scan.status: failed`.

The default workdir prompt is controlled by `ssh.default_workdir` and is `~/workspace` in the bundled settings. The written server entry still stores an explicit `workdir`.

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

`setup-key` only checks local private/public key file presence and gives passwordless SSH guidance. It does not generate keys, copy public keys, edit `authorized_keys`, or rewrite SSH client configuration.

Then verify the remote working directory:

```powershell
python <skill-dir>\scripts\remote_ssh.py workspace-check --settings <settings> --server <id-or-name>
```

`workdir` is the boundary for built-in file modifications. The helper does not claim to sandbox arbitrary shell commands.

## Scan and Query Software

Use `scan-software` after key changes, tool installs, or any time the user asks for a fresh software inventory:

```powershell
python <skill-dir>\scripts\remote_ssh.py scan-software --settings <settings> --server <id-or-name> --timeout 30
```

The command is read-only on the remote host and writes the cached `software_scan` object to the local server list. It scans the configured catalog from `config/defaults.json`, including Python, Conda, CUDA/nvcc, NVIDIA driver, GCC, G++, CMake, Vivado, Vitis, and Xilinx FPGA PCIe devices.

The software catalog is trusted local configuration because its command templates are rendered into the remote scan script. Use only reviewed settings, and treat cached `raw_summary` as local operational data rather than public documentation.

Use `software` to answer cached availability questions:

```powershell
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py software --settings <settings> --server <id-or-name> --name vivado
```

If the cache is missing or a named tool was not scanned, refresh with `scan-software`.

## File Operations

Read-only operations run directly after `check` and `workspace-check`:

```powershell
python <skill-dir>\scripts\remote_ssh.py file-list --settings <settings> --server <id-or-name> --path .
python <skill-dir>\scripts\remote_ssh.py file-stat --settings <settings> --server <id-or-name> --path src/main.py
python <skill-dir>\scripts\remote_ssh.py file-download --settings <settings> --server <id-or-name> --remote src/main.py --local copied/main.py
```

Remote paths must be relative to `workdir`. Absolute paths, drive paths, backslashes, empty paths, and `..` are rejected. Downloads write only inside `paths.downloads_dir`.

Write operations create request files first:

```powershell
python <skill-dir>\scripts\remote_ssh.py request-upload --settings <settings> --server <id-or-name> --local tmp/file.txt --remote tmp/file.txt --reason "sync validation file"
python <skill-dir>\scripts\remote_ssh.py request-mkdir --settings <settings> --server <id-or-name> --path tmp/new-dir --reason "prepare workspace"
python <skill-dir>\scripts\remote_ssh.py request-delete --settings <settings> --server <id-or-name> --path tmp/file.txt --reason "cleanup"
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings> --request <request.json> --execute
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
- The helper enters the server `workdir` before running the command when `workdir` is configured.
- The helper does not make arbitrary remote commands safe; it only passes the requested command to the remote shell.
- Request files include a lightweight risk summary for obvious shell risks such as `sudo`, `rm`, redirection, pipes, backgrounding, and absolute paths.
- Increase `--timeout` only when the remote command is expected to take longer.
- Do not use `--accept-new-host-key` unless the user accepts that OpenSSH may update `known_hosts`.

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
