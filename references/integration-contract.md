# Erie Remote SSH Integration Contract

## Contents

- Purpose
- Dependency Gate
- Selection Gate
- Command Contract
- Failure Contract
- Long-Running Jobs

## Purpose

This contract is for downstream skills such as `ResearchAssistant`, `VivadoDeveloper`, `VitisDeveloper`, `VerilogGenerator`, and `HLSGenerator` that depend on `erie-remote-ssh` for remote execution instead of implementing ad hoc SSH behavior.

## Dependency Gate

- If `erie-remote-ssh` is not installed, downstream skills must stop and tell the user that the dependency is missing.
- If settings or server-list JSON are missing, downstream skills must stop and route the user to `discover`, `configure`, `init-config`, or `add-server --interactive`.
- Do not let downstream skills guess hidden paths for `server_list.local.json`; they should pass `--settings` or `--config` explicitly when the path is known.

Recommended blocked message shape:

```text
status: blocked
blocked_reason: missing_remote_ssh_dependency | missing_server_list | no_enabled_ssh
next_action: install the skill, configure a server list, or select a different execution path
```

## Selection Gate

- Always run `discover` before assuming a usable SSH target exists.
- If the user did not already name a server id or name, run `choices` and wait for an explicit selection.
- If the user only gives a host or IP, run `choices --host <host>` and require explicit disambiguation when multiple logins match the same machine.
- Do not connect to a disabled server unless the user explicitly overrides the safety boundary.

## Command Contract

Downstream skills should treat these fields as stable machine-readable output when parsing CLI text:

- `status`
- `server_id`
- `server_name`
- `workdir_status`
- `software_cache_status`
- `message`
- `next_action`

Preferred lifecycle:

1. `discover`
2. `choices`
3. `check`
4. `workspace-check`
5. `request-*` or `exec` / `exec-detached`
6. `run-request`, `status`, `tail-log`

## Failure Contract

- Authentication failures should steer the user toward `configure-key --interactive` instead of generic SSH advice.
- Missing workspace or project workdir failures should say whether the problem is global server `workdir` or project-specific `effective_workdir`.
- Missing software cache should tell the caller to run `scan-software` or `workspace-check`.
- Request-driven actions should keep the next step explicit: review request, confirm sensitive upload, or run `run-request --execute`.

## Long-Running Jobs

- Prefer `exec-detached` or `request-command --detached` for Vivado, Vitis, HLS, emulation, synthesis, or board-validation flows.
- Treat local timeout boundaries as transport boundaries, not proof of remote job failure.
- Resume with `status --job <job-id>` and `tail-log --job <job-id>`.
