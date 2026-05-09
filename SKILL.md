---
name: erie-remote-ssh
description: "Use when Codex is working on FPGA, RTL, Verilog/SystemVerilog, HLS, C++/Python, neural network, GPU acceleration, FPGA acceleration, application testing, or skill testing tasks and the user explicitly needs SSH or remote server work: discover/add/list SSH servers, resolve same-host multi-account or multi-port targets, read or validate server-list JSON, configure passwordless/key-based SSH guidance, check a remote ~/workspace, operate files inside workdir, review/run remote commands, troubleshoot SSH, or collect remote inventory."
---

# Erie Remote SSH

## Core Rule

Discover configured servers first, validate locally second, keep built-in file operations inside `workdir`, and connect only when the user or the requested workflow explicitly requires real SSH access.

## Trigger Policy

Use this skill for conservative remote SSH work in FPGA, RTL, Verilog/SystemVerilog, HLS, C++/Python, neural network, GPU acceleration, FPGA acceleration, application testing, and skill testing workflows when the user explicitly needs SSH or remote server operations.

Use it to discover, add, or list SSH servers; parse and validate server-list JSON; inspect key-based and passwordless SSH readiness; check the remote `~/workspace`; operate files inside `workdir`; review and execute request files for remote writes or commands; troubleshoot SSH failures; and collect remote inventory.

Do not use it for purely local development, local file editing, ordinary FPGA/RTL/HLS/GPU discussion without remote intent, non-SSH protocols, or tasks that require directly modifying `~/.ssh` or bypassing request review.

## Update / Reports Preservation

Before updating or replacing this skill from GitHub, a local directory, a release artifact, or another source, inspect the target `erie-remote-ssh/reports` directory. If it exists, ask the user whether to clear it or preserve it. Preserve `reports` by default unless the user explicitly confirms cleanup. `reports` is a local runtime artifact root and is not managed by git.

Use `scripts/remote_ssh.py` for deterministic operations whenever possible:

```powershell
python <skill-dir>\scripts\remote_ssh.py list --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py discover --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py choices --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py choices --settings <settings.json> --host <host-or-ip>
python <skill-dir>\scripts\remote_ssh.py configure --settings <settings.json> --interactive
python <skill-dir>\scripts\remote_ssh.py add-server --settings <settings.json> --interactive
python <skill-dir>\scripts\remote_ssh.py update-server --settings <settings.json> --server <id-or-name> --interactive
python <skill-dir>\scripts\remote_ssh.py configure-key --settings <settings.json> --server <id-or-name> --interactive
python <skill-dir>\scripts\remote_ssh.py setup-key --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py check --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py workspace-check --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py file-list --settings <settings.json> --server <id-or-name> --path <relative>
python <skill-dir>\scripts\remote_ssh.py request-upload --settings <settings.json> --server <id-or-name> --local <local-path> --remote <relative> --reason <text>
python <skill-dir>\scripts\remote_ssh.py request-command --settings <settings.json> --server <id-or-name> --reason <text> -- <remote command>
python <skill-dir>\scripts\remote_ssh.py run-request --settings <settings.json> --request <request.json> --execute
python <skill-dir>\scripts\remote_ssh.py command --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py exec --settings <settings.json> --server <id-or-name> -- <remote command>
python <skill-dir>\scripts\remote_ssh.py scan-software --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py software --settings <settings.json> --server <id-or-name> [--name <tool>]
python <skill-dir>\scripts\remote_ssh.py inventory --settings <settings.json> --server <id-or-name>
```

## Hard Checkpoints

Execute these checkpoints in order. Do not skip a checkpoint unless it is irrelevant to the user's requested task.

