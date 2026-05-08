---
name: erie-remote-ssh
description: "Use when Codex is working on FPGA, RTL, Verilog/SystemVerilog, HLS, C++/Python, neural network, GPU acceleration, FPGA acceleration, application testing, or skill testing tasks and the user explicitly needs SSH or remote server work: discover/add/list SSH servers, read or validate server-list JSON, configure passwordless/key-based SSH guidance, check a remote ~/workspace, operate files inside workdir, review/run remote commands, troubleshoot SSH, or collect remote inventory."
---

# Erie Remote SSH

## Core Rule

Discover configured servers first, validate locally second, keep built-in file operations inside `workdir`, and connect only when the user or the requested workflow explicitly requires real SSH access.

## Trigger Policy

Use this skill for conservative remote SSH work in FPGA, RTL, Verilog/SystemVerilog, HLS, C++/Python, neural network, GPU acceleration, FPGA acceleration, application testing, and skill testing workflows when the user explicitly needs SSH or remote server operations.

Use it to discover, add, or list SSH servers; parse and validate server-list JSON; inspect key-based and passwordless SSH readiness; check the remote `~/workspace`; operate files inside `workdir`; review and execute request files for remote writes or commands; troubleshoot SSH failures; and collect remote inventory.

Do not use it for purely local development, local file editing, ordinary FPGA/RTL/HLS/GPU discussion without remote intent, non-SSH protocols, or tasks that require directly modifying `~/.ssh` or bypassing request review.

Use `scripts/remote_ssh.py` for deterministic operations whenever possible:

```powershell
python <skill-dir>\scripts\remote_ssh.py list --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py discover --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py choices --settings <settings.json>
python <skill-dir>\scripts\remote_ssh.py add-server --settings <settings.json> --interactive
python <skill-dir>\scripts\remote_ssh.py setup-key --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py check --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py workspace-check --settings <settings.json> --server <id-or-name>
python <skill-dir>\scripts\remote_ssh.py file-list --settings <settings.json> --server <id-or-name> --path <relative>
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
3. Configure when needed: if discovery reports no server list or no enabled SSH server, use `scripts/bat/config/configure_remote_ssh.bat`, `scripts/shell/config/configure_remote_ssh.sh`, `scripts/powershell/config/configure_remote_ssh.ps1`, `init-config`, or `add-server --interactive`.
4. Present choices: if the server list exists and the user has not already named a server, run `choices` and show every selectable server grouped by category and function; wait for the user to choose an id or name before remote access. If only one enabled server exists, still present it and confirm unless the user already selected it.
5. List targets: use `list` for compact tabular inspection; use `--all` only when disabled servers matter.
6. Prepare keys when needed: run `setup-key` to inspect local key files and get passwordless SSH setup guidance without modifying `~/.ssh`.
7. Check target: run `check` before any `command`, `exec`, or `inventory` operation.
8. Scan software: `add-server --interactive` runs a mandatory read-only software scan for enabled servers; use `scan-software` to refresh cached tool availability.
9. Check workspace: run `workspace-check` before file operations or write requests.
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

## Configuration Discovery

`discover` only inspects configured JSON. It does not scan networks, probe arbitrary hosts, generate SSH keys, or modify SSH client state.

`choices` is a local selection gate. It groups enabled SSH servers by `category`, shows explicit or inferred `functions`, cached software availability, validation status, and workspace status. It does not connect, scan, or write the server list.

`add-server --interactive` writes a server entry to the configured server list, creates a local backup when replacing an existing list, performs schema validation, then runs a mandatory read-only software scan for enabled servers. The server entry is retained with `software_scan.status: failed` if the SSH scan cannot complete.

Default configuration reads skill-local `config/server_list.local.json` through `config/defaults.json`. Use `config/server_list.template.json` as the non-sensitive template, or use `--config <server-list.json>` when a user provides an explicit alternate server-list JSON.

`add-server --interactive` stores an explicit `workdir` on every server entry. The default prompt value comes from `ssh.default_workdir`, which defaults to `~/workspace`.

`setup-key` checks local private/public key file presence and prints passwordless SSH setup guidance. It does not generate keys, copy public keys, edit `authorized_keys`, or rewrite SSH client configuration.

Use `software --server <id-or-name>` or `software --server <id-or-name> --name <tool>` to answer whether cached software such as Python, Conda, CUDA, GCC/G++, CMake, Vivado, or Vitis is installed. Use `scan-software` when the user needs a fresh result.

## Safety

- Never modify `~/.ssh`, generate keys, or rewrite SSH client configuration.
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
