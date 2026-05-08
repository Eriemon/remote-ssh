# Erie Remote SSH Review Checklist

Use this checklist before declaring an Erie Remote SSH task or skill change fully validated.

## Skill Structure

- `SKILL.md` frontmatter contains only `name` and `description`.
- The public repository directory may be `remote-ssh`; `SKILL.md` name and `agents/openai.yaml` default prompt still use `erie-remote-ssh`.
- The description states both capability and trigger conditions.
- The body is concise and points to references for details.
- Every file under `references/` is directly linked from `SKILL.md`.
- Public repository docs such as `README.md`, `README-CN.md`, `CONTRIBUTING.md`, and `SECURITY.md` stay at the repository root; unrelated documentation is not added.
- Paths, validation targets, and tool locations are settings-driven rather than hardcoded in scripts.
- Software inventory probes and Xilinx install roots are settings-driven through `inventory.software_catalog`.
- Discovery and add-server workflows are represented in `SKILL.md` without bloating the skill body.
- Platform wrappers stay thin and delegate to `scripts/remote_ssh.py`.
- Platform-specific `.bat`, `.sh`, and `.ps1` wrappers live under `scripts/bat`, `scripts/shell`, or `scripts/powershell`; only Python helpers live directly in `scripts`.
- Runtime configuration does not depend on a repository-level `ref` directory.
- `config/server_list.template.json` exists and contains only placeholder, non-sensitive values.
- The public repository intentionally does not include a bundled `.gitignore`; release review confirms local server lists, backups, request/download/tmp/log output, and other operational artifacts are absent from commits.

## Safety and Privacy

- Default CLI output redacts host, username, port, key name, and key path.
- Full connection details require an explicit `--show-sensitive` flag.
- Default SSH arguments do not accept or write new host keys.
- Any use of `--accept-new-host-key` is user-approved because it may update `known_hosts`.
- The workflow does not modify `~/.ssh`, generate keys, or rewrite SSH config.
- Inventory output is treated as a report and does not update the source JSON; `scan-software` is the explicit write-back path for `software_scan`.
- `discover` reads configuration only and does not scan networks or probe unknown hosts.
- `add-server --interactive` does not generate keys, modify SSH config, or scan networks; enabled additions connect only for the mandatory read-only software scan.
- Failed mandatory scans are cached as `software_scan.status: failed` without discarding the server entry.
- `setup-key` does not generate keys, copy public keys, edit `authorized_keys`, modify `~/.ssh`, or rewrite SSH config.
- Passwordless verification keeps `BatchMode=yes` in default SSH options.
- Server-list backups are ignored by git and do not expose sensitive details through committed files.
- Request files do not contain real host, username, port, key name, or key path values.
- `choices` output is redacted by default and does not connect, scan, or write the server list.
- Download and request directories are ignored or kept out of commits.

## Schema and Selection

- Config root is a JSON object with supported `version`.
- `servers` is an array and every entry is an object.
- Required server fields are present and non-empty where applicable.
- `enabled` is a JSON boolean, not a coerced string or number.
- Server selectors fail clearly on unknown or ambiguous matches.
- `choices` groups servers by `category`, shows explicit or inferred `functions`, and treats missing metadata as compatible legacy input.
- Disabled servers require explicit override.
- `init-config` creates the v1 empty list and refuses overwrite unless `--force` is explicit.
- Interactive add rejects duplicate id/name selectors, invalid ports, empty required fields, and non-boolean enabled values.
- Interactive add runs and caches a software scan for enabled servers.
- Remote file paths reject empty paths, absolute paths, drive paths, backslashes, and parent traversal.
- Built-in write/delete/upload operations resolve remote targets under `workdir` again at execution time.

## Execution

- `list` defaults to enabled servers and redacted output.
- `choices` runs after discovery and before remote access when the user has not already selected a server.
- `check` runs before `command`, `exec`, or `inventory`.
- `command` does not connect.
- `exec` and `inventory` use positive timeouts.
- `scan-software` uses a positive timeout and writes only the local `software_scan` cache.
- `software` reads cached install status and does not connect to the remote host.
- Remote commands are short and non-destructive unless the user clearly requested otherwise.
- Missing optional inventory tools degrade to `not detected`.
- `run-request` requires `--execute` and re-runs local/server checks before acting.
- Recursive delete requires an explicit request flag.

## Validation

- Run the standard skill validator.
- Run positive CLI tests for choices, list, check, command, exec, software, scan-software, and inventory when network access is allowed.
- Run choices tests for category grouping, JSON output, legacy missing metadata, disabled visibility with `--all`, redaction, and no server-list mutation.
- Run negative CLI tests for malformed JSON, unsupported version, invalid server entries, invalid fields, disabled targets, missing keys, and invalid timeouts.
- Run configuration tests for default settings, copied settings, environment-variable paths, and CLI overrides.
- Run discovery and add-server tests for missing lists, empty lists, enabled servers, backups, duplicate entries, invalid ports, and empty required fields.
- Run software catalog tests for duplicate ids, invalid directory scans, cached named-tool queries, scan write-back, and add-server scan write-back.
- Run passwordless setup tests for existing private/public key files and missing-key guidance.
- Run configure script tests for Windows batch and POSIX shell where the shell runner is available.
- Run request and path-boundary tests for upload, mkdir, delete, command risk summaries, invalid paths, and missing `--execute`.
- Run real SSH file tests for workspace check, upload, list, stat, download, command request, and cleanup when network access is allowed.
- Run wrapper tests for `.bat`, `.ps1`, and `.sh` entry points where the runner exists.
- Run isolated no-ref validation from a temporary copy that does not contain a repository-level `ref` directory.
- Confirm `--with-ssh` requires an explicit real `--server-list`; without one, only offline skill development confidence can be claimed.
- Confirm `git status` is clean after validation.