1. Read settings: prefer user-provided `--settings`; otherwise use the skill's default settings.
2. Discover configuration: run `discover` before assuming a server list or usable SSH target exists.
3. Choose configuration mode before changes: before adding or modifying server configuration, fixing a missing private key, repairing failed passwordless SSH, creating an initial server list, or handling a list with no enabled servers, ask the user in the conversation whether they want manual instructions, the guided configuration script, or cancellation. Do not launch `configure --interactive`, `add-server --interactive`, `update-server --interactive`, or a platform wrapper until the user explicitly chooses guided script configuration. Do not launch `configure-key --interactive` until the user explicitly chooses guided script configuration for key-only repair.
4. Present choices: if the server list exists and the user has not already named a server, run `choices` and show every selectable server grouped by category and function; wait for the user to choose an id or name before remote access. If the user names only a host/IP, run `choices --host <host>`; when multiple logins exist for that host, ask which id or port to use before connecting. If only one enabled server exists, still present it and confirm unless the user already selected it.
5. List targets: use `list` for compact tabular inspection; use `--all` only when disabled servers matter.
6. Prepare keys when needed: run `setup-key` to inspect local key files. If the private key is missing for an existing server, or `workspace-check` / `exec -- echo ok` fails with `Permission denied`, `publickey`, or another key-based authentication error, ask whether to run the guided key-only repair flow. `configure-key --interactive` only changes key_name and validation caches after successful verification; it must not change host, port, username, workdir, enabled, category, functions, or notes. For full server edits, use `configure --interactive` only after explicit user choice.
7. Check target: run `check` before any `command`, `exec`, or `inventory` operation.
8. Scan software: `add-server --interactive` runs a mandatory read-only software scan for enabled servers; use `scan-software` to refresh cached tool availability.
9. Check workspace: run `workspace-check` after passwordless SSH is ready. It backs up the selected server-list JSON, writes `validation` and `workspace_check`, and refreshes cached `software_scan`.
10. Read directly: `file-list`, `file-stat`, and `file-download` may run directly after checks.
11. Request writes: use `request-upload`, `request-mkdir`, `request-delete`, or `request-command` before modifications or arbitrary commands.
12. Execute explicitly: use `run-request --execute` only after reviewing the request and risks.
13. Review output: inspect warnings, redaction, side effects, and failures before reporting results.

## Sensitive Output

Default output is redacted. Use `--show-sensitive` only when the user explicitly needs runnable connection details.

`command` defaults to a redacted SSH command shape. Add `--show-sensitive` to print a runnable command.

`--accept-new-host-key` may update the user's SSH `known_hosts` file. Do not use it unless the user explicitly accepts that external side effect.

If the requested server target, write action, host-key change, or other sensitive side effect is ambiguous, ask the user before connecting or executing.

## Remote Execution

`exec` runs the provided command through the remote user's shell after entering the configured `workdir`. It does not make arbitrary remote commands safe. Prefer `request-command` plus `run-request --execute` for engineering workflows that need review.

## File Operations

Built-in file operations accept remote paths relative to `workdir` only. They reject absolute paths, drive paths, and parent traversal. Write, delete, upload, mkdir, and arbitrary command operations require request files and `run-request --execute`.

Uploads also validate the local source. `request-upload --local` must resolve inside configured `paths.upload_roots`, which defaults to `${project_root}`. Use a custom settings file with explicit upload roots, such as a current workspace or data directory, before uploading files outside the skill project. Sensitive local sources such as `.codex`, `.ssh`, private keys, `.env`, `known_hosts`, `authorized_keys`, and system directories require `--confirm-sensitive-local-upload` on both `request-upload` and `run-request --execute`, plus a clear `--reason`.

## Configuration Discovery

`discover` only inspects configured JSON. It does not scan networks, probe arbitrary hosts, generate SSH keys, or modify SSH client state.

`configure --interactive` is the configuration gate. Use it after the user has explicitly chosen guided script configuration in the conversation. Inside the CLI, the user must explicitly enter `script`, `manual`, or `cancel`; pressing Enter does not choose a default. Guided mode can initialize a missing list, add a server, update a server, and generate a local SSH key only after showing the target path and receiving explicit confirmation. It does not install public keys on the remote host.

`choices` is a local selection gate. It groups enabled SSH servers by `category`, shows explicit or inferred `functions`, cached software availability, validation status, and workspace status. Use `choices --host <host>` when the user gives an IP or hostname and the same machine may have multiple usernames or ports. It does not connect, scan, or write the server list.

