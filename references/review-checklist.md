# Erie Remote SSH Review Checklist

Use this checklist before declaring an Erie Remote SSH task or skill change fully validated.

## Skill Structure

- `SKILL.md` frontmatter contains only `name` and `description`.
- The skill directory name, `SKILL.md` name, and `agents/openai.yaml` default prompt all use `erie-remote-ssh`.
- The description states both capability and trigger conditions.
- The body is concise and points to references for details.
- Every file under `references/` is directly linked from `SKILL.md`.
- No README, installation guide, changelog, or unrelated documentation is added inside the skill.
- Paths, validation targets, and tool locations are settings-driven rather than hardcoded in scripts.
- Software inventory probes, multi-version PATH scans, executable globs, and Xilinx install roots are settings-driven through `inventory.software_catalog`.
- Discovery and add-server workflows are represented in `SKILL.md` without bloating the skill body.
- The configuration gate is documented at both levels: the agent asks the user in conversation first, and the CLI requires explicit manual/script/cancel input before any server-list mutation.
- Platform wrappers stay thin and delegate to `scripts/remote_ssh.py`.
- Platform-specific `.bat`, `.sh`, and `.ps1` wrappers live under `scripts/bat`, `scripts/shell`, or `scripts/powershell`; only Python helpers live directly in `scripts`.
- Runtime configuration does not depend on a repository-level `ref` directory.
- `config/server_list.template.json` exists and contains only placeholder, non-sensitive values.
- Skill-local `.gitignore` ignores `config/server_list.local.json`, backups, request/download/tmp/log output, and complements repository-level ignores.

## Safety and Privacy

- Default CLI output redacts host, username, port, key name, and key path.
- Full connection details require an explicit `--show-sensitive` flag.
- Default SSH arguments do not accept or write new host keys.
- Any use of `--accept-new-host-key` is user-approved because it may update `known_hosts`.
- The workflow does not silently modify `~/.ssh`, generate keys, or rewrite SSH config.
- Guided key generation shows the target path, requires confirmation, refuses overwrite, explains the required one-time manual remote login, and never installs public keys remotely.
- Inventory output is treated as a report and does not update the source JSON; `scan-software` is the explicit write-back path for `software_scan`.
- `discover` reads configuration only and does not scan networks or probe unknown hosts.
- `add-server --interactive` does not generate keys without explicit user confirmation, modify SSH config, or scan networks; enabled additions connect only for the mandatory read-only software scan.
- `configure --interactive`, `add-server --interactive`, and `update-server --interactive` ask how to handle a missing private key before writing an enabled unusable entry.
- Failed mandatory scans are cached as `software_scan.status: failed` without discarding the server entry.
- `workspace-check` creates a server-list backup, writes validation/workspace metadata, and refreshes `software_scan` only when invoked explicitly.
- `setup-key` does not generate keys, copy public keys, run `ssh-copy-id`, edit `authorized_keys`, modify `~/.ssh`, or rewrite SSH config.
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
- Same-host multi-login entries are allowed, and ambiguous host/user@host selectors require choosing an id/name or port before SSH.
- `choices` groups servers by `category`, shows explicit or inferred `functions`, and treats missing metadata as compatible legacy input.
- Disabled servers require explicit override.
- `init-config` creates the v1 empty list and refuses overwrite unless `--force` is explicit.
- `configure --interactive` offers manual, script, and cancel modes without a default before changing server configuration.
- Existing-server guided configuration shows a redacted server summary, then requires an explicit add/update/cancel action without a default.
- Interactive add rejects duplicate id/name selectors, invalid ports, empty required fields, and non-boolean enabled values.
- Interactive add warns on same-host entries and defaults to cancelling exact host+username+port duplicates.
- Interactive add runs and caches a software scan for enabled servers.
- Interactive update preserves the selected server id, validates edited fields, creates a backup, and refreshes cached software for enabled entries.
- Remote file paths reject empty paths, absolute paths, drive paths, backslashes, and parent traversal.
- Built-in write/delete/upload operations resolve remote targets under `workdir` again at execution time.
- Upload local source paths resolve under configured `paths.upload_roots`; request execution revalidates the recorded upload root and relative path.
- Sensitive local upload sources require `--confirm-sensitive-local-upload` and a non-empty reason at request creation and execution.

## Execution

- `list` defaults to enabled servers and redacted output.
- `choices` runs after discovery and before remote access when the user has not already selected a server.
- `check` runs before `command`, `exec`, or `inventory`.
- `command` does not connect.
- `exec` and `inventory` use positive timeouts.
- `scan-software` uses a positive timeout and writes only the local `software_scan` cache.
- `workspace-check` uses a positive timeout, writes `validation` / `workspace_check`, and auto-refreshes `software_scan` after successful workspace validation.
- `software` reads cached install status, prints multi-version rows from cached `versions`, and does not connect to the remote host.
- Remote commands are short and non-destructive unless the user clearly requested otherwise.
- Missing optional inventory tools degrade to `not detected`.
- `run-request` requires `--execute` and re-runs local/server checks before acting.
- Recursive delete requires an explicit request flag.

## Validation

- Run the standard skill validator.
- Run positive CLI tests for choices, list, check, command, exec, software, scan-software, and inventory when network access is allowed.
- Run choices tests for category grouping, JSON output, legacy missing metadata, disabled visibility with `--all`, same-host `--host` filtering, redaction, and no server-list mutation.
- Run negative CLI tests for malformed JSON, unsupported version, invalid server entries, invalid fields, disabled targets, missing keys, and invalid timeouts.
- Run configuration tests for default settings, copied settings, environment-variable paths, and CLI overrides.
- Run discovery and add-server tests for missing lists, empty lists, enabled servers, backups, duplicate entries, same-host prompts, invalid ports, and empty required fields.
- Run configure/update tests for manual mode, script mode, cancel mode, explicit no-default prompts, missing-key generate/disable/cancel branches, fake `ssh_keygen`, and server-list backups.
- Run software catalog tests for duplicate ids, invalid `path_scan`, unsafe executable globs, invalid directory scans, cached named-tool queries, multi-version table output, scan write-back, workspace-check write-back, and add-server scan write-back.
- Run passwordless setup tests for existing private/public key files and missing-key guidance.
- Run configure script tests for Windows batch and POSIX shell where the shell runner is available.
- Run request and path-boundary tests for upload, mkdir, delete, command risk summaries, invalid paths, and missing `--execute`.
- Run upload-root tests for default roots, external configured roots, `${cwd}`, sensitive path confirmation, and tampered request JSON.
- Run real SSH file tests for workspace check, upload, list, stat, download, command request, and cleanup when network access is allowed.
- Run wrapper tests for `.bat`, `.ps1`, and `.sh` entry points where the runner exists.
- Run isolated no-ref validation from a temporary copy that does not contain a repository-level `ref` directory.
- Confirm `--with-ssh` requires an explicit real `--server-list`; without one, only offline skill development confidence can be claimed.
- Confirm `git status` is clean after validation.