`add-server --interactive` uses a grouped wizard for connection, key/workdir, and metadata fields, then shows a redacted summary before writing. It supports `category`, semicolon/comma-separated `functions`, and `notes`. Treat it as a lower-level maintenance or validation entry point; for normal agent-guided configuration, use `configure --interactive` after the user chooses guided script mode. When the host already exists, the prompt lists existing usernames and ports so the user can confirm another login entry or cancel a duplicate. If the configured private key is missing, it prompts to generate the key, save the server disabled, or cancel before writing an enabled unusable entry. The server entry is retained with `software_scan.status: failed` if the SSH scan cannot complete.

`update-server --interactive` uses a field menu. Choose `show`, one editable field, `all`, `done`, or `cancel`; only selected fields are prompted. `done` shows a redacted summary and requires `save_server_record` before writing. `configure --interactive` lists configured servers with numbers before `update`, and accepts a number, id, or name. Editing host, port, username, key_name, or workdir clears validation/workspace/software caches; editing only metadata preserves them. Connection/key/workdir changes and enabling a server use the same missing-key gate as `add-server` and refresh cached software for enabled connection changes; metadata-only edits do not force a key check or scan.

`configure-key --interactive` repairs an existing entry's local key reference and passwordless validation only. Use it after the user explicitly chooses guided script configuration for a missing private key or key-based authentication failure. It may prompt for `key_name`, generate a local Ed25519 key after explicit confirmation, and print `authorized_keys` guidance. It does not install public keys remotely, run `ssh-copy-id`, rewrite SSH client configuration, or write the server list until the candidate key verifies the remote `workdir`. After verification succeeds, it backs up the selected server-list JSON and writes only `key_name`, `validation`, `workspace_check`, and refreshed `software_scan`.

Default configuration reads and writes skill-local `config/server_list.local.json` through `config/defaults.json`. Use `config/server_list.template.json` as the non-sensitive template, or use `--config <server-list.json>` when a user provides an explicit alternate server-list JSON.

`add-server --interactive` stores an explicit `workdir` on every server entry. The default prompt value comes from `ssh.default_workdir`, which defaults to `~/workspace`.

`setup-key` checks local private/public key file presence and prints passwordless SSH setup guidance. It does not generate keys, copy public keys, edit `authorized_keys`, run `ssh-copy-id`, or rewrite SSH client configuration; use `configure-key --interactive` for existing-server key-only repair when the user chooses guided local key generation. Passwordless handoff still requires the user to log in to the remote account once with a password, console, existing jump host, or administrator path and append the public key to `~/.ssh/authorized_keys`; only after that should Codex run `check` and `exec -- echo ok`.

`workspace-check` connects to the selected server, verifies the configured `workdir`, creates a `.bak.<timestamp>` backup next to the selected server-list JSON, writes `validation` and `workspace_check`, then refreshes `software_scan`. With bundled defaults, this writes `config/server_list.local.json`; `--config` or custom settings write to their resolved server-list file.

Use `software --server <id-or-name>` or `software --server <id-or-name> --name <tool>` to answer whether cached software such as Python, Conda, CUDA, GCC/G++, CMake, Vivado, or Vitis is installed. Use `scan-software` when the user needs a fresh result, suspects tool installs changed, or needs complete multi-version details.

## Safety

- Never silently modify `~/.ssh`, generate keys, rewrite SSH client configuration, run `ssh-copy-id`, or edit remote `authorized_keys`. Key generation is allowed only inside the guided configuration or guided key-only repair flow after showing the target path and receiving explicit user confirmation.
- Never copy real hostnames, usernames, key names, ports, or inventory snapshots into public docs unless explicitly required.
- Never depend on a repository-level `ref` directory; server-list templates and local config belong under this skill's `config` directory.
- Prefer short timeouts and clear error summaries.
- Use argument-list subprocess execution. Avoid shell string execution for SSH calls.
- If a server is marked disabled, do not connect unless the user explicitly overrides the safety check.

## References

- Read `references/server-list-schema.md` when validating or extending the JSON format.
- Read `references/configuration.md` when changing settings, paths, request/download directories, validation targets, or tool locations.
- Read `references/workflows.md` for discovery, configuration, file operations, request review, execution, inventory, and troubleshooting details.
- Read `references/review-checklist.md` before claiming the skill or a remote operation is fully validated.
